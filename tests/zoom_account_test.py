"""Assistant réservé à un compte (session Zoom de la personne, « comme si c'était moi ») : règles du contrôle et IHM.

Contrôle d'essai lancé à part, avec des assistants simulés : aucun appel n'atteint un vrai assistant (état, entrée en
réunion, ouverture de page et vérification de session sont imités et notés). Comptes fictifs « essai » (administrateur
à qui l'assistant est réservé) et « autre ».

    # sur un hôte qui a le venv du contrôle et son fichier d'environnement
    python tests/zoom_account_test.py serve                      # port 8099
    # dans un conteneur qui a Chrome (réseau de l'hôte)
    BASE=http://127.0.0.1:8099 PW=… python tests/zoom_account_test.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path


def serve():
    data = Path(os.environ.get("TEST_DATA_DIR", "/tmp/ctltest/data"))      # jamais le DATA_DIR du vrai contrôle
    os.environ["DATA_DIR"] = str(data)
    data.mkdir(parents=True, exist_ok=True)
    (data / "voice_questions.json").write_text(json.dumps({"items": {}, "answers": {}}))
    os.environ.update(ADMIN_LOGIN="essai", ADMIN_NAME="Compte Essai", COOKIE_SECURE="0", SECRET_KEY=os.urandom(16).hex())
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "control"))
    os.chdir(Path(__file__).resolve().parent.parent / "control")
    import app as A
    import uvicorn
    fake = {"state": {}, "session": True, "calls": []}

    async def worker_status(name):
        return {"name": name, "online": True, "state": fake["state"].get(name, "idle")}

    class R:
        status_code, content = 200, b"\xff\xd8\xff"

        def json(self):
            return {"state": "joining"}

    async def wcall(name, method, path, **kw):
        fake["calls"].append([name, method, path, kw.get("json")])
        return R()

    async def zoom_session(w):
        fake["calls"].append([w, "GET", "/zoom/session", None])
        if fake["session"] is not None:
            A._zoom_seen[w] = {"signed_in": fake["session"], "at": "2026-11-15T22:00:00+01:00"}
        return fake["session"]

    async def no_push(names=None):
        return {}
    A.worker_status, A.wcall, A.zoom_session, A.voiceprints_push = worker_status, wcall, zoom_session, no_push
    A.vp_start_build = lambda names, by: {"state": "done"}

    @A.app.post("/test/set")
    async def test_set(body: dict):
        fake.update({k: v for k, v in body.items() if k in ("state", "session")})
        fake["calls"].clear()
        return fake

    @A.app.get("/test/calls")
    async def test_calls():
        return fake["calls"]
    uvicorn.run(A.app, host="127.0.0.1", port=int(os.environ.get("TEST_PORT", "8099")), log_level="warning")


ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what)


async def main():
    import httpx
    from playwright.async_api import async_playwright
    BASE, PW = os.environ["BASE"], os.environ["PW"]
    me = httpx.Client(base_url=BASE, timeout=30)
    check(me.post("/api/login", json={"login": "essai", "password": PW}).status_code == 200, "connexion (essai, administrateur)")
    ws = me.get("/api/config").json()["workers"]
    W = ws[-1]                                       # assistant réservé pour l'essai
    others = [w for w in ws if w != W]
    r = me.post("/api/users", json={"login": "autre", "name": "Compte Autre", "role": "user"})
    pw2 = r.json().get("password")
    other = httpx.Client(base_url=BASE, timeout=30)
    check(other.post("/api/login", json={"login": "autre", "password": pw2}).status_code == 200, "connexion (autre, utilisateur)")
    st = lambda **kw: me.post("/test/set", json=kw)
    calls = lambda: me.get("/test/calls").json()
    joins = lambda: [c for c in calls() if c[2] == "/meeting/join"]
    ZOOM = "https://zoom.us/j/123456789?pwd=essai"

    check(me.put(f"/api/workers/{W}/reserve", json={"login": "inconnu"}).status_code == 404, "réservation à un compte inconnu : refusée")
    check(other.put(f"/api/workers/{W}/reserve", json={"login": "autre"}).status_code == 403, "réservation par un non-administrateur : refusée")
    check(me.put(f"/api/workers/{W}/reserve", json={"login": "essai"}).status_code == 200, f"{W} réservé à essai")
    check(me.get("/api/config").json()["zoomWorkers"] == [W] and other.get("/api/config").json()["zoomWorkers"] == [],
          "configuration : l'assistant réservé n'est annoncé qu'à sa personne")

    # les autres ne peuvent ni l'utiliser ni le voir
    ow = {x["name"]: x for x in other.get("/api/workers").json()}
    check(ow[W]["state"] == "taken" and ow[W].get("reserved_for") == "Compte Essai", f"liste des assistants (autre) : {W} « réservé à Compte Essai »")
    st(state={}, session=True)
    r = other.post("/api/meetings", json={"url": ZOOM, "worker": W})
    check(r.status_code == 403 and "réservé" in r.json()["detail"] and not joins(), f"autre : réunion sur {W} refusée ({r.status_code})")
    check(other.get(f"/api/workers/{W}/screenshot").status_code == 403, f"autre : écran de {W} refusé")
    check(other.post(f"/api/workers/{W}/open", json={"url": "https://zoom.us/profile"}).status_code == 403, f"autre : ouvrir une page sur {W} refusé")
    check(other.post("/api/zoom-account/signin").status_code == 404, "autre : pas d'assistant réservé, pas de connexion Zoom")
    r = other.post("/api/meetings", json={"url": ZOOM})
    j = joins()
    check(r.status_code == 200 and j and j[-1][0] != W and j[-1][3]["as_me"] is False, f"autre, Zoom automatique : un assistant partagé, en invité ({j[-1][0] if j else None})")
    st(state={w: "in_meeting" for w in others}, session=True)
    r = other.post("/api/meetings", json={"url": ZOOM})
    check(r.status_code == 409 and not joins(), "autre : assistants partagés occupés, jamais le réservé (409)")

    # la personne à qui il est réservé
    st(state={}, session=True)
    r = me.post("/api/meetings", json={"url": ZOOM})
    j = joins()
    check(r.status_code == 200 and r.json().get("as_me") is True and j and j[-1][0] == W and j[-1][3]["as_me"] is True,
          f"essai, Zoom (automatique) : {W}, avec son compte")
    check([c[2] for c in calls()][:1] == ["/zoom/session"], "session Zoom vérifiée avant l'entrée")
    st(state={}, session=True)
    r = me.post("/api/meetings", json={"url": ZOOM, "as_me": False})
    j = joins()
    check(r.status_code == 200 and j and j[-1][0] != W and j[-1][3]["as_me"] is False, "essai, case décochée : un autre assistant, en invité")
    st(state={}, session=True)
    r = me.post("/api/meetings", json={"url": "https://teams.microsoft.com/l/meetup-join/essai", "as_me": True})
    j = joins()
    check(r.status_code == 200 and j and j[-1][0] != W and j[-1][3]["as_me"] is False, "essai, Teams : un assistant partagé d'abord")
    st(state={W: "in_meeting"}, session=True)
    r = me.post("/api/meetings", json={"url": ZOOM, "as_me": True})
    check(r.status_code == 409 and "occupé" in r.json()["detail"] and not joins(), "essai, son assistant Zoom occupé, compte exigé : 409 explicite")
    st(state={W: "in_meeting"}, session=True)
    r = me.post("/api/meetings", json={"url": ZOOM})
    j = joins()
    check(r.status_code == 200 and r.json().get("note") and j and j[-1][0] != W and j[-1][3]["as_me"] is False,
          "essai, automatique (réunion planifiée) : son assistant occupé, un autre en invité, avec une note")
    st(state={}, session=False)
    r = me.post("/api/meetings", json={"url": ZOOM, "as_me": True})
    check(r.status_code == 409 and "session Zoom" in r.json()["detail"] and not joins(), "session Zoom fermée : entrée refusée, message clair")
    st(state={}, session=False)
    r = me.post("/api/meetings", json={"url": ZOOM})
    j = joins()
    check(r.status_code == 200 and "session Zoom fermée" in (r.json().get("note") or "") and j and j[-1][0] != W
          and j[-1][3]["as_me"] is False, "session fermée, réunion planifiée : un autre assistant en invité, avec une note")
    st(state={}, session=None)
    r = me.post("/api/meetings", json={"url": ZOOM, "as_me": True})
    check(r.status_code == 200 and joins(), "session impossible à vérifier : entrée tentée quand même")
    st(state={}, session=True)
    r = me.post("/api/meetings", json={"url": "https://teams.microsoft.com/l/meetup-join/essai", "worker": W})
    j = joins()
    check(r.status_code == 200 and j and j[-1][0] == W and j[-1][3]["as_me"] is False, f"essai, Teams sur {W} choisi : pas de mode compte Zoom")

    # libérer l'assistant : seulement session fermée
    st(session=True)
    check(me.put(f"/api/workers/{W}/reserve", json={"login": None}).status_code == 409, "rendre partagé avec la session ouverte : refusé")
    st(session=None)
    check(me.put(f"/api/workers/{W}/reserve", json={"login": None}).status_code == 409, "rendre partagé, session impossible à vérifier : refusé")

    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        for w, h, login, pw in ((1280, 900, "essai", PW), (390, 844, "essai", PW), (1280, 900, "autre", pw2)):
            ctx = await b.new_context(viewport={"width": w, "height": h})
            pg = await ctx.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
            await pg.goto(BASE + "/#reglages")
            await pg.fill("#loginId", login); await pg.fill("#pw", pw); await pg.click("#loginForm button")
            if login == "autre":
                await pg.wait_for_function("() => document.querySelector('#dlgPw')?.open || !document.querySelector('#tab-set').classList.contains('hidden')")
                if await pg.evaluate("() => document.querySelector('#dlgPw')?.open"):
                    await pg.evaluate("() => document.querySelector('#dlgPw').close()")
                await pg.wait_for_selector("#tab-set:not(.hidden)")
                check(await pg.locator("#zoomBox").is_hidden() and await pg.locator("#asMeRow").is_hidden(),
                      "autre : ni carte « Votre compte Zoom » ni case « avec mon compte »")
                await ctx.close()
                continue
            await pg.wait_for_selector("#tab-set:not(.hidden)")
            st(state={}, session=True)
            await pg.click("#zoomBox summary")
            await pg.wait_for_selector("#zoomMine .zoom-w")
            txt = await pg.locator("#zoomMine").inner_text()
            check(f"L'assistant {W} vous est réservé" in txt, f"[{w}] « Votre compte Zoom » : assistant réservé annoncé")
            await pg.locator(".z-check").click()
            await pg.wait_for_function("() => document.querySelector('#zoomMine').textContent.includes('session Zoom ouverte')")
            check(True, f"[{w}] « Vérifier la session » : session ouverte")
            if w == 1280:
                check(await pg.locator("#zoomAdmin .zoom-res").count() == len(ws), "administrateur : réservations de chaque assistant")
                await pg.locator("#zoomBox").screenshot(path="zoom-account.png")
                await pg.locator(".z-in").click()
                await pg.wait_for_function(f"() => location.hash === '#m/{W}'")
                c = [x for x in calls() if x[2] == "/browser/open"]
                check(c and c[-1][0] == W and c[-1][3]["url"] == "https://zoom.us/signin" and c[-1][3]["owner"] == "essai",
                      "« Se connecter à Zoom » : page de connexion ouverte sur son assistant, écran affiché")
                await pg.goto(BASE + "/#home")
                await pg.click("#opts summary")
                check(await pg.locator("#asMeRow").is_visible() and await pg.locator("#f-asme").is_checked(),
                      "lancement : case « Zoom : entrer avec mon compte » cochée")
                await pg.locator("#opts").screenshot(path="zoom-launch.png")
            else:
                over = await pg.evaluate("""() => { const W = document.documentElement.clientWidth;
                  return [...document.querySelectorAll('#zoomBox *')].filter(e => e.getBoundingClientRect().right > W + 1).length; }""")
                check(over == 0, f"[{w}] rien ne dépasse à droite ({over})")
                await pg.locator("#zoomBox").screenshot(path="zoom-account-phone.png")
            check(not errs, f"[{w}] aucune erreur JavaScript {errs[:2]}")
            await ctx.close()
        await b.close()
    print(f"{ok} vérifications réussies, {fails} échec(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    serve() if sys.argv[1:] == ["serve"] else asyncio.run(main())
