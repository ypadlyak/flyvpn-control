# flyvpn-control

A tiny **web panel + Telegram bot** that drives [invilso/fly-vpn](https://github.com/invilso/fly-vpn)
headlessly from a home server, so anyone in the house can switch the exit-node country
fast — tap a flag, done. Built for the "I want Netflix in another country *now*" case.

```
 phone ──▶ web panel / Telegram bot ──▶ flyvpn-control (home server) ──▶ Fly.io machine (exit node)
                                                                              │
 phone (Tailscale app) ◀───────────── auto-approved exit node ◀──────────────┘
```

The server is only the **orchestrator** — it spins exit nodes up/down but never routes
its own traffic. Your **phones/laptops are the consumers**: the Tailscale app selects
the exit node.

It runs anywhere Docker runs — a Raspberry Pi, a NAS, a VPS, an old laptop, or Unraid.
A single container holds both the web panel and the Telegram bot.

## How switching works

1. You tap a country in the web panel or Telegram.
2. flyvpn-control runs fly-vpn's `preflight()` (destroys the old Fly app + machine,
   ensures the Tailscale ACL auto-approves the exit node) then `launch(region)`
   (boots a fresh `tailscale/tailscale` machine in that region, ~5 s).
3. The node joins your tailnet as an **auto-approved exit node** named `fly-vpn-exit`.
4. On each device, open the **Tailscale app → exit node → fly-vpn-exit**.

> ⚠️ Each switch recreates the node, so your devices must **re-pick the exit node** in
> the Tailscale app after a country change (two taps). fly-vpn runs one node at a time;
> the node name stays `fly-vpn-exit`, so the picker never clutters with stale entries.

## Prerequisites

- A **home server** (or any always-on box) with **Docker**.
- A **Fly.io** account with a payment method, and an API token
  (`flyctl tokens create org -o personal`, or copy `access_token` from `~/.fly/config.yml`).
- A **Tailscale** account + Admin **API key** with ACL read/write, auth-key create,
  and devices write scopes: <https://login.tailscale.com/admin/settings/keys>.
- The official **Tailscale app** on each device (iOS/Android/desktop), all logged into
  the same tailnet.

## Deploy

### Option A — Docker Compose (recommended, works on any host)

```bash
git clone https://github.com/ypadlyak/flyvpn-control.git
cd flyvpn-control
cp .env.example .env          # then edit .env and fill in your tokens
docker compose up -d --build
```

Open `http://<server>:8787`. (See *Reaching the panel* below for the right address to
bookmark on phones.)

### Option B — plain `docker run` (no compose)

```bash
docker build -t flyvpn-control .
docker run -d --name flyvpn-control --restart unless-stopped \
  -p 8787:8787 \
  -v "$PWD/data:/data" \
  --env-file .env \
  flyvpn-control
```

`--env-file .env` reads the same file as compose. On hosts without `docker compose`
(e.g. a stock Unraid or a minimal box), this is the simplest path.

### Option C — Unraid

- **Compose Manager plugin:** put this folder on a share and run Option A from its
  console.
- **Docker tab / Community Applications:** use `unraid-template/flyvpn-control.xml`
  (replace the `ypadlyak` namespace with your own image, or build locally). It maps port
  `8787`, the `/data` volume to `/mnt/user/appdata/flyvpn-control`, and exposes every env
  var below as a template field.

### Pre-built image

CI publishes a multi-arch image (amd64 + arm64) to
`ghcr.io/ypadlyak/flyvpn-control`. Swap `build:`/`docker build` for that image to skip
building on the host. (It's a private package by default — make it public or log in to
pull.)

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `FLY_API_TOKEN` | yes | — | Fly.io API token (app create/destroy). |
| `FLY_ORG` | yes | `personal` | Fly organisation slug. |
| `TS_OAUTH_CLIENT_ID` | yes¹ | — | Tailscale OAuth client ID (see scopes below). |
| `TS_OAUTH_CLIENT_SECRET` | yes¹ | — | Tailscale OAuth client secret. |
| `TAILSCALE_API_KEY` | no | — | Legacy: Tailscale Admin API key, used only if no OAuth client is set. Expires after ≤90 days. |
| `TELEGRAM_BOT_TOKEN` | no | — | Enables the Telegram bot if set. |
| `TELEGRAM_ALLOWED_IDS` | no² | — | Comma-separated chat IDs allowed to control the VPN. |
| `FLY_APP_NAME` | no | auto | Override the (globally-unique) Fly app name; auto-generated and persisted otherwise. |
| `VM_MEMORY` | no | `512` | Memory (MB) for the exit-node machine. |
| `MAX_NODE_HOURS` | no | `8` | Auto-stop the node after N hours (cost cap; `0` disables). |
| `MONITOR_INTERVAL` | no | `120` | Seconds between max-age checks. |
| `PORT` | no | `8787` | Web panel port. |
| `TS_LOGIN_SERVER` | no | — | Headscale server URL (self-hosted control server). |

¹ Not needed if using a Headscale `TS_LOGIN_SERVER`.

**Tailscale OAuth client:** create one at
<https://login.tailscale.com/admin/settings/oauth> with these scopes:

- Keys → **Auth Keys: Write**, tag `tag:ephemeral-vpn`
- Devices → **Core: Write**, tag `tag:ephemeral-vpn`
- General → **Policy File: Write**

`tag:ephemeral-vpn` must be listed in `tagOwners` in your tailnet policy. Unlike
API keys, OAuth clients don't expire. The controller swaps the client for
short-lived (1 h) access tokens and refreshes them automatically.
² Required *in practice* if the bot is enabled — the bot refuses to start without it.

## Telegram bot (optional, control from anywhere)

1. Message **@BotFather** → `/newbot` → put the token in `TELEGRAM_BOT_TOKEN`.
2. Each person messages **@userinfobot** to get their numeric chat ID; put them
   (comma-separated) in `TELEGRAM_ALLOWED_IDS`.
3. Restart the container. In the chat: `/start` → tap a country; ⏹ Stop appears only
   while a node is running.

> The bot **refuses to start** with an empty `TELEGRAM_ALLOWED_IDS` (deny-by-default) —
> otherwise anyone who found the bot could control your VPN.

## Verifying your tokens

On startup the container logs a credential check, e.g.:

```
✅ fly_auth              org: personal
✅ tailscale_acl_read    ok
✅ tailscale_acl_setup   auto-approve configured
```

A ❌ means switching won't work until you fix that token/scope. Re-check any time at
**`http://<host>:8787/healthz`** — `200` when all green, `503` otherwise. The Tailscale
check is idempotent and also ensures the exit-node auto-approver exists.

## Reaching the panel while an exit node is active

When a device selects the exit node, Tailscale routes **all** its traffic through the
node — including to your home LAN. So open the panel at the **server's Tailscale
address**, not its LAN IP: traffic between tailnet peers goes direct and bypasses the
exit node, so `http://<server>.<tailnet>.ts.net:8787` (or the `100.x` Tailscale IP)
stays reachable. Bookmark *that* on phones. (Alternatively, enable "Allow local network
access" in the Tailscale exit-node settings.)

The Telegram bot is unaffected — it runs on the server and reaches Telegram over the
server's own connection regardless of which exit node a device uses.

## Securing the web panel

The panel has no login. Keep it private by **only exposing it on your tailnet** — don't
port-forward 8787 to the internet. The Telegram bot works from anywhere and is
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
