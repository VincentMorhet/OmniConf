# Envoie une sinusoïde en WebRTC ; la page loopback la rejoue -> on doit la recevoir en retour.
import asyncio, json, os, sys, urllib.request, numpy as np
sys.path.insert(0, os.path.dirname(__file__))
from bridge_test import Sine
from aiortc import RTCPeerConnection, RTCSessionDescription
TOKEN = os.environ["AGENT_TOKEN"]; URL = os.environ.get("AGENT", "http://127.0.0.1:8001")
async def main():
    pc = RTCPeerConnection(); pc.addTransceiver(Sine(), direction="sendrecv"); got = []
    @pc.on("track")
    def on_track(tr):
        async def eat():
            try:
                while True: got.append(np.abs((await tr.recv()).to_ndarray()).max())
            except Exception: pass
        asyncio.ensure_future(eat())
    await pc.setLocalDescription(await pc.createOffer())
    req = urllib.request.Request(URL + "/webrtc/offer", method="POST",
        data=json.dumps({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}).encode(),
        headers={"X-Agent-Token": TOKEN, "Content-Type": "application/json"})
    ans = json.loads(urllib.request.urlopen(req).read())
    await pc.setRemoteDescription(RTCSessionDescription(ans["sdp"], ans["type"]))
    await asyncio.sleep(8)
    tail = got[-200:]
    print(json.dumps({"frames": len(got), "peak_last_4s": int(max(tail)) if tail else 0}))
    await pc.close()
asyncio.run(main())
