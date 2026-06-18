FROM python:3.14-slim

# git: pip needs it to install fly-vpn from GitHub.
# ca-certificates: HTTPS to Fly/Tailscale APIs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# fly-vpn's preflight() runs `tailscale version` to confirm the CLI exists.
# We only need the binary present (the box never joins the tailnet), so copy
# it from the official image — no daemon, no auth.
COPY --from=tailscale/tailscale:latest /usr/local/bin/tailscale /usr/local/bin/tailscale

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY controller.py server.py ./

# Persist ~/.fly_vpn.db (usage stats) on a mounted volume.
ENV HOME=/data PORT=8787
VOLUME ["/data"]
EXPOSE 8787

CMD ["python", "server.py"]
