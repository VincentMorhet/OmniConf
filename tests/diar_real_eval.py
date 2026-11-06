"""Évaluation de la distinction des voix (worker/speakers.py) sur des empreintes déjà calculées :
rejoue les phrases dans l'ordre réel avec le vrai Diarizer (assign + merge_pass périodique).

Entrée : un fichier .npz produit par diar_sim (clé « data » : {nom: (empreintes, durées, vérité|None, textes|None)}),
calculé avec le même modèle que TRANSCRIPT_SPK_MODEL.
Usage (dans l'image worker) : python diar_real_eval.py cache.npz [NEW MERGE]…
Sortie : nombre de groupes ≥ 3 % du temps de parole, et pour les données de synthèse la pureté
et le regroupement de chaque voix (1,00 = toutes ses phrases dans un même groupe)."""
import collections, importlib, os, sys
import numpy as np

sys.path.insert(0, os.environ.get("WORKER_DIR", "/opt/webconf/worker"))
import speakers as S

data = np.load(sys.argv[1], allow_pickle=True)["data"].item()
pairs = [(float(a), float(b)) for a, b in zip(sys.argv[2::2], sys.argv[3::2])] or [(S.NEW_T, S.MERGE_T)]


def run(E, D):
    z = S.Diarizer.__new__(S.Diarizer)   # sans charger le modèle : les empreintes sont fournies
    z.extractor, z.error, z.bot_names = object(), None, set()
    z.reset()
    ids = []
    for i, (e, d) in enumerate(zip(E, D)):
        ids.append(z.assign(e, d, None))
        if i % 5 == 4:   # la transcription fusionne périodiquement
            z.merge_pass()
    z.merge_pass()
    return [z.resolve(i) for i in ids]


for new_t, merge_t in pairs:
    S.NEW_T, S.MERGE_T = new_t, merge_t
    print(f"NEW={new_t:.2f} MERGE={merge_t:.2f}")
    for name, (E, D, W, T) in data.items():
        lab = run(E, D)
        tot = collections.Counter()
        for l, d in zip(lab, D):
            tot[l] += d
        big = [l for l, t in tot.most_common() if t >= 0.03 * D.sum()]
        line = f"  {name[:15]:15s} {len(big)} groupes ≥3 % : " + " ".join(f"{tot[l] / 60:.0f} min" for l in big[:8])
        if W:
            by = collections.defaultdict(collections.Counter)
            for l, w in zip(lab, W):
                by[l][w] += 1
            pur = sum(c.most_common(1)[0][1] for c in by.values()) / len(W)
            ok = {w: collections.Counter(l for l, x in zip(lab, W) if x == w).most_common(1)[0][1] / W.count(w) for w in set(W)}
            line += f" · pureté {pur:.2f} · " + " ".join(f"{w}={v:.2f}" for w, v in sorted(ok.items()))
        print(line)
