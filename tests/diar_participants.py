# Lance 3 participants Jitsi (Chrome headless), chacun avec un faux micro lisant son fichier audio
import asyncio, sys
from playwright.async_api import async_playwright
ROOM = sys.argv[1]; STAY = float(sys.argv[2])
import os; D = os.environ.get("CONV", "/tmp/conv_out"); PEOPLE = [("Alice Martin", f"{D}/alice.wav"), ("Bruno Petit", f"{D}/bruno.wav"), ("Salle Nord", f"{D}/salle.wav")]
async def one(p, name, wav):
    b = await p.chromium.launch(channel="chrome", headless=True, args=[
        "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", f"--use-file-for-fake-audio-capture={wav}",
        "--autoplay-policy=no-user-gesture-required"])
    page = await (await b.new_context()).new_page()
    await page.goto(f'{ROOM}#config.prejoinConfig.enabled=false&config.startWithVideoMuted=true&config.startWithAudioMuted=false&userInfo.displayName="{name}"')
    return b, page
async def unmute(page):
    for _ in range(60):
        try:
            if await page.evaluate("() => !!(window.APP && APP.conference && APP.conference.isJoined && APP.conference.isJoined())"):
                break
        except Exception: pass
        await asyncio.sleep(0.5)
    await page.evaluate("() => APP.conference.muteAudio(false)")
    return await page.evaluate("() => APP.conference.isLocalAudioMuted()")
async def main():
    async with async_playwright() as p:
        bs = await asyncio.gather(*(one(p, n, w) for n, w in PEOPLE))
        print("muted after unmute:", await asyncio.gather(*(unmute(pg) for _, pg in bs)), flush=True)
        await asyncio.sleep(STAY)
        for b, _ in bs: await b.close()
asyncio.run(main())
