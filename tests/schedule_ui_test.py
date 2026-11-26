"""Planifier une réunion depuis l'accueil : une date remplie dans « Plus tard » planifie TOUJOURS (bouton « Planifier »,
bouton principal ou touche Entrée), elle ne lance jamais l'assistant tout de suite ; puis la planification démarre bien à
l'heure. Liste « Planifiées » (26/11) : en haut à côté de « Réunions en cours », rien ne dépasse à droite, les réunions
passées (lancées, ou manquées depuis plus de 2 h) n'y sont plus, et un lien Outlook « vrai-lien<safelinks…> » est coupé au
« < ». Contrôle d'essai avec un assistant simulé (données fictives, aucun vrai assistant).

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

    @A.app.post("/test/inject")
    async def test_inject(items: list[dict]):     # planifications fictives (passées, manquées, lien Outlook très long)
        A.save_schedule(A.load_schedule() + items)
        return {"ok": True}

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
        await pg.wait_for_function("() => ![...document.querySelectorAll('#schedule li')].some(l => l.innerText.includes(\"Point d'essai\"))", timeout=40000)
        check(True, "lancée : elle quitte la liste « Planifiées » (elle est dans « Réunions en cours »)")

        # lien copié d'un courriel Outlook en texte brut, planifié depuis le formulaire : enregistré propre
        T = "https://teams.microsoft.com/meet/1234567890123456?p=AbCdEf0123456789xy"
        polluted = (T + "%3Chttps://fra01.safelinks.protection.outlook.com/ap/t-0000abcd/?url=https%3A%2F%2Fteams.microsoft.com"
                    "%2Fmeet%2F1234567890123456%3Fp%3DAbCdEf0123456789xy&data=05%7C02%7C" + "x" * 300 + "&reserved=0")
        await fill(pg, polluted, local(later + dt.timedelta(hours=1)))
        await pg.click("#btnSchedule")
        await toast(pg, "planifiée")
        st = state()
        check(any(x["url"] == T for x in st["schedule"]), "lien « vrai-lien%3Csafelinks… » : enregistré coupé au « < » (code p= intact)")

        now = dt.datetime.now(dt.timezone.utc)
        g = "https://www.google.com/url?q=https://fra01.safelinks.protection.outlook.com/ap/t-0000abcd/?url%3Dhttps%253A%252F%252Fteams.microsoft.com%252Fl%252Fmeetup-join%252F19%25253ameeting_" + "Z" * 400
        rj = ctl.post("/test/inject", json=[
            {"id": "passee1", "url": "https://teams.example/hier", "title": "Réunion d'hier", "when": (now - dt.timedelta(days=1)).isoformat(),
             "status": "started", "result": "wA", "owner": "essai"},
            {"id": "manq_old", "url": "https://teams.example/vieille", "title": "Manquée il y a 5 h", "when": (now - dt.timedelta(hours=5)).isoformat(),
             "status": "missed", "result": "trop tard (> 30 min)", "owner": "essai"},
            {"id": "manq_new", "url": "https://teams.example/recente", "title": "Manquée il y a 40 min", "when": (now - dt.timedelta(minutes=40)).isoformat(),
             "status": "missed", "result": "trop tard (> 30 min)", "owner": "essai"},
            {"id": "long1", "url": g, "when": (now + dt.timedelta(hours=3)).isoformat(), "status": "pending", "owner": "essai"},
        ])
        check(rj.status_code == 200, f"planifications fictives ajoutées ({rj.status_code} {rj.text[:200]})")
        await pg.reload()                                   # (goto vers la même adresse #home ne recharge pas la page)
        await pg.wait_for_function("() => document.querySelectorAll('#schedule li').length === 5", timeout=10000)
        txt = await pg.locator("#schedule").inner_text()
        if os.environ.get("DEBUG"):
            print("DEBUG état :", [(x["id"], x["status"]) for x in state()["schedule"]])
            print("DEBUG api :", await pg.evaluate("() => fetch('/api/schedule').then(r => r.json()).then(a => a.map(x => x.id + ':' + x.status))"))
            print("DEBUG liste :", txt.replace("\n", " | ")[:600])
        check("Réunion d'hier" not in txt and "Point d'essai" not in txt and "lancée" not in txt, "réunions passées et lancées absentes de la liste")
        check("Manquée il y a 5 h" not in txt, "manquée depuis plus de 2 h : absente")
        check("Manquée il y a 40 min" in txt and "⚠️ manquée : trop tard" in txt, "manquée depuis 40 min : encore signalée (⚠️ manquée)")
        check("safelinks" not in txt and "google.com" not in txt and "Réunion Teams" in txt,
              "lien sans titre : « Réunion Teams » au lieu du lien illisible")
        lay = await pg.evaluate("""() => { const r = q => document.querySelector(q).getBoundingClientRect();
            const live = r('#topRow > section:first-child'), sch = r('#schedSec'), main = r('main');
            const lis = [...document.querySelectorAll('#schedule li')].map(l => l.getBoundingClientRect());
            const btns = [...document.querySelectorAll('#schedule li button')].map(b => b.getBoundingClientRect());
            return { side: sch.left >= live.right - 1 && Math.abs(sch.top - live.top) < 2,
                     above: sch.bottom <= r('#recentSec').top || document.querySelector('#recentSec').classList.contains('hidden'),
                     inside: lis.every(b => b.right <= main.right + 1) && btns.every(b => b.right <= sch.right + 1 && b.width > 0),
                     noscroll: document.documentElement.scrollWidth <= innerWidth + 1 }; }""")
        check(lay["side"], "ordinateur : « Planifiées » en haut, à droite de « Réunions en cours »")
        check(lay["inside"] and lay["noscroll"], "ordinateur : rien ne dépasse à droite (boutons visibles), pas de défilement horizontal")
        await pg.screenshot(path=str(OUT / "sched-desktop.png"))

        ph = await page(390, 844)
        await fill(ph, "https://teams.example/rdv-tel", local(later))
        ov = await ph.evaluate("""() => { const b = document.querySelector('#btnStart'), g = document.querySelector('.opts-grid').getBoundingClientRect();
            const inside = ['#f-when', '#btnWhenClear', '#btnSchedule'].every(q => document.querySelector(q).getBoundingClientRect().right <= g.right + 1);
            return document.documentElement.scrollWidth <= innerWidth + 1 && b.scrollWidth <= b.clientWidth + 1 && inside; }""")
        check(ov, "téléphone : bouton « Planifier · … » lisible, date et boutons dans le cadre, pas de défilement horizontal")
        lay = await ph.evaluate("""() => { const r = q => document.querySelector(q).getBoundingClientRect();
            const live = r('#topRow > section:first-child'), sch = r('#schedSec');
            const ask = document.querySelector('#askSec'), rec = document.querySelector('#recentSec');
            const after = [ask, rec].filter(e => !e.classList.contains('hidden')).every(e => e.getBoundingClientRect().top >= sch.bottom - 1);
            const items = [...document.querySelectorAll('#schedule li, #schedule li button')].every(e => e.getBoundingClientRect().right <= innerWidth + 1);
            return { below: sch.top >= live.bottom - 1 && after, items, noscroll: document.documentElement.scrollWidth <= innerWidth + 1 }; }""")
        check(lay["below"], "téléphone : « Planifiées » juste sous « Réunions en cours », avant le reste")
        check(lay["items"] and lay["noscroll"], "téléphone : lignes et boutons de « Planifiées » dans l'écran")
        await ph.screenshot(path=str(OUT / "sched-phone.png"), full_page=True)
        await br.close()
    print(f"\n{ok} ok, {fails} échec(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    serve() if sys.argv[1:] == ["serve"] else asyncio.run(run())
