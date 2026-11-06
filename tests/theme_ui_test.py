"""Test du thème blanc / noir : bascule, mémorisation après rechargement, rendu des écrans
(accueil, vue d'une réunion, enregistrements, compte rendu) dans les deux thèmes.
Usage : BASE=https://192.168.0.233 PW=… WA=w3 OUT=/tmp/shots python theme_ui_test.py
(la page tests/loopback.html doit être copiée dans le conteneur WA : /tmp/loopback.html)"""
import asyncio, json, os
from playwright.async_api import async_playwright

BASE = os.environ.get("BASE", "https://192.168.0.233"); PW = os.environ["PW"]
WA = os.environ.get("WA", "w3"); OUT = os.environ.get("OUT", "/tmp")
THEME = "document.documentElement.dataset.theme"
BG = "getComputedStyle(document.body).backgroundColor"


async def main():
    out = {}
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True, args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
        ctx = await b.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900}, color_scheme="dark")
        page = await ctx.new_page(); errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await page.goto(BASE)
        out["defaut_appareil_sombre"] = await page.evaluate(THEME)
        await page.fill("#pw", PW); await page.click("#loginForm button")
        await page.wait_for_selector("#tab-home:not(.hidden) .wchip")
        await page.click("#themeBtn")
        out["apres_clic"] = {"theme": await page.evaluate(THEME), "memorise": await page.evaluate("localStorage.theme"),
                             "fond": await page.evaluate(BG), "bouton": await page.inner_text("#themeBtn")}
        await page.reload(); await page.wait_for_selector("#tab-home:not(.hidden) .wchip"); await asyncio.sleep(2)
        out["apres_rechargement"] = await page.evaluate(THEME)
        await page.screenshot(path=f"{OUT}/theme-blanc-accueil.png")

        # Réunion de test, vue dans les deux thèmes
        await page.fill("#f-url", "file:///tmp/loopback.html"); await page.click("#opts summary")
        await page.select_option("#f-platform", "open"); await page.select_option("#f-worker", WA)
        await page.fill("#f-title", "Essai thème"); await page.click("#btnStart")
        view = page.locator("#tab-meet .worker:not(.hidden)")
        await view.locator(".badge.in_meeting").wait_for(timeout=30000)
        await page.wait_for_function("(() => { const i = document.querySelector('#tab-meet .worker:not(.hidden) .shot'); return i && i.naturalWidth > 0; })()", timeout=15000)
        await asyncio.sleep(2)
        await page.screenshot(path=f"{OUT}/theme-blanc-reunion.png")
        await page.click("#themeBtn"); await asyncio.sleep(1)
        out["retour_noir"] = {"theme": await page.evaluate(THEME), "fond": await page.evaluate(BG)}
        await page.screenshot(path=f"{OUT}/theme-noir-reunion.png")
        await page.click("#themeBtn")
        await view.locator(".b-leave").click(); await view.locator(".badge.idle").wait_for(timeout=30000)

        # Enregistrements et compte rendu (premier enregistrement qui en a un)
        await page.click("button[data-tab=rec]"); await page.wait_for_selector("#recordings .ritem")
        await asyncio.sleep(2); await page.screenshot(path=f"{OUT}/theme-blanc-enregistrements.png")
        cr = page.locator("#recordings .ritem .cr").first
        if await cr.count():
            await cr.click(); await page.wait_for_selector("#playerCr h4", timeout=20000); await asyncio.sleep(2)
            await page.screenshot(path=f"{OUT}/theme-blanc-compte-rendu.png")
            await page.click("#playerTabs .vt[data-v=tr]"); await asyncio.sleep(1)
            await page.screenshot(path=f"{OUT}/theme-blanc-transcription.png")
            out["compte_rendu_vu"] = True
        out["erreurs_js"] = errs
        await b.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))

asyncio.run(main())
