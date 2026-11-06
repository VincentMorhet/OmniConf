"""Panneau audio : liste des périphériques, choix micro/sortie, changement de micro à chaud pendant une session."""
import asyncio, json, os
from playwright.async_api import async_playwright
BASE = os.environ.get("BASE", "https://192.168.0.230")  # IHM à tester
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True, args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
        ctx = await b.new_context(ignore_https_errors=True, viewport={"width": 390, "height": 844})
        page = await ctx.new_page(); errs = []
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errs.append(str(e)))
        await page.goto(BASE + "/"); await page.fill("#pw", os.environ["PW"]); await page.click("#loginForm button")
        await page.wait_for_selector(".worker")
        await page.click("#audioBtn"); await page.wait_for_timeout(500)
        await page.click("#micTestBtn"); await page.wait_for_timeout(1500)
        out = {"mics": await page.locator("#micSel option").all_inner_texts(),
               "outputs": await page.locator("#spkSel option").all_inner_texts(),
               "mic_level": await page.eval_on_selector("#micTestLvl", "m => m.value")}
        await page.screenshot(path="/tmp/audio-panel.png")
        opts = await page.locator("#micSel option").evaluate_all("o => o.map(x => x.value)")
        if len(opts) > 1:
            await page.select_option("#micSel", opts[1]); await page.wait_for_timeout(500)
        out["saved_micId"] = await page.evaluate("localStorage.micId")
        await page.click("#audioModal .close")
        # session audio sur w2 (libre) puis changement de micro à chaud
        w2 = page.locator(".worker", has=page.locator(".wname", has_text="w2"))
        await w2.locator(".b-audio").click()
        await page.wait_for_function("sessions.w2 && sessions.w2.pc.connectionState === 'connected'", timeout=15000)
        before = await page.evaluate("sessions.w2.stream.getAudioTracks()[0].label")
        await page.evaluate("setMicDevice('')"); await page.wait_for_timeout(800)
        after = await page.evaluate("[sessions.w2.stream.getAudioTracks()[0].label, sessions.w2.pc.getSenders()[0].track === sessions.w2.stream.getAudioTracks()[0], sessions.w2.pc.connectionState]")
        out.update({"track_before": before, "track_after_label/sender_ok/state": after, "errors": errs})
        await w2.locator(".b-audio").click()
        print(json.dumps(out, ensure_ascii=False, indent=1)); await b.close()
asyncio.run(main())
