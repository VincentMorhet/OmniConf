"""IHM : panneau de transcription en direct sur la carte w2, puis transcription dans le lecteur des enregistrements."""
import asyncio, json, os
from playwright.async_api import async_playwright
BASE = os.environ.get("BASE", "https://192.168.0.230")  # IHM à tester
W = os.environ.get("W", "w2")
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        ctx = await b.new_context(ignore_https_errors=True, viewport={"width": 1400, "height": 1000})
        page = await ctx.new_page()
        errs = []; page.on("pageerror", lambda e: errs.append(str(e))); page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await page.goto(BASE); await page.fill("#pw", os.environ["PW"]); await page.click("#loginForm button")
        card = f".worker:has(.wname:text('{W}'))"
        await page.wait_for_selector(card)
        await asyncio.sleep(8)
        out = {"panel_visible": await page.is_visible(f"{card} .tr-panel"),
               "status": await page.inner_text(f"{card} .tr-status"),
               "live_text": (await page.inner_text(f"{card} .tr-body"))[:400]}
        await page.locator(f"{card} .tr-panel").screenshot(path="/tmp/tr_panel.png")
        await page.click(f"{card} .b-leave"); await asyncio.sleep(10)
        out["after_leave_status"] = await page.inner_text(f"{card} .tr-status")
        await page.click("[data-tab=rec]"); await page.wait_for_selector(".ritem")
        first = page.locator(".ritem").first
        out["first_item"] = (await first.inner_text())[:200]
        out["has_tr_badge"] = await first.locator(".thumb .tr").count()
        await first.locator(".play").click(); await asyncio.sleep(3)
        out["player_tr"] = (await page.inner_text("#playerTr"))[:300]
        out["md_link"] = await page.get_attribute("#playerMd", "href")
        md = await page.evaluate("async (u) => (await fetch(u)).text()", out["md_link"])
        out["md"] = md[:500]
        await page.locator("#playerTr .ts.seek").nth(1).click(); await asyncio.sleep(1)
        out["video_time_after_seek"] = await page.evaluate("document.querySelector('#player video').currentTime")
        await page.screenshot(path="/tmp/tr_player.png")
        out["js_errors"] = errs
        print(json.dumps(out, ensure_ascii=False, indent=1)); await b.close()
asyncio.run(main())
