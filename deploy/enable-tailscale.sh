#!/usr/bin/env bash
# À lancer SUR la VM de contrôle (root). Ajoute la VM au tailnet et active le HTTPS *.ts.net
# (certificat valide -> micro autorisé sur iPhone/iPad). Prérequis : HTTPS activé dans la console
# Tailscale (DNS > HTTPS Certificates).
set -euo pipefail
HOST=${1:-webconf}
curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg -o /usr/share/keyrings/tailscale-archive-keyring.gpg
curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list -o /etc/apt/sources.list.d/tailscale.list
apt-get update -q && apt-get install -y -q tailscale
grep -q TS_PERMIT_CERT_UID /etc/default/tailscaled || echo 'TS_PERMIT_CERT_UID=caddy' >> /etc/default/tailscaled
systemctl restart tailscaled
tailscale up --hostname="$HOST"          # affiche un lien de connexion à ouvrir
FQDN=$(tailscale status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
sed -i '/^TS_HOSTNAME=/d' /etc/webconf/control.env && echo "TS_HOSTNAME=$FQDN" >> /etc/webconf/control.env
cat >> /etc/caddy/Caddyfile <<CADDY
https://$FQDN {
	encode gzip
	reverse_proxy 127.0.0.1:8000
}
CADDY
systemctl reload caddy
echo "IHM accessible sur : https://$FQDN"
