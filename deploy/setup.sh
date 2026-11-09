#!/usr/bin/env bash
# JS Entertainment (desk) — one-shot server setup for a fresh Ubuntu/Debian Hetzner box.
# Run as root ON THE SERVER, after the code is in /opt/jsent-desk:
#     bash /opt/jsent-desk/deploy/setup.sh <admin-password> [domain]
# Without a domain it serves over HTTP on the server IP (deploy now);
# re-run with the domain later to switch on automatic HTTPS:
#     bash /opt/jsent-desk/deploy/setup.sh 'my-strong-pass'                       # IP, HTTP
#     bash /opt/jsent-desk/deploy/setup.sh 'my-strong-pass' desk.jsentertainment.co
#
# Idempotent: safe to re-run to update (it reinstalls deps + restarts).
set -euo pipefail

PASSWORD="${1:?usage: setup.sh <admin-password> [domain]}"
DOMAIN="${2:-}"
APP_DIR="/opt/jsent-desk"
ENV_FILE="$APP_DIR/jsent.env"
SERVICE="jsent-desk"
RUN_USER="${SUDO_USER:-root}"   # run the app as the invoking user, not root

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip curl debian-keyring debian-archive-keyring apt-transport-https

echo "==> Installing Caddy (auto-HTTPS reverse proxy)"
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  apt-get update -y
  apt-get install -y caddy
fi

echo "==> Python virtualenv + dependencies"
cd "$APP_DIR"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip -q
./.venv/bin/pip install -r requirements.txt -q

echo "==> Writing environment file (preserves an existing secret)"
if [ -f "$ENV_FILE" ] && grep -qE '^(JSENT|TRENDY)_SECRET=' "$ENV_FILE"; then
  SECRET="$(grep -E '^(JSENT|TRENDY)_SECRET=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
else
  SECRET="$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9')"
fi
cat > "$ENV_FILE" <<EOF
JSENT_PASSWORD=$PASSWORD
JSENT_SECRET=$SECRET
PORT=8782
# Add data-source keys here later (then: systemctl restart $SERVICE):
# YOUTUBE_API_KEY=
# TWITCH_CLIENT_ID=
# TWITCH_CLIENT_SECRET=
# APIFY_TOKEN=
# ANTHROPIC_API_KEY=
EOF
chmod 600 "$ENV_FILE"

echo "==> Ownership ($RUN_USER)"
chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

echo "==> systemd service (gunicorn, restarts on boot/crash)"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=JS Entertainment — Desk
After=network.target

[Service]
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/gunicorn app:app --bind 127.0.0.1:8782 --workers 2 --timeout 120
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"

# Caddy: write OUR site into its own file under /etc/caddy/sites/ and make sure the
# main Caddyfile imports that directory, so other sites on the same box are left alone.
SITES_DIR=/etc/caddy/sites
mkdir -p "$SITES_DIR"
if [ -n "$DOMAIN" ]; then
  echo "==> Caddy reverse proxy + automatic HTTPS for $DOMAIN"
  cat > "$SITES_DIR/$SERVICE.caddy" <<EOF
$DOMAIN {
    reverse_proxy 127.0.0.1:8782
}
EOF
  ACCESS="https://$DOMAIN"
else
  echo "==> Caddy reverse proxy on :80 (HTTP, reachable by server IP)"
  cat > "$SITES_DIR/$SERVICE.caddy" <<EOF
:80 {
    reverse_proxy 127.0.0.1:8782
}
EOF
  ACCESS="http://<server-ip>"
fi
if ! grep -q "import $SITES_DIR/\*.caddy" /etc/caddy/Caddyfile 2>/dev/null; then
  printf '\nimport %s/*.caddy\n' "$SITES_DIR" >> /etc/caddy/Caddyfile
fi
caddy validate --config /etc/caddy/Caddyfile >/dev/null
systemctl reload caddy || systemctl restart caddy

echo ""
echo "==> DONE."
echo "    The desk is live at: $ACCESS  (admin login uses your password)"
echo "    Creator links:       $ACCESS/me/<id>"
echo "    Check status:        systemctl status $SERVICE"
echo "    View logs:           journalctl -u $SERVICE -f"
