"""IHM : compte rendu dans le lecteur (onglets, tableaux, horodatages, Word/Markdown, renommage qui met
à jour le CR) ; optionnellement points d'étape en direct sur la carte d'un worker (LIVE=w1).
Usage (sur un worker) : PW=… python report_ui_test.py"""
import asyncio, json, os, re
from playwright.async_api import async_playwright
BASE = os.environ.get("BASE", "https://192.168.0.230")  # IHM à tester
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        ctx = await b.new_context(ignore_https_errors=True, viewport={"width": int(os.environ.get("VW", 1400)), "height": 1000})
        page = await ctx.new_page()
        errs = []; page.on("pageerror", lambda e: errs.append(str(e))); page.on("console", lambda m: m.type == "error" and errs.append(m.text))
        answers = ["Claire Dubois"]
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept(answers.pop(0)) if d.type == "prompt" and answers else d.accept()))
        await page.goto(BASE); await page.fill("#pw", os.environ["PW"]); await page.click("#loginForm button")
        out = {}
        live = os.environ.get("LIVE")
        if live:
            card = f".worker:has(.wname:text('{live}'))"
            await page.wait_for_selector(f"{card} .tr-panel:not(.hidden)", timeout=20000)
            await page.click(f"{card} .vt[data-v=an]"); await asyncio.sleep(3)
            out["live_status"] = await page.inner_text(f"{card} .tr-status")
            out["live_sections"] = await page.locator(f"{card} .an-body h4").all_inner_texts()
            await page.locator(f"{card} .tr-panel").screenshot(path="/tmp/an_live.png")
            if os.environ.get("ONLY_LIVE"):
                print(json.dumps(out, ensure_ascii=False, indent=1)); print("errors:", errs); return
        await page.click("[data-tab=rec]"); await asyncio.sleep(3)
        item = page.locator(".ritem", has=page.locator(".cr")).first
        out["rec_buttons"] = await item.locator(".ractions").inner_text()
        await item.locator(".cr").click(); await asyncio.sleep(4)
        out["tabs"] = [t for t in await page.locator("#playerTabs .vt:visible").all_inner_texts()]
        out["cr_sections"] = await page.locator("#playerCr h4").all_inner_texts()
        out["cr_h2"] = await page.inner_text("#playerCr h2")
        out["n_actions"] = await page.locator("#playerCr section:has(h4:text('Actions')) tr").count() - 1
        out["first_action"] = await page.locator("#playerCr section:has(h4:text('Actions')) tr").nth(1).inner_text()
        await page.locator("#playerCr").screenshot(path="/tmp/cr_player.png")
        # clic sur un horodatage du CR -> transcription au bon endroit (pas de vidéo)
        await page.locator("#playerCr .ts.seek").nth(3).click(); await asyncio.sleep(1)
        out["after_seek_view"] = await page.locator("#playerTabs .vt.on").inner_text()
        out["highlighted"] = await page.locator("#playerTr p.hl").count()
        # renommage dans la transcription -> le CR suit
        await page.locator("#playerSpk .chip", has_text="Salle Nord (2)").click(); await asyncio.sleep(2)
        await page.click("#playerTabs .vt[data-v=cr]"); await asyncio.sleep(1)
        out["cr_has_new_name"] = "Claire Dubois" in await page.inner_text("#playerCr")
        href = await page.get_attribute("#playerCr a[href*=docx]", "href")
        r = await page.request.get(BASE + href)
        open("/tmp/cr.docx", "wb").write(await r.body()); out["docx"] = [r.status, r.headers.get("content-disposition"), len(await r.body())]
        md = await (await page.request.get(BASE + href.replace("docx", "md"))).text()
        out["md_has_new_name"] = "Claire Dubois" in md
        await page.click("#playerTabs .vt[data-v=an]"); await asyncio.sleep(1)
        out["an_sections"] = await page.locator("#playerAn h4").all_inner_texts()
        # remet le nom automatique
        answers.append(""); await page.click("#playerTabs .vt[data-v=tr]")
        await page.locator("#playerSpk .chip", has_text="Claire Dubois").click(); await asyncio.sleep(2)
        print(json.dumps(out, ensure_ascii=False, indent=1)); print("errors:", errs)
        await b.close()
asyncio.run(main())
