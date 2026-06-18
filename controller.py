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
from dataclasses import dataclass

from flyexit.constants import FLY_REGIONS
from flyexit.fly_ops import destroy_app
from flyexit.session import LaunchStatus, PreflightStatus, VPNSession

# code -> "🇳🇱 Amsterdam"
REGION_LABELS: dict[str, str] = dict(FLY_REGIONS)

FLY_ORG = os.environ.get("FLY_ORG", "personal")
TS_API_KEY = os.environ.get("TAILSCALE_API_KEY", "")
TS_LOGIN_SERVER = os.environ.get("TS_LOGIN_SERVER", "")  # Headscale, optional
VM_MEMORY = int(os.environ.get("VM_MEMORY", "512"))


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


def _new_session() -> VPNSession:
    return VPNSession(ts_api_key=TS_API_KEY, ts_login_server=TS_LOGIN_SERVER)


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
        if not TS_API_KEY:
            return [Check("tailscale_key", False, "No TAILSCALE_API_KEY set.")]

        from flyexit.acl_setup import setup_acl
        from flyexit.tailscale_api import TailscaleAPIClient

        client = TailscaleAPIClient(TS_API_KEY)

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
        if not TS_API_KEY and not TS_LOGIN_SERVER:
            raise ControllerError(
                "No Tailscale auth configured (set TAILSCALE_API_KEY)."
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

            lr = session.launch(pf.app_name, region, vm_memory=VM_MEMORY)
            if lr.status is not LaunchStatus.OK:
                detail = lr.error or "machine failed to start"
                if lr.hint:
                    detail = f"{detail} — {lr.hint}"
                raise ControllerError(detail)

            # Deliberately NOT calling session.wait_and_connect(): the phones
            # are the consumers, not this box. The node is already an
            # auto-approved exit node the moment it joins the tailnet.
            return Status(
                active=True,
                region=region,
                region_label=REGION_LABELS.get(region, region),
                state="started",
            )

    def stop(self) -> Status:
        """Destroy the Fly app (and its machine). Ephemeral TS device self-removes."""
        with self._lock:
            destroy_app(self._app_name)
            return Status(active=False)

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
