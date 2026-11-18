"""Nommer une voix depuis « Qui parle ? » : refusé si les passages écoutés ne sont pas (ou plus) de cette voix
(transcription refaite entre-temps). Transcription fictive dans un dossier temporaire, code réel de l'assistant.

    python tests/speaker_expect_test.py          # environnement d'un assistant
"""
import json
import os
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="spk-"))
os.environ.update(RECORD_DIR=str(tmp), PROFILE_DIR=str(tmp / "profil"), AGENT_TOKEN=os.environ.get("AGENT_TOKEN") or "essai")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import agent  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

doc = {"meta": {}, "speakers": {"S1": {"label": "Intervenant 1"}, "S2": {"label": "Intervenant 2"}},
       "raw": [{"t": 10.0, "te": 14.0, "spk": "S1", "text": "un"}, {"t": 20.0, "te": 24.0, "spk": "S2", "text": "deux"},
               {"t": 30.0, "te": 33.0, "spk": "S1", "text": "trois"}],
       "paragraphs": [{"t": 10.0, "spk": "S1", "text": "un"}, {"t": 20.0, "spk": "S2", "text": "deux"}]}
(tmp / "essai.transcript.json").write_text(json.dumps(doc))
ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what, flush=True)


H = {"x-agent-token": os.environ["AGENT_TOKEN"]}
with TestClient(agent.app) as c:
    u = "/recordings/essai.mp4/speakers"
    r = c.patch(u, headers=H, json={"id": "S2", "name": "Personne A", "via": "question", "expect": [11.0, 31.0]})
    check(r.status_code == 409 and "voix changée" in r.json()["detail"], f"passages écoutés d'une autre voix : refusé ({r.status_code})")
    sp = json.loads((tmp / "essai.transcript.json").read_text())["speakers"]
    check(not sp["S2"].get("name"), "rien de nommé")
    r = c.patch(u, headers=H, json={"id": "S1", "name": "Personne A", "via": "question", "expect": [11.0, 31.0, 21.0]})
    sp = json.loads((tmp / "essai.transcript.json").read_text())["speakers"]
    check(r.status_code == 200 and sp["S1"].get("name") == "Personne A", "passages de cette voix (2 sur 3) : nommée")
    r = c.patch(u, headers=H, json={"id": "S9", "name": "Personne A", "expect": [11.0]})
    check(r.status_code in (404, 409), f"voix absente : refusé ({r.status_code})")
    r = c.patch(u, headers=H, json={"id": "S2", "name": None, "expect": [11.0]})
    check(r.status_code == 200, "retirer un nom : pas de vérification")
    r = c.patch(u, headers=H, json={"id": "S2", "name": "Personne B"})
    check(r.status_code == 200, "nom donné à la main (sans passages) : pas de vérification")
print(f"{ok} vérifications réussies, {fails} échec(s)")
sys.exit(1 if fails else 0)
