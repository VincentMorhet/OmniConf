"""Import d'enregistrements passés : IHM, contrôle et assistants, de bout en bout, sans réseau ni IA.

Vrai contrôle (données fictives, comptes « essai » et « autre ») ; deux assistants simulés qui font tourner le vrai
module d'import (worker/importer.py : envoi par morceaux, conversion ffmpeg) avec une transcription et un compte rendu
imités. Les fichiers sont générés (mire et bip) ; rien n'atteint un vrai assistant.

    # dans un conteneur d'assistant (ffmpeg) : deux assistants simulés, ports 8098 et 8097
    python tests/import_ui_test.py worker 8098 wA &  python tests/import_ui_test.py worker 8097 wB &
    # sur un hôte qui a le venv du contrôle (aucun fichier d'environnement réel : tout est fictif)
    python tests/import_ui_test.py serve                                 # port 8099
    # dans un conteneur qui a Chrome (réseau de l'hôte)
    BASE=http://127.0.0.1:8099 PW=… python tests/import_ui_test.py
"""
import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WK = Path(os.environ.get("TEST_DIR", "/tmp/wk"))

FAKE_OFFLINE = r'''
import json, os, sys, time
from pathlib import Path
rec = Path(os.environ["RECORDINGS_DIR"]); args = sys.argv[1:]; stem = args[0][:-4]
meta = json.loads((rec / (stem + ".mp4.json")).read_text())
for what in ("image", "image", "son", "son"):
    print(f"offline INFO … {what} 0 min", flush=True); time.sleep(1.5)
(rec / (stem + ".transcript.json")).write_text(json.dumps({"meta": {**meta, "offline": {"why": "essai"}}, "raw": [],
                                                            "paragraphs": [], "speakers": {}}))
print("offline INFO terminé en 6 s", flush=True)
'''


# ----------------------------------------------------------------------------- assistant simulé
def worker(port: int, name: str):
    import uvicorn
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse
    sys.path.insert(0, str(ROOT / "worker"))
    import importer as I
    from recorder import Recorder
    rec_dir = WK / f"rec-{name}"
    rec = Recorder(str(rec_dir), ":99", "1280x720")
    fake = WK / "fake_offline.py"
    fake.write_text(FAKE_OFFLINE)
    reports: dict = {}

    def launch_report(stem):
        async def run():
            await asyncio.sleep(2)
            (rec_dir / (stem + ".report.json")).write_text(json.dumps({"cr": {"titre": None}}))
            reports[stem].update(state="done", step=None)
        reports[stem] = {"state": "running", "step": "rédaction"}
        asyncio.get_running_loop().create_task(run())
        return True

    imp = I.Importer(rec_dir, name, launch_report=launch_report, report_state=lambda s: reports.get(s))
    imp.offline_cmd = [sys.executable, str(fake)]
    app = FastAPI()
    app.include_router(I.router(imp, []))

    @app.on_event("startup")
    async def _start():
        imp.start()

    @app.get("/status")
    async def status():
        return {"state": "idle", "name": name, "imports": imp.status(), "transcript": {"active": False}}

    @app.get("/disk")
    async def disk():
        import shutil
        u = shutil.disk_usage(rec_dir)
        return {"total": u.total, "used": u.used, "free": u.free}

    @app.get("/recordings")
    async def recordings():
        items = rec.list()
        for x in items:
            stem = rec.stem(x["name"])
            x.update(report=(rec_dir / (stem + ".report.json")).exists(), cr_title=None, final_cr=False,
                     report_job=reports.get(stem) if (reports.get(stem) or {}).get("state") == "running" else None,
                     slides_job=None, import_job=imp.public(imp.for_stem(stem)))
        return items

    @app.get("/recordings/{fname}/thumb")
    async def thumb(fname: str):
        t = await rec.thumbnail(fname)
        if not t:
            raise HTTPException(404)
        return FileResponse(t, media_type="image/jpeg")

    @app.get("/recordings/{fname}")
    async def file(fname: str):
        p = rec.path(fname)
        if not p:
            raise HTTPException(404)
        return FileResponse(p, media_type="video/mp4")

    @app.get("/test/chunks")
    async def chunks():
        return {"offsets": offsets}

    offsets: list = []

    @app.middleware("http")
    async def note(request, call_next):
        if request.method == "PUT" and request.url.path.startswith("/imports/"):
            offsets.append(int(request.query_params.get("offset", -1)))
        return await call_next(request)

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


# ----------------------------------------------------------------------------- contrôle d'essai
def serve():
    data = Path(os.environ.get("TEST_DATA_DIR", "/tmp/ctltest/data"))     # jamais le DATA_DIR du vrai contrôle
    data.mkdir(parents=True, exist_ok=True)
    (data / "voice_questions.json").write_text(json.dumps({"items": {}, "answers": {}}))
    os.environ.update(DATA_DIR=str(data), ADMIN_LOGIN="essai", ADMIN_NAME="Compte Essai", COOKIE_SECURE="0",
                      SECRET_KEY=os.urandom(16).hex(), AGENT_TOKEN="essai-sans-secret",
                      WORKERS="wA=http://127.0.0.1:8098,wB=http://127.0.0.1:8097")
    sys.path.insert(0, str(ROOT / "control"))
    os.chdir(ROOT / "control")
    import app as A
    import uvicorn

    async def no_push(names=None):
        return {}
    A.voiceprints_push = no_push
    A.vp_start_build = lambda names, by: {"state": "done"}

    @A.app.get("/test/chunks/{w}")
    async def chunks(w: str):
        return (await A.http.get(A.WORKERS[w] + "/test/chunks")).json()
    uvicorn.run(A.app, host="127.0.0.1", port=int(os.environ.get("TEST_PORT", "8099")), log_level="warning")


# ----------------------------------------------------------------------------- essais
ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what, flush=True)


def ff(*args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def make_media() -> dict:
    from zoneinfo import ZoneInfo
    d = WK / "media"
    d.mkdir(parents=True, exist_ok=True)
    m = {"teams": d / "Point projet-20261103_140512-Enregistrement de la réunion.mp4",
         "zoom": d / "GMT20261029-083000_Recording_1920x1080.mp4",
         "mp3": d / "dictaphone.mp3", "obs": d / "obs 2026-10-28 09-15-00.webm",
         "big": d / "Revue sprint-20261020_100000-Enregistrement de la réunion.mp4",
         "big2": d / "Atelier-20261021_100000-Enregistrement de la réunion.mp4",
         "junk": d / "notes.mp4", "txt": d / "notes.txt"}
    ff("-f", "lavfi", "-i", "testsrc=s=640x360:r=10:d=12", "-f", "lavfi", "-i", "sine=f=440:d=12", "-c:v", "libx264",
       "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(m["teams"]))
    ff("-f", "lavfi", "-i", "testsrc=s=640x360:r=10:d=10", "-f", "lavfi", "-i", "sine=f=330:d=10", "-c:v", "libx264",
       "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(m["zoom"]))
    ff("-f", "lavfi", "-i", "sine=f=500:d=9", "-c:a", "libmp3lame", "-b:a", "64k", str(m["mp3"]))
    ts = dt.datetime(2026, 10, 15, 16, 0, 0, tzinfo=ZoneInfo("Europe/Paris")).timestamp()
    os.utime(m["mp3"], (ts, ts))                 # date du fichier : fin de l'enregistrement
    ff("-f", "lavfi", "-i", "testsrc=s=320x240:r=10:d=8", "-f", "lavfi", "-i", "sine=f=300:d=8", "-c:v", "libvpx",
       "-b:v", "300k", "-c:a", "libopus", "-shortest", str(m["obs"]))
    for k in ("big", "big2"):                    # ~40 Mo : trois morceaux de 16 Mo
        ff("-f", "lavfi", "-i", "testsrc=s=1280x720:r=25:d=20", "-f", "lavfi", "-i", "sine=f=440:d=20",
           "-vf", "noise=alls=40:allf=t", "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "16M", "-maxrate", "16M",
           "-bufsize", "8M", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(m[k]))   # bruit : ne se compresse pas
    m["junk"].write_bytes(os.urandom(80_000))
    m["txt"].write_text("pas une vidéo\n")
    return m


async def cards(pg) -> list[dict]:
    return await pg.evaluate("""() => [...document.querySelectorAll('#recordings .ritem')].map(el => ({
        title: el.querySelector('.rtitle').textContent, meta: el.querySelector('.rmeta').textContent,
        chips: [...el.querySelectorAll('.rtags .chip')].map(c => c.textContent)}))""")


TOASTS = "() => { window.__toasts = []; const o = toast; toast = (m, ms) => { window.__toasts.push(String(m)); return o(m, ms); }; }"


async def toasted(pg, text, timeout=10):
    return await wait_for(lambda: pg.evaluate("(t) => (window.__toasts || []).find(x => x.includes(t)) || ''", text), timeout, 0.2)


async def wait_for(pred, timeout=120, step=0.5):
    t = time.time()
    while time.time() - t < timeout:
        r = await pred()
        if r:
            return r
        await asyncio.sleep(step)
    return None


async def main():
    import httpx
    from playwright.async_api import async_playwright
    BASE, PW = os.environ["BASE"], os.environ["PW"]
    media = make_media()
    me = httpx.Client(base_url=BASE, timeout=60)
    check(me.post("/api/login", json={"login": "essai", "password": PW}).status_code == 200, "connexion (essai)")
    pw2 = me.post("/api/users", json={"login": "autre", "name": "Compte Autre", "role": "user"}).json().get("password")
    other = httpx.Client(base_url=BASE, timeout=60)
    check(other.post("/api/login", json={"login": "autre", "password": pw2}).status_code == 200, "connexion (autre)")

    # --- API : chacun ses imports
    small = media["teams"]
    j = me.post("/api/imports", json={"file": small.name, "size": small.stat().st_size, "last_modified": 1, "title": "Essai API"}).json()
    w, jid = j["worker"], j["id"]
    check(j["stage"] == "upload" and w in ("wA", "wB"), f"import créé sur un assistant ({w})")
    check(other.put(f"/api/imports/{w}/{jid}?offset=0", content=b"x").status_code == 403, "autre : envoi dans l'import d'essai refusé")
    check(other.delete(f"/api/imports/{w}/{jid}").status_code == 403, "autre : abandon de l'import d'essai refusé")
    check(other.get("/api/imports").json() == [], "autre : ne voit pas les imports d'essai")
    check(any(x["id"] == jid for x in me.get("/api/imports").json()), "essai : voit son import")
    r = me.post("/api/imports", json={"file": "notes.txt", "size": 10})
    check(r.status_code == 400 and "format non pris en charge" in r.json()["detail"], "extension .txt refusée")
    check(me.delete(f"/api/imports/{w}/{jid}").status_code == 200 and not me.get("/api/imports").json(), "import abandonné : effacé")

    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        ctx = await b.new_context(viewport={"width": 1280, "height": 900}, locale="fr-FR", timezone_id="Europe/Paris")
        pg = await ctx.new_page()
        errs, dialogs, puts = [], [], []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("dialog", lambda d: (dialogs.append(d.type), asyncio.ensure_future(d.accept())))
        pg.on("request", lambda rq: puts.append(rq.url) if rq.method == "PUT" else None)
        await pg.goto(BASE + "/#rec")
        await pg.fill("#loginId", "essai")
        await pg.fill("#pw", PW)
        await pg.click("#loginForm button")
        await pg.wait_for_selector("#importBtn", state="visible")
        await pg.evaluate(TOASTS)

        # --- trois fichiers d'un coup : titre, date et plateforme devinés
        await pg.set_input_files("#importFile", [str(media["teams"]), str(media["zoom"]), str(media["mp3"])])
        await pg.wait_for_selector("#dlgImport[open] .imp-row:nth-child(3)")
        rows = await pg.evaluate("""() => [...document.querySelectorAll('#impList .imp-row')].map(r => ({
            t: r.querySelector('.i-title').value, d: r.querySelector('.i-date').value, p: r.querySelector('.i-plat').value,
            src: r.querySelector('.imp-src').textContent, info: r.querySelector('.imp-file small').textContent}))""")
        check(rows[0]["t"] == "Point projet" and rows[0]["d"] == "2026-11-03T14:05" and rows[0]["p"] == "teams",
              f"Teams : titre, date et plateforme lus dans le nom ({rows[0]})")
        check(rows[1]["t"] == "" and rows[1]["d"] == "2026-10-29T09:30" and rows[1]["p"] == "zoom",
              f"Zoom (« GMT… ») : heure UTC remise à l'heure de Paris ({rows[1]})")
        check(rows[2]["t"] == "dictaphone" and rows[2]["d"] == "2026-10-15T15:59" and "moins la durée" in rows[2]["src"]
              and rows[2]["p"] == "web", f"son sans date dans le nom : date du fichier moins la durée ({rows[2]})")
        check("0:12" in rows[0]["info"] or "12" in rows[0]["info"], f"taille et durée affichées ({rows[0]['info']})")
        await pg.screenshot(path=str(WK / "import-dialog.png"))
        check(await pg.is_checked("#impTr"), "« Transcrire et faire le compte rendu » coché par défaut")
        await pg.click("#impOk")
        seen_chips, seen_rows = set(), set()

        async def done3():
            cs = await cards(pg)
            for c in cs:
                seen_chips.update(c["chips"])
            seen_rows.update(await pg.evaluate("() => [...document.querySelectorAll('#imports .imp small')].map(x => x.textContent.split(' ')[0])"))
            imp = [c for c in cs if "Importée" in c["chips"]]
            return imp if len(imp) == 3 and all("Compte rendu" in c["chips"] for c in imp) else None
        imp = await wait_for(done3, 150, 0.3)
        check(imp is not None, f"trois réunions importées, transcrites et résumées ({await cards(pg) if not imp else 'ok'})")
        check(any(x.startswith("Transcription") for x in seen_chips), f"avancement montré sur la carte ({sorted(seen_chips)})")
        check({"Conversion", "Transcription"} <= seen_rows, f"travaux suivis dans la liste des imports ({sorted(seen_rows)})")
        cs = {c["title"]: c for c in await cards(pg)}
        check("Point projet" in cs and cs["Point projet"]["meta"].startswith("14:05 · Teams"), f"carte Teams : {cs.get('Point projet')}")
        check("Réunion importée" in cs and cs["Réunion importée"]["meta"].startswith("09:30 · Zoom"), f"carte Zoom : {cs.get('Réunion importée')}")
        check("dictaphone" in cs and cs["dictaphone"]["meta"].startswith("15:59 · Web"), f"carte du son : {cs.get('dictaphone')}")
        days = await pg.evaluate("() => [...document.querySelectorAll('#recordings h3.day')].map(h => h.textContent)")
        check(any("3 novembre" in d for d in days) and any("29 octobre" in d for d in days), f"rangées à leur date ({days})")
        recs = me.get("/api/recordings").json()
        check(len({x["worker"] for x in recs}) == 2, f"imports répartis sur les deux assistants ({sorted(x['worker'] for x in recs)})")
        check(all(x["mine"] and x["meta"]["owner"] == "essai" for x in recs), "réunions importées : à essai")
        check(not other.get("/api/recordings").json(), "autre : ne voit pas les réunions importées d'essai")
        x = recs[0]
        check(other.post(f"/api/recordings/{x['worker']}/{x['name']}/transcribe").status_code in (403, 404),
              "autre : transcription d'une réunion d'essai refusée")
        await pg.wait_for_selector("#imports", state="hidden", timeout=15000)
        check(True, "liste des imports vidée une fois tout fini")

        # --- sans transcription, puis « Transcrire et faire le compte rendu »
        await pg.set_input_files("#importFile", str(media["obs"]))
        await pg.wait_for_selector("#dlgImport[open]")
        await pg.uncheck("#impTr")
        await pg.click("#impOk")

        async def obs_card():
            return next((c for c in await cards(pg) if c["title"].startswith("obs") and "Vidéo seule" in c["chips"]), None)
        c = await wait_for(obs_card, 60)
        check(c is not None, f"WebM importé sans transcription : vidéo seule ({c})")
        el = pg.locator("#recordings .ritem", has=pg.locator(".rtitle:text-is('obs')"))
        await el.locator(".more").click()
        check(await el.locator(".trn").is_visible(), "menu : « Transcrire et faire le compte rendu »")
        await el.locator(".trn").click()
        await pg.wait_for_selector("#dlgConfirm[open]")
        await pg.click("#cfYes")

        async def obs_done():
            c = next((c for c in await cards(pg) if c["title"].startswith("obs")), None)
            if c:
                seen_chips.update(c["chips"])
            return c if c and "Compte rendu" in c["chips"] else None
        seen_chips.clear()
        c = await wait_for(obs_done, 90, 0.3)
        check(c is not None and any(x.startswith("Transcription") for x in seen_chips),
              f"vidéo transcrite après coup puis résumée ({sorted(seen_chips)})")
        await pg.evaluate("() => { document.querySelector('#impTr').checked = true; }")

        # --- envoi coupé, page rechargée, reprise au même endroit
        aborted = []

        async def cut(route):
            if route.request.method == "PUT" and "offset=16777216" in route.request.url:
                aborted.append(route.request.url)
                await route.abort()
            else:
                await route.continue_()
        is_put = lambda url: "/api/imports/" in url and "offset=" in url
        await pg.route(is_put, cut)
        await pg.set_input_files("#importFile", str(media["big"]))
        await pg.wait_for_selector("#dlgImport[open]")
        await pg.click("#impOk")
        r = await wait_for(lambda: pg.evaluate("() => document.querySelector('#imports')?.textContent.includes('connexion perdue')"), 30)
        check(bool(aborted) and r, "coupure pendant l'envoi : nouvel essai annoncé")
        await pg.reload()                        # page fermée pendant l'envoi
        await pg.unroute(is_put)
        await pg.wait_for_selector("#importBtn", state="visible")
        await pg.evaluate(TOASTS)
        check("beforeunload" in dialogs, f"quitter la page pendant un envoi : avertissement ({dialogs})")
        row = await wait_for(lambda: pg.evaluate("() => [...document.querySelectorAll('#imports .imp')].map(x => x.textContent).find(t => t.includes('Envoi interrompu'))"), 20)
        want = f"{round(100 * 16777216 / media['big'].stat().st_size)} %"
        check(bool(row) and want in row, f"envoi interrompu affiché avec son avancement ({want} : {row and row.strip()[:120]})")
        puts.clear()
        async with pg.expect_file_chooser() as fc:
            await pg.click("#imports .i-resume")
        await (await fc.value).set_files(str(media["big"]))
        await pg.wait_for_selector("#dlgImport[open]")
        await pg.click("#impOk")
        toast = await toasted(pg, "Reprise de l'envoi")
        check(bool(toast), f"reprise annoncée ({toast})")

        async def big_done():
            return next((c for c in await cards(pg) if c["title"] == "Revue sprint" and "Compte rendu" in c["chips"]), None)
        c = await wait_for(big_done, 120)
        offs = [int(u.split("offset=")[1].split("&")[0]) for u in puts if "/api/imports/" in u and "offset=" in u]
        check(c is not None and offs == [16777216, 33554432], f"reprise à 16 Mo, pas depuis le début (morceaux envoyés : {offs})")

        # --- annulation pendant l'envoi
        async def slow(route):
            await asyncio.sleep(2)
            await route.continue_()
        await pg.route(is_put, slow)
        await pg.set_input_files("#importFile", str(media["big2"]))
        await pg.wait_for_selector("#dlgImport[open]")
        await pg.click("#impOk")
        await pg.wait_for_selector("#imports .i-cancel")
        line = await pg.evaluate("() => document.querySelector('#imports .imp small').textContent")
        check(line.startswith("Envoi : ") and " sur " in line, f"envoi suivi dans la liste des imports ({line})")
        await pg.click("#imports .i-cancel")
        await pg.wait_for_selector("#dlgConfirm[open]")
        await pg.click("#cfYes")
        await asyncio.sleep(3)
        await pg.unroute(is_put)
        js = me.get("/api/imports").json()
        check(not [x for x in js if x["file"] == media["big2"].name and x["stage"] != "terminé"]
              and not [c for c in await cards(pg) if c["title"] == "Atelier"], "envoi annulé : rien ne reste")

        # --- fichier illisible : erreur claire, « Retirer »
        await pg.set_input_files("#importFile", str(media["junk"]))
        await pg.wait_for_selector("#dlgImport[open]")
        await pg.click("#impOk")
        r = await wait_for(lambda: pg.evaluate("() => document.querySelector('#imports .imp.bad')?.textContent || ''"), 30)
        check(r and "fichier illisible" in r and "Réessayer" in r, f"fichier illisible : erreur affichée ({(r or '').strip()[:100]})")
        await pg.click("#imports .imp.bad .i-cancel")
        await pg.wait_for_selector("#dlgConfirm[open]")
        await pg.click("#cfYes")
        await pg.wait_for_selector("#imports .imp.bad", state="detached", timeout=10000)
        check(True, "erreur retirée")
        await pg.set_input_files("#importFile", str(media["txt"]))
        await pg.wait_for_selector("#dlgImport[open]")
        await pg.click("#impOk")
        t = await toasted(pg, "format non pris en charge")
        check(bool(t), f"extension refusée : message ({t})")
        await pg.screenshot(path=str(WK / "import-list.png"), full_page=True)

        # --- téléphone
        ph = await b.new_context(viewport={"width": 390, "height": 844}, locale="fr-FR", timezone_id="Europe/Paris",
                                 is_mobile=True, has_touch=True)
        pp = await ph.new_page()
        pp.on("pageerror", lambda e: errs.append(str(e)))
        await pp.goto(BASE + "/#rec")
        await pp.fill("#loginId", "essai")
        await pp.fill("#pw", PW)
        await pp.click("#loginForm button")
        await pp.wait_for_selector("#importBtn", state="visible")
        await pp.set_input_files("#importFile", [str(media["teams"]), str(media["mp3"])])
        await pp.wait_for_selector("#dlgImport[open] .imp-row:nth-child(2)")
        await asyncio.sleep(0.6)                 # fin de l'animation de la feuille
        box = await pp.evaluate("() => { const r = document.querySelector('#dlgImport').getBoundingClientRect(); return [document.documentElement.scrollWidth, innerWidth, r.left, r.right]; }")
        check(box[0] <= box[1] + 1 and box[2] <= 1 and box[3] >= box[1] - 1,
              f"téléphone : dialogue d'import en pleine largeur, sans défilement horizontal ({box})")
        await pp.screenshot(path=str(WK / "import-phone.png"))
        await pp.click("#dlgImport button[value=cancel]")
        check(not errs, f"aucune erreur JavaScript ({errs[:3]})")
        await b.close()
    me.delete("/api/users/autre")
    print(f"{ok} vérifications réussies, {fails} échec(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    if sys.argv[1:2] == ["worker"]:
        worker(int(sys.argv[2]), sys.argv[3])
    elif sys.argv[1:2] == ["serve"]:
        serve()
    else:
        asyncio.run(main())
