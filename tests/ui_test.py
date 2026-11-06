"""Test IHM de bout en bout avec un vrai Chrome (micro simulé = bip) :
login -> lancement réunion (page loopback) sur w1 -> audio WebRTC -> micro ouvert -> stats -> quitter."""
import asyncio, json, os, sys
from playwright.async_api import async_playwright
BASE = os.environ.get("BASE", "https://192.168.0.230"); PW = os.environ["PW"]
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True, args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
        ctx = await b.new_context(ignore_https_errors=True, viewport={"width": 390, "height": 844})  # taille iPhone
        page = await ctx.new_page()
        page.on("console", lambda m: print("console:", m.text) if m.type == "error" else None)
        await page.goto(BASE)
        await page.fill("#pw", PW); await page.click("#loginForm button")
        await page.wait_for_selector(".worker .wname:text('w1')")
        await page.fill("#f-url", "file:///tmp/loopback.html"); await page.select_option("#f-platform", "web")
        await page.select_option("#f-worker", "w1"); await page.fill("#f-title", "test IHM")
        await page.click("#btnStart")
        w1 = page.locator(".worker", has=page.locator(".wname", has_text="w1"))
        await w1.locator(".badge.in_meeting").wait_for(timeout=20000)
        await w1.locator(".rec:not(.hidden)").wait_for(timeout=10000)
        await w1.locator(".b-audio").click()
        await page.wait_for_function("sessions.w1 && sessions.w1.pc.connectionState === 'connected'", timeout=15000)
        await w1.locator(".b-mic").click()   # micro ouvert
        await asyncio.sleep(5)
        stats = await page.evaluate("""async () => { const r = {}; (await sessions.w1.pc.getStats()).forEach(s => {
            if (s.type === 'inbound-rtp' || s.type === 'outbound-rtp') r[s.type] = {bytes: s.bytesReceived ?? s.bytesSent, level: s.audioLevel};
          }); return r; }""")
        shot = await w1.locator(".shot").evaluate("i => i.naturalWidth")
        await page.screenshot(path="/tmp/ui.png", full_page=True)
        print(json.dumps({"stats": stats, "screenshot_width": shot, "mic_btn": await w1.locator(".b-mic").inner_text()}))
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await w1.locator(".b-leave").click()
        await w1.locator(".badge.idle").wait_for(timeout=20000)
        await page.click("button[data-tab=rec]")
        await page.wait_for_selector("#recordings li .play")
        first = await page.locator("#recordings li").first.inner_text()
        await page.locator("#recordings li .play").first.click()
        await page.wait_for_function("document.querySelector('#player video').readyState >= 2", timeout=15000)
        dur = await page.evaluate("document.querySelector('#player video').duration")
        await page.screenshot(path="/tmp/ui-rec.png")
        print(json.dumps({"first_recording": first, "video_duration": dur}))
        await b.close()
asyncio.run(main())
