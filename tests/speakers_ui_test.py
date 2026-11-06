"""IHM : pastilles d'intervenants, renommage en direct puis dans le lecteur, fichier Markdown."""
import asyncio, json, os, re
from playwright.async_api import async_playwright
BASE = os.environ.get("BASE", "https://192.168.0.230")  # IHM à tester
W = os.environ.get("W", "w2")
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        page = await (await b.new_context(ignore_https_errors=True, viewport={"width": 1400, "height": 1000})).new_page()
        errs = []; page.on("pageerror", lambda e: errs.append(str(e)))
        answers = ["Marie Durand", "Jean Leroy"]
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept(answers.pop(0)) if d.type == "prompt" and answers else d.accept()))
        await page.goto(BASE); await page.fill("#pw", os.environ["PW"]); await page.click("#loginForm button")
        card = f".worker:has(.wname:text('{W}'))"
        await page.wait_for_selector(f"{card} .tr-spk .chip", timeout=20000)
        out = {"chips_live": await page.locator(f"{card} .tr-spk .chip").all_inner_texts()}
        # renommage en direct : « Salle Nord (2) » -> « Marie Durand »
        await page.locator(f"{card} .tr-spk .chip", has_text="Salle Nord (2)").click(); await asyncio.sleep(1.5)
        out["chips_after_rename"] = await page.locator(f"{card} .tr-spk .chip").all_inner_texts()
        out["para_names"] = await page.locator(f"{card} .tr-body p .spk").all_inner_texts()
        await page.locator(f"{card} .tr-panel").screenshot(path="/tmp/spk_panel.png")
        page.on("dialog", lambda d: None)
        await page.click(f"{card} .b-leave"); await asyncio.sleep(10)
        await page.click("[data-tab=rec]"); await asyncio.sleep(3)
        await page.locator(".ritem .play").first.click(); await asyncio.sleep(3)
        out["player_chips"] = await page.locator("#playerSpk .chip").all_inner_texts()
        # renommage dans le lecteur : « Salle Nord » -> « Jean Leroy »
        await page.locator("#playerSpk .chip", has_text=re.compile(r"^Salle Nord")).first.click(); await asyncio.sleep(2)
        out["player_chips_after"] = await page.locator("#playerSpk .chip").all_inner_texts()
        md = await page.evaluate("async (u) => (await fetch(u)).text()", await page.get_attribute("#playerMd", "href"))
        out["md"] = md[:900]
        await page.screenshot(path="/tmp/spk_player.png")
        out["js_errors"] = errs
        print(json.dumps(out, ensure_ascii=False, indent=1)); await b.close()
asyncio.run(main())
