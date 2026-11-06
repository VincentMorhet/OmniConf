#!/usr/bin/env bash
# Installe (ou met à jour) Omniconf sur l'instance OVH créée par Terraform :
# disque de données, workers en conteneurs, puis l'IHM avec un certificat Let's Encrypt.
#
#   cd deploy/ovh && terraform apply && ./deploy-ovh.sh
#
# Secrets : .agent_token et .ovh_token à la racine du dépôt (jamais dans Terraform ni son état).
# Essai sur une VM ordinaire, sans Terraform : IP=… SITE=https://… VOL=- TLS_MODE=internal ./deploy-ovh.sh
# Relancer le script met le code à jour et conserve réglages et données.
set -euo pipefail
cd "$(dirname "$0")"
TF=${TF:-terraform}
TLS=${TLS_MODE:-acme}   # internal : certificat interne (essais sans nom public)
IP=${IP:-$($TF output -raw public_ipv4)}
SITE=${SITE:-$($TF output -raw site_address)}
VOL=${VOL:-$($TF output -raw data_volume_id)}
NAMES=${NAMES:-$($TF output -raw worker_names)}
cd ../..
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new debian@$IP"

echo "=== $IP : attente de la fin du démarrage"
for i in $(seq 60); do $SSH true 2>/dev/null && break; sleep 5; done
$SSH 'cloud-init status --wait >/dev/null 2>&1 || true'

echo "=== $IP : disque de données sur /srv/webconf"
$SSH "sudo VOL=$VOL bash -s" <<'DISK'
set -euo pipefail
if ! blkid -L webconf-data >/dev/null 2>&1; then
  dev=; [ "${#VOL}" -lt 20 ] || dev=$(ls /dev/disk/by-id/*"${VOL:0:20}"* 2>/dev/null | grep -v -- -part | head -1 || true)
  if [ -z "$dev" ]; then  # à défaut : le seul disque vierge (ni partition ni système de fichiers)
    mapfile -t empty < <(lsblk -dnpo NAME,TYPE | awk '$2=="disk"{print $1}' | while read -r d; do
      [ "$(lsblk -no NAME "$d" | wc -l)" = 1 ] && [ -z "$(blkid -o value -s TYPE "$d" || true)" ] && echo "$d"; done)
    [ "${#empty[@]}" = 1 ] || { echo "disque de données introuvable (${#empty[@]} candidats)"; exit 1; }
    dev=${empty[0]}
  fi
  dev=$(readlink -f "$dev")
  [ -z "$(blkid -o value -s TYPE "$dev" || true)" ] || { echo "$dev n'est pas vierge : arrêt"; exit 1; }
  mkfs.ext4 -q -L webconf-data "$dev"
fi
mkdir -p /srv/webconf
grep -q 'LABEL=webconf-data' /etc/fstab || echo 'LABEL=webconf-data /srv/webconf ext4 defaults,nofail 0 2' >> /etc/fstab
mountpoint -q /srv/webconf || mount /srv/webconf
df -h /srv/webconf | tail -1
DISK

echo "=== $IP : workers"
HOST=$IP WORKER_URL_HOST=127.0.0.1 CONTROL_IPS=127.0.0.1 NAMES="$NAMES" bash deploy/deploy-containers.sh

echo "=== $IP : IHM ($SITE)"
WL=""
for n in $NAMES; do WL="${WL:+$WL,}$n=http://127.0.0.1:$($SSH "sudo sed -n 's/^AGENT_PORT=//p' /etc/webconf/containers/$n.env")"; done
T=$(cat .agent_token)
tar --exclude=__pycache__ -czf - control | $SSH 'rm -rf /tmp/control && tar -C /tmp -xzf -'
# Mise à jour : adresse, certificat et liste des workers suivent Terraform (le reste de control.env est conservé)
$SSH "f=/etc/webconf/control.env; [ ! -f \$f ] || { sudo sed -i -e '/^WORKERS=/d' -e '/^SITE_ADDRESS=/d' -e '/^TLS_MODE=/d' \$f
  printf '%s\n' 'WORKERS=$WL' 'SITE_ADDRESS=$SITE' 'TLS_MODE=$TLS' | sudo tee -a \$f >/dev/null; }"
$SSH "sudo AGENT_TOKEN='$T' WORKERS='$WL' SITE_ADDRESS='$SITE' TLS_MODE=$TLS bash /tmp/control/install-control.sh | tail -2"

echo "=== $IP : vérification"
K=; [ "$TLS" = acme ] || K=-k   # certificat interne : pas de vérification de l'émetteur
for i in $(seq 24); do curl -fsS $K -o /dev/null "$SITE/" 2>/dev/null && break; sleep 5; done
curl -sS $K -o /dev/null -w "IHM : $SITE (HTTP %{http_code}, certificat $TLS)\n" "$SITE/" || true
echo "Mot de passe : ADMIN_PASSWORD dans /etc/webconf/control.env (ssh debian@$IP)"
