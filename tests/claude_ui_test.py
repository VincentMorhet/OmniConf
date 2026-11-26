"""Clé d'API Anthropic de chaque compte, côté contrôle et IHM : enregistrée chiffrée, jamais réaffichée, envoyée à
l'assistant avant une réunion, un compte rendu ou un import (la clé du PROPRIÉTAIRE de la réunion), retirée partout ;
réglages dans l'IHM ; modèle de « En ce moment » affiché. Contrôle d'essai avec des assistants simulés (fausse clé,
données fictives, aucun appel à Anthropic).

    TEST_DATA_DIR=/tmp/clui/data TEST_PORT=8099 ADMIN_PASSWORD=… python tests/claude_ui_test.py serve   # contrôle
    PW=… BASE=http://127.0.0.1:8099 python tests/claude_ui_test.py                                     # navigateur
"""
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("TEST_DIR", "/tmp/wk"))
KEY = "sk-ant-api03-essai" + "Q" * 60 + "Ab9z"          # fausse clé, jamais une vraie
KEY2 = "sk-ant-api03-refusee" + "R" * 60 + "Zz00"


def iso(sec_ago: float) -> str:
    return (dt.datetime.now().astimezone() - dt.timedelta(seconds=sec_ago)).isoformat(timespec="seconds")


def serve():
    data = Path(os.environ.get("TEST_DATA_DIR", "/tmp/clui/data"))     # jamais le DATA_DIR du vrai contrôle
    data.mkdir(parents=True, exist_ok=True)
    (data / "voice_questions.json").write_text(json.dumps({"items": {}, "answers": {}}))
    (data / "voice_consents.json").write_text("[]")
    os.environ.update(DATA_DIR=str(data), ADMIN_LOGIN="essai", ADMIN_NAME="Compte Essai", COOKIE_SECURE="0",
                      SECRET_KEY=os.urandom(16).hex(), AGENT_TOKEN="essai-sans-secret",
                      WORKERS="wA=http://127.0.0.1:9,wB=http://127.0.0.1:9")
    sys.path.insert(0, str(ROOT / "control"))
    os.chdir(ROOT / "control")
    import app as A
    import uvicorn

    calls = []
    NOW = {"chemin": ["Budget 2027", "Ventilation par lot"], "niveau": 2, "depuis": 310.0, "changed_at": iso(5),
           "en_cours": "{S1} détaille la part de l'hébergement", "v": 3, "at": iso(5), "running": False, "error": None,
           "secs": 1.1, "every": 20, "model": "Claude Haiku 5.5", "claude_error": None, "live": True, "hist": []}
    W = {"wA": {"state": "idle", "owner": None, "viewers": [], "recording": False, "listeners": 0, "vnc_port": 8096,
                "transcript": {"active": False, "enabled": True}},
         "wB": {"state": "in_meeting", "owner": "essai", "last_owner": "essai", "viewers": [], "platform": "teams",
                "title": "Comité (essai)", "url": "https://teams.example/x", "since": iso(600), "joined_at": iso(600),
                "recording": True, "recording_file": "20261126-090000_teams.mp4", "recording_since": iso(590),
                "listeners": 0, "vnc_port": 8096, "me_name": "Compte Essai",
                "transcript": {"active": True, "enabled": True, "file": "20261126-090000_teams", "started_at": iso(590),
                               "lines": 3, "paragraphs": 3, "error": None, "n_speakers": 2, "slides": 0,
                               "analysis": {"v": 1, "running": False, "at": iso(120), "next_in": 180, "error": None,
                                            "count": 1, "every": 300, "now": NOW}}}}
    REC = {"wA": [{"name": "20261125-140000_zoom.mp4", "kind": "video", "transcript": True, "report": True, "size": 1,
                   "mtime": 1, "duration": 600, "recording": False,
                   "meta": {"owner": "bruno", "title": "Réunion de Bruno", "platform": "zoom", "worker": "wA"}}]}
    SP = {"S1": {"label": "Alice Martin", "color": 1, "source": "ui"}}

    async def worker_status(name):
        return {**json.loads(json.dumps(W[name])), "name": name, "online": True}

    async def worker_recordings(n, fresh=False):
        return [{**x, "worker": n} for x in REC.get(n, [])]

    class R:
        def __init__(self, d=None, content=b"", code=200):
            self.d, self.content, self.status_code = d or {}, content, code

        def json(self):
            return self.d

    async def wcall(name, method, path, **kw):
        body = kw.get("json")
        calls.append({"w": name, "path": path, "body": body})
        if path == "/ai/check":
            if body["key"] == KEY2:
                raise A.HTTPException(422, [{"loc": ["body", "key"], "input": body["key"]}])   # ne doit jamais remonter
            return R({"models": {m: "ok" for m in body["models"]}})
        if path == "/ai/remember":
            return R({"ok": True})
        if path.startswith("/transcript/analysis"):
            return R({**W["wB"]["transcript"]["analysis"], "state": {"synthese": "Le comité revoit le budget."},
                      "flashes": [], "upto": 300, "speakers": SP, "aliases": {}})
        if path.startswith("/transcript/slides"):
            return R({"file": "x", "slides": []})
        if path.startswith("/transcript"):
            return R({"file": "x", "total": 0, "paragraphs": [], "pending": [], "slides": 0, "speakers": SP, "aliases": {}})
        if path.startswith("/screenshot"):
            return R(content=b"\xff\xd8\xff\xd9")
        return R({"ok": True, "state": "joining"})

    A.worker_status, A.wcall, A.worker_recordings = worker_status, wcall, worker_recordings

    @A.app.get("/test/state")
    async def test_state():
        f = data / "ai_keys.json"
        return {"calls": calls, "file": f.read_text() if f.exists() else "",
                "mode": oct(f.stat().st_mode & 0o777) if f.exists() else None}

    @A.app.post("/test/reset")
    async def test_reset():
        calls.clear()
        return {"ok": True}

    @A.app.post("/test/now")
    async def test_now(body: dict):
        NOW.update(body)
        NOW["v"] += 1
        return {"ok": True}

    uvicorn.run(A.app, host="127.0.0.1", port=int(os.environ.get("TEST_PORT", "8099")), log_level="warning")


ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what, flush=True)


async def run():
    import httpx
    from playwright.async_api import async_playwright
    BASE, PW = os.environ["BASE"], os.environ["PW"]
    me = httpx.Client(base_url=BASE, timeout=60)
    check(me.post("/api/login", json={"login": "essai", "password": PW}).status_code == 200, "connexion (essai, administrateur)")
    pwb = me.post("/api/users", json={"login": "bruno", "name": "Bruno Fictif", "role": "user"}).json().get("password")
    bru = httpx.Client(base_url=BASE, timeout=60)
    bru.post("/api/login", json={"login": "bruno", "password": pwb})
    bru.post("/api/me/password", json={"current": pwb, "new": pwb + "-Nouveau9"})
    S = lambda: me.get("/test/state").json()
    pushes = lambda: [c for c in S()["calls"] if c["path"] == "/ai/remember"]

    d = me.get("/api/me/ai").json()
    check(not d["configured"] and d["report"] == "claude-fable-5-2" and d["now"] == "claude-haiku-5-5",
          "sans clé : rien de configuré ; Fable 5.2 et Haiku 5.5 proposés par défaut")
    check(me.put("/api/me/ai", json={"key": "pas-une-cle"}).status_code == 400, "clé mal formée refusée")
    check(me.put("/api/me/ai", json={"now": "gpt-4"}).status_code == 400, "modèle inconnu refusé")
    me.post("/test/reset")
    r = me.put("/api/me/ai", json={"key": KEY, "now": "claude-sonnet-5-5"})
    d = r.json()
    check(r.status_code == 200 and d["configured"] and d["hint"] == "…Ab9z" and d["now"] == "claude-sonnet-5-5"
          and KEY not in r.text, f"clé enregistrée : seuls ses 4 derniers caractères sont renvoyés ({d['hint']})")
    st = S()
    check(st["file"] and KEY not in st["file"] and KEY[-30:] not in st["file"] and st["mode"] == "0o600",
          f"fichier des clés : clé chiffrée, droits 600 ({st['mode']})")
    p = pushes()
    check(sorted(c["w"] for c in p) == ["wA", "wB"] and all(c["body"]["owner"] == "essai" and c["body"]["ai"]["key"] == KEY
                                                              and c["body"]["ai"]["now"] == "claude-sonnet-5-5" for c in p),
          "nouveau réglage envoyé aux assistants (mémoire seulement)")
    check(KEY not in me.get("/api/me/ai").text and KEY not in me.get("/api/config").text and KEY not in me.get("/api/me").text,
          "la clé n'est renvoyée par aucune page de l'IHM")
    d = bru.get("/api/me/ai").json()
    check(not d["configured"], "Bruno ne voit pas (et n'utilise pas) la clé d'un autre compte")

    me.post("/test/reset")
    d = me.post("/api/me/ai/test").json()
    chk = [c for c in S()["calls"] if c["path"] == "/ai/check"]
    check(d["check"] == {"Claude Fable 5.2": "ok", "Claude Sonnet 5.5": "ok"} and chk and chk[0]["body"]["key"] == KEY
          and chk[0]["body"]["models"] == ["claude-fable-5-2", "claude-sonnet-5-5"], f"essai de la clé par un assistant ({d['check']})")

    me.post("/test/reset")
    r = me.post("/api/meetings", json={"url": "https://teams.example/rdv", "worker": "wA"})
    c = S()["calls"]
    i_push = next((i for i, x in enumerate(c) if x["path"] == "/ai/remember"), None)
    i_join = next((i for i, x in enumerate(c) if x["path"] == "/meeting/join"), None)
    check(r.status_code == 200 and i_push is not None and i_join is not None and i_push < i_join
          and c[i_push]["body"]["ai"]["key"] == KEY and "ai" not in c[i_join]["body"] and KEY not in json.dumps(c[i_join]["body"]),
          "réunion : réglage Claude envoyé juste avant l'entrée, jamais dans la demande d'entrée")
    me.post("/test/reset")
    me.post("/api/recordings/wA/20261125-140000_zoom.mp4/report")
    p = pushes()
    check(len(p) == 1 and p[0]["body"]["owner"] == "bruno" and p[0]["body"]["ai"] is None,
          "compte rendu refait par un administrateur : clé du propriétaire (Bruno, aucune), jamais celle de l'administrateur")
    me.post("/test/reset")
    me.put("/api/me/ai", json={"report": ""})
    p = pushes()
    check(all(x["body"]["ai"]["report"] is None and x["body"]["ai"]["now"] == "claude-sonnet-5-5" for x in p),
          "« Modèles OVH » pour le compte rendu : seul « En ce moment » passe par Claude")
    me.post("/test/reset")
    me.put("/api/me/ai", json={"now": ""})
    check(all(x["body"]["ai"] is None for x in pushes()), "aucun usage choisi : rien envoyé (modèles OVH partout)")
    me.put("/api/me/ai", json={"report": "claude-fable-5-2", "now": "claude-haiku-5-5"})

    me.put("/api/me/ai", json={"key": KEY2})
    r = me.post("/api/me/ai/test")
    check(r.status_code == 422 and KEY2 not in r.text and KEY2[-20:] not in r.text, f"erreur de l'assistant : jamais la clé dans le message ({r.text[:80]})")
    me.put("/api/me/ai", json={"key": KEY})

    async with async_playwright() as pw:
        br = await pw.chromium.launch(channel="chrome", headless=True)
        errs = []

        async def page(w, h, hash_):
            pg = await (await br.new_context(viewport={"width": w, "height": h}, locale="fr-FR", timezone_id="Europe/Paris")).new_page()
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda dlg: asyncio.ensure_future(dlg.accept()))
            await pg.goto(BASE + "/" + hash_)
            await pg.fill("#loginId", "essai")
            await pg.fill("#pw", PW)
            await pg.click("#loginForm button")
            await pg.wait_for_selector("#tab-home:not(.hidden), #tab-set:not(.hidden), .worker.meet:not(.hidden)", timeout=15000)
            return pg

        # « En ce moment » : modèle affiché, ou erreur de Claude
        pg = await page(1280, 900, "#m/wB")
        sel = ".worker.meet:not(.hidden) .an-body .an-sec.now .meta"
        await pg.wait_for_function(f"() => document.querySelector('{sel}')?.innerText.includes('Claude Haiku 5.5')", timeout=15000)
        check(True, "« En ce moment » : le modèle Claude utilisé est indiqué")
        me.post("/test/now", json={"model": "Qwen3.8-27B", "claude_error": "crédit Anthropic épuisé ou facturation à régler"})
        await pg.wait_for_function(f"() => document.querySelector('{sel}')?.innerText.includes('crédit')", timeout=15000)
        t = await pg.locator(sel).inner_text()
        check("Claude : crédit Anthropic épuisé" in t and "modèle OVH" in t, f"Claude indisponible : on le voit, modèle OVH utilisé ({t!r})")

        # Réglages
        await pg.goto(BASE + "/#reglages")
        await pg.click("#aiBox summary")
        await pg.wait_for_selector("#aiMine .ai-save", timeout=10000)
        txt = await pg.locator("#aiMine").inner_text()
        html = await pg.content()
        check("Clé enregistrée (…Ab9z)" in txt and KEY not in html and KEY[-20:] not in html,
              f"réglages : clé enregistrée indiquée par ses 4 derniers caractères, jamais affichée ({txt.splitlines()[0]!r})")
        check(await pg.locator("#aiKey").get_attribute("type") == "password" and not await pg.input_value("#aiKey"),
              "champ de la clé masqué et vide")
        check(await pg.locator("#aiReport").input_value() == "claude-fable-5-2" and await pg.locator("#aiNow").input_value() == "claude-haiku-5-5",
              "modèles choisis affichés")
        opts = await pg.locator("#aiNow option").all_inner_texts()
        check(opts == ["Claude Haiku 5.5 (rapide, peu coûteux)", "Claude Sonnet 5.5 (plus précis)", "Modèle OVH (comme avant)"],
              f"choix pour « En ce moment » ({opts})")
        await pg.select_option("#aiNow", "claude-sonnet-5-5")
        await pg.fill("#aiKey", KEY)
        await pg.click("#aiMine .ai-save")
        await pg.wait_for_function("() => document.querySelector('#toast').innerText.includes('Claude répond')", timeout=15000)
        await pg.wait_for_function("() => document.querySelector('#aiMine .ai-check')?.innerText.includes('Sonnet')", timeout=10000)
        t = await pg.locator("#aiMine .ai-check").inner_text()
        check("✅ Claude Fable 5.2" in t and "✅ Claude Sonnet 5.5" in t and me.get("/api/me/ai").json()["now"] == "claude-sonnet-5-5",
              f"enregistrer une clé : essai fait aussitôt, résultat par modèle ({t!r})")
        check(not await pg.input_value("#aiKey"), "champ de la clé vidé après l'enregistrement")
        await pg.screenshot(path=str(OUT / "claude-settings.png"))
        ph = await page(390, 844, "#reglages")
        await ph.click("#aiBox summary")
        await ph.wait_for_selector("#aiMine .ai-save", timeout=10000)
        ov = await ph.evaluate("() => document.documentElement.scrollWidth <= innerWidth + 1")
        check(ov, "téléphone : pas de défilement horizontal")
        await ph.locator("#aiBox").screenshot(path=str(OUT / "claude-settings-phone.png"))
        me.post("/test/reset")
        await pg.click("#aiMine .ai-del")
        await pg.wait_for_function("() => document.querySelector('#aiMine')?.innerText.includes('Aucune clé')", timeout=10000)
        st = S()
        check(not me.get("/api/me/ai").json()["configured"] and "essai" not in json.loads(st["file"] or "{}")
              and sorted(c["w"] for c in st["calls"] if c["path"] == "/ai/remember" and c["body"]["ai"] is None) == ["wA", "wB"],
              "retirer la clé : effacée du contrôle et de la mémoire des assistants")
        check(not errs, f"aucune erreur JavaScript ({errs[:3]})")
        await br.close()
    print(f"\n{ok} ok, {fails} échec(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    serve() if sys.argv[1:] == ["serve"] else asyncio.run(run())
