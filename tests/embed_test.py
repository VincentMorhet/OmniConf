"""Empreinte de quelques passages d'une réunion (worker/voiceprints.embed_segments), sur un fichier généré.

    python tests/embed_test.py          # environnement d'un assistant (ffmpeg, modèle d'empreinte vocale)
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import voiceprints  # noqa: E402

ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what, flush=True)


d = Path(tempfile.mkdtemp())
mp4 = d / "essai.mp4"
subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x240:r=5:d=30", "-f", "lavfi",
                "-i", "sine=f=220:d=30,volume=0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                str(mp4)], check=True)
v = voiceprints.embed_segments(mp4, [(5.0, 9.0), (12.0, 12.5), (20.0, 40.0)])
check(len(v) == 3 and v[1] is None, f"passage trop court : pas d'empreinte ({[x is None for x in v]})")
check(v[0] is not None and abs(np.linalg.norm(v[0]) - 1) < 1e-3 and len(v[0]) >= 128, f"empreinte normée ({len(v[0] or [])} valeurs)")
v2 = voiceprints.embed_segments(mp4, [(5.0, 9.0)])
check(float(np.dot(v[0], v2[0])) > 0.999, "même passage deux fois : même empreinte")
check(v[2] is not None, "passage qui dépasse la fin du fichier : empreinte sur ce qui existe")
print(f"{ok} vérifications réussies, {fails} échec(s)")
sys.exit(1 if fails else 0)
