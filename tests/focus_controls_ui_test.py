"""Lancement d'une réunion : vue en grand directement ; Enregistrer / Quitter avec confirmation.

    BASE=https://<ihm de test> PW=<mot de passe admin> W=w4 python tests/focus_controls_ui_test.py
Rejoint une salle Jitsi vide (meet.ffmuc.net) avec l'assistant W, puis le fait quitter.
"""
import asyncio
import os
import sys
import time

from playwright.async_api import async_playwright

BASE, PW, W, ADMIN = os.environ["BASE"], os.environ["PW"], os.environ.get("W", "w4"), os.environ.get("ADMIN", "admin")
ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what, flush=True)


async def status(pg):
    return await pg.evaluate(f"fetch('/api/workers').then(r => r.json()).then(l => l.find(x => x.name === '{W}'))")


async def wait_state(pg, cond, secs=90):
    for _ in range(secs):
        st = await status(pg)
        if cond(st):
            return st
        await pg.wait_for_timeout(1000)
    return await status(pg)


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True, args=["--ignore-certificate-errors"])
        ctx = await b.new_context(viewport={"width": 1280, "height": 860}, ignore_https_errors=True)
        pg = await ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        await pg.goto(BASE + "/")
        await pg.fill("#loginId", ADMIN); await pg.fill("#pw", PW); await pg.click("#loginForm button")
        await pg.wait_for_selector("#app:not(.hidden)")
        st = await status(pg)
        check(st["state"] == "idle", f"{W} libre au départ ({st['state']})")
        # lancement depuis le formulaire de l'accueil
        await pg.fill("#f-url", f"https://meet.ffmuc.net/OmniconfGrand{int(time.time())}")
        await pg.click("#opts summary")
        await pg.select_option("#f-worker", W)
        await pg.fill("#f-title", "Test vue en grand")
        await pg.click("#btnStart")
        await pg.wait_for_function(f"location.hash === '#suivre/{W}'", timeout=15000)
        check(await pg.is_visible("#tab-focus"), "après le lancement : vue en grand ouverte directement")
        check(await pg.is_visible("#fRecBtn") and await pg.is_visible("#fLeave"), "boutons Enregistrer et Quitter visibles")
        st = await wait_state(pg, lambda s: s["state"] in ("in_meeting", "error") and s.get("recording"))
        check(st["state"] == "in_meeting" and st["recording"], f"en réunion, enregistrement en cours ({st['state']})")
        await pg.wait_for_function("document.querySelector('#fRecBtn').textContent.includes('Arrêter')", timeout=10000)
        # arrêt de l'enregistrement : annuler puis confirmer
        await pg.click("#fRecBtn")
        await pg.wait_for_selector("#dlgConfirm[open]")
        check("Arrêter l'enregistrement" in await pg.text_content("#cfTitle"), "confirmation avant d'arrêter l'enregistrement")
        await pg.locator("#dlgConfirm").screenshot(path="confirm-rec.png")
        await pg.keyboard.press("Enter")   # le bouton actif est « Annuler »
        await pg.wait_for_selector("#dlgConfirm", state="hidden")
        await pg.wait_for_timeout(1500)
        check((await status(pg))["recording"], "Annuler (ou Entrée) : l'enregistrement continue")
        await pg.click("#fRecBtn"); await pg.wait_for_selector("#dlgConfirm[open]"); await pg.click("#cfYes")
        st = await wait_state(pg, lambda s: not s.get("recording"), 20)
        check(not st["recording"] and st["state"] == "in_meeting", "confirmé : enregistrement arrêté, l'assistant reste")
        await pg.wait_for_function("document.querySelector('#fRecBtn').textContent.includes('Enregistrer')", timeout=10000)
        # redémarrage : sans confirmation
        await pg.click("#fRecBtn")
        await pg.wait_for_timeout(800)
        check(not await pg.is_visible("#dlgConfirm"), "démarrer l'enregistrement : pas de confirmation")
        st = await wait_state(pg, lambda s: s.get("recording"), 20)
        check(st["recording"], "enregistrement relancé")
        # départ : annuler puis confirmer
        await pg.click("#fLeave"); await pg.wait_for_selector("#dlgConfirm[open]")
        check("Quitter" in await pg.text_content("#cfTitle"), "confirmation avant de quitter")
        await pg.locator("#dlgConfirm").screenshot(path="confirm-leave.png")
        await pg.screenshot(path="focus-bar.png")
        await pg.click("#cfNo"); await pg.wait_for_timeout(1500)
        check((await status(pg))["state"] == "in_meeting", "Annuler : l'assistant reste en réunion")
        await pg.click("#fLeave"); await pg.wait_for_selector("#dlgConfirm[open]"); await pg.click("#cfYes")
        st = await wait_state(pg, lambda s: s["state"] == "idle", 40)
        check(st["state"] == "idle", "confirmé : l'assistant a quitté la réunion")
        # téléphone : barre de la vue en grand sans débordement
        await pg.set_viewport_size({"width": 390, "height": 844}); await pg.wait_for_timeout(500)
        sw = await pg.evaluate("document.documentElement.scrollWidth")
        check(sw <= 390, f"téléphone : pas de défilement horizontal ({sw})")
        await pg.screenshot(path="focus-phone.png")
        check(not errs, f"aucune erreur JavaScript {errs[:2]}")
        # nettoyage : enregistrements du test supprimés
        for r in await pg.evaluate("fetch('/api/recordings').then(r => r.json())"):
            if (r.get("meta") or {}).get("title") == "Test vue en grand":
                await pg.evaluate(f"fetch('/api/recordings/{r['worker']}/{r['name']}', {{method: 'DELETE'}})")
        await b.close()
    print(f"\n{ok} vérifications réussies, {fails} échecs")
    sys.exit(1 if fails else 0)


asyncio.run(main())
