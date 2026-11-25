"""Planifier une réunion depuis l'accueil : une date remplie dans « Plus tard » planifie TOUJOURS (bouton « Planifier »,
bouton principal ou touche Entrée), elle ne lance jamais l'assistant tout de suite ; puis la planification démarre bien à
l'heure. Contrôle d'essai avec un assistant simulé (données fictives, aucun vrai assistant).

    TEST_DATA_DIR=/tmp/schedui/data TEST_PORT=8099 ADMIN_PASSWORD=… python tests/schedule_ui_test.py serve   # contrôle
    PW=… BASE=http://127.0.0.1:8099 python tests/schedule_ui_test.py                                         # navigateur
"""
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("TEST_DIR", "/tmp/wk"))


def serve():
    data = Path(os.environ.get("TEST_DATA_DIR", "/tmp/schedui/data"))     # jamais le DATA_DIR du vrai contrôle
    data.mkdir(parents=True, exist_ok=True)
    (data / "voice_questions.json").write_text(json.dumps({"items": {}, "answers": {}}))
    (data / "voice_consents.json").write_text("[]")
    os.environ.update(DATA_DIR=str(data), ADMIN_LOGIN="essai", ADMIN_NAME="Compte Essai", COOKIE_SECURE="0",
                      SECRET_KEY=os.urandom(16).hex(), AGENT_TOKEN="essai-sans-secret", WORKERS="wA=http://127.0.0.1:9")
    sys.path.insert(0, str(ROOT / "control"))
    os.chdir(ROOT / "control")
    import app as A
    import uvicorn

    started = []

    async def worker_status(name):
        return {"name": name, "online": True, "state": "idle", "owner": None, "viewers": [], "recording": False,
                "listeners": 0, "vnc_port": 8096, "transcript": {"active": False, "enabled": True}}

    class R:
        status_code = 200

        def __init__(self, d=None, content=b""):
            self.d, self.content = d or {}, content

        def json(self):
            return self.d

    async def wcall(name, method, path, **kw):
        if path.startswith("/screenshot"):
            return R(content=b"\xff\xd8\xff\xd9")
        if path.startswith("/recordings"):
            return R([])
        return R({"ok": True})

    async def start_meeting(m, u):                 # lancement simulé : on note seulement qui l'a demandé et quand
        started.append({"url": m.url, "title": m.title, "by": u["login"], "at": dt.datetime.now(dt.timezone.utc).isoformat()})
        return {"ok": True, "name": "wA"}

    A.worker_status, A.wcall, A.start_meeting = worker_status, wcall, start_meeting

    @A.app.get("/test/state")
    async def test_state():
        return {"started": started, "schedule": A.load_schedule()}

    uvicorn.run(A.app, host="127.0.0.1", port=int(os.environ.get("TEST_PORT", "8099")), log_level="warning")


ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what, flush=True)


def local(d: dt.datetime) -> str:          # valeur d'un champ datetime-local (heure locale du navigateur)
    return d.strftime("%Y-%m-%dT%H:%M")


async def run():
    import httpx
    from playwright.async_api import async_playwright
    BASE, PW = os.environ["BASE"], os.environ["PW"]
    ctl = httpx.Client(base_url=BASE)
    state = lambda: ctl.get("/test/state").json()
    later = (dt.datetime.now() + dt.timedelta(days=2)).replace(hour=11, minute=0)
    async with async_playwright() as p:
        br = await p.chromium.launch(channel="chrome", headless=True)

        async def page(w, h):
            pg = await (await br.new_context(viewport={"width": w, "height": h}, timezone_id="Europe/Paris")).new_page()
            await pg.goto(BASE + "/#home")
            await pg.fill("#loginId", "essai")
            await pg.fill("#pw", PW)
            await pg.click("#loginForm button")
            await pg.wait_for_selector("#tab-home:not(.hidden)", timeout=15000)
            return pg

        async def fill(pg, url, when):
            await pg.evaluate("() => { document.querySelector('#toast').textContent = ''; }")   # pas de message d'avant
            await pg.fill("#f-url", url)
            if not await pg.evaluate("() => document.querySelector('#opts').open"):
                await pg.click("#opts summary")
            await pg.fill("#f-when", when)

        async def toast(pg, part):
            await pg.wait_for_function(f"() => document.querySelector('#toast').innerText.includes({json.dumps(part)})", timeout=8000)
            return await pg.locator("#toast").inner_text()

        pg = await page(1280, 900)
        await fill(pg, "https://teams.example/rdv-1", local(later))
        lab = await pg.locator("#btnStart").inner_text()
        check(lab.startswith("Planifier ·") and "à 11:00" in lab, f"date remplie : le bouton principal devient « {lab} »")
        await pg.press("#f-when", "Enter")                 # le cas du 24/11 : Entrée dans le champ de la date
        t = await toast(pg, "planifiée")
        st = state()
        check(len(st["schedule"]) == 1 and not st["started"], f"Entrée dans la date : planifiée, pas lancée ({t!r})")
        check(st["schedule"][0]["url"] == "https://teams.example/rdv-1" and st["schedule"][0]["status"] == "pending"
              and dt.datetime.fromisoformat(st["schedule"][0]["when"]).astimezone().strftime("%H:%M") == "11:00",
              "planification enregistrée à la bonne heure")
        await pg.wait_for_function("() => document.querySelectorAll('#schedule li').length === 1", timeout=8000)
        li = await pg.locator("#schedule li").first.inner_text()
        check("prévue" in li and "à 11:00" in li and "Annuler" in li and "pending" not in li, f"liste « Planifiées » en français ({li!r})")
        check(await pg.locator("#btnStart").inner_text() == "Envoyer l'assistant" and not await pg.input_value("#f-url")
              and not await pg.input_value("#f-when"), "formulaire vidé, bouton principal revenu à « Envoyer l'assistant »")

        await fill(pg, "https://teams.example/rdv-2", local(later))
        await pg.click("#opts summary")                     # options repliées : seule la date les rappelle
        await pg.press("#f-url", "Enter")
        await toast(pg, "planifiée")
        st = state()
        check(len(st["schedule"]) == 2 and not st["started"], "Entrée dans le lien, options repliées : planifiée, pas lancée")

        await fill(pg, "https://teams.example/rdv-3", local(later))
        await pg.click("#btnStart")
        await toast(pg, "planifiée")
        st = state()
        check(len(st["schedule"]) == 3 and not st["started"], "clic sur le bouton principal avec une date : planifiée, pas lancée")

        await fill(pg, "https://teams.example/rdv-passe", local(dt.datetime.now() - dt.timedelta(hours=3)))
        await pg.click("#btnStart")
        t = await toast(pg, "passée")
        st = state()
        check(len(st["schedule"]) == 3 and not st["started"], f"heure passée : refusée, rien de lancé ({t!r})")

        check(await pg.locator("#btnWhenClear").is_visible(), "bouton ✕ pour effacer la date")
        await pg.evaluate("() => { document.querySelector('#toast').textContent = ''; }")
        await pg.click("#btnWhenClear")
        check(await pg.locator("#btnStart").inner_text() == "Envoyer l'assistant", "date effacée : « Envoyer l'assistant » revient")
        await pg.fill("#f-url", "https://teams.example/tout-de-suite")
        await pg.click("#btnStart")
        await toast(pg, "rejoint la réunion")
        st = state()
        check(len(st["started"]) == 1 and st["started"][0]["url"].endswith("/tout-de-suite") and len(st["schedule"]) == 3,
              "sans date : lancée tout de suite (comme avant)")

        await pg.goto(BASE + "/#home")
        await pg.wait_for_function("() => document.querySelectorAll('#schedule li').length === 3", timeout=8000)
        await pg.locator("#schedule li button").first.click()
        await pg.wait_for_function("() => document.querySelectorAll('#schedule li').length === 2", timeout=8000)
        check(len(state()["schedule"]) == 2, "« Annuler » retire la planification")

        # une planification à la minute suivante : le planificateur du contrôle la lance à l'heure (tour de 15 s)
        soon = (dt.datetime.now() + dt.timedelta(minutes=1)).replace(second=0, microsecond=0).astimezone()
        await fill(pg, "https://teams.example/bientot", local(soon))
        await pg.fill("#f-title", "Point d'essai")
        await pg.click("#btnSchedule")
        await toast(pg, "planifiée")
        check(not any(x["url"].endswith("/bientot") for x in state()["started"]), "pas lancée avant l'heure")
        for _ in range(60):
            await asyncio.sleep(2)
            s = [x for x in state()["started"] if x["url"].endswith("/bientot")]
            if s:
                break
        at = dt.datetime.fromisoformat(s[0]["at"]) if s else None
        check(bool(s) and s[0]["title"] == "Point d'essai" and s[0]["by"] == "essai" and at >= soon
              and (at - soon).total_seconds() < 25, f"lancée à l'heure par le planificateur ({s[0]['at'] if s else 'jamais'})")
        await pg.wait_for_function("() => [...document.querySelectorAll('#schedule li')].some(l => l.innerText.includes('lancée sur wA'))", timeout=40000)
        check(True, "liste : « lancée sur wA »")
        await pg.screenshot(path=str(OUT / "sched-desktop.png"))

        ph = await page(390, 844)
        await fill(ph, "https://teams.example/rdv-tel", local(later))
        ov = await ph.evaluate("""() => { const b = document.querySelector('#btnStart'), g = document.querySelector('.opts-grid').getBoundingClientRect();
            const inside = ['#f-when', '#btnWhenClear', '#btnSchedule'].every(q => document.querySelector(q).getBoundingClientRect().right <= g.right + 1);
            return document.documentElement.scrollWidth <= innerWidth + 1 && b.scrollWidth <= b.clientWidth + 1 && inside; }""")
        check(ov, "téléphone : bouton « Planifier · … » lisible, date et boutons dans le cadre, pas de défilement horizontal")
        await ph.screenshot(path=str(OUT / "sched-phone.png"), full_page=True)
        await br.close()
    print(f"\n{ok} ok, {fails} échec(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    serve() if sys.argv[1:] == ["serve"] else asyncio.run(run())
