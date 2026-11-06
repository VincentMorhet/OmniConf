# Worker webconf en conteneur : même contenu qu'une VM worker (voir install-worker.sh).
# Un conteneur = une réunion à la fois. Réseau de l'hôte (WebRTC direct), ports réglables par variables.
#   docker build -t webconf-worker worker/
FROM debian:12-slim

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update -q \
 && apt-get install -y -q --no-install-recommends \
      xvfb pulseaudio pulseaudio-utils ffmpeg x11vnc novnc websockify python3-venv \
      fonts-noto fonts-noto-color-emoji fonts-liberation matchbox-window-manager \
      supervisor curl ca-certificates tzdata \
 && curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get install -y -q /tmp/chrome.deb \
 && rm -rf /tmp/chrome.deb /var/lib/apt/lists/*

# Dépendances Python (couche séparée : le code change plus souvent)
COPY requirements.txt /opt/webconf/worker/requirements.txt
RUN python3 -m venv /opt/webconf/venv \
 && /opt/webconf/venv/bin/pip install -q --no-cache-dir --upgrade pip \
 && /opt/webconf/venv/bin/pip install -q --no-cache-dir -r /opt/webconf/worker/requirements.txt

# Modèle d'empreinte vocale (distinction des intervenants), ~26 Mo
RUN mkdir -p /opt/webconf/models \
 && curl -fsSL -o /opt/webconf/models/wespeaker_en_voxceleb_resnet34_LM.onnx \
      https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx

# Utilisateur bot (même uid que sur les VM : les fichiers des volumes restent compatibles)
RUN useradd -u 1500 -m -s /bin/bash bot \
 && mkdir -p /run/user/1500 /srv/webconf/recordings /srv/webconf/chrome-profile /etc/webconf \
 && chown bot:bot /run/user/1500 /srv/webconf /srv/webconf/recordings /srv/webconf/chrome-profile \
 && chmod 700 /run/user/1500 \
 && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

COPY system/pulse.pa /etc/webconf/pulse.pa
COPY container/supervisord.conf /etc/webconf/supervisord.conf
COPY container/entrypoint.sh /usr/local/bin/webconf-entrypoint
COPY --chown=root:root . /opt/webconf/worker
RUN rm -rf /opt/webconf/worker/__pycache__ /opt/webconf/worker/container /opt/webconf/worker/system \
 && chmod 755 /usr/local/bin/webconf-entrypoint

# Valeurs par défaut identiques à une VM worker ; changer les ports et l'écran pour chaque
# conteneur supplémentaire sur un même hôte (réseau de l'hôte partagé).
ENV XDG_RUNTIME_DIR=/run/user/1500 \
    DISPLAY=:99 \
    SCREEN_SIZE=1920x1200 \
    AGENT_PORT=8001 \
    VNC_PORT=6080 \
    RFB_PORT=5900 \
    KIOSK=1 \
    BOT_DISPLAY_NAME=Assistant \
    ICE_SERVERS=[] \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1

USER bot
WORKDIR /opt/webconf/worker
VOLUME ["/srv/webconf"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${AGENT_PORT}/health" >/dev/null || exit 1
ENTRYPOINT ["/usr/local/bin/webconf-entrypoint"]
