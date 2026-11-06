#!/bin/sh
# Démarrage du conteneur worker : vérifie la configuration puis lance les 5 services sous supervisord.
set -e
: "${AGENT_TOKEN:?AGENT_TOKEN manquant}"
: "${WORKER_NAME:?WORKER_NAME manquant}"
# Le volume /srv/webconf peut être vide au premier démarrage
mkdir -p /srv/webconf/recordings /srv/webconf/chrome-profile
# Verrous laissés par un arrêt brutal (Chrome refuse sinon de rouvrir le profil ; Xvfb l'écran)
rm -f /srv/webconf/chrome-profile/SingletonLock /srv/webconf/chrome-profile/SingletonSocket \
      /srv/webconf/chrome-profile/SingletonCookie "/tmp/.X${DISPLAY#:}-lock" 2>/dev/null || true
echo "worker $WORKER_NAME : API :$AGENT_PORT, VNC :$VNC_PORT, écran $DISPLAY ($SCREEN_SIZE)"
exec /usr/bin/supervisord -n -c /etc/webconf/supervisord.conf
