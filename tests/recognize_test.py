"""Reconnaissance d'office des anciennes réunions (worker/voiceprints.recognize_sure), sur une transcription fictive.

    python tests/recognize_test.py          # environnement d'un assistant
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import voiceprints as V  # noqa: E402

ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what, flush=True)


sp = {"S1": {"label": "Intervenant 1", "auto": "Intervenant 1", "source": "voice"},
      "S2": {"label": "Intervenant 2", "auto": "Intervenant 2", "source": "voice"},
      "S3": {"label": "Nom Écran", "auto": "Nom Écran", "source": "ui"},
      "S4": {"label": "Personne Main", "name": "Personne Main", "source": "manual"},
      "S5": {"label": "Intervenant 5", "auto": "Intervenant 5", "source": "voice"},
      "S6": {"label": "Personne Zeta", "auto": "Personne Zeta", "print_sim": 0.9, "source": "voiceprint"},
      "S7": {"label": "Intervenant 7", "auto": "Intervenant 7", "source": "voice"},
      "S8": {"label": "Intervenant 8", "auto": "Intervenant 8", "source": "voice"},
      "ME": {"label": "Moi", "source": "me"}}
doc = {"speakers": copy.deepcopy(sp), "raw": [], "paragraphs": []}
Q = lambda sid, name, sim, sure=True: {"sid": sid, "name": name, "sim": sim, "sure": sure, "label": sp[sid]["label"]}
qs = [Q("S1", "Alice Essai", 0.90), Q("S2", "Alice Essai", 0.85),          # même personne : seule la meilleure
      Q("S3", "Bruno Essai", 0.95),                                         # nom à l'écran : pas touché
      Q("S5", "Chloé Essai", 0.92),                                         # refusée à l'écoute pour cette voix
      Q("S7", "Personne Zeta", 0.93),                                       # déjà reconnue dans la réunion
      Q("S8", "David Essai", 0.78, sure=False)]                             # pas sûre : reste une question
auto, rest = V.recognize_sure(doc, qs, {"S5": ["Chloé Essai"]})
s = doc["speakers"]
check([a["sid"] for a in auto] == ["S1"], f"une seule voix nommée d'office : la plus ressemblante, sûre ({[a['sid'] for a in auto]})")
check(s["S1"]["label"] == "Alice Essai" and s["S1"]["source"] == "voiceprint" and s["S1"]["print_sim"] == 0.90
      and not s["S1"].get("name"), "nommée comme en fin de réunion (« reconnue par son empreinte », pas « à la main »)")
check(sorted(q["sid"] for q in rest) == ["S2", "S3", "S5", "S7", "S8"], f"les autres restent des questions ({sorted(q['sid'] for q in rest)})")
check(s["S3"]["label"] == "Nom Écran" and s["S4"]["label"] == "Personne Main" and s["S6"]["label"] == "Personne Zeta",
      "noms à l'écran, à la main et déjà reconnus : intacts")
check(s["S5"]["source"] == "voice" and s["S2"]["source"] == "voice", "voix refusée à l'écoute et doublon : restent inconnues")
auto2, _ = V.recognize_sure(doc, [Q("S2", "Alice Essai", 0.95)], {})
check(not auto2, "personne déjà reconnue dans la réunion : pas une deuxième fois")
print(f"{ok} vérifications réussies, {fails} échec(s)")
sys.exit(1 if fails else 0)
