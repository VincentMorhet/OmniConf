"""Test IHM des workers en conteneurs (plusieurs workers sur un même hôte) :
- chaque worker est en ligne, son écran distant (VNC, port propre à chaque conteneur) répond via le contrôle ;
- worker A : réunion « page loopback », audio WebRTC dans les deux sens, enregistrement relu dans le lecteur.
Usage : BASE=https://192.168.0.233 PW=… WA=w3 WORKERS=w3,w4 python container_ui_test.py
(la page tests/loopback.html doit être copiée dans le conteneur A : /tmp/loopback.html)"""
import asyncio, json, os
from playwright.async_api import async_playwright

BASE = os.environ.get("BASE", "https://192.168.0.233"); PW = os.environ["PW"]
WA = os.environ.get("WA", "w3"); WORKERS = os.environ.get("WORKERS", "w3,w4").split(",")

VNC = """async (name) => new Promise(res => {
  const ws = new WebSocket(`wss://${location.host}/vnc/${name}/websockify`, ['binary']);
  ws.binaryType = 'arraybuffer'; const t = setTimeout(() => res('timeout'), 8000);
  ws.onmessage = e => { clearTimeout(t); res(new TextDecoder().decode(e.data).trim()); ws.close(); };
  ws.onerror = () => { clearTimeout(t); res('erreur'); }; })"""


async def main():
    out = {}
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True, args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
        ctx = await b.new_context(ignore_https_errors=True, viewport={"width": 1400, "height": 900})
        page = await ctx.new_page(); errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await page.goto(BASE); await page.fill("#pw", PW); await page.click("#loginForm button")
        await page.wait_for_selector(".worker")
        st = await page.evaluate("async () => (await (await fetch('/api/workers')).json()).map(w => [w.name, w.online, w.state, w.vnc_port])")
        out["workers"] = st
        out["vnc"] = {w: await page.evaluate(VNC, w) for w in WORKERS}

        # Worker A : réunion loopback (le son reçu par le bot lui est renvoyé)
        await page.fill("#f-url", "file:///tmp/loopback.html"); await page.select_option("#f-platform", "open")
        await page.select_option("#f-worker", WA); await page.fill("#f-title", "test conteneur")
        await page.uncheck("#f-tr") if await page.locator("#f-tr").count() else None
        await page.click("#btnStart")
        card = page.locator(".worker", has=page.locator(".wname", has_text=WA))
        await card.locator(".badge.in_meeting").wait_for(timeout=30000)
        await card.locator(".rec:not(.hidden)").wait_for(timeout=15000)
        await card.locator(".b-audio").click()
        await page.wait_for_function(f"sessions['{WA}'] && sessions['{WA}'].pc.connectionState === 'connected'", timeout=20000)
        await card.locator(".b-mic").click()
        await asyncio.sleep(6)
        out["webrtc"] = await page.evaluate(f"""async () => {{ const r = {{}}; (await sessions['{WA}'].pc.getStats()).forEach(s => {{
            if (s.type === 'inbound-rtp' || s.type === 'outbound-rtp') r[s.type] = {{bytes: s.bytesReceived ?? s.bytesSent, level: s.audioLevel}};
            if (s.type === 'candidate-pair' && s.state === 'succeeded' && s.nominated) r.pair = s.id; }}); return r; }}""")
        out["apercu_largeur"] = await card.locator(".shot").evaluate("i => i.naturalWidth")
        await page.screenshot(path="/tmp/ct-ui.png")
        await card.locator(".b-leave").click()
        await card.locator(".badge.idle").wait_for(timeout=30000)

        # Enregistrement relu dans le lecteur
        await page.click("button[data-tab=rec]")
        print(json.dumps(out, ensure_ascii=False), flush=True)  # résultats intermédiaires
        await page.wait_for_selector(".ritem .play")
        item = page.locator(".ritem", has_text="test conteneur").first
        out["enregistrement"] = (await item.inner_text()).replace("\n", " | ")[:160]
        await item.locator(".play").click()
        await page.wait_for_function("document.querySelector('#player video').readyState >= 2", timeout=20000)
        out["video_duree_s"] = round(await page.evaluate("document.querySelector('#player video').duration"), 1)
        out["erreurs_js"] = errs
        await b.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))

asyncio.run(main())
