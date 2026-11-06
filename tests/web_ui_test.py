"""IHM : lien d'une webconf quelconque (nom par défaut, options, carte worker, pas d'erreur JS)."""
import asyncio, json, os
from playwright.async_api import async_playwright
BASE = os.environ.get("BASE", "https://192.168.0.230")  # IHM à tester
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        page = await (await b.new_context(ignore_https_errors=True)).new_page()
        errs = []; page.on("pageerror", lambda e: errs.append(str(e))); page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await page.goto(BASE); await page.fill("#pw", os.environ["PW"]); await page.click("#loginForm button")
        await page.wait_for_selector(".worker")
        await page.fill("#f-url", "https://meet.ffmuc.net/WebconfUiTest")
        out = {"web_name": await page.input_value("#f-name"),
               "options": await page.eval_on_selector_all("#f-platform option", "o => o.map(x => x.textContent)"),
               "placeholder": await page.get_attribute("#f-url", "placeholder")}
        await page.select_option("#f-worker", "w2"); await page.uncheck("#f-rec"); await page.click("#btnStart")
        for _ in range(20):
            await asyncio.sleep(3)
            info = await page.inner_text(".worker:has(.wname:text('w2')) .winfo")
            badge = await page.inner_text(".worker:has(.wname:text('w2')) .badge")
            if badge == "en réunion": break
        out["card"] = f"{badge} | {info}"
        await page.click(".worker:has(.wname:text('w2')) .b-leave"); await asyncio.sleep(8)
        out["after_leave"] = await page.inner_text(".worker:has(.wname:text('w2')) .badge")
        out["js_errors"] = errs
        print(json.dumps(out, ensure_ascii=False, indent=1)); await b.close()
asyncio.run(main())
