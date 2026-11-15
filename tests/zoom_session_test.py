"""Session Zoom de chacun (« comme si c'était moi ») : règles du contrôle et IHM.

Contrôle d'essai lancé à part, avec des assistants simulés : aucun appel n'atteint un vrai assistant (connexion,
entrée en réunion, retour de session sont imités et notés). Comptes fictifs « essai » (administrateur) et « autre » ;
cookie Zoom inventé.

    # sur un hôte qui a le venv du contrôle et son fichier d'environnement
    python tests/zoom_session_test.py serve                      # port 8099
    # dans un conteneur qui a Chrome (réseau de l'hôte)
    BASE=http://127.0.0.1:8099 PW=… python tests/zoom_session_test.py
"""
import asyncio
import json
import os
import stat
import sys
from pathlib import Path

COOK = [{"name": "_zm_essai", "value": "VALEUR-SECRETE-123", "domain": ".zoom.us", "path": "/", "secure": True}]
COOK2 = [{"name": "_zm_essai", "value": "VALEUR-RAFRAICHIE-456", "domain": ".zoom.us", "path": "/", "secure": True}]


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
    fake = {"state": {}, "sess": {}, "signed": True, "out": {}, "calls": [], "join": None}

    async def worker_status(name):
        o = fake["sess"].get(name)
        return {"name": name, "online": True, "state": fake["state"].get(name, "idle"), "session_out": name in fake["out"],
                **({"session_of": o[0], "session_kind": o[1]} if o else {})}

    class R:
        status_code, content = 200, b"\xff\xd8\xff"

        def __init__(self, d):
            self.d = d

        def json(self):
            return self.d

    async def wcall(name, method, path, **kw):
        j = kw.get("json") or {}
        fake["calls"].append([name, path, {k: v for k, v in j.items() if k != "session"}])
        d = {"state": "joining"}
        if path == "/zoom/capture/start":
            fake["state"][name], fake["sess"][name] = "manual", (j["owner"], "capture")
        elif path == "/zoom/capture/finish":
            if fake["signed"]:
                fake["sess"].pop(name, None)
                fake["state"][name] = "idle"
                d = {"signed_in": True, "cookies": COOK}
            else:
                d = {"signed_in": False}
        elif path == "/zoom/capture/cancel":
            fake["sess"].pop(name, None)
            fake["state"][name] = "idle"
        elif path == "/zoom/session/out":
            d = fake["out"].pop(name, {})
        elif path == "/meeting/join":
            fake["join"] = {"worker": name, "as_me": j.get("as_me"), "required": j.get("as_me_required"),
                            "session": "aucune" if not j.get("session") else "la sienne" if j["session"] in (COOK, COOK2) else "autre"}
        return R(d)

    async def no_push(names=None):
        return {}
    A.worker_status, A.wcall, A.voiceprints_push = worker_status, wcall, no_push
    for n in A.WORKERS:              # aucun vrai assistant joignable (écran distant, listes) : port fermé
        A.WORKERS[n] = "http://127.0.0.1:9"
    A.vp_start_build = lambda names, by: {"state": "done"}

    @A.app.post("/test/set")
    async def test_set(body: dict):
        for k in ("state", "signed", "out"):
            if k in body:
                fake[k] = body[k]
        if "sess" in body:
            fake["sess"] = {w: tuple(v) for w, v in body["sess"].items()}
        fake["calls"].clear()
        fake["join"] = None
        return {}

    @A.app.get("/test/state")
    async def test_state():
        txt = A.SESSIONS_FILE.read_text() if A.SESSIONS_FILE.exists() else ""
        return {"calls": fake["calls"], "join": fake["join"], "plain": "VALEUR-" in txt,
                "logins": sorted(json.loads(txt)) if txt else [],
                "modes": [oct(stat.S_IMODE(f.stat().st_mode)) for f in (A.SESSIONS_FILE, A.SESSIONS_KEY) if f.exists()],
                "value": (A.sess_get("essai") or [{}])[0].get("value", "")}

    @A.app.post("/test/collect")
    async def test_collect():
        await A.sessions_collect()
        return {}
    uvicorn.run(A.app, host="127.0.0.1", port=int(os.environ.get("TEST_PORT", "8099")), log_level="warning")


ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what)


async def open_zoom(pg):
    """Carte « Votre compte Zoom » ouverte et relue (un clic sur une carte déjà ouverte la refermerait)."""
    if await pg.evaluate("() => document.querySelector('#zoomBox').open"):
        await pg.evaluate("() => loadZoom()")
    else:
        await pg.click("#zoomBox summary")


async def main():
    import httpx
    from playwright.async_api import async_playwright
    BASE, PW = os.environ["BASE"], os.environ["PW"]
    me = httpx.Client(base_url=BASE, timeout=30)
    check(me.post("/api/login", json={"login": "essai", "password": PW}).status_code == 200, "connexion (essai)")
    ws = me.get("/api/config").json()["workers"]
    W1, W2 = ws[0], ws[-1]
    pw2 = me.post("/api/users", json={"login": "autre", "name": "Compte Autre", "role": "user"}).json().get("password")
    other = httpx.Client(base_url=BASE, timeout=30)
    check(other.post("/api/login", json={"login": "autre", "password": pw2}).status_code == 200, "connexion (autre)")
    st = lambda **kw: me.post("/test/set", json=kw)
    S = lambda: me.get("/test/state").json()
    ZOOM, TEAMS = "https://zoom.us/j/123456789?pwd=essai", "https://teams.microsoft.com/l/meetup-join/essai"

    # sans session
    st(state={}, sess={})
    check(me.get("/api/zoom-account").json() == {"session": None, "capture": None}, "pas de session au départ")
    r = me.post("/api/meetings", json={"url": ZOOM, "as_me": True})
    check(r.status_code == 409 and "pas de session Zoom" in r.json()["detail"] and not S()["join"], "avec mon compte, sans session : refusé, message clair")
    me.post("/api/meetings", json={"url": ZOOM})
    check(S()["join"] == {"worker": W1, "as_me": False, "required": False, "session": "aucune"}, "réunion planifiée sans session : en invité")

    # connexion : page de connexion sur un assistant libre, lui seul y a accès
    st(state={}, sess={}, signed=False)
    r = me.post("/api/zoom-account/signin")
    w = r.json().get("name")
    c = [x for x in S()["calls"] if x[1] == "/zoom/capture/start"]
    check(r.status_code == 200 and c and c[-1][2]["owner"] == "essai", f"« Se connecter à Zoom » : page de connexion ouverte sur {w}")
    check(me.get("/api/zoom-account").json()["capture"] == w, "connexion en cours annoncée")
    ow = {x["name"]: x for x in other.get("/api/workers").json()}
    check(ow[w]["state"] == "taken", f"autre : {w} occupé pendant la connexion")
    check(other.get(f"/api/workers/{w}/screenshot").status_code == 403, f"autre : écran de {w} refusé")
    check(other.post(f"/api/workers/{w}/open", json={"url": "https://zoom.us/profile"}).status_code == 403, f"autre : ouvrir une page sur {w} refusé")
    mw = {x["name"]: x for x in me.get("/api/workers").json()}
    check(mw[w]["state"] == "manual" and mw[w].get("session_of") == "essai", f"essai : {w} visible (connexion en cours)")
    r = me.post("/api/zoom-account/finish")
    check(r.status_code == 409 and "pas encore connecté" in r.json()["detail"], "« J'ai terminé » avant d'être connecté : 409")
    me.post("/test/set", json={"signed": True, "sess": {w: ["essai", "capture"]}, "state": {w: "manual"}})
    r = me.post("/api/zoom-account/finish")
    s = S()
    check(r.status_code == 200 and r.json()["session"]["saved_at"] and s["value"] == "VALEUR-SECRETE-123", "session reprise et enregistrée")
    check(not s["plain"] and s["modes"] == ["0o600", "0o600"], f"session chiffrée sur le disque, fichiers en 600 ({s['modes']})")
    check(me.get("/api/config").json()["zoomSession"]["saved_at"], "configuration : session annoncée (sans cookie)")
    check("VALEUR" not in json.dumps(me.get("/api/zoom-account").json()) + json.dumps(me.get("/api/config").json()),
          "aucun cookie renvoyé à l'IHM")

    # réunions
    st(state={}, sess={})
    r = me.post("/api/meetings", json={"url": ZOOM})
    check(r.json().get("as_me") is True and S()["join"] == {"worker": W1, "as_me": True, "required": False, "session": "la sienne"},
          "Zoom : premier assistant libre, avec sa session")
    st(state={}, sess={})
    me.post("/api/meetings", json={"url": ZOOM, "as_me": True})
    check(S()["join"]["required"] is True and S()["join"]["session"] == "la sienne", "case cochée : session exigée")
    st(state={}, sess={})
    me.post("/api/meetings", json={"url": ZOOM, "as_me": False})
    check(S()["join"]["session"] == "aucune", "case décochée : en invité, sans session")
    st(state={}, sess={})
    me.post("/api/meetings", json={"url": TEAMS, "as_me": True})
    check(S()["join"]["session"] == "aucune", "Teams : jamais de session Zoom")
    st(state={}, sess={})
    other.post("/api/meetings", json={"url": ZOOM})
    check(S()["join"]["session"] == "aucune", "réunion d'un autre compte : jamais la session d'essai")
    st(state={W1: "in_meeting"}, sess={W1: ["essai", "meeting"]})
    r = me.post("/api/meetings", json={"url": ZOOM, "as_me": True})
    check(r.status_code == 409 and "déjà utilisée" in r.json()["detail"] and not S()["join"], "deuxième réunion avec sa session : refusée (une à la fois)")
    st(state={W1: "in_meeting"}, sess={W1: ["essai", "meeting"]})
    r = me.post("/api/meetings", json={"url": ZOOM})
    check(r.status_code == 200 and r.json().get("note") and S()["join"]["session"] == "aucune" and S()["join"]["worker"] != W1,
          "deuxième réunion planifiée : en invité, avec une note")
    check(other.get(f"/api/workers/{W1}/screenshot").status_code == 403, "autre : assistant en réunion avec la session d'essai, écran refusé")

    # fin de réunion : session rafraîchie rendue, ou expirée
    st(state={}, sess={}, out={W2: {"owner": "essai", "kind": "meeting", "cookies": COOK2}})
    me.post("/test/collect")
    z = me.get("/api/zoom-account").json()["session"]
    check(S()["value"] == "VALEUR-RAFRAICHIE-456" and z["used_at"] and not z["expired"], "fin de réunion : session rafraîchie gardée")
    st(state={}, sess={}, out={W2: {"owner": "essai", "kind": "meeting", "cookies": None}})
    me.post("/test/collect")
    z = me.get("/api/zoom-account").json()["session"]
    check(z["expired"] and S()["value"] == "", "déconnecté par Zoom pendant la réunion : session marquée expirée, effacée")
    r = me.post("/api/meetings", json={"url": ZOOM, "as_me": True})
    check(r.status_code == 409, "session expirée, compte exigé : refusé")
    st(state={}, sess={})
    me.post("/api/meetings", json={"url": ZOOM})
    check(S()["join"]["session"] == "aucune", "session expirée, réunion planifiée : en invité")

    # oublier ; suppression d'un compte
    me.post("/test/set", json={"signed": True, "sess": {}, "state": {}})
    other.post("/api/zoom-account/signin")
    check(other.post("/api/zoom-account/finish").status_code == 200 and "autre" in S()["logins"], "autre : sa propre session")
    check(me.delete("/api/zoom-account").status_code == 200 and me.get("/api/zoom-account").json()["session"] is None, "oublier ma session")
    check(me.delete("/api/users/autre").status_code == 200 and "autre" not in S()["logins"], "compte supprimé : sa session aussi")

    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        for w, h in ((1280, 900), (390, 844)):
            st(state={}, sess={}, signed=True)
            ctx = await b.new_context(viewport={"width": w, "height": h})
            pg = await ctx.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
            await pg.goto(BASE + "/#reglages")
            await pg.fill("#loginId", "essai"); await pg.fill("#pw", PW); await pg.click("#loginForm button")
            await pg.wait_for_selector("#tab-set:not(.hidden)")
            await open_zoom(pg)
            await pg.wait_for_selector("#zoomMine .z-in")
            check("Aucune session Zoom" in await pg.locator("#zoomMine").inner_text() and await pg.locator("#asMeRow").is_hidden(),
                  f"[{w}] sans session : « Se connecter à Zoom », pas de case au lancement")
            await pg.locator(".z-in").click()
            await pg.wait_for_function("() => location.hash.startsWith('#m/')")
            check(True, f"[{w}] « Se connecter à Zoom » : écran de l'assistant affiché")
            await pg.goto(BASE + "/#reglages")
            await open_zoom(pg)
            await pg.wait_for_selector("#zoomMine .z-done")
            check("connexion en cours" in await pg.locator("#zoomMine").inner_text(), f"[{w}] connexion en cours : « J'ai terminé »")
            if w == 1280:
                await pg.locator("#zoomBox").screenshot(path="zoom-capture.png")
            await pg.locator(".z-done").click()
            await pg.wait_for_function("() => document.querySelector('#zoomMine').textContent.includes('session Zoom enregistrée')")
            check(await pg.locator("#asMeRow").count() == 1, f"[{w}] session enregistrée")
            await pg.goto(BASE + "/#home")
            await pg.click("#opts summary")
            check(await pg.locator("#asMeRow").is_visible() and await pg.locator("#f-asme").is_checked(), f"[{w}] lancement : case « avec mon compte » cochée")
            if w == 1280:
                await pg.goto(BASE + "/#reglages")
                await open_zoom(pg)
                await pg.wait_for_selector("#zoomMine .z-forget")
                await pg.locator("#zoomBox").screenshot(path="zoom-saved.png")
                await pg.locator(".z-forget").click()
                await pg.wait_for_function("() => document.querySelector('#zoomMine').textContent.includes('Aucune session')")
                check(await pg.locator("#asMeRow").is_hidden(), "oublier : plus de case au lancement")
            else:
                await pg.goto(BASE + "/#reglages")
                await open_zoom(pg)
                await pg.wait_for_selector("#zoomMine .z-forget")
                over = await pg.evaluate("""() => { const W = document.documentElement.clientWidth;
                  return [...document.querySelectorAll('#zoomBox *')].filter(e => e.getBoundingClientRect().right > W + 1).length; }""")
                check(over == 0, f"[{w}] rien ne dépasse à droite ({over})")
                await pg.locator("#zoomBox").screenshot(path="zoom-phone.png")
            check(not errs, f"[{w}] aucune erreur JavaScript {errs[:2]}")
            await ctx.close()
        await b.close()
    print(f"{ok} vérifications réussies, {fails} échec(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    serve() if sys.argv[1:] == ["serve"] else asyncio.run(main())
