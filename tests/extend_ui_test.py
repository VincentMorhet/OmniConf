"""« Prolonger » une réunion commencée (27/11) : repousser le départ automatique (durée max choisie au lancement).

Contrôle d'essai (données fictives, port 8099) et faux assistants (port 8098, un préfixe par assistant) qui appliquent
la même règle que le vrai (/meeting/extend) ; navigateur depuis le conteneur d'essai. Rien n'est envoyé aux vrais
assistants. Le vrai point d'entrée de l'assistant est essayé à part, dans une vraie réunion d'essai sur w3.

    TEST_DATA_DIR=/tmp/extest/data TEST_PORT=8099 ADMIN_PASSWORD=… python tests/extend_ui_test.py serve
    PW=… BASE=http://127.0.0.1:8099 python tests/extend_ui_test.py
"""
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("TEST_DIR", "/tmp/wk"))
WPORT = int(os.environ.get("WORKER_PORT", "8098"))


def serve():
    data = Path(os.environ.get("TEST_DATA_DIR", "/tmp/extest/data"))     # jamais le DATA_DIR du vrai contrôle
    data.mkdir(parents=True, exist_ok=True)
    (data / "voice_questions.json").write_text(json.dumps({"items": {}, "answers": {}}))
    (data / "voice_consents.json").write_text("[]")
    W = {n: f"http://127.0.0.1:{WPORT}/{n}" for n in ("wA", "wB", "wC", "wD")}
    os.environ.update(DATA_DIR=str(data), ADMIN_LOGIN="essai", ADMIN_NAME="Compte Essai", COOKIE_SECURE="0",
                      SECRET_KEY=os.urandom(16).hex(), AGENT_TOKEN="essai-sans-secret",
                      WORKERS=",".join(f"{k}={v}" for k, v in W.items()))
    sys.path.insert(0, str(ROOT / "control"))
    os.chdir(ROOT / "control")
    import app as A
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request

    if not A.USERS.get("lea"):
        A.USERS.create("lea", "Léa Essai")
    A.USERS.set_password("lea", os.environ["ADMIN_PASSWORD"], must_change=False)
    if not A.USERS.get("marc"):
        A.USERS.create("marc", "Marc Essai")
    A.USERS.set_password("marc", os.environ["ADMIN_PASSWORD"], must_change=False)
    now = dt.datetime.now().astimezone()
    at = lambda m: (now + dt.timedelta(minutes=m)).isoformat()    # noqa: E731
    base = {"online": True, "state": "in_meeting", "platform": "teams", "viewers": [], "recording": True,
            "recording_since": at(-30), "recording_file": "x.mp4", "listeners": 0, "vnc_port": 8096,
            "since": at(-30), "joined_at": at(-30), "url": "https://teams.microsoft.com/l/meetup-join/essai",
            "transcript": {"active": False, "enabled": True}}
    st = {"wA": {**base, "title": "Réunion d'essai", "owner": "essai", "auto_leave_at": at(8)},
          "wB": {**base, "title": "Réunion de Léa", "owner": "lea", "viewers": ["marc"], "auto_leave_at": at(50)},
          "wC": {**base, "title": "Réunion sans limite", "owner": "essai", "auto_leave_at": None},
          "wD": {**base, "title": "Assistant pas à jour", "owner": "essai", "auto_leave_at": at(40)}}
    calls = []
    F = FastAPI()

    def check_token(request: Request):
        if request.headers.get("x-agent-token") != "essai-sans-secret":
            raise HTTPException(401)

    @F.get("/{w}/status")
    async def w_status(w: str, request: Request):
        check_token(request)
        return {**st[w], "name": w}

    @F.post("/{w}/meeting/extend")
    async def w_extend(w: str, request: Request):     # même règle que worker/agent.py
        check_token(request)
        body = await request.json()
        calls.append({"w": w, **body})
        if w == "wD":
            raise HTTPException(404, "Not Found")
        s = st[w]
        if s["state"] not in ("joining", "lobby", "in_meeting") or not s["auto_leave_at"]:
            raise HTTPException(409, "pas de départ automatique prévu")
        n = dt.datetime.now().astimezone()
        new = min(max(dt.datetime.fromisoformat(s["auto_leave_at"]), n) + dt.timedelta(minutes=int(body["minutes"])),
                  n + dt.timedelta(hours=12))
        s["auto_leave_at"] = new.isoformat()
        return {**s, "name": w}

    @F.api_route("/{w}/{rest:path}", methods=["GET", "POST", "PATCH", "PUT"])
    async def w_other(w: str, rest: str):
        return [] if rest in ("recordings", "imports", "voices") else {}

    @A.app.get("/test/calls")
    async def test_calls():
        return calls

    @A.app.get("/test/state/{w}")
    async def test_state(w: str):
        return st[w]

    async def main():
        s1 = uvicorn.Server(uvicorn.Config(A.app, host="127.0.0.1", port=int(os.environ.get("TEST_PORT", "8099")), log_level="warning"))
        s2 = uvicorn.Server(uvicorn.Config(F, host="127.0.0.1", port=WPORT, log_level="warning"))
        await asyncio.gather(s1.serve(), s2.serve())
    asyncio.run(main())


ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what, flush=True)


async def run():
    import httpx
    from playwright.async_api import async_playwright
    BASE, PW = os.environ["BASE"], os.environ["PW"]

    def client(login=None):
        c = httpx.Client(base_url=BASE, timeout=30)
        if login:
            r = c.post("/api/login", json={"login": login, "password": PW})
            assert r.status_code == 200, r.text
        return c
    adm, lea, marc, anon = client("essai"), client("lea"), client("marc"), client()
    hm = lambda iso: dt.datetime.fromisoformat(iso).astimezone(dt.timezone(dt.timedelta(hours=1))).strftime("%H:%M")  # noqa: E731
    left = lambda iso: (dt.datetime.fromisoformat(iso) - dt.datetime.now().astimezone()).total_seconds() / 60  # noqa: E731

    print("API")
    a0 = adm.get("/test/state/wA").json()["auto_leave_at"]
    for bad in ({"minutes": 0}, {"minutes": 721}, {"minutes": "abc"}, {}, []):
        r = adm.post("/api/workers/wA/extend", json=bad)
        check(r.status_code == 400, f"durée refusée {bad!r} -> {r.status_code}")
    check(anon.post("/api/workers/wA/extend", json={"minutes": 30}).status_code == 401, "sans compte : 401")
    r = lea.post("/api/workers/wA/extend", json={"minutes": 30})
    check(r.status_code == 403, f"autre compte (lea sur la réunion d'essai) : 403 ({r.status_code})")
    r = lea.post("/api/workers/wB/extend", json={"minutes": 30})
    check(r.status_code == 200 and abs(left(r.json()["auto_leave_at"]) - 80) < 2, "propriétaire (lea) : +30 min depuis l'heure prévue")
    r = adm.post("/api/workers/wC/extend", json={"minutes": 30})
    check(r.status_code == 409 and adm.get("/test/state/wC").json()["auto_leave_at"] is None,
          "réunion sans départ automatique : 409, aucune limite ajoutée")
    r = adm.post("/api/workers/wD/extend", json={"minutes": 30})
    check(r.status_code == 409 and "pas encore" in r.json().get("detail", ""), f"assistant pas à jour : message clair ({r.text[:90]})")
    check(adm.get("/test/state/wA").json()["auto_leave_at"] == a0, "réunion d'essai : rien de changé par ces refus")
    calls = adm.get("/test/calls").json()
    check(all(set(c) == {"w", "minutes"} for c in calls) and not any(c["w"] == "wA" for c in calls),
          "l'assistant ne reçoit que la durée, et jamais pour un refus")

    async with async_playwright() as pw:
        br = await pw.chromium.launch(channel="chrome", headless=True)

        async def page(w, h, who="essai"):
            ctx = await br.new_context(viewport={"width": w, "height": h}, timezone_id="Europe/Paris")
            pg = await ctx.new_page()
            pg.on("pageerror", lambda e: print("  [erreur JS]", e))
            await pg.goto(BASE + "/#home")
            await pg.fill("#loginId", who)
            await pg.fill("#pw", PW)
            await pg.click("#loginForm button")
            await pg.wait_for_selector("#tab-home:not(.hidden)", timeout=15000)
            return pg

        print("Interface")
        pg = await page(1280, 860)
        await pg.wait_for_function("() => document.querySelector('#toast').classList.contains('show')", timeout=10000)
        t = await pg.inner_text("#toast")
        check("Réunion d'essai" in t and hm(a0) in t, f"accueil : avertissement 10 min avant le départ ({t[:110]})")
        await pg.goto(BASE + "/#m/wA")
        card = pg.locator("#workers article.worker:not(.hidden)")
        await card.locator(".b-extend").wait_for(state="visible", timeout=8000)
        check("warn" in (await card.locator(".b-extend").get_attribute("class")), "bouton « Prolonger » mis en évidence")
        note = await card.locator(".wnote").inner_text()
        check(f"tout seul à {hm(a0)}" in note and "Prolonger" in note, f"note du départ proche ({note[:100]})")
        await pg.screenshot(path=str(OUT / "ext-card.png"))
        await card.locator(".b-extend").click()
        await pg.wait_for_selector("#dlgExtend[open]")
        txt = await pg.inner_text("#exText")
        check("Réunion d'essai" in txt and hm(a0) in txt, f"fenêtre : heure prévue ({txt[:100]})")
        await pg.screenshot(path=str(OUT / "ext-dialog.png"))
        await pg.click("#dlgExtend button[value='60']")
        await pg.wait_for_function("() => document.querySelector('#toast').innerText.includes('repoussé')", timeout=8000)
        a1 = adm.get("/test/state/wA").json()["auto_leave_at"]
        check(abs(left(a1) - 68) < 2, f"+1 h depuis l'heure prévue (départ {hm(a0)} -> {hm(a1)})")
        check(hm(a1) in await pg.inner_text("#toast"), "message : nouvelle heure")
        await pg.wait_for_function(f"() => document.querySelector('#workers article.worker:not(.hidden) .winfo').innerText.includes('{hm(a1)}')", timeout=8000)
        check("warn" not in (await card.locator(".b-extend").get_attribute("class")), "plus d'alerte une fois prolongée")
        check("tout seul" not in await card.locator(".wnote").inner_text(), "note du départ proche retirée")
        # Annuler : rien n'est envoyé
        n = len(adm.get("/test/calls").json())
        await card.locator(".b-extend").click()
        await pg.wait_for_selector("#dlgExtend[open]")
        await pg.click("#exNo")
        await pg.wait_for_timeout(800)
        check(len(adm.get("/test/calls").json()) == n, "Annuler : rien n'est envoyé")
        # réunion sans départ automatique, et réunion d'un autre montrée en spectateur : pas de bouton
        await pg.goto(BASE + "/#m/wC")
        await pg.wait_for_timeout(1500)
        c = pg.locator("#workers article.worker:not(.hidden)")
        check(not await c.locator(".b-extend").is_visible(), "sans départ automatique : pas de « Prolonger »")
        sp = await page(1280, 860, "marc")
        await sp.goto(BASE + "/#m/wB")
        await sp.wait_for_timeout(2000)
        c = sp.locator("#workers article.worker:not(.hidden)")
        check("Léa" in await c.locator(".wtitle").inner_text() and not await c.locator(".b-extend").is_visible(),
              "spectateur (marc) : pas de « Prolonger »")
        r = marc.post("/api/workers/wB/extend", json={"minutes": 30})
        check(r.status_code == 403, f"spectateur (API) : 403 ({r.status_code})")
        await sp.context.close()
        # assistant pas à jour : message clair dans l'interface
        await pg.goto(BASE + "/#m/wD")
        c = pg.locator("#workers article.worker:not(.hidden)")
        await c.locator(".b-extend").wait_for(state="visible", timeout=8000)
        await c.locator(".b-extend").click()
        await pg.wait_for_selector("#dlgExtend[open]")
        await pg.click("#dlgExtend button[value='30']")
        await pg.wait_for_function("() => document.querySelector('#toast').innerText.includes('pas encore')", timeout=8000)
        check(True, "assistant pas à jour : message affiché")
        # suivre en grand
        await pg.goto(BASE + "/#suivre/wA")
        await pg.wait_for_selector("#fExtend:not(.hidden)", timeout=8000)
        info = await pg.inner_text("#fInfo")
        check(f"départ auto à {hm(a1)}" in info, f"suivre en grand : heure de départ ({info})")
        await pg.click("#fExtend")
        await pg.wait_for_selector("#dlgExtend[open]")
        await pg.click("#dlgExtend button[value='30']")
        await pg.wait_for_function("() => document.querySelector('#toast').innerText.includes('repoussé')", timeout=8000)
        a2 = adm.get("/test/state/wA").json()["auto_leave_at"]
        check(abs(left(a2) - 98) < 2, f"suivre en grand : +30 min ({hm(a1)} -> {hm(a2)})")
        await pg.wait_for_function(f"() => document.querySelector('#fInfo').innerText.includes('{hm(a2)}')", timeout=8000)
        check(True, "suivre en grand : heure mise à jour")
        # téléphone
        ph = await page(390, 844)
        await ph.goto(BASE + "/#m/wA")
        c = ph.locator("#workers article.worker:not(.hidden)")
        await c.locator(".b-extend").wait_for(state="visible", timeout=8000)
        await c.locator(".b-extend").click()
        await ph.wait_for_selector("#dlgExtend[open]")
        await ph.wait_for_timeout(500)
        box = await ph.locator("#dlgExtend").bounding_box()
        bts = [await ph.locator(f"#dlgExtend button[value='{v}']").bounding_box() for v in (30, 60, 120, 240)]
        check(box["x"] >= 0 and box["x"] + box["width"] <= 390 and all(b["x"] + b["width"] <= 390 for b in bts),
              "téléphone : la fenêtre tient dans l'écran")
        check(await ph.evaluate("() => document.documentElement.scrollWidth <= 390"), "téléphone : rien ne dépasse à droite")
        await ph.screenshot(path=str(OUT / "ext-phone.png"))
        await br.close()
    print(f"\n{ok} vérifications réussies, {fails} échecs")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        serve()
    else:
        asyncio.run(run())
