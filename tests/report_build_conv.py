# Construit un fichier audio par participant (tours de parole placés sur une timeline commune) ; « me » = micro de l IHM
import glob, os, wave, json, numpy as np, sys
src, dst = sys.argv[1], sys.argv[2]
LEAD, GAP, TAIL, R = float(os.environ.get("LEAD", 40)), float(os.environ.get("GAP", 2.5)), 60.0, 16000
turns = sorted(glob.glob(src + "/*.wav"))
who = {"alice": "alice", "bruno": "bruno", "salle1": "salle", "salle2": "salle", "me": "me"}
clips, t, plan = [], LEAD, []
for f in turns:
    w = wave.open(f); x = np.frombuffer(w.readframes(w.getnframes()), np.int16); assert w.getframerate() == R
    name = os.path.basename(f)[:-4].split("_")[1]
    clips.append((who[name], t, x)); plan.append({"t": round(t - LEAD, 1), "who": name, "dur": round(len(x) / R, 1)})
    t += len(x) / R + GAP
total = int((t + TAIL) * R)
os.makedirs(dst, exist_ok=True)
for p in set(who.values()):
    out = np.zeros(total, np.int16)
    for w_, st, x in clips:
        if w_ == p:
            out[int(st * R):int(st * R) + len(x)] = x
    with wave.open(f"{dst}/{p}.wav", "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(R); w.writeframes(out.tobytes())
print(json.dumps(plan))
