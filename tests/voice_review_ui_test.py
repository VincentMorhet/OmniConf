"""IHM : écoute et validation des voix avant le calcul d'une empreinte vocale (Réglages > Empreintes vocales).

Contrôle d'essai lancé à part, avec des données fictives (« Personne Essai », vecteurs aléatoires, aucune
empreinte réelle) et sans diffusion aux assistants ; les extraits sont lus sur un vrai assistant :

    # sur l'hôte des assistants de test (venv du contrôle, fichier d'environnement du contrôle de test)
    WORKER=w3 STEM=<enregistrement de w3 sans .mp4> python tests/voice_review_ui_test.py serve   # port 8099
    # dans un conteneur qui a Chrome (réseau de l'hôte)
    BASE=http://127.0.0.1:8099 PW=… python tests/voice_review_ui_test.py
"""
import asyncio
import json
import os
import random
import sys
from pathlib import Path

NAME = "Personne Essai"


def serve():
    """Contrôle d'essai : DATA_DIR neuf, une personne cochée, deux voix candidates fictives, aucune diffusion."""
    data = Path(os.environ.get("TEST_DATA_DIR", "/tmp/ctltest/data"))      # jamais le DATA_DIR du vrai contrôle
    os.environ["DATA_DIR"] = str(data)
    data.mkdir(parents=True, exist_ok=True)
    w, stem = os.environ["WORKER"], os.environ["STEM"]
    (data / "voice_consents.json").write_text(json.dumps([{"id": "essai1", "name": NAME, "consent": True,
                                                           "at": "2026-11-14T09:00:00+01:00", "by": "essai", "note": None, "history": []}]))
    rnd = random.Random(7)

    def group(kind, label, n, sec, t0):
        return {"worker": w, "meeting": stem, "title": "Réunion d'essai", "kind": kind, "label": label,
                "sum": [rnd.uniform(-1, 1) for _ in range(192)], "n": n, "sec": sec, "dropped": 1,
                "clips": [{"t": t0, "te": t0 + 6, "text": "Premier extrait d'essai.", "sim": 0.9},
                          {"t": t0 + 30, "te": t0 + 36, "text": "Deuxième extrait d'essai.", "sim": 0.88},
                          {"t": t0 + 60, "te": t0 + 63, "text": "Extrait le moins ressemblant.", "sim": 0.55, "odd": True}],
                "found_at": "2026-11-14T09:00:00+01:00"}
    (data / "voiceprints.json").write_text(json.dumps({NAME: {
        "groups": {f"{w}/{stem}|moi": group("moi", "Moi", 40, 180.0, 20.0),
                   f"{w}/{stem}|vignette": group("vignette", NAME, 12, 50.0, 200.0)}, "review": {}}}))
    os.environ.update(ADMIN_LOGIN="essai", ADMIN_NAME="Compte Essai", COOKIE_SECURE="0",
                      SECRET_KEY=os.urandom(16).hex())
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "control"))
    os.chdir(Path(__file__).resolve().parent.parent / "control")
    import app as A
    import uvicorn

    async def no_push(names=None):            # rien n'est envoyé aux assistants
        return {w: "essai : non diffusé"}
    A.voiceprints_push = no_push
    uvicorn.run(A.app, host="127.0.0.1", port=int(os.environ.get("TEST_PORT", "8099")), log_level="warning")


ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what)


async def main():
    import httpx
    from playwright.async_api import async_playwright
    BASE, PW, ADMIN = os.environ["BASE"], os.environ["PW"], os.environ.get("ADMIN", "essai")
    api = httpx.Client(base_url=BASE, verify=False, timeout=30)
    check(api.post("/api/login", json={"login": ADMIN, "password": PW}).status_code == 200, "connexion")
    vp = api.get("/api/voiceprints").json()["people"][NAME]
    check(vp["voices"] == 2 and vp["pending"] == 2 and not vp.get("sec"), f"2 voix à écouter, pas d'empreinte ({vp})")
    check(api.get("/api/voiceprints/voices", params={"name": "Inconnu Total"}).status_code == 404, "personne non cochée : refusée")
    voices = api.get("/api/voiceprints/voices", params={"name": NAME}).json()["voices"]
    check(all("sum" not in v for v in voices), "aucun vecteur envoyé à l'IHM")
    r = api.get("/api/voiceprints/clip", params={"name": NAME, "id": voices[0]["id"], "i": 0})
    check(r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and len(r.content) > 5000,
          f"extrait MP3 lu sur l'assistant ({r.status_code}, {len(r.content)} octets)")
    check(api.get("/api/voiceprints/clip", params={"name": NAME, "id": voices[0]["id"], "i": 9}).status_code == 404,
          "extrait inexistant : refusé")
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        for w, h in ((1280, 900), (390, 844)):
            pg = await (await b.new_context(viewport={"width": w, "height": h})).new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            await pg.goto(BASE + "/#reglages")
            await pg.fill("#loginId", ADMIN); await pg.fill("#pw", PW); await pg.click("#loginForm button")
            await pg.wait_for_selector("#tab-set:not(.hidden)")
            await pg.click("#voiceBox summary")
            row = pg.locator("#voiceList .consent", has_text=NAME)
            await row.wait_for()
            txt = await row.inner_text()
            check(("2" if w == 1280 else "1") + " voix à écouter" in txt, f"[{w}] voix à écouter annoncées")
            await row.locator(".vp-voices summary").click()
            await row.locator(".vv").first.wait_for()
            check(await row.locator(".vv").count() == 2 and await row.locator(".vv .clip").count() == 6, f"[{w}] 2 voix, 6 extraits")
            check(await row.locator(".vv .clip.odd").count() == 2, f"[{w}] passage le moins ressemblant signalé")
            if w == 1280:
                await row.locator(".vv .clip").first.click()
                await pg.wait_for_function("() => { const b = document.querySelector('.vv .clip'); return b && !b.classList.contains('loading') && b.classList.contains('playing'); }", timeout=20000)
                check(True, "extrait en lecture (bouton en pause)")
                await pg.wait_for_function("() => !document.querySelector('.vv .clip.playing')", timeout=20000)
                check(True, "fin de l'extrait : bouton remis en lecture")
                await row.locator(".vv").first.locator(".yes").click()
                await pg.wait_for_selector("#voiceList .vv.ok")
                await pg.wait_for_function("() => document.querySelector('#voiceList .vp-voices')?.open")
                check(True, "voix validée (encadré resté ouvert)")
                vp = api.get("/api/voiceprints").json()["people"][NAME]
                check(vp["validated"] == 1 and vp["sec"] == 180.0 and vp["n"] == 40, f"empreinte faite de la seule voix validée ({vp['sec']} s)")
                txt = await row.inner_text()
                check("empreinte vocale active" in txt and "1 voix à écouter" in txt, "ligne de la personne mise à jour")
                await row.locator(".vv.todo .nope").click()
                await pg.wait_for_selector("#voiceList .vv.no")
                vp = api.get("/api/voiceprints").json()["people"][NAME]
                check(vp["rejected"] == 1 and vp["sec"] == 180.0, "voix refusée : pas dans l'empreinte")
                await row.screenshot(path="voice-review.png")
                await row.locator(".vv.ok .yes").click()          # second clic : remise à écouter
                await pg.wait_for_function("() => document.querySelectorAll('#voiceList .vv.todo').length === 1")
                vp = api.get("/api/voiceprints").json()["people"][NAME]
                check(vp["validated"] == 0 and not vp.get("sec"), "voix remise à écouter : plus d'empreinte")
            else:
                over = await pg.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
                check(over <= 1, f"[{w}] pas de défilement horizontal ({over} px)")
                await row.screenshot(path="voice-review-phone.png")
            check(not errs, f"[{w}] aucune erreur JavaScript {errs[:2]}")
        await b.close()
    print(f"{ok} vérifications réussies, {fails} échec(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    serve() if sys.argv[1:] == ["serve"] else asyncio.run(main())
