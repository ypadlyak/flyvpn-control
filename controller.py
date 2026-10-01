"""Headless control layer around fly-vpn's VPNSession.

The Unraid box is the *orchestrator*, not a VPN consumer: it spins the
ephemeral Fly exit-node up/down but never routes its own traffic. The phones
(Tailscale app) are the consumers and pick the auto-approved exit node.

Switching country is a single operation: ``preflight()`` destroys the old Fly
app + machine and recreates a fresh app, then ``launch(region)`` boots a new
exit node in the chosen region. We never call ``wait_and_connect()`` (that is
the part that would route *this* machine's traffic through the node).
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass

from flyexit.constants import FLY_REGIONS
from flyexit.fly_ops import destroy_app
from flyexit.session import LaunchStatus, PreflightStatus, VPNSession

# code -> "🇳🇱 Amsterdam"
REGION_LABELS: dict[str, str] = dict(FLY_REGIONS)

FLY_ORG = os.environ.get("FLY_ORG", "personal")
# Tailscale auth: an OAuth client (never expires) is preferred; the legacy
# TAILSCALE_API_KEY (expires after <= 90 days) is the fallback.
TS_OAUTH_CLIENT_ID = os.environ.get("TS_OAUTH_CLIENT_ID", "")
TS_OAUTH_CLIENT_SECRET = os.environ.get("TS_OAUTH_CLIENT_SECRET", "")
TS_API_KEY = os.environ.get("TAILSCALE_API_KEY", "")
TS_AUTH_CONFIGURED = bool((TS_OAUTH_CLIENT_ID and TS_OAUTH_CLIENT_SECRET) or TS_API_KEY)
TS_LOGIN_SERVER = os.environ.get("TS_LOGIN_SERVER", "")  # Headscale, optional
VM_MEMORY = int(os.environ.get("VM_MEMORY", "512"))

# Auto-teardown: stop a node after this many hours up (cost cap). 0 disables.
MAX_NODE_HOURS = float(os.environ.get("MAX_NODE_HOURS", "8"))


class ControllerError(RuntimeError):
    """A switch/stop operation failed; message is user-facing."""


@dataclass(slots=True)
class Status:
    active: bool
    region: str | None = None
    region_label: str | None = None
    state: str | None = None  # Fly machine state, e.g. "started"


@dataclass(slots=True)
class Check:
    """One credential/scope validation result."""

    name: str
    ok: bool
    detail: str = ""


# Refresh OAuth access tokens this many seconds before they expire (they last 1 h).
_TOKEN_REFRESH_MARGIN = 60
_token_lock = threading.Lock()
_token = ""
_token_expires_at = 0.0


def _ts_token() -> str:
    """Return a Tailscale API bearer token: a fresh OAuth access token, else the API key.

    OAuth tokens are cached until shortly before expiry. Raises on a failed
    token exchange. Returns "" if no Tailscale auth is configured.
    """
    global _token, _token_expires_at  # noqa: PLW0603
    if not (TS_OAUTH_CLIENT_ID and TS_OAUTH_CLIENT_SECRET):
        return TS_API_KEY

    import httpx

    with _token_lock:
        if time.monotonic() < _token_expires_at - _TOKEN_REFRESH_MARGIN:
            return _token
        resp = httpx.post(
            "https://api.tailscale.com/api/v2/oauth/token",
            data={
                "client_id": TS_OAUTH_CLIENT_ID,
                "client_secret": TS_OAUTH_CLIENT_SECRET,
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        _token = body["access_token"]
        _token_expires_at = time.monotonic() + int(body.get("expires_in", 3600))
        return _token


def _new_session() -> VPNSession:
    # A session lives only for one switch() (well under the 1 h token TTL), so a
    # token fetched here stays valid for the session's lifetime.
    if TS_LOGIN_SERVER:
        return VPNSession(ts_login_server=TS_LOGIN_SERVER)
    try:
        token = _ts_token()
    except Exception as exc:  # noqa: BLE001
        raise ControllerError(f"Tailscale OAuth token exchange failed: {exc}") from exc
    return VPNSession(ts_api_key=token)


def _purge_exit_devices() -> int:
    """Delete every tailnet device named ``fly-vpn-exit`` (SaaS only).

    Keeps the exit node's name stable across country switches: a lingering
    offline ephemeral device would otherwise force the next node to register as
    ``fly-vpn-exit-1``. Best-effort — never raises. Returns the count removed.
    """
    if not TS_AUTH_CONFIGURED or TS_LOGIN_SERVER:
        return 0

    import httpx

    from flyexit.constants import TS_EXIT_HOSTNAME
    from flyexit.tailscale_api import TailscaleAPIClient

    removed = 0
    try:
        token = _ts_token()
        client = TailscaleAPIClient(token)
        resp = httpx.get(
            "https://api.tailscale.com/api/v2/tailnet/-/devices",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        for d in resp.json().get("devices", []):
            hostname = d.get("hostname", "")
            name = d.get("name", "")
            if hostname == TS_EXIT_HOSTNAME or name.startswith(f"{TS_EXIT_HOSTNAME}."):
                device_id = d.get("id") or d.get("nodeId")
                if device_id and client.delete_device(device_id):
                    removed += 1
    except Exception:  # noqa: BLE001, S110
        pass
    return removed


def _resolve_app_name() -> str:
    """Return a stable, globally-unique Fly app name.

    Fly app names are globally unique across all accounts, so the upstream
    default ``fly-vpn-node`` (and even ``fly-vpn-personal``) is usually already
    taken. We can't reliably derive the org slug from an org-scoped Machines
    token on a fresh account, so instead we mint a random unique name once and
    persist it in the keystore (``$HOME/.fly_vpn.db`` → mounted /data), keeping
    it stable across restarts. ``FLY_APP_NAME`` overrides for a chosen name.
    """
    from flyexit import keystore

    stored = keystore.get("flyvpn_control_app")
    if stored:
        return stored

    name = os.environ.get("FLY_APP_NAME", "").strip() or f"fly-vpn-{secrets.token_hex(4)}"
    keystore.set("flyvpn_control_app", name)
    return name


class Controller:
    """Serialises all VPN operations behind one lock — one switch at a time."""

    def __init__(self) -> None:
        # Coarse lock: a country switch takes a few seconds and must not overlap
        # with another switch/stop, or two people tapping at once corrupt state.
        self._lock = threading.Lock()
        self._app_name = _resolve_app_name()
        # Wall-clock time the current node was launched (for max-age auto-stop).
        # None when nothing is running. Boot-reconcile guarantees consistency:
        # any node from a previous process is destroyed, so a live process never
        # has an unknown launch time.
        self._launched_at: float | None = None

    # -- validation --------------------------------------------------------

    def validate(self) -> list[Check]:
        """Validate Fly + Tailscale credentials and scopes.

        Cheap and (mostly) non-destructive — meant to run at startup so
        credential problems surface before the first country switch rather
        than as a cryptic failure mid-tap. The Tailscale ACL check is
        idempotent: it also ensures exit-node auto-approval is configured.
        """
        checks: list[Check] = [self._check_fly()]
        checks.extend(self._check_tailscale())
        return checks

    @staticmethod
    def _check_fly() -> Check:
        from flyexit.fly_api import get_client

        client = get_client()
        if client is None:
            return Check("fly_auth", False, "No Fly token (set FLY_API_TOKEN).")
        try:
            ok, slug = client.check_auth()
        finally:
            client.close()
        if not ok:
            return Check("fly_auth", False, "Token invalid or expired.")
        return Check("fly_auth", True, f"org: {slug or '?'}")

    @staticmethod
    def _check_tailscale() -> list[Check]:
        if TS_LOGIN_SERVER:
            return [Check("tailscale", True, "Headscale mode — SaaS API checks skipped.")]
        if not TS_AUTH_CONFIGURED:
            return [
                Check(
                    "tailscale_key",
                    False,
                    "No Tailscale auth set (TS_OAUTH_CLIENT_ID/SECRET or TAILSCALE_API_KEY).",
                )
            ]

        from flyexit.acl_setup import setup_acl
        from flyexit.tailscale_api import TailscaleAPIClient

        try:
            client = TailscaleAPIClient(_ts_token())
        except Exception as exc:  # noqa: BLE001
            return [Check("tailscale_oauth", False, f"OAuth token exchange failed: {exc}")]

        # 1. ACL read — proves the key is valid and has ACL read scope.
        try:
            client.get_acl()
        except Exception as exc:  # noqa: BLE001
            return [Check("tailscale_acl_read", False, f"cannot read ACL: {exc}")]
        read = Check("tailscale_acl_read", True, "ok")

        # 2. ACL write — setup_acl is idempotent and also ensures the
        #    exit-node auto-approver exists (needs ACL write scope).
        wrote = setup_acl(client)
        write = Check(
            "tailscale_acl_setup",
            wrote,
            "auto-approve configured" if wrote else "ACL write failed (need write scope)",
        )
        return [read, write]

    # -- queries -----------------------------------------------------------

    def status(self) -> Status:
        """Live status straight from the Fly Machines API (survives restarts)."""
        from flyexit.fly_api import get_client

        client = get_client()
        if client is None:
            return Status(active=False)
        try:
            machines = client.list_machines(self._app_name)
        finally:
            client.close()

        for m in machines:
            state = m.get("state", "")
            if state in ("destroying", "destroyed"):
                continue
            region = m.get("region")
            return Status(
                active=True,
                region=region,
                region_label=REGION_LABELS.get(region, region),
                state=state,
            )
        return Status(active=False)

    # -- mutations ---------------------------------------------------------

    def switch(self, region: str) -> Status:
        """Tear down whatever is running and bring up an exit node in *region*."""
        if region not in REGION_LABELS:
            raise ControllerError(f"Unknown region '{region}'.")
        if not TS_AUTH_CONFIGURED and not TS_LOGIN_SERVER:
            raise ControllerError(
                "No Tailscale auth configured "
                "(set TS_OAUTH_CLIENT_ID + TS_OAUTH_CLIENT_SECRET)."
            )

        with self._lock:
            session = _new_session()

            # preflight: checks tailscale CLI + Fly auth, ensures ACL has
            # exit-node auto-approval, and destroys+recreates the Fly app
            # (this is what removes the previous country's machine). We pass our
            # own globally-unique app name so preflight doesn't re-derive it.
            pf = session.preflight(self._app_name, FLY_ORG)
            if pf.status is not PreflightStatus.OK:
                raise ControllerError(self._explain_preflight(pf))
            self._app_name = pf.app_name

            # Remove the previous (now-destroyed) exit-node device so the new one
            # reclaims the exact hostname `fly-vpn-exit` instead of getting a
            # `-1`/`-2` suffix from the lingering ephemeral device.
            _purge_exit_devices()

            lr = session.launch(pf.app_name, region, vm_memory=VM_MEMORY)
            if lr.status is not LaunchStatus.OK:
                detail = lr.error or "machine failed to start"
                if lr.hint:
                    detail = f"{detail} — {lr.hint}"
                raise ControllerError(detail)

            # Deliberately NOT calling session.wait_and_connect(): the phones
            # are the consumers, not this box. The node is already an
            # auto-approved exit node the moment it joins the tailnet.
            self._launched_at = time.time()
            return Status(
                active=True,
                region=region,
                region_label=REGION_LABELS.get(region, region),
                state="started",
            )

    def stop(self) -> Status:
        """Destroy the Fly app (and its machine), then remove the tailnet device."""
        with self._lock:
            destroy_app(self._app_name)
            # Remove the device now rather than waiting for ephemeral cleanup, so
            # it doesn't linger as a stale "offline" entry in the exit-node picker.
            _purge_exit_devices()
            self._launched_at = None
            return Status(active=False)

    # -- auto-teardown -----------------------------------------------------

    def reconcile_boot(self) -> bool:
        """Kill-switch: destroy any node left over from a previous process.

        Called once at startup. A graceful stop tears the node down, but a hard
        crash (SIGKILL) or host reboot can leave a Fly machine billing. We can't
        know its launch time, so the safe move is to destroy it — the node is
        cheap to re-launch with a tap.  Returns True if something was cleaned up.
        """
        from flyexit.fly_api import get_client

        client = get_client()
        if client is None:
            return False
        try:
            existed = client.app_exists(self._app_name)
        finally:
            client.close()
        if existed:
            destroy_app(self._app_name)
        self._launched_at = None
        return existed

    def autostop_check(self) -> str | None:
        """Stop the node if it has exceeded its max age. Returns a reason or None.

        Meant to be polled periodically. Idle-based stopping isn't implemented:
        the orchestrator can't see phone-through-node traffic, and it's always
        online itself, so there's no reliable idle signal — max age is the cap.
        """
        started = self._launched_at
        if started is None or MAX_NODE_HOURS <= 0:
            return None
        if time.time() - started >= MAX_NODE_HOURS * 3600:
            self.stop()
            return f"max age {MAX_NODE_HOURS:g}h reached"
        return None

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _explain_preflight(pf) -> str:  # noqa: ANN001
        msgs = {
            PreflightStatus.TAILSCALE_MISSING: (
                "Tailscale CLI not found in the container."
            ),
            PreflightStatus.AUTH_FAILED: "Fly.io auth failed — check FLY_API_TOKEN.",
            PreflightStatus.APP_FAILED: "Could not create the Fly app.",
        }
        base = msgs.get(pf.status, "Preflight failed.")
        return f"{base} {pf.error}".strip()
