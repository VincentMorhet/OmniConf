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

NAME, OTHER = "Personne Essai", "Personne Témoin"


def serve():
    """Contrôle d'essai : DATA_DIR neuf, une personne cochée, deux voix candidates fictives, aucune diffusion."""
    data = Path(os.environ.get("TEST_DATA_DIR", "/tmp/ctltest/data"))      # jamais le DATA_DIR du vrai contrôle
    os.environ["DATA_DIR"] = str(data)
    data.mkdir(parents=True, exist_ok=True)
    w, stem = os.environ["WORKER"], os.environ["STEM"]
    (data / "voice_consents.json").write_text(json.dumps([
        {"id": f"essai{i}", "name": n, "consent": True, "at": "2026-11-14T09:00:00+01:00", "by": "essai", "note": None, "history": []}
        for i, n in enumerate((NAME, OTHER))]))
    rnd = random.Random(7)

    def vec():
        v = [rnd.uniform(-1, 1) for _ in range(192)]
        k = sum(x * x for x in v) ** 0.5
        return [x / k for x in v]

    def clip(t, te, text, sim, **kw):
        return {"t": t, "te": te, "text": text, "sim": sim, "d": te - t, "v": vec(), **kw}

    def group(kind, label, n, sec, t0):
        return {"worker": w, "meeting": stem, "title": "Réunion d'essai", "kind": kind, "label": label,
                "sum": [x * n for x in vec()], "n": n, "sec": sec, "dropped": 1,
                "clips": [clip(t0, t0 + 6, "Premier extrait d'essai.", 0.9), clip(t0 + 30, t0 + 36, "Deuxième extrait d'essai.", 0.88),
                          clip(t0 + 60, t0 + 63, "Extrait le moins ressemblant.", 0.55, odd=True)],
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
    A.vp_schedule = lambda names, by, delay=60: None     # pas de nouvelle recherche sur les assistants
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
    who = lambda n=NAME: api.get("/api/voiceprints").json()["people"][n]
    vp = who()
    check(vp["voices"] == 2 and vp["pending"] == 2 and not vp.get("sec"), f"2 voix à écouter, pas d'empreinte ({vp})")
    check(api.get("/api/voiceprints/voices", params={"name": "Inconnu Total"}).status_code == 404, "personne non cochée : refusée")
    voices = api.get("/api/voiceprints/voices", params={"name": NAME}).json()["voices"]
    check(all("sum" not in v and all("v" not in c for c in v["clips"]) for v in voices), "aucun vecteur envoyé à l'IHM")
    t0 = voices[0]["clips"][0]["t"]
    r = api.get("/api/voiceprints/clip", params={"name": NAME, "id": voices[0]["id"], "t": t0})
    check(r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and len(r.content) > 5000,
          f"extrait MP3 lu sur l'assistant ({r.status_code}, {len(r.content)} octets)")
    check(api.get("/api/voiceprints/clip", params={"name": NAME, "id": voices[0]["id"], "t": 9999}).status_code == 404,
          "extrait inexistant : refusé")
    check(api.post("/api/voiceprints/attribute", json={"name": NAME, "id": voices[0]["id"], "to": "Inconnu Total"}).status_code == 400,
          "attribution à une personne non cochée : refusée")
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        for w, h in ((1280, 900), (390, 844)):
            pg = await (await b.new_context(viewport={"width": w, "height": h})).new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
            await pg.goto(BASE + "/#reglages")
            await pg.fill("#loginId", ADMIN); await pg.fill("#pw", PW); await pg.click("#loginForm button")
            await pg.wait_for_selector("#tab-set:not(.hidden)")
            await pg.click("#voiceBox summary")
            row = pg.locator(f"#voiceList .consent:has(label b:text-is('{NAME}'))")
            await row.wait_for()
            if w == 1280:
                check("2 voix à écouter" in await row.inner_text(), f"[{w}] voix à écouter annoncées")
                await row.locator(".vp-voices summary").click()
                await row.locator(".vv").first.wait_for()
                check(await row.locator(".vv").count() == 2 and await row.locator(".vv .clip").count() == 6, f"[{w}] 2 voix, 6 extraits")
                check(await row.locator(".vv .clip.odd").count() == 2 and await row.locator(".vv .cx").count() == 6,
                      f"[{w}] passage le moins ressemblant signalé, un ✕ par extrait")
                await row.locator(".vv .clip").first.click()
                await pg.wait_for_function("() => { const b = document.querySelector('.vv .clip'); return b && !b.classList.contains('loading') && b.classList.contains('playing'); }", timeout=20000)
                check(True, "extrait en lecture (bouton en pause)")
                await pg.wait_for_function("() => !document.querySelector('.vv .clip.playing')", timeout=20000)
                check(True, "fin de l'extrait : bouton remis en lecture")
                moi = row.locator(".vv", has_text="canal « Moi »")
                await moi.locator(".yes").click()
                await pg.wait_for_selector("#voiceList .vv.ok")
                await pg.wait_for_function("() => document.querySelector('#voiceList .vp-voices')?.open")
                vp = who()
                check(vp["validated"] == 1 and vp["sec"] == 180.0 and vp["n"] == 40, f"voix validée, encadré resté ouvert ({vp['sec']} s)")
                # un seul extrait refusé, puis rétabli
                await moi.locator(".clip-row").first.locator(".cx").click()
                await pg.wait_for_selector("#voiceList .vv.ok .clip-row.refused")
                vp = who()
                check(vp["n"] == 39 and vp["sec"] == 174.0 and vp["validated"] == 1, f"extrait refusé seul : retiré de l'empreinte ({vp['n']} passages, {vp['sec']} s)")
                await moi.locator(".clip-row.refused .cx-undo").click()
                await pg.wait_for_function("() => !document.querySelector('#voiceList .vv.ok .clip-row.refused')")
                check(who()["n"] == 40, "extrait rétabli")
                # extrait attribué à une autre personne cochée
                await moi.locator(".clip-row").first.locator(".cx").click()
                await pg.wait_for_selector("#voiceList .vv.ok .clip-row.refused .cx-to")
                await moi.locator(".clip-row.refused .cx-to").select_option(OTHER)
                await pg.wait_for_selector(f"#voiceList .vv.ok .clip-row.refused :text('attribué à')")
                vp, ot = who(), who(OTHER)
                check(vp["n"] == 39 and ot["validated"] == 1 and ot["n"] == 1 and ot["sec"] == 6.0,
                      f"extrait attribué : retiré de l'un ({vp['n']}), ajouté à l'autre ({ot['n']} passage, {ot['sec']} s)")
                # voix entière attribuée
                vig = row.locator(".vv", has_text="d'après sa vignette")
                await vig.locator(".vv-to").select_option(OTHER)
                await pg.wait_for_selector("#voiceList .vv.no :text('attribuée à')")
                vp, ot = who(), who(OTHER)
                check(vp["rejected"] == 1 and vp["sec"] == 174.0 and ot["validated"] == 2 and ot["n"] == 13 and ot["sec"] == 56.0,
                      f"voix attribuée : refusée pour l'un, validée pour l'autre ({ot['n']} passages, {ot['sec']} s)")
                await row.screenshot(path="voice-review.png")
                orow = pg.locator(f"#voiceList .consent:has(label b:text-is('{OTHER}'))")
                await orow.locator(".vp-voices summary").click()
                await orow.locator(".vv.manual").first.wait_for()
                check(await orow.locator(".vv.manual").count() == 2, "les voix attribuées apparaissent chez l'autre personne")
                await orow.screenshot(path="voice-review-other.png")
                await orow.locator(".vv.manual").first.locator(".nope").click()
                await pg.wait_for_function(f"() => document.querySelectorAll('#voiceList .consent .vv.manual').length === 1")
                check(who(OTHER)["validated"] == 1, "voix attribuée retirée")
                await moi.locator(".yes").click()                 # second clic : remise à écouter
                await pg.wait_for_function("() => document.querySelectorAll('#voiceList .vv.todo').length === 1")
                vp = who()
                check(vp["validated"] == 0 and not vp.get("sec"), "voix remise à écouter : plus d'empreinte")
            else:
                await row.locator(".vp-voices summary").click()
                await row.locator(".vv").first.wait_for()
                check(await row.locator(".vv").count() == 2, f"[{w}] voix affichées")
                over = await pg.evaluate("""() => { const W = document.documentElement.clientWidth;
                  return [...document.querySelectorAll('#voiceList *')].filter(e => e.getBoundingClientRect().right > W + 1).length; }""")
                check(over == 0, f"[{w}] rien ne dépasse à droite ({over})")
                await row.screenshot(path="voice-review-phone.png")
            check(not errs, f"[{w}] aucune erreur JavaScript {errs[:2]}")
        await b.close()
    print(f"{ok} vérifications réussies, {fails} échec(s)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    serve() if sys.argv[1:] == ["serve"] else asyncio.run(main())
