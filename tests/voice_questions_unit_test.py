"""« Est-ce X ? » côté assistant : voix douteuses repérées pendant la recherche des voix (worker/voiceprints.py).

Données fictives seulement (« Personne Essai », vecteurs aléatoires, son factice) ; aucune empreinte réelle, aucun
enregistrement lu :

    python tests/voice_questions_unit_test.py      # dans un environnement qui a numpy (venv d'un assistant)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="vq-"))
os.environ["LEXICON_FILE"] = str(TMP / "lexique-absent.md")
os.environ["VOICEPRINTS_FILE"] = str(TMP / "voiceprints.json")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import numpy as np  # noqa: E402

import reid  # noqa: E402
import voiceprints as V  # noqa: E402

ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what)


rnd = np.random.default_rng(3)
unit = lambda v: v / np.linalg.norm(v)
A, B, C = (unit(rnd.normal(size=192)) for _ in range(3))      # empreintes de 3 personnes fictives


def near(ref, sim):
    """Vecteur unitaire de similarité `sim` avec ref."""
    o = rnd.normal(size=192)
    o = unit(o - (o @ ref) * ref)
    return unit(sim * ref + (1 - sim * sim) ** 0.5 * o)


# chaque voix de la réunion fictive : (vecteur du centre, nombre de phrases)
VOICES = {"S1": near(A, 0.76), "S2": near(A, 0.93), "S3": near(B, 0.40), "S4": near(A, 0.90),
          "S5": near(A, 0.80), "S6": near(A, 0.88), "S7": near(A, 0.85)}
# S5 : entre A et C (deux empreintes proches) ; S6 : canal « Moi » = A dans la 2e réunion
calls = {"embed": 0, "pcm": 0}


def fake_embed(pcm, spans, ex=None):
    calls["embed"] += 1
    out = []
    for a, b in spans:
        sid = SPAN_SID[round(a, 2)]
        out.append(VOICES[sid] + rnd.normal(scale=0.02, size=192))   # phrases proches du centre de leur voix
    return out


def fake_pcm(media):
    calls["pcm"] += 1
    return np.zeros(10)


reid.embed_all, reid.load_pcm, reid._extractor = fake_embed, fake_pcm, lambda: object()
SPAN_SID = {}


def meeting(stem, speakers, me_name=None, me_sec=0):
    raw, t = [], 10.0
    for sid, n in speakers.items():
        for i in range(n):
            raw.append({"t": t, "te": t + 5.0, "spk": sid, "text": f"Phrase {i} de {sid}."})
            SPAN_SID[round(t, 2)] = sid
            t += 6.0
    if me_sec:
        raw.append({"t": t, "te": t + me_sec, "spk": "ME", "text": "Je parle au micro de l'assistant."})
    doc = {"meta": {"title": "Réunion d'essai", "me_name": me_name}, "raw": raw, "paragraphs": [],
           "speakers": {sid: {"label": f"Intervenant {k}", "auto": f"Intervenant {k}", "name": None, "source": "voice"}
                        for k, sid in enumerate(speakers, 1)}}
    (TMP / f"{stem}.transcript.json").write_text(json.dumps(doc))
    (TMP / f"{stem}.mp4").write_bytes(b"factice")
    return doc


prints = {"Personne Essai": A, "Personne Témoin": B, "Personne Voisine": unit(A + 0.18 * C)}
# Personne Voisine : très proche de A (cosinus ≈ 0,98) -> pour S5, deux empreintes trop proches
doc = meeting("20261114-100000_essai", {"S1": 8, "S2": 8, "S3": 8, "S4": 2, "S5": 8, "S7": 8})
doc["speakers"]["S2"]["label"] = "Personne Essai (2)"             # déjà nommée ainsi par la vignette
doc["speakers"]["S7"].update(name="Quelqu'un", source="manual")    # nommée à la main
(TMP / "20261114-100000_essai.transcript.json").write_text(json.dumps(doc))
mt = (TMP / "20261114-100000_essai.transcript.json").stat().st_mtime


def get_pcm():
    return fake_pcm(None), None


# empreintes : A et Voisine trop proches -> une seule ressemblance ; on teste d'abord sans Voisine
P = {n: unit(v) for n, v in prints.items() if n != "Personne Voisine"}
qs = V.ask("20261114-100000_essai", mt, doc, P, get_pcm)
by = {q["sid"]: q for q in qs}
check(set(by) == {"S1", "S5"}, f"questions sur S1 (0,76) et S5 (0,80) seulement ({sorted(by)})")
check(by.get("S1", {}).get("name") == "Personne Essai" and not by["S1"]["sure"] and 0.70 <= by["S1"]["sim"] < 0.82,
      f"S1 : « Est-ce Personne Essai ? », doute ({by.get('S1', {}).get('sim')})")
check("S2" not in by, "S2 très ressemblante mais déjà nommée ainsi (vignette) : pas de question")
check("S3" not in by, "S3 trop différente : pas de question")
check("S4" not in by, "S4 trop courte (2 phrases) : pas de question")
check("S7" not in by, "S7 nommée à la main : pas de question")
q = by["S1"]
check(len(q["clips"]) >= 3 and all(set(c) <= {"t", "te", "text", "odd"} for c in q["clips"]),
      "3 extraits à écouter, positions et texte seulement (aucun vecteur)")
check(not any(isinstance(x, (list, np.ndarray)) and len(x) >= 64 for x in q.values()), "aucune empreinte dans la question")
check(json.dumps(qs) is not None, "questions sérialisables (JSON)")
n0 = calls["embed"]
qs2 = V.ask("20261114-100000_essai", mt, doc, P, get_pcm)
check(calls["embed"] == n0 and qs2 == qs, "voix déjà calculées : rien n'est recalculé (mémoire)")

# deux empreintes trop proches : alternative proposée
qs3 = V.ask("20261114-100000_essai", mt, doc, {n: unit(v) for n, v in prints.items()}, get_pcm)
alt = [q for q in qs3 if q.get("alt")]
check(alt and {alt[0]["name"], alt[0]["alt"]} == {"Personne Essai", "Personne Voisine"},
      f"empreintes trop proches : « Est-ce A ou B ? » ({[(q['sid'], q['name'], q.get('alt')) for q in alt]})")

# réunion antérieure à l'empreinte : très ressemblante mais pas reconnue -> question « sûre »
doc2 = meeting("20261101-090000_essai", {"S1": 8, "S2": 8})
qs4 = V.ask("20261101-090000_essai", (TMP / "20261101-090000_essai.transcript.json").stat().st_mtime, doc2, P, get_pcm)
s2 = next((q for q in qs4 if q["sid"] == "S2"), None)
check(s2 and s2["sure"] and s2["sim"] >= 0.82, f"voix très ressemblante pas encore nommée : question marquée sûre ({s2 and s2['sim']})")

# canal « Moi » : la personne qui parle au micro de l'assistant n'est pas cherchée dans le son de la réunion
doc3 = meeting("20261102-090000_essai", {"S1": 8, "S6": 8}, me_name="Personne Essai", me_sec=40)
qs5 = V.ask("20261102-090000_essai", (TMP / "20261102-090000_essai.transcript.json").stat().st_mtime, doc3, P, get_pcm)
check(all(q["name"] != "Personne Essai" for q in qs5), "propriétaire au micro « Moi » : pas de question à son nom")

# voix reconnue par empreinte (ré-identification) : pas de question
doc["speakers"]["S1"].update(source="voiceprint", print_sim=0.9)
check(all(q["sid"] != "S1" for q in V.ask("20261114-100000_essai", mt, doc, P, get_pcm)), "voix déjà reconnue : pas de question")
doc["speakers"]["S1"].update(source="voice")
doc["speakers"]["S1"].pop("print_sim")

# extract : questions renvoyées avec les voix candidates, son chargé seulement s'il le faut
V.save({n: {"v": [float(x) for x in v], "sec": 300, "n": 50, "meetings": 3} for n, v in P.items()})
V._ASK_CACHE.clear()
calls["pcm"] = 0
asks = {}
res = V.extract(TMP, ["Personne Essai", "Personne Témoin"], asks=asks)
check(set(asks) >= {"20261114-100000_essai"} and asks["20261114-100000_essai"]["title"] == "Réunion d'essai",
      f"extract : questions par réunion, avec le titre ({ {k: len(v['questions']) for k, v in asks.items()} })")
p0, calls["pcm"] = calls["pcm"], 0
asks2 = {}
V.extract(TMP, ["Personne Essai", "Personne Témoin"], asks=asks2)
check(asks2 == asks, "nouvelle recherche : mêmes questions")
# seule la réunion où « Personne Essai » parle au micro « Moi » a des passages à calculer pour les voix candidates
check(p0 == 3 and calls["pcm"] == 1, f"voix déjà calculées : son relu seulement pour les voix candidates ({p0} puis {calls['pcm']})")
check(isinstance(V.extract(TMP, ["Personne Essai"]), dict), "sans asks : résultat inchangé (compatibilité)")

# voix confirmée (réponse « oui ») : rejoint les voix de la personne sous l'origine « confirmée »
doc["speakers"]["S1"].update(name="Personne Essai", source="manual", via="question")
sm = V.samples(doc, {V.key("Personne Essai"): "Personne Essai"}, None)
check({x[2] for x in sm.get("Personne Essai", [])} == {"confirmée"},
      "voix confirmée en réponse à une question : origine « confirmée »")
doc["speakers"]["S1"].pop("via")
sm = V.samples(doc, {V.key("Personne Essai"): "Personne Essai"}, None)
check({x[2] for x in sm.get("Personne Essai", [])} == {"nommée"}, "renommée à la main ensuite : origine « nommée »")

# renommer une voix garde la source « voiceprint » des autres, et le marqueur « confirmée » si le nom ne change pas
import transcriber as T  # noqa: E402
d = {"speakers": {"S1": {"label": "Personne Témoin", "auto": "Personne Témoin", "name": None, "source": "voiceprint", "print_sim": 0.9},
                  "S2": {"label": "Intervenant 2", "auto": "Intervenant 2", "name": None, "source": "voice"}}}
T.rename_in_doc(d, "S2", "Personne Essai", "question")
check(d["speakers"]["S1"]["source"] == "voiceprint" and d["speakers"]["S2"].get("via") == "question"
      and d["speakers"]["S2"]["source"] == "manual", "renommage : « voiceprint » gardé pour les autres, voix marquée confirmée")
T.rename_in_doc(d, "S2", "Personne Essai")
check(d["speakers"]["S2"].get("via") == "question", "même nom redonné : toujours confirmée")
T.rename_in_doc(d, "S2", "Autre Nom")
check("via" not in d["speakers"]["S2"], "renommée autrement : plus confirmée")
T.rename_in_doc(d, "S2", None)
check(d["speakers"]["S2"]["source"] == "voice" and d["speakers"]["S2"]["label"] == "Intervenant 2", "nom retiré : nom automatique")

print(f"{ok} vérifications réussies, {fails} échec(s)")
sys.exit(1 if fails else 0)
