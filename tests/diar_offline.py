import sys, glob, os, wave, numpy as np, importlib.util
spec = importlib.util.spec_from_file_location("speakers", "/tmp/speakers_new.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
d = m.Diarizer()
def load(f):
    w = wave.open(f); return np.frombuffer(w.readframes(w.getnframes()), np.int16)
# tours de parole passés par Opus (comme dans une visio), deux fois la conversation
import subprocess
turns = sorted(glob.glob("/tmp/conv/*.wav"))
seq = []
for rep in range(2):
    for f in turns:
        subprocess.run(["ffmpeg","-loglevel","error","-y","-i",f,"-c:a","libopus","-b:a","24k","/tmp/x.opus"]); subprocess.run(["ffmpeg","-loglevel","error","-y","-i","/tmp/x.opus","-ar","16000","-ac","1","/tmp/x.wav"])
        x = load("/tmp/x.wav"); who = os.path.basename(f)[:-4].split("_")[1]
        # découpe en 2 morceaux comme Whisper le ferait (phrases plus courtes)
        h = len(x) // 2
        for part in (x[:h], x[h:]):
            seq.append((who, d.assign(d.embed(part), len(part) / 16000, None)))
d.merge_pass()
res = [(w, d.resolve(s)) for w, s in seq]
lab = d.labels()
from collections import Counter, defaultdict
by = defaultdict(Counter)
for w, s in res: by[w][lab[s]["label"]] += 1
for w, c in by.items(): print(f"{w:8s} -> {dict(c)}")
print("groupes :", len(d.spk))
