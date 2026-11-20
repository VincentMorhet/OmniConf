"""IHM de « En ce moment » : chemin du sujet (sujet › sous-sujet › point), niveau qui vient de changer mis en
évidence, mise à jour sans attendre le point d'étape suivant, fil des sujets, vignette de l'accueil. Contrôle d'essai
avec un assistant simulé (données fictives, aucun vrai assistant).

    TEST_DATA_DIR=/tmp/nowui/data TEST_PORT=8099 ADMIN_PASSWORD=… python tests/now_ui_test.py serve   # contrôle d'essai
    PW=… BASE=http://127.0.0.1:8099 python tests/now_ui_test.py                                       # navigateur
"""
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("TEST_DIR", "/tmp/wk"))


def iso(sec_ago: float) -> str:
    return (dt.datetime.now().astimezone() - dt.timedelta(seconds=sec_ago)).isoformat(timespec="seconds")


def serve():
    data = Path(os.environ.get("TEST_DATA_DIR", "/tmp/nowui/data"))     # jamais le DATA_DIR du vrai contrôle
    data.mkdir(parents=True, exist_ok=True)
    (data / "voice_questions.json").write_text(json.dumps({"items": {}, "answers": {}}))
    (data / "voice_consents.json").write_text("[]")
    os.environ.update(DATA_DIR=str(data), ADMIN_LOGIN="essai", ADMIN_NAME="Compte Essai", COOKIE_SECURE="0",
                      SECRET_KEY=os.urandom(16).hex(), AGENT_TOKEN="essai-sans-secret", WORKERS="wA=http://127.0.0.1:9")
    sys.path.insert(0, str(ROOT / "control"))
    os.chdir(ROOT / "control")
    import app as A
    import uvicorn

    NOW = {"chemin": ["Budget 2027", "Ventilation par lot"], "niveau": 2, "depuis": 310.0, "changed_at": iso(5),
           "en_cours": "{S1} détaille la part de l'hébergement", "v": 3, "at": iso(5), "running": False, "error": None,
           "secs": 0.8, "every": 20, "model": "petit-modele", "live": True,
           "hist": [{"t": 5.0, "chemin": ["Ordre du jour"], "niveau": None},
                    {"t": 60.0, "chemin": ["Budget 2027", "Enveloppe globale"], "niveau": 1},
                    {"t": 310.0, "chemin": ["Budget 2027", "Ventilation par lot"], "niveau": 2}]}
    W = {"wA": {"state": "in_meeting", "owner": "essai", "last_owner": "essai", "viewers": [], "platform": "teams",
                "title": "Comité budgétaire (essai)", "url": "https://teams.example/x", "since": iso(600),
                "joined_at": iso(600), "recording": True, "recording_file": "20261120-090000_teams.mp4",
                "recording_since": iso(590), "listeners": 0, "vnc_port": 8096, "me_name": "Compte Essai",
                "transcript": {"active": True, "enabled": True, "file": "20261120-090000_teams", "started_at": iso(590),
                               "lines": 3, "paragraphs": 3, "error": None, "n_speakers": 2, "slides": 0,
                               "analysis": {"v": 1, "running": False, "at": iso(120), "next_in": 180, "error": None,
                                            "count": 1, "every": 300, "now": NOW}}}}
    SP = {"S1": {"label": "Alice Martin", "color": 1, "source": "ui"}, "S2": {"label": "Bruno Petit", "color": 2, "source": "ui"}}
    fetches = {"analysis": 0}

    async def worker_status(name):
        return {**json.loads(json.dumps(W[name])), "name": name, "online": True}

    class R:
        status_code = 200

        def __init__(self, d=None, content=b""):
            self.d, self.content = d or {}, content

        def json(self):
            return self.d

    async def wcall(name, method, path, **kw):
        tr = W[name]["transcript"]
        if path.startswith("/transcript/analysis"):
            fetches["analysis"] += 1
            return R({**tr["analysis"], "state": {"en_cours": "ANCIEN-EN-COURS (point d'étape)", "synthese": "Le comité passe en revue le budget.",
                                                  "sujets": [{"titre": "Budget 2027", "t": "01:00", "statut": "en cours"}]},
                      "flashes": [], "upto": 300, "speakers": SP, "aliases": {}})
        if path.startswith("/transcript/slides"):
            return R({"file": tr["file"], "slides": []})
        if path.startswith("/transcript"):
            return R({"file": tr["file"], "total": 1, "paragraphs": [{"id": 1, "t": 5.0, "spk": "S1", "text": "Bonjour."}],
                      "pending": [], "slides": 0, "speakers": SP, "aliases": {}})
        if path.startswith("/screenshot"):
            return R(content=b"\xff\xd8\xff\xd9")
        return R({"ok": True})

    A.worker_status, A.wcall = worker_status, wcall

    @A.app.post("/test/now")
    async def test_now(body: dict):
        if body.get("changed_ago") is not None:
            body["changed_at"] = iso(body.pop("changed_ago"))
        W["wA"]["transcript"]["analysis"]["now"].update(body)
        return {"ok": True}

    @A.app.get("/test/state")
    async def test_state():
        return fetches

    uvicorn.run(A.app, host="127.0.0.1", port=int(os.environ.get("TEST_PORT", "8099")), log_level="warning")


ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what, flush=True)


PANEL = """() => { const c = document.querySelector('.worker.meet:not(.hidden)'), s = c && c.querySelector('.an-body .an-sec.now');
  if (!s) return null;
  return { path: [...s.querySelectorAll('.now-path .lvl')].map(e => e.innerText), chg: [...s.querySelectorAll('.now-path .lvl.chg')].map(e => e.innerText),
           badge: s.querySelector('.now-badge')?.innerText || '', text: s.innerText,
           hist: [...c.querySelectorAll('.an-body .now-hist li')].map(e => e.innerText),
           body: c.querySelector('.an-body').innerText }; }"""


async def run():
    import httpx
    from playwright.async_api import async_playwright
    BASE, PW = os.environ["BASE"], os.environ["PW"]
    ctl = httpx.Client(base_url=BASE)
    async with async_playwright() as p:
        br = await p.chromium.launch(channel="chrome", headless=True)

        async def page(w, h, hash_):
            pg = await (await br.new_context(viewport={"width": w, "height": h})).new_page()
            await pg.goto(BASE + "/" + hash_)
            await pg.fill("#loginId", "essai")
            await pg.fill("#pw", PW)
            await pg.click("#loginForm button")
            await pg.wait_for_selector("#tab-home:not(.hidden), .worker.meet:not(.hidden)", timeout=15000)
            return pg

        pg = await page(1280, 900, "#home")
        await pg.wait_for_selector("#liveGrid .tile:not(.hidden) .tnow:not(.hidden)", timeout=10000)
        t0 = await pg.locator("#liveGrid .tile:not(.hidden) .tnow").inner_text()
        check("S1" not in t0, f"accueil : jamais d'identifiant de voix brut ({t0!r})")
        await pg.wait_for_function("() => document.querySelector('#liveGrid .tile:not(.hidden) .tnow')?.innerText.includes('Alice')", timeout=15000)
        tn = await pg.locator("#liveGrid .tile:not(.hidden) .tnow").inner_text()
        check("Nouveau sous-sujet" in tn and "Budget 2027 › Ventilation par lot" in tn and "Alice Martin" in tn,
              f"accueil : sujet en cours, changement récent et voix nommée ({tn!r})")
        await pg.goto(BASE + "/#m/wA")
        await pg.wait_for_function(f"() => ({PANEL})()?.path?.length === 2", timeout=10000)
        v = await pg.evaluate(PANEL)
        check(v["path"] == ["Budget 2027", "Ventilation par lot"] and v["chg"] == ["Ventilation par lot"],
              f"chemin du sujet ; seul le niveau qui a changé est en évidence ({v['chg']})")
        check(v["badge"] == "↪ Nouveau sous-sujet" and "Alice Martin détaille" in v["text"] and "depuis 05:10" in v["text"],
              f"badge, phrase de l'instant, heure du changement ({v['badge']!r})")
        check("ANCIEN-EN-COURS" not in v["body"], "la phrase du point d'étape (5 min) est remplacée par la plus récente")
        check(len(v["hist"]) == 3 and "Ventilation par lot" in v["hist"][0] and "Ordre du jour" in v["hist"][-1],
              f"fil des sujets, du plus récent au plus ancien ({len(v['hist'])})")
        await pg.screenshot(path=str(OUT / "now-desktop.png"))

        n0 = ctl.get("/test/state").json()["analysis"]
        ctl.post("/test/now", json={"chemin": ["Recrutements", "Postes ouverts"], "niveau": 1, "depuis": 400.0,
                                    "changed_ago": 1, "en_cours": "{S2} annonce trois postes", "v": 4,
                                    "hist": [{"t": 5.0, "chemin": ["Ordre du jour"], "niveau": None},
                                             {"t": 60.0, "chemin": ["Budget 2027", "Enveloppe globale"], "niveau": 1},
                                             {"t": 310.0, "chemin": ["Budget 2027", "Ventilation par lot"], "niveau": 2},
                                             {"t": 400.0, "chemin": ["Recrutements", "Postes ouverts"], "niveau": 1}]})
        await pg.wait_for_function(f"() => ({PANEL})()?.path?.[0] === 'Recrutements'", timeout=15000)
        v = await pg.evaluate(PANEL)
        check(v["badge"] == "🔀 Nouveau sujet" and v["chg"] == ["Recrutements", "Postes ouverts"] and "Bruno Petit annonce" in v["text"],
              f"nouveau sujet affiché en quelques secondes, tous ses niveaux en évidence ({v['chg']})")
        check(ctl.get("/test/state").json()["analysis"] == n0, "sans relire tout le compte rendu en direct (pas de nouvel appel)")
        ctl.post("/test/now", json={"changed_ago": 300, "v": 5, "en_cours": "{S2} détaille les profils"})
        await pg.wait_for_function(f"() => ({PANEL})()?.text?.includes('profils')", timeout=15000)
        v = await pg.evaluate(PANEL)
        check(not v["badge"] and not v["chg"], "changement ancien (plus de 2 min) : plus de mise en évidence")
        ph = await page(390, 844, "#m/wA")
        await ph.wait_for_function(f"() => ({PANEL})()?.path?.length === 2", timeout=10000)
        ov = await ph.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
        check(ov, "téléphone : pas de défilement horizontal")
        await ph.screenshot(path=str(OUT / "now-phone.png"), full_page=True)
        ctl.post("/test/now", json={"live": False, "v": 6})
        await pg.wait_for_function("() => document.querySelector('.worker.meet:not(.hidden) .an-body')?.innerText.includes('ANCIEN-EN-COURS')", timeout=15000)
        check(True, "fin du direct : retour à la phrase du point d'étape")
        await br.close()
    print(f"\n{ok} ok, {fails} échec(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    serve() if sys.argv[1:] == ["serve"] else asyncio.run(run())
