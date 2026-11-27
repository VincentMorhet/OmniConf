"""Lien public d'une vidéo (27/11) : toute personne qui a le lien /v/<jeton> regarde la VIDÉO seule, sans compte.

Contrôle d'essai (données fictives, port 8099) avec un faux assistant (port 8098) qui sert une vraie petite vidéo ;
navigateur et appels HTTP depuis le conteneur d'essai. Rien n'est envoyé aux vrais assistants.

    TEST_DATA_DIR=/tmp/pubtest/data TEST_PORT=8099 ADMIN_PASSWORD=… SAMPLE=/tmp/pubtest/sample.mp4 \
        python tests/public_link_test.py serve                                   # contrôle + faux assistant
    PW=… BASE=http://127.0.0.1:8099 SAMPLE=… python tests/public_link_test.py    # vérifications
"""
import asyncio
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("TEST_DIR", "/tmp/wk"))
WPORT = int(os.environ.get("WORKER_PORT", "8098"))

R_OPEN = "20261120-100000_teams.mp4"       # à essai : celle qu'on partage
R_LEA = "20261121-140000_teams.mp4"        # à lea
R_SHARED = "20261122-090000_teams.mp4"     # à essai, partagée avec lea (lecture seule)
R_LIVE = "20261126-220000_teams.mp4"       # à essai, enregistrement en cours
R_TXT = "20261101-080000_imp.transcript.json"   # transcription seule (pas de vidéo)
R_DEL = "20261123-110000_teams.mp4"        # à essai : supprimée pendant l'essai


def serve():
    data = Path(os.environ.get("TEST_DATA_DIR", "/tmp/pubtest/data"))     # jamais le DATA_DIR du vrai contrôle
    data.mkdir(parents=True, exist_ok=True)
    (data / "voice_questions.json").write_text(json.dumps({"items": {}, "answers": {}}))
    (data / "voice_consents.json").write_text("[]")
    os.environ.update(DATA_DIR=str(data), ADMIN_LOGIN="essai", ADMIN_NAME="Compte Essai", COOKIE_SECURE="0",
                      SECRET_KEY=os.urandom(16).hex(), AGENT_TOKEN="essai-sans-secret", WORKERS=f"wA=http://127.0.0.1:{WPORT}")
    sys.path.insert(0, str(ROOT / "control"))
    os.chdir(ROOT / "control")
    import app as A
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, Response

    sample = Path(os.environ["SAMPLE"])
    if not A.USERS.get("lea"):
        A.USERS.create("lea", "Léa Essai")
    A.USERS.set_password("lea", os.environ["ADMIN_PASSWORD"], must_change=False)

    def item(name, owner, title, started, **kw):
        return {"name": name, "kind": "video", "duration": 6.0, "recording": False, "size": sample.stat().st_size,
                "mtime": time.time(), "transcript": False, "report": False, "cr_title": None, "final_cr": False,
                "report_job": None, "slides_job": None, "import_job": None,
                "meta": {"owner": owner, "title": title, "started_at": started, "platform": "teams", "shared_with": [], **kw.pop("meta", {})},
                **kw}
    recs = [item(R_OPEN, "essai", "Réunion d'essai publique", "2026-11-20T10:00:00+01:00"),
            item(R_LEA, "lea", "Réunion de Léa", "2026-11-21T14:00:00+01:00"),
            item(R_SHARED, "essai", "Réunion partagée avec Léa", "2026-11-22T09:00:00+01:00", meta={"shared_with": ["lea"]}),
            item(R_LIVE, "essai", "Réunion en cours", "2026-11-26T22:00:00+01:00", recording=True),
            item(R_TXT, "essai", "Transcription seule", "2026-11-01T08:00:00+01:00", kind="transcript"),
            item(R_DEL, "essai", "Réunion à supprimer", "2026-11-23T11:00:00+01:00")]
    W = FastAPI()

    def check_token(request: Request):
        if request.headers.get("x-agent-token") != "essai-sans-secret":
            raise HTTPException(401)

    @W.get("/recordings")
    async def w_list(request: Request):
        check_token(request)
        return recs

    @W.get("/recordings/{name}")
    async def w_file(name: str, request: Request):
        check_token(request)
        if not any(r["name"] == name and r["kind"] == "video" for r in recs):
            raise HTTPException(404)
        return FileResponse(sample, media_type="video/mp4", filename=name)

    @W.get("/recordings/{name}/thumb")
    async def w_thumb(name: str, request: Request):
        check_token(request)
        return Response(b"\xff\xd8\xff\xd9", media_type="image/jpeg")

    @W.delete("/recordings/{name}")
    async def w_del(name: str, request: Request):
        check_token(request)
        recs[:] = [r for r in recs if r["name"] != name]
        return {"ok": True}

    @W.api_route("/{rest:path}", methods=["GET", "POST", "PATCH", "PUT"])
    async def w_other(rest: str):
        return [] if rest in ("imports", "voices") else {}

    async def worker_status(name):
        return {"name": name, "online": True, "state": "idle", "owner": None, "viewers": [], "recording": False,
                "listeners": 0, "vnc_port": 8096, "transcript": {"active": False, "enabled": True}}
    A.worker_status = worker_status

    @A.app.get("/test/links")
    async def test_links():
        return A._pub_all()

    @A.app.post("/test/expire")
    async def test_expire():                  # tous les liens : échéance dépassée d'une minute
        d = A._pub_all()
        for v in d.values():
            v["expires"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).isoformat(timespec="seconds")
        A._pub_write(d)
        return {"ok": True}

    async def main():
        s1 = uvicorn.Server(uvicorn.Config(A.app, host="127.0.0.1", port=int(os.environ.get("TEST_PORT", "8099")), log_level="warning"))
        s2 = uvicorn.Server(uvicorn.Config(W, host="127.0.0.1", port=WPORT, log_level="warning"))
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
    sample = Path(os.environ["SAMPLE"]).read_bytes()

    def client(login=None):
        c = httpx.Client(base_url=BASE, timeout=30)
        if login:
            r = c.post("/api/login", json={"login": login, "password": PW})
            assert r.status_code == 200, r.text
        return c
    adm, lea, anon = client("essai"), client("lea"), client()
    rurl = lambda f: f"/api/recordings/wA/{f}"

    print("— API")
    check(adm.get(rurl(R_OPEN) + "/public").json() == {"link": None}, "pas de lien au départ")
    check(adm.post(rurl(R_OPEN) + "/public", json={"days": 3}).status_code == 400, "durée hors liste (3 j) : refusée")
    r = adm.post(rurl(R_OPEN) + "/public", json={"days": 7})
    link = r.json().get("link") or {}
    tok = (link.get("path") or "").removeprefix("/v/")
    check(r.status_code == 200 and re.fullmatch(r"[A-Za-z0-9_-]{24,}", tok or "") is not None, f"lien créé : /v/<jeton de {len(tok)} caractères>")
    exp = dt.datetime.fromisoformat(link["expires"]) if link.get("expires") else None
    check(exp is not None and abs((exp - dt.datetime.now(dt.timezone.utc)).total_seconds() - 7 * 86400) < 120, "échéance : dans 7 jours")

    p = anon.get(f"/v/{tok}")
    check(p.status_code == 200 and "text/html" in p.headers.get("content-type", "") and "noindex" in p.headers.get("x-robots-tag", "")
          and p.headers.get("referrer-policy") == "no-referrer", "page publique sans compte (noindex, no-referrer)")
    info = anon.get(f"/api/public/{tok}").json()
    check(info.get("title") == "Réunion d'essai publique" and info.get("duration") == 6.0 and set(info) == {"title", "started_at", "duration", "expires"},
          f"infos publiques : titre, date, durée, échéance — rien d'autre ({sorted(info)})")
    v = anon.get(f"/api/public/{tok}/video", headers={"Range": "bytes=0-99"})
    check(v.status_code == 206 and v.content == sample[:100] and v.headers.get("content-range", "").startswith("bytes 0-99/"),
          "vidéo par morceaux (Range, lecture sur iPhone)")
    v = anon.get(f"/api/public/{tok}/video")
    check(v.status_code == 200 and v.content == sample, f"vidéo entière ({len(v.content)} octets, identique)")
    check(anon.get(f"/api/public/{tok}/thumb").status_code == 200, "vignette")
    check(anon.get(rurl(R_OPEN)).status_code == 401 and anon.get(rurl(R_OPEN) + "/transcript").status_code == 401
          and anon.get("/api/recordings").status_code == 401, "sans compte : les adresses habituelles restent fermées")
    check(anon.get(f"/api/public/{tok}/transcript").status_code == 404 and anon.get(f"/api/public/{tok}/report").status_code == 404,
          "par le lien : ni transcription, ni compte rendu")
    st = [anon.get(f"/api/public/{bad}").status_code for bad in ("x" * 24, tok[:-1] + ("A" if tok[-1] != "A" else "B"), "court")]
    check(st == [404, 404, 404] and anon.get(f"/api/public/{tok[:-1] + ('A' if tok[-1] != 'A' else 'B')}/video").status_code == 404,
          "jeton faux ou incomplet : 404")
    lst = {x["name"]: x for x in adm.get("/api/recordings").json()}
    check(lst[R_OPEN]["public_link"] is True and not lst[R_SHARED]["public_link"], "liste : la réunion porte « lien public »")
    vw = adm.get(rurl(R_OPEN) + "/public").json()["link"]
    check(vw["views"] == 1 and vw["active"], f"le propriétaire voit le nombre d'ouvertures ({vw['views']})")

    r2 = adm.post(rurl(R_OPEN) + "/public", json={"days": None}).json()["link"]
    tok2 = r2["path"].removeprefix("/v/")
    check(tok2 != tok and anon.get(f"/api/public/{tok}").status_code == 404 and anon.get(f"/api/public/{tok2}").status_code == 200
          and r2["expires"] is None, "« Nouveau lien » sans limite : l'ancien cesse aussitôt")
    check(len(adm.get("/test/links").json()) == 1, "un seul lien par vidéo")

    check(lea.post(rurl(R_OPEN) + "/public", json={"days": 7}).status_code == 403
          and lea.get(rurl(R_OPEN) + "/public").status_code == 403, "un autre compte ne peut ni voir ni créer le lien")
    check(lea.post(rurl(R_SHARED) + "/public", json={"days": 7}).status_code == 403, "réunion partagée en lecture seule : pas de lien public")
    check(adm.post(rurl(R_LIVE) + "/public", json={"days": 7}).status_code == 409, "enregistrement en cours : refusé")
    check(adm.post(rurl(R_TXT) + "/public", json={"days": 7}).status_code == 400, "transcription seule (pas de vidéo) : refusé")
    rl = lea.post(rurl(R_LEA) + "/public", json={"days": 30})
    tokl = (rl.json().get("link") or {}).get("path", "").removeprefix("/v/")
    check(rl.status_code == 200 and anon.get(f"/api/public/{tokl}").status_code == 200, "un utilisateur partage sa propre vidéo")
    adm.patch("/api/users/lea", json={"disabled": True})
    check(anon.get(f"/api/public/{tokl}").status_code == 404, "compte qui a créé le lien désactivé : le lien s'arrête")
    adm.patch("/api/users/lea", json={"disabled": False})
    check(anon.get(f"/api/public/{tokl}").status_code == 200, "compte réactivé : le lien remarche")

    rd = adm.post(rurl(R_DEL) + "/public", json={"days": 7}).json()["link"]["path"].removeprefix("/v/")
    adm.delete(rurl(R_DEL))
    check(anon.get(f"/api/public/{rd}").status_code == 404 and not any(v["file"] == R_DEL for v in adm.get("/test/links").json().values()),
          "réunion supprimée : son lien est effacé")
    adm.post("/test/expire")
    check(anon.get(f"/api/public/{tok2}").status_code == 404 and anon.get(f"/api/public/{tok2}/video").status_code == 404,
          "lien échu : 404")
    vw = adm.get(rurl(R_OPEN) + "/public").json()["link"]
    check(vw and vw["active"] is False, "le propriétaire voit qu'il est échu")
    check(adm.delete(rurl(R_OPEN) + "/public").status_code == 200 and adm.get(rurl(R_OPEN) + "/public").json() == {"link": None}
          and adm.delete(rurl(R_LEA) + "/public").status_code == 200, "« Retirer le lien »")
    check(adm.get("/test/links").json() == {}, "plus aucun lien enregistré")

    print("— IHM")
    async with async_playwright() as pw:
        br = await pw.chromium.launch(channel="chrome", headless=True)

        async def page(w, h, login=True):
            ctx = await br.new_context(viewport={"width": w, "height": h}, timezone_id="Europe/Paris")
            await ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=BASE)
            pg = await ctx.new_page()
            if login:
                await pg.goto(BASE + "/#rec")
                await pg.fill("#loginId", "essai")
                await pg.fill("#pw", PW)
                await pg.click("#loginForm button")
                await pg.wait_for_selector("#tab-rec:not(.hidden)", timeout=15000)
            return pg

        def card(pg, title):
            return pg.locator("#recordings .ritem", has_text=title).first

        pg = await page(1280, 900)
        c = card(pg, "Réunion d'essai publique")
        await c.wait_for(timeout=10000)
        await c.locator(".more").click()
        check(await c.locator(".rmenu button.pub").is_visible(), "menu de la vidéo : « Lien public… »")
        live = card(pg, "Réunion en cours")
        check(await live.locator("button.pub").count() == 0 and await card(pg, "Transcription seule").locator("button.pub").count() == 0,
              "pas de « Lien public… » sur un enregistrement en cours ni sur une transcription seule")
        await c.locator(".rmenu button.pub").click()
        await pg.wait_for_selector("#dlgPublic[open]")
        check(await pg.locator("#pbHas").is_hidden() and await pg.locator("#pbCreate").inner_text() == "Créer le lien"
              and "sans compte" in await pg.locator("#dlgPublic").inner_text(), "fenêtre : avertissement, « Créer le lien »")
        await pg.select_option("#pbDays", "7")
        await pg.click("#pbCreate")
        await pg.wait_for_function("() => document.querySelector('#pbUrl').value.includes('/v/')", timeout=8000)
        url = await pg.input_value("#pbUrl")
        info = await pg.locator("#pbInfo").inner_text()
        check(url.startswith(BASE + "/v/") and "valable jusqu'au" in info and "pas encore ouvert" in info, f"lien affiché ({info!r})")
        await pg.click("#pbCopy")
        clip = await pg.evaluate("() => navigator.clipboard.readText()")
        check(clip == url, "« Copier » met le lien dans le presse-papiers")
        await pg.screenshot(path=str(OUT / "pub-dialog.png"))
        await pg.locator("#dlgPublic button[value=cancel]").click()
        await pg.wait_for_function("() => [...document.querySelectorAll('#recordings .ritem')].some(e => e.innerText.includes('Lien public'))", timeout=8000)
        check(True, "carte : pastille « 🌐 Lien public »")

        pub = await page(1280, 900, login=False)
        await pub.goto(url)
        await pub.wait_for_selector("#ok:not(.hidden)", timeout=10000)
        await pub.wait_for_function("() => document.querySelector('video').readyState >= 2", timeout=15000)
        st = await pub.evaluate("() => ({ t: document.querySelector('h1').innerText, d: document.querySelector('video').duration, sub: document.querySelector('#sub').innerText })")
        check(st["t"] == "Réunion d'essai publique" and 5 < st["d"] < 7 and "2026" in st["sub"], f"page publique sans compte : la vidéo se charge ({st})")
        await pub.evaluate("() => document.querySelector('video').play()")
        await pub.wait_for_function("() => document.querySelector('video').currentTime > 0.5", timeout=10000)
        check(True, "la vidéo se lit")
        check(await pub.locator("#loginForm").count() == 0 and await pub.evaluate("() => !document.cookie"), "aucune page de connexion, aucun cookie")
        await pub.screenshot(path=str(OUT / "pub-desktop.png"))

        ph = await page(390, 844, login=False)
        await ph.goto(url)
        await ph.wait_for_function("() => document.querySelector('video').readyState >= 1", timeout=15000)
        ov = await ph.evaluate("() => document.documentElement.scrollWidth <= innerWidth + 1 && document.querySelector('video').getBoundingClientRect().right <= innerWidth + 1")
        check(ov, "téléphone : la vidéo tient dans l'écran")
        await ph.screenshot(path=str(OUT / "pub-phone.png"), full_page=True)

        # retirer le lien depuis l'IHM : la page publique ne montre plus la vidéo
        await c.locator(".more").click()
        await c.locator(".rmenu button.pub").click()
        await pg.wait_for_selector("#dlgPublic[open]")
        await pg.wait_for_function("() => !document.querySelector('#pbHas').classList.contains('hidden')", timeout=8000)
        info = await pg.locator("#pbInfo").inner_text()
        check("ouvert 2 fois" in info, f"la fenêtre compte les ouvertures ({info!r})")
        await pg.click("#pbDel")
        await pg.wait_for_selector("#dlgConfirm[open]")
        await pg.click("#cfYes")
        await pg.wait_for_function("() => document.querySelector('#pbHas').classList.contains('hidden')", timeout=8000)
        await pg.locator("#dlgPublic button[value=cancel]").click()
        gone = await page(1280, 900, login=False)
        await gone.goto(url)
        await gone.wait_for_selector("#gone:not(.hidden)", timeout=10000)
        check(await gone.locator("#ok").is_hidden(), "lien retiré : « Cette vidéo n'est pas disponible »")
        await gone.screenshot(path=str(OUT / "pub-gone.png"))
        await br.close()
    print(f"\n{ok} ok, {fails} échec(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    serve() if sys.argv[1:] == ["serve"] else asyncio.run(run())
