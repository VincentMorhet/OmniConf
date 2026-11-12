#!/usr/bin/env bash
# Installe la VM de contrôle. Usage (root) :
#   AGENT_TOKEN=xxx WORKERS="w1=http://192.168.0.231:8001,w2=http://192.168.0.232:8001" \
#   ADMIN_PASSWORD=... SITE_ADDRESS="https://192.168.0.230" ./install-control.sh
# TLS_MODE=acme (optionnel) -> certificat public Let's Encrypt pour SITE_ADDRESS=https://nom.de.domaine
#   (serveur joignable depuis Internet sur 80/443, ex. chez OVH : voir deploy/ovh/)
# TS_HOSTNAME=webconf.<tailnet>.ts.net (optionnel) -> certificat HTTPS valide via Tailscale (voir deploy/enable-tailscale.sh)
set -euo pipefail
: "${AGENT_TOKEN:?}" "${WORKERS:?}"
SRC=$(cd "$(dirname "$0")" && pwd)
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3-venv novnc caddy curl
id webconf >/dev/null 2>&1 || useradd -r -m -d /var/lib/webconf -s /usr/sbin/nologin webconf
mkdir -p /opt/webconf /etc/webconf /var/lib/webconf
rm -rf /opt/webconf/control && cp -r "$SRC" /opt/webconf/control
[ -d /opt/webconf/venv ] || python3 -m venv /opt/webconf/venv
/opt/webconf/venv/bin/pip install -q --upgrade pip
/opt/webconf/venv/bin/pip install -q -r /opt/webconf/control/requirements.txt
chown -R webconf:webconf /var/lib/webconf

if [ ! -f /etc/webconf/control.env ]; then
  ADMIN_PASSWORD=${ADMIN_PASSWORD:-$(python3 -c 'import secrets;print(secrets.token_urlsafe(12))')}
  cat > /etc/webconf/control.env <<ENV
ADMIN_PASSWORD=$ADMIN_PASSWORD
SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
AGENT_TOKEN=$AGENT_TOKEN
WORKERS=$WORKERS
ICE_SERVERS=${ICE_SERVERS:-[]}
DATA_DIR=/var/lib/webconf
SITE_ADDRESS=${SITE_ADDRESS:-https://$(hostname -I | awk '{print $1}')}
TS_HOSTNAME=${TS_HOSTNAME:-}
TLS_MODE=${TLS_MODE:-internal}
ENV
  echo ">>> Mot de passe IHM : $ADMIN_PASSWORD"
fi
chmod 640 /etc/webconf/control.env; chgrp webconf /etc/webconf/control.env
. /etc/webconf/control.env
# Bloc principal (certificat interne Caddy, ou Let's Encrypt si TLS_MODE=acme)
# + bloc Tailscale optionnel (certificat valide, requis pour iPhone/iPad)
{
  echo "$SITE_ADDRESS {"; [ "${TLS_MODE:-internal}" = acme ] || echo "	tls internal"; echo "	encode gzip"; echo "	reverse_proxy 127.0.0.1:8000"; echo "}"
  if [ -n "${TS_HOSTNAME:-}" ]; then
    echo "https://$TS_HOSTNAME {"; echo "	encode gzip"; echo "	reverse_proxy 127.0.0.1:8000"; echo "}"
  fi
} > /etc/caddy/Caddyfile
cp /opt/webconf/control/system/webconf-control.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now webconf-control
systemctl restart webconf-control caddy
# Certificat racine de Caddy (tls internal), publié sur /ca.crt pour les postes clients (Claude Code / serveur MCP)
CA=/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
for i in $(seq 1 20); do [ -f $CA ] && break; sleep 1; done
[ -f $CA ] && install -m 644 -o webconf -g webconf $CA "${DATA_DIR:-/var/lib/webconf}/caddy-root.crt" || true
echo "IHM : $SITE_ADDRESS"
