"""Test du mode « Suivre en grand » pendant une vraie réunion parlée (à lancer pendant
container_meeting_e2e.sh) : onglet #suivre/<assistant>, écran fluide (noVNC en lecture seule),
écoute, sous-titres en direct, résumé de l'IA (mise à jour forcée), affichage téléphone, retour.
Usage : BASE=https://192.168.0.233 PW=… WA=w4 OUT=/tmp/shots python focus_ui_test.py"""
import asyncio, json, os, time
from playwright.async_api import async_playwright

BASE = os.environ.get("BASE", "https://192.168.0.233"); PW = os.environ["PW"]
WA = os.environ.get("WA", "w4"); OUT = os.environ.get("OUT", "/tmp")


async def main():
    out = {}
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True, args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
        ctx = await b.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900})
        page = await ctx.new_page(); errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        await page.goto(BASE); await page.fill("#pw", PW); await page.click("#loginForm button")
        await page.wait_for_selector("#tab-home:not(.hidden) .wchip")
        # attendre que l'assistant soit en réunion, puis « Suivre en grand » depuis la vignette (nouvel onglet)
        tile = page.locator("#liveGrid .tile:not(.hidden)", has_text=f"Assistant {WA}")
        await tile.wait_for(timeout=180000)
        async with ctx.expect_page() as pinfo:
            await tile.locator(".tfollow").click()
        f = await pinfo.value; f.on("pageerror", lambda e: errs.append("onglet: " + str(e))); f.on("console", lambda m: errs.append("console: " + m.text[:200]) if m.type == "error" else None)
        await f.wait_for_selector("#tab-focus:not(.hidden)")
        out["adresse_onglet"] = f.url.split("/", 3)[-1]
        await f.wait_for_selector("#fVnc canvas", timeout=20000)
        await f.wait_for_function("document.querySelector('#fMsg').classList.contains('hidden')", timeout=20000)
        out["ecran_fluide"] = await f.evaluate("(() => { const c = document.querySelector('#fVnc canvas'); return [c.width, c.height]; })()")
        await f.locator("#fCtl .b-audio").click()
        await f.wait_for_function(f"sessions['{WA}'] && sessions['{WA}'].pc.connectionState === 'connected'", timeout=20000)
        # sous-titres
        t0 = time.time()
        await f.wait_for_function("!document.querySelector('#fCaps').classList.contains('hidden') && document.querySelector('#fCaps').innerText.length > 10", timeout=120000)
        out["sous_titres_apres_s"] = round(time.time() - t0)
        out["sous_titres"] = (await f.inner_text("#fCaps")).replace("\n", " / ")[:300]
        await f.screenshot(path=f"{OUT}/focus-1-sous-titres.png")
        # résumé de l'IA : mise à jour forcée au bout d'une minute de parole
        await asyncio.sleep(60)
        await f.locator("#fSidebox .an-now").click()
        try:
            await f.wait_for_selector("#fSidebox .an-body h4", timeout=240000)
        except Exception:
            out["diagnostic"] = await f.evaluate(f"""({{an: JSON.stringify(trState('{WA}').an?.state || null).slice(0, 200), v: trState('{WA}').anV,
                view: trState('{WA}').view, panneau: document.querySelector('#fSidebox .an-body')?.innerText.slice(0, 200),
                statut: JSON.stringify(lastStatus['{WA}']?.transcript?.analysis)}})""")
            out["erreurs_js"] = errs; print(json.dumps(out, ensure_ascii=False, indent=1)); raise
        await asyncio.sleep(2)
        out["resume_sections"] = await f.locator("#fSidebox .an-body h4").all_inner_texts()
        await f.screenshot(path=f"{OUT}/focus-2-resume.png")
        await f.click("#fSidebox .vt[data-v=tr]"); await asyncio.sleep(1)
        out["transcription_paragraphes"] = await f.locator("#fSidebox .tr-body p").count()
        await f.click("#fSidebox .vt[data-v=an]")
        # sans le panneau, puis format téléphone
        await f.click("#fSide"); await asyncio.sleep(2)
        await f.screenshot(path=f"{OUT}/focus-3-sans-panneau.png")
        await f.click("#fSide")
        await f.set_viewport_size({"width": 390, "height": 844}); await asyncio.sleep(3)
        await f.screenshot(path=f"{OUT}/focus-4-iphone.png")
        await f.set_viewport_size({"width": 1440, "height": 900})
        # retour à la vue normale : le panneau et l'écoute y reviennent
        await f.click("#fBack")
        await f.wait_for_selector("#tab-meet:not(.hidden) .worker:not(.hidden) .side .an-body")
        out["retour_vue_normale"] = {"panneau": await f.locator("#tab-meet .worker:not(.hidden) .side").count(),
                                     "ecoute": await f.locator("#tab-meet .worker:not(.hidden) .audio-ctl .b-audio").inner_text()}
        out["erreurs_js"] = errs
        await b.close()
    print(json.dumps(out, ensure_ascii=False, indent=1), flush=True)

asyncio.run(main())
