"""Transcription en direct : audio PulseAudio → Whisper (OVH AI Endpoints) → mise en forme par Qwen.

Deux flux séparés :
  - meeting_out.monitor : ce que disent les participants (son de la réunion), découpé en intervenants
    par empreinte vocale + orateur actif affiché par la plateforme (voir speakers.py) ;
  - user_in.monitor     : ce que tu dis depuis l'IHM (« Moi »).
Chaque flux est découpé sur les silences (VAD par énergie). Chaque segment part vers Whisper. Toutes
les ~20 s, les lignes brutes passent par le LLM, qui ponctue, retire les hésitations, corrige les
termes mal reconnus (glossaire) et regroupe en paragraphes.
Fichiers à côté des MP4 : <base>.transcript.json (brut + mis en forme) et <base>.md (lisible).
"""
import asyncio
import datetime as dt
import io
import json
import logging
import os
import re
import threading
import time
import wave
from pathlib import Path

import httpx
import numpy as np

import analyst
import slides as slides_mod
from speakers import Diarizer

log = logging.getLogger("transcriber")

API = os.environ.get("OVH_AI_BASE_URL", "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1")
TOKEN = os.environ.get("OVH_AI_ENDPOINTS_ACCESS_TOKEN", "")
ASR_MODEL = os.environ.get("TRANSCRIPT_ASR_MODEL", "whisper-large-v3")
LLM_MODEL = os.environ.get("TRANSCRIPT_LLM_MODEL", "Qwen3.8-27B")
LANG = os.environ.get("TRANSCRIPT_LANG", "fr")
GLOSSARY = os.environ.get("TRANSCRIPT_GLOSSARY", "")
ME = os.environ.get("TRANSCRIPT_ME_LABEL", "Moi")
OTHERS = os.environ.get("TRANSCRIPT_OTHERS_LABEL", "Participants")
THRESH_DB = float(os.environ.get("TRANSCRIPT_VAD_DB", "-45"))
FORMAT_EVERY = float(os.environ.get("TRANSCRIPT_FORMAT_EVERY", "20"))

RATE = 16000
FRAME = 320  # 20 ms
# Phrases que Whisper invente sur du bruit ou du silence (sous-titres YouTube…)
HALLU = re.compile(r"sous-titr|amara\.org|merci d'avoir regardé|abonnez-vous|thanks for watching|\bMFP\b|"
                   r"^\W*(thank you|you)\W*$", re.I)
# Boucles : 2 à 4 mots répétés au moins 4 fois de suite (« je pense que je pense que… »), ou un même mot
# 8 fois (« non, non, non, non » se dit vraiment : réglé sur de vraies réunions)
LOOP_PHRASE = re.compile(r"\b(\w+(?:\W+\w+){1,3}?)(?:\W+\1\b){3,}", re.I)
LOOP_WORD = re.compile(r"\b(\w+)\b(?:\W+\1\b){7,}", re.I)
MERCI = re.compile(r"^\W*(merci(\s+(beaucoup|à tous|à toutes))?\W*){2,}$", re.I)
RETRY_CHUNK = float(os.environ.get("TRANSCRIPT_RETRY_CHUNK", "6"))   # tronçons du second essai (secondes)


def suspect_reason(text: str, segs: list, dur: float) -> str | None:
    """Transcription probablement inventée par Whisper (hallucination) : raison, sinon None."""
    t = (text or "").strip()
    if not t:
        return None
    if HALLU.search(t):
        return "phrase type de sous-titrage"
    if LOOP_PHRASE.search(t) or LOOP_WORD.search(t):
        return "boucle de répétition"
    words = re.findall(r"\w+", t.lower())
    if len(words) >= 16 and len(set(words)) / len(words) < 0.25:
        return "boucle de répétition"
    if any(float(s.get("compression_ratio") or 0) > 2.4 for s in segs):
        return "texte anormalement répétitif"
    if MERCI.match(t) or (re.fullmatch(r"\W*merci\W*", t, re.I) and dur > 8):
        return "« Merci » en série"
    return None


def seg_conf(s: dict) -> float | None:
    """Confiance d'un segment Whisper (0-1) : probabilité moyenne des mots, d'après avg_logprob."""
    try:
        return max(0.0, min(1.0, float(np.exp(float(s["avg_logprob"])))))
    except (KeyError, TypeError, ValueError):
        return None

SYSTEM_PROMPT = """Tu mets en forme la transcription automatique (speech-to-text) d'une réunion en {lang}.
Chaque ligne : [horodatage] [identifiant] nom du locuteur : texte.
Règles :
- Ponctuation, majuscules et accents corrects ; retire les hésitations (euh, ben, hum), les faux départs
  et les répétitions involontaires.
- Corrige les mots manifestement mal reconnus d'après le contexte{glossary}.
- Ne résume pas, n'invente rien, n'ajoute aucun commentaire. Conserve le registre et le sens exacts.
- Ne supprime aucune information : noms, chiffres, dates, objets et compléments restent tous présents.
- Regroupe les lignes consécutives d'un MÊME identifiant en paragraphes ; ne fusionne jamais des
  identifiants différents et ne change jamais l'identifiant d'une ligne.
- Garde l'horodatage de la première ligne de chaque paragraphe.
- Si un passage est inintelligible, écris [inaudible].
Réponds UNIQUEMENT en JSON : {{"paragraphs": [{{"t": "mm:ss", "id": "S1", "text": "..."}}]}}"""


def _fmt_t(sec: float) -> str:
    sec = max(0, int(sec))
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _parse_t(s: str) -> float | None:
    try:
        parts = [int(x) for x in str(s).strip().split(":")]
    except ValueError:
        return None
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return float(sec)


def _wav(pcm: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.astype(np.int16).tobytes())
    return buf.getvalue()


class Transcriber:
    def __init__(self, out_dir: str):
        self.dir = Path(out_dir)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10),
                                      headers={"Authorization": f"Bearer {TOKEN}"})
        self.tasks: list[asyncio.Task] = []
        self.procs: list[asyncio.subprocess.Process] = []
        self.asr_sem = asyncio.Semaphore(3)
        self.fmt_lock = asyncio.Lock()
        self.diar = Diarizer()
        self.dlock = threading.Lock()  # l'empreinte vocale est calculée hors de la boucle asyncio
        self.llm = analyst.LLM()
        self.an_task: asyncio.Task | None = None
        self.reset()

    # ------------------------------------------------------------------ état
    def reset(self):
        self.active = False
        self.base = None          # chemin sans extension des fichiers de sortie
        self.t0 = 0.0             # epoch de référence (= début de l'enregistrement si lié)
        self.meta: dict = {}
        self.raw: list[dict] = []          # {id, t, te, spk, text, done}
        self.paragraphs: list[dict] = []   # {id, t, spk, text}
        self.slides: list[dict] = []       # diapositives lues à l'écran : {n, t, titre, texte, visuel}
        self.slide_reader = None
        self.participants: dict[str, dict] = {}   # nom affiché -> {first, last, seen, spoke_first, spoke_last}
        self.asr_stats = {"suspects": 0, "retries": 0, "fixed": 0, "removed": []}
        self._next_id = 1
        self.diar.reset()
        self.inflight = 0
        self.errors = 0
        self.last_error = None
        # points d'étape (analyse en direct) : état cumulé + un « flash » par tranche de ~5 min
        self.analysis = {"v": 0, "state": None, "flashes": [], "upto": 0.0, "at": None, "running": False,
                         "error": None, "last_run": time.time()}

    @property
    def enabled(self) -> bool:
        return bool(TOKEN)

    def status(self) -> dict:
        return {"active": self.active, "enabled": self.enabled, "file": self.base.name if self.base else None,
                "lines": len(self.raw), "paragraphs": len(self.paragraphs), "error": self.last_error,
                "llm": LLM_MODEL, "asr": ASR_MODEL, "n_speakers": len(self.diar.spk),
                "voice_id": self.diar.extractor is not None, "ui_speaker": self.diar.ui_active,
                "analysis": self.analysis_status(), "slides": len(self.slides),
                "slides_error": self.slide_reader.error if self.slide_reader else None}

    def analysis_status(self) -> dict:
        a = self.analysis
        nxt = max(0, round(a["last_run"] + analyst.LIVE_EVERY - time.time())) if self.active else None
        return {"v": a["v"], "running": a["running"], "at": a["at"], "next_in": nxt, "error": a["error"],
                "count": len(a["flashes"]), "model": analyst.LIVE_MODEL, "every": analyst.LIVE_EVERY}

    # ------------------------------------------------------------------ cycle de vie
    async def start(self, base: Path, t0: float, meta: dict, bot_names: tuple = (), me_name: str | None = None):
        if self.active:
            return
        if not self.enabled:
            raise RuntimeError("OVH_AI_ENDPOINTS_ACCESS_TOKEN absent de worker.env")
        self.reset()
        self.diar.bot_names = {n.strip().lower() for n in bot_names if n}
        self.active = True
        self.base, self.t0 = base, t0
        self.meta = {**meta, "started_at": dt.datetime.fromtimestamp(t0).astimezone().isoformat(),
                     "asr": ASR_MODEL, "llm": LLM_MODEL, "lang": LANG, "me_name": me_name or None}
        for source, speaker in (("meeting_out.monitor", "meet"), ("user_in.monitor", "ME")):
            self.tasks.append(asyncio.create_task(self._capture(source, speaker)))
        self.tasks.append(asyncio.create_task(self._format_loop()))
        if slides_mod.ENABLED:   # diapositives partagées : lues par un modèle d'image
            self.slide_reader = slides_mod.SlideReader(self.http, API, base, self.slides, on_change=self._save)
            self.tasks.append(asyncio.create_task(self.slide_reader.live(
                os.environ.get("DISPLAY", ":99"), os.environ.get("SCREEN_SIZE", "1920x1200"), t0, lambda: self.active)))
        self._save()
        log.info("transcription -> %s", base)

    async def stop(self):
        if not self.active:
            return None
        self.active = False
        for p in self.procs:  # fin des captures : le dernier segment est envoyé
            try:
                p.terminate()
            except ProcessLookupError:
                pass
        await asyncio.sleep(0.5)
        for _ in range(60):  # attend les réponses Whisper en cours
            if not self.inflight:
                break
            await asyncio.sleep(0.5)
        for t in self.tasks:
            t.cancel()
        if self.an_task and not self.an_task.done():
            self.an_task.cancel()  # le compte rendu final prend le relais
        self.analysis["running"] = False
        self.tasks, self.procs = [], []
        self._merge()
        await self._format(final=True)
        self.meta["ended_at"] = dt.datetime.now().astimezone().isoformat()
        self._save()
        log.info("transcription terminée : %d lignes, %d paragraphes", len(self.raw), len(self.paragraphs))
        return self.base.name

    # ------------------------------------------------------------------ capture + VAD
    async def _capture(self, source: str, speaker: str):
        proc = await asyncio.create_subprocess_exec(
            "parec", "-d", source, "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=100",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        self.procs.append(proc)
        pre = 15            # 300 ms gardées avant le début de la parole
        hang = 40           # 800 ms de silence = fin de phrase
        max_frames = 1000   # 20 s max par segment
        hist: list[np.ndarray] = []
        seg: list[np.ndarray] = []
        voiced = silent = 0
        seg_start = 0.0
        frames_read = 0
        try:
            while True:
                try:
                    data = await proc.stdout.readexactly(FRAME * 2)
                except asyncio.IncompleteReadError:
                    break
                frames_read += 1
                f = np.frombuffer(data, dtype=np.int16)
                rms = float(np.sqrt(np.mean(f.astype(np.float32) ** 2))) + 1e-9
                loud = 20 * np.log10(rms / 32768) > THRESH_DB
                now = time.time()
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
                    if len(seg) >= max_frames:  # coupe au point le plus calme des 3 dernières secondes
                        tail = [float(np.abs(x).mean()) for x in seg[-150:]]
                        cut = len(seg) - 150 + int(np.argmin(tail)) + 1
                    keep, seg = seg[:cut], seg[cut:]
                    if voiced >= 25:  # au moins 0,5 s de parole
                        self._submit(np.concatenate(keep), seg_start, speaker)
                    seg_start += cut * 0.02
                    voiced = sum(1 for x in seg if 20 * np.log10(np.sqrt(np.mean(x.astype(np.float32) ** 2)) / 32768 + 1e-9) > THRESH_DB)
                    silent = 0
                    if not voiced:
                        hist, seg = seg[-pre:], []
            if seg and voiced >= 25:
                self._submit(np.concatenate(seg), seg_start, speaker)
        except asyncio.CancelledError:
            pass
        finally:
            if proc.returncode is None:
                proc.kill()

    def _submit(self, pcm: np.ndarray, start: float, stream: str):
        """Réserve la place de la phrase (ordre chronologique) puis l'envoie à Whisper."""
        entry = {"id": self._next_id, "t": round(start - self.t0, 1), "te": round(start - self.t0 + len(pcm) / RATE, 1),
                 "stream": stream, "spk": "ME" if stream == "ME" else None, "text": None, "done": False}
        self._next_id += 1
        self.raw.append(entry)
        self.inflight += 1
        asyncio.create_task(self._asr(entry, pcm))

    async def _whisper(self, pcm: np.ndarray) -> dict:
        async with self.asr_sem:
            for attempt in range(3):
                try:
                    r = await self.http.post(
                        f"{API}/audio/transcriptions",
                        data={"model": ASR_MODEL, "language": LANG, "response_format": "verbose_json",
                              "temperature": "0", "prompt": "Réunion professionnelle." + (f" {GLOSSARY}." if GLOSSARY else "")},
                        files={"file": ("seg.wav", _wav(pcm), "audio/wav")})
                    r.raise_for_status()
                    return r.json()
                except httpx.HTTPError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(1 + attempt * 2)

    @staticmethod
    def _segments(d: dict, dur: float, offset: float = 0.0) -> list[dict]:
        segs = [{**x, "start": float(x.get("start", 0)) + offset, "end": float(x.get("end", dur)) + offset}
                for x in d.get("segments") or []
                if not (x.get("no_speech_prob", 0) > 0.6 and x.get("avg_logprob", 0) < -0.8) and x.get("text", "").strip()]
        if not d.get("segments") and (d.get("text") or "").strip():
            segs = [{"start": offset, "end": offset + dur, "text": d["text"]}]
        return segs

    @staticmethod
    def _chunks(pcm: np.ndarray, target: float) -> list[tuple[int, np.ndarray]]:
        """Découpe en tronçons de ~`target` s, aux endroits les plus calmes."""
        out, i, n, step = [], 0, len(pcm), int(target * RATE)
        while n - i > step * 1.4:
            lo, hi = i + int(step * 0.7), min(n, i + int(step * 1.3))
            win = pcm[lo:hi][: (hi - lo) // FRAME * FRAME].reshape(-1, FRAME).astype(np.float32)
            cut = lo + int(np.argmin(np.abs(win).mean(axis=1))) * FRAME + FRAME // 2 if len(win) else i + step
            out.append((i, pcm[i:cut]))
            i = cut
        out.append((i, pcm[i:]))
        return out

    async def _asr(self, entry: dict, pcm: np.ndarray):
        try:
            dur = len(pcm) / RATE
            segs = self._segments(await self._whisper(pcm), dur)
            text = " ".join(x["text"].strip() for x in segs)
            why = suspect_reason(text, segs, dur)
            if why:
                # Hallucination probable : nouvelle transcription en tronçons plus courts
                self.asr_stats["suspects"] += 1
                self.asr_stats["retries"] += 1
                retry = []
                for off, part in self._chunks(pcm, RETRY_CHUNK):
                    if len(part) >= RATE // 2:
                        retry += self._segments(await self._whisper(part), len(part) / RATE, off / RATE)
                rtext = " ".join(x["text"].strip() for x in retry)
                rwhy = suspect_reason(rtext, retry, dur)
                entry["retranscrit"] = True
                if not rwhy:                      # second essai propre : on le garde
                    segs, why = retry, None
                    self.asr_stats["fixed"] += 1
                elif _similar(text, rtext) and not HALLU.search(rtext):
                    why = None                    # même texte des deux façons : probablement prononcé tel quel
                else:
                    segs, why = retry, rwhy       # toujours douteux : signalé
                if why and any(HALLU.search(x["text"]) for x in segs):
                    # phrase type de sous-titres même au second essai : retirée de la transcription
                    self.asr_stats["removed"].append({"t": entry["t"], "te": entry["te"], "text": rtext[:200], "raison": why})
                    segs = [x for x in segs if not HALLU.search(x["text"])]
                    why = suspect_reason(" ".join(x["text"] for x in segs), segs, dur) if segs else None
            parts = self._split(entry, segs, dur)
            for p in parts:
                if why:
                    p["suspect"] = why
                if entry.get("retranscrit"):
                    p["retranscrit"] = True
            if entry["stream"] != "ME":
                await asyncio.to_thread(self._diarize, parts, pcm, entry["t"])
            if len(parts) > 1:  # une phrase Whisper par entrée : permet un locuteur par phrase
                for p in parts[1:]:
                    p["id"] = self._next_id
                    self._next_id += 1
                    self.raw.append(p)
            first = parts[0] if parts else {"text": "", "spk": entry["spk"]}
            entry.update({k: first[k] for k in ("t", "te", "spk", "ui", "conf", "suspect", "retranscrit") if k in first})
            entry["text"] = first["text"]
            self.last_error = None
        except Exception as e:
            self.errors += 1
            self.last_error = f"Whisper : {str(e)[:150]}"
            log.warning("asr: %s", e)
            entry["text"] = ""
        finally:
            self.inflight -= 1

    @staticmethod
    def _split(entry: dict, segs: list, dur: float) -> list[dict]:
        """Regroupe les segments Whisper par phrase (≥ 1,5 s si possible, pour une empreinte fiable)."""
        parts, cur = [], None
        for x in segs:
            s0, s1 = max(0.0, float(x.get("start", 0))), min(dur, float(x.get("end", dur)))
            txt = x["text"].strip()
            c = seg_conf(x)
            if cur and (cur["_e"] - cur["_s"] < 1.5 or s1 - s0 < 0.8):
                cur["text"] += " " + txt
                cur["_e"] = s1
                if c is not None:
                    cur["_c"].append((max(0.1, s1 - s0), c))
                continue
            cur = {"_s": s0, "_e": s1, "text": txt, "stream": entry["stream"], "spk": entry["spk"], "done": False,
                   "_c": [(max(0.1, s1 - s0), c)] if c is not None else []}
            parts.append(cur)
        for p in parts:
            p["t"] = round(entry["t"] + p["_s"], 1)
            p["te"] = round(entry["t"] + p["_e"], 1)
            cs = p.pop("_c")
            if cs:
                p["conf"] = round(sum(w * c for w, c in cs) / sum(w for w, _ in cs), 2)
        return parts

    def _diarize(self, parts: list[dict], pcm: np.ndarray, base_t: float):
        for p in parts:
            a, b = int(p["_s"] * RATE), int(p["_e"] * RATE)
            emb = self.diar.embed(pcm[a:b]) if b - a > 0 else None
            ui = self.diar.ui_name(self.t0 + p["t"], self.t0 + p["te"])
            with self.dlock:
                p["spk"] = self.diar.assign(emb, p["te"] - p["t"], ui)
            p["ui"] = ui

    def meeting_speech_since(self, secs: float) -> bool:
        """Un participant (pas « Moi ») a parlé dans les `secs` dernières secondes."""
        now = time.time() - (self.t0 or time.time())
        return any(e["spk"] != "ME" and now - e.get("te", e["t"]) < secs for e in self.raw[-20:])

    def save_extra(self, kind: str, data: dict):
        """Fichier annexe de la réunion : <stem>.<kind>.json (diagnostics…)."""
        if self.base:
            p = Path(str(self.base) + f".{kind}.json")
            p.write_text(json.dumps(data, ensure_ascii=False, indent=1))

    def ui_sample(self, names):
        if self.active:
            self.diar.ui_sample(time.time(), names)
            self._seen(names, spoke=True)

    def participants_sample(self, names):
        if self.active:
            self._seen(names)

    def _seen(self, names, spoke: bool = False):
        """Participants vus à l'écran (vignettes) ou affichés comme orateur : premières/dernières apparitions."""
        now = round(time.time() - self.t0, 1)
        for n in names or ():
            if not n or n.strip().lower() in self.diar.bot_names:
                continue
            p = self.participants.setdefault(n, {"first": now, "last": now, "seen": 0})
            p["last"] = now
            p["seen"] += 1
            if spoke:
                p.setdefault("spoke_first", now)
                p["spoke_last"] = now

    def _merge(self):
        with self.dlock:
            remap = self.diar.merge_pass()
        if remap:
            for e in self.raw:
                if e.get("spk") in remap:
                    e["spk"] = remap[e["spk"]]
            for p in self.paragraphs:
                if p.get("spk") in remap:
                    p["spk"] = remap[p["spk"]]

    def labels(self) -> dict:
        with self.dlock:
            lab = self.diar.labels()
        lab["ME"] = {"label": ME, "auto": ME, "color": 0, "source": "me"}
        return lab

    def rename_speaker(self, sid: str, name: str | None) -> bool:
        with self.dlock:
            ok = self.diar.rename(sid, name)
        if ok:
            self._save()
        return ok

    # ------------------------------------------------------------------ mise en forme LLM
    def _ready_lines(self) -> list[dict]:
        """Lignes transcrites pas encore mises en forme, dans l'ordre chronologique, jusqu'à la
        première phrase dont Whisper n'a pas encore rendu le texte (pour ne rien mettre dans le désordre)."""
        out = []
        for e in sorted((e for e in self.raw if not e["done"]), key=lambda e: e["t"]):
            if e["text"] is None:
                break
            out.append(e)
        return out

    async def _format_loop(self):
        try:
            while True:
                await asyncio.sleep(FORMAT_EVERY)
                self._merge()
                await self._format()
                self.maybe_analyze()
        except asyncio.CancelledError:
            pass

    async def _format(self, final: bool = False):
        async with self.fmt_lock:
            lines = self._ready_lines()
            if not final and lines and time.time() - self.t0 - lines[-1]["te"] < 2:
                lines = lines[:-1]  # la phrase en cours de prononciation attendra le prochain tour
            if not lines:
                return
            useful = [e for e in lines if e["text"]]
            if not useful:
                for e in lines:
                    e["done"] = True
                return
            lab = self.labels()
            name = lambda sid: lab.get(sid, {}).get("label", sid or "?")
            context = "\n".join(f"[{_fmt_t(p['t'])}] [{p['spk']}] {name(p['spk'])} : {p['text']}"
                                 for p in self.paragraphs[-2:])
            raw_txt = "\n".join(f"[{_fmt_t(e['t'])}] [{e['spk']}] {name(e['spk'])} : {e['text']}" for e in useful)
            user = (f"Contexte déjà mis en forme (ne pas le répéter) :\n{context}\n\n" if context else "") + \
                   f"Transcription brute à mettre en forme :\n{raw_txt}"
            paras = None
            try:
                r = await self.http.post(f"{API}/chat/completions", json={
                    "model": LLM_MODEL, "temperature": 0.2, "max_tokens": 4000, "reasoning_effort": "none",
                    "messages": [{"role": "system", "content": SYSTEM_PROMPT.format(lang=LANG, glossary=f" et ce glossaire : {GLOSSARY}" if GLOSSARY else "")},
                                 {"role": "user", "content": user}]})
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"] or ""
                m = re.search(r"\{.*\}", content, re.S)
                paras = json.loads(m.group(0))["paragraphs"] if m else None
            except Exception as e:
                self.last_error = f"Qwen : {str(e)[:150]}"
                log.warning("format: %s", e)
            valid = {e["spk"] for e in useful}
            out = []
            if paras and all(str(p.get("id")) in valid for p in paras if p.get("text")):
                for p in paras:
                    text = str(p.get("text", "")).strip()
                    if not text:
                        continue
                    t = _parse_t(p.get("t", ""))
                    out.append({"t": t if t is not None else useful[0]["t"], "spk": str(p["id"]), "text": text})
            else:  # LLM indisponible ou locuteurs mélangés : on garde le brut, regroupé par locuteur
                for e in useful:
                    if out and out[-1]["spk"] == e["spk"]:
                        out[-1]["text"] += " " + e["text"]
                    else:
                        out.append({"t": e["t"], "spk": e["spk"], "text": e["text"]})
            for p in out:
                p["id"] = len(self.paragraphs) + 1
                self.paragraphs.append(p)
            for e in lines:
                e["done"] = True
            self._save()

    # ------------------------------------------------------------------ points d'étape (analyse)
    def maybe_analyze(self, force: bool = False) -> bool:
        a = self.analysis
        if not self.active or (self.an_task and not self.an_task.done()):
            return False
        if not force and time.time() - a["last_run"] < analyst.LIVE_EVERY:
            return False
        new_words = sum(len(p["text"].split()) for p in self.paragraphs if p["t"] >= a["upto"])
        if new_words < (5 if force else analyst.LIVE_MIN_WORDS):
            if not force:
                a["last_run"] = time.time() - analyst.LIVE_EVERY + 60  # on retentera dans une minute
            return False
        a["last_run"] = time.time()
        self.an_task = asyncio.create_task(self._analyze())
        return True

    async def _analyze(self):
        a = self.analysis
        a["running"] = True
        doc = self.doc()
        since = a["upto"]
        upto = max((p["t"] for p in self.paragraphs), default=0.0) + 0.1
        try:
            res = await asyncio.wait_for(analyst.live_analysis(self.llm, doc, a["state"], since), 300)
            flash = res.pop("flash")
            if flash.get("resume") or flash.get("points"):
                a["flashes"].append({**flash, "de": since, "a": upto - 0.1,
                                     "at": dt.datetime.now().astimezone().isoformat()})
            a["state"] = res
            a["upto"], a["error"] = upto, None
            a["at"] = dt.datetime.now().astimezone().isoformat()
            a["v"] += 1
            self._save()
            log.info("point d'étape %d (%.0f s)", a["v"], res.get("_secs", 0))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            a["error"] = f"analyse : {str(e)[:150]}"
            log.warning("analyse: %s", e)
        finally:
            a["running"] = False

    def analysis_doc(self) -> dict:
        a = self.analysis
        return {**self.analysis_status(), "state": a["state"], "flashes": a["flashes"], "upto": a["upto"],
                "speakers": self.labels(), "aliases": dict(self.diar.aliases), "file": self.base.name if self.base else None}

    # ------------------------------------------------------------------ lecture / fichiers
    def snapshot(self, after: int = 0) -> dict:
        """Paragraphes mis en forme après `after` + lignes brutes en attente de mise en forme."""
        pending = [{"t": e["t"], "spk": e["spk"], "text": e["text"]}
                   for e in sorted(self.raw, key=lambda e: e["t"]) if not e["done"] and e["text"]]
        return {**self.status(), "t0": self.t0, "paragraphs": self.paragraphs[after:], "total": len(self.paragraphs),
                "pending": pending, "transcribing": self.inflight, "speakers": self.labels(),
                "aliases": dict(self.diar.aliases)}

    def slides_after(self, after: int = 0) -> dict:
        return {"file": self.base.name if self.base else None, "total": len(self.slides), "slides": self.slides[after:]}

    def doc(self) -> dict:
        a = self.analysis
        return {"meta": self.meta, "t0": self.t0, "speakers": self.labels(), "diarizer": self.diar.state(),
                "paragraphs": self.paragraphs, "slides": self.slides,
                "participants": [{"name": k, **v} for k, v in sorted(self.participants.items(), key=lambda kv: kv[1]["first"])],
                "asr_stats": self.asr_stats,
                "raw": [{k: e[k] for k in ("t", "te", "spk", "ui", "text", "conf", "suspect", "retranscrit") if k in e}
                        for e in self.raw if e["text"]],
                "analysis": {"state": a["state"], "flashes": a["flashes"], "upto": a["upto"], "at": a["at"]}}

    def _save(self):
        if self.base:
            save_doc(self.base, self.doc())


def _similar(a: str, b: str) -> bool:
    wa, wb = re.findall(r"\w+", (a or "").lower()), re.findall(r"\w+", (b or "").lower())
    if not wa or not wb:
        return False
    sa, sb = set(wa), set(wb)
    return len(sa & sb) / len(sa | sb) >= 0.6 and 0.6 <= len(wa) / len(wb) <= 1.6


def save_doc(base: Path, doc: dict):
    Path(str(base) + ".transcript.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1))
    Path(str(base) + ".md").write_text(to_markdown(doc))


def rename_in_doc(doc: dict, sid: str, name: str | None) -> bool:
    """Renomme un intervenant dans une transcription enregistrée (et recalcule les doublons « (2) »)."""
    spk = doc.get("speakers") or {}
    if sid not in spk or sid == "ME":
        return False
    name = (name or "").strip() or None
    spk[sid]["name"] = name
    seen: dict[str, int] = {}
    for k, v in spk.items():
        if k == "ME":
            continue
        base = v.get("name") or v.get("auto") or v.get("label") or k
        v["source"] = "manual" if v.get("name") else ("voice" if base.startswith("Intervenant") else "ui")
        seen[base] = seen.get(base, 0) + 1
        v["label"] = base if seen[base] == 1 else f"{base} ({seen[base]})"
    return True


def speaker_label(doc: dict, p: dict) -> str:
    return (doc.get("speakers") or {}).get(p.get("spk"), {}).get("label") or p.get("speaker") or "?"


def to_markdown(doc: dict) -> str:
    meta = doc.get("meta", {})
    lines = [f"# {meta.get('title') or 'Réunion'}", ""]
    if meta.get("started_at"):
        start = analyst.local(meta["started_at"])
        lines.append(f"*{start:%d/%m/%Y %H:%M}" + (f" · {meta['url']}" if meta.get("url") else "") + "*")
        lines.append("")
    spk = [v["label"] for k, v in (doc.get("speakers") or {}).items()
           if any(p.get("spk") == k for p in doc.get("paragraphs", []))]
    if spk:
        lines += ["Intervenants : " + ", ".join(spk), ""]
    items = [(p["t"], f"**[{_fmt_t(p['t'])}] {speaker_label(doc, p)}** : {p['text']}") for p in doc.get("paragraphs", [])]
    for s in doc.get("slides") or []:   # diapositives à leur place dans le fil de la réunion
        if s.get("repeat_of"):
            items.append((s["t"] - 0.01, f"> 🖥 [{_fmt_t(s['t'])}] retour à « {s.get('titre') or 'la diapositive'} »"))
            continue
        q = [f"> 🖥 **[{_fmt_t(s['t'])}] {s.get('titre') or 'Diapositive'}**"] + [f"> - {x}" for x in s.get("texte") or []]
        if s.get("visuel"):
            q.append(f"> _{s['visuel']}_")
        items.append((s["t"] - 0.01, "\n".join(q)))
    for _, line in sorted(items, key=lambda x: x[0]):
        lines.append(line)
        lines.append("")
    if not doc.get("paragraphs"):
        lines.append("_(aucune parole transcrite)_")
    return "\n".join(lines) + "\n"
