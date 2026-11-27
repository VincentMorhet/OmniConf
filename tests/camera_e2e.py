"""Caméra « image » de bout en bout dans une vraie réunion Jitsi (27/11), sur un assistant d'essai (w3 de la 233).

1. L'assistant entre avec une image d'essai (orange) : il entre caméra COUPÉE, et la reste.
2. La caméra « OmniConf (image) » apparaît une fois l'entrée confirmée (journal de l'assistant).
3. On l'allume comme l'utilisateur le ferait via « Prendre la main » : touche « V » de Jitsi envoyée par l'écran
   distant (VNC) de l'assistant.
4. Un témoin (autre navigateur) reçoit bien la vidéo de l'assistant, et c'est l'image d'essai (pixel central orange).
Puis tout le monde sort. Aucune donnée réelle : salle Jitsi publique jetable, image fictive.

À lancer dans un conteneur d'assistant AUTRE que celui essayé (réseau de l'hôte : l'API et le VNC de w3 sont sur
127.0.0.1) : AGENT_PORT=8001 RFB_PORT=5900 AGENT_TOKEN=… CAM_JPEG=/tmp/wk/cam.jpg python tests/camera_e2e.py
"""
import asyncio
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request

PORT, RFB, TOKEN = os.environ["AGENT_PORT"], int(os.environ["RFB_PORT"]), os.environ["AGENT_TOKEN"]
BOT = "Essai caméra"
ROOM = f"https://meet.ffmuc.net/OmniconfCamera{int(time.time())}"
OUT = os.environ.get("TEST_DIR", "/tmp/wk")
ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what, flush=True)


def call(method, path, body=None):
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method,
                               data=json.dumps(body).encode() if body is not None else None,
                               headers={"x-agent-token": TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as x:
        return json.load(x)


def rfb_key(keysym: int):
    """Appui sur une touche par l'écran distant (protocole RFB, sans mot de passe en local), comme « Prendre la main »."""
    s = socket.create_connection(("127.0.0.1", RFB), timeout=10)
    s.recv(12)
    s.sendall(b"RFB 003.008\n")
    n = s.recv(1)[0]
    s.recv(n)
    s.sendall(b"\x01")                  # sécurité : aucune
    s.recv(4)
    s.sendall(b"\x01")                  # partagé
    init = s.recv(24)
    s.recv(struct.unpack(">I", init[20:24])[0])
    for down in (1, 0):
        s.sendall(struct.pack(">BBxxI", 4, down, keysym))
        time.sleep(0.08)
    s.close()


PEER_STATE = """(bot) => {
  const st = APP.store.getState(), parts = st['features/base/participants'];
  const remote = [...(parts.remote?.values?.() || [])].find(p => p.name === bot);
  if (!remote) return { found: false };
  const vt = st['features/base/tracks'].find(t => !t.local && t.participantId === remote.id && t.mediaType === 'video');
  return { found: true, video: !!vt, muted: vt ? !!vt.muted : null };
}"""

PEER_PIXEL = """async (bot) => {
  const st = APP.store.getState(), parts = st['features/base/participants'];
  const remote = [...parts.remote.values()].find(p => p.name === bot);
  const vt = st['features/base/tracks'].find(t => !t.local && t.participantId === remote.id && t.mediaType === 'video');
  const v = document.createElement('video'); v.muted = true; v.playsInline = true;
  v.srcObject = new MediaStream([vt.jitsiTrack.getTrack()]);
  document.body.appendChild(v); await v.play().catch(() => {});
  for (let i = 0; i < 50 && !(v.videoWidth > 0 && v.currentTime > 0.3); i++) await new Promise(r => setTimeout(r, 200));
  const c = document.createElement('canvas'); c.width = v.videoWidth; c.height = v.videoHeight;
  const g = c.getContext('2d'); g.drawImage(v, 0, 0);
  const px = Array.from(g.getImageData(c.width >> 1, c.height >> 1, 1, 1).data.slice(0, 3));
  return { w: v.videoWidth, h: v.videoHeight, px };
}"""


async def main():
    from playwright.async_api import async_playwright
    card = os.environ.get("NO_IMAGE") == "1"        # sans image choisie : carte violette avec le nom de l'assistant
    img = None if card else base64.b64encode(open(os.environ["CAM_JPEG"], "rb").read()).decode()
    st = call("GET", "/status")
    check(st["state"] == "idle", f"assistant d'essai libre ({st['state']})")
    call("POST", "/meeting/join", {"url": ROOM, "platform": "web", "record": False, "transcribe": False,
                                   "display_name": BOT, "duration_min": 15, "camera_image": img})
    print("  salle :", ROOM, flush=True)
    for _ in range(60):
        st = call("GET", "/status")
        if st["state"] in ("in_meeting", "error"):
            break
        time.sleep(2)
    check(st["state"] == "in_meeting", f"assistant dans la réunion ({st['state']} {st.get('error') or st.get('note') or ''})")
    async with async_playwright() as pw:
        br = await pw.chromium.launch(channel="chrome", headless=True, args=[
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"])
        ctx = await br.new_context(viewport={"width": 1280, "height": 800}, permissions=["microphone", "camera"])
        pg = await ctx.new_page()
        await pg.goto(ROOM + '#userInfo.displayName="Témoin"&config.prejoinConfig.enabled=false'
                      '&config.startWithAudioMuted=true&config.startWithVideoMuted=true', wait_until="domcontentloaded")
        await pg.wait_for_function("() => window.APP && APP.conference && APP.conference.isJoined && APP.conference.isJoined()", timeout=60000)
        s = {}
        for _ in range(40):
            s = await pg.evaluate(PEER_STATE, BOT)
            if s.get("found"):
                break
            await asyncio.sleep(1)
        check(s.get("found"), "le témoin voit l'assistant dans la réunion")
        await asyncio.sleep(12)          # la caméra « image » devient disponible (≤ 5 s après l'entrée confirmée)
        s = await pg.evaluate(PEER_STATE, BOT)
        check(not s.get("video") or s.get("muted"), f"caméra de l'assistant COUPÉE à l'entrée et ensuite ({s})")
        shot = call("GET", "/status")
        check(shot["state"] == "in_meeting", "assistant toujours dans la réunion")
        print("  « Prendre la main » : touche V (caméra de Jitsi) par l'écran distant", flush=True)
        rfb_key(0x76)
        for _ in range(30):
            s = await pg.evaluate(PEER_STATE, BOT)
            if s.get("video") and not s.get("muted"):
                break
            await asyncio.sleep(1)
        check(s.get("video") and not s.get("muted"), f"caméra allumée à la main : le témoin reçoit la vidéo ({s})")
        if s.get("video") and not s.get("muted"):
            p = await pg.evaluate(PEER_PIXEL, BOT)
            r, g, b = p["px"]
            if card:
                check(b > 180 and r < 170 and g < 120, f"sans image : la carte violette avec le nom ({p})")
            else:
                check(r > 200 and 90 < g < 180 and b < 80, f"c'est l'image d'essai : pixel central orange ({p})")
            check(p["w"] > 0 and abs(p["w"] / max(1, p["h"]) - 16 / 9) < 0.05, f"format 16/9 ({p['w']}×{p['h']})")
        await pg.screenshot(path=f"{OUT}/cam-e2e-peer.png")
        rfb_key(0x76)                    # éteinte de nouveau
        for _ in range(20):
            s = await pg.evaluate(PEER_STATE, BOT)
            if not s.get("video") or s.get("muted"):
                break
            await asyncio.sleep(1)
        check(not s.get("video") or s.get("muted"), f"éteinte de nouveau à la main ({s})")
        await br.close()
    call("POST", "/meeting/leave")
    time.sleep(3)
    check(call("GET", "/status")["state"] == "idle", "assistant sorti, libre")
    print(f"\n{ok} vérifications réussies, {fails} échecs")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    asyncio.run(main())
