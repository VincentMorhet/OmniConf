"""Whisper refusé (429, 5xx, réseau) : essais espacés, délai « Retry-After » respecté ; erreur 400 : pas d'essai de
plus. Faux service HTTP en mémoire, aucun appel réseau.

    python tests/asr_retry_test.py          # environnement d'un assistant (venv du worker)
"""
import asyncio
import sys
import time
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import transcriber as T  # noqa: E402

ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what, flush=True)


async def run(plan, tries=5, wait_max=0.05):
    calls = []

    def handler(req):
        calls.append(time.monotonic())
        step = plan[min(len(calls), len(plan)) - 1]
        if step == "net":
            raise httpx.ConnectError("coupure", request=req)
        code, headers = step if isinstance(step, tuple) else (step, {})
        if code == 200:
            return httpx.Response(200, json={"text": "bonjour", "segments": []})
        return httpx.Response(code, headers=headers, text="refus")
    tr = T.Transcriber("/tmp")
    tr.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tr.asr_tries, tr.asr_wait_max = tries, wait_max
    t0 = time.monotonic()
    try:
        r = await tr._whisper(np.zeros(16000, dtype=np.int16))
        res = r.get("text")
    except httpx.HTTPError as e:
        res = f"erreur {getattr(getattr(e, 'response', None), 'status_code', 'réseau')}"
    return res, len(calls), round(time.monotonic() - t0, 2), tr.asr_stats.get("throttled", 0)


async def main():
    r = await run([429, 429, 200])
    check(r[0] == "bonjour" and r[1] == 3 and r[3] == 2, f"429 deux fois puis réponse : 3 essais ({r})")
    r = await run([503, 502, 200])
    check(r[0] == "bonjour" and r[1] == 3, f"503 puis 502 puis réponse ({r})")
    r = await run([503], tries=4)
    check(r[0] == "erreur 503" and r[1] == 4, f"503 sans fin : erreur après 4 essais ({r})")
    r = await run([400])
    check(r[0] == "erreur 400" and r[1] == 1, f"400 : pas d'essai de plus ({r})")
    r = await run(["net", 200])
    check(r[0] == "bonjour" and r[1] == 2, f"coupure réseau puis réponse ({r})")
    r = await run([(429, {"retry-after": "1.5"}), 200], wait_max=8)
    check(r[0] == "bonjour" and r[2] >= 1.4, f"« Retry-After: 1.5 » respecté ({r[2]} s)")
    tr = T.Transcriber("/tmp")
    check(tr.asr_tries == 5 and tr.asr_wait_max == 8.0, "en direct : 5 essais, 8 s au plus entre deux (≈ 15 s en tout)")
    print(f"{ok} vérifications réussies, {fails} échec(s)")
    sys.exit(1 if fails else 0)


asyncio.run(main())
