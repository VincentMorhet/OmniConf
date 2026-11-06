"""Diarisation hors ligne sur les tours de parole de la réunion fictive (voix de synthèse passées par Opus),
avec ou sans noms affichés simulés. Usage : python diar_offline_ui.py speakers.py dossier_tours [ui]"""
import glob, os, subprocess, sys, wave, importlib.util
from collections import Counter, defaultdict
import numpy as np
spec = importlib.util.spec_from_file_location("speakers", sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
d = m.Diarizer(); use_ui = len(sys.argv) > 3
UI = {"alice": "Alice Martin", "bruno": "Bruno Petit", "salle1": "Salle Nord", "salle2": "Salle Nord"}
def load(f):
    w = wave.open(f); return np.frombuffer(w.readframes(w.getnframes()), np.int16)
seq = []
for f in sorted(glob.glob(sys.argv[2] + "/*.wav")):
    who = os.path.basename(f)[:-4].split("_")[1]
    if who == "me": continue
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", f, "-c:a", "libopus", "-b:a", "24k", "/tmp/x.opus"])
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", "/tmp/x.opus", "-ar", "16000", "-ac", "1", "/tmp/x.wav"])
    x = load("/tmp/x.wav"); n = max(1, round(len(x) / 16000 / 6))  # morceaux de ~6 s comme les phrases Whisper
    for part in np.array_split(x, n):
        seq.append((who, d.assign(d.embed(part), len(part) / 16000, UI[who] if use_ui else None)))
        d.merge_pass()
res = [(w, d.resolve(s)) for w, s in seq]; lab = d.labels(); by = defaultdict(Counter)
for w, s in res: by[w][lab[s]["label"]] += 1
for w, c in by.items(): print(f"{w:8s} -> {dict(c)}")
print("groupes :", len(d.spk))
