"""Import d'enregistrements faits ailleurs (vidéo ou son d'une réunion passée : Teams, Zoom, OBS, dictaphone…).

1. Envoi par morceaux (`write`) : un envoi coupé (réseau, assistant relancé) reprend là où il s'est arrêté ; choisir
   à nouveau le même fichier reprend aussi un envoi laissé en plan (gardé 48 h).
2. Conversion en MP4 lisible par les navigateurs, index en tête : copie sans réencodage quand c'est déjà du H.264/AAC
   (enregistrements Teams et Zoom), sinon réencodage ; un son seul reçoit une image fixe.
3. Transcription après coup (offline.py), puis compte rendu, comme une réunion enregistrée par l'assistant.
   Une même chose peut être demandée pour une vidéo déjà là, restée sans transcription.

Une seule conversion ou transcription à la fois par assistant, en basse priorité (nice) : une réunion en cours sur
le même hôte garde la main. Les travaux sont notés sur disque (<enregistrements>/.imports/) et reprennent après un
redémarrage de l'assistant.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import re
import secrets
import shutil
import sys
import time
import unicodedata
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

log = logging.getLogger("import")

MAX_SIZE = int(float(os.environ.get("IMPORT_MAX_GB", "20")) * 1024 ** 3)
CHUNK_MAX = 64 * 1024 ** 2
MARGIN = 2 * 1024 ** 3            # place gardée libre sur le disque des enregistrements
KEEP_UPLOAD = 48 * 3600           # envoi interrompu : gardé 48 h pour reprendre
KEEP_DONE = 7 * 86400             # travaux finis ou en erreur : affichés 7 jours
NICE = ["nice", "-n", "15"]
VIDEO_EXT = {"mp4", "m4v", "mov", "mkv", "webm", "avi", "wmv", "flv", "mpg", "mpeg", "ts", "mts", "m2ts", "3gp", "ogv"}
AUDIO_EXT = {"mp3", "m4a", "aac", "wav", "ogg", "oga", "opus", "flac", "wma", "amr", "weba"}
ACTIVE = ("attente", "conversion", "transcription", "compte rendu")
PLATFORMS = {"teams", "zoom", "web"}
UPLOAD_BUSY = 180                 # un morceau reçu il y a moins de 3 min : envoi en cours (pas de mise à jour)


class UserError(Exception):
    """Erreur montrée telle quelle à l'utilisateur."""


class Cancelled(Exception):
    pass


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()     # « Comité » -> « Comite »
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")
    return s[:60] or "reunion"


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


async def probe(path: Path) -> dict:
    p = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await p.communicate()
    if p.returncode != 0:
        why = (err.decode(errors="replace").strip().splitlines() or ["?"])[-1].replace(str(path) + ": ", "")
        raise UserError(f"fichier illisible : ce n'est pas une vidéo ou un son reconnu ({why[:120]})")
    return json.loads(out or b"{}")


def streams(info: dict) -> tuple[dict | None, dict | None, float]:
    """(piste vidéo, piste son, durée en secondes) ; la pochette d'un MP3 ou d'un M4A n'est pas une vidéo."""
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = 0.0
    for x in (info.get("format") or {}, a or {}, v or {}):
        try:
            dur = dur or float(x.get("duration") or 0)
        except ValueError:
            pass
    return v, a, dur


def convert_cmd(src: Path, dst: Path, v: dict | None, a: dict, encode: bool = False, dur: float = 0.0,
                trim: float | None = None, mutes: list | None = None) -> tuple[list[str], str]:
    """Commande ffmpeg vers un MP4 lisible partout (H.264 + AAC, index en tête) et ce qu'elle fait.
    `encode` : tout réencoder (la simple copie a échoué) ; `dur` : durée gardée ; `trim` : fin coupée à partir de
    cette seconde ; `mutes` : plages [début, fin] dont le son est effacé (passages privés)."""
    base = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
    acopy = a.get("codec_name") == "aac" and not encode and not mutes
    aopt = ["-c:a", "copy"] if acopy else ["-c:a", "aac", "-b:a", "160k"]
    if mutes:
        expr = "+".join(f"between(t,{x:.3f},{y:.3f})" for x, y in mutes)
        aopt = ["-af", f"volume=enable='{expr}':volume=0"] + aopt
    tail = ["-t", f"{trim:.3f}"] if trim is not None else []
    if v is None:                 # son seul : image fixe sombre, une image par seconde
        cmd = base + ["-i", str(src), "-f", "lavfi", "-i", f"color=c=0x161b26:s=1280x720:r=1:d={max(1.0, dur):.2f}",
                      "-map", "1:v", "-map", "0:a:0", "-shortest",
                      "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-crf", "35",
                      "-pix_fmt", "yuv420p", "-g", "10", *aopt]
        what = "mise en forme du son"
    else:
        vcopy = v.get("codec_name") == "h264" and v.get("pix_fmt") in ("yuv420p", "yuvj420p") and not encode
        vopt = ["-c:v", "copy"] if vcopy else [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
            "-vf", "scale='min(1920,iw)':-2"]
        cmd = base + ["-i", str(src), "-map", "0:v:0", "-map", "0:a:0", *vopt, *aopt]
        what = "copie de la vidéo" if vcopy and acopy else "conversion de la vidéo"
    if mutes or trim is not None:
        what += " (passages privés coupés)"
    return cmd + tail + ["-movflags", "+faststart", "-f", "mp4", str(dst)], what


class Importer:
    def __init__(self, rec_dir, worker: str = "", worker_dir: Path | None = None,
                 launch_report=None, report_state=None, live_stem=None):
        self.rec = Path(rec_dir)
        self.dir = self.rec / ".imports"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.worker = worker
        self.worker_dir = Path(worker_dir or Path(__file__).resolve().parent)
        self.launch_report = launch_report or (lambda stem: False)       # stem -> bool (lancé)
        self.report_state = report_state or (lambda stem: None)          # stem -> {state, step, error} | None
        self.live_stem = live_stem or (lambda: None)                     # réunion en cours d'enregistrement
        self.offline_cmd = [sys.executable, str(self.worker_dir / "offline.py")]
        self.jobs: dict[str, dict] = {}
        self.queue: asyncio.Queue | None = None
        self.current: str | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        for f in self.dir.glob("*.json"):
            try:
                j = json.loads(f.read_text())
                self.jobs[j["id"]] = j
            except (ValueError, KeyError, OSError):
                log.warning("travail d'import illisible : %s", f.name)
        for t in self.dir.glob("*.mp4.tmp"):          # conversion interrompue : reprise depuis le fichier reçu
            t.unlink(missing_ok=True)

    # ------------------------------------------------------------------ état
    def _save(self, j: dict):
        j["updated"] = time.time()
        tmp = self.dir / (j["id"] + ".json.tmp")
        tmp.write_text(json.dumps(j, ensure_ascii=False, indent=1))
        tmp.replace(self.dir / (j["id"] + ".json"))

    def _set(self, j: dict, **kw):
        j.update(kw)
        self._save(j)

    def _part(self, j: dict) -> Path:
        return self.dir / (j["id"] + ".part")

    def _forget(self, j: dict):
        for p in (self._part(j), self.dir / (j["id"] + ".json"), self.dir / (j["id"] + ".log"),
                  self.dir / (j["id"] + ".mp4.tmp")):
            p.unlink(missing_ok=True)
        self.jobs.pop(j["id"], None)

    @staticmethod
    def public(j: dict | None) -> dict | None:
        if not j:
            return None
        keys = ("id", "kind", "owner", "file", "size", "last_modified", "received", "title", "started_at", "platform", "transcribe", "cuts",
                "stage", "step", "pct", "error", "note", "stem", "duration", "audio_only", "created", "updated")
        return {k: j.get(k) for k in keys}

    def get(self, jid: str, owner: str | None = None) -> dict:
        j = self.jobs.get(jid)
        if not j:
            raise HTTPException(404, "import inconnu")
        if owner is not None and j.get("owner") != owner:
            raise HTTPException(403, "import d'un autre utilisateur")
        return j

    def listing(self, owner: str | None = None) -> list[dict]:
        return sorted((self.public(j) for j in self.jobs.values() if owner is None or j.get("owner") == owner),
                      key=lambda x: -(x.get("created") or 0))

    def for_stem(self, stem: str) -> dict | None:
        return next((j for j in self.jobs.values() if j.get("stem") == stem and j["stage"] in ACTIVE), None)

    def busy_stems(self) -> tuple:
        return tuple(j["stem"] for j in self.jobs.values() if j.get("stem") and j["stage"] in ACTIVE)

    def status(self) -> dict:
        now = time.time()
        active = sum(j["stage"] in ACTIVE for j in self.jobs.values())
        uploading = sum(j["stage"] == "upload" and now - j.get("updated", 0) < UPLOAD_BUSY for j in self.jobs.values())
        return {"active": active, "uploading": uploading, "busy": bool(active or uploading)}

    # ------------------------------------------------------------------ envoi
    def create(self, owner: str, me_name: str | None, file: str, size: int, last_modified: int | None = None,
               title: str | None = None, started_at: str | None = None, platform: str | None = None,
               transcribe: bool = True, cuts: list | None = None, notes: str | None = None) -> dict:
        file = os.path.basename(file or "").strip()[:200]
        ext = file.rsplit(".", 1)[-1].lower() if "." in file else ""
        if ext not in VIDEO_EXT | AUDIO_EXT:
            raise UserError(f"format non pris en charge (.{ext or '?'}) : vidéo (MP4, MOV, MKV, WebM, AVI…) "
                               "ou son (MP3, M4A, WAV, OGG…)")
        if not 0 < size <= MAX_SIZE:
            raise UserError(f"fichier trop gros (au plus {MAX_SIZE // 1024 ** 3} Go)" if size > 0 else "fichier vide")
        if started_at:
            try:
                dt.datetime.fromisoformat(started_at)
            except ValueError:
                raise UserError("date de la réunion illisible")
        platform = platform if platform in PLATFORMS else "web"
        title = (title or "").strip()[:200] or None
        cuts = check_cuts(cuts)
        notes = (notes or "").strip()[:4000] or None
        for j in self.jobs.values():                  # même fichier, même personne, envoi pas fini : on reprend
            if (j["stage"] == "upload" and j.get("owner") == owner and j["file"] == file and j["size"] == size
                    and j.get("last_modified") == last_modified):
                self._set(j, title=title or j.get("title"), started_at=started_at or j.get("started_at"),
                          platform=platform, transcribe=transcribe, me_name=me_name, cuts=cuts, notes=notes)
                return j
        free = shutil.disk_usage(self.rec).free
        if free < 2.1 * size + MARGIN:                # fichier reçu + vidéo convertie, et de la marge
            raise UserError(f"place insuffisante sur l'assistant {self.worker} ({free / 1024 ** 3:.1f} Go libres)")
        j = {"id": secrets.token_hex(8), "kind": "upload", "owner": owner, "me_name": me_name, "file": file,
             "size": size, "last_modified": last_modified, "received": 0, "title": title, "started_at": started_at,
             "platform": platform, "transcribe": transcribe, "cuts": cuts, "notes": notes,
             "stage": "upload", "step": None, "pct": 0,
             "error": None, "note": None, "stem": None, "created": time.time()}
        self._part(j).write_bytes(b"")
        self.jobs[j["id"]] = j
        self._save(j)
        log.info("import %s : %s (%d octets) pour %s", j["id"], file, size, owner)
        return j

    def write(self, jid: str, owner: str | None, offset: int, data: bytes) -> dict:
        j = self.get(jid, owner)
        if j["stage"] != "upload":
            raise HTTPException(409, {"message": "envoi déjà terminé", "received": j["received"]})
        if offset != j["received"]:
            raise HTTPException(409, {"message": "morceau hors séquence", "received": j["received"]})
        if offset + len(data) > j["size"]:
            raise HTTPException(400, "plus de données que la taille annoncée")
        p = self._part(j)
        if not p.exists() or p.stat().st_size < offset:
            self._set(j, received=p.stat().st_size if p.exists() else 0)
            raise HTTPException(409, {"message": "fichier reçu incomplet", "received": j["received"]})
        with open(p, "r+b") as f:
            f.seek(offset)
            f.write(data)
            f.truncate()
        self._set(j, received=offset + len(data), pct=round(100 * (offset + len(data)) / j["size"], 1))
        return j

    def finish(self, jid: str, owner: str | None) -> dict:
        j = self.get(jid, owner)
        if j["stage"] != "upload":
            return j
        if j["received"] != j["size"] or self._part(j).stat().st_size != j["size"]:
            raise HTTPException(409, {"message": "fichier pas entièrement reçu", "received": j["received"]})
        self._set(j, stage="attente", step="en attente", pct=None)
        self._enqueue(j)
        return j

    def transcribe_existing(self, stem: str, owner: str | None, me_name: str | None, force: bool = False) -> dict:
        """Transcription après coup d'une vidéo ; `force` : la refaire (transcription incomplète, par exemple)."""
        mp4 = self.rec / (stem + ".mp4")
        if not mp4.exists():
            raise HTTPException(404, "vidéo introuvable")
        if (self.rec / (stem + ".transcript.json")).exists() and not force:
            raise HTTPException(409, "cette réunion a déjà une transcription")
        if self.for_stem(stem):
            raise HTTPException(409, "transcription déjà demandée")
        if self.live_stem() == stem:
            raise HTTPException(409, "enregistrement en cours")
        try:
            meta = json.loads(Path(str(mp4) + ".json").read_text())
        except (OSError, ValueError):
            meta = {}
        j = {"id": secrets.token_hex(8), "kind": "transcribe", "owner": owner, "me_name": me_name, "file": mp4.name,
             "size": mp4.stat().st_size, "received": None, "title": meta.get("title"), "started_at": meta.get("started_at"),
             "platform": meta.get("platform"), "transcribe": True, "stage": "attente", "step": "en attente",
             "pct": None, "error": None, "note": None, "stem": stem, "audio_only": bool(meta.get("audio_only")),
             "created": time.time(), "tr_started": bool(force)}           # refaite : offline.py --force
        self.jobs[j["id"]] = j
        self._save(j)
        self._enqueue(j)
        log.info("transcription après coup demandée : %s (%s)", stem, owner)
        return j

    def retry(self, jid: str, owner: str | None) -> dict:
        j = self.get(jid, owner)
        if j["stage"] not in ("erreur", "annulé"):
            raise HTTPException(409, "rien à reprendre")
        if not j.get("stem") and not self._part(j).exists():
            raise HTTPException(409, "fichier reçu effacé : importez-le de nouveau")
        if j.get("stem") and not (self.rec / (j["stem"] + ".mp4")).exists():
            raise HTTPException(409, "vidéo supprimée entre-temps")
        self._set(j, stage="attente", step="en attente", pct=None, error=None, note=None)
        self._enqueue(j)
        return j

    async def cancel(self, jid: str, owner: str | None) -> dict:
        """Abandonne un envoi ou un travail ; une vidéo déjà convertie reste (sans la transcription partielle)."""
        j = self.get(jid, owner)
        if j["stage"] in ACTIVE:
            j["cancel"] = True
            if self.current == jid and self.proc and self.proc.returncode is None:
                self.proc.kill()
            if self.current != jid:
                self._cleanup_cancel(j)
            for _ in range(100):
                if self.current != jid:
                    break
                await asyncio.sleep(0.1)
        if j["stage"] in ("upload", "erreur", "annulé", "terminé") or not j.get("stem"):
            self._forget(j)
        return {"ok": True}

    def _cleanup_cancel(self, j: dict):
        if j.get("tr_started") and j.get("stem"):     # transcription partielle : retirée
            for ext in (".transcript.json", ".md"):
                (self.rec / (j["stem"] + ext)).unlink(missing_ok=True)
        self._set(j, stage="annulé", step=None, pct=None, cancel=False)

    # ------------------------------------------------------------------ travaux
    def start(self):
        """Au démarrage de l'assistant : reprend les travaux interrompus, nettoie les vieux."""
        self.queue = asyncio.Queue()
        self.purge()
        for j in sorted(self.jobs.values(), key=lambda x: x.get("created") or 0):
            if j["stage"] in ACTIVE:
                log.info("import %s repris (%s)", j["id"], j["stage"])
                self._enqueue(j)
        self._task = asyncio.create_task(self._run())

    def purge(self):
        now = time.time()
        for j in list(self.jobs.values()):
            age = now - (j.get("updated") or j.get("created") or now)
            if j["stage"] == "upload" and age > KEEP_UPLOAD:
                log.info("envoi %s abandonné depuis 48 h : effacé", j["id"])
                self._forget(j)
            elif j["stage"] in ("terminé", "erreur", "annulé") and age > KEEP_DONE:
                self._forget(j)
            elif self._part(j).exists() and (j["stage"] in ("terminé", "annulé")
                                             or j["stage"] == "erreur" and age > KEEP_UPLOAD):
                self._part(j).unlink(missing_ok=True)          # fichier reçu : gardé 48 h pour « Réessayer »

    def _enqueue(self, j: dict):
        if self.queue is not None:
            self.queue.put_nowait(j["id"])

    async def _run(self):
        last_purge = time.time()
        while True:
            jid = await self.queue.get()
            if time.time() - last_purge > 3600:
                self.purge()
                last_purge = time.time()
            j = self.jobs.get(jid)
            if not j or j["stage"] not in ACTIVE:
                continue
            self.current = jid
            try:
                if j["kind"] == "upload" and not j.get("stem"):
                    await self._convert(j)
                if j["transcribe"]:
                    if j["stage"] in ("attente", "conversion", "transcription"):
                        await self._transcribe(j)
                    await self._report(j)
                self._set(j, stage="terminé", step=None, pct=None)
                log.info("import %s terminé : %s", jid, j.get("stem"))
            except Cancelled:
                self._cleanup_cancel(j)
                log.info("import %s annulé", jid)
            except UserError as e:
                self._set(j, stage="erreur", step=None, pct=None, error=str(e)[:300])
                log.warning("import %s : %s", jid, e)
            except Exception as e:
                log.exception("import %s", jid)
                self._set(j, stage="erreur", step=None, pct=None, error=f"{type(e).__name__} : {e}"[:300])
            finally:
                self.current, self.proc = None, None

    def _check_cancel(self, j: dict):
        if j.get("cancel"):
            raise Cancelled()

    def _stem_for(self, started: dt.datetime, title: str) -> str:
        base = f"{started.astimezone():%Y%m%d-%H%M%S}_{_slug(title)}"
        stem, n = base, 1
        while any(self.rec.glob(glob_escape(stem) + ".*")):
            n += 1
            stem = f"{base}-{n}"
        return stem

    async def _convert(self, j: dict):
        part = self._part(j)
        if not part.exists():
            raise UserError("fichier reçu introuvable : importez-le de nouveau")
        self._set(j, stage="conversion", step="lecture du fichier", pct=0)
        info = await probe(part)
        v, a, dur = streams(info)
        if not a:
            raise UserError("aucune piste son dans ce fichier : rien à transcrire")
        if dur < 5:
            raise UserError("enregistrement trop court (moins de 5 secondes)")
        try:
            started = dt.datetime.fromisoformat(j["started_at"]) if j.get("started_at") else None
        except ValueError:
            started = None
        if started and started.tzinfo is None:
            started = started.astimezone()
        started = started or dt.datetime.now().astimezone() - dt.timedelta(seconds=dur)
        title = j.get("title") or re.sub(r"[_]+", " ", j["file"].rsplit(".", 1)[0]).strip() or "Réunion importée"
        cuts = j.get("cuts") or []
        trim = min((x for x, y in cuts if y is None), default=None)
        trim = trim if trim is not None and trim < dur else None
        mutes = [(x, min(y, trim or y)) for x, y in cuts if y is not None and x < (trim if trim is not None else dur)]
        keep = trim if trim is not None else dur
        if keep < 5:
            raise UserError("il ne reste presque rien après les coupures demandées")
        tmp = self.dir / (j["id"] + ".mp4.tmp")
        cmd, what = convert_cmd(part, tmp, v, a, dur=keep, trim=trim, mutes=mutes)
        self._set(j, step=what, duration=round(keep, 1), audio_only=v is None)
        try:
            await self._ffmpeg(j, cmd, keep)
        except UserError as e:
            if "-c:v" not in cmd or "copy" not in cmd:
                raise
            log.info("import %s : copie impossible (%s), réencodage", j["id"], e)
            cmd, what = convert_cmd(part, tmp, v, a, encode=True, dur=keep, trim=trim, mutes=mutes)
            self._set(j, step=what, pct=0)
            await self._ffmpeg(j, cmd, keep)
        out = await probe(tmp)
        _, a2, d2 = streams(out)
        if not a2 or d2 < keep - max(5.0, 0.02 * keep):
            tmp.unlink(missing_ok=True)
            raise UserError(f"conversion incomplète ({d2:.0f} s sur {keep:.0f} s)")
        self._check_cancel(j)
        ended = started + dt.timedelta(seconds=min(d2, keep))     # durée du fichier d'origine (après coupure)
        stem = self._stem_for(started, title)
        meta = {"title": title, "platform": j.get("platform") or "web", "worker": self.worker, "owner": j["owner"],
                "started_at": started.astimezone().isoformat(timespec="seconds"),
                "ended_at": ended.astimezone().isoformat(timespec="seconds"),
                "imported": {"file": j["file"], "size": j["size"], "at": _now(), "by": j["owner"]}}
        if v is None:
            meta["audio_only"] = True
        if trim is not None or mutes:               # passages privés : son effacé, fin coupée (rien n'en reste)
            meta["cuts"] = [{"from": round(x, 1), "to": round(y, 1)} for x, y in mutes] + (
                [{"from": round(trim, 1), "to": None, "original_duration": round(dur, 1)}] if trim is not None else [])
        if j.get("notes"):
            meta["cr_notes"] = j["notes"]
        mp4 = self.rec / (stem + ".mp4")
        os.replace(tmp, mp4)
        mj = Path(str(mp4) + ".json")
        mj.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
        for p in (mp4, mj):                           # rangée à sa date dans la liste des réunions
            os.utime(p, (ended.timestamp(), ended.timestamp()))
        part.unlink(missing_ok=True)
        self._set(j, stem=stem, title=title, started_at=meta["started_at"], step=None,
                  stage="transcription" if j["transcribe"] else "terminé", pct=None if not j["transcribe"] else 0)
        log.info("import %s : %s (%s, %.0f s)", j["id"], mp4.name, what, d2)

    async def _ffmpeg(self, j: dict, cmd: list[str], dur: float):
        self.proc = await asyncio.create_subprocess_exec(*NICE, *cmd, stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.PIPE)
        err = asyncio.create_task(self.proc.stderr.read())
        last = 0.0
        async for line in self.proc.stdout:
            m = re.match(rb"out_time_(?:us|ms)=(\d+)", line)
            if m and dur and time.monotonic() - last > 2:
                last = time.monotonic()
                self._set(j, pct=min(99.0, round(int(m.group(1)) / 1e6 / dur * 100, 1)))
        rc = await self.proc.wait()
        e = (await err).decode(errors="replace").strip()
        self._check_cancel(j)
        if rc != 0:
            raise UserError("conversion impossible : " + ((e.splitlines() or ["?"])[-1][:200]))

    async def _transcribe(self, j: dict):
        stem = j["stem"]
        mp4 = self.rec / (stem + ".mp4")
        if not mp4.exists():
            raise UserError("vidéo supprimée entre-temps")
        if not j.get("duration"):
            _, _, dur = streams(await probe(mp4))
            j["duration"] = round(dur, 1)
        tr = self.rec / (stem + ".transcript.json")
        resume = bool(j.get("tr_started"))
        if tr.exists() and not resume:
            raise UserError("cette réunion a déjà une transcription")
        self._set(j, stage="transcription", step="préparation", pct=0, tr_started=True)
        cmd = [*NICE, *self.offline_cmd, stem + ".mp4"] + (["--force"] if resume else [])
        if j.get("me_name"):
            cmd += ["--me", j["me_name"]]
        if j.get("audio_only"):
            cmd += ["--no-video", "--no-slides"]
        env = {**os.environ, "RECORDINGS_DIR": str(self.rec), "WORKER_DIR": str(self.worker_dir), "PYTHONUNBUFFERED": "1"}
        logf = open(self.dir / (j["id"] + ".log"), "ab")
        tail: list[str] = []
        total = max(1.0, (j.get("duration") or 60) / 60)
        try:
            self.proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                             stderr=asyncio.subprocess.STDOUT, env=env)
            async for raw in self.proc.stdout:
                logf.write(raw)
                line = raw.decode(errors="replace").rstrip()
                tail = (tail + [line])[-8:]
                if m := re.search(r"… image (\d+) min", line):
                    self._set(j, step=f"lecture des images : {m.group(1)} / {total:.0f} min",
                              pct=round(min(30, 30 * int(m.group(1)) / total), 1))
                elif m := re.search(r"… son (\d+) min", line):
                    self._set(j, step=f"transcription : {m.group(1)} / {total:.0f} min",
                              pct=round(min(95, 30 + 65 * int(m.group(1)) / total), 1))
                elif "terminé en" in line:
                    self._set(j, step="dernière passe sur les voix", pct=96)
            rc = await self.proc.wait()
        finally:
            logf.close()
        self._check_cancel(j)
        if rc != 0 or not tr.exists():
            why = next((x for x in reversed(tail) if x.strip()), "?")
            raise UserError(f"transcription impossible : {why[-200:]}")
        self._set(j, step=None, pct=97)

    async def _report(self, j: dict):
        stem = j["stem"]
        self._set(j, stage="compte rendu", step="rédaction du compte rendu", pct=98)
        st = self.report_state(stem)
        if not (st and st.get("state") == "running") and not self.launch_report(stem):
            st = self.report_state(stem)
            if not st:
                self._set(j, note="compte rendu non lancé (rédaction désactivée sur cet assistant)")
                return
        while (st := self.report_state(stem)) and st.get("state") == "running":
            self._check_cancel(j)
            if st.get("step") and st["step"] != j.get("step"):
                self._set(j, step=st["step"])
            await asyncio.sleep(2)
        if st and st.get("state") in ("error", "skipped"):
            self._set(j, note=f"compte rendu : {st.get('error') or st['state']}")


def check_cuts(cuts) -> list:
    """Plages à couper : [[début, fin], …] en secondes depuis le début du fichier ; fin vide (None) : jusqu'au bout."""
    out = []
    for c in cuts or []:
        try:
            x = float(c[0])
            y = None if len(c) < 2 or c[1] is None else float(c[1])
        except (TypeError, ValueError, IndexError):
            raise UserError(f"plage à couper illisible : {c!r}")
        if x < 0 or (y is not None and y <= x):
            raise UserError(f"plage à couper incorrecte : {c!r}")
        out.append([x, y])
    if len(out) > 50:
        raise UserError("trop de plages à couper (50 au plus)")
    return sorted(out, key=lambda c: c[0])


def glob_escape(s: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", s)


# ----------------------------------------------------------------------------- API de l'assistant
class ImportReq(BaseModel):
    owner: str
    me_name: str | None = None
    file: str
    size: int
    last_modified: int | None = None
    title: str | None = None
    started_at: str | None = None
    platform: str | None = None
    transcribe: bool = True
    cuts: list[list[float | None]] | None = None
    notes: str | None = None


class TranscribeReq(BaseModel):
    owner: str | None = None
    me_name: str | None = None
    force: bool = False


def router(imp: Importer, deps: list) -> APIRouter:
    r = APIRouter(dependencies=deps)

    def _user_err(e: UserError):
        return HTTPException(507 if "place insuffisante" in str(e) else 400, str(e))

    @r.post("/imports")
    async def create(req: ImportReq):
        try:
            return imp.public(imp.create(**req.model_dump()))
        except UserError as e:
            raise _user_err(e)

    @r.get("/imports")
    async def list_(owner: str | None = None):
        return imp.listing(owner)

    @r.get("/imports/{jid}")
    async def one(jid: str, owner: str | None = None):
        return imp.public(imp.get(jid, owner))

    @r.put("/imports/{jid}")
    async def chunk(jid: str, request: Request, offset: int, owner: str | None = None):
        data = await request.body()
        if len(data) > CHUNK_MAX:
            raise HTTPException(413, "morceau trop gros")
        return imp.public(await asyncio.to_thread(imp.write, jid, owner, offset, data))

    @r.post("/imports/{jid}/finish")
    async def finish(jid: str, owner: str | None = None):
        return imp.public(imp.finish(jid, owner))

    @r.post("/imports/{jid}/retry")
    async def retry(jid: str, owner: str | None = None):
        return imp.public(imp.retry(jid, owner))

    @r.delete("/imports/{jid}")
    async def cancel(jid: str, owner: str | None = None):
        return await imp.cancel(jid, owner)

    @r.post("/recordings/{name}/transcribe")
    async def transcribe(name: str, req: TranscribeReq):
        stem = os.path.basename(name).removesuffix(".mp4")
        return imp.public(imp.transcribe_existing(stem, req.owner, req.me_name, req.force))

    return r
