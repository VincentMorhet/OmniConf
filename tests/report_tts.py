"""Génère les tours de parole de la réunion fictive (fixtures/make_meeting.py) en voix de synthèse OVH,
pour un test de bout en bout : participants Jitsi (alice, bruno, salle1, salle2) + « Moi » (me, joué
dans le micro de l'IHM). Usage : python report_tts.py meeting.transcript.json dossier_sortie"""
import json, os, sys, time, httpx
K = os.environ.get("OVH_AI_ENDPOINTS_ACCESS_TOKEN") or open(os.path.expanduser("~/t/tok")).read().strip().split("=", 1)[-1]
VOICES = {"S1": ("it-it", "it-IT", "Italian-IT-Female-1", "alice"), "S2": ("de-de", "de-DE", "German-DE-Male-1", "bruno"),
          "S3": ("es-es", "es-ES", "Spanish-ES-Female-1", "salle1"), "S4": ("en-us", "en-US", "English-US.Male-1", "salle2"),
          "ME": ("en-us", "en-US", "English-US.Female-1", "me")}
doc = json.load(open(sys.argv[1])); out = sys.argv[2]; os.makedirs(out, exist_ok=True)
with httpx.Client(timeout=60, headers={"Authorization": f"Bearer {K}"}) as c:
    for i, p in enumerate(doc["paragraphs"]):
        ep, lang, voice, who = VOICES[p["spk"]]
        f = f"{out}/{i:03d}_{who}.wav"
        if os.path.exists(f): continue
        for a in range(4):
            r = c.post(f"https://nvr-tts-{ep}.endpoints.kepler.ai.cloud.ovh.net/api/v1/tts/text_to_audio",
                       json={"encoding": 1, "language_code": lang, "sample_rate_hz": 16000, "text": p["text"], "voice_name": voice})
            if r.status_code == 200: break
            print("retry", r.status_code, r.text[:100]); time.sleep(2)
        r.raise_for_status()
        open(f, "wb").write(r.content); print(f, len(r.content))
