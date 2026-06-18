"""Web panel + Telegram bot for the fly-vpn controller.

Both front-ends call the same Controller. Long (blocking) Fly/Tailscale calls
run in a worker thread so the async event loop stays responsive.

Env:
  FLY_API_TOKEN          Fly.io API token            (required)
  TAILSCALE_API_KEY      Tailscale Admin API key     (required for SaaS)
  TELEGRAM_BOT_TOKEN     enable the bot if set        (optional)
  TELEGRAM_ALLOWED_IDS   comma-separated chat IDs     (required if bot on)
  PORT                   web panel port (default 8787)
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from controller import FLY_REGIONS, Controller, ControllerError, Status

logging.basicConfig(level=logging.INFO, format="%(asctime)s [flyvpn] %(message)s")
log = logging.getLogger("flyvpn")

# httpx/httpcore log full request URLs at INFO — and python-telegram-bot puts
# the bot token in the URL path, which would leak it into the logs. Mute them.
for _noisy in ("httpx", "httpcore", "telegram", "telegram.ext"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

ctrl = Controller()

PORT = int(os.environ.get("PORT", "8787"))
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_IDS = {
    int(x) for x in os.environ.get("TELEGRAM_ALLOWED_IDS", "").replace(" ", "").split(",") if x
}


# ---------------------------------------------------------------------------
# Web panel
# ---------------------------------------------------------------------------

def _page(status: Status) -> str:
    if status.active:
        banner = f"🟢 Connected — <b>{status.region_label}</b> <span class=dim>({status.state})</span>"
    else:
        banner = "⚪ No exit node running"

    buttons = "".join(
        f'<button class="region" onclick="go(\'{code}\')" '
        f'data-code="{code}">{label}</button>'
        for code, label in FLY_REGIONS
    )

    return f"""<!doctype html><html><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Fly VPN</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: system-ui, sans-serif; margin: 0; background:#0e0f13; color:#e8e8ea;
         padding: 16px; max-width: 640px; margin: 0 auto; }}
  h1 {{ font-size: 1.2rem; margin: 8px 0 4px; }}
  .dim {{ color:#8a8a93; font-weight:400; }}
  #banner {{ background:#1a1b22; border:1px solid #2a2c36; border-radius:12px;
             padding:14px 16px; margin:12px 0 18px; font-size:1.05rem; }}
  .grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:10px; }}
  button.region {{ font-size:1.05rem; padding:16px 10px; border-radius:12px;
            border:1px solid #2a2c36; background:#171922; color:#e8e8ea; text-align:left; }}
  button.region.active {{ border-color:#3fb950; background:#11231a; }}
  button:active {{ transform:scale(.98); }}
  #stop {{ width:100%; margin-top:16px; padding:16px; font-size:1.05rem;
           border-radius:12px; border:1px solid #5c2b2b; background:#2a1414; color:#ff9b9b; }}
  #stop.hidden {{ display:none; }}
  #toast {{ position:fixed; left:50%; bottom:24px; transform:translateX(-50%);
            background:#1a1b22; border:1px solid #2a2c36; padding:12px 18px;
            border-radius:10px; opacity:0; transition:opacity .2s; max-width:90%; }}
  #toast.show {{ opacity:1; }}
  .busy {{ opacity:.5; pointer-events:none; }}
</style></head><body>
<h1>Fly VPN <span class=dim>· exit node</span></h1>
<div id=banner>{banner}</div>
<div class=grid id=grid>{buttons}</div>
<button id=stop class="{"" if status.active else "hidden"}" onclick="stop()">⏹ Stop VPN</button>
<div id=toast></div>
<script>
const active = {('"'+status.region+'"') if status.region else "null"};
function mark() {{
  document.querySelectorAll('.region').forEach(b =>
    b.classList.toggle('active', b.dataset.code === window._active));
}}
window._active = active; mark();
function toast(m) {{ const t=document.getElementById('toast'); t.textContent=m;
  t.classList.add('show'); setTimeout(()=>t.classList.remove('show'), 3500); }}
async function call(url) {{
  document.body.classList.add('busy');
  toast('Working…');
  try {{
    const r = await fetch(url, {{method:'POST'}});
    const d = await r.json();
    if (!r.ok) {{ toast('⚠ ' + (d.detail||'failed')); return; }}
    window._active = d.active ? d.region : null; mark();
    document.getElementById('stop').classList.toggle('hidden', !d.active);
    document.getElementById('banner').innerHTML = d.active
      ? '🟢 Connected — <b>'+d.region_label+'</b>'
      : '⚪ No exit node running';
    toast(d.active ? ('✅ '+d.region_label+' — re-pick the exit node in the Tailscale app')
                   : '⏹ Stopped');
  }} catch(e) {{ toast('⚠ '+e); }}
  finally {{ document.body.classList.remove('busy'); }}
}}
const go = c => call('/switch/'+c);
const stop = () => call('/stop');
</script>
</body></html>"""


app = FastAPI()


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    status = await asyncio.to_thread(ctrl.status)
    return _page(status)


@app.get("/status")
async def status_json() -> JSONResponse:
    s = await asyncio.to_thread(ctrl.status)
    return JSONResponse(dataclasses.asdict(s))


@app.post("/switch/{region}")
async def switch(region: str) -> JSONResponse:
    try:
        s = await asyncio.to_thread(ctrl.switch, region)
    except ControllerError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return JSONResponse(dataclasses.asdict(s))


@app.post("/stop")
async def stop() -> JSONResponse:
    s = await asyncio.to_thread(ctrl.stop)
    return JSONResponse(dataclasses.asdict(s))


@app.get("/healthz")
async def healthz() -> JSONResponse:
    checks = await asyncio.to_thread(ctrl.validate)
    ok = all(c.ok for c in checks)
    return JSONResponse(
        {"ok": ok, "checks": [dataclasses.asdict(c) for c in checks]},
        status_code=200 if ok else 503,
    )


async def _log_credential_checks() -> None:
    checks = await asyncio.to_thread(ctrl.validate)
    for c in checks:
        mark = "✅" if c.ok else "❌"
        log.info("%s %-22s %s", mark, c.name, c.detail)
    if not all(c.ok for c in checks):
        log.warning(
            "One or more credential checks failed — switching will not work "
            "until these are fixed. See /healthz."
        )


# ---------------------------------------------------------------------------
# Telegram bot (optional)
# ---------------------------------------------------------------------------

def _build_bot():  # noqa: ANN202
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import (
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
    )

    def allowed(update: Update) -> bool:
        # Deny-by-default: no empty-allowlist bypass. The bot won't even start
        # without ALLOWED_IDS (see _startup), but guard here too.
        chat = update.effective_chat
        return bool(chat) and chat.id in ALLOWED_IDS

    def menu() -> InlineKeyboardMarkup:
        rows, row = [], []
        for code, label in FLY_REGIONS:
            row.append(InlineKeyboardButton(label, callback_data=f"sw:{code}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("⏹ Stop VPN", callback_data="stop")])
        return InlineKeyboardMarkup(rows)

    async def start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not allowed(update):
            await update.message.reply_text("⛔ Not authorised.")
            return
        s = await asyncio.to_thread(ctrl.status)
        head = (
            f"🟢 Connected — {s.region_label}" if s.active else "⚪ No exit node running"
        )
        await update.message.reply_text(
            f"{head}\n\nPick a country:", reply_markup=menu()
        )

    async def on_button(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        if not allowed(update):
            await q.edit_message_text("⛔ Not authorised.")
            return
        data = q.data
        try:
            if data == "stop":
                await q.edit_message_text("⏹ Stopping…")
                await asyncio.to_thread(ctrl.stop)
                await q.edit_message_text("⏹ VPN stopped.", reply_markup=menu())
                return
            code = data.split(":", 1)[1]
            await q.edit_message_text(f"🚀 Switching to {code}…")
            s = await asyncio.to_thread(ctrl.switch, code)
            await q.edit_message_text(
                f"✅ {s.region_label}\nRe-pick the exit node in the Tailscale app.",
                reply_markup=menu(),
            )
        except ControllerError as e:
            await q.edit_message_text(f"⚠ {e}", reply_markup=menu())

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler(["start", "menu", "vpn"], start))
    application.add_handler(CommandHandler("status", start))
    application.add_handler(CallbackQueryHandler(on_button))
    return application


async def _monitor_loop() -> None:
    """Poll the controller for auto-stop conditions (max-age cost cap)."""
    interval = int(os.environ.get("MONITOR_INTERVAL", "120"))
    while True:
        await asyncio.sleep(interval)
        try:
            reason = await asyncio.to_thread(ctrl.autostop_check)
        except Exception as exc:  # noqa: BLE001
            log.warning("Auto-stop check failed: %s", exc)
            continue
        if reason:
            log.info("🛑 Auto-stopped exit node: %s", reason)


@app.on_event("startup")
async def _startup() -> None:
    await _log_credential_checks()

    # Kill-switch: clean up any node left billing by a previous unclean exit.
    if await asyncio.to_thread(ctrl.reconcile_boot):
        log.info("🧹 Boot reconcile: destroyed a leftover exit node.")

    # Background cost cap.
    app.state.monitor = asyncio.create_task(_monitor_loop())

    if not BOT_TOKEN:
        log.info("TELEGRAM_BOT_TOKEN not set — web panel only.")
        return
    if not ALLOWED_IDS:
        log.error(
            "Refusing to start Telegram bot: TELEGRAM_ALLOWED_IDS is empty. "
            "Set it (comma-separated chat IDs) to enable the bot — web panel still runs."
        )
        return
    bot = _build_bot()
    await bot.initialize()
    await bot.start()
    await bot.updater.start_polling(drop_pending_updates=True)
    app.state.bot = bot
    log.info("Telegram bot polling started.")


@app.on_event("shutdown")
async def _shutdown() -> None:
    # Teardown-on-stop: a graceful `docker stop` (SIGTERM) should not leave a
    # node billing. Hard kills (SIGKILL) are covered by boot reconcile instead.
    monitor = getattr(app.state, "monitor", None)
    if monitor is not None:
        monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor

    try:
        s = await asyncio.to_thread(ctrl.status)
        if s.active:
            log.info("Shutting down — tearing down active exit node (%s).", s.region)
            await asyncio.to_thread(ctrl.stop)
    except Exception as exc:  # noqa: BLE001
        log.warning("Shutdown teardown failed: %s", exc)

    bot = getattr(app.state, "bot", None)
    if bot is None:
        return
    with contextlib.suppress(Exception):
        await bot.updater.stop()
        await bot.stop()
        await bot.shutdown()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)  # noqa: S104
