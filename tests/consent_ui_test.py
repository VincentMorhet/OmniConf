"""IHM : consentements aux empreintes vocales (ajout, attestation obligatoire, retrait, suppression).

    BASE=https://<ihm de test> PW=<mot de passe admin> python tests/consent_ui_test.py
"""
import asyncio
import os
import sys

import httpx
from playwright.async_api import async_playwright

BASE, PW, ADMIN = os.environ["BASE"], os.environ["PW"], os.environ.get("ADMIN", "admin")
NAME = "Personne Essai"
ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what)


async def main():
    api = httpx.Client(base_url=BASE, verify=False)
    api.post("/api/login", json={"login": ADMIN, "password": PW})
    for x in api.get("/api/voice-consents").json()["items"]:
        if x["name"] == NAME:
            api.put(f"/api/voice-consents/{x['id']}", json={"consent": False}); api.delete(f"/api/voice-consents/{x['id']}")
    check(api.post("/api/voice-consents", json={"name": NAME}).status_code == 200, "ajout par l'API")
    cid = next(x["id"] for x in api.get("/api/voice-consents").json()["items"] if x["name"] == NAME)
    check(api.put(f"/api/voice-consents/{cid}", json={"consent": True}).status_code == 400, "consentement refusé sans l'attestation")
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True, args=["--ignore-certificate-errors"])
        for w, h in ((1280, 900), (390, 844)):
            pg = await (await b.new_context(viewport={"width": w, "height": h}, ignore_https_errors=True)).new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
            await pg.goto(BASE + "/#rec")
            await pg.fill("#loginId", ADMIN); await pg.fill("#pw", PW); await pg.click("#loginForm button")
            await pg.wait_for_selector("#tab-rec:not(.hidden)")
            check(await pg.is_visible("#voiceBox"), f"[{w}] encadré visible pour l'administrateur")
            await pg.click("#voiceBox summary")
            row = pg.locator("#voiceList .consent", has_text=NAME)
            await row.wait_for()
            if w == 1280:
                await row.locator("input[type=checkbox]").click()   # ne se coche qu'après l'attestation
                await pg.wait_for_selector("#dlgConsent[open]")
                check(await pg.is_disabled("#csOk"), "« Valider » grisé tant que l'attestation n'est pas cochée")
                await pg.locator("#dlgConsent").screenshot(path="consent-dialog.png")
                await pg.check("#csAttest"); await pg.fill("#csNote", "courriel du 14/11")
                await pg.click("#csOk")
                await pg.wait_for_selector(f"#voiceList .consent:has-text('{NAME}') :text('consentement attesté')")
                check(True, "consentement attesté (date et compte affichés)")
                await pg.locator("#voiceBox").screenshot(path="consent-box.png")
                await row.locator("input[type=checkbox]").click()
                await pg.wait_for_selector(f"#voiceList .consent:has-text('{NAME}') :text('retiré le')")
                check(True, "consentement retiré")
            sw = await pg.evaluate("document.documentElement.scrollWidth")
            check(sw <= w, f"[{w}] pas de défilement horizontal ({sw})")
            check(not errs, f"[{w}] aucune erreur JavaScript {errs[:2]}")
        await b.close()
    x = next(x for x in api.get("/api/voice-consents").json()["items"] if x["name"] == NAME)
    check([h["action"] for h in x["history"]] == ["ajout", "consentement attesté", "consentement retiré : empreinte à effacer"],
          "historique complet (ajout, attestation, retrait)")
    check(api.delete(f"/api/voice-consents/{cid}").status_code == 200, "ligne supprimée")
    print(f"\n{ok} vérifications réussies, {fails} échecs")
    sys.exit(1 if fails else 0)


asyncio.run(main())
