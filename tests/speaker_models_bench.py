"""Banc d'essai (python tests/speaker_models_bench.py reunions.json modele1.onnx modele2.onnx …)

 des modèles d'empreinte vocale sur de vraies réunions (vérité : nom affiché par la plateforme,
phrases ≥ 3 s, postes partagés écartés). Mesures : AUC (mêmes / autres personnes) et regroupement (B-cubed F1)."""
import json, os, subprocess, sys, time
import numpy as np
import sherpa_onnx

# fichier JSON : {"<réunion>": ["<nom affiché d'un poste partagé à écarter>", …], …} ; dossier D : <réunion>.mp4 et .transcript.json
MEET = json.load(open(sys.argv[1]))
MODELS = sys.argv[2:]
D = os.environ.get("BENCH_DIR", "/tmp/bench")
segs = {}
for m, excl in MEET.items():
    d = json.load(open(f"{D}/{m}.transcript.json"))
    pcm = np.frombuffer(subprocess.run(["ffmpeg", "-loglevel", "error", "-i", f"{D}/{m}.mp4", "-vn", "-ac", "1", "-ar", "16000",
                                        "-f", "s16le", "-"], capture_output=True).stdout, np.int16).astype(np.float32) / 32768
    per, out = {}, []
    for e in d["raw"]:
        if e.get("text") and e.get("spk") != "ME" and e.get("ui") and e["ui"] not in excl and e["te"] - e["t"] >= 3:
            if per.get(e["ui"], 0) >= 40:
                continue
            per[e["ui"]] = per.get(e["ui"], 0) + 1
            a, b = int(e["t"] * 16000), int(min(e["te"], e["t"] + 10) * 16000)
            if len(pcm[a:b]) < 2.5 * 16000:          # hors de la bande son (ou trop court) : écarté
                continue
            out.append((e["ui"], pcm[a:b]))
    segs[m] = [x for x in out if sum(1 for y in out if y[0] == x[0]) >= 3]   # au moins 3 phrases par personne
    print(m, len(segs[m]), "phrases,", len({x[0] for x in segs[m]}), "personnes", flush=True)


def bcubed(labels, clusters):
    n = len(labels); p = r = 0.0
    for i in range(n):
        same_c = [j for j in range(n) if clusters[j] == clusters[i]]
        same_l = [j for j in range(n) if labels[j] == labels[i]]
        both = sum(1 for j in same_c if labels[j] == labels[i])
        p += both / len(same_c); r += both / len(same_l)
    p, r = p / n, r / n
    return 2 * p * r / (p + r)


def agglo(S, T):
    groups = [[i] for i in range(len(S))]
    while len(groups) > 1:
        best = None
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                s = S[np.ix_(groups[i], groups[j])].mean()
                if s >= T and (best is None or s > best[0]):
                    best = (s, i, j)
        if not best:
            break
        _, i, j = best
        groups[i] += groups.pop(j)
    c = [0] * len(S)
    for g, idx in enumerate(groups):
        for k in idx:
            c[k] = g
    return c


# empreintes par modèle (mises en cache), puis combinaisons (concaténation des empreintes centrées)
cache = {}
for model in MODELS:
    ex = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=f"/tmp/models/{model}", num_threads=4))
    for m, ss in segs.items():
        E = []
        for _, x in ss:
            st = ex.create_stream(); st.accept_waveform(16000, x); st.input_finished()
            E.append(np.array(ex.compute(st)))
        E = np.array(E); E = E - E.mean(axis=0); E /= np.linalg.norm(E, axis=1, keepdims=True)
        cache[(model, m)] = E
    print("empreintes :", model, flush=True)
short = {m: m.split("_speech_")[-1].split("_sv")[0] if "speech" in m else m.split("_")[2] for m in MODELS}
combos = [[m] for m in MODELS] + [MODELS[:2], MODELS[:3], [MODELS[0], MODELS[2]]]
for combo in combos:
    aucs, f1s = [], {}
    for m, ss in segs.items():
        E = np.concatenate([cache[(k, m)] for k in combo], axis=1) / np.sqrt(len(combo))
        S = E @ E.T
        lab = [l for l, _ in ss]
        same = np.array([S[i, j] for i in range(len(ss)) for j in range(i + 1, len(ss)) if lab[i] == lab[j]])
        diff = np.array([S[i, j] for i in range(len(ss)) for j in range(i + 1, len(ss)) if lab[i] != lab[j]])
        aucs.append(float((same[:, None] > diff[None, :]).mean()))
        for T in np.arange(-0.05, 0.4, 0.05):
            f1s.setdefault(round(T, 2), []).append(bcubed(lab, agglo(S, T)))
    curve = " ".join(f"{t:+.2f}:{np.mean(v):.3f}" for t, v in sorted(f1s.items()))
    bestT = max(f1s, key=lambda t: np.mean(f1s[t]))
    print(f"{'+'.join(short[k] for k in combo):40} AUC {np.mean(aucs):.3f} | F1 {np.mean(f1s[bestT]):.3f} à {bestT} "
          f"({' '.join(f'{x:.2f}' for x in f1s[bestT])}) | courbe {curve}", flush=True)
