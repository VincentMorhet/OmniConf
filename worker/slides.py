"""Diapositives et documents partagés à l'écran : lecture par un modèle d'image (OVH AI Endpoints).

L'écran de l'assistant est surveillé (toutes les ~3 s, en direct ; ou les images d'un MP4 enregistré).
Quand l'image change nettement puis se stabilise (nouvelle diapositive), une capture est envoyée au
modèle d'image, qui retranscrit le texte et décrit les schémas. Chaque diapositive est datée comme les
paroles : elle s'insère dans la transcription, dans les points d'étape et dans le compte rendu.

Fichiers : la liste est dans <stem>.transcript.json (clé « slides ») ; les captures dans
<stem>.slide-NNN.jpg, à côté de la vidéo.
"""
import asyncio
import base64
import json
import logging
import os
import re
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("slides")

MODEL = os.environ.get("SLIDES_MODEL", "Mistral-Small-3.2-24B-Instruct-2506")
ENABLED = os.environ.get("SLIDES", "1") == "1"
EVERY = float(os.environ.get("SLIDES_EVERY", "3"))          # secondes entre deux coups d'œil à l'écran
MIN_GAP = float(os.environ.get("SLIDES_MIN_GAP", "6"))      # secondes minimum entre deux lectures
MAX_PER_MEETING = int(os.environ.get("SLIDES_MAX", "400"))
W, H = 96, 60                                               # vignette de comparaison (niveaux de gris)
# Réglés sur une vraie réunion Teams de 80 min (38 lectures) et une présentation de test (4 sur 4) :
# une diapositive de texte ne change que 2 à 6 % des pixels de la vignette ; la même diapositive, ~0,1 %.
PIXEL = 12         # écart de niveau de gris à partir duquel un pixel a changé
CHANGE = 0.02      # part de l'image qui a changé (vs. la dernière lue) pour déclencher une lecture
STABLE = 0.01      # part maximum qui bouge encore entre deux coups d'œil (animation, vidéo en cours)

PROMPT = ("Image : écran d'une visioconférence. S'il y a une diapositive, un document ou une application "
          "partagée, retranscris fidèlement son texte (titre, puces, libellés importants), puis décris en 2 à "
          "4 phrases les schémas, tableaux ou graphiques (ce qu'ils montrent, pas leur mise en forme) ; laisse "
          "« visuel » vide s'il n'y en a pas. "
          "Ignore l'interface de la visio (boutons, barre d'outils, vignettes et noms des participants). "
          "S'il n'y a que des vignettes de participants ou une vidéo, réponds partage=false. "
          "Réponds uniquement en JSON, en français : "
          '{"partage": true|false, "titre": "...", "texte": ["..."], "visuel": "..."}')


def _frac(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(np.abs(a.astype(np.int16) - b.astype(np.int16)) > PIXEL))


class ChangeTracker:
    """Décide quand lire l'écran : changement net par rapport à la dernière lecture, puis image stable."""

    def __init__(self):
        self.ref = None
        self.prev = None

    def feed(self, g: np.ndarray) -> bool:
        changed = self.ref is None or _frac(g, self.ref) > CHANGE
        stable = self.prev is not None and _frac(g, self.prev) < STABLE
        self.prev = g
        if changed and stable:
            self.ref = g
            return True
        return False


def _words(s: dict) -> set:
    return set(re.findall(r"\w{2,}", ((s.get("titre") or "") + " " + " ".join(s.get("texte") or [])).lower()))


def _same(a: dict, b: dict) -> bool:
    """Même diapositive relue (le modèle varie un peu d'une lecture à l'autre) : ≥ 80 % de mots communs."""
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.8


def _parse(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    texte = d.get("texte") or []
    if isinstance(texte, str):
        texte = [texte]
    return {"partage": bool(d.get("partage")), "titre": str(d.get("titre") or "").strip(),
            "texte": [t for t in (re.sub(r"^[•●▪◦·\-–*]\s*", "", str(x).strip()) for x in texte) if t],
            "visuel": str(d.get("visuel") or "").strip()}


class SlideReader:
    """Lit les diapositives et les ajoute à une liste (en direct ou depuis un MP4)."""

    def __init__(self, http, api: str, stem_path: Path, slides: list, on_change=None):
        self.http, self.api, self.base, self.slides = http, api, stem_path, slides
        self.on_change = on_change or (lambda: None)
        self.tracker = ChangeTracker()
        self.last_call = 0.0
        self.calls = 0
        self.error = None
        self.retry = False   # lecture vide : un second essai (page encore en cours d'affichage…)

    def _img_path(self, n: int) -> Path:
        return Path(f"{self.base}.slide-{n:03d}.jpg")

    async def read(self, t: float, jpg: bytes) -> dict | None:
        """Envoie une capture au modèle d'image ; ajoute la diapositive si elle est nouvelle."""
        self.last_call, self.calls = time.time(), self.calls + 1
        body = {"model": MODEL, "max_tokens": 1200, "temperature": 0,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}}]}]}
        try:
            r = await self.http.post(f"{self.api}/chat/completions", json=body, timeout=120)
            r.raise_for_status()
            s = _parse(r.json()["choices"][0]["message"]["content"])
            self.error = None
        except Exception as e:
            self.error = f"lecture de l'écran : {e}"[:200]
            log.warning(self.error)
            return None
        # le modèle répond parfois « pas de partage » sur une diapositive qu'il a pourtant lue : on garde
        # dès qu'il y a un titre ou au moins deux lignes de texte
        if not s or not (s["titre"] or len(s["texte"]) >= 2) or (not s["partage"] and not s["texte"]):
            log.info("écran à %.0f s : pas de partage", t)
            self.retry = not self.retry
            if self.retry:
                self.tracker.ref = None   # relu au prochain coup d'œil stable
            return None
        self.retry = False
        full = [x for x in self.slides if not x.get("repeat_of")]
        if self.slides and _same(self._full(self.slides[-1]), s):
            log.info("écran à %.0f s : même diapositive", t)
            return None   # même diapositive (curseur, vignette qui bouge…)
        prev = next((x for x in reversed(full) if _same(x, s)), None)
        if prev:   # retour sur une diapositive déjà vue : simple mention
            n = len(self.slides) + 1
            self.slides.append({"n": n, "t": round(t, 1), "titre": prev["titre"], "repeat_of": prev["n"]})
            log.info("diapositive %d à %.0f s : retour à la n°%d", n, t, prev["n"])
            self.on_change()
            return self.slides[-1]
        if re.match(r"^(aucun|pas de|il n'y a)", s["visuel"], re.I):
            s["visuel"] = ""
        n = len(self.slides) + 1
        self._img_path(n).write_bytes(jpg)
        s = {"n": n, "t": round(t, 1), "titre": s["titre"], "texte": s["texte"], "visuel": s["visuel"]}
        self.slides.append(s)
        log.info("diapositive %d à %.0f s : %s", n, t, s["titre"][:60])
        self.on_change()
        return s

    def _full(self, s: dict) -> dict:
        """Une mention « retour à » renvoie vers la diapositive d'origine."""
        while s.get("repeat_of"):
            s = next((x for x in self.slides if x["n"] == s["repeat_of"]), {})
        return s

    def due(self, g: np.ndarray) -> bool:
        if time.time() - self.last_call < MIN_GAP or self.calls >= MAX_PER_MEETING:
            self.tracker.prev = g   # pas de lecture possible maintenant : on ne « consomme » pas le changement
            return False
        return self.tracker.feed(g)

    # ------------------------------------------------------------------ en direct
    async def live(self, display: str, size: str, t0: float, is_active):
        grab = ["ffmpeg", "-loglevel", "error", "-f", "x11grab", "-video_size", size, "-i", display, "-frames:v", "1"]
        while is_active():
            await asyncio.sleep(EVERY)
            try:
                g = await _run([*grab, "-vf", f"scale={W}:{H},format=gray", "-f", "rawvideo", "pipe:1"])
                if len(g) != W * H:
                    continue
                if self.due(np.frombuffer(g, np.uint8)):
                    t = time.time() - t0
                    jpg = await _run([*grab, "-vf", "scale=1280:-2", "-q:v", "4", "-f", "mjpeg", "pipe:1"])
                    if jpg:
                        await self.read(t, jpg)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # pragma: no cover
                log.warning("surveillance de l'écran : %s", e)

    # ------------------------------------------------------------------ après coup, depuis la vidéo
    async def from_video(self, mp4: Path, offset: float = 0.0, progress=None, step: float = 3.0):
        """Parcourt la vidéo (une image toutes les `step` s) ; offset = décalage vidéo -> transcription."""
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-i", str(mp4), "-vf", f"fps=1/{step},scale={W}:{H},format=gray",
            "-f", "rawvideo", "pipe:1", stdout=asyncio.subprocess.PIPE)
        i = 0
        try:
            while True:
                g = await proc.stdout.readexactly(W * H)
                tv = i * step
                i += 1
                if progress:
                    progress(tv)
                if not self.tracker.feed(np.frombuffer(g, np.uint8)) or self.calls >= MAX_PER_MEETING:
                    continue
                jpg = await _run(["ffmpeg", "-loglevel", "error", "-ss", f"{tv:.2f}", "-i", str(mp4),
                                  "-frames:v", "1", "-vf", "scale=1280:-2", "-q:v", "4", "-f", "mjpeg", "pipe:1"])
                if jpg:
                    await self.read(tv - offset, jpg)
        except asyncio.IncompleteReadError:
            pass
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()


async def _run(cmd: list[str]) -> bytes:
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    return out


def slides_text(slides: list, fmt_t) -> list[tuple[float, str]]:
    """Lignes « écran » pour les modèles de langue : (t, texte)."""
    out = []
    for s in slides or []:
        if s.get("repeat_of"):
            first = next((x for x in slides if x["n"] == s["repeat_of"]), {})
            out.append((s["t"], f"[{fmt_t(s['t'])}] [ÉCRAN PARTAGÉ] retour à la diapositive « {s.get('titre') or 'sans titre'} »"
                                + (f" (montrée à {fmt_t(first['t'])})" if first else "")))
            continue
        body = " ; ".join(s.get("texte") or [])
        line = f"[{fmt_t(s['t'])}] [ÉCRAN PARTAGÉ] {s.get('titre') or 'Diapositive'}" + (f" — {body}" if body else "")
        if s.get("visuel"):
            line += f" — (visuel : {s['visuel']})"
        out.append((s["t"], line))
    return out
