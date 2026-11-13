"""Enregistrement écran (Xvfb) + audio réunion + voix de l'utilisateur via ffmpeg."""
import asyncio
import datetime as dt
import json
import logging
import os
import re
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("recorder")


def _top_atoms(path: Path, limit: int = 4) -> list[str]:
    out, pos, size = [], 0, path.stat().st_size
    with open(path, "rb") as f:
        while pos + 8 <= size and len(out) < limit:
            f.seek(pos)
            h = f.read(16)
            n, t = struct.unpack(">I4s", h[:8])
            if n == 1:
                n = struct.unpack(">Q", h[8:16])[0]
            out.append(t.decode("latin-1"))
            if n < 8:
                break
            pos += n
    return out


def _packets(path: Path) -> list[str] | None:
    r = subprocess.run(["ffprobe", "-v", "error", "-count_packets", "-show_entries", "stream=codec_type,nb_read_packets",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=600)
    return r.stdout.split() if r.returncode == 0 and r.stdout.strip() else None


def faststart(path: Path) -> str:
    """MP4 fragmenté (sûr pendant l'enregistrement : lisible même si ffmpeg est tué) → MP4 classique, index en tête.

    Le navigateur connaît alors la durée tout de suite et saute à n'importe quel moment : sur une réunion de 1 h 23,
    0,6 s au lieu de 34 s avant de pouvoir lire à partir d'un horodatage. Copie sans réencodage, vérifiée paquet
    par paquet avant de remplacer le fichier (remplacement atomique, date du fichier conservée).
    Renvoie « ok », « déjà » (rien à faire) ou la raison de l'échec (le fichier d'origine est alors gardé tel quel)."""
    try:
        atoms = _top_atoms(path)
    except (OSError, struct.error) as e:
        return f"illisible ({e})"
    if "moov" in atoms and "moof" not in atoms and atoms.index("moov") < (atoms.index("mdat") if "mdat" in atoms else 99):
        return "déjà"
    tmp = path.with_name(path.name + ".faststart-tmp")
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(path), "-map", "0", "-c", "copy",
                            "-movflags", "+faststart", "-f", "mp4", str(tmp)], capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            return "ffmpeg : " + (r.stderr.strip().splitlines() or ["?"])[-1][:200]
        a, b = _packets(path), _packets(tmp)
        if not a or a != b:
            return f"vérification : paquets {a} ≠ {b}"
        st = path.stat()
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, path)
        return "ok"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"erreur : {e}"
    finally:
        tmp.unlink(missing_ok=True)


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s or "").strip("-")
    return s[:60] or "reunion"


class Recorder:
    def __init__(self, record_dir: str, display: str, size: str,
                 fps: float = float(os.environ.get("RECORD_FPS", "2")),
                 crf: int = int(os.environ.get("RECORD_CRF", "32")),
                 audio_bitrate: str = os.environ.get("RECORD_AUDIO_BITRATE", "192k")):
        self.crf = crf
        self.audio_bitrate = audio_bitrate
        self.dir = Path(record_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.display = display
        self.size = size
        self.fps = fps
        self.proc = None
        self.file: Path | None = None
        self.meta: dict = {}
        self.started_at: dt.datetime | None = None
        self._finalizing: asyncio.Task | None = None
        for t in self.dir.glob("*.faststart-tmp"):   # conversion interrompue (redémarrage) : l'original est intact
            t.unlink(missing_ok=True)

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self, title: str, meta: dict):
        if self.active:
            return self.file.name
        self.started_at = dt.datetime.now().astimezone()
        name = f"{self.started_at:%Y%m%d-%H%M%S}_{_slug(title)}.mp4"
        self.file = self.dir / name
        self.meta = {**meta, "title": title, "started_at": self.started_at.isoformat()}
        gop = max(2, round(self.fps * 10))  # une image clé toutes les ~10 s (navigation dans la vidéo)
        pulse_in = ["-thread_queue_size", "4096", "-f", "pulse", "-sample_rate", "48000", "-channels", "2",
                    "-fragment_size", "3840"]
        cmd = [
            "ffmpeg", "-y", "-loglevel", "warning", "-nostdin",
            # Vidéo : 2 i/s par défaut, compressée au maximum (contenu quasi statique : partage d'écran, visages)
            "-thread_queue_size", "64", "-f", "x11grab", "-draw_mouse", "0",
            "-framerate", str(self.fps), "-video_size", self.size, "-i", self.display,
            # Audio : son de la réunion + ta voix, 48 kHz stéréo
            *pulse_in, "-i", "meeting_out.monitor",
            *pulse_in, "-i", "user_in.monitor",
            "-filter_complex",
            "[1:a]aresample=async=1:first_pts=0[a1];[2:a]aresample=async=1:first_pts=0[a2];"
            "[a1][a2]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.97:level=false[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "libx264", "-preset", "slow", "-tune", "stillimage", "-crf", str(self.crf),
            "-pix_fmt", "yuv420p", "-g", str(gop), "-bf", "0",
            "-c:a", "aac", "-b:a", self.audio_bitrate, "-ar", "48000", "-ac", "2",
            # MP4 fragmenté : le fichier reste lisible même si ffmpeg est tué
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            str(self.file),
        ]
        log.info("recording -> %s", self.file)
        self.proc = await asyncio.create_subprocess_exec(*cmd, stderr=open(str(self.file) + ".log", "w"))
        self._write_meta()
        return name

    async def stop(self, finalize: bool = True):
        if not self.active:
            return None
        self.proc.send_signal(signal.SIGINT)
        try:
            await asyncio.wait_for(self.proc.wait(), 15)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()
        self.meta["ended_at"] = dt.datetime.now().astimezone().isoformat()
        self._write_meta()
        name = self.file.name
        self.proc = None
        if finalize:   # en tâche de fond : ne retarde ni la sortie de la réunion ni la transcription
            self._finalizing = asyncio.create_task(self._faststart(self.file))
        return name

    async def _faststart(self, f: Path):
        """Lecture à partir d'un horodatage : index en tête (quelques secondes, sans réencodage)."""
        t0 = time.monotonic()
        try:
            res = await asyncio.to_thread(faststart, f)
        except Exception as e:   # le fichier fragmenté reste lisible : rien n'est perdu
            res = f"erreur : {e}"
        log.info("faststart %s : %s (%.1f s)", f.name, res, time.monotonic() - t0)

    def _write_meta(self):
        if self.file:
            Path(str(self.file) + ".json").write_text(json.dumps(self.meta, ensure_ascii=False, indent=1))

    @staticmethod
    def stem(name: str) -> str:
        name = os.path.basename(name)
        for ext in (".transcript.json", ".report.json", ".mp4", ".md"):
            if name.endswith(ext):
                return name[: -len(ext)]
        return name

    def transcript_path(self, name: str, kind: str = "json") -> Path | None:
        p = self.dir / (self.stem(name) + (".transcript.json" if kind == "json" else ".md"))
        return p if p.exists() else None

    def list(self) -> list[dict]:
        out = []
        # transcriptions sans vidéo (réunion transcrite sans enregistrement)
        for t in self.dir.glob("*.transcript.json"):
            if (self.dir / (self.stem(t.name) + ".mp4")).exists():
                continue
            try:
                meta = json.loads(t.read_text()).get("meta", {})
            except ValueError:
                meta = {}
            st = t.stat()
            out.append({"name": t.name, "kind": "transcript", "transcript": True, "size": st.st_size,
                        "mtime": st.st_mtime, "meta": meta, "recording": False,
                        "duration": self._duration(meta, False)})
        for f in sorted(self.dir.glob("*.mp4"), reverse=True):
            meta = {}
            mf = Path(str(f) + ".json")
            if mf.exists():
                try:
                    meta = json.loads(mf.read_text())
                except ValueError:
                    pass
            st = f.stat()
            live = self.active and self.file == f
            out.append({"name": f.name, "kind": "video", "size": st.st_size, "mtime": st.st_mtime, "meta": meta,
                        "recording": live, "duration": self._duration(meta, live),
                        "transcript": (self.dir / (self.stem(f.name) + ".transcript.json")).exists()})
        return sorted(out, key=lambda x: -x["mtime"])

    def path(self, name: str) -> Path | None:
        p = (self.dir / os.path.basename(name))
        return p if p.suffix == ".mp4" and p.exists() else None

    def delete(self, name: str, busy_stems: tuple = ()) -> bool:
        """Supprime la vidéo et/ou la transcription associée (sauf si en cours)."""
        stem = self.stem(name)
        if stem in busy_stems or (self.active and self.stem(self.file.name) == stem):
            return False
        p = self.path(name)
        t = self.transcript_path(name)
        if not p and not t:
            return False
        extras = [self.dir / (stem + ".transcript.json"), self.dir / (stem + ".md"), self.dir / (stem + ".report.json"),
                  self.dir / (stem + ".speakerdebug.json"), self.dir / (stem + ".cr-final.md"),
                  self.dir / (stem + ".cr-final.pdf"), self.dir / (stem + ".cr-final.json")]
        if p:
            extras += [p, Path(str(p) + ".json"), Path(str(p) + ".log"), Path(str(p) + ".jpg")]
        extras += list(self.dir.glob(f"{stem}.slide-*.jpg"))   # captures des diapositives
        for extra in extras:
            extra.unlink(missing_ok=True)
        return True

    @staticmethod
    def _duration(meta: dict, live: bool) -> float | None:
        try:
            start = dt.datetime.fromisoformat(meta["started_at"])
            end = dt.datetime.now().astimezone() if live else dt.datetime.fromisoformat(meta["ended_at"])
            return round((end - start).total_seconds())
        except (KeyError, ValueError, TypeError):
            return None

    def rename(self, name: str, title: str) -> bool:
        """Change le titre affiché (métadonnées), pas le nom du fichier."""
        return self.update_meta(name, {"title": title})

    def update_meta(self, name: str, fields: dict) -> bool:
        """Met à jour les métadonnées (titre, propriétaire, partage) de la vidéo et de sa transcription."""
        t = self.transcript_path(name)
        if t:
            try:
                doc = json.loads(t.read_text())
                doc.setdefault("meta", {}).update(fields)
                t.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
            except ValueError:
                pass
        p = self.path(name)
        if not p:
            return bool(t)
        mf = Path(str(p) + ".json")
        meta = json.loads(mf.read_text()) if mf.exists() else {}
        meta.update(fields)
        mf.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
        if self.active and p == self.file:
            self.meta.update(fields)
        return True

    async def thumbnail(self, name: str) -> Path | None:
        """Miniature JPEG (image à ~10 s), générée une fois puis mise en cache."""
        p = self.path(name)
        if not p:
            return None
        thumb = Path(str(p) + ".jpg")
        live = self.active and p == self.file
        if thumb.exists() and not live:
            return thumb
        for ss in ("10", "1", "0"):
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-loglevel", "error", "-y", "-ss", ss, "-i", str(p), "-frames:v", "1",
                "-vf", "scale=320:-2", "-q:v", "5", str(thumb),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            if thumb.exists() and thumb.stat().st_size > 0:
                return thumb
        return None


if __name__ == "__main__":
    # Conversion des enregistrements existants :
    #   docker exec -u bot webconf-wN /opt/webconf/venv/bin/python /opt/webconf/worker/recorder.py --faststart [fichiers…]
    # Sans fichier : tous les .mp4 du dossier des enregistrements, sauf ceux modifiés depuis moins de 2 min
    # (enregistrement en cours).
    if sys.argv[1:2] != ["--faststart"]:
        sys.exit("usage : recorder.py --faststart [fichier.mp4 …]")
    files = [Path(a) for a in sys.argv[2:]] or sorted(Path(os.environ.get("RECORD_DIR", "/srv/webconf/recordings")).glob("*.mp4"))
    bad = 0
    for f in files:
        if not sys.argv[2:] and time.time() - f.stat().st_mtime < 120:
            print(f"{f.name} : ignoré (modifié il y a moins de 2 min)", flush=True)
            continue
        t0 = time.monotonic()
        res = faststart(f)
        bad += res not in ("ok", "déjà")
        print(f"{f.name} : {res} ({time.monotonic() - t0:.1f} s)", flush=True)
    sys.exit(1 if bad else 0)
