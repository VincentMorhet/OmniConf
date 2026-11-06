#!/usr/bin/env bash
# Installe un worker. Usage (root) : WORKER_NAME=w1 AGENT_TOKEN=xxx CONTROL_IP=192.168.0.230 ./install-worker.sh
set -euo pipefail
: "${AGENT_TOKEN:?}" "${WORKER_NAME:?}"
SRC=$(cd "$(dirname "$0")" && pwd)
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q xvfb pulseaudio pulseaudio-utils ffmpeg x11vnc novnc websockify python3-venv \
  fonts-noto fonts-noto-color-emoji fonts-liberation matchbox-window-manager nftables curl ca-certificates
if ! command -v google-chrome >/dev/null; then
  curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  apt-get install -y -q /tmp/chrome.deb
fi
# PulseAudio système désactivé pour les sessions (on lance le nôtre)
systemctl --global disable pulseaudio.service pulseaudio.socket 2>/dev/null || true

id bot >/dev/null 2>&1 || useradd -u 1500 -m -s /bin/bash bot
loginctl enable-linger bot   # crée /run/user/1500 en permanence
mkdir -p /opt/webconf /srv/webconf/recordings /srv/webconf/chrome-profile /etc/webconf
rm -rf /opt/webconf/worker && cp -r "$SRC" /opt/webconf/worker
[ -d /opt/webconf/venv ] || python3 -m venv /opt/webconf/venv
/opt/webconf/venv/bin/pip install -q --upgrade pip
/opt/webconf/venv/bin/pip install -q -r /opt/webconf/worker/requirements.txt
# Modèle d'empreinte vocale (distinction des intervenants dans la transcription), ~26 Mo
mkdir -p /opt/webconf/models
SPK=/opt/webconf/models/wespeaker_en_voxceleb_resnet34_LM.onnx
[ -s "$SPK" ] || curl -fsSL -o "$SPK" \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx \
  || echo "ATTENTION : modèle d'empreinte vocale non téléchargé (intervenants non distingués par la voix)"
chown -R bot:bot /srv/webconf

cp /opt/webconf/worker/system/pulse.pa /etc/webconf/pulse.pa
if [ ! -f /etc/webconf/worker.env ]; then
cat > /etc/webconf/worker.env <<ENV
WORKER_NAME=$WORKER_NAME
AGENT_TOKEN=$AGENT_TOKEN
DISPLAY=:99
SCREEN_SIZE=${SCREEN_SIZE:-1600x900}
BOT_DISPLAY_NAME=${BOT_DISPLAY_NAME:-Assistant}
ICE_SERVERS=${ICE_SERVERS:-[]}
KIOSK=1
ENV
fi
# Transcription en direct (OVH AI Endpoints) : clé ajoutée ou mise à jour si fournie
if [ -n "${OVH_AI_ENDPOINTS_ACCESS_TOKEN:-}" ]; then
  sed -i '/^OVH_AI_ENDPOINTS_ACCESS_TOKEN=/d' /etc/webconf/worker.env
  echo "OVH_AI_ENDPOINTS_ACCESS_TOKEN=$OVH_AI_ENDPOINTS_ACCESS_TOKEN" >> /etc/webconf/worker.env
fi
chmod 640 /etc/webconf/worker.env; chgrp bot /etc/webconf/worker.env
cp /opt/webconf/worker/system/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now webconf-xvfb webconf-wm webconf-pulse webconf-vnc webconf-agent
systemctl restart webconf-xvfb webconf-wm webconf-pulse webconf-vnc webconf-agent

# Pare-feu : API (8001) et VNC (6080) uniquement depuis la VM de contrôle ; UDP ouvert pour WebRTC
if [ -n "${CONTROL_IP:-}" ]; then
cat > /etc/nftables.conf <<NFT
flush ruleset
table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
    ip protocol icmp accept
    tcp dport 22 accept
    ip saddr $CONTROL_IP tcp dport { 8001, 6080 } accept
    udp dport 1024-65535 accept
  }
}
NFT
systemctl enable --now nftables && nft -f /etc/nftables.conf
fi
echo "worker $WORKER_NAME installé"
