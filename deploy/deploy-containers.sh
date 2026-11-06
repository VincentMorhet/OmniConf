#!/usr/bin/env bash
# Déploie des workers webconf en conteneurs Docker sur un hôte (VM Debian 12), depuis pve-00.
# Un conteneur = un worker = une réunion à la fois. Réseau de l'hôte (WebRTC direct) :
# chaque conteneur a ses propres ports (API, VNC) et son propre écran virtuel.
#
#   HOST=192.168.0.233 NAMES="w3 w4" CONTROL_IPS="192.168.0.230" ./deploy/deploy-containers.sh
#
#   worker n°i : API 8000+i · VNC 6079+i · écran :98+i   (le 1er reprend les valeurs d'une VM : 8001, 6080, :99)
#   données    : /srv/webconf/<nom>/{recordings,chrome-profile} sur l'hôte
#   réglages   : /etc/webconf/containers/<nom>.env sur l'hôte (conservés ; clés ajoutées si absentes)
# Variables : CPUS (4) et MEMORY (6g) par conteneur, OVH via .ovh_token, jeton via .agent_token.
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=${HOST:?HOST manquant (ip de l hôte Docker)}
NAMES=(${NAMES:-w3 w4})
CONTROL_IPS=${CONTROL_IPS:-192.168.0.230}
CPUS=${CPUS:-4}; MEMORY=${MEMORY:-6g}
SSH="ssh -o BatchMode=yes debian@$HOST"
[ -f .agent_token ] || python3 -c "import secrets;print(secrets.token_urlsafe(24))" > .agent_token
T=$(cat .agent_token)
OVH=$( [ -f .ovh_token ] && cat .ovh_token || true )

echo "=== $HOST : Docker"
$SSH 'command -v docker >/dev/null || {
  set -e; export DEBIAN_FRONTEND=noninteractive
  sudo apt-get update -q >/dev/null && sudo apt-get install -y -q ca-certificates curl nftables >/dev/null
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -q >/dev/null
  sudo apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null; }
  sudo docker version --format "Docker {{.Server.Version}}"'

echo "=== $HOST : image webconf-worker"
tar -C worker --exclude=__pycache__ -czf - . | $SSH 'sudo rm -rf /opt/webconf/src/worker && sudo mkdir -p /opt/webconf/src/worker && sudo tar -C /opt/webconf/src/worker -xzf -'
$SSH 'cd /opt/webconf/src/worker && sudo docker build -q --network host -t webconf-worker:latest . && sudo docker image prune -f >/dev/null'

echo "=== $HOST : conteneurs ${NAMES[*]}"
COMPOSE="name: webconf
x-worker: &worker
  image: webconf-worker:latest
  network_mode: host
  init: true
  shm_size: 1gb
  restart: unless-stopped
  stop_grace_period: 30s
  cpus: $CPUS
  mem_limit: $MEMORY
  logging: {driver: json-file, options: {max-size: 10m, max-file: \"3\"}}
services:"
i=0; WL=""
for n in "${NAMES[@]}"; do
  i=$((i+1)); api=$((8000+i)); vnc=$((6079+i)); rfb=$((5899+i)); disp=":$((98+i))"
  WL="${WL:+$WL,}$n=http://$HOST:$api"
  COMPOSE+="
  $n:
    <<: *worker
    container_name: webconf-$n
    hostname: webconf-$n
    env_file: /etc/webconf/containers/$n.env
    volumes: [\"/srv/webconf/$n:/srv/webconf\"]"
  # Réglages : créés au premier déploiement, puis seules les clés manquantes sont ajoutées
  $SSH "sudo mkdir -p /etc/webconf/containers /srv/webconf/$n && sudo chown 1500:1500 /srv/webconf/$n
    f=/etc/webconf/containers/$n.env; sudo touch \$f; sudo chmod 600 \$f
    add() { sudo grep -q \"^\$1=\" \$f || echo \"\$1=\$2\" | sudo tee -a \$f >/dev/null; }
    add WORKER_NAME $n; add AGENT_TOKEN '$T'; add AGENT_PORT $api; add VNC_PORT $vnc; add RFB_PORT $rfb
    add DISPLAY $disp; add SCREEN_SIZE 1600x900; add BOT_DISPLAY_NAME Assistant; add ICE_SERVERS '[]'
    [ -z '$OVH' ] || { sudo sed -i '/^OVH_AI_ENDPOINTS_ACCESS_TOKEN=/d' \$f; add OVH_AI_ENDPOINTS_ACCESS_TOKEN '$OVH'; }"
done
printf '%s\n' "$COMPOSE" | $SSH 'sudo tee /opt/webconf/compose.yml >/dev/null && cd /opt/webconf && sudo docker compose up -d --remove-orphans 2>&1 | tail -n +1 | grep -vE "^\s*$" | tail -4'

echo "=== $HOST : pare-feu (API et VNC des workers réservés au contrôle)"
# Table dédiée (pas de « flush ruleset » : les règles de Docker sont conservées)
$SSH "sudo tee /etc/nftables.conf >/dev/null <<NFT
#!/usr/sbin/nft -f
table inet webconf
delete table inet webconf
table inet webconf {
  chain input {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
    meta l4proto { icmp, ipv6-icmp } accept
    tcp dport { 22, 80, 443 } accept
    ip saddr { $CONTROL_IPS } tcp dport { 8001-8099, 6080-6099 } accept
    udp dport 1024-65535 accept
  }
}
NFT
sudo apt-get install -y -q nftables >/dev/null 2>&1 || true
sudo systemctl enable -q nftables && sudo nft -f /etc/nftables.conf"

echo "=== $HOST : état"
sleep 20
for n in "${NAMES[@]}"; do
  $SSH "sudo docker inspect -f '{{.Name}} {{.State.Status}} santé={{if .State.Health}}{{.State.Health.Status}}{{end}}' webconf-$n"
done
echo
echo "Ligne à ajouter à WORKERS dans /etc/webconf/control.env (VM de contrôle) :"
echo "  $WL"
