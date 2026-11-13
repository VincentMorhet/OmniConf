"""Empreintes vocales de référence : seulement pour les personnes dont le consentement est attesté.

Le registre des consentements est tenu par le contrôle (Réglages > Empreintes vocales). Le contrôle demande à chaque
assistant de calculer, sur ses propres enregistrements, les empreintes des personnes cochées (`extract`), en fait
la moyenne et renvoie à chaque assistant l'ensemble des empreintes (`save`). Décocher une personne : son empreinte
est effacée du contrôle et, au plus tard 2 minutes après, de chaque assistant.

Phrases retenues pour l'empreinte d'une personne (auteur certain, 2,5 s au moins) :
- voix nommée d'après la vignette de la personne ET vignette de cette personne signalée pendant la phrase ;
- voix nommée à la main avec ce nom ;
- canal « Moi » (micro de l'utilisateur de l'assistant) des réunions lancées par le compte de cette personne.
Seules des empreintes (vecteurs de 192 nombres) sortent de l'assistant, jamais le son.

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
def samples(doc: dict, targets: dict[str, str], owner_name: str | None) -> dict[str, list[tuple[float, float, str]]]:
    """Phrases sûres de chaque personne visée dans une transcription : {nom: [(début, fin, nature)]}.
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
        if e["spk"] == "ME":
            if owner_k in targets:
                who, kind = targets[owner_k], "moi"
        else:
            s = sp.get(e["spk"]) or {}
            if s.get("source") == "manual":
                k = key(s.get("name") or s.get("label"))
                if k in targets:
                    who, kind = targets[k], "nommée"
            elif s.get("source") == "ui" and not s.get("no_tile"):
                k = key(s.get("label"))
                if k in targets and key(e.get("ui")) == k:
                    who, kind = targets[k], "vignette"
        if who:
            out.setdefault(who, []).append((a, b, kind))
    for who in out:
        out[who] = sorted(out[who], key=lambda x: x[0] - x[1])[:MAX_PER_MEETING]
    return out


def extract(rec_dir: Path, names: list[str], owners: dict[str, str] | None = None) -> dict:
    """Sommes d'empreintes par personne sur les enregistrements de l'assistant (le contrôle en fait la moyenne).
    {nom: {"sum": [...], "n": phrases, "sec": secondes, "meetings": {réunion: phrases}, "kinds": {nature: phrases}}}"""
    import reid
    targets = {key(n): n for n in names if key(n)}
    owners = owners or {}
    found: dict[str, list] = {}       # nom -> [(empreinte, nature, réunion, durée)]
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
            embs = reid.embed_all(pcm, [(a, b) for a, b, _ in spans], ex)
            found.setdefault(who, []).extend((_unit(v), kind, stem, min(b - a, reid.MAX_SEG_SEC))
                                             for (a, b, kind), v in zip(spans, embs) if v is not None)
        log.info("empreintes : %s -> %s", stem, {w: len(s) for w, s in todo.items()})
    out: dict[str, dict] = {}
    for who, L in found.items():
        # phrases parasites écartées (voix nommée à la main qui mêlait plusieurs personnes, écho) : comparées à
        # une empreinte provisoire faite des phrases les plus sûres (micro « Moi », vignette). Calibrage :
        # au seuil 0,50, 96 % des phrases de la personne gardées, 2 % de celles des autres
        sure = [u for u, kind, _, _ in L if kind in ("moi", "vignette")]
        ref = _unit(np.mean(sure if len(sure) >= 10 else [u for u, *_ in L], axis=0))
        keep = [x for x in L if float(x[0] @ ref) >= PHRASE_MIN]
        if not keep:
            continue
        o = {"sum": [float(v) for v in np.sum([u for u, *_ in keep], axis=0)], "n": len(keep),
             "sec": round(sum(d for *_, d in keep), 1), "meetings": {}, "kinds": {}, "dropped": len(L) - len(keep)}
        for _, kind, stem, _ in keep:
            o["meetings"][stem] = o["meetings"].get(stem, 0) + 1
            o["kinds"][kind] = o["kinds"].get(kind, 0) + 1
        out[who] = o
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
