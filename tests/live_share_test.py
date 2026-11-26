"""Réunion partagée en direct (spectateurs : regarder et écouter, micro au seul propriétaire) et transcription d'une
réunion précédente masquée au démarrage d'une nouvelle : API, écran distant (proxy VNC) et IHM.

Contrôle d'essai lancé à part, assistants simulés en mémoire (aucun appel à un vrai assistant), serveur VNC simulé
qui note ce qu'il reçoit, comptes fictifs.

    # sur un hôte qui a le venv du contrôle (aucun fichier d'environnement réel : tout est fictif)
    python tests/live_share_test.py serve                   # port 8099 (VNC simulé : 8096)
    # dans un conteneur qui a Chrome (réseau de l'hôte)
    BASE=http://127.0.0.1:8099 PW=… python tests/live_share_test.py
"""
import asyncio
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("TEST_DIR", "/tmp/wk"))
VNC_PORT = int(os.environ.get("TEST_VNC_PORT", "8096"))
REAL_VNC = int(os.environ.get("REAL_VNC_PORT", "0"))   # serveur : écran réel d'un assistant d'essai (websockify) pour wA


def iso(sec_ago: float) -> str:
    return (dt.datetime.now().astimezone() - dt.timedelta(seconds=sec_ago)).isoformat()


def serve():
    data = Path(os.environ.get("TEST_DATA_DIR", "/tmp/ctltest/data"))     # jamais le DATA_DIR du vrai contrôle
    data.mkdir(parents=True, exist_ok=True)
    (data / "voice_questions.json").write_text(json.dumps({"items": {}, "answers": {}}))
    (data / "voice_consents.json").write_text("[]")
    os.environ.update(DATA_DIR=str(data), ADMIN_LOGIN="essai", ADMIN_NAME="Compte Essai", COOKIE_SECURE="0",
                      SECRET_KEY=os.urandom(16).hex(), AGENT_TOKEN="essai-sans-secret",
                      WORKERS="wA=http://127.0.0.1:9,wB=http://127.0.0.1:9,wC=http://127.0.0.1:9")
    sys.path.insert(0, str(ROOT / "control"))
    os.chdir(ROOT / "control")
    import app as A
    import uvicorn
    import websockets

    def tr(file, active, started, v, n=3):
        return {"active": active, "enabled": True, "file": file, "started_at": started, "lines": n, "paragraphs": n,
                "error": None, "n_speakers": 2, "slides": 0,
                "analysis": {"v": v, "running": False, "at": iso(60) if v else None, "next_in": 200 if active else None,
                             "error": None, "count": v, "every": 300}}
    W = {
        "wA": {"state": "in_meeting", "owner": "essai", "last_owner": "essai", "viewers": [], "platform": "teams",
               "title": "Point d'équipe (essai)", "url": "https://teams.example/lien-secret?pwd=XYZ", "since": iso(600),
               "joined_at": iso(600), "recording": True, "recording_file": "20261118-090000_teams.mp4",
               "recording_since": iso(590), "listeners": 0, "vnc_port": VNC_PORT, "me_name": "Compte Essai",
               "transcript": tr("20261118-090000_teams", True, iso(590), 2)},
        "wB": {"state": "idle", "owner": None, "last_owner": "essai", "viewers": [], "platform": None, "title": None,
               "url": None, "since": iso(3600), "joined_at": iso(9000), "recording": False, "recording_file": None,
               "listeners": 0, "vnc_port": VNC_PORT, "transcript": tr("20261117-150000_web", False, iso(8990), 5)},
        "wC": {"state": "in_meeting", "owner": "chloe", "last_owner": "chloe", "viewers": [], "platform": "zoom",
               "title": "Réunion de Chloé", "url": "https://zoom.example/j/1", "since": iso(300), "joined_at": iso(300),
               "recording": True, "recording_file": "20261118-093000_zoom.mp4", "recording_since": iso(290),
               "listeners": 0, "vnc_port": VNC_PORT, "transcript": tr("20261118-093000_zoom", True, iso(290), 1)},
    }
    TEXT = {"20261118-090000_teams": ("Budget 2027 (marqueur-A)", "On valide le budget."),
            "20261117-150000_web": ("ANCIENNE-REUNION-marqueur", "Texte de la réunion d'hier."),
            "20261118-093000_zoom": ("Sujet de Chloé", "Bonjour à tous."),
            "20261118-100500_web": (None, "Nouvelle réunion : bonjour.")}
    calls = {"offers": [], "mute": [], "actions": [], "vnc": {}}
    # enregistrements simulés ; comme le vrai assistant, le partage en direct est gardé sur ceux de la réunion
    REC = {"wA": [{"name": "20261118-090000_teams.mp4", "kind": "video", "transcript": True, "report": True, "size": 1,
                   "mtime": time.time(), "duration": 600, "recording": True,
                   "meta": {"owner": "essai", "title": "Point d'équipe (essai)", "platform": "teams", "worker": "wA"}}]}

    async def worker_recordings(n, fresh=False):
        return [{**json.loads(json.dumps(x)), "worker": n} for x in REC.get(n, [])]

    async def worker_status(name):
        return {**json.loads(json.dumps(W[name])), "name": name, "online": True}

    class R:
        status_code = 200

        def __init__(self, d=None, content=b""):
            self.d, self.content = d or {}, content

        def json(self):
            return self.d

    async def wcall(name, method, path, **kw):
        w = W[name]
        f = w["transcript"]["file"]
        body = kw.get("json")
        if body is None and kw.get("content"):
            body = json.loads(kw["content"])
        if path.startswith("/transcript/analysis"):
            now, _ = TEXT.get(f, (None, ""))
            v = w["transcript"]["analysis"]["v"]
            return R({"v": v, "state": {"en_cours": now, "synthese": now} if now and v else None, "flashes": [],
                      "speakers": {}, "aliases": {}})
        if path.startswith("/transcript/slides"):
            return R({"file": f, "slides": []})
        if path.startswith("/transcript"):
            _, txt = TEXT.get(f, (None, ""))
            after = int(path.split("after=")[1]) if "after=" in path else 0
            pars = [{"id": 1, "t": 5.0, "spk": "S1", "text": txt}][after:]
            return R({"file": f, "total": 1, "paragraphs": pars, "pending": [], "slides": 0,
                      "speakers": {"S1": {"label": "Intervenant 1", "color": 1, "source": "voice"}}, "aliases": {}})
        if path == "/meeting/viewers":
            if w["state"] == "idle":
                return R({"detail": "aucune réunion"})
            old, new = set(w["viewers"]), set(body["viewers"]) - {w["owner"]}
            w["viewers"] = sorted(new)
            for x in REC.get(name, []):
                m = x["meta"]
                lv = set(m.get("live_viewers") or [])
                m["shared_with"] = sorted(((set(m.get("shared_with") or []) - ((old - new) & lv)) | new) - {m["owner"]})
                m["live_viewers"] = sorted((lv - (old - new)) | new)
            return R({"viewers": w["viewers"]})
        if path == "/webrtc/offer":
            calls["offers"].append({"worker": name, **body})
            return R({"sdp": "v=0", "type": "answer", "peer_id": "x"})
        if path == "/meeting/mute":
            calls["mute"].append(name)
            return R({"ok": True})
        if path.startswith("/screenshot"):
            return R(content=b"\xff\xd8\xff\xd9")
        calls["actions"].append([name, path])
        return R({"ok": True})

    A.worker_status, A.wcall, A.worker_recordings = worker_status, wcall, worker_recordings

    async def vnc_handler(ws, *a):                       # serveur VNC simulé : note les octets reçus
        key = str(len(calls["vnc"]))
        calls["vnc"][key] = ""
        try:
            async for m in ws:
                calls["vnc"][key] += (m if isinstance(m, bytes) else m.encode()).hex()
        except Exception:
            pass

    if REAL_VNC:
        W["wA"]["vnc_port"] = REAL_VNC
    else:
        @A.app.on_event("startup")
        async def _vnc():
            A.app.state.vnc = await websockets.serve(vnc_handler, "127.0.0.1", VNC_PORT, subprotocols=["binary"])

    @A.app.post("/test/{w}/{what}")
    async def test_set(w: str, what: str):
        x = W[w]
        if what == "join":            # nouvelle réunion sur un assistant qui vient de servir (transcription pas encore lancée)
            x.update(state="joining", owner="essai", viewers=[], joined_at=iso(0), since=iso(0), title="Nouvelle réunion",
                     platform="web", url="https://meet.example/x")
        elif what == "transcribe":    # la transcription de la nouvelle réunion démarre
            x.update(state="in_meeting", recording=True, recording_file="20261118-100500_web.mp4", recording_since=iso(0))
            x["transcript"] = tr("20261118-100500_web", True, iso(0), 0)
        elif what == "end":           # fin de réunion : le partage s'arrête (comme l'assistant)
            x.update(state="idle", owner=None, viewers=[], recording=False, recording_file=None)
            x["transcript"]["active"] = False
        return {"ok": True}

    @A.app.get("/test/state")
    async def test_state():
        return {"W": W, "calls": calls}
    uvicorn.run(A.app, host="127.0.0.1", port=int(os.environ.get("TEST_PORT", "8099")), log_level="warning")


ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what, flush=True)


async def login(pg, base, login_, pw, hash_="#home"):
    await pg.goto(base + "/" + hash_)
    await pg.fill("#loginId", login_)
    await pg.fill("#pw", pw)
    await pg.click("#loginForm button")
    await pg.wait_for_selector("#tab-home:not(.hidden), .worker.meet:not(.hidden)", timeout=15000)


async def real_vnc(me, pws):
    """Écran réel (x11vnc d'un assistant d'essai) vu par un spectateur, avec le vrai noVNC en lecture seule."""
    from playwright.async_api import async_playwright
    BASE = os.environ["BASE"]
    check(me.post("/api/workers/wA/viewers", json={"logins": ["bruno"]}).status_code == 200, "essai partage la réunion avec Bruno")
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        ctx = await b.new_context(viewport={"width": 1280, "height": 900}, locale="fr-FR")
        pg = await ctx.new_page()
        await login(pg, BASE, "bruno", pws["bruno"])
        await pg.goto(BASE + "/novnc/vnc.html?path=vnc/wA/websockify&autoconnect=1&resize=scale&view_only=1")
        ok_ = False
        for _ in range(30):
            await asyncio.sleep(1)
            st = await pg.evaluate("() => { const c = document.querySelector('#noVNC_container canvas, canvas'); return { w: c ? c.width : 0, status: (document.querySelector('#noVNC_status') || {}).textContent || '' }; }")
            if st["w"] > 100:
                ok_ = True
                break
        check(ok_, f"noVNC en lecture seule : écran réel reçu à travers le filtre ({st})")
        await asyncio.sleep(3)
        await pg.mouse.click(400, 300)
        await pg.keyboard.type("abc")
        await asyncio.sleep(2)
        st2 = await pg.evaluate("() => { const c = document.querySelector('canvas'); return { w: c ? c.width : 0, status: (document.querySelector('#noVNC_status') || {}).textContent || '' }; }")
        check(st2["w"] > 100 and "connect" not in st2["status"].lower().replace("connecté", ""), f"toujours connecté après clic et frappe ({st2})")
        await pg.screenshot(path=str(OUT / "live-real-vnc.png"))
        await b.close()
    print(f"\n{ok} ok, {fails} échec(s)")
    sys.exit(1 if fails else 0)


async def main():
    import httpx
    from playwright.async_api import async_playwright
    BASE, PW = os.environ["BASE"], os.environ["PW"]
    me = httpx.Client(base_url=BASE, timeout=60)
    check(me.post("/api/login", json={"login": "essai", "password": PW}).status_code == 200, "connexion (essai)")
    pws = {}
    for login_, name in (("bruno", "Bruno Fictif"), ("chloe", "Chloé Fictive")):
        pws[login_] = me.post("/api/users", json={"login": login_, "name": name, "role": "user"}).json().get("password")
    bru, chl = httpx.Client(base_url=BASE, timeout=60), httpx.Client(base_url=BASE, timeout=60)
    for c, login_ in ((bru, "bruno"), (chl, "chloe")):      # mot de passe provisoire changé (sinon fenêtre imposée)
        c.post("/api/login", json={"login": login_, "password": pws[login_]})
        new = pws[login_] + "-Nouveau9"
        check(c.post("/api/me/password", json={"current": pws[login_], "new": new}).status_code == 200, f"{login_} : mot de passe changé")
        pws[login_] = new
    S = lambda: me.get("/test/state").json()
    ws_of = lambda c: {x["name"]: x for x in c.get("/api/workers").json()}
    if os.environ.get("REAL_VNC") == "1":
        return await real_vnc(me, pws)

    # --- avant le partage : rien pour Bruno
    check(ws_of(bru)["wA"]["state"] == "taken", "avant le partage, Bruno voit l'assistant occupé")
    for path in ("transcript", "analysis", "slides", "screenshot"):
        check(bru.get(f"/api/workers/wA/{path}").status_code == 403, f"avant le partage : {path} refusé à Bruno")
    check(bru.post("/api/workers/wA/viewers", json={"logins": ["bruno"]}).status_code == 403, "Bruno ne peut pas se partager la réunion")
    check(me.post("/api/workers/wC/viewers", json={"logins": ["bruno"]}).status_code == 403,
          "un administrateur ne partage pas la réunion d'un autre compte")
    check(me.post("/api/workers/wA/viewers", json={"logins": ["bruno", "inconnu"]}).status_code == 400, "compte inconnu refusé")
    r = me.post("/api/workers/wA/viewers", json={"logins": ["bruno", "essai", "Bruno "]})
    check(r.status_code == 200 and r.json()["viewers"] == ["bruno"], f"essai partage la réunion avec Bruno ({r.json()})")

    # --- spectateur : regarder et écouter
    st = ws_of(bru)["wA"]
    check(st.get("viewer") and st["state"] == "in_meeting" and st["title"] == "Point d'équipe (essai)"
          and st["owner_name"] == "Compte Essai" and st.get("talk") is False, "Bruno voit la réunion en spectateur")
    check("url" not in st and "recording_file" not in st and "viewers" not in st and "vnc_port" not in st,
          "ni le lien de la réunion, ni ses fichiers, ni la liste des spectateurs")
    for path in ("transcript?after=0", "analysis", "slides", "screenshot"):
        check(bru.get(f"/api/workers/wA/{path}").status_code == 200, f"spectateur : {path.split('?')[0]} autorisé")
    for act in ("mute", "leave", "rec-stop", "tr-stop", "speakers", "analyze", "open", "rec-start", "tr-start"):
        check(bru.post(f"/api/workers/wA/{act}", json={"url": "https://x.example"}).status_code == 403, f"spectateur : « {act} » refusé")
    check(not S()["calls"]["mute"] and not S()["calls"]["actions"], "aucune action transmise à l'assistant")
    r = bru.post("/api/workers/wA/offer", json={"sdp": "v=0 bruno", "type": "offer", "listen_only": False})
    o = S()["calls"]["offers"][-1]
    check(r.status_code == 200 and o["listen_only"] is True and o["sdp"] == "v=0 bruno", "spectateur : écoute seule imposée par le contrôle")
    me.post("/api/workers/wA/offer", json={"sdp": "v=0 essai", "type": "offer"})
    check(S()["calls"]["offers"][-1]["listen_only"] is False, "propriétaire : son micro passe")
    check(me.post("/api/workers/wA/mute").status_code == 200 and S()["calls"]["mute"] == ["wA"], "propriétaire : bouton muet autorisé")
    me.post("/api/workers/wC/offer", json={"sdp": "v=0 admin", "type": "offer"})
    check(S()["calls"]["offers"][-1]["listen_only"] is True and ws_of(me)["wC"]["talk"] is False,
          "administrateur sur la réunion d'un autre compte : écoute seule (micro au propriétaire)")
    check(me.post("/api/workers/wC/mute").status_code == 403, "administrateur : bouton muet refusé sur la réunion d'un autre")
    check(ws_of(chl)["wA"]["state"] == "taken" and chl.get("/api/workers/wA/transcript").status_code == 403,
          "un compte non invité ne voit toujours rien")

    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        errs = []

        async def page(login_, pw, vw=1280, vh=900, tag="pc", hash_="#home"):
            ctx = await b.new_context(viewport={"width": vw, "height": vh}, locale="fr-FR", timezone_id="Europe/Paris",
                                      is_mobile=tag == "tel", has_touch=tag == "tel")
            pg = await ctx.new_page()
            pg.on("pageerror", lambda e: errs.append(str(e)))
            await pg.add_init_script("""window.__gum = 0; if (navigator.mediaDevices) navigator.mediaDevices.getUserMedia = async () => { window.__gum++; throw new Error('test'); };""")
            await login(pg, BASE, login_, pw, hash_)
            return pg

        # --- écran distant : clavier et souris d'un spectateur jetés, ceux du propriétaire transmis
        HS = "524642203030332e3030380a" + "01" + "00"            # version, sécurité aucune, ClientInit
        FBUR, KEY, PTR = "03000000000007800438", "0401000000000061", "050100640064"
        send = """async ([hexs]) => { const ws = new WebSocket(`ws://${location.host}/vnc/wA/websockify`, ['binary']);
          ws.binaryType = 'arraybuffer';
          await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej('erreur'); ws.onclose = (e) => rej('fermé ' + e.code); });
          for (const h of hexs) { ws.send(new Uint8Array(h.match(/../g).map(x => parseInt(x, 16)))); await new Promise(r => setTimeout(r, 80)); }
          await new Promise(r => setTimeout(r, 400)); ws.close(); return 'ok'; }"""
        pv = await page("bruno", pws["bruno"])
        n0 = len(S()["calls"]["vnc"])
        res = await pv.evaluate(send, [[HS, FBUR, KEY, PTR, FBUR]])
        got = S()["calls"]["vnc"].get(str(n0), "")
        check(res == "ok" and got == "524642203030332e3030380a" + "01" + "01" + FBUR + FBUR,
              f"écran du spectateur : seules les demandes d'images arrivent au serveur VNC ({got[:60]}…)")
        po = await page("essai", PW)
        n0 = len(S()["calls"]["vnc"])
        await po.evaluate(send, [[HS, FBUR, KEY, PTR]])
        got = S()["calls"]["vnc"].get(str(n0), "")
        check(got == HS + FBUR + KEY + PTR, "écran du propriétaire : clavier et souris transmis")
        pc = await page("chloe", pws["chloe"])
        res = await pc.evaluate("""async () => { const ws = new WebSocket(`ws://${location.host}/vnc/wA/websockify`, ['binary']);
          return await new Promise(res => { ws.onopen = () => res('ouvert'); ws.onclose = (e) => res('fermé ' + e.code); }); }""")
        check(res.startswith("fermé"), f"écran refusé à un compte non invité ({res})")
        await pc.context.close()

        # --- IHM du spectateur
        await pv.wait_for_selector("#liveGrid .tile:not(.hidden)")
        tm = await pv.locator("#liveGrid .tile:not(.hidden)").first.inner_text()
        check("partagée par Compte Essai" in tm and "Point d'équipe" in tm, f"accueil de Bruno : la réunion partagée est listée ({tm[:80]!r})")
        await pv.goto(BASE + "/#m/wA")
        card = pv.locator(".worker.meet:not(.hidden)")
        await card.wait_for()
        await pv.wait_for_function("() => document.querySelector('.worker.meet:not(.hidden) .an-body')?.innerText.includes('marqueur-A')", timeout=10000)
        v = await pv.evaluate("""() => { const c = document.querySelector('.worker.meet:not(.hidden)'), vis = (s) => { const e = c.querySelector(s); return !!e && e.offsetParent !== null; };
          return { viewer: c.classList.contains('viewer'), actions: vis('.actions'), audio: vis('.b-audio'), follow: vis('.b-follow'),
                   toggle: vis('.tr-toggle'), now: vis('.an-now'), note: c.querySelector('.wnote').innerText, info: c.querySelector('.winfo').innerText }; }""")
        check(v["viewer"] and not v["actions"] and not v["toggle"] and not v["now"] and v["audio"] and v["follow"],
              f"carte du spectateur : écouter et suivre en grand, aucune commande ({v})")
        check("spectateur" in v["note"] and "Compte Essai" in v["note"] and "partagée par Compte Essai" in v["info"],
              "il sait qu'il est spectateur et qui partage")
        n_off = len(S()["calls"]["offers"])
        await card.locator(".b-audio").click()
        await asyncio.sleep(2)
        gum = await pv.evaluate("() => window.__gum")
        offs = S()["calls"]["offers"][n_off:]
        check(gum == 0 and offs and offs[-1]["listen_only"] is True, f"écouter : le micro n'est jamais demandé au spectateur ({gum}, {len(offs)} offre)")
        await card.locator(".vt[data-v=tr]").click()
        chip = card.locator(".tr-spk .chip").first
        await chip.wait_for(timeout=8000)
        check("renommer" not in (await chip.get_attribute("title") or ""), "voix : pas de renommage proposé au spectateur")
        await pv.screenshot(path=str(OUT / "live-viewer.png"))
        await pv.goto(BASE + "/#suivre/wA")
        await pv.wait_for_selector("#tab-focus.viewer", timeout=10000)
        f = await pv.evaluate("() => ['#fHand', '#fRecBtn', '#fLeave', '#fCc'].map(s => document.querySelector(s).offsetParent !== null)")
        check(f == [False, False, False, True], f"suivre en grand : ni « Prendre la main », ni enregistrer, ni quitter ({f})")
        ph = await page("bruno", pws["bruno"], 390, 844, "tel", "#m/wA")
        await ph.wait_for_selector(".worker.meet.viewer:not(.hidden)")
        await asyncio.sleep(1)
        await ph.screenshot(path=str(OUT / "live-viewer-phone.png"), full_page=True)
        await ph.context.close()

        # --- IHM du propriétaire : bouton, fenêtre de partage
        await po.goto(BASE + "/#m/wA")
        card = po.locator(".worker.meet:not(.hidden)")
        await card.wait_for()
        bl = card.locator(".b-live")
        await bl.wait_for(timeout=8000)
        check(await bl.is_visible(), "propriétaire : bouton « Partager en direct »")
        info = await card.locator(".winfo").inner_text()
        check("partagée avec" in info, f"propriétaire : il voit avec qui la réunion est partagée ({info!r})")
        await bl.click()
        await po.wait_for_selector("#dlgLive[open]")
        rows = await po.locator("#lvList label").all_inner_texts()
        checked = await po.evaluate("() => [...document.querySelectorAll('#lvList input:checked')].map(x => x.value)")
        check(len(rows) == 2 and not any("(essai)" in r for r in rows) and checked == ["bruno"],
              f"fenêtre : les autres comptes, Bruno déjà coché ({rows}, {checked})")
        await po.screenshot(path=str(OUT / "live-share-dialog.png"))
        await po.locator("#lvList input[value=bruno]").uncheck()
        await po.locator("#lvList input[value=chloe]").check()
        await po.click("#dlgLive button[value=ok]")
        await asyncio.sleep(1.5)
        check(S()["W"]["wA"]["viewers"] == ["chloe"], f"partage changé : Chloé à la place de Bruno ({S()['W']['wA']['viewers']})")
        await asyncio.sleep(3.5)
        check(ws_of(bru)["wA"]["state"] == "taken" and bru.get("/api/workers/wA/transcript").status_code == 403,
              "Bruno retiré : il ne voit plus rien")
        check(ws_of(chl)["wA"].get("viewer"), "Chloé est spectatrice")

        # --- fin de réunion : le direct s'arrête, l'enregistrement et le compte rendu restent partagés
        recs = lambda c: [x["name"] for x in c.get("/api/recordings").json() if x["worker"] == "wA"]
        pcv = await page("chloe", pws["chloe"], hash_="#m/wA")
        await pcv.wait_for_selector(".worker.meet.viewer:not(.hidden)", timeout=10000)
        await asyncio.sleep(2)
        me.post("/test/wA/end")
        check(chl.get("/api/workers/wA/transcript").status_code == 403 and not ws_of(chl)["wA"].get("viewer"),
              "fin de réunion : la spectatrice ne suit plus le direct")
        check(recs(chl) == ["20261118-090000_teams.mp4"], f"fin de réunion : l'enregistrement reste partagé avec Chloé ({recs(chl)})")
        check(recs(bru) == [], "Bruno, retiré du direct pendant la réunion, n'a pas l'enregistrement")
        mine = next(x for x in me.get("/api/recordings").json() if x["worker"] == "wA")
        check(mine["shared_with"] == ["chloe"] and mine["mine"], f"propriétaire : partage visible sur l'enregistrement ({mine['shared_with']})")
        await pcv.wait_for_function("() => document.querySelector('.worker.meet:not(.hidden) .ended:not(.hidden) .ended-txt')?.innerText.includes('vous l')", timeout=15000)
        e = await pcv.evaluate("""() => { const c = document.querySelector('.worker.meet:not(.hidden)'), b = c.querySelector('.ended .b-final');
          return { txt: c.querySelector('.ended-txt').innerText, btn: b.offsetParent !== null ? b.innerText : null }; }""")
        check("Compte Essai vous l'a partagée" in e["txt"] and "Enregistrements" in e["txt"] and e["btn"] == "Voir le compte rendu final",
              f"écran de fin de la spectatrice : réunion partagée, bouton vers le compte rendu ({e})")
        await pcv.screenshot(path=str(OUT / "live-viewer-ended.png"))
        await pcv.locator(".worker.meet:not(.hidden) .ended .b-final").click()
        await pcv.wait_for_function("() => location.hash.startsWith('#enr/wA/20261118-090000_teams')", timeout=10000)
        check(True, "le bouton ouvre la page de l'enregistrement partagé")
        await pcv.context.close()

        # --- assistant qui vient de servir : la transcription précédente n'est pas montrée au départ d'une réunion
        await po.goto(BASE + "/#m/wB")
        card = po.locator(".worker.meet:not(.hidden)")
        await card.wait_for()
        await po.wait_for_function("() => document.querySelector('.worker.meet:not(.hidden) .an-body')?.textContent.includes('ANCIENNE')", timeout=10000)
        check(True, "assistant libre : le compte rendu de sa dernière réunion est encore chargé")
        me.post("/test/wB/join")
        await asyncio.sleep(4)
        txt = await po.evaluate("() => { const c = document.querySelector('.worker.meet:not(.hidden)'); return [c.querySelector('.an-body').textContent, c.querySelector('.tr-body').textContent, c.querySelector('.badge').textContent]; }")
        check("ANCIENNE" not in txt[0] and "hier" not in txt[1], f"nouvelle réunion qui démarre : rien de la précédente ({txt[2]} ; {txt[0][:70]!r})")
        await po.goto(BASE + "/#home")
        await po.wait_for_selector("#liveGrid .tile:not(.hidden)")
        await asyncio.sleep(2)
        tiles = await po.locator("#liveGrid .tile:not(.hidden)").all_inner_texts()
        check(not any("ANCIENNE" in t for t in tiles), "accueil : la vignette ne montre pas « En ce moment » de l'ancienne réunion")
        me.post("/test/wB/transcribe")
        await po.goto(BASE + "/#m/wB")
        await asyncio.sleep(5)
        txt = await po.evaluate("() => { const c = document.querySelector('.worker.meet:not(.hidden)'); return [c.querySelector('.an-body').innerText, c.querySelector('.tr-body').innerText]; }")
        check("ANCIENNE" not in txt[0] and "apparaîtra" in txt[0], f"transcription de la nouvelle réunion : compte rendu en attente ({txt[0][:80]!r})")
        await po.locator(".worker.meet:not(.hidden) .vt[data-v=tr]").click()
        await po.wait_for_function("() => document.querySelector('.worker.meet:not(.hidden) .tr-body')?.innerText.includes('Nouvelle réunion')", timeout=10000)
        check(True, "la transcription de la nouvelle réunion s'affiche")
        check(not errs, f"aucune erreur JavaScript ({errs[:3]})")
        await b.close()
    print(f"\n{ok} ok, {fails} échec(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        serve()
    else:
        asyncio.run(main())
