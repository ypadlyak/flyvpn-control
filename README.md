# flyvpn-control

A tiny **web panel + Telegram bot** that drives [invilso/fly-vpn](https://github.com/invilso/fly-vpn)
headlessly from your Unraid server, so you (and your wife) can switch the exit-node
country fast — tap a flag, done. Built for the "I want Netflix in another country
*now*" case.

```
 phone ──▶ web panel / Telegram bot ──▶ flyvpn-control (Unraid) ──▶ Fly.io machine (exit node)
                                                                          │
 phone (Tailscale app) ◀───────────── auto-approved exit node ◀──────────┘
```

The Unraid box is only the **orchestrator** — it spins exit nodes up/down but never
routes its own traffic. Your **phones are the consumers**: the Tailscale Android app
selects the exit node.

## How switching works

1. You tap a country in the web panel or Telegram.
2. flyvpn-control runs fly-vpn's `preflight()` (destroys the old Fly app + machine,
   ensures the Tailscale ACL auto-approves the exit node) then `launch(region)`
   (boots a fresh `tailscale/tailscale` machine in that region, ~5 s).
3. The node joins your tailnet as an **auto-approved exit node** named `fly-vpn-exit`.
4. On each phone, open the **Tailscale app → exit node → fly-vpn-exit**.

> ⚠️ Because each switch tears the node down and brings up a *new* device, your phones
> must **re-pick the exit node** in the Tailscale app after a country change (two taps).
> That's a limitation of fly-vpn today (one node at a time; "switch without teardown"
> is on its roadmap).

## Prerequisites

- A **Fly.io** account + API token (`flyctl tokens create org -o personal`, or copy
  `access_token` from `~/.fly/config.yml`).
- A **Tailscale** account + Admin **API key** with ACL read/write, auth-key create,
  and devices write scopes: <https://login.tailscale.com/admin/settings/keys>.
- The official **Tailscale app** on each Android device, all logged into the same tailnet.
- Unraid with Docker (and the *Compose Manager* plugin if you want compose).

## Deploy on Unraid

### Option A — docker compose (Compose Manager plugin)

1. Copy this folder to e.g. `/boot/config/plugins/compose/flyvpn-control/` or any share.
2. `cp .env.example .env` and fill in your tokens.
3. `docker compose up -d --build`.
4. Open `http://<unraid-ip>:8787` and add it to your phone's home screen.

### Option B — Unraid "Add Container" (Docker tab)

Build/push the image first (`docker build -t youruser/flyvpn-control .` then push), or
point Unraid at this repo. Then add a container with:

| Field | Value |
|-------|-------|
| Repository | `youruser/flyvpn-control:latest` |
| Network | `bridge` |
| Port | `8787` → `8787` |
| Path | `/data` → `/mnt/user/appdata/flyvpn-control` |
| Var `FLY_API_TOKEN` | *your Fly token* |
| Var `FLY_ORG` | `personal` |
| Var `TAILSCALE_API_KEY` | *your TS API key* |
| Var `TELEGRAM_BOT_TOKEN` | *optional* |
| Var `TELEGRAM_ALLOWED_IDS` | *optional, e.g.* `111111,222222` |

## Telegram bot (optional but great for "from anywhere")

1. Message **@BotFather** → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Message **@userinfobot** from each phone to get the numeric chat IDs; put them
   (comma-separated) in `TELEGRAM_ALLOWED_IDS` so only you two can control it.
3. Restart the container. In the chat: `/start` → tap a country, or `⏹ Stop VPN`.

> The bot is reachable by anyone who knows its name — **always set
> `TELEGRAM_ALLOWED_IDS`**. With it empty the bot logs a warning and accepts everyone.

## Verifying your tokens

On startup the container logs a credential check, e.g.:

```
✅ fly_auth              org: personal
✅ tailscale_acl_read    ok
✅ tailscale_acl_setup   auto-approve configured
```

A ❌ means switching won't work until you fix that token/scope. You can re-check any
time at **`http://<host>:8787/healthz`** — returns `200` when all green, `503` otherwise.
The Tailscale check is idempotent and also ensures the exit-node auto-approver exists.

## Reaching the panel while an exit node is active

When a phone selects the exit node, Tailscale routes **all** its traffic through the
node — including to your home LAN. So open the panel at the **server's Tailscale
address**, not its LAN IP: traffic between tailnet peers goes direct and bypasses the
exit node, so `http://<server>.<tailnet>.ts.net:8787` (or the `100.x` Tailscale IP)
stays reachable. Bookmark *that* on the phones. (Alternatively, enable "Allow local
network access" in the Tailscale exit-node settings.)

The Telegram bot is unaffected — it runs on the server and reaches Telegram over the
server's own connection regardless of which exit node a phone uses.

## Securing the web panel

The panel has no login. Keep it private by **only exposing it on your tailnet** —
don't port-forward 8787 to the internet. The Telegram bot works from anywhere and is
access-listed (`TELEGRAM_ALLOWED_IDS`).

## Cost & auto-teardown

Per-second Fly billing, typically well under $1/month for casual use. Stop the node
(`⏹`) when done and billing stops. Three safety nets prevent a forgotten node from
billing forever:

- **Max age** — the node auto-stops after `MAX_NODE_HOURS` (default 8; `0` disables).
- **Graceful stop** — stopping/restarting the container tears the node down.
- **Boot reconcile** — on startup, any node left over from a hard crash is destroyed.

## Files

- `controller.py` — wraps fly-vpn's `VPNSession`: `switch(region)`, `stop()`, `status()`,
  `validate()` (token/scope checks).
- `server.py` — FastAPI web panel + Telegram bot + `/healthz`, one process, one controller.
- `Dockerfile` / `docker-compose.yml` / `.env.example` — deployment.
- `unraid-template/flyvpn-control.xml` — Unraid Community Applications template.
- `.github/workflows/docker-publish.yml` — CI: build + push image to GHCR.
- `LICENSE` — MIT (builds on MIT-licensed fly-vpn).

## Credits

Orchestrates [invilso/fly-vpn](https://github.com/invilso/fly-vpn) (MIT). This project is
a thin headless control layer on top of it.
