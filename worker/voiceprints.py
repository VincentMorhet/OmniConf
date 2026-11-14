"""Empreintes vocales de référence : seulement pour les personnes dont le consentement est attesté.

Le registre des consentements est tenu par le contrôle (Réglages > Empreintes vocales). Le contrôle demande à chaque
assistant de chercher, sur ses propres enregistrements, les passages sûrs des personnes cochées (`extract`) : ils
sont regroupés en « voix » (une par réunion et par origine), chacune avec quelques extraits à écouter. Une
personne habilitée écoute et valide (ou refuse) chaque voix ; l'empreinte est la moyenne des seules voix validées.
Le contrôle renvoie ensuite à chaque assistant l'ensemble des empreintes (`save`). Décocher une personne : son
empreinte et ses voix sont effacées du contrôle et, au plus tard 2 minutes après, de chaque assistant.

Passages sûrs d'une personne (2,5 s au moins) :
- voix nommée d'après la vignette de la personne ET vignette de cette personne signalée pendant la phrase ;
- voix nommée à la main avec ce nom ;
- canal « Moi » (micro de l'utilisateur de l'assistant) des réunions lancées par le compte de cette personne.
Seules des empreintes (vecteurs de 192 nombres) et des positions dans les vidéos sortent de l'assistant ; le son
des extraits n'est lu qu'à la demande (écoute dans l'IHM), jamais conservé ailleurs.

Reconnaissance (`match`, appelée par la ré-identification de fin de réunion) : le centre de chaque groupe de
voix est comparé aux empreintes ; un groupe très proche d'une empreinte (et nettement plus que des autres)
prend ce nom. Ce nom l'emporte sur la vignette (cas d'une personne qui parle depuis le poste d'une autre),
mais pas sur un nom donné à la main.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
from pathlib import Path

import numpy as np

import lexicon

log = logging.getLogger("voiceprints")

PATH = Path(os.environ.get("VOICEPRINTS_FILE", "/srv/webconf/voiceprints.json"))
MIN_SEC = 2.5             # phrases plus courtes : empreinte peu fiable
MAX_PER_MEETING = 80      # phrases (les plus longues) par personne et par réunion
MIN_PRINT_SEC = 60.0      # empreinte utilisée pour reconnaître à partir d'une minute de voix
# Calibrage (11 réunions, 82 voix, TitaNet large, empreintes brutes) : même personne d'une réunion à l'autre
# 0,88 à 0,96 (médiane 0,905 ; canal « Moi » -> voix captée par la réunion : 0,90) ; personnes différentes
# nommées : 0,76 au plus (médiane 0,31). Seuil 0,82 : aucune confusion observée.
MATCH = float(os.environ.get("VOICEPRINT_MATCH", "0.82"))     # similarité (cosinus, empreintes brutes) minimale
MARGIN = float(os.environ.get("VOICEPRINT_MARGIN", "0.10"))   # avance minimale sur la 2e empreinte la plus proche
MIN_PHRASES = 3           # phrases longues d'un groupe pour calculer son centre
PHRASE_MIN = 0.50         # phrase gardée pour l'empreinte si assez proche des phrases sûres de la personne
MIN_GROUP_SEC = 8.0       # groupe trop court : centre peu fiable, pas de reconnaissance
CLIP_MAX = 12.0           # extrait à écouter : 12 s au plus
NEG_MIN = 0.50            # phrase retirée si elle ressemble à un extrait refusé (≥ 0,50) plus qu'à la personne
KINDS_FR = {"moi": "micro de l'assistant (canal « Moi »)", "nommée": "voix nommée à la main", "vignette": "vignette"}
_DUP = re.compile(r"\s\(\d+\)$")


def key(label: str | None) -> str:
    """« CAMILLE DURAND (Société / Ville) (2) » -> « camille durand » (comparaison des noms)."""
    if not label:
        return ""
    s = _DUP.sub("", label.strip())
    m = lexicon.ORG.match(s)
    if m and not re.search(r"(…|\.\.\.)\s*$", m.group("name")) and not re.fullmatch(r"\d+", (m.group("org") or m.group("org2") or "")):
        s = m.group("name").strip()
    lex = lexicon.load()
    return lexicon.norm(lex.canonical(s) or s)


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    return v / (np.linalg.norm(v) + 1e-9)


# ----------------------------------------------------------------------------- stockage (poussé par le contrôle)
def load() -> dict:
    """{nom: {"v": [...], "sec": s, "n": n, ...}} (personnes consentantes uniquement)."""
    try:
        d = json.loads(PATH.read_text())
        return d.get("prints") or {}
    except (OSError, ValueError):
        return {}


def version(prints: dict | None = None) -> str | None:
    prints = load() if prints is None else prints
    if not prints:
        return None
    return hashlib.sha1(json.dumps(prints, sort_keys=True).encode()).hexdigest()[:12]


def save(prints: dict) -> str | None:
    clean = {}
    for name, p in (prints or {}).items():
        v = p.get("v") or []
        if len(v) >= 64 and all(isinstance(x, (int, float)) for x in v):
            clean[str(name)[:80]] = {"v": [round(float(x), 6) for x in v], "sec": float(p.get("sec") or 0),
                                     "n": int(p.get("n") or 0), "meetings": int(p.get("meetings") or 0),
                                     "built_at": p.get("built_at")}
    if not clean:
        PATH.unlink(missing_ok=True)      # plus aucune personne consentante : rien ne reste sur l'assistant
        return None
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"prints": clean, "saved_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}))
    os.chmod(tmp, 0o600)
    os.replace(tmp, PATH)
    return version(clean)


# ----------------------------------------------------------------------------- calcul sur les enregistrements
def samples(doc: dict, targets: dict[str, str], owner_name: str | None) -> dict[str, list[tuple]]:
    """Phrases sûres de chaque personne visée dans une transcription : {nom: [(début, fin, nature, texte, voix)]}.
    `targets` : {clé du nom: nom}."""
    sp = doc.get("speakers") or {}
    owner_k = key(owner_name)
    out: dict[str, list] = {}
    for e in doc.get("raw") or []:
        if not e.get("text") or not e.get("spk"):
            continue
        a, b = float(e["t"]), float(e.get("te") or e["t"])
        if b - a < MIN_SEC:
            continue
        who = kind = None
        s = sp.get(e["spk"]) or {}
        if e["spk"] == "ME":
            if owner_k in targets:
                who, kind = targets[owner_k], "moi"
        else:
            if s.get("source") == "manual":
                k = key(s.get("name") or s.get("label"))
                if k in targets:
                    who, kind = targets[k], "nommée"
            elif s.get("source") == "ui" and not s.get("no_tile"):
                k = key(s.get("label"))
                if k in targets and key(e.get("ui")) == k:
                    who, kind = targets[k], "vignette"
        if who:
            out.setdefault(who, []).append((a, b, kind, (e.get("text") or "").strip(),
                                            "Moi" if e["spk"] == "ME" else (s.get("label") or e["spk"])))
    for who in out:
        out[who] = sorted(out[who], key=lambda x: x[0] - x[1])[:MAX_PER_MEETING]
    return out


def _clips(rows: list[dict]) -> list[dict]:
    """Extraits à écouter d'une voix : 3 passages répartis dans la réunion (les plus ressemblants de chaque tiers,
    4 à 12 s de préférence) et le passage gardé le moins ressemblant (pour repérer une voix mélangée). Chaque
    extrait garde son empreinte (« v ») : un extrait refusé à l'écoute est retiré de l'empreinte sans attendre.
    rows : [{"sim", "a", "b", "text", "u", "d"}]."""
    rows = sorted(rows, key=lambda r: r["a"])
    out, used = [], set()
    third = max(1, -(-len(rows) // 3))
    for i in range(0, len(rows), third):
        part = rows[i:i + third]
        good = [r for r in part if 4.0 <= r["b"] - r["a"]] or part
        r = max(good, key=lambda r: (r["sim"], r["b"] - r["a"]))
        used.add(r["a"])
        out.append(r)
    odd = min(rows, key=lambda r: r["sim"])
    if odd["a"] not in used and odd["sim"] < 0.75:
        out.append({**odd, "odd": True})
    return [{"t": round(r["a"], 2), "te": round(min(r["b"], r["a"] + CLIP_MAX), 2), "text": r["text"][:200],
             "sim": round(r["sim"], 2), "d": round(r["d"], 2), "v": [round(float(x), 5) for x in r["u"]],
             **({"odd": True} if r.get("odd") else {})} for r in out]


def _title(rec_dir: Path, stem: str, meta: dict) -> str | None:
    """Titre du compte rendu s'il existe (le titre de la réunion n'est souvent que la plateforme)."""
    try:
        rr = json.loads((rec_dir / f"{stem}.report.json").read_text())
        rr = rr.get("report") or rr
        t = ((rr.get("cr") or {}).get("titre") or rr.get("titre") or "").strip()
        if t:
            return t[:120]
    except (OSError, ValueError, AttributeError):
        pass
    return meta.get("title")


def extract(rec_dir: Path, names: list[str], owners: dict[str, str] | None = None,
            exclude: dict[str, list[str]] | None = None, refs: dict[str, list[float]] | None = None,
            negatives: dict[str, list] | None = None) -> dict:
    """Voix candidates de chaque personne sur les enregistrements de l'assistant, une par réunion et par origine
    (le contrôle les fait valider, puis fait la moyenne des voix validées) :
    {nom: {"<réunion>|<origine>": {"meeting", "title", "kind", "label", "sum": [...], "n", "sec", "dropped",
    "dropped_neg", "clips": [{"t", "te", "text", "sim", "d", "v", "odd"?}]}}}.
    `exclude` : {nom: [voix refusées]} (écartées de la référence) ; `refs` : {nom: empreinte validée} ;
    `negatives` : {nom: [[réunion, début]]} extraits refusés à l'écoute : ces phrases, et toutes celles qui leur
    ressemblent plus qu'à la personne, sont retirées de ses voix."""
    import reid
    targets = {key(n): n for n in names if key(n)}
    owners, exclude, refs, negatives = owners or {}, exclude or {}, refs or {}, negatives or {}
    found: dict[str, list] = {}       # nom -> [(empreinte, nature, réunion, durée, début, fin, texte)]
    info: dict[str, dict] = {}        # réunion|nature -> titre, nom de la voix
    ex = None
    for t in sorted(rec_dir.glob("*.transcript.json")):
        stem = t.name[: -len(".transcript.json")]
        media = rec_dir / (stem + ".mp4")
        if not media.exists():
            continue
        try:
            doc = json.loads(t.read_text())
        except ValueError:
            continue
        meta = doc.get("meta") or {}
        owner_name = owners.get(meta.get("owner") or "") or doc.get("me_name") or meta.get("me_name")
        todo = samples(doc, targets, owner_name)
        if not todo:
            continue
        ex = ex or reid._extractor()
        pcm = reid.load_pcm(media)
        for who, spans in todo.items():
            embs = reid.embed_all(pcm, [(a, b) for a, b, *_ in spans], ex)
            for (a, b, kind, text, label), v in zip(spans, embs):
                if v is None:
                    continue
                found.setdefault(who, []).append((_unit(v), kind, stem, min(b - a, reid.MAX_SEG_SEC), a, b, text))
                g = info.setdefault(f"{stem}|{kind}", {"meeting": stem, "title": _title(rec_dir, stem, meta),
                                                         "kind": kind, "labels": {}})
                g["labels"][label] = g["labels"].get(label, 0) + 1
        log.info("empreintes : %s -> %s", stem, {w: len(s) for w, s in todo.items()})
    out: dict[str, dict] = {}
    for who, L in found.items():
        # phrases parasites écartées (voix nommée à la main qui mêlait plusieurs personnes, écho) : comparées à
        # l'empreinte validée si elle existe, sinon à une référence provisoire faite des phrases les plus sûres
        # (micro « Moi », vignette) hors voix refusées. Calibrage : au seuil 0,50, 96 % des phrases de la personne
        # gardées, 2 % de celles des autres
        no = set(exclude.get(who) or [])
        negs = [(str(x[0]), float(x[1])) for x in (negatives.get(who) or []) if isinstance(x, (list, tuple)) and len(x) == 2]
        is_neg = [any(stem == ns and abs(a - nt) < 0.05 for ns, nt in negs) for _, _, stem, _, a, _, _ in L]
        N = np.array([x[0] for x, n in zip(L, is_neg) if n]) if any(is_neg) else None
        if refs.get(who) and len(refs[who]) >= 64:
            ref = _unit(refs[who])
        else:
            pool = [x for x, n in zip(L, is_neg) if f"{x[2]}|{x[1]}" not in no and not n]
            sure = [x[0] for x in pool if x[1] in ("moi", "vignette")]
            ref = _unit(np.mean(sure if len(sure) >= 10 else [x[0] for x in pool] or [x[0] for x in L], axis=0))
        groups: dict[str, dict] = {}
        for u, kind, stem, d, a, b, text in L:
            gid = f"{stem}|{kind}"
            g = groups.setdefault(gid, {"keep": [], "dropped": 0, "dropped_neg": 0})
            sim = float(u @ ref)
            near_neg = float(np.max(N @ u)) if N is not None else -1.0
            if near_neg >= NEG_MIN and near_neg > sim:     # semblable à un extrait refusé plus qu'à la personne
                g["dropped_neg"] += 1
            elif sim >= PHRASE_MIN:
                g["keep"].append({"u": u, "d": d, "sim": sim, "a": a, "b": b, "text": text})
            else:
                g["dropped"] += 1
        res = {}
        for gid, g in groups.items():
            if not g["keep"]:
                continue
            i = info[gid]
            res[gid] = {"meeting": i["meeting"], "title": i["title"], "kind": i["kind"],
                        "label": max(i["labels"], key=i["labels"].get),
                        "sum": [round(float(x), 6) for x in np.sum([k["u"] for k in g["keep"]], axis=0)],
                        "n": len(g["keep"]), "sec": round(sum(k["d"] for k in g["keep"]), 1), "dropped": g["dropped"],
                        "dropped_neg": g["dropped_neg"], "clips": _clips(g["keep"])}
        if res:
            out[who] = res
    return out


# ----------------------------------------------------------------------------- reconnaissance
def usable(prints: dict | None = None) -> dict[str, np.ndarray]:
    prints = load() if prints is None else prints
    return {n: _unit(p["v"]) for n, p in prints.items() if p.get("v") and float(p.get("sec") or 0) >= MIN_PRINT_SEC}


def match(centroids: dict, secs: dict, prints: dict[str, np.ndarray]) -> dict:
    """centroids : {groupe: centre (empreintes brutes normalisées)}, secs : {groupe: durée}.
    Renvoie {groupe: (nom, similarité, écart avec la 2e empreinte)} pour les groupes reconnus."""
    out = {}
    if not prints:
        return out
    names = list(prints)
    P = np.array([prints[n] for n in names])
    for g, c in centroids.items():
        if secs.get(g, 0) < MIN_GROUP_SEC:
            continue
        s = P @ _unit(c)
        order = np.argsort(-s)
        best = float(s[order[0]])
        second = float(s[order[1]]) if len(order) > 1 else -1.0
        if best >= MATCH and best - second >= MARGIN:
            out[g] = (names[order[0]], round(best, 3), round(best - second, 3))
    return out
