import asyncio, json, os
from playwright.async_api import async_playwright
BASE = os.environ.get("BASE", "https://192.168.0.230")  # IHM à tester
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        out = {}
        for label, vp in (("desktop", {"width": 1400, "height": 900}), ("mobile", {"width": 390, "height": 844})):
            ctx = await b.new_context(ignore_https_errors=True, viewport=vp, device_scale_factor=1)
            page = await ctx.new_page()
            errs = []; page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
            await page.goto(BASE + "/#rec"); await page.fill("#pw", os.environ["PW"]); await page.click("#loginForm button")
            await page.wait_for_selector(".ritem"); await page.wait_for_timeout(2500)
            out[label] = {"items": await page.locator(".ritem").count(), "count_badge": await page.inner_text("#recCount"),
                          "days": await page.locator("h3.day").all_inner_texts(), "storage": await page.inner_text("#storage"),
                          "thumbs_loaded": await page.evaluate("[...document.querySelectorAll('.ritem img')].filter(i=>i.naturalWidth>0).length"),
                          "errors": errs}
            await page.screenshot(path=f"/tmp/rec-{label}.png", full_page=False)
            if label == "desktop":
                await page.fill("#r-search", "portail"); out["search_portail"] = await page.locator(".ritem").count()
                await page.fill("#r-search", ""); await page.select_option("#r-platform", "zoom"); out["filter_zoom"] = await page.locator(".ritem").count()
                await page.select_option("#r-platform", "")
                page.on("dialog", lambda d: asyncio.ensure_future(d.accept("Revue sprint portail (renommé)") if d.type == "prompt" else d.dismiss()))
                it = page.locator(".ritem", has_text="Revue sprint portail")
                await it.locator(".ren").click(); await page.wait_for_selector(".ritem:has-text('(renommé)')")
                out["renamed"] = True
                await page.locator(".ritem", has_text="(renommé)").locator(".play").click()
                await page.wait_for_function("document.querySelector('#player video').readyState >= 2", timeout=15000)
                out["player_title"] = await page.inner_text("#playerTitle")
                dl = await page.get_attribute("#playerDl", "href")
                r = await page.request.get(BASE + dl)
                out["download"] = {"status": r.status, "disposition": r.headers.get("content-disposition"), "bytes": len(await r.body())}
                await page.screenshot(path="/tmp/rec-player.png")
            await ctx.close()
        print(json.dumps(out, ensure_ascii=False, indent=1)); await b.close()
asyncio.run(main())
