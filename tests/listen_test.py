"""Mesure le niveau sonore réellement décodé par Chrome : audio live (WebRTC) et lecture d'un enregistrement."""
import asyncio, json, os
from playwright.async_api import async_playwright
BASE = os.environ.get("BASE", "https://192.168.0.230")  # IHM à tester
MEASURE = """async (stream) => { const ctx = new AudioContext(); await ctx.resume();
  const an = ctx.createAnalyser(); an.fftSize = 2048; ctx.createMediaStreamSource(stream).connect(an);
  const buf = new Float32Array(2048); let peak = 0, sum = 0, n = 0;
  for (let i = 0; i < 60; i++) { await new Promise(r => setTimeout(r, 50)); an.getFloatTimeDomainData(buf);
    for (const v of buf) { peak = Math.max(peak, Math.abs(v)); sum += v * v; n++; } }
  ctx.close(); return {peak: +peak.toFixed(3), rms: +Math.sqrt(sum / n).toFixed(4)}; }"""
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        page = await (await b.new_context(ignore_https_errors=True)).new_page()
        await page.goto(BASE + "/"); await page.fill("#pw", os.environ["PW"]); await page.click("#loginForm button")
        w1 = page.locator(".worker", has=page.locator(".wname", has_text="w1"))
        await w1.locator(".b-audio").click()
        await page.wait_for_function("sessions.w1 && sessions.w1.pc.connectionState === 'connected' && sessions.w1.audio.srcObject", timeout=15000)
        live = await page.evaluate(f"({MEASURE})(sessions.w1.audio.srcObject)")
        el = await page.evaluate("({paused: sessions.w1.audio.paused, muted: sessions.w1.audio.muted, volume: sessions.w1.audio.volume})")
        await w1.locator(".b-audio").click()   # on se déconnecte de la réunion
        await page.goto(BASE + "/#rec"); await page.wait_for_selector(".ritem .play")
        await page.locator(".ritem").nth(int(os.environ.get("IDX","0"))).locator(".play").click()
        await page.wait_for_function("document.querySelector('#player video').currentTime > 1", timeout=20000)
        await page.evaluate("document.querySelector('#player video').currentTime = " + os.environ.get("SEEK","60") + "")
        await page.wait_for_timeout(1000)
        rec = await page.evaluate(f"({MEASURE})(document.querySelector('#player video').captureStream())")
        print(json.dumps({"live_webrtc": live, "live_element": el, "recording_playback": rec})); await b.close()
asyncio.run(main())
