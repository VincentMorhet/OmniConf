"""Pont audio WebRTC <-> PulseAudio.

  réunion (Chrome) --> sink "meeting_out" --monitor--> parec --> WebRTC --> navigateur de l'utilisateur
  micro utilisateur --> WebRTC --> pacat --> sink "user_in" --monitor--> source "virtmic" --> Chrome
"""
import asyncio
import fractions
import logging
import uuid

import av
import numpy as np
from aiortc import (MediaStreamTrack, RTCConfiguration, RTCIceServer,
                    RTCPeerConnection, RTCSessionDescription)

log = logging.getLogger("audio")

RATE = 48000
CHANNELS = 2
FRAME_SAMPLES = 960  # 20 ms
FRAME_BYTES = FRAME_SAMPLES * CHANNELS * 2


class PulseCaptureTrack(MediaStreamTrack):
    """Piste audio lue depuis une source PulseAudio via parec."""
    kind = "audio"

    def __init__(self, device: str):
        super().__init__()
        self.device = device
        self.proc = None
        self.pts = 0

    async def _ensure(self):
        if self.proc is None:
            self.proc = await asyncio.create_subprocess_exec(
                "parec", "-d", self.device, "--format=s16le", f"--rate={RATE}",
                f"--channels={CHANNELS}", "--latency-msec=20", "--raw",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def recv(self):
        await self._ensure()
        try:
            data = await self.proc.stdout.readexactly(FRAME_BYTES)
        except asyncio.IncompleteReadError:
            self.stop()
            raise
        arr = np.frombuffer(data, dtype=np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(arr, format="s16", layout="stereo")
        frame.sample_rate = RATE
        frame.pts = self.pts
        frame.time_base = fractions.Fraction(1, RATE)
        self.pts += FRAME_SAMPLES
        return frame

    def stop(self):
        super().stop()
        if self.proc and self.proc.returncode is None:
            self.proc.kill()


async def _pump_to_pulse(track: MediaStreamTrack, device: str):
    """Écrit la piste reçue (micro de l'utilisateur) dans un sink PulseAudio."""
    proc = await asyncio.create_subprocess_exec(
        "pacat", "-d", device, "--format=s16le", f"--rate={RATE}",
        f"--channels={CHANNELS}", "--latency-msec=60", "--raw",
        stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    resampler = av.AudioResampler(format="s16", layout="stereo", rate=RATE)
    try:
        while True:
            frame = await track.recv()
            for f in resampler.resample(frame):
                proc.stdin.write(f.to_ndarray().tobytes())
            await proc.stdin.drain()
    except Exception as e:  # MediaStreamError à la fin de la piste
        log.info("fin du flux micro: %r", e)
    finally:
        if proc.returncode is None:
            proc.kill()


class AudioBridge:
    def __init__(self, ice_servers: list[dict], out_device="meeting_out.monitor", in_device="user_in"):
        self.ice = [RTCIceServer(**s) for s in ice_servers]
        self.out_device = out_device
        self.in_device = in_device
        self.peers: dict[str, RTCPeerConnection] = {}

    @property
    def listeners(self) -> int:
        return len(self.peers)

    async def handle_offer(self, sdp: str, type_: str) -> dict:
        pc = RTCPeerConnection(RTCConfiguration(iceServers=self.ice or None))
        pid = uuid.uuid4().hex[:8]
        self.peers[pid] = pc
        out_track = PulseCaptureTrack(self.out_device)
        tasks = []

        @pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                pc.addTrack(out_track)
                tasks.append(asyncio.ensure_future(_pump_to_pulse(track, self.in_device)))

        @pc.on("connectionstatechange")
        async def on_state():
            log.info("peer %s: %s", pid, pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                await self._close(pid, out_track, tasks)

        await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type=type_))
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type, "peer_id": pid}

    async def _close(self, pid, out_track, tasks):
        pc = self.peers.pop(pid, None)
        out_track.stop()
        for t in tasks:
            t.cancel()
        if pc:
            await pc.close()

    async def close_all(self):
        for pc in list(self.peers.values()):
            await pc.close()
        self.peers.clear()
