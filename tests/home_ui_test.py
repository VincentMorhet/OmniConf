"""Test de la nouvelle IHM (accueil + vue d'une réunion) avec un vrai Chrome :
accueil vide -> lancement depuis l'accueil -> vue de la réunion (écran, audio, panneau compte rendu)
-> retour à l'accueil (vignette « en direct ») -> prise en main (noVNC intégré) -> affichage iPhone
-> départ (écran « réunion terminée ») -> « Dernières réunions » sur l'accueil.
Usage : BASE=https://192.168.0.233 PW=… WA=w3 python home_ui_test.py
(la page tests/loopback.html doit être copiée dans le conteneur WA : /tmp/loopback.html)"""
import asyncio, json, os
from playwright.async_api import async_playwright

BASE = os.environ.get("BASE", "https://192.168.0.233"); PW = os.environ["PW"]
WA = os.environ.get("WA", "w3"); OUT = os.environ.get("OUT", "/tmp")


async def main():
    out = {}
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True, args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
        ctx = await b.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900})
        page = await ctx.new_page(); errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await page.goto(BASE)
        await page.screenshot(path=f"{OUT}/ui-0-login.png")
        await page.fill("#pw", PW); await page.click("#loginForm button")
        await page.wait_for_selector("#tab-home:not(.hidden) .wchip")
        await page.wait_for_function("document.querySelector('#today').textContent.includes('assistant')")
        out["accueil"] = {"titre": await page.inner_text("#hello"), "sous_titre": await page.inner_text("#today"),
                          "assistants": await page.locator(".wchip").all_inner_texts(),
                          "aucune_reunion_affiche": await page.locator("#liveEmpty").is_visible()}
        await page.screenshot(path=f"{OUT}/ui-1-accueil.png")

        # Lancement depuis l'accueil : on arrive directement dans la vue de la réunion
        await page.fill("#f-url", "file:///tmp/loopback.html")
        await page.click("#opts summary")
        await page.select_option("#f-platform", "open"); await page.select_option("#f-worker", WA)
        await page.fill("#f-title", "Point d'équipe hebdomadaire")
        await page.screenshot(path=f"{OUT}/ui-2-options.png")
        await page.click("#btnStart")
        await page.wait_for_function(f"location.hash === '#m/{WA}'", timeout=15000)
        view = page.locator(f"#tab-meet .worker:not(.hidden)")
        await view.locator(".badge.in_meeting").wait_for(timeout=30000)
        await page.wait_for_function("(() => { const i = document.querySelector('#tab-meet .worker:not(.hidden) .shot'); return i && i.naturalWidth > 0; })()", timeout=15000)
        await view.locator(".b-audio").click()
        await page.wait_for_function(f"sessions['{WA}'] && sessions['{WA}'].pc.connectionState === 'connected'", timeout=20000)
        await asyncio.sleep(4)
        side = await view.locator(".side").bounding_box(); stage = await view.locator(".screen").bounding_box()
        out["vue_reunion"] = {"titre": await view.locator(".wtitle").inner_text(), "infos": await view.locator(".winfo").inner_text(),
                              "panneau_a_droite": side["x"] > stage["x"] + stage["width"] - 5,
                              "onglet_actif": await view.locator(".vt.on").inner_text(),
                              "panneau": (await view.locator(".an-body").inner_text())[:160],
                              "apercu_largeur": await view.locator(".shot").evaluate("i => i.naturalWidth")}
        await page.screenshot(path=f"{OUT}/ui-3-reunion.png")

        # Retour à l'accueil : la réunion y figure en vignette
        await view.locator(".back").click()
        tile = page.locator("#liveGrid .tile:not(.hidden)").first
        await tile.wait_for(timeout=5000)
        await page.wait_for_function("(() => { const i = document.querySelector('#liveGrid .tile:not(.hidden) img'); return i && i.naturalWidth > 0; })()", timeout=15000)
        out["vignette"] = (await tile.inner_text()).replace("\n", " | ")
        out["badge_menu"] = await page.inner_text("#liveBadge")
        await page.screenshot(path=f"{OUT}/ui-4-accueil-en-cours.png")

        # Retour dans la réunion par la vignette, prise en main (noVNC intégré)
        await tile.click()
        await view.locator(".b-screen").click()
        frame = page.frame_locator(f"#tab-meet .worker:not(.hidden) iframe.vnc")
        await frame.locator("#noVNC_container canvas, canvas").first.wait_for(timeout=20000)
        await asyncio.sleep(3)
        out["prise_en_main"] = await view.locator(".b-screen").inner_text()
        await page.screenshot(path=f"{OUT}/ui-5-prise-en-main.png")
        await view.locator(".b-screen").click()

        # Affichage iPhone : panneau sous l'écran
        await page.set_viewport_size({"width": 390, "height": 844})
        await asyncio.sleep(2)
        side = await view.locator(".side").bounding_box(); stage = await view.locator(".screen").bounding_box()
        out["iphone_panneau_dessous"] = side["y"] >= stage["y"] + stage["height"] - 5
        await page.screenshot(path=f"{OUT}/ui-6-iphone.png", full_page=True)
        await page.set_viewport_size({"width": 1440, "height": 900})

        # Départ : écran « réunion terminée »
        await view.locator(".b-leave").click()
        await view.locator(".badge.idle").wait_for(timeout=30000)
        await view.locator(".ended:not(.hidden)").wait_for(timeout=5000)
        out["fin"] = (await view.locator(".ended").inner_text()).replace("\n", " | ")
        await page.screenshot(path=f"{OUT}/ui-7-fin.png")

        # Accueil : plus de réunion en cours, la réunion est dans « Dernières réunions »
        await view.locator(".back").click()
        await page.wait_for_selector("#liveEmpty:not(.hidden)", timeout=10000)
        await page.wait_for_function("document.querySelector('#recent').innerText.includes(\"Point d'équipe\")", timeout=30000)
        out["dernieres"] = (await page.locator("#recent .ritem").first.inner_text()).replace("\n", " | ")
        await page.screenshot(path=f"{OUT}/ui-8-accueil-apres.png", full_page=True)
        out["erreurs_js"] = errs
        await b.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))

asyncio.run(main())
