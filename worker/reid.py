"""Ré-identification des intervenants en fin de réunion (avant le compte rendu).

Pendant la réunion, les voix sont regroupées au fil de l'eau : une même personne peut être coupée en plusieurs
« voix », et une personne sans vignette à elle (poste partagé, salle) prend le nom de la vignette encadrée à ce
moment-là. Ici, on reprend toute la réunion d'un coup :

1. empreinte vocale de chaque phrase, calculée sur le son de l'enregistrement (signature du canal retirée) ;
2. regroupement global (lien moyen) des phrases assez longues, puis rattachement des phrases courtes ;
3. chaque groupe est nommé d'après les noms affichés par la plateforme pendant ses phrases (les noms donnés
   à la main par l'utilisateur l'emportent) ; un groupe qui a reçu plusieurs noms différents sans majorité
   nette est une voix « sans vignette » (poste partagé ou personne sans vignette visible) : à nommer.

Le résultat remplace les voix de la transcription (sauvegarde *.transcript.avant-reid.json à côté).
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess
from collections import Counter
from pathlib import Path

import numpy as np

log = logging.getLogger("reid")

RATE = 16000
VERSION = 1
# TitaNet large (NVIDIA NeMo) : meilleur modèle du banc d'essai (6 réunions, 29 personnes ; voir README)
MODEL = os.environ.get("REID_MODEL", "/opt/webconf/models/nemo_en_titanet_large.onnx")
THRESHOLD = float(os.environ.get("REID_THRESHOLD", "0.20"))   # lien moyen, empreintes centrées (réglé sur 12/11 et 10/11)
MIN_GROUP_SEC = float(os.environ.get("REID_MIN_GROUP_SEC", "20"))  # groupe plus court : rattaché au plus proche
LONG_SEC = float(os.environ.get("REID_LONG_SEC", "2.5"))       # phrases qui forment les groupes
MIN_SEC = 1.2                                                   # en dessous : pas d'empreinte fiable
ASSIGN_MIN = float(os.environ.get("REID_ASSIGN_MIN", "0.15"))  # phrase courte : rattachée au groupe le plus proche
NAME_SHARE = 0.6           # part des noms affichés pour nommer un groupe
SPREAD_SHARE = 0.2         # « sans vignette » : au moins deux noms à ≥ 20 % chacun, aucun à ≥ 60 %
MAX_SEG_SEC = 12.0
ENABLED = os.environ.get("REID", "1") == "1"


def load_pcm(media: Path) -> np.ndarray:
    out = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(media), "-vn", "-ac", "1", "-ar", str(RATE),
                          "-f", "s16le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, np.int16).astype(np.float32) / 32768


def _extractor():
    import sherpa_onnx
    return sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=MODEL, num_threads=int(os.environ.get("REID_THREADS", "2"))))


def embed_all(pcm: np.ndarray, spans: list[tuple[float, float]], ex=None) -> list[np.ndarray | None]:
    ex = ex or _extractor()
    out = []
    for a, b in spans:
        if b - a < MIN_SEC:
            out.append(None)
            continue
        x = pcm[int(a * RATE):int(min(b, a + MAX_SEG_SEC) * RATE)]
        if len(x) < MIN_SEC * RATE:
            out.append(None)
            continue
        st = ex.create_stream()
        st.accept_waveform(RATE, x)
        st.input_finished()
        out.append(np.array(ex.compute(st), dtype=np.float32))
    return out


def average_linkage(S: np.ndarray, threshold: float) -> list[int]:
    """Regroupement hiérarchique (lien moyen) sur une matrice de similarités ; arrêt sous le seuil."""
    n = len(S)
    M = S.astype(np.float64).copy()
    np.fill_diagonal(M, -np.inf)
    size = np.ones(n)
    alive = np.ones(n, bool)
    members = {i: [i] for i in range(n)}
    while True:
        k = int(np.argmax(M))
        i, j = divmod(k, n)
        if M[i, j] < threshold:
            break
        # fusion de j dans i (Lance-Williams, lien moyen)
        new = (size[i] * M[i] + size[j] * M[j]) / (size[i] + size[j])
        M[i, :] = new
        M[:, i] = new
        M[i, i] = -np.inf
        M[j, :] = -np.inf
        M[:, j] = -np.inf
        size[i] += size[j]
        alive[j] = False
        members[i] += members.pop(j)
    labels = [0] * n
    for g, (_, idx) in enumerate(sorted(members.items(), key=lambda kv: min(kv[1]))):
        for k in idx:
            labels[k] = g
    return labels


def reidentify(doc: dict, media: Path, pcm: np.ndarray | None = None, ex=None, threshold: float | None = None,
               cache: dict | None = None, prints: dict | None = None) -> dict:
    """Recalcule les voix de la transcription `doc` (modifié en place). Renvoie un résumé.
    `cache` (facultatif) garde les empreintes d'un appel à l'autre (essais de seuils).
    `prints` : empreintes de référence {nom: vecteur} (par défaut celles des personnes consentantes, voiceprints.py)."""
    import voiceprints
    threshold = THRESHOLD if threshold is None else threshold
    prints = voiceprints.usable() if prints is None else prints
    raw = doc.get("raw") or []
    old_spk = doc.get("speakers") or {}
    manual = {k: v.get("name") for k, v in old_spk.items() if v.get("source") == "manual" and v.get("name")}
    idx = [i for i, e in enumerate(raw) if e.get("text") and e.get("spk") and e["spk"] != "ME"]
    if len(idx) < 10:
        return {"skipped": "trop peu de phrases"}
    spans = [(float(raw[i]["t"]), float(raw[i].get("te") or raw[i]["t"])) for i in idx]
    if cache is not None and "embs" in cache:
        embs = cache["embs"]
    else:
        pcm = load_pcm(media) if pcm is None else pcm
        embs = embed_all(pcm, spans, ex)
        if cache is not None:
            cache["embs"] = embs
    ok = [k for k, e in enumerate(embs) if e is not None]
    if len(ok) < 5:
        return {"skipped": "empreintes indisponibles"}
    E = np.array([embs[k] for k in ok])
    mu = E.mean(axis=0)                                   # signature du canal (moyenne de la séance)
    Ec = E - mu
    Ec /= np.linalg.norm(Ec, axis=1, keepdims=True) + 1e-9
    emb_c = {k: Ec[n] for n, k in enumerate(ok)}
    dur = {k: spans[k][1] - spans[k][0] for k in range(len(idx))}

    # 1. groupes formés par les phrases longues
    longs = [k for k in ok if dur[k] >= LONG_SEC]
    if len(longs) < 3:
        return {"skipped": "trop peu de phrases longues"}
    L = np.array([emb_c[k] for k in longs])
    lab_long = average_linkage(L @ L.T, threshold)
    groups: dict[int, list[int]] = {}
    for k, g in zip(longs, lab_long):
        groups.setdefault(g, []).append(k)
    norm = lambda v: v / (np.linalg.norm(v) + 1e-9)
    # micro-groupes (quelques phrases) : dissous, leurs phrases rejoignent le groupe le plus proche
    big = {g: ks for g, ks in groups.items() if sum(dur[k] for k in ks) >= MIN_GROUP_SEC} or groups
    cen = {g: norm(np.mean([emb_c[k] for k in ks], axis=0)) for g, ks in big.items()}
    for g, ks in groups.items():
        if g not in big:
            for k in ks:
                big[max(cen, key=lambda h: float(emb_c[k] @ cen[h]))].append(k)
    groups = big
    cen = {g: norm(np.mean([emb_c[k] for k in ks], axis=0)) for g, ks in groups.items()}

    # 2. phrases courtes : groupe le plus proche ; sinon groupe majoritaire de l'ancienne voix ; sinon voisine
    assign = {k: g for g, ks in groups.items() for k in ks}
    uncertain = 0
    by_old: dict[str, Counter] = {}
    for k, g in assign.items():
        by_old.setdefault(raw[idx[k]]["spk"], Counter())[g] += dur[k]
    for k in range(len(idx)):
        if k in assign:
            continue
        if k in emb_c:
            sims = {g: float(emb_c[k] @ c) for g, c in cen.items()}
            g, s = max(sims.items(), key=lambda x: x[1])
            if s >= ASSIGN_MIN:
                assign[k] = g
                continue
        old = raw[idx[k]]["spk"]
        if by_old.get(old):
            assign[k] = by_old[old].most_common(1)[0][0]
        uncertain += 1
    for k in range(len(idx)):          # restantes : comme la phrase précédente
        if k not in assign:
            assign[k] = assign.get(k - 1, next(iter(groups)))

    # 2 bis. empreintes de référence (personnes consentantes) : centre brut de chaque groupe (la signature du
    # canal n'est pas retirée : les empreintes viennent d'autres réunions) ; groupes reconnus sous le même nom
    # fusionnés (une personne coupée en plusieurs groupes)
    recog = {}
    # la personne qui parle au micro de l'assistant (canal « Moi ») n'est pas aussi dans le son de la réunion :
    # une voix proche de la sienne y est un écho ou une autre personne
    me_sec = sum(float(e.get("te") or e["t"]) - float(e["t"]) for e in raw if e.get("spk") == "ME" and e.get("text"))
    me_key = voiceprints.key((doc.get("meta") or {}).get("me_name")) if me_sec >= 30 else ""
    prints = {n: v for n, v in (prints or {}).items() if not me_key or voiceprints.key(n) != me_key}
    if prints:
        unit = {k: embs[k] / (np.linalg.norm(embs[k]) + 1e-9) for k in ok}
        rcen, gsec = {}, Counter()
        for k, g in assign.items():
            gsec[g] += dur[k]
            if k in unit and dur[k] >= LONG_SEC:
                rcen.setdefault(g, []).append(unit[k])
        recog = voiceprints.match({g: np.mean(v, axis=0) for g, v in rcen.items() if len(v) >= voiceprints.MIN_PHRASES},
                                  gsec, prints)
        first_g: dict[str, int] = {}
        for g in sorted(recog, key=lambda g: -gsec[g]):
            nm = recog[g][0]
            if nm not in first_g:
                first_g[nm] = g
                continue
            keep = first_g[nm]
            for k in assign:
                if assign[k] == g:
                    assign[k] = keep
            groups[keep] = groups.get(keep, []) + groups.pop(g, [])
            recog.pop(g)

    # 3. noms : votes des noms affichés (pondérés par la durée) ; noms manuels prioritaires
    votes: dict[int, Counter] = {g: Counter() for g in groups}
    man: dict[int, Counter] = {g: Counter() for g in groups}
    man_dur: dict[str, Counter] = {}
    gtot = Counter()
    for k, g in assign.items():
        e = raw[idx[k]]
        votes.setdefault(g, Counter())
        gtot[g] += dur[k]
        if e.get("ui"):
            votes[g][e["ui"]] += dur[k]
        if e["spk"] in manual:
            man_dur.setdefault(e["spk"], Counter())[g] += dur[k]
    # une voix renommée à la main pendant la réunion peut mêler plusieurs personnes (poste partagé) : son nom va
    # au seul groupe qui contient l'essentiel de ses phrases (et dont elle forme la majorité), pas aux autres
    for o, c in man_dur.items():
        g, w = c.most_common(1)[0]
        if w >= 0.5 * gtot[g]:
            man.setdefault(g, Counter())[manual[o]] += w
    # nouvelles voix S1…, dans l'ordre d'apparition
    first = {}
    for k in sorted(assign, key=lambda k: spans[k][0]):
        first.setdefault(assign[k], spans[k][0])
    order = sorted(first, key=first.get)
    new_id = {g: f"S{n}" for n, g in enumerate(order, 1)}
    clusters, diar = [], {}
    for n, g in enumerate(order, 1):
        v, total = votes.get(g, Counter()), sum(votes.get(g, Counter()).values())
        name = man[g].most_common(1)[0][0] if man.get(g) else None
        top = v.most_common(3)
        spread = (total > 0 and top and top[0][1] / total < NAME_SHARE
                  and sum(1 for _, w in top if w / total >= SPREAD_SHARE) >= 2)
        pr = recog.get(g) if not name else None
        diar[new_id[g]] = {"n": sum(1 for k in assign if assign[k] == g), "votes": {a: round(w, 1) for a, w in v.items()},
                           "name": name, "num": n, "no_tile": bool(spread and not name and not pr),
                           **({"print": {"name": pr[0], "sim": pr[1], "margin": pr[2]}} if pr else {})}
        clusters.append({"id": new_id[g], "secs": round(sum(dur[k] for k in assign if assign[k] == g)),
                         "noms_affiches": [(a, round(w / total, 2)) for a, w in top] if total else [],
                         "nom_manuel": name, "sans_vignette": bool(spread and not name and not pr),
                         **({"empreinte": {"nom": pr[0], "similarite": pr[1], "ecart": pr[2]}} if pr else {})})

    # 4. réécriture : phrases, paragraphes (voix majoritaire de leurs phrases), état des voix
    mapping = {idx[k]: new_id[assign[k]] for k in assign}
    paras = doc.get("paragraphs") or []
    old_of_line = {i: raw[i]["spk"] for i in mapping}
    for i, sid in mapping.items():
        raw[i]["spk_avant"] = old_of_line[i]
        raw[i]["spk"] = sid
    mixed = 0
    times = [p["t"] for p in paras] + [float("inf")]
    for n, p in enumerate(paras):
        if p.get("spk") == "ME":
            continue
        w = Counter()
        for i in mapping:
            e = raw[i]
            if times[n] - 0.5 <= e["t"] < times[n + 1] - 0.5 and old_of_line[i] == p.get("spk"):
                w[e["spk"]] += len((e.get("text") or "").split())
        if w:
            sid, top = w.most_common(1)[0]
            if top < 0.7 * sum(w.values()):
                mixed += 1
            p["spk_avant"] = p.get("spk")
            p["spk"] = sid
    doc["diarizer"] = diar
    me = (doc.get("speakers") or {}).get("ME")
    speakers, seen = {}, Counter()
    for sid, st in diar.items():
        v = Counter(st["votes"]); total = sum(v.values())
        top = v.most_common(1)[0] if v else None
        voted = top[0] if top and top[1] >= 2 and top[1] >= NAME_SHARE * total else None
        auto = voted or (f"Intervenant {st['num']} (sans vignette)" if st["no_tile"] else f"Intervenant {st['num']}")
        pr = (st.get("print") or {}).get("name")
        # vignette d'un autre pendant ses phrases (personne sur le poste d'un autre) : gardée pour la justification
        tile = (voted if voted and voiceprints.key(voted) != voiceprints.key(pr) else None) if pr else None
        if pr:
            auto = pr
        base = st["name"] or auto
        seen[base] += 1
        src = "manual" if st["name"] else "voiceprint" if pr else ("voice" if auto.startswith("Intervenant") else "ui")
        speakers[sid] = {"label": base if seen[base] == 1 else f"{base} ({seen[base]})", "auto": auto, "name": st["name"],
                         "color": st["num"], "source": src,
                         **({"no_tile": True} if st["no_tile"] else {}),
                         **({"print_sim": st["print"]["sim"]} if pr and not st["name"] else {}),
                         **({"tile": tile} if tile and not st["name"] else {})}
    if me:
        speakers["ME"] = me
    doc["speakers"] = speakers
    return {"version": VERSION, "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "model": os.path.basename(MODEL), "threshold": threshold, "phrases": len(idx), "phrases_longues": len(longs),
            "incertaines": uncertain, "paragraphes_mixtes": mixed, "voix_avant": len([k for k in old_spk if k != "ME"]),
            "voix_apres": len(order), "groupes": clusters,
            "empreintes": {"personnes": sorted(prints), "reconnues": {new_id[g]: r[0] for g, r in recog.items() if g in new_id}}}
