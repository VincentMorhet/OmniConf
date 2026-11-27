"""Image de la caméra de l'assistant (27/11) : chaque compte choisit une image (Réglages), l'IHM la prépare en JPEG
1280×720 (image entière, centrée sur un fond flouté), le contrôle la garde (mode 600) et l'envoie à l'assistant avec
chaque réunion de ce compte. L'assistant ne l'allume jamais (essai à part, dans une vraie réunion Jitsi).

Contrôle d'essai (données fictives, port 8099) et faux assistant (port 8098) qui note ce qu'il reçoit ; navigateur
depuis le conteneur d'essai. Rien n'est envoyé aux vrais assistants.

    TEST_DATA_DIR=/tmp/camtest/data TEST_PORT=8099 ADMIN_PASSWORD=… python tests/camera_ui_test.py serve
    PW=… BASE=http://127.0.0.1:8099 python tests/camera_ui_test.py
"""
import asyncio
import base64
import json
import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("TEST_DIR", "/tmp/wk"))
WPORT = int(os.environ.get("WORKER_PORT", "8098"))


def serve():
    data = Path(os.environ.get("TEST_DATA_DIR", "/tmp/camtest/data"))     # jamais le DATA_DIR du vrai contrôle
    data.mkdir(parents=True, exist_ok=True)
    (data / "voice_questions.json").write_text(json.dumps({"items": {}, "answers": {}}))
    (data / "voice_consents.json").write_text("[]")
    os.environ.update(DATA_DIR=str(data), ADMIN_LOGIN="essai", ADMIN_NAME="Compte Essai", COOKIE_SECURE="0",
                      SECRET_KEY=os.urandom(16).hex(), AGENT_TOKEN="essai-sans-secret",
                      WORKERS=f"wA=http://127.0.0.1:{WPORT}", DEFAULT_NAMES=json.dumps({"teams": "Assistant d'essai"}))
    sys.path.insert(0, str(ROOT / "control"))
    os.chdir(ROOT / "control")
    import app as A
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request

    for login, name in (("lea", "Léa Essai"), ("marc", "Marc Essai")):
        if not A.USERS.get(login):
            A.USERS.create(login, name)
        A.USERS.set_password(login, os.environ["ADMIN_PASSWORD"], must_change=False)
    joins = []
    F = FastAPI()

    @F.get("/status")
    async def w_status(request: Request):
        if request.headers.get("x-agent-token") != "essai-sans-secret":
            raise HTTPException(401)
        return {"name": "wA", "online": True, "state": "idle", "owner": None, "viewers": [], "recording": False,
                "listeners": 0, "vnc_port": 8096, "transcript": {"active": False, "enabled": True}}

    @F.post("/meeting/join")
    async def w_join(request: Request):
        b = await request.json()
        joins.append({"owner": b.get("owner"), "camera_image": b.get("camera_image"), "display_name": b.get("display_name")})
        return {"state": "joining", "name": "wA"}

    @F.api_route("/{rest:path}", methods=["GET", "POST", "PATCH", "PUT"])
    async def w_other(rest: str):
        return [] if rest in ("recordings", "imports", "voices") else {}

    @A.app.get("/test/joins")
    async def test_joins():
        return joins

    @A.app.get("/test/camfile/{login}")
    async def test_camfile(login: str):
        f = A.cam_file(login)
        return {"exists": f.is_file(), "mode": oct(f.stat().st_mode & 0o777) if f.is_file() else None,
                "dir_mode": oct(A.CAM_DIR.stat().st_mode & 0o777) if A.CAM_DIR.is_dir() else None,
                "b64": base64.b64encode(f.read_bytes()).decode() if f.is_file() else None}

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


def jpeg_size(b: bytes):
    """(largeur, hauteur) d'un JPEG, lues dans son en-tête SOF."""
    i = 2
    while i < len(b) - 9:
        if b[i] != 0xFF:
            return None
        m, ln = b[i + 1], struct.unpack(">H", b[i + 2:i + 4])[0]
        if m in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", b[i + 5:i + 9])
            return w, h
        i += 2 + ln
    return None


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
    adm, lea, anon = client("essai"), client("lea"), client()
    start = lambda c: c.post("/api/meetings", json={"url": "https://teams.microsoft.com/l/meetup-join/essai", "worker": "wA"})  # noqa: E731

    print("API")
    check(anon.get("/api/me/camera").status_code == 401 and anon.put("/api/me/camera", content=b"\xff\xd8\xff").status_code == 401,
          "sans compte : 401")
    check(adm.get("/api/me/camera").status_code == 404, "aucune image au départ : 404")
    r = adm.put("/api/me/camera", content=b"\x89PNG\r\n\x1a\n" + b"0" * 100, headers={"Content-Type": "image/jpeg"})
    check(r.status_code == 400, f"autre chose qu'un JPEG : refusé ({r.status_code})")
    r = adm.put("/api/me/camera", content=b"\xff\xd8\xff" + b"0" * 2_000_001, headers={"Content-Type": "image/jpeg"})
    check(r.status_code == 413, f"plus de 2 Mo : refusé ({r.status_code})")
    start(adm)
    j = adm.get("/test/joins").json()[-1]
    check(j["owner"] == "essai" and j["camera_image"] is None, "réunion sans image choisie : rien envoyé (carte avec le nom)")

    async with async_playwright() as pw:
        br = await pw.chromium.launch(channel="chrome", headless=True)

        async def page(w, h, who="essai"):
            ctx = await br.new_context(viewport={"width": w, "height": h}, timezone_id="Europe/Paris")
            pg = await ctx.new_page()
            pg.on("pageerror", lambda e: print("  [erreur JS]", e))
            await pg.goto(BASE + "/#set")
            await pg.fill("#loginId", who)
            await pg.fill("#pw", PW)
            await pg.click("#loginForm button")
            await pg.wait_for_selector("#tab-set:not(.hidden)", timeout=15000)
            return pg

        # image d'essai : portrait rouge (600×900), produite par le navigateur
        pg = await page(1280, 900)
        png = await pg.evaluate("""() => { const c = document.createElement('canvas'); c.width = 600; c.height = 900;
            const g = c.getContext('2d'); g.fillStyle = '#e01010'; g.fillRect(0, 0, 600, 900);
            g.fillStyle = '#fff'; g.font = 'bold 90px sans-serif'; g.fillText('ESSAI', 150, 470);
            return c.toDataURL('image/png').split(',')[1]; }""")
        src = OUT / "cam-src.png"
        src.write_bytes(base64.b64decode(png))

        print("Interface")
        await pg.click("#camBox summary")
        await pg.wait_for_selector("#camMine .cam-card", timeout=8000)
        txt = await pg.inner_text("#camMine")
        check("Assistant d'essai" in txt and "Choisir une image" in txt, f"sans image : carte avec le nom de l'assistant ({txt[:80]!r})")
        await pg.screenshot(path=str(OUT / "cam-none.png"))
        await pg.set_input_files("#camFile", str(src))
        await pg.wait_for_function("() => document.querySelector('#toast').innerText.includes('Image enregistrée')", timeout=10000)
        await pg.wait_for_selector("#camMine .cam-prev img", timeout=8000)
        await pg.wait_for_function("() => document.querySelector('#camMine .cam-prev img').naturalWidth > 0", timeout=8000)
        f = adm.get("/test/camfile/essai").json()
        b = base64.b64decode(f["b64"])
        check(jpeg_size(b) == (1280, 720), f"JPEG 1280×720 ({jpeg_size(b)}, {len(b) // 1024} Ko)")
        check(f["mode"] == "0o600" and f["dir_mode"] == "0o700", f"fichier 600, dossier 700 ({f['mode']}, {f['dir_mode']})")
        px = await pg.evaluate("""() => { const i = document.querySelector('#camMine .cam-prev img');
            const c = document.createElement('canvas'); c.width = 1280; c.height = 720; const g = c.getContext('2d');
            g.drawImage(i, 0, 0, 1280, 720);
            const at = (x, y) => Array.from(g.getImageData(x, y, 1, 1).data.slice(0, 3));
            return { centre: at(640, 200), bord: at(40, 360), w: i.naturalWidth, h: i.naturalHeight }; }""")
        c, e = px["centre"], px["bord"]
        check(c[0] > 180 and c[1] < 80 and c[2] < 80, f"image entière au centre (rouge {c})")
        check(max(e) < 140 and e[0] >= e[1], f"bords : fond flouté et assombri ({e})")
        check("Changer d'image" in await pg.inner_text("#camMine") and await pg.locator(".cam-del").is_visible(),
              "boutons « Changer d'image… » et « Retirer l'image »")
        await pg.screenshot(path=str(OUT / "cam-set.png"))
        me = adm.get("/api/me").json()["user"]
        check(me.get("camera_image") is True, "le compte sait qu'il a une image")
        r = adm.get("/api/me/camera")
        check(r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and "no-store" in r.headers.get("cache-control", ""),
              "lecture de sa propre image (sans cache)")
        start(adm)
        j = adm.get("/test/joins").json()[-1]
        check(j["camera_image"] == f["b64"], "réunion : l'image du compte est envoyée à l'assistant")
        check(lea.get("/api/me/camera").status_code == 404, "un autre compte ne voit pas cette image (404 : la sienne seulement)")
        start(lea)
        j = adm.get("/test/joins").json()[-1]
        check(j["owner"] == "lea" and j["camera_image"] is None, "réunion d'un autre compte : pas l'image d'essai")
        # retirer (avec confirmation)
        await pg.click(".cam-del")
        await pg.wait_for_selector("#dlgConfirm[open]")
        await pg.click("#cfYes")
        await pg.wait_for_selector("#camMine .cam-card", timeout=8000)
        check(not adm.get("/test/camfile/essai").json()["exists"], "« Retirer l'image » : fichier supprimé")
        start(adm)
        check(adm.get("/test/joins").json()[-1]["camera_image"] is None, "après retrait : plus d'image envoyée")
        # compte supprimé : son image aussi
        lea.put("/api/me/camera", content=b, headers={"Content-Type": "image/jpeg"})
        check(adm.get("/test/camfile/lea").json()["exists"], "image de lea enregistrée")
        r = adm.delete("/api/users/lea")
        check(r.status_code == 200 and not adm.get("/test/camfile/lea").json()["exists"], "compte supprimé : son image aussi")
        # téléphone
        ph = await page(390, 844)
        await ph.click("#camBox summary")
        await ph.wait_for_selector("#camMine .cam-card", timeout=8000)
        await ph.set_input_files("#camFile", str(src))
        await ph.wait_for_selector("#camMine .cam-prev img", timeout=10000)
        bx = await ph.locator(".cam-prev").bounding_box()
        check(bx["x"] >= 0 and bx["x"] + bx["width"] <= 390, "téléphone : l'aperçu tient dans l'écran")
        check(await ph.evaluate("() => document.documentElement.scrollWidth <= 390"), "téléphone : rien ne dépasse à droite")
        await ph.locator("#camBox").scroll_into_view_if_needed()
        await ph.screenshot(path=str(OUT / "cam-phone.png"))
        await br.close()
    print(f"\n{ok} vérifications réussies, {fails} échecs")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        serve()
    else:
        asyncio.run(run())
