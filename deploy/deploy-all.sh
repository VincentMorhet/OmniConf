#!/usr/bin/env bash
# (Re)déploie le code sur les 3 VM depuis pve-00. Conserve la config existante (/etc/webconf/*.env).
set -euo pipefail
cd "$(dirname "$0")/.."
CTL=${CTL:-192.168.0.230}; WORKERS_IP=(${WORKERS_IP:-192.168.0.231 192.168.0.232})
[ -f .agent_token ] || python3 -c "import secrets;print(secrets.token_urlsafe(24))" > .agent_token
T=$(cat .agent_token); i=0; WL=""
OVH=$( [ -f .ovh_token ] && cat .ovh_token || true )  # clé OVH AI Endpoints (transcription), facultative
for ip in "${WORKERS_IP[@]}"; do
  i=$((i+1)); WL="${WL:+$WL,}w$i=http://$ip:8001"
  scp -qr worker debian@"$ip":/tmp/ && ssh debian@"$ip" "sudo WORKER_NAME=w$i AGENT_TOKEN=$T CONTROL_IP=$CTL OVH_AI_ENDPOINTS_ACCESS_TOKEN=$OVH bash /tmp/worker/install-worker.sh | tail -1" &
done
wait
scp -qr control debian@"$CTL":/tmp/ && ssh debian@"$CTL" "sudo AGENT_TOKEN=$T WORKERS='$WL' bash /tmp/control/install-control.sh | tail -2"
