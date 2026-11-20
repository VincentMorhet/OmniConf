"""« En ce moment » : fusion des réponses du modèle (intitulés stables, niveau du changement), fenêtre relue, boucle du
transcripteur avec un faux modèle (aucun appel réseau, données fictives).

    python tests/now_unit_test.py
"""
import asyncio
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("OVH_AI_ENDPOINTS_ACCESS_TOKEN", "test")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import analyst  # noqa: E402

ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what)


M = analyst.merge_now
A = ["Budget 2027", "Ventilation par lot", "Lot 3 : hébergement"]
prev = {"chemin": A, "depuis": 300.0}

r = M(None, {"chemin": ["Budget 2027", "Ventilation par lot"], "changement": 2, "depuis": "05:10", "en_cours": "x"}, 290, 100, 400)
check(r["chemin"] == ["Budget 2027", "Ventilation par lot"] and r["niveau"] is None and r["depuis"] == 310,
      "premier point : chemin pris tel quel, pas de changement annoncé")
r = M(prev, {"chemin": ["Le budget 2027", "Ventilation", "Hébergement"], "changement": 0, "en_cours": "{S1} détaille"}, 350, 100, 400)
check(r["chemin"] == A and r["niveau"] is None and r["depuis"] == 300, "même sujet reformulé : intitulés précédents gardés")
check(r["en_cours"] == "{S1} détaille", "phrase de l'instant mise à jour")
r = M(prev, {"chemin": ["Budget global", "Ventilation par lot", "Lot 4 : licences"], "changement": 3, "depuis": "06:40"}, 390, 100, 420)
check(r["chemin"] == A[:2] + ["Lot 4 : licences"] and r["niveau"] == 3 and r["depuis"] == 400,
      "nouveau point : niveaux supérieurs recopiés à l'identique")
r = M(prev, {"chemin": ["Budget 2027", "Calendrier de vote"], "changement": 2, "depuis": "99:00"}, 390, 100, 420)
check(r["chemin"] == ["Budget 2027", "Calendrier de vote"] and r["niveau"] == 2 and r["depuis"] == 420,
      "nouveau sous-sujet : point précis abandonné ; heure hors fenêtre ramenée à la fenêtre")
r = M(prev, {"chemin": ["Recrutements", "Postes ouverts"], "changement": 1, "depuis": "x"}, 395, 100, 420)
check(r["chemin"] == ["Recrutements", "Postes ouverts"] and r["niveau"] == 1 and r["depuis"] == 395,
      "nouveau sujet ; heure illisible : première phrase nouvelle")
r = M(prev, {"chemin": A, "changement": 2}, 395, 100, 420)
check(r["niveau"] is None and r["chemin"] == A, "changement annoncé mais mêmes intitulés : rien ne change")
r = M({"chemin": A[:2], "depuis": 300.0}, {"chemin": A, "changement": 0, "depuis": "06:00"}, 360, 100, 420)
check(r["chemin"] == A and r["niveau"] == 3 and r["depuis"] == 360, "niveau plus précis qui apparaît : nouveau point")
r = M(prev, {"garde": [True, False, False], "chemin": ["Budget", "Calendrier de vote", "Date du vote"], "depuis": "06:50"}, 395, 100, 420)
check(r["chemin"] == ["Budget 2027", "Calendrier de vote", "Date du vote"] and r["niveau"] == 2 and r["depuis"] == 410,
      "« garde » par niveau : premier niveau refusé = niveau du changement, niveau gardé recopié")
r = M(prev, {"garde": [True, True, True], "chemin": ["Budget", "Ventilation", "Hébergement des données"]}, 395, 100, 420)
check(r["chemin"] == A and r["niveau"] is None, "tous les niveaux gardés : chemin inchangé")
r = M(prev, {"garde": ["false", True, True], "chemin": ["Recrutements", "Postes", "Profils"]}, 395, 100, 420)
check(r["chemin"] == ["Recrutements", "Postes", "Profils"] and r["niveau"] == 1, "« garde » en texte accepté")
r = M(prev, {"garde": [False, False, False], "chemin": ["budget 2027", "Calendrier", "Vote"]}, 395, 100, 420)
check(r["niveau"] == 2 and r["chemin"] == ["Budget 2027", "Calendrier", "Vote"],
      "niveau 1 annoncé changé mais recopié tel quel : changement compté au niveau 2, intitulé d'origine gardé")
r = M(prev, {"chemin": ["a", "b", "c", "d", "e"], "changement": "1"}, 395, 100, 420)
check(len(r["chemin"]) == 3 and r["niveau"] == 1, "au plus 3 niveaux ; « changement » en texte accepté")
try:
    M(prev, {"chemin": [], "changement": 1}, 395, 100, 420)
    check(False, "chemin vide refusé")
except ValueError:
    check(True, "chemin vide refusé (état précédent conservé par l'appelant)")

u = analyst.now_user([(100.0, "S1", "bonjour"), (130.0, "S2", "passons au budget")], {"S1": "Alice", "S2": "Bruno"},
                     prev, 120.0, ["Ordre du jour", "Budget 2027"])
check("▶ NOUVEAU" in u and u.index("▶ NOUVEAU") < u.index("passons au budget") and u.index("bonjour") < u.index("▶ NOUVEAU"),
      "marque ▶ NOUVEAU avant les phrases nouvelles seulement")
check("Chemin actuel (depuis 05:00) : 1. Budget 2027 › 2. Ventilation par lot › 3. Lot 3 : hébergement" in u
      and "Sujets déjà repérés dans la réunion : Ordre du jour ; Budget 2027." in u and "{S2} Bruno : passons" in u,
      "chemin précédent, sujets repérés et identifiants des voix transmis")


# ---------------------------------------------------------------- boucle du transcripteur, faux modèle
import transcriber as T  # noqa: E402

calls = []


async def fake_chat_json(self, spec, system, user, **kw):
    calls.append(user)
    if len(calls) == 1:
        return {"chemin": ["Ordre du jour"], "changement": 0, "depuis": "00:05", "en_cours": "{S1} ouvre la séance"}, {"secs": 0.4}
    return {"chemin": ["Budget 2027", "Ventilation par lot"], "changement": 1, "depuis": "00:40",
            "en_cours": "{S2} présente le budget"}, {"secs": 0.5}

analyst.LLM.chat_json = fake_chat_json


async def loop_test():
    tr = T.Transcriber("/tmp")
    tr.active, tr.t0 = True, time.time() - 60
    tr.paragraphs = [{"id": 1, "t": 5.0, "spk": "S1", "text": "Bonjour à tous, je vous propose de commencer par l'ordre du jour."}]
    tr.raw = [{"t": 5.0, "te": 9.0, "spk": "S1", "text": "bonjour à tous", "done": True}]
    await tr._now()
    st = tr.now_status()
    check(st["chemin"] == ["Ordre du jour"] and st["niveau"] is None and st["v"] == 1 and st["live"],
          "premier point d'« En ce moment »")
    await tr._now()
    check(len(calls) == 1, "pas de nouvel appel sans paroles nouvelles")
    tr.raw.append({"t": 40.0, "te": 50.0, "spk": "S2", "done": False,
                   "text": "alors passons maintenant au budget deux mille vingt-sept et à sa ventilation par lot"})
    await tr._now()
    st = tr.now_status()
    check(len(calls) == 2 and "passons maintenant au budget" in calls[1], "ligne pas encore mise en forme relue aussitôt")
    check(st["chemin"] == ["Budget 2027", "Ventilation par lot"] and st["niveau"] == 1 and st["depuis"] == 40,
          "changement de sujet détecté (niveau 1, depuis 00:40)")
    check([h["niveau"] for h in st["hist"]] == [None, 1] and st["changed_at"], "fil des sujets : 2 entrées")
    check(tr.analysis_status()["now"]["v"] == 2, "« En ce moment » envoyé avec l'état de l'assistant")
    d = tr.doc()["analysis"]
    check(d["now"]["chemin"][0] == "Budget 2027" and len(d["now_hist"]) == 2, "enregistré dans la transcription")
    tr.active = False
    check(tr.now_status()["live"] is False, "réunion finie : plus « en direct »")
    tr.reset()
    check(tr.now_status() is None, "nouvelle réunion : « En ce moment » vidé")

asyncio.run(loop_test())


# ---------------------------------------------------------------- mise en forme : petit modèle, puis secours
class FakeResp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


async def format_test():
    seen = []

    async def post(url, json=None, **kw):
        seen.append((json["model"], json.get("reasoning_effort")))
        if json["model"] == T.LLM_MODEL:      # petit modèle : identifiant de voix inventé
            return FakeResp('{"paragraphs": [{"t": "00:01", "id": "S9", "text": "Bonjour à tous."}]}')
        return FakeResp('{"paragraphs": [{"t": "00:01", "id": "S1", "text": "Bonjour à tous,"}, {"t": "00:02", "id": "S1", "text": "on commence."}]}')

    tr = T.Transcriber("/tmp")
    tr.active, tr.t0, tr.base = True, time.time() - 30, None
    tr.http.post = post
    tr.raw = [{"t": 1.0, "te": 3.0, "spk": "S1", "text": "bonjour à tous euh on commence", "done": False}]
    await tr._format(final=True)
    check(seen == [(T.LLM_MODEL, "none"), (T.LLM_FALLBACK, "none")] and T.LLM_MODEL != T.LLM_FALLBACK,
          f"réponse du petit modèle inutilisable : tronçon refait avec le modèle de secours ({seen})")
    check([p["text"] for p in tr.paragraphs] == ["Bonjour à tous, on commence."] and tr.last_error is None,
          "texte du modèle de secours gardé (phrases d'une même voix regroupées), aucune erreur affichée")

    async def down(url, json=None, **kw):
        raise RuntimeError("HTTP 503")
    tr.http.post = down
    tr.raw.append({"t": 5.0, "te": 7.0, "spk": "S2", "text": "d'accord", "done": False})
    await tr._format(final=True)
    check(tr.paragraphs[-1]["text"] == "d'accord" and "mise en forme" in (tr.last_error or ""),
          "aucun modèle disponible : texte brut gardé, erreur affichée")

asyncio.run(format_test())
print(f"\n{ok} ok, {fails} échec(s)")
sys.exit(1 if fails else 0)
