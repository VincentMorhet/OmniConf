"""IHM : « Est-ce X ? » — voix douteuses à confirmer (accueil et page d'une réunion).

Contrôle d'essai lancé à part, avec des données fictives (« Personne Essai », « Personne Témoin », questions
inventées) et sans rien envoyer aux assistants (ni empreinte, ni renommage : les appels sont seulement notés) ; les
extraits sont lus sur un vrai assistant de test :

    # sur l'hôte des assistants de test (venv du contrôle, fichier d'environnement du contrôle de test)
    WORKER=w3 STEM=<enregistrement de w3 sans .mp4> python tests/voice_questions_ui_test.py serve   # port 8099
    # dans un conteneur qui a Chrome (réseau de l'hôte)
    BASE=http://127.0.0.1:8099 PW=… WORKER=w3 STEM=… python tests/voice_questions_ui_test.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

NAME, OTHER = "Personne Essai", "Personne Témoin"


def serve():
    data = Path(os.environ.get("TEST_DATA_DIR", "/tmp/ctltest/data"))      # jamais le DATA_DIR du vrai contrôle
    os.environ["DATA_DIR"] = str(data)
    data.mkdir(parents=True, exist_ok=True)
    w, stem = os.environ["WORKER"], os.environ["STEM"]
    (data / "voice_consents.json").write_text(json.dumps([
        {"id": f"essai{i}", "name": n, "consent": True, "at": "2026-11-14T09:00:00+01:00", "by": "essai", "note": None, "history": []}
        for i, n in enumerate((NAME, OTHER))]))
    (data / "voiceprints.json").write_text(json.dumps({NAME: {"groups": {}, "review": {}}, OTHER: {"groups": {}, "review": {}}}))

    def q(sid, label, name, sim, t0, **kw):
        return {"sid": sid, "label": label, "name": name, "sim": sim, "sure": sim >= 0.82, "n": 12, "sec": 75.0,
                "clips": [{"t": t0, "te": t0 + 6, "text": "Premier extrait d'essai."},
                          {"t": t0 + 30, "te": t0 + 36, "text": "Deuxième extrait d'essai."},
                          {"t": t0 + 60, "te": t0 + 64, "text": "Troisième extrait d'essai.", "odd": True}],
                "worker": w, "meeting": stem, "title": "Réunion d'essai", "found_at": "2026-11-15T09:00:00+01:00", **kw}
    items = {f"{w}/{stem}|S1": q("S1", "Intervenant 1", NAME, 0.91, 20.0),
             f"{w}/{stem}|S2": q("S2", "Vignette Erronée (2)", NAME, 0.78, 120.0, alt=OTHER, alt_sim=0.74),
             f"{w}/{stem}|S3": q("S3", "Intervenant 3 (sans vignette)", OTHER, 0.74, 220.0),
             f"{w}/{stem}|S4": q("S4", "Intervenant 4", "Inconnu Total", 0.95, 320.0)}       # personne non cochée
    (data / "voice_questions.json").write_text(json.dumps({"items": items, "answers": {}}))
    os.environ.update(ADMIN_LOGIN="essai", ADMIN_NAME="Compte Essai", COOKIE_SECURE="0", SECRET_KEY=os.urandom(16).hex())
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "control"))
    os.chdir(Path(__file__).resolve().parent.parent / "control")
    import app as A
    import uvicorn
    calls = {"named": [], "scheduled": []}

    async def no_push(names=None):            # rien n'est envoyé aux assistants
        return {w: "essai : non diffusé"}

    async def fake_name(qq, name, via):       # pas de renommage sur l'assistant : appel noté
        calls["named"].append([qq["sid"], name, via])
    A.voiceprints_push = no_push
    A.vp_start_build = lambda names, by: {"state": "done", "names": [], "workers": {}, "people": {}}   # aucune recherche
    A.vp_schedule = lambda names, by, delay=60: calls["scheduled"].append(list(names))
    A._vq_name = fake_name

    @A.app.get("/test/state")
    async def state():
        return {"calls": calls, "answers": A.vq_load()["answers"],
                "review": {n: e.get("review") for n, e in A.vp_load().items()}}
    uvicorn.run(A.app, host="127.0.0.1", port=int(os.environ.get("TEST_PORT", "8099")), log_level="warning")


ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what)


async def main():
    import httpx
    from playwright.async_api import async_playwright
    BASE, PW, ADMIN = os.environ["BASE"], os.environ["PW"], os.environ.get("ADMIN", "essai")
    W, STEM = os.environ["WORKER"], os.environ["STEM"]
    api = httpx.Client(base_url=BASE, verify=False, timeout=30)
    check(api.post("/api/login", json={"login": ADMIN, "password": PW}).status_code == 200, "connexion")
    st = lambda: api.get("/test/state").json()
    d = api.get("/api/voice-questions").json()
    qs = d["questions"]
    check([q["sid"] for q in qs] == ["S1", "S2", "S3"], f"3 questions, la plus ressemblante d'abord ; personne non cochée écartée ({[q['sid'] for q in qs]})")
    check(qs[1].get("alt") == OTHER and qs[1].get("alt_sim") == 0.74, "hésitation entre deux personnes transmise")
    check(not any(len(x) >= 64 for q in qs for x in q.values() if isinstance(x, list)), "aucune empreinte envoyée à l'IHM")
    r = api.get("/api/voice-questions/clip", params={"id": qs[0]["id"], "t": 20.0})
    check(r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and len(r.content) > 5000,
          f"extrait MP3 lu sur l'assistant ({r.status_code}, {len(r.content)} octets)")
    check(api.get("/api/voice-questions/clip", params={"id": f"{W}/{STEM}|S4", "t": 320.0}).status_code == 404,
          "extrait d'une personne non cochée : refusé")
    check(api.post("/api/voice-questions/answer", json={"id": qs[0]["id"], "answer": "other", "to": "Inconnu Total"}).status_code == 400,
          "attribution à une personne non cochée : refusée")
    check(api.post("/api/voice-questions/answer", json={"id": qs[0]["id"], "answer": "peut-être"}).status_code == 400, "réponse inconnue : refusée")
    check(not st()["calls"]["named"], "rien de renommé après les refus")
    gid = f"{W}/{STEM}|confirmée"
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        for w, h in ((1280, 900), (390, 844)):
            ctx = await b.new_context(viewport={"width": w, "height": h}, ignore_https_errors=True)
            pg = await ctx.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda dd: asyncio.ensure_future(dd.accept()))
            await pg.goto(BASE + "/#home")
            await pg.fill("#loginId", ADMIN); await pg.fill("#pw", PW); await pg.click("#loginForm button")
            await pg.wait_for_selector("#askSec:not(.hidden) .ask-q")
            box = pg.locator("#askBox")
            if w == 1280:
                check(await pg.locator("#askCount").inner_text() == "3", "accueil : « Qui parle ? » avec 3 questions")
                check("Est-ce Personne Essai ?" in await box.inner_text() and "Intervenant 1" in await box.inner_text(),
                      "la plus ressemblante d'abord : « Est-ce Personne Essai ? »")
                await box.locator(".clip").first.click()
                await pg.wait_for_function("() => { const b = document.querySelector('#askBox .clip'); return b && !b.classList.contains('loading') && b.classList.contains('playing'); }", timeout=20000)
                check(True, "extrait en lecture")
                await pg.wait_for_function("() => !document.querySelector('#askBox .clip.playing')", timeout=20000)
                check(True, "fin de l'extrait")
                await box.screenshot(path="voice-ask.png")
                # oui, puis annuler
                await box.locator(".a-yes").click()
                await pg.wait_for_selector("#askBox .ask-done")
                s = st()
                check(s["calls"]["named"][-1] == ["S1", NAME, "question"], f"oui : voix nommée « {NAME} » dans la réunion ({s['calls']['named'][-1:]})")
                check((s["review"][NAME].get(gid) or {}).get("ok") is True and s["review"][NAME][gid].get("via") == "question",
                      "oui : voix confirmée validée d'avance pour l'empreinte")
                check(s["calls"]["scheduled"][-1:] == [[NAME]], "oui : recherche des voix programmée (empreinte enrichie)")
                check(await pg.locator("#askCount").inner_text() == "2" and "Vignette Erronée" in await box.inner_text()
                      and "s'appelle maintenant Personne Essai" in await box.inner_text(), "réponse rappelée (avec « Annuler »), question suivante, compteur à 2")
                await box.screenshot(path="voice-ask-done.png")
                await box.locator(".a-undo").click()
                await pg.wait_for_function("() => document.querySelector('#askCount').textContent === '3'")
                s = st()
                check(s["calls"]["named"][-1] == ["S1", None, None] and gid not in (s["review"][NAME] or {}) and not s["answers"],
                      "annuler : nom retiré, voix retirée de l'empreinte, question reposée")
                # plus tard : question suivante
                await box.locator(".a-skip").click()
                await pg.wait_for_function("() => document.querySelector('#askBox .ask-q')?.textContent.includes('Vignette Erronée')")
                check("hésitation entre" in await box.inner_text() and await box.locator(".a-alt").count() == 1,
                      "plus tard : question suivante (hésitation entre deux personnes, bouton « C'est Personne Témoin »)")
                await box.locator(".a-no").click()
                await pg.wait_for_function("() => document.querySelector('#askCount').textContent === '2'")
                s = st()
                check(s["answers"].get(f"{W}/{STEM}|S2", {}).get("answer") == "no" and s["calls"]["named"][-1] == ["S1", None, None],
                      "non : réponse notée, rien de renommé")
                check("Intervenant 3" in await box.inner_text(), "la question mise de côté revient après les autres")
                await box.locator(".a-to").select_option(NAME)
                await pg.wait_for_function("() => document.querySelector('#askCount').textContent === '1'")
                s = st()
                check(s["calls"]["named"][-1] == ["S3", NAME, "question"] and (s["review"][NAME].get(gid) or {}).get("ok") is True
                      and s["answers"][f"{W}/{STEM}|S3"].get("to") == NAME, "c'est quelqu'un d'autre : voix nommée et confirmée pour cette personne")
                # page de la réunion : sa dernière question
                await pg.goto(f"{BASE}/#enr/{W}/{STEM}.mp4")
                try:
                    await pg.wait_for_selector("#playerAsk .pl-ask summary", timeout=30000)
                except Exception:
                    print(await pg.evaluate("""() => JSON.stringify({hash: location.hash, url: player.url, ask: document.querySelector('#playerAsk')?.outerHTML.slice(0, 300),
                      tr: document.querySelector('#playerTr')?.textContent.slice(0, 200), hidden: document.querySelector('#player')?.className})"""), errs)
                    await pg.screenshot(path="voice-ask-fail.png")
                    raise
                check("1 voix à confirmer" in await pg.locator("#playerAsk summary").inner_text(), "page de la réunion : 1 voix à confirmer")
                await pg.locator("#playerAsk summary").click()
                await pg.locator("#playerAsk .a-unk").click()
                await pg.wait_for_function("() => !document.querySelector('#playerAsk .ask-q') && document.querySelector('#playerAsk .ask-done')")
                s = st()
                check(s["answers"].get(f"{W}/{STEM}|S1", {}).get("answer") == "unknown", "je ne sais pas : question écartée")
                check(api.get("/api/voice-questions").json()["questions"] == [], "plus aucune question")
                await pg.locator("#playerAsk .a-undo").click()          # remise en jeu pour l'essai sur téléphone
                await pg.wait_for_selector("#playerAsk .pl-ask summary")
                check(len(api.get("/api/voice-questions").json()["questions"]) == 1, "annuler depuis la page de la réunion : question reposée")
            else:
                check(await box.locator(".ask-q").count() == 1, f"[{w}] question affichée")
                over = await pg.evaluate("""() => { const W = document.documentElement.clientWidth;
                  return [...document.querySelectorAll('#askSec *')].filter(e => e.getBoundingClientRect().right > W + 1).length; }""")
                check(over == 0, f"[{w}] rien ne dépasse à droite ({over})")
                await pg.locator("#askSec").screenshot(path="voice-ask-phone.png")
            check(not errs, f"[{w}] aucune erreur JavaScript {errs[:2]}")
            await ctx.close()
        await b.close()
    print(f"{ok} vérifications réussies, {fails} échec(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    serve() if sys.argv[1:] == ["serve"] else asyncio.run(main())
