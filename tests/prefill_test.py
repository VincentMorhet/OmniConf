import asyncio, json, os
from playwright.async_api import async_playwright
BASE = os.environ.get("BASE", "https://192.168.0.230")  # IHM à tester
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        page = await (await b.new_context(ignore_https_errors=True)).new_page()
        await page.goto(BASE); await page.fill("#pw", os.environ["PW"]); await page.click("#loginForm button")
        await page.wait_for_selector(".worker")
        out = {"dur": await page.input_value("#f-dur")}
        await page.fill("#f-url", "https://us02web.zoom.us/j/81234567890?pwd=abc"); out["zoom"] = await page.input_value("#f-name")
        await page.fill("#f-url", "https://teams.microsoft.com/l/meetup-join/19%3ameeting_x/0"); out["teams"] = await page.input_value("#f-name")
        await page.fill("#f-name", "Autre nom"); await page.fill("#f-url", "https://zoom.us/j/123456789"); out["manuel_conservé"] = await page.input_value("#f-name")
        print(json.dumps(out, ensure_ascii=False)); await b.close()
asyncio.run(main())
