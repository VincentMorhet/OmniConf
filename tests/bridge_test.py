"""Test du pont audio : envoie une sinusoïde 440 Hz comme 'micro' et vérifie
(1) qu'elle arrive dans user_in.monitor (donc dans le micro virtuel de Chrome)
(2) qu'un son joué dans meeting_out revient bien au client WebRTC."""
import asyncio, fractions, json, os, subprocess, sys, urllib.request
import av, numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack

TOKEN = os.environ["AGENT_TOKEN"]; URL = os.environ.get("AGENT", "http://127.0.0.1:8001")

class Sine(MediaStreamTrack):
    kind = "audio"
    def __init__(self): super().__init__(); self.pts = 0; self.t0 = None
    async def recv(self):
        loop = asyncio.get_event_loop()
        if self.t0 is None: self.t0 = loop.time()
        await asyncio.sleep(max(0, self.t0 + self.pts / 48000 - loop.time()))
        t = (np.arange(960) + self.pts) / 48000
        s = (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16)
        f = av.AudioFrame.from_ndarray(np.repeat(s, 2).reshape(1, -1), format="s16", layout="stereo")
        f.sample_rate = 48000; f.pts = self.pts; f.time_base = fractions.Fraction(1, 48000); self.pts += 960
        return f

async def main():
    pc = RTCPeerConnection()
    pc.addTransceiver(Sine(), direction="sendrecv")
    got = []
    @pc.on("track")
    def on_track(tr):
        async def eat():
            while True:
                fr = await tr.recv(); got.append(np.abs(fr.to_ndarray()).max())
        asyncio.ensure_future(eat())
    await pc.setLocalDescription(await pc.createOffer())
    req = urllib.request.Request(URL + "/webrtc/offer", method="POST",
        data=json.dumps({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}).encode(),
        headers={"X-Agent-Token": TOKEN, "Content-Type": "application/json"})
    ans = json.loads(urllib.request.urlopen(req).read())
    await pc.setRemoteDescription(RTCSessionDescription(ans["sdp"], ans["type"]))
    await asyncio.sleep(3)
    env = {**os.environ, "XDG_RUNTIME_DIR": "/run/user/1500"}
    # (1) le micro arrive-t-il dans user_in ?
    raw = subprocess.run(["timeout", "2", "parec", "-d", "user_in.monitor", "--format=s16le", "--rate=48000",
                          "--channels=2", "--raw"], capture_output=True, env=env).stdout
    mic_peak = int(np.abs(np.frombuffer(raw, dtype=np.int16)).max()) if raw else 0
    # (2) un son joué dans meeting_out revient-il ?
    got.clear()
    tone = subprocess.Popen(["ffmpeg", "-loglevel", "quiet", "-f", "lavfi", "-i", "sine=f=880:d=3", "-f", "pulse", "-device", "meeting_out", "t"], env=env)
    await asyncio.sleep(2.5); tone.wait()
    out_peak = int(max(got)) if got else 0
    print(json.dumps({"ice": pc.iceConnectionState, "mic_peak_in_user_in": mic_peak,
                      "meeting_audio_peak_received": out_peak, "frames": len(got)}))
    await pc.close()
    sys.exit(0 if mic_peak > 3000 and out_peak > 3000 else 1)

if __name__ == "__main__":
    asyncio.run(main())
