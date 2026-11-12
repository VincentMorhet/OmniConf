"""VM de contrôle : IHM web + API. Pilote les workers, relaie l'audio WebRTC (signalisation),
l'écran distant (noVNC) et les enregistrements. Planifie les réunions."""
import asyncio
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import uuid
from pathlib import Path

import httpx
import websockets
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("control")

PASSWORD = os.environ["ADMIN_PASSWORD"]
TOKEN = os.environ["AGENT_TOKEN"]
# WORKERS="w1=http://192.168.0.231:8001,w2=http://192.168.0.232:8001"
WORKERS = dict(p.split("=", 1) for p in os.environ["WORKERS"].split(",") if p)
ICE = json.loads(os.environ.get("ICE_SERVERS", "[]"))
# Valeurs par défaut (surchargeables dans control.env)
DEFAULT_NAMES = json.loads(os.environ.get("DEFAULT_NAMES") or
                           '{"zoom": "Assistant", "teams": "Assistant", "web": "Assistant"}')
DEFAULT_DURATION = int(os.environ.get("DEFAULT_DURATION_MIN", "180"))


def unwrap_url(url: str) -> str:
    """Lien réel derrière un lien de protection (Outlook « safelinks ») ou une redirection Google."""
    for _ in range(3):
        try:
            u = httpx.URL(url)
        except Exception:
            return url
        host, q = (u.host or "").lower(), u.params
        if host.endswith("safelinks.protection.outlook.com") and q.get("url"):
            url = q["url"]
        elif (host.startswith("www.google.") or host.startswith("google.")) and u.path == "/url" and (q.get("q") or q.get("url")):
            url = q.get("q") or q.get("url")
        else:
            break
    return url


def detect_platform(url: str) -> str:
    url = unwrap_url(url)
    host = (httpx.URL(url).host or "") if url.startswith("http") else ""
    if "teams.microsoft" in host or "teams.live" in host:
        return "teams"
    if "zoom.us" in host or "zoom.com" in host:
        return "zoom"
    return "web"
DATA = Path(os.environ.get("DATA_DIR", "/var/lib/webconf"))
DATA.mkdir(parents=True, exist_ok=True)
SCHEDULE_FILE = DATA / "schedule.json"
LEXICON_FILE = DATA / "lexicon.md"   # lexique de l'installation (noms, corrections, sigles, registre), poussé aux workers
HERE = Path(__file__).parent
NOVNC_DIR = Path(os.environ.get("NOVNC_DIR", "/usr/share/novnc"))

app = FastAPI(title="webconf-control")
app.add_middleware(SessionMiddleware, secret_key=os.environ["SECRET_KEY"], max_age=30 * 86400,
                   https_only=os.environ.get("COOKIE_SECURE", "1") == "1", same_site="strict")
http = httpx.AsyncClient(headers={"X-Agent-Token": TOKEN}, timeout=httpx.Timeout(20, connect=3))


# ----------------------------------------------------------------------------- auth
def require_login(request: Request):
    if not request.session.get("ok"):
        raise HTTPException(401, "non connecté")


class Login(BaseModel):
    password: str


@app.post("/api/login")
async def login(body: Login, request: Request):
    if not hmac.compare_digest(body.password.encode(), PASSWORD.encode()):
        await asyncio.sleep(1.5)
        raise HTTPException(401, "mot de passe incorrect")
    request.session["ok"] = True
    return {"ok": True}


@app.post("/api/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request):
    return {"logged_in": bool(request.session.get("ok"))}


AUTH = [Depends(require_login)]


# ----------------------------------------------------------------------------- workers
def wurl(name: str) -> str:
    if name not in WORKERS:
        raise HTTPException(404, "worker inconnu")
    return WORKERS[name]


async def wcall(name: str, method: str, path: str, **kw):
    try:
        r = await http.request(method, wurl(name) + path, **kw)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"{name} injoignable: {e}")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text
        raise HTTPException(r.status_code, detail)
    return r


async def worker_status(name: str) -> dict:
    try:
        r = await http.get(wurl(name) + "/status", timeout=3)
        return {**r.json(), "name": name, "online": True}
    except Exception:
        return {"name": name, "online": False, "state": "offline"}


@app.get("/api/config", dependencies=AUTH)
async def config():
    return {"iceServers": ICE, "workers": list(WORKERS), "defaultNames": DEFAULT_NAMES,
            "defaultDuration": DEFAULT_DURATION}


@app.get("/api/workers", dependencies=AUTH)
async def workers():
    return await asyncio.gather(*(worker_status(n) for n in WORKERS))


class StartMeeting(BaseModel):
    url: str
    platform: str | None = None
    display_name: str | None = None
    passcode: str | None = None
    record: bool = True
    title: str | None = None
    duration_min: int | None = None
    transcribe: bool = True
    worker: str | None = None  # None/"auto" = premier worker libre
    calendar: dict | None = None  # événement d'agenda (eid Google / id Exchange, organisateur, invités), facultatif


async def start_meeting(m: StartMeeting) -> dict:
    target = m.worker if m.worker and m.worker != "auto" else None
    if not target:
        for st in await asyncio.gather(*(worker_status(n) for n in WORKERS)):
            if st.get("online") and st["state"] in ("idle", "manual", "error"):
                target = st["name"]
                break
    if not target:
        raise HTTPException(409, "aucun worker libre")
    m.url = unwrap_url(m.url)
    body = m.model_dump(exclude={"worker"})
    platform = m.platform or detect_platform(m.url)
    body["platform"] = platform
    body["display_name"] = m.display_name or DEFAULT_NAMES.get(platform) or DEFAULT_NAMES.get("web")
    body["duration_min"] = m.duration_min or DEFAULT_DURATION
    r = await wcall(target, "POST", "/meeting/join", json=body)
    return {**r.json(), "name": target}


@app.post("/api/meetings", dependencies=AUTH)
async def api_start(m: StartMeeting):
    return await start_meeting(m)


@app.post("/api/workers/{name}/{action}", dependencies=AUTH)
async def worker_action(name: str, action: str, request: Request):
    paths = {"leave": "/meeting/leave", "mute": "/meeting/mute", "rec-start": "/recording/start",
             "rec-stop": "/recording/stop", "open": "/browser/open", "reset": "/browser/reset",
             "offer": "/webrtc/offer", "tr-start": "/transcript/start", "tr-stop": "/transcript/stop",
             "speakers": "/transcript/speakers", "analyze": "/transcript/analyze"}
    if action not in paths:
        raise HTTPException(404)
    body = await request.body()
    r = await wcall(name, "POST", paths[action], content=body or b"{}",
                    headers={"Content-Type": "application/json"}, timeout=60)
    return JSONResponse(r.json())


@app.get("/api/workers/{name}/transcript", dependencies=AUTH)
async def live_transcript(name: str, after: int = 0):
    r = await wcall(name, "GET", f"/transcript?after={after}", timeout=5)
    return JSONResponse(r.json(), headers={"Cache-Control": "no-store"})


@app.get("/api/workers/{name}/slides", dependencies=AUTH)
async def live_slides(name: str, after: int = 0):
    r = await wcall(name, "GET", f"/transcript/slides?after={after}", timeout=5)
    return JSONResponse(r.json(), headers={"Cache-Control": "no-store"})


@app.get("/api/slides/{name}/{stem}/{n}", dependencies=AUTH)
async def slide_image(name: str, stem: str, n: int):
    r = await wcall(name, "GET", f"/slides/{stem}/{n}", timeout=10)
    return Response(r.content, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/recordings/{name}/{file}/slides", dependencies=AUTH)
async def recording_slides(name: str, file: str):
    return (await wcall(name, "POST", f"/recordings/{file}/slides")).json()


@app.get("/api/workers/{name}/analysis", dependencies=AUTH)
async def live_analysis(name: str):
    r = await wcall(name, "GET", "/transcript/analysis", timeout=5)
    return JSONResponse(r.json(), headers={"Cache-Control": "no-store"})


class SpeakerName(BaseModel):
    id: str
    name: str | None = None


@app.patch("/api/recordings/{name}/{file}/speakers", dependencies=AUTH)
async def recording_speaker_rename(name: str, file: str, body: SpeakerName):
    return (await wcall(name, "PATCH", f"/recordings/{file}/speakers", json=body.model_dump())).json()


@app.get("/api/workers/{name}/screenshot", dependencies=AUTH)
async def screenshot(name: str, w: int = 640):
    r = await wcall(name, "GET", f"/screenshot?w={w}")
    return Response(r.content, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ----------------------------------------------------------------------------- enregistrements
@app.get("/api/recordings", dependencies=AUTH)
async def recordings():
    async def one(n):
        try:
            return [{**x, "worker": n} for x in (await http.get(wurl(n) + "/recordings", timeout=5)).json()]
        except Exception:
            return []
    lists = await asyncio.gather(*(one(n) for n in WORKERS))
    return sorted((x for l in lists for x in l), key=lambda x: -x["mtime"])


@app.get("/api/recordings/{name}/{file}", dependencies=AUTH)
async def recording_get(name: str, file: str, request: Request, download: int = 0):
    """Relais en streaming avec support Range (lecture vidéo sur iPhone/iPad)."""
    headers = {"X-Agent-Token": TOKEN}
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]
    req = http.build_request("GET", wurl(name) + f"/recordings/{file}", headers=headers, timeout=None)
    r = await http.send(req, stream=True)
    if r.status_code >= 400:
        await r.aclose()
        raise HTTPException(r.status_code)
    out_h = {k: v for k, v in r.headers.items()
             if k.lower() in ("content-length", "content-range", "accept-ranges", "last-modified", "etag")}
    out_h["Content-Disposition"] = f'{"attachment" if download else "inline"}; filename="{file}"'

    async def body():
        try:
            async for chunk in r.aiter_raw():
                yield chunk
        finally:
            await r.aclose()
    return StreamingResponse(body(), status_code=r.status_code, headers=out_h, media_type="video/mp4")


@app.get("/api/recordings/{name}/{file}/transcript", dependencies=AUTH)
async def recording_transcript(name: str, file: str, fmt: str = "json", download: int = 0):
    r = await wcall(name, "GET", f"/recordings/{file}/transcript?fmt={'md' if fmt == 'md' else 'json'}")
    if fmt != "md":
        return JSONResponse(r.json())
    stem = file.removesuffix(".mp4").removesuffix(".transcript.json")
    return Response(r.content, media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{stem}.md"'})


REPORT_TYPES = {"md": ("text/markdown; charset=utf-8", "md"),
                "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx")}


@app.get("/api/recordings/{name}/{file}/report", dependencies=AUTH)
async def recording_report(name: str, file: str, fmt: str = "json", download: int = 0):
    r = await wcall(name, "GET", f"/recordings/{file}/report?fmt={fmt if fmt in REPORT_TYPES else 'json'}", timeout=60)
    if fmt not in REPORT_TYPES:
        return JSONResponse(r.json(), headers={"Cache-Control": "no-store"})
    mime, ext = REPORT_TYPES[fmt]
    stem = file.removesuffix(".mp4").removesuffix(".transcript.json")
    return Response(r.content, media_type=mime, headers={
        "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{stem} - compte rendu.{ext}"'})


@app.post("/api/recordings/{name}/{file}/report", dependencies=AUTH)
async def recording_report_build(name: str, file: str):
    return (await wcall(name, "POST", f"/recordings/{file}/report")).json()


@app.delete("/api/recordings/{name}/{file}", dependencies=AUTH)
async def recording_delete(name: str, file: str):
    return (await wcall(name, "DELETE", f"/recordings/{file}")).json()


@app.get("/api/recordings/{name}/{file}/thumb", dependencies=AUTH)
async def recording_thumb(name: str, file: str):
    r = await wcall(name, "GET", f"/recordings/{file}/thumb")
    return Response(r.content, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


class Rename(BaseModel):
    title: str


@app.patch("/api/recordings/{name}/{file}", dependencies=AUTH)
async def recording_rename(name: str, file: str, body: Rename):
    return (await wcall(name, "PATCH", f"/recordings/{file}", json=body.model_dump())).json()


@app.get("/api/recordings/{name}/{file}/final", dependencies=AUTH)
async def recording_final(name: str, file: str, fmt: str = "md", download: int = 0):
    """Compte rendu final déposé par Claude (outil MCP attach_cr)."""
    if fmt not in ("md", "pdf", "json"):
        raise HTTPException(400)
    r = await wcall(name, "GET", f"/recordings/{file}/final?fmt={fmt}")
    if fmt == "json":
        return JSONResponse(r.json())
    stem = file.removesuffix(".mp4").removesuffix(".transcript.json")
    mime = "text/markdown; charset=utf-8" if fmt == "md" else "application/pdf"
    return Response(r.content, media_type=mime, headers={
        "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{stem} - compte rendu final.{fmt}"'})


@app.get("/api/storage", dependencies=AUTH)
async def storage():
    async def one(n):
        try:
            return {"worker": n, **(await http.get(wurl(n) + "/disk", timeout=3)).json()}
        except Exception:
            return {"worker": n, "error": "injoignable"}
    return await asyncio.gather(*(one(n) for n in WORKERS))


# ----------------------------------------------------------------------------- planification
def load_schedule() -> list[dict]:
    try:
        return json.loads(SCHEDULE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return []


def save_schedule(items):
    tmp = SCHEDULE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1))
    tmp.replace(SCHEDULE_FILE)


class ScheduleItem(StartMeeting):
    when: dt.datetime  # ISO avec fuseau


@app.get("/api/schedule", dependencies=AUTH)
async def schedule_list():
    return sorted(load_schedule(), key=lambda x: x["when"])


@app.post("/api/schedule", dependencies=AUTH)
async def schedule_add(item: ScheduleItem):
    if item.when.tzinfo is None:
        raise HTTPException(400, "date sans fuseau horaire")
    items = load_schedule()
    entry = {**json.loads(item.model_dump_json()), "id": uuid.uuid4().hex[:10], "status": "pending"}
    items.append(entry)
    save_schedule(items)
    return entry


@app.delete("/api/schedule/{sid}", dependencies=AUTH)
async def schedule_del(sid: str):
    save_schedule([x for x in load_schedule() if x["id"] != sid])
    return {"ok": True}


async def scheduler():
    while True:
        await asyncio.sleep(15)
        items = load_schedule()
        changed = False
        now = dt.datetime.now(dt.timezone.utc)
        for it in items:
            if it["status"] != "pending" or dt.datetime.fromisoformat(it["when"]) > now:
                continue
            changed = True
            if now - dt.datetime.fromisoformat(it["when"]) > dt.timedelta(minutes=30):
                it["status"], it["result"] = "missed", "trop tard (> 30 min)"
                continue
            try:
                res = await start_meeting(StartMeeting(**{k: it[k] for k in StartMeeting.model_fields if it.get(k) is not None}))
                it["status"], it["result"] = "started", res["name"]
                log.info("réunion planifiée lancée sur %s", res["name"])
            except HTTPException as e:
                # on réessaie au prochain tour tant qu'on est dans la fenêtre de 30 min
                it["status"], it["result"] = "pending", f"en attente: {e.detail}"
        if changed:
            save_schedule(items)


# ----------------------------------------------------------------------------- lexique
def lexicon_version(text: str) -> str | None:
    """Même empreinte que worker/lexicon.py."""
    return hashlib.sha1(text.encode()).hexdigest()[:12] if text.strip() else None


def lexicon_text() -> str | None:
    """None : lexique jamais saisi (on ne touche pas aux workers)."""
    try:
        return LEXICON_FILE.read_text(encoding="utf-8")
    except OSError:
        return None


async def lexicon_push(names=None, force: bool = False) -> dict:
    """Envoie le lexique aux workers qui n'ont pas la bonne version. {worker: version ou erreur}."""
    text = lexicon_text()
    if text is None:
        return {}
    want = lexicon_version(text)
    out = {}

    async def one(n):
        st = await worker_status(n)
        if not st.get("online"):
            out[n] = "injoignable"
            return
        if st.get("lexicon") == want and not force:
            out[n] = want
            return
        try:
            r = await http.put(wurl(n) + "/lexicon", json={"text": text}, timeout=10)
            out[n] = r.json().get("version") if r.status_code < 400 else f"erreur {r.status_code}"
        except Exception as e:
            out[n] = f"erreur : {str(e)[:80]}"
    await asyncio.gather(*(one(n) for n in (names or WORKERS)))
    return out


async def lexicon_sync():
    """Workers redémarrés ou recréés : ils retrouvent le lexique au plus tard 2 minutes après."""
    while True:
        try:
            await lexicon_push()
        except Exception as e:  # pragma: no cover
            log.warning("lexique : %s", e)
        await asyncio.sleep(120)


class LexiconBody(BaseModel):
    text: str


@app.get("/api/lexicon", dependencies=AUTH)
async def lexicon_get():
    text = lexicon_text()
    st = await asyncio.gather(*(worker_status(n) for n in WORKERS))
    mt = LEXICON_FILE.stat().st_mtime if text is not None else None
    return {"text": text or "", "version": lexicon_version(text or ""),
            "updated_at": dt.datetime.fromtimestamp(mt).astimezone().isoformat() if mt else None,
            "workers": {x["name"]: x.get("lexicon") if x.get("online") else "injoignable" for x in st}}


@app.put("/api/lexicon", dependencies=AUTH)
async def lexicon_put(body: LexiconBody):
    if len(body.text) > 200_000:
        raise HTTPException(413, "lexique trop long (200 000 caractères au plus)")
    if LEXICON_FILE.exists():   # version précédente gardée (retour arrière à la main)
        LEXICON_FILE.replace(LEXICON_FILE.with_suffix(".prev.md"))
    tmp = LEXICON_FILE.with_suffix(".tmp")
    tmp.write_text(body.text, encoding="utf-8")
    tmp.replace(LEXICON_FILE)
    log.info("lexique enregistré (%s, %d caractères)", lexicon_version(body.text), len(body.text))
    return {"version": lexicon_version(body.text), "workers": await lexicon_push()}


@app.on_event("startup")
async def startup():
    asyncio.create_task(scheduler())
    asyncio.create_task(lexicon_sync())


# ----------------------------------------------------------------------------- écran distant (noVNC)
@app.websocket("/vnc/{name}/websockify")
async def vnc_proxy(ws: WebSocket, name: str):
    if not ws.session.get("ok") or name not in WORKERS:
        await ws.close(code=4401)
        return
    host = httpx.URL(WORKERS[name]).host
    # Port VNC annoncé par le worker (plusieurs conteneurs par hôte) ; 6080 par défaut (VM)
    vnc_port = int((await worker_status(name)).get("vnc_port") or 6080)
    proto = ws.headers.get("sec-websocket-protocol", "")
    subprotocols = ["binary"] if "binary" in proto else None
    await ws.accept(subprotocol="binary" if subprotocols else None)
    try:
        async with websockets.connect(f"ws://{host}:{vnc_port}/", subprotocols=subprotocols,
                                      max_size=None, open_timeout=5) as up:
            async def down():
                async for msg in up:
                    if isinstance(msg, bytes):
                        await ws.send_bytes(msg)
                    else:
                        await ws.send_text(msg)

            async def upstream():
                while True:
                    m = await ws.receive()
                    if m["type"] == "websocket.disconnect":
                        return
                    if m.get("bytes") is not None:
                        await up.send(m["bytes"])
                    elif m.get("text") is not None:
                        await up.send(m["text"])
            tasks = [asyncio.create_task(down()), asyncio.create_task(upstream())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in tasks:
                t.cancel()
    except Exception as e:
        log.info("vnc %s: %r", name, e)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


@app.get("/novnc/{path:path}")
async def novnc_static(path: str, request: Request):
    require_login(request)
    p = (NOVNC_DIR / path).resolve()
    if not str(p).startswith(str(NOVNC_DIR.resolve())) or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


# ----------------------------------------------------------------------------- serveur MCP (accès de Claude)
import mcp_server  # noqa: E402

mcp = mcp_server.mount(app, http=http, workers=WORKERS, token=TOKEN, data_dir=DATA, secret=os.environ["SECRET_KEY"],
                       site=os.environ.get("SITE_ADDRESS"), require_login=Depends(require_login))


# ----------------------------------------------------------------------------- IHM
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.get("/")
async def index():
    # Version des fichiers dans leur adresse : après une mise à jour, le navigateur recharge
    # app.js et style.css au lieu de garder en cache l'ancienne version (page incohérente)
    html = (HERE / "static" / "index.html").read_text()
    for f in ("app.js", "style.css"):
        v = int((HERE / "static" / f).stat().st_mtime)
        html = html.replace(f'"/static/{f}"', f'"/static/{f}?v={v}"')
    return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(HERE / "static" / "manifest.webmanifest", media_type="application/manifest+json")
