"""Test des actions sur les enregistrements (taille iPad) : boutons visibles en entier dans chaque
vignette, menu « ⋯ » (télécharger, renommer, supprimer), suppression avec confirmation depuis
le menu puis depuis le lecteur. Crée deux courts enregistrements jetables sur l'assistant WA.
Usage : BASE=https://192.168.0.233 PW=… WA=w3 OUT=/tmp/shots python rec_actions_ui_test.py
(la page tests/loopback.html doit être copiée dans le conteneur WA : /tmp/loopback.html)"""
import asyncio, json, os
from playwright.async_api import async_playwright

BASE = os.environ.get("BASE", "https://192.168.0.233"); PW = os.environ["PW"]
WA = os.environ.get("WA", "w3"); OUT = os.environ.get("OUT", "/tmp")
FIT = """() => [...document.querySelectorAll('#recordings .ritem')].map(c => { const r = c.getBoundingClientRect();
  return [...c.querySelectorAll('.ractions > *')].every(b => { const x = b.getBoundingClientRect(); return x.right <= r.right + 1 && x.width > 30; }); })"""


async def meeting(page, title):
    await page.goto(BASE + "/#home"); await page.wait_for_selector("#tab-home:not(.hidden) .wchip")
    await page.fill("#f-url", "file:///tmp/loopback.html"); await page.click("#opts summary")
    await page.select_option("#f-platform", "open"); await page.select_option("#f-worker", WA)
    await page.uncheck("#f-tr"); await page.fill("#f-title", title); await page.click("#btnStart")
    view = page.locator("#tab-meet .worker:not(.hidden)")
    await view.locator(".badge.in_meeting").wait_for(timeout=30000); await asyncio.sleep(4)
    await view.locator(".b-leave").click(); await view.locator(".badge.idle").wait_for(timeout=30000)


async def main():
    out = {}
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        ctx = await b.new_context(ignore_https_errors=True, viewport={"width": 1000, "height": 720}, color_scheme="light")
        page = await ctx.new_page(); errs = []; dialogs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("dialog", lambda d: (dialogs.append(d.message), asyncio.ensure_future(d.accept())))
        await page.goto(BASE); await page.fill("#pw", PW); await page.click("#loginForm button")
        for t in ("A supprimer 1", "A supprimer 2"):
            await meeting(page, t)
        await page.click("button[data-tab=rec]"); await page.wait_for_selector("#recordings .ritem")
        await page.wait_for_function("document.querySelector('#recordings').innerText.includes('A supprimer 2')", timeout=20000)
        out["boutons_visibles_en_entier"] = all(await page.evaluate(FIT))
        out["boutons_vignette"] = await page.locator("#recordings .ritem").first.locator(".ractions > *").all_inner_texts()

        # 1) suppression par le menu « ⋯ »
        card = page.locator("#recordings .ritem", has_text="A supprimer 1").first
        await card.locator(".more").click()
        out["menu"] = await card.locator(".rmenu > *").all_inner_texts()
        await page.screenshot(path=f"{OUT}/rec-menu.png")
        await page.mouse.click(5, 300)   # clic ailleurs : le menu se ferme
        out["menu_ferme_au_clic_ailleurs"] = await card.locator(".rmenu").is_hidden()
        await card.locator(".more").click(); await card.locator(".rmenu .del").click()
        await page.wait_for_function("!document.querySelector('#recordings').innerText.includes('A supprimer 1')", timeout=15000)
        out["supprime_par_menu"] = True

        # 2) suppression depuis le lecteur
        await page.locator("#recordings .ritem", has_text="A supprimer 2").first.locator(".play").click()
        await page.wait_for_selector("#player:not(.hidden)")
        await asyncio.sleep(1); await page.screenshot(path=f"{OUT}/rec-lecteur.png")
        await page.click("#playerDel")
        await page.wait_for_selector("#player.hidden", state="attached", timeout=15000)
        await page.wait_for_function("!document.querySelector('#recordings').innerText.includes('A supprimer 2')", timeout=15000)
        out["supprime_depuis_lecteur"] = True
        out["confirmations"] = [d.split("\n")[0] for d in dialogs if d.startswith("Supprimer")]
        await page.screenshot(path=f"{OUT}/rec-apres.png")
        out["erreurs_js"] = errs
        await b.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))

asyncio.run(main())
