#!/usr/bin/env bash
# Réunion Jitsi de bout en bout sur un worker en conteneur, à lancer sur l'hôte Docker :
#   3 participants (voix de synthèse, conteneur jetable) + « Moi » joué dans le micro du bot,
#   transcription en direct, points d'étape, puis compte rendu automatique à la sortie.
# Usage : sudo W=w4 BASE=https://192.168.0.233 bash container_meeting_e2e.sh /chemin/vers/tests
# Clé OVH lue dans /etc/webconf/containers/$W.env ; mot de passe IHM dans /etc/webconf/control.env.
set -euo pipefail
T=$(realpath "${1:?dossier tests}"); W=${W:-w4}; BASE=${BASE:-https://192.168.0.233}
IMG=webconf-worker:latest; WORK=/tmp/ct-e2e; mkdir -p $WORK; chown -R 1500:1500 $WORK
export OVH_AI_ENDPOINTS_ACCESS_TOKEN=$(sed -n 's/^OVH_AI_ENDPOINTS_ACCESS_TOKEN=//p' /etc/webconf/containers/$W.env)
export PW=$(sed -n 's/^ADMIN_PASSWORD=//p' /etc/webconf/control.env)
run() { docker run --rm --network host --shm-size 1g -v "$T:/tests:ro" -v $WORK:/work -w /work \
          -e OVH_AI_ENDPOINTS_ACCESS_TOKEN -e PW -e BASE -e LEAD=40 -e CONV=/work/conv \
          --entrypoint /opt/webconf/venv/bin/python $IMG "$@"; }

echo "== voix de synthèse et pistes des participants"
run /tests/report_tts.py /tests/fixtures/meeting_demo.transcript.json /work/tts | tail -1
run /tests/report_build_conv.py /work/tts /work/conv >$WORK/plan.json
STAY=$(run -c "import wave;w=wave.open('/work/conv/alice.wav');print(int(w.getnframes()/w.getframerate())+5)")
echo "durée de la conversation : ${STAY}s"

ROOM="https://meet.ffmuc.net/OmniconfConteneur$(date +%s)"
echo "== le bot $W rejoint $ROOM"
run /tests/report_e2e_ctl.py start "$ROOM" "$W"
run /tests/report_e2e_ctl.py wait "$W"

echo "== participants + « Moi »"
docker cp $WORK/conv/me.wav webconf-$W:/tmp/me.wav
run /tests/diar_participants.py "$ROOM" "$STAY" &
P=$!
sleep 6; docker exec webconf-$W paplay -d user_in /tmp/me.wav &
for i in 1 2 3; do sleep $((STAY / 3)); run /tests/report_e2e_ctl.py status "$W" | cut -c1-300; done
wait $P || true
run /tests/report_e2e_ctl.py leave "$W"

echo "== compte rendu (rédigé automatiquement à la sortie)"
t0=$(date +%s)
for i in $(seq 1 120); do
  L=$(run /tests/report_e2e_ctl.py recs "$W" | grep -m1 "^$W " || true)
  case "$L" in *"'state': 'running'"*|"") sleep 15 ;; *) break ;; esac
done
echo "$L"; echo "attente du compte rendu : $(( $(date +%s) - t0 ))s"
