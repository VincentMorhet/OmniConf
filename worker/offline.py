"""Transcription après coup d'une réunion enregistrée, quand la transcription en direct n'a pas tourné
(par ex. l'assistant n'a pas pu rejoindre seul et l'enregistrement a été lancé à la main).

Reprend la chaîne du direct sur la vidéo : découpage de la parole, Whisper (hallucinations retranscrites),
empreintes vocales, mise en forme par le LLM, diapositives. L'orateur actif est repéré dans l'image (cadre
de couleur de Teams autour de sa vignette) et son nom lu par le modèle d'image : les voix sont nommées comme
en direct. Les participants sont relevés sur les vignettes toutes les minutes.

  docker exec -u bot webconf-wN /opt/webconf/venv/bin/python /opt/webconf/worker/offline.py <nom>.mp4 [--force]

Écrit <nom>.transcript.json et <nom>.md à côté de la vidéo ; le compte rendu se lance ensuite comme
d'habitude (bouton « Régénérer » ou POST /recordings/<nom>/report).
"""
import argparse
import asyncio
import base64
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

sys.path.insert(0, os.environ.get("WORKER_DIR", "/opt/webconf/worker"))
import lexicon  # noqa: E402
import slides as slides_mod  # noqa: E402
import transcriber as T  # noqa: E402
from meetings import clean_name  # noqa: E402

log = logging.getLogger("offline")
REC = Path(os.environ.get("RECORDINGS_DIR", "/srv/webconf/recordings"))
FPS = 2                 # images analysées par seconde de vidéo
ROSTER_EVERY = 60       # secondes entre deux relevés des participants
MAX_NAME_CALLS = 400    # lectures de nom de vignette (modèle d'image) au plus
BATCH = 30              # lignes brutes par appel de mise en forme
VISION = slides_mod.MODEL

NAME_PROMPT = ("Image : vignette d'un participant dans une visioconférence (Microsoft Teams, Zoom…). Donne le nom "
               "affiché sur la vignette (étiquette en bas), exactement tel qu'écrit, avec la mention entre "
               "parenthèses s'il y en a une ; s'il est tronqué, termine-le par « … ». Ignore les initiales de "
               'l\'avatar et les icônes. Réponds uniquement en JSON : {"nom": "..."} ou {"nom": null}.')
ROSTER_PROMPT = ("Image : écran d'une visioconférence. Liste les noms des participants écrits sur leurs vignettes "
                 "(étiquettes de nom des vignettes vidéo ou avatars), de gauche à droite et de haut en bas. Ignore "
                 "le contenu de l'écran partagé, la conversation (chat) et la barre d'outils. Recopie chaque nom "
                 "exactement, avec la mention entre parenthèses s'il y en a une ; s'il est tronqué, termine-le "
                 'par « … ». Réponds uniquement en JSON : {"noms": ["..."]}.')


# ----------------------------------------------------------------------------- image
def _runs(mask: np.ndarray, gap: int = 2) -> list[tuple[int, int]]:
    """Plages [début, fin] des valeurs vraies d'un vecteur, en tolérant des trous de `gap`."""
    idx = np.nonzero(mask)[0]
    out: list[list[int]] = []
    for i in idx:
        if out and i - out[-1][1] <= gap:
            out[-1][1] = int(i)
        else:
            out.append([int(i), int(i)])
    return [(a, b) for a, b in out]


def find_boxes(rgb: np.ndarray, min_edge: int = 30) -> list:
    """Cadres des orateurs actifs (Teams : bordure violette #5B5FC7 de 2 px autour de la vignette ; deux
    vignettes sont encadrées quand deux personnes parlent en même temps).

    Renvoie une liste de (x0, y0, x1, y1) dans l'image analysée."""
    a = rgb.astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    # (à mi-résolution, le sous-échantillonnage des couleurs de la vidéo délave le bord de 2 px : seuils larges)
    m = (b >= 130) & (b - r >= 40) & (b - g >= 40)
    edges = [e for e in _runs(m.sum(0) >= min_edge) if e[1] - e[0] <= 4]   # bords verticaux fins
    found = []
    for i in range(len(edges)):
        for j in range(i + 1, len(edges)):
            x0, x1 = edges[i][0], edges[j][1]
            if not 30 <= x1 - x0 <= rgb.shape[1] * 0.95:
                continue
            col0 = max(_runs(m[:, edges[i][0]:edges[i][1] + 1].any(1)), key=lambda p: p[1] - p[0])
            col1 = max(_runs(m[:, edges[j][0]:edges[j][1] + 1].any(1)), key=lambda p: p[1] - p[0])
            if min(col0[1], col1[1]) - max(col0[0], col1[0]) < min_edge:
                continue
            y0, y1 = min(col0[0], col1[0]), max(col0[1], col1[1])
            # coins arrondis : les bords horizontaux sont un peu au-dessus / au-dessous des bords verticaux
            top = max(m[y, x0:x1 + 1].mean() for y in range(max(0, y0 - 4), y0 + 4))
            bot = max(m[y, x0:x1 + 1].mean() for y in range(max(0, y1 - 3), min(m.shape[0], y1 + 5)))
            inner = m[y0 + 4:y1 - 3, x0 + 4:x1 - 3].mean() if y1 - y0 > 10 and x1 - x0 > 10 else 1
            if top >= 0.7 and bot >= 0.7 and inner < 0.2 and not any(x0 < b[2] and b[0] < x1 for b in found):
                found.append((x0, y0, x1, y1))
    return found


def signature(rgb: np.ndarray, box) -> str:
    """Empreinte du bas de la vignette (là où s'affiche le nom) : même nom -> même empreinte."""
    x0, y0, x1, y1 = box
    h = y1 - y0
    reg = rgb[y0 + int(h * 0.72):y1 - 2, x0 + 3:x1 - 2].mean(axis=2)
    if reg.size == 0:
        return "vide"
    ys = np.linspace(0, reg.shape[0], 7).astype(int)
    xs = np.linspace(0, reg.shape[1], 25).astype(int)
    cells = [reg[ys[i]:max(ys[i] + 1, ys[i + 1]), xs[j]:max(xs[j] + 1, xs[j + 1])].mean()
             for i in range(6) for j in range(24)]
    q = bytes(int(c) // 40 for c in cells)
    return f"{(x1 - x0) // 6}x{h // 6}:" + hashlib.md5(q).hexdigest()[:12]


async def _ffmpeg(*args: str) -> bytes:
    p = await asyncio.create_subprocess_exec("ffmpeg", "-loglevel", "error", *args, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    return out


async def _vision(http, prompt: str, jpg: bytes, max_tokens: int = 300) -> dict | None:
    body = {"model": VISION, "max_tokens": max_tokens, "temperature": 0, "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}}]}]}
    for attempt in range(3):
        try:
            r = await http.post(f"{T.API}/chat/completions", json=body, timeout=90)
            r.raise_for_status()
            txt = r.json()["choices"][0]["message"]["content"] or ""
            i = txt.find("{")
            return json.JSONDecoder().raw_decode(txt[i:])[0] if i >= 0 else None   # 1er objet JSON
        except Exception as e:
            if attempt == 2:
                log.warning("modèle d'image : %s", str(e)[:150])
                return None
            await asyncio.sleep(2 + attempt * 3)


def _clean(n) -> str | None:
    if not isinstance(n, str):
        return None
    n = re.sub(r"\s+", " ", n).replace("...", "…")
    n = re.sub(r"\s*\([A-Z]{1,3}\)\s*$", "", n)          # initiales de l'avatar recopiées : « (VV) »
    n = _strip_initials(n)
    if re.fullmatch(r"[A-Z]{1,3}", n.strip()):          # pastille seule (« JG ») : pas un nom
        return None
    n = clean_name(n).strip(" .,;:&")
    return n if len(n) >= 2 and n.lower() not in ("null", "none", "aucun", "inconnu") else None


def _strip_initials(n: str) -> str:
    """Pastille de l'avatar lue avec le nom : « CD Camille… », « CD (Durand, C…) », « D dur… » -> le nom.
    Seulement si les initiales correspondent aux mots qui suivent (sinon on ne touche à rien)."""
    m = re.match(r"^([A-Z]{1,3})\s+\(?(.+?)\)?$", n.strip())
    if not m:
        return n
    ini, rest = m.group(1), m.group(2).strip()
    firsts = {w[0].upper() for w in re.findall(r"[^\W\d_][\w'’-]*", rest)}
    if len(re.sub(r"[\W_…]", "", rest)) >= 2 and (set(ini) <= firsts or ini[0] == rest[:1].upper()):
        return rest
    return n


def _norm(n: str) -> str:
    """Clé de comparaison : sans accents, casse, ponctuation ni points de suspension."""
    n = "".join(c for c in unicodedata.normalize("NFKD", n) if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[._\-…]+", " ", n)).strip()


def resolve_names(obs: list[str]) -> dict[str, str]:
    """Regroupe les lectures d'un même nom : fautes de lecture (« Camille Durad » / « Camille Durand »), accents,
    casse, noms tronqués (« Camille Dur… », « CAMILLE ») -> nom complet le plus vu, s'il est unique."""
    cnt = Counter(o for o in obs if o)
    canon: list[str] = []                 # une forme par personne, de la plus vue à la moins vue
    out: dict[str, str] = {}
    for n, _ in cnt.most_common():
        k = _norm(n)
        for c in canon:
            if abs(len(k) - len(_norm(c))) <= 2 and SequenceMatcher(None, k, _norm(c)).ratio() >= 0.88:
                out[n] = c
                break
        else:
            canon.append(n)
            out[n] = n
    full = {}
    for c in canon:
        p = _norm(c)
        longer = [d for d in canon if d != c and len(_norm(d)) > len(p) and _norm(d).startswith(p)]
        if len(p) >= 4 and longer:
            top = max(longer, key=lambda d: len(_norm(d)))
            if all(_norm(top).startswith(_norm(d)) for d in longer):
                full[c] = top
    return {n: full.get(c, c) for n, c in out.items()}


def rename_everywhere(doc, mapping: dict[str, str]):
    """Remplace des noms (valeurs et clés exactes) dans tout le document de transcription."""
    if isinstance(doc, dict):
        return {mapping.get(k, k) if isinstance(k, str) else k: rename_everywhere(v, mapping) for k, v in doc.items()}
    if isinstance(doc, list):
        return [rename_everywhere(v, mapping) for v in doc]
    return mapping.get(doc, doc) if isinstance(doc, str) else doc


def merge_participants(parts: list[dict]) -> list[dict]:
    """Fusionne les entrées d'un même participant (après regroupement des noms)."""
    out: dict[str, dict] = {}
    for p in parts:
        q = out.get(p["name"])
        if not q:
            out[p["name"]] = dict(p)
            continue
        q["first"], q["last"], q["seen"] = min(q["first"], p["first"]), max(q["last"], p["last"]), q["seen"] + p["seen"]
        for k, f in (("spoke_first", min), ("spoke_last", max)):
            if k in p:
                q[k] = f(q[k], p[k]) if k in q else p[k]
    return sorted(out.values(), key=lambda p: p["first"])


class VideoScan:
    """Parcourt la vidéo : orateur actif (cadre coloré) et participants (relevé périodique)."""

    def __init__(self, http, mp4: Path, w: int, h: int):
        self.http, self.mp4, self.w, self.h = http, mp4, w, h
        self.vw, self.vh = w // 2, h // 2
        self.sem = asyncio.Semaphore(4)
        self.samples: list[tuple[float, tuple]] = []     # (temps vidéo, empreintes des vignettes encadrées)
        self.tasks: dict[str, asyncio.Task] = {}
        self.rosters: list[tuple[float, asyncio.Task]] = []
        self.pos = 0.0

    async def _name(self, tv: float, box) -> str | None:
        x0, y0, x1, y1 = (v * 2 for v in box)
        x0, y0 = max(0, x0 - 2), max(0, y0 - 2)
        cw, ch = min(self.w - x0, x1 - x0 + 4), min(self.h - y0, y1 - y0 + 4)
        k = max(1.0, 480 / cw)
        async with self.sem:
            jpg = await _ffmpeg("-ss", f"{tv:.2f}", "-i", str(self.mp4), "-frames:v", "1", "-vf",
                                f"crop={cw}:{ch}:{x0}:{y0},scale={int(cw * k) // 2 * 2}:-2", "-q:v", "3", "-f", "mjpeg", "pipe:1")
            d = await _vision(self.http, NAME_PROMPT, jpg, 60) if jpg else None
        return _clean((d or {}).get("nom"))

    async def _roster(self, tv: float) -> list[str]:
        async with self.sem:
            jpg = await _ffmpeg("-ss", f"{tv:.2f}", "-i", str(self.mp4), "-frames:v", "1", "-vf", "scale=1600:-2",
                                "-q:v", "3", "-f", "mjpeg", "pipe:1")
            d = await _vision(self.http, ROSTER_PROMPT, jpg, 400) if jpg else None
        return [c for c in (_clean(n) for n in ((d or {}).get("noms") or [])) if c]

    async def run(self):
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-i", str(self.mp4), "-vf", f"fps={FPS},scale={self.vw}:{self.vh}:flags=area",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1", stdout=asyncio.subprocess.PIPE)
        n, size = 0, self.vw * self.vh * 3
        try:
            while True:
                try:
                    buf = await proc.stdout.readexactly(size)
                except asyncio.IncompleteReadError:
                    break
                tv = n / FPS
                n += 1
                self.pos = tv
                rgb = np.frombuffer(buf, np.uint8).reshape(self.vh, self.vw, 3)
                sigs = []
                for box in find_boxes(rgb)[:3]:
                    sig = signature(rgb, box)
                    if sig not in self.tasks and len(self.tasks) < MAX_NAME_CALLS:
                        self.tasks[sig] = asyncio.create_task(self._name(tv, box))
                    if sig in self.tasks:
                        sigs.append(sig)
                self.samples.append((tv, tuple(sigs)))
                if (n - 1) % (FPS * ROSTER_EVERY) == FPS * 5:   # à 5 s, puis toutes les minutes
                    self.rosters.append((tv, asyncio.create_task(self._roster(tv))))
                if n % 50 == 0:
                    await asyncio.sleep(0)
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
        names = {s: await t for s, t in self.tasks.items()}
        rosters = [(tv, await t) for tv, t in self.rosters]
        return names, rosters


MERGE_MAX_WORDS = int(os.environ.get("OFFLINE_MERGE_MAX_WORDS", "120"))   # même seuil que la « salle partagée » du CR


def merge_same_name(doc: dict) -> dict[str, str]:
    """Voix nommées d'après l'écran qui portent le même nom : les petites voix secondaires sont regroupées avec
    la principale (l'empreinte vocale coupe parfois une même personne en plusieurs voix).

    Une voix secondaire qui parle vraiment (au moins MERGE_MAX_WORDS mots) reste à part : c'est souvent une
    AUTRE personne sur le même poste ou dans la même salle (Teams encadre l'appareil, pas la personne). Le
    compte rendu l'affiche « Nom (voix 2) » avec un avertissement « salle partagée »."""
    st, spk = doc.get("diarizer") or {}, doc.get("speakers") or {}
    words: dict[str, int] = {}
    for p in doc.get("paragraphs") or []:
        words[p.get("spk")] = words.get(p.get("spk"), 0) + len(str(p.get("text") or "").split())
    groups: dict[str, list[str]] = {}
    for sid, v in spk.items():
        if sid != "ME" and v.get("source") == "ui" and not v.get("name"):
            groups.setdefault(v.get("auto") or v["label"], []).append(sid)
    remap = {}
    for name, sids in groups.items():
        if len(sids) < 2:
            continue
        keep = max(sids, key=lambda k: (st.get(k) or {}).get("n", 0))
        for k in sids:
            if k == keep or words.get(k, 0) >= MERGE_MAX_WORDS:
                continue
            remap[k] = keep
            if k in st and keep in st:
                st[keep]["n"] += st[k]["n"]
                for nm, w in st[k]["votes"].items():
                    st[keep]["votes"][nm] = st[keep]["votes"].get(nm, 0) + w
            st.pop(k, None)
            spk.pop(k, None)
    for e in doc.get("raw", []) + doc.get("paragraphs", []):
        if e.get("spk") in remap:
            e["spk"] = remap[e["spk"]]
    first = next((k for k in spk if k != "ME"), None)
    if first:
        T.rename_in_doc(doc, first, spk[first].get("name"))   # recalcule les libellés « (2) »
    return remap


def finalize(base: Path):
    """Dernière passe sur le fichier : noms regroupés partout, participants fusionnés, voix d'un même nom."""
    p = Path(str(base) + ".transcript.json")
    doc = json.loads(p.read_text())
    obs = []
    for q in doc.get("participants", []):
        obs += [q["name"]] * max(1, q.get("seen", 0))
    obs += [r["ui"] for r in doc.get("raw", []) if r.get("ui")]
    for sid, v in (doc.get("speakers") or {}).items():
        if sid != "ME" and v.get("source") == "ui":
            obs += [v.get("auto") or v["label"]] * max(1, (doc.get("diarizer", {}).get(sid) or {}).get("n", 1))
    clean = {n: _clean(n) or n for n in set(obs)}
    res = resolve_names([clean[n] for n in obs])
    mapping = {n: res.get(clean[n], clean[n]) for n in set(obs)}
    mapping = {k: v for k, v in mapping.items() if k != v}
    doc = rename_everywhere(doc, mapping)
    doc["participants"] = merge_participants(doc.get("participants", []))
    remap = merge_same_name(doc)
    T.save_doc(base, doc)
    log.info("noms regroupés : %s", mapping)
    log.info("voix regroupées : %d -> %s", len(remap), {k: v["label"] for k, v in doc["speakers"].items()})


# ----------------------------------------------------------------------------- transcription
class Offline(T.Transcriber):
    def __init__(self, out_dir: str):
        super().__init__(out_dir)
        self.asr_sem = asyncio.Semaphore(int(os.environ.get("OFFLINE_ASR_PARALLEL", "6")))
        self.audio_pos = 0.0

    def _ready_lines(self):
        return super()._ready_lines()[:BATCH]

    async def feed_audio(self, mp4: Path):
        """Même découpage que la capture en direct (_capture), sur la piste son de la vidéo."""
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-i", str(mp4), "-vn", "-ac", "1", "-ar", str(T.RATE), "-f", "s16le", "pipe:1",
            stdout=asyncio.subprocess.PIPE)
        pre, hang, max_frames = 15, 40, 1000
        hist: list[np.ndarray] = []
        seg: list[np.ndarray] = []
        voiced = silent = 0
        seg_start = 0.0
        frames = 0
        loud_of = lambda x: 20 * np.log10(np.sqrt(np.mean(x.astype(np.float32) ** 2)) / 32768 + 1e-9) > T.THRESH_DB
        try:
            while True:
                try:
                    data = await proc.stdout.readexactly(T.FRAME * 2)
                except asyncio.IncompleteReadError:
                    break
                frames += 1
                now = self.t0 + frames * 0.02
                self.audio_pos = frames * 0.02
                f = np.frombuffer(data, dtype=np.int16)
                loud = loud_of(f)
                if not seg:
                    hist.append(f)
                    hist = hist[-pre:]
                    if loud:
                        seg = list(hist)
                        seg_start = now - len(hist) * 0.02
                        voiced, silent = 1, 0
                    continue
                seg.append(f)
                if loud:
                    voiced += 1
                    silent = 0
                else:
                    silent += 1
                if silent >= hang or len(seg) >= max_frames:
                    cut = len(seg)
                    if len(seg) >= max_frames:
                        tail = [float(np.abs(x).mean()) for x in seg[-150:]]
                        cut = len(seg) - 150 + int(np.argmin(tail)) + 1
                    keep, seg = seg[:cut], seg[cut:]
                    if voiced >= 25:
                        while self.inflight >= 12:      # pas d'avance démesurée sur Whisper
                            await asyncio.sleep(0.2)
                        self._submit(np.concatenate(keep), seg_start, "meet")
                    seg_start += cut * 0.02
                    voiced = sum(1 for x in seg if loud_of(x))
                    silent = 0
                    if not voiced:
                        hist, seg = seg[-pre:], []
            if seg and voiced >= 25:
                self._submit(np.concatenate(seg), seg_start, "meet")
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()

    async def format_all(self, audio_done: asyncio.Event):
        while True:
            ready = T.Transcriber._ready_lines(self)
            if len(ready) >= BATCH or (audio_done.is_set() and ready):
                self._merge()
                await self._format(final=True)
            elif audio_done.is_set() and not self.inflight:
                return
            else:
                await asyncio.sleep(2)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="nom de la vidéo (…mp4) dans le dossier des enregistrements")
    ap.add_argument("--force", action="store_true", help="remplace une transcription existante")
    ap.add_argument("--no-slides", action="store_true")
    ap.add_argument("--finalize", action="store_true", help="seulement la dernière passe (noms, voix) sur le fichier")
    ap.add_argument("--me", default=os.environ.get("TRANSCRIPT_ME_NAME") or None,
                    help="nom de l'utilisateur (voix « ME »), ex. « Camille Durand »")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    mp4 = REC / os.path.basename(args.name)
    stem = mp4.name[:-4] if mp4.name.endswith(".mp4") else mp4.name
    mp4 = REC / (stem + ".mp4")
    if not mp4.exists():
        sys.exit(f"vidéo introuvable : {mp4}")
    base = REC / stem
    if args.finalize:
        return finalize(base)
    if Path(str(base) + ".transcript.json").exists() and not args.force:
        sys.exit("transcription déjà présente (--force pour la remplacer)")
    mp = REC / (stem + ".mp4.json")
    rmeta = json.loads(mp.read_text()) if mp.exists() else {}
    try:
        t0 = dt.datetime.fromisoformat(rmeta["started_at"]).timestamp()
    except (KeyError, ValueError):
        t0 = mp4.stat().st_mtime
    probe = (await _ffmpeg_probe(mp4)) or (1920, 1200)

    tr = Offline(str(REC))
    tr.reset()
    tr.diar.bot_names = set()
    tr.active = True
    tr.base, tr.t0 = base, t0
    tr.meta = {**{k: rmeta.get(k) for k in ("title", "platform", "url", "site", "worker", "calendar", "owner", "shared_with",
                                           "late_start") if rmeta.get(k)},
               "started_at": dt.datetime.fromtimestamp(t0).astimezone().isoformat(),
               "ended_at": rmeta.get("ended_at"), "asr": T.ASR_MODEL, "llm": T.LLM_MODEL, "lang": T.LANG,
               "me_name": args.me, "lexicon": lexicon.load().version or None, "offline": {"at": dt.datetime.now().astimezone().isoformat(),
                                            "why": "transcription faite après coup, à partir de la vidéo"}}
    started = time.time()

    # 1. image : orateur actif et participants
    scan = VideoScan(tr.http, mp4, *probe)
    prog = asyncio.create_task(_progress(lambda: f"image {int(scan.pos // 60)} min, noms lus {sum(t.done() for t in scan.tasks.values())}/{len(scan.tasks)}"))
    names, rosters = await scan.run()
    prog.cancel()
    every = [n for n in names.values() if n] + [n for _, r in rosters for n in r]
    full = resolve_names(every)
    lex = lexicon.load()          # noms de référence (casse, noms tronqués complétés)
    full = {k: lex.canonical(v) or v for k, v in full.items()}
    named = 0
    speaking = []           # (temps vidéo, noms encadrés)
    for tv, sigs in scan.samples:
        ns = tuple(dict.fromkeys(full[names[s]] for s in sigs if names.get(s)))
        speaking.append((tv, ns))
        named += bool(ns)
        tr.diar.ui_sample(t0 + tv, ns)
    parts: dict[str, dict] = {}
    for tv, r in rosters:
        for n in r:
            p = parts.setdefault(full.get(n, n), {"first": round(tv, 1), "last": round(tv, 1), "seen": 0})
            p["last"], p["seen"] = round(tv, 1), p["seen"] + 1
    for tv, ns in speaking:
        for n in ns:
            p = parts.setdefault(n, {"first": round(tv, 1), "last": round(tv, 1), "seen": 0})
            p.setdefault("spoke_first", round(tv, 1))
            p["spoke_last"] = round(tv, 1)
            p["first"], p["last"] = min(p["first"], round(tv, 1)), max(p["last"], round(tv, 1))
    tr.participants = parts
    log.info("image : %d relevés, %d avec un orateur nommé, %d vignettes lues, %d participants (%.0f s)",
             len(scan.samples), named, len(names), len(parts), time.time() - started)
    log.info("orateurs vus : %s", dict(Counter(n for _, ns in speaking for n in ns).most_common(25)))

    # 2. son : Whisper, voix, mise en forme ; diapositives en parallèle
    audio_done = asyncio.Event()
    fmt = asyncio.create_task(tr.format_all(audio_done))
    sl = None
    if not args.no_slides and slides_mod.ENABLED:
        tr.slide_reader = slides_mod.SlideReader(tr.http, T.API, base, tr.slides, on_change=tr._save)
        sl = asyncio.create_task(tr.slide_reader.from_video(mp4, 0.0))
    prog = asyncio.create_task(_progress(lambda: f"son {int(tr.audio_pos // 60)} min, en cours {tr.inflight}, "
                                                 f"lignes {len(tr.raw)}, paragraphes {len(tr.paragraphs)}, "
                                                 f"voix {len(tr.diar.spk)}, diapositives {len(tr.slides)}"))
    await tr.feed_audio(mp4)
    for _ in range(600):
        if not tr.inflight:
            break
        await asyncio.sleep(0.5)
    audio_done.set()
    await fmt
    if sl:
        await sl
    prog.cancel()
    tr._merge()
    await tr._format(final=True)
    tr.active = False
    tr._save()
    lab = tr.labels()
    log.info("terminé en %.0f s : %d lignes, %d paragraphes, %d diapositives, Whisper %s, erreurs %d",
             time.time() - started, len(tr.raw), len(tr.paragraphs), len(tr.slides), tr.asr_stats, tr.errors)
    log.info("voix : %s", {k: (v["label"], v.get("source")) for k, v in lab.items()})
    await tr.http.aclose()
    await tr.llm.close()
    finalize(base)


async def _ffmpeg_probe(mp4: Path):
    p = await asyncio.create_subprocess_exec("ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                                             "stream=width,height", "-of", "csv=p=0", str(mp4),
                                             stdout=asyncio.subprocess.PIPE)
    out, _ = await p.communicate()
    try:
        w, h = out.decode().strip().split(",")[:2]
        return int(w), int(h)
    except ValueError:
        return None


async def _progress(text):
    while True:
        await asyncio.sleep(30)
        log.info("… %s", text())


if __name__ == "__main__":
    asyncio.run(main())
