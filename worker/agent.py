"""Agent worker : rejoint une réunion Teams/Zoom dans Chrome (Xvfb), enregistre, et expose
un pont audio WebRTC pour intervenir depuis l'IHM.

API (header X-Agent-Token obligatoire sauf /health) — appelée uniquement par la VM de contrôle.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import re
import shutil
import time
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

import analyst
from audio_bridge import AudioBridge
from meetings import (PLATFORMS, SPEAKER_DEBUG_JS, Browser, active_speakers, clean_name, detect_platform, unwrap_url,
                      visible_participants)
from recorder import Recorder, _slug
import slides as slides_mod
from transcriber import API as AI_API, Transcriber, rename_in_doc, save_doc, to_markdown

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("agent")

TOKEN = os.environ["AGENT_TOKEN"]
DISPLAY = os.environ.get("DISPLAY", ":99")
SCREEN = os.environ.get("SCREEN_SIZE", "1920x1200")
NAME = os.environ.get("WORKER_NAME", os.uname().nodename)
BOT_NAME = os.environ.get("BOT_DISPLAY_NAME", "Assistant")
ICE = json.loads(os.environ.get("ICE_SERVERS", "[]"))
VNC_PORT = int(os.environ.get("VNC_PORT", "6080"))  # écran distant (websockify), lu par le contrôle
CHANNEL = os.environ.get("CHROME_CHANNEL", "chrome" if shutil.which("google-chrome") else "")

browser = Browser(os.environ.get("PROFILE_DIR", "/srv/webconf/chrome-profile"), SCREEN, CHANNEL,
                  kiosk=os.environ.get("KIOSK", "1") == "1")
recorder = Recorder(os.environ.get("RECORD_DIR", "/srv/webconf/recordings"), DISPLAY, SCREEN)
bridge = AudioBridge(ICE)
transcriber = Transcriber(os.environ.get("RECORD_DIR", "/srv/webconf/recordings"))

state = {"state": "idle", "platform": None, "url": None, "title": None, "since": None, "error": None,
         "auto_leave_at": None}
join_task: asyncio.Task | None = None
watch_task: asyncio.Task | None = None
speaker_task: asyncio.Task | None = None
bot_name: str = BOT_NAME
REPORT_AUTO = os.environ.get("REPORT_AUTO", "1") == "1"
reports: dict[str, dict] = {}   # stem -> {state: running|done|error|skipped, step, started, error}
slides_jobs: dict[str, dict] = {}   # stem -> lecture des diapositives après coup {state, step, n, error}

app = FastAPI(title="webconf-worker")


def auth(x_agent_token: str = Header("")):
    if x_agent_token != TOKEN:
        raise HTTPException(401, "bad token")


def set_state(s: str, **kw):
    if s != state["state"]:
        log.info("state %s -> %s", state["state"], s)
        state["since"] = dt.datetime.now().astimezone().isoformat()
    state["state"] = s
    state.update(kw)


class JoinReq(BaseModel):
    url: str
    platform: str | None = None
    display_name: str | None = None
    passcode: str | None = None
    record: bool = True
    title: str | None = None
    duration_min: int | None = None  # quitte automatiquement après N minutes
    transcribe: bool = True          # transcription en direct (si la clé OVH est configurée)
    calendar: dict | None = None     # événement d'agenda (eid Google / id Exchange, organisateur, invités), facultatif


class OpenReq(BaseModel):
    url: str


class RecReq(BaseModel):
    title: str | None = None


class Offer(BaseModel):
    sdp: str
    type: str


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/status", dependencies=[Depends(auth)])
async def status():
    return {**state, "name": NAME, "recording": recorder.active,
            "recording_file": recorder.file.name if recorder.active else None,
            "recording_since": recorder.started_at.isoformat() if recorder.active else None,
            "listeners": bridge.listeners, "transcript": transcriber.status(),
            "vnc_port": VNC_PORT}


async def _run_join(req: JoinReq, platform: str):
    try:
        async with browser.lock:
            page = await browser.page()
            await page.bring_to_front()
            impl = PLATFORMS.get(platform)
            name = req.display_name or BOT_NAME
            global bot_name
            bot_name = name
            recognized = True
            if impl is None:  # autre plateforme : on ouvre la page, finir à la main via l'écran distant
                await page.goto(req.url, wait_until="domcontentloaded", timeout=45000)
            elif platform in ("zoom", "web"):
                recognized = await impl.join(page, req.url, name, set_state, req.passcode)
            else:
                await impl.join(page, req.url, name, set_state)
        note = None
        if platform == "web" and not recognized:
            note = "entrée automatique non confirmée : vérifiez via « Écran » (enregistrement lancé)"
        elif impl is None:
            note = "page ouverte : rejoignez à la main via « Écran » (enregistrement lancé)"
        set_state("in_meeting", note=note)
        meta = {"platform": platform, "url": req.url, "worker": NAME, "site": _site(req.url, platform)}
        if req.calendar:
            meta["calendar"] = req.calendar
        if req.record and not recorder.active:
            await recorder.start(req.title or _site(req.url, platform), meta)
        if req.transcribe and transcriber.enabled:
            await _start_transcript(req.title or _site(req.url, platform), meta)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.exception("join failed")
        # on garde la page ouverte : l'utilisateur peut terminer à la main via noVNC
        set_state("error", error=str(e)[:300])


async def _start_transcript(title: str, meta: dict):
    """Démarre la transcription ; fichiers à côté du MP4 en cours (mêmes horodatages que la vidéo)."""
    if transcriber.active:
        return
    try:
        if recorder.active:
            base, t0 = recorder.file.with_suffix(""), recorder.started_at.timestamp()
        else:
            now = dt.datetime.now().astimezone()
            base, t0 = recorder.dir / f"{now:%Y%m%d-%H%M%S}_{_slug(title)}", now.timestamp()
        await transcriber.start(base, t0, {**meta, "title": title}, bot_names=(clean_name(bot_name), BOT_NAME),
                                me_name=_me_name())
        _start_speaker_loop()
    except Exception as e:
        log.warning("transcription non démarrée : %s", e)
        transcriber.last_error = str(e)[:200]


def _me_name() -> str:
    """Nom de l'utilisateur dans les comptes rendus : « Prénom NOM - Société » -> « Prénom NOM »."""
    if os.environ.get("TRANSCRIPT_ME_NAME"):
        return os.environ["TRANSCRIPT_ME_NAME"]
    n = re.sub(r"\s*(\(.*\)|[-–|].*)$", "", clean_name(bot_name or "")).strip()
    return (n.title() if n.isupper() else n) or "Moi"


# ----------------------------------------------------------------------------- compte rendu final
def _launch_report(stem: str | None, force: bool = False):
    if not stem or not transcriber.enabled or (not REPORT_AUTO and not force):
        return False
    job = reports.get(stem)
    if job and job["state"] == "running":
        return False
    reports[stem] = {"state": "running", "step": "préparation", "started": time.time(), "error": None}
    asyncio.create_task(_report_job(stem))
    return True


async def _report_job(stem: str):
    job = reports[stem]
    try:
        p = recorder.dir / (stem + ".transcript.json")
        doc = json.loads(p.read_text())
        words = sum(len(x["text"].split()) for x in analyst.paragraphs(doc))
        if words < 30:
            job.update(state="skipped", step=None, error="réunion trop courte pour un compte rendu")
            return
        rep = await analyst.build_report(transcriber.llm, doc, lambda st: job.update(step=st))
        (recorder.dir / (stem + ".report.json")).write_text(json.dumps(rep, ensure_ascii=False, indent=1))
        job.update(state="done", step=None, secs=rep["secs"])
        log.info("compte rendu %s : %d s", stem, rep["secs"])
    except Exception as e:
        log.exception("compte rendu %s", stem)
        job.update(state="error", step=None, error=str(e)[:300])


def _start_speaker_loop():
    global speaker_task
    if speaker_task and not speaker_task.done():
        return
    speaker_task = asyncio.create_task(_speaker_loop())


async def _speaker_loop():
    """Relève ~4 fois/s l'orateur actif affiché par la plateforme (pour nommer les voix)."""
    last_dbg, shots, last_roster = time.time(), [], 0.0
    while transcriber.active:
        try:
            if state["state"] == "in_meeting" and browser.ctx:
                page = await browser.page()
                transcriber.ui_sample(await active_speakers(page))
                if time.time() - last_roster > 10:   # participants visibles (vignettes, liste)
                    last_roster = time.time()
                    transcriber.participants_sample(await visible_participants(page))
                # Des participants parlent mais aucun nom n'est relevé (interface de la plateforme
                # changée ?) : instantanés des éléments « parole » de la page, gardés à côté de la
                # réunion (<stem>.speakerdebug.json) pour adapter SPEAKER_JS
                if time.time() - last_dbg > 30 and len(shots) < 6 and not transcriber.diar.ui_active \
                        and transcriber.meeting_speech_since(20):
                    last_dbg = time.time()
                    shots.append({"at": dt.datetime.now().astimezone().isoformat(), "url": page.url[:200],
                                  **await _speaker_candidates(page)})
                    transcriber.save_extra("speakerdebug", {"platform": state["platform"], "snapshots": shots})
        except Exception as e:
            log.debug("speaker probe: %s", e)
        await asyncio.sleep(0.25)


async def _speaker_candidates(page) -> dict:
    cands = []
    for f in page.frames:
        try:
            cands += [{"frame": f.url[:80], **c} for c in await f.evaluate(SPEAKER_DEBUG_JS)]
        except Exception:
            pass
    return {"active": await active_speakers(page), "candidates": cands[:80]}


def _site(url: str, platform: str) -> str:
    if platform in ("teams", "zoom"):
        return platform
    host = urlparse(url).hostname or platform or "web"
    return host[4:] if host.startswith("www.") else host


async def _watchdog():
    """Détecte la fin de réunion (bouton Quitter disparu) et la durée max."""
    missing = 0
    seen = False  # pour « Autre » : on ne conclut à la fin que si la barre de réunion a été reconnue
    while True:
        await asyncio.sleep(5)
        if state["auto_leave_at"] and dt.datetime.now().astimezone() >= dt.datetime.fromisoformat(state["auto_leave_at"]):
            log.info("durée max atteinte")
            await _leave()
            return
        impl = PLATFORMS.get(state["platform"] or "")
        if state["state"] != "in_meeting" or impl is None or browser.lock.locked():
            continue
        try:
            ok = await impl.in_meeting(await browser.page())
        except Exception:
            ok = False
        if ok and not seen:
            seen = True
            if state.get("note"):
                set_state("in_meeting", note=None)
        missing = 0 if ok else missing + 1
        if seen and missing >= 6:  # ~30 s sans barre d'outils de réunion
            log.info("réunion terminée (détectée)")
            await _leave(click=False)
            return


@app.post("/meeting/join", dependencies=[Depends(auth)])
async def join(req: JoinReq):
    global join_task, watch_task
    if state["state"] in ("joining", "lobby", "in_meeting"):
        raise HTTPException(409, f"worker occupé ({state['state']})")
    req.url = unwrap_url(req.url)   # lien safelinks / redirection Google : on rejoint le vrai lien
    platform = req.platform if req.platform in ("teams", "zoom", "web", "open") else detect_platform(req.url)
    leave_at = None
    if req.duration_min:
        leave_at = (dt.datetime.now().astimezone() + dt.timedelta(minutes=req.duration_min)).isoformat()
    set_state("joining", platform=platform, url=req.url, title=req.title, error=None, note=None,
              auto_leave_at=leave_at)
    join_task = asyncio.create_task(_run_join(req, platform))
    if watch_task:
        watch_task.cancel()
    watch_task = asyncio.create_task(_watchdog())
    return await status()


async def _leave(click: bool = True):
    global join_task
    if join_task and not join_task.done():
        join_task.cancel()
    set_state("leaving")
    await recorder.stop()
    _launch_report(await transcriber.stop())
    impl = PLATFORMS.get(state["platform"] or "")
    if click and impl and browser.ctx:
        try:
            await impl.leave(await browser.page())
            await asyncio.sleep(1)
        except Exception:
            pass
    await browser.blank()
    set_state("idle", platform=None, url=None, title=None, auto_leave_at=None, error=None, note=None)


@app.post("/meeting/leave", dependencies=[Depends(auth)])
async def leave():
    if watch_task:
        watch_task.cancel()
    await _leave()
    return await status()


@app.post("/meeting/mute", dependencies=[Depends(auth)])
async def mute():
    """Bascule le micro dans la réunion (raccourci clavier de la plateforme)."""
    impl = PLATFORMS.get(state["platform"] or "")
    if not impl:
        raise HTTPException(400, "pas de réunion")
    await impl.toggle_mute(await browser.page())
    return {"ok": True}


@app.post("/browser/open", dependencies=[Depends(auth)])
async def open_url(req: OpenReq):
    """Ouvre une URL (ex: se connecter à un compte Teams/Zoom une fois, la session est conservée)."""
    if state["state"] in ("joining", "lobby", "in_meeting"):
        raise HTTPException(409, "réunion en cours")
    await browser.goto(req.url)
    set_state("manual", platform=None, url=req.url)
    return await status()


@app.post("/browser/reset", dependencies=[Depends(auth)])
async def reset():
    await browser.blank()
    if state["state"] in ("manual", "error"):
        set_state("idle", platform=None, url=None, error=None)
    return await status()


@app.post("/recording/start", dependencies=[Depends(auth)])
async def rec_start(req: RecReq):
    name = await recorder.start(req.title or state["title"] or state["platform"] or "enregistrement",
                                {"platform": state["platform"], "url": state["url"], "worker": NAME})
    return {"file": name}


@app.post("/recording/stop", dependencies=[Depends(auth)])
async def rec_stop():
    return {"file": await recorder.stop()}


class TransReq(BaseModel):
    title: str | None = None


@app.post("/transcript/start", dependencies=[Depends(auth)])
async def transcript_start(req: TransReq):
    if not transcriber.enabled:
        raise HTTPException(400, "clé OVH AI Endpoints non configurée sur ce worker")
    await _start_transcript(req.title or state["title"] or _site(state["url"] or "", state["platform"] or "web"),
                            {"platform": state["platform"], "url": state["url"], "worker": NAME})
    return transcriber.status()


@app.post("/transcript/stop", dependencies=[Depends(auth)])
async def transcript_stop():
    _launch_report(await transcriber.stop())
    return transcriber.status()


@app.post("/transcript/analyze", dependencies=[Depends(auth)])
async def transcript_analyze():
    """Point d'étape immédiat (sinon automatique toutes les ANALYSIS_EVERY secondes)."""
    if not transcriber.active:
        raise HTTPException(400, "pas de transcription en cours")
    if not transcriber.maybe_analyze(force=True) and not transcriber.analysis["running"]:
        raise HTTPException(400, "rien de nouveau à analyser pour l'instant")
    return transcriber.analysis_status()


@app.get("/transcript/analysis", dependencies=[Depends(auth)])
async def transcript_analysis():
    return transcriber.analysis_doc()


def _report_paths(name: str):
    stem = recorder.stem(name)
    return stem, recorder.dir / (stem + ".transcript.json"), recorder.dir / (stem + ".report.json")


@app.get("/recordings/{name}/report", dependencies=[Depends(auth)])
async def recording_report(name: str, fmt: str = "json"):
    stem, tp, rp = _report_paths(name)
    if not tp.exists():
        raise HTTPException(404, "pas de transcription")
    doc = json.loads(tp.read_text())
    rep = json.loads(rp.read_text()) if rp.exists() else None
    if fmt == "json":
        return {"report": rep, "job": reports.get(stem), "speakers": doc.get("speakers"),
                "me_name": analyst.me_label(doc), "meta": doc.get("meta"),
                "analysis": doc.get("analysis"), "live": transcriber.active and transcriber.base and transcriber.base.name == stem}
    if not rep:
        raise HTTPException(404, "pas de compte rendu")
    if fmt == "md":
        return Response(analyst.report_markdown(rep, doc), media_type="text/markdown; charset=utf-8")
    if fmt == "docx":
        data = await asyncio.to_thread(analyst.report_docx, rep, doc)
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    raise HTTPException(400, "format inconnu")


@app.post("/recordings/{name}/report", dependencies=[Depends(auth)])
async def recording_report_build(name: str):
    """(Re)génère le compte rendu, par ex. après avoir renommé les intervenants ou si la génération a échoué."""
    stem, tp, _ = _report_paths(name)
    if not tp.exists():
        raise HTTPException(404, "pas de transcription")
    if transcriber.active and transcriber.base and transcriber.base.name == stem:
        raise HTTPException(409, "transcription en cours : le compte rendu sera généré à la fin de la réunion")
    if not _launch_report(stem, force=True):
        raise HTTPException(409, "compte rendu déjà en cours de génération")
    return reports[stem]


class SpeakerReq(BaseModel):
    id: str
    name: str | None = None


@app.post("/transcript/speakers", dependencies=[Depends(auth)])
async def transcript_rename_live(req: SpeakerReq):
    if not transcriber.rename_speaker(req.id, req.name):
        raise HTTPException(404, "intervenant inconnu")
    return {"ok": True, "speakers": transcriber.labels()}


@app.patch("/recordings/{name}/speakers", dependencies=[Depends(auth)])
async def recording_rename_speaker(name: str, req: SpeakerReq):
    """Renomme un intervenant d'une transcription (en cours ou terminée)."""
    if transcriber.active and transcriber.base and recorder.stem(name) == transcriber.base.name:
        return await transcript_rename_live(req)
    p = recorder.transcript_path(name, "json")
    if not p:
        raise HTTPException(404, "pas de transcription")
    doc = json.loads(p.read_text())
    if not rename_in_doc(doc, req.id, req.name):
        raise HTTPException(404, "intervenant inconnu")
    if transcriber.base and recorder.stem(name) == transcriber.base.name and req.id in transcriber.diar.spk:
        transcriber.rename_speaker(req.id, req.name)  # réunion qui vient de finir : état mémoire = fichier
        return {"ok": True, "speakers": transcriber.labels()}
    save_doc(p.parent / recorder.stem(p.name), doc)
    return {"ok": True, "speakers": doc["speakers"]}


@app.get("/debug/speakers", dependencies=[Depends(auth)])
async def debug_speakers():
    """Diagnostic : ce que voit la détection d'orateur actif dans la page de réunion."""
    page = await browser.page()
    cands = []
    for f in page.frames:
        try:
            cands += [{"frame": f.url[:80], **c} for c in await f.evaluate(SPEAKER_DEBUG_JS)]
        except Exception:
            pass
    return {"active": await active_speakers(page), "candidates": cands[:80],
            "recent": transcriber.diar.ui[-20:], "speakers": transcriber.labels()}


@app.get("/transcript/slides", dependencies=[Depends(auth)])
async def transcript_slides(after: int = 0):
    """Diapositives lues à l'écran pendant la réunion en cours (après les `after` premières)."""
    return transcriber.slides_after(max(0, after))


@app.get("/slides/{stem}/{n}", dependencies=[Depends(auth)])
async def slide_image(stem: str, n: int):
    p = recorder.dir / f"{os.path.basename(stem)}.slide-{n:03d}.jpg"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


async def _slides_job(stem: str):
    """Lit après coup les diapositives d'une réunion enregistrée (images de la vidéo)."""
    job = slides_jobs[stem]
    try:
        mp4, tp = recorder.dir / (stem + ".mp4"), recorder.dir / (stem + ".transcript.json")
        rmeta_p = recorder.dir / (stem + ".mp4.json")
        rmeta = json.loads(rmeta_p.read_text()) if rmeta_p.exists() else {}
        try:
            rec_t0 = dt.datetime.fromisoformat(rmeta["started_at"]).timestamp()
        except (KeyError, ValueError):
            rec_t0 = mp4.stat().st_mtime
        doc = json.loads(tp.read_text()) if tp.exists() else None
        offset = (doc.get("t0") or rec_t0) - rec_t0 if doc else 0.0   # temps vidéo -> temps transcription
        dur = recorder._duration(rmeta, False) or 0
        for old in recorder.dir.glob(f"{stem}.slide-*.jpg"):
            old.unlink(missing_ok=True)
        found: list = []
        reader = slides_mod.SlideReader(transcriber.http, AI_API, recorder.dir / stem, found,
                                        on_change=lambda: job.update(n=len(found)))
        await reader.from_video(mp4, offset, progress=lambda tv: job.update(
            step=f"{int(tv // 60)} min" + (f" sur {int(dur // 60)}" if dur else "")))
        doc = json.loads(tp.read_text()) if tp.exists() else {   # relu : renommages faits entre-temps
            "meta": {k: rmeta.get(k) for k in ("title", "platform", "url", "site", "started_at", "ended_at")},
            "t0": rec_t0, "speakers": {}, "paragraphs": [], "raw": []}
        doc["slides"] = found
        save_doc(recorder.dir / stem, doc)
        job.update(state="done", step=None, n=len(found), error=reader.error if not found else None)
        log.info("diapositives %s : %d", stem, len(found))
    except Exception as e:
        log.exception("diapositives %s", stem)
        job.update(state="error", step=None, error=str(e)[:300])


@app.post("/recordings/{name}/slides", dependencies=[Depends(auth)])
async def recording_slides(name: str):
    """(Re)lit les diapositives d'un enregistrement ; ensuite, régénérer le compte rendu pour en tenir compte."""
    stem = recorder.stem(name)
    if not (recorder.dir / (stem + ".mp4")).exists():
        raise HTTPException(404, "pas de vidéo")
    if not transcriber.enabled:
        raise HTTPException(400, "clé OVH AI Endpoints absente")
    if transcriber.active and transcriber.base and transcriber.base.name == stem:
        raise HTTPException(409, "réunion en cours : les diapositives sont déjà lues en direct")
    if (slides_jobs.get(stem) or {}).get("state") == "running":
        raise HTTPException(409, "lecture des diapositives déjà en cours")
    slides_jobs[stem] = {"state": "running", "step": "démarrage", "n": 0, "error": None, "started": time.time()}
    asyncio.create_task(_slides_job(stem))
    return slides_jobs[stem]


# ----------------------------------------------------------------------------- audio d'un passage
@app.get("/recordings/{name}/audio", dependencies=[Depends(auth)])
async def recording_audio(name: str, start: float = 0.0, end: float | None = None, fmt: str = "mp3"):
    """Audio de la réunion (son de la réunion + micro de l'IHM), en mono 16 kHz : tout ou un passage.
    `start`/`end` sont en secondes depuis le début de la vidéo."""
    p = recorder.path(recorder.stem(name) + ".mp4")
    if not p:
        raise HTTPException(404, "pas de vidéo")
    if fmt not in ("mp3", "wav"):
        raise HTTPException(400, "format : mp3 ou wav")
    cmd = ["ffmpeg", "-loglevel", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += ["-i", str(p)]
    if end is not None and end > start:
        cmd += ["-t", f"{end - start:.2f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000"]
    cmd += ["-c:a", "libmp3lame", "-b:a", "48k", "-f", "mp3"] if fmt == "mp3" else ["-c:a", "pcm_s16le", "-f", "wav"]
    proc = await asyncio.create_subprocess_exec(*cmd, "pipe:1", stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)

    async def body():
        try:
            while chunk := await proc.stdout.read(65536):
                yield chunk
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
    return StreamingResponse(body(), media_type="audio/mpeg" if fmt == "mp3" else "audio/wav")


# ----------------------------------------------------------------------------- compte rendu final (déposé par Claude)
class FinalCR(BaseModel):
    markdown: str
    pdf_b64: str | None = None
    author: str | None = None


def _final_paths(name: str):
    stem = recorder.stem(name)
    return recorder.dir / f"{stem}.cr-final.md", recorder.dir / f"{stem}.cr-final.pdf", recorder.dir / f"{stem}.cr-final.json"


@app.put("/recordings/{name}/final", dependencies=[Depends(auth)])
async def recording_final_put(name: str, req: FinalCR):
    stem = recorder.stem(name)
    if not (recorder.dir / (stem + ".mp4")).exists() and not (recorder.dir / (stem + ".transcript.json")).exists():
        raise HTTPException(404, "réunion inconnue")
    md, pdf, info = _final_paths(name)
    data = None
    if req.pdf_b64:   # vérifié avant d'écrire quoi que ce soit
        import base64
        import binascii
        try:
            data = base64.b64decode(req.pdf_b64, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(400, "PDF : base64 invalide")
        if not data.startswith(b"%PDF"):
            raise HTTPException(400, "le fichier joint n'est pas un PDF")
    md.write_text(req.markdown)
    if data:
        pdf.write_bytes(data)
    else:
        pdf.unlink(missing_ok=True)
    meta = {"at": dt.datetime.now().astimezone().isoformat(), "author": req.author, "md_chars": len(req.markdown),
            "pdf_bytes": pdf.stat().st_size if pdf.exists() else None}
    info.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


@app.get("/recordings/{name}/final", dependencies=[Depends(auth)])
async def recording_final_get(name: str, fmt: str = "json"):
    md, pdf, info = _final_paths(name)
    if fmt == "md" and md.exists():
        return Response(md.read_text(), media_type="text/markdown; charset=utf-8")
    if fmt == "pdf" and pdf.exists():
        return FileResponse(pdf, media_type="application/pdf")
    if fmt == "json" and info.exists():
        return json.loads(info.read_text())
    raise HTTPException(404, "pas de compte rendu final")


@app.get("/transcript", dependencies=[Depends(auth)])
async def transcript_live(after: int = 0):
    """Transcription en direct : paragraphes mis en forme après `after` + lignes brutes en attente."""
    return transcriber.snapshot(max(0, after))


@app.get("/recordings/{name}/transcript", dependencies=[Depends(auth)])
async def recording_transcript(name: str, fmt: str = "json"):
    p = recorder.transcript_path(name, "json")
    if not p:
        raise HTTPException(404, "pas de transcription")
    doc = json.loads(p.read_text())
    if fmt == "md":
        return Response(to_markdown(doc), media_type="text/markdown; charset=utf-8")
    return doc


_cr_titles: dict[str, tuple[float, str | None]] = {}


def _cr_title(p) -> str | None:
    """Titre du compte rendu (pour des listes plus parlantes que « teams » ou « zoom ») ; mis en cache."""
    try:
        mt = p.stat().st_mtime
    except FileNotFoundError:
        return None
    c = _cr_titles.get(p.name)
    if not c or c[0] != mt:
        try:
            c = (mt, (json.loads(p.read_text()).get("cr") or {}).get("titre"))
        except (ValueError, OSError):
            c = (mt, None)
        _cr_titles[p.name] = c
    return c[1]


@app.get("/recordings", dependencies=[Depends(auth)])
async def recordings():
    items = recorder.list()
    for x in items:
        stem = recorder.stem(x["name"])
        x["report"] = (recorder.dir / (stem + ".report.json")).exists()
        x["cr_title"] = _cr_title(recorder.dir / (stem + ".report.json")) if x["report"] else None
        x["final_cr"] = (recorder.dir / (stem + ".cr-final.json")).exists()
        x["report_job"] = reports.get(stem)
        x["slides_job"] = slides_jobs.get(stem)
    return items


@app.get("/recordings/{name}", dependencies=[Depends(auth)])
async def recording_file(name: str):
    p = recorder.path(name)
    if not p:
        raise HTTPException(404)
    return FileResponse(p, media_type="video/mp4", filename=p.name)


@app.get("/recordings/{name}/thumb", dependencies=[Depends(auth)])
async def recording_thumb(name: str):
    t = await recorder.thumbnail(name)
    if not t:
        raise HTTPException(404)
    return FileResponse(t, media_type="image/jpeg", headers={"Cache-Control": "max-age=300"})


class RenameReq(BaseModel):
    title: str


@app.patch("/recordings/{name}", dependencies=[Depends(auth)])
async def recording_rename(name: str, req: RenameReq):
    if not recorder.rename(name, req.title.strip()[:200]):
        raise HTTPException(404)
    return {"ok": True}


@app.get("/disk", dependencies=[Depends(auth)])
async def disk():
    u = shutil.disk_usage(recorder.dir)
    return {"total": u.total, "used": u.used, "free": u.free}


@app.delete("/recordings/{name}", dependencies=[Depends(auth)])
async def recording_delete(name: str):
    busy = (recorder.stem(transcriber.base.name),) if transcriber.active and transcriber.base else ()
    busy += tuple(k for k, j in reports.items() if j["state"] == "running")
    if not recorder.delete(name, busy):
        raise HTTPException(400, "impossible de supprimer")
    return {"ok": True}


@app.get("/screenshot", dependencies=[Depends(auth)])
async def screenshot(w: int = 640):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-f", "x11grab", "-video_size", SCREEN, "-i", DISPLAY,
        "-frames:v", "1", "-vf", f"scale={min(max(w, 160), 1920)}:-2", "-q:v", "6", "-f", "mjpeg", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    if not out:
        raise HTTPException(500, "capture impossible")
    return Response(out, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/webrtc/offer", dependencies=[Depends(auth)])
async def webrtc_offer(o: Offer):
    return await bridge.handle_offer(o.sdp, o.type)


@app.on_event("shutdown")
async def shutdown():
    await recorder.stop()
    await bridge.close_all()
