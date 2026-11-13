"""VM de contrôle : IHM web + API. Pilote les workers, relaie l'audio WebRTC (signalisation),
l'écran distant (noVNC) et les enregistrements. Planifie les réunions."""
import asyncio
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from pathlib import Path

import httpx
import websockets
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from users import Users, bot_name, can_edit, can_see, is_admin

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("control")

PASSWORD = os.environ.get("ADMIN_PASSWORD")   # mot de passe du premier administrateur (création des comptes)
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
LEXICON_FILE = DATA / "lexicon.md"
CONSENT_FILE = DATA / "voice_consents.json"   # consentements aux empreintes vocales (attestés par un administrateur)
VOICEPRINT_FILE = DATA / "voiceprints.json"   # empreintes de référence (personnes consentantes seulement)
USERS = Users(DATA / "users.json", admin_password=PASSWORD, admin_login=os.environ.get("ADMIN_LOGIN"),
              admin_name=os.environ.get("ADMIN_NAME"),
              bot_names=json.loads(os.environ["DEFAULT_NAMES"]) if os.environ.get("DEFAULT_NAMES") else None)   # lexique de l'installation (noms, corrections, sigles, registre), poussé aux workers
HERE = Path(__file__).parent
NOVNC_DIR = Path(os.environ.get("NOVNC_DIR", "/usr/share/novnc"))

app = FastAPI(title="webconf-control")
app.add_middleware(SessionMiddleware, secret_key=os.environ["SECRET_KEY"], max_age=30 * 86400,
                   https_only=os.environ.get("COOKIE_SECURE", "1") == "1", same_site="strict")
http = httpx.AsyncClient(headers={"X-Agent-Token": TOKEN}, timeout=httpx.Timeout(20, connect=3))


# ----------------------------------------------------------------------------- auth
def session_user(session) -> dict | None:
    """Compte de la session (None si absent, désactivé, ou mot de passe changé depuis la connexion)."""
    u = USERS.get(session.get("user"))
    if not u or u.get("disabled") or session.get("pwv") != u.get("pw_changed"):
        return None
    return u


def current_user(request: Request) -> dict:
    u = session_user(request.session)
    if not u:
        raise HTTPException(401, "non connecté")
    return u


def require_login(request: Request):
    current_user(request)


def require_admin(u: dict = Depends(current_user)) -> dict:
    if not is_admin(u):
        raise HTTPException(403, "réservé aux administrateurs")
    return u


class Login(BaseModel):
    login: str = ""          # vide : premier administrateur (ancienne connexion par mot de passe seul)
    password: str


@app.post("/api/login")
async def login(body: Login, request: Request):
    u = USERS.authenticate(body.login.strip().lower() or USERS.first_admin() or "", body.password)
    if not u:
        await asyncio.sleep(1.5)
        raise HTTPException(401, "identifiant ou mot de passe incorrect")
    request.session.clear()
    request.session.update(user=u["login"], pwv=u.get("pw_changed"))
    log.info("connexion de %s", u["login"])
    return {"ok": True, "must_change": bool(u.get("must_change"))}


@app.post("/api/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


def me_public(u: dict) -> dict:
    return {**USERS.public(u), "admin": is_admin(u)}


@app.get("/api/me")
async def me(request: Request):
    u = session_user(request.session)
    return {"logged_in": bool(u), "user": me_public(u) if u else None}


class MyAccount(BaseModel):
    name: str | None = None
    bot_names: dict | None = None


@app.patch("/api/me")
async def me_update(body: MyAccount, u: dict = Depends(current_user)):
    try:
        return me_public(USERS.update(u["login"], **{k: v for k, v in body.model_dump().items() if v is not None}))
    except ValueError as e:
        raise HTTPException(400, str(e))


class PasswordChange(BaseModel):
    current: str
    new: str


@app.post("/api/me/password")
async def me_password(body: PasswordChange, request: Request, u: dict = Depends(current_user)):
    if not USERS.authenticate(u["login"], body.current):
        await asyncio.sleep(1.5)
        raise HTTPException(400, "mot de passe actuel incorrect")
    if body.new == body.current:
        raise HTTPException(400, "le nouveau mot de passe doit être différent")
    try:
        USERS.set_password(u["login"], body.new)
    except ValueError as e:
        raise HTTPException(400, str(e))
    request.session["pwv"] = USERS.get(u["login"]).get("pw_changed")   # cette session reste ouverte, les autres non
    return {"ok": True}


# ----------------------------------------------------------------------------- gestion des comptes (administrateurs)
class NewUser(BaseModel):
    login: str
    name: str
    role: str = "user"
    bot_names: dict | None = None


class UserUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    bot_names: dict | None = None
    disabled: bool | None = None


@app.get("/api/users")
async def users_list(u: dict = Depends(require_admin)):
    return [USERS.public(x) for x in USERS.all()]


@app.get("/api/users/directory")
async def users_directory(u: dict = Depends(current_user)):
    """Comptes actifs (identifiant et nom), pour partager une réunion."""
    return [{"login": x["login"], "name": x["name"]} for x in USERS.all() if not x.get("disabled")]


@app.post("/api/users")
async def users_create(body: NewUser, u: dict = Depends(require_admin)):
    try:
        user, pw = USERS.create(body.login, body.name, body.role, body.bot_names)
    except ValueError as e:
        raise HTTPException(400, str(e))
    log.info("compte %s créé par %s", user["login"], u["login"])
    return {"user": user, "password": pw}


@app.patch("/api/users/{login}")
async def users_update(login: str, body: UserUpdate, u: dict = Depends(require_admin)):
    try:
        return USERS.update(login, **{k: v for k, v in body.model_dump().items() if v is not None})
    except KeyError:
        raise HTTPException(404, "compte inconnu")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/users/{login}/password")
async def users_reset_password(login: str, u: dict = Depends(require_admin)):
    """Nouveau mot de passe provisoire (à changer à la prochaine connexion) ; ses sessions sont fermées."""
    try:
        pw = USERS.set_password(login)
    except KeyError:
        raise HTTPException(404, "compte inconnu")
    log.info("mot de passe de %s réinitialisé par %s", login, u["login"])
    return {"password": pw}


@app.delete("/api/users/{login}")
async def users_delete(login: str, u: dict = Depends(require_admin)):
    if login == u["login"]:
        raise HTTPException(400, "impossible de supprimer son propre compte")
    try:
        USERS.delete(login)
    except KeyError:
        raise HTTPException(404, "compte inconnu")
    except ValueError as e:
        raise HTTPException(400, str(e))
    log.info("compte %s supprimé par %s (ses réunions restent, visibles des administrateurs)", login, u["login"])
    return {"ok": True}


AUTH = [Depends(require_login)]
CU = Depends(current_user)


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


# ----------------------------------------------------------------------------- droits sur les assistants
def can_use_worker(u: dict, st: dict) -> bool:
    """Assistant libre, ou utilisé par ce compte (les administrateurs ont accès à tout)."""
    return is_admin(u) or not st.get("owner") or st.get("owner") == u["login"]


def can_read_live(u: dict, st: dict) -> bool:
    """Transcription, analyse et diapositives en mémoire : celles de la dernière réunion de ce compte."""
    if is_admin(u) or st.get("owner") == u["login"]:
        return True
    return not st.get("owner") and st.get("last_owner") in (None, u["login"])


async def worker_for(name: str, u: dict, live: bool = False) -> dict:
    st = await worker_status(wurl(name) and name)
    if not (can_read_live(u, st) if live else can_use_worker(u, st)):
        raise HTTPException(403, "assistant utilisé par un autre utilisateur")
    return st


def public_status(u: dict, st: dict) -> dict:
    """État d'un assistant tel que ce compte peut le voir."""
    if not can_use_worker(u, st):
        return {"name": st["name"], "online": st.get("online"), "state": "taken", "other": True,
                "since": st.get("since")}
    out = {**st, "mine": bool(st.get("owner")) and st.get("owner") == u["login"]}
    if is_admin(u) and st.get("owner"):
        out["owner_name"] = USERS.names().get(st["owner"], st["owner"])
    if not can_read_live(u, st):    # dernière transcription d'un autre compte : rien n'en est montré
        out["transcript"] = {"active": False}
        out.update(title=None, url=None, recording_file=None)
    return out


@app.get("/api/config")
async def config(u: dict = CU):
    return {"iceServers": ICE, "workers": list(WORKERS), "defaultDuration": DEFAULT_DURATION,
            "defaultNames": {p: bot_name(u, p, DEFAULT_NAMES) for p in ("zoom", "teams", "web")},
            "user": me_public(u)}


@app.get("/api/workers")
async def workers(u: dict = CU):
    return [public_status(u, st) for st in await asyncio.gather(*(worker_status(n) for n in WORKERS))]


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


async def start_meeting(m: StartMeeting, u: dict) -> dict:
    target = m.worker if m.worker and m.worker != "auto" else None
    if target:
        await worker_for(target, u)
    else:
        sts = await asyncio.gather(*(worker_status(n) for n in WORKERS))
        # d'abord un assistant libre, sinon un des siens resté ouvert (navigation, erreur)
        for st in sorted(sts, key=lambda x: x.get("state") != "idle"):
            if st.get("online") and st["state"] in ("idle", "manual", "error") and can_use_worker(u, st):
                target = st["name"]
                break
    if not target:
        raise HTTPException(409, "aucun assistant libre")
    m.url = unwrap_url(m.url)
    body = m.model_dump(exclude={"worker"})
    platform = m.platform or detect_platform(m.url)
    body["platform"] = platform
    body["display_name"] = m.display_name or bot_name(u, platform, DEFAULT_NAMES)
    body["owner"], body["me_name"] = u["login"], u.get("name")
    body["duration_min"] = m.duration_min or DEFAULT_DURATION
    r = await wcall(target, "POST", "/meeting/join", json=body)
    return {**r.json(), "name": target}


@app.post("/api/meetings")
async def api_start(m: StartMeeting, u: dict = CU):
    return await start_meeting(m, u)


@app.post("/api/workers/{name}/{action}")
async def worker_action(name: str, action: str, request: Request, u: dict = CU):
    paths = {"leave": "/meeting/leave", "mute": "/meeting/mute", "rec-start": "/recording/start",
             "rec-stop": "/recording/stop", "open": "/browser/open", "reset": "/browser/reset",
             "offer": "/webrtc/offer", "tr-start": "/transcript/start", "tr-stop": "/transcript/stop",
             "speakers": "/transcript/speakers", "analyze": "/transcript/analyze"}
    if action not in paths:
        raise HTTPException(404)
    await worker_for(name, u, live=action in ("speakers", "analyze"))
    body = await request.body()
    if action in ("open", "rec-start", "tr-start"):   # l'assistant devient celui de ce compte
        try:
            d = json.loads(body or b"{}")
        except ValueError:
            raise HTTPException(400, "JSON invalide")
        body = json.dumps({**d, "owner": u["login"], "me_name": u.get("name")}).encode()
    r = await wcall(name, "POST", paths[action], content=body or b"{}",
                    headers={"Content-Type": "application/json"}, timeout=60)
    return JSONResponse(r.json())


@app.get("/api/workers/{name}/transcript")
async def live_transcript(name: str, after: int = 0, u: dict = CU):
    await worker_for(name, u, live=True)
    r = await wcall(name, "GET", f"/transcript?after={after}", timeout=5)
    return JSONResponse(r.json(), headers={"Cache-Control": "no-store"})


@app.get("/api/workers/{name}/slides")
async def live_slides(name: str, after: int = 0, u: dict = CU):
    await worker_for(name, u, live=True)
    r = await wcall(name, "GET", f"/transcript/slides?after={after}", timeout=5)
    return JSONResponse(r.json(), headers={"Cache-Control": "no-store"})


@app.get("/api/slides/{name}/{stem}/{n}")
async def slide_image(name: str, stem: str, n: int, u: dict = CU):
    rec = next((x for x in await worker_recordings(name) if _stem(x["name"]) == stem), None)
    if rec:                                 # diapositive d'une réunion enregistrée : il faut y avoir droit
        if not can_see(u, rec.get("meta")):
            raise HTTPException(403, "réunion d'un autre utilisateur")
    else:                                   # réunion en cours, pas encore listée : celle de ce compte
        st = await worker_for(name, u, live=True)
        if _stem(((st.get("transcript") or {}).get("file") or "")) != stem and not is_admin(u):
            raise HTTPException(403, "diapositive inconnue")
    r = await wcall(name, "GET", f"/slides/{stem}/{n}", timeout=10)
    return Response(r.content, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/recordings/{name}/{file}/slides")
async def recording_slides(name: str, file: str, u: dict = CU):
    await rec_meta(name, file, u, edit=True)
    return (await wcall(name, "POST", f"/recordings/{file}/slides")).json()


@app.get("/api/workers/{name}/analysis")
async def live_analysis(name: str, u: dict = CU):
    await worker_for(name, u, live=True)
    r = await wcall(name, "GET", "/transcript/analysis", timeout=5)
    return JSONResponse(r.json(), headers={"Cache-Control": "no-store"})


class SpeakerName(BaseModel):
    id: str
    name: str | None = None


@app.patch("/api/recordings/{name}/{file}/speakers")
async def recording_speaker_rename(name: str, file: str, body: SpeakerName, u: dict = CU):
    await rec_meta(name, file, u, edit=True)
    return (await wcall(name, "PATCH", f"/recordings/{file}/speakers", json=body.model_dump())).json()


@app.get("/api/workers/{name}/screenshot")
async def screenshot(name: str, w: int = 640, u: dict = CU):
    await worker_for(name, u)
    r = await wcall(name, "GET", f"/screenshot?w={w}")
    return Response(r.content, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ----------------------------------------------------------------------------- enregistrements
_rec_cache: dict[str, tuple[float, list]] = {}


async def worker_recordings(n: str, fresh: bool = False) -> list[dict]:
    """Enregistrements d'un assistant (gardés 10 s : chaque lecture de fichier vérifie les droits)."""
    hit = _rec_cache.get(n)
    if hit and not fresh and time.monotonic() - hit[0] < 10:
        return hit[1]
    try:
        items = [{**x, "worker": n} for x in (await http.get(wurl(n) + "/recordings", timeout=5)).json()]
    except Exception:
        return hit[1] if hit else []
    _rec_cache[n] = (time.monotonic(), items)
    return items


def _stem(file: str) -> str:
    return file.removesuffix(".mp4").removesuffix(".transcript.json")


async def rec_meta(name: str, file: str, u: dict, edit: bool = False, must_exist: bool = True) -> dict | None:
    """Métadonnées d'une réunion si ce compte y a droit (lecture, ou modification si edit), sinon 403/404."""
    wurl(name)
    stem = _stem(file)
    for fresh in (False, True):
        rec = next((x for x in await worker_recordings(name, fresh) if _stem(x["name"]) == stem), None)
        if rec:
            break
    if not rec:
        if must_exist:
            raise HTTPException(404, "réunion inconnue")
        return None
    meta = rec.get("meta") or {}
    if not (can_edit(u, meta) if edit else can_see(u, meta)):
        if must_exist or edit:
            raise HTTPException(403, "réunion d'un autre utilisateur")
        return None
    return meta


@app.get("/api/recordings")
async def recordings(u: dict = CU):
    lists = await asyncio.gather(*(worker_recordings(n, fresh=True) for n in WORKERS))
    names = USERS.names()
    out = []
    for x in (x for l in lists for x in l):
        meta = x.get("meta") or {}
        if not can_see(u, meta):
            continue
        owner = meta.get("owner")
        out.append({**x, "owner": owner, "owner_name": names.get(owner, owner) if owner else None,
                    "mine": owner == u["login"], "can_edit": can_edit(u, meta),
                    "shared_with": meta.get("shared_with") or []})
    return sorted(out, key=lambda x: -x["mtime"])


class Access(BaseModel):
    shared_with: list[str] | None = None
    owner: str | None = None      # administrateurs seulement


@app.put("/api/recordings/{name}/{file}/access")
async def recording_access(name: str, file: str, body: Access, u: dict = CU):
    """Partager une réunion avec d'autres comptes (lecture seule), ou en changer le propriétaire (administrateur)."""
    meta = await rec_meta(name, file, u, edit=True)
    known = {x["login"] for x in USERS.all()}
    fields = {}
    if body.shared_with is not None:
        bad = [x for x in body.shared_with if x not in known]
        if bad:
            raise HTTPException(400, "compte(s) inconnu(s) : " + ", ".join(bad))
        fields["shared_with"] = [x for x in body.shared_with if x != (body.owner or meta.get("owner"))]
    if body.owner is not None:
        if not is_admin(u):
            raise HTTPException(403, "seul un administrateur change le propriétaire")
        if body.owner not in known:
            raise HTTPException(400, "compte inconnu")
        fields["owner"] = body.owner
    r = await wcall(name, "PATCH", f"/recordings/{file}/meta", json=fields)
    _rec_cache.pop(name, None)
    return r.json()


@app.get("/api/recordings/{name}/{file}")
async def recording_get(name: str, file: str, request: Request, download: int = 0, u: dict = CU):
    """Relais en streaming avec support Range (lecture vidéo sur iPhone/iPad)."""
    await rec_meta(name, file, u)
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


@app.get("/api/recordings/{name}/{file}/transcript")
async def recording_transcript(name: str, file: str, fmt: str = "json", download: int = 0, u: dict = CU):
    await rec_meta(name, file, u)
    r = await wcall(name, "GET", f"/recordings/{file}/transcript?fmt={'md' if fmt == 'md' else 'json'}")
    if fmt != "md":
        return JSONResponse(r.json())
    stem = file.removesuffix(".mp4").removesuffix(".transcript.json")
    return Response(r.content, media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{stem}.md"'})


REPORT_TYPES = {"md": ("text/markdown; charset=utf-8", "md"),
                "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx")}


@app.get("/api/recordings/{name}/{file}/report")
async def recording_report(name: str, file: str, fmt: str = "json", download: int = 0, variant: str = "travail",
                           u: dict = CU):
    await rec_meta(name, file, u)
    variant = "diffusion" if variant == "diffusion" else "travail"
    r = await wcall(name, "GET", f"/recordings/{file}/report?fmt={fmt if fmt in REPORT_TYPES else 'json'}&variant={variant}",
                    timeout=60)
    if fmt not in REPORT_TYPES:
        return JSONResponse(r.json(), headers={"Cache-Control": "no-store"})
    mime, ext = REPORT_TYPES[fmt]
    stem = file.removesuffix(".mp4").removesuffix(".transcript.json")
    label = "compte rendu (diffusion)" if variant == "diffusion" else "compte rendu"
    return Response(r.content, media_type=mime, headers={
        "Content-Disposition": f'{"attachment" if download else "inline"}; filename="{stem} - {label}.{ext}"'})


@app.post("/api/recordings/{name}/{file}/report")
async def recording_report_build(name: str, file: str, u: dict = CU):
    await rec_meta(name, file, u, edit=True)
    return (await wcall(name, "POST", f"/recordings/{file}/report")).json()


@app.delete("/api/recordings/{name}/{file}")
async def recording_delete(name: str, file: str, u: dict = CU):
    await rec_meta(name, file, u, edit=True)
    _rec_cache.pop(name, None)
    return (await wcall(name, "DELETE", f"/recordings/{file}")).json()


@app.get("/api/recordings/{name}/{file}/thumb")
async def recording_thumb(name: str, file: str, u: dict = CU):
    await rec_meta(name, file, u)
    r = await wcall(name, "GET", f"/recordings/{file}/thumb")
    return Response(r.content, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


class Rename(BaseModel):
    title: str


@app.patch("/api/recordings/{name}/{file}")
async def recording_rename(name: str, file: str, body: Rename, u: dict = CU):
    await rec_meta(name, file, u, edit=True)
    _rec_cache.pop(name, None)
    return (await wcall(name, "PATCH", f"/recordings/{file}", json=body.model_dump())).json()


@app.get("/api/recordings/{name}/{file}/final")
async def recording_final(name: str, file: str, fmt: str = "md", download: int = 0, u: dict = CU):
    """Compte rendu final déposé par Claude (outil MCP attach_cr)."""
    await rec_meta(name, file, u)
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


def _sched_owner(x: dict) -> str | None:
    return x.get("owner") or USERS.first_admin()   # planifications d'avant les comptes : au premier administrateur


@app.get("/api/schedule")
async def schedule_list(u: dict = CU):
    names = USERS.names()
    return sorted(({**x, "owner_name": names.get(_sched_owner(x))} for x in load_schedule()
                   if is_admin(u) or _sched_owner(x) == u["login"]), key=lambda x: x["when"])


@app.post("/api/schedule")
async def schedule_add(item: ScheduleItem, u: dict = CU):
    if item.when.tzinfo is None:
        raise HTTPException(400, "date sans fuseau horaire")
    items = load_schedule()
    entry = {**json.loads(item.model_dump_json()), "id": uuid.uuid4().hex[:10], "status": "pending", "owner": u["login"]}
    items.append(entry)
    save_schedule(items)
    return entry


@app.delete("/api/schedule/{sid}")
async def schedule_del(sid: str, u: dict = CU):
    items = load_schedule()
    it = next((x for x in items if x["id"] == sid), None)
    if it and not (is_admin(u) or _sched_owner(it) == u["login"]):
        raise HTTPException(403, "planification d'un autre utilisateur")
    save_schedule([x for x in items if x["id"] != sid])
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
            owner = USERS.get(_sched_owner(it))
            if not owner or owner.get("disabled"):
                it["status"], it["result"] = "missed", "compte supprimé ou désactivé"
                continue
            try:
                res = await start_meeting(StartMeeting(**{k: it[k] for k in StartMeeting.model_fields if it.get(k) is not None}), owner)
                it["status"], it["result"] = "started", res["name"]
                log.info("réunion planifiée lancée sur %s", res["name"])
            except HTTPException as e:
                # on réessaie au prochain tour tant qu'on est dans la fenêtre de 30 min
                it["status"], it["result"] = "pending", f"en attente: {e.detail}"
        if changed:
            save_schedule(items)


# ----------------------------------------------------------------------------- consentements (empreintes vocales)
CONSENT_TEXT = ("J'atteste sur l'honneur avoir reçu le consentement explicite de cette personne à l'utilisation de son "
                "empreinte vocale pour la reconnaître dans les réunions enregistrées par OmniConf.")


def load_consents() -> list[dict]:
    try:
        return json.loads(CONSENT_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return []


def save_consents(items: list[dict]):
    tmp = CONSENT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(CONSENT_FILE)


class ConsentPerson(BaseModel):
    name: str
    note: str | None = None


class ConsentSet(BaseModel):
    consent: bool | None = None
    note: str | None = None
    attest: bool = False          # case « sur l'honneur » cochée dans la fenêtre de confirmation


@app.get("/api/voice-consents")
async def consents_list(u: dict = Depends(require_admin)):
    names = USERS.names()
    items = [{**x, "by_name": names.get(x.get("by"), x.get("by"))} for x in load_consents()]
    return {"items": sorted(items, key=lambda x: (not x.get("consent"), x["name"].lower())), "attestation": CONSENT_TEXT}


@app.post("/api/voice-consents")
async def consents_add(body: ConsentPerson, u: dict = Depends(require_admin)):
    name = " ".join(body.name.split())[:80]
    if len(name) < 3:
        raise HTTPException(400, "nom obligatoire (Prénom Nom)")
    items = load_consents()
    if any(x["name"].lower() == name.lower() for x in items):
        raise HTTPException(400, "cette personne est déjà dans la liste")
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    entry = {"id": uuid.uuid4().hex[:8], "name": name, "consent": False, "at": None, "by": None,
             "note": (body.note or "").strip()[:300] or None,
             "history": [{"at": now, "by": u["login"], "action": "ajout"}]}
    save_consents(items + [entry])
    return entry


@app.put("/api/voice-consents/{cid}")
async def consents_set(cid: str, body: ConsentSet, u: dict = Depends(require_admin)):
    items = load_consents()
    x = next((i for i in items if i["id"] == cid), None)
    if not x:
        raise HTTPException(404, "personne inconnue")
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    if body.note is not None:
        x["note"] = body.note.strip()[:300] or None
    if body.consent is True and not x["consent"]:
        if not body.attest:
            raise HTTPException(400, "l'attestation sur l'honneur doit être cochée")
        x.update(consent=True, at=now, by=u["login"])
        x["history"].append({"at": now, "by": u["login"], "action": "consentement attesté", "texte": CONSENT_TEXT})
        log.info("consentement (empreinte vocale) de %s attesté par %s", x["name"], u["login"])
        save_consents(items)
        vp_start_build([x["name"]], u["login"])          # empreinte calculée tout de suite (tâche de fond)
        return x
    elif body.consent is False and x["consent"]:
        x.update(consent=False, at=now, by=u["login"])
        save_consents(items)
        gone = vp_forget(x["name"])                       # effacée du contrôle, puis des assistants
        pushed = await voiceprints_push()
        x["history"].append({"at": now, "by": u["login"], "action": "consentement retiré : empreinte effacée"
                             + ("" if gone else " (aucune n'avait été calculée)")
                             + f" ; assistants : {', '.join(f'{k} {v}' for k, v in pushed.items()) or 'aucun'}"})
        log.info("consentement (empreinte vocale) de %s retiré par %s ; empreinte effacée", x["name"], u["login"])
    save_consents(items)
    return x


@app.delete("/api/voice-consents/{cid}")
async def consents_delete(cid: str, u: dict = Depends(require_admin)):
    items = load_consents()
    x = next((i for i in items if i["id"] == cid), None)
    if not x:
        raise HTTPException(404, "personne inconnue")
    if x["consent"]:
        raise HTTPException(400, "retirer d'abord le consentement (décocher), puis supprimer la ligne")
    save_consents([i for i in items if i["id"] != cid])
    return {"ok": True}


# ----------------------------------------------------------------------------- empreintes vocales de référence
# Calculées par les assistants sur leurs enregistrements (phrases dont l'auteur est certain), moyennées ici, puis
# renvoyées à chaque assistant : seulement pour les personnes dont le consentement est attesté (et coché).
vp_state: dict = {"job": None}


def vp_load() -> dict:
    try:
        return json.loads(VOICEPRINT_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def vp_save(d: dict):
    tmp = VOICEPRINT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    tmp.replace(VOICEPRINT_FILE)


def vp_consented() -> list[str]:
    return [x["name"] for x in load_consents() if x.get("consent")]


def vp_forget(name: str) -> bool:
    d = vp_load()
    gone = d.pop(name, None) is not None
    if gone:
        vp_save(d)
    return gone


def vp_payload() -> dict:
    """Empreintes envoyées aux assistants : personnes cochées au moment de l'envoi, rien d'autre."""
    ok = set(vp_consented())
    return {n: {k: p.get(k) for k in ("v", "sec", "n", "meetings", "built_at")} for n, p in vp_load().items() if n in ok}


def vp_version(payload: dict) -> str | None:
    """Même empreinte que worker/voiceprints.py (après arrondi à 6 décimales côté assistant)."""
    if not payload:
        return None
    norm = {n: {"v": [round(float(x), 6) for x in p["v"]], "sec": float(p.get("sec") or 0), "n": int(p.get("n") or 0),
                "meetings": int(p.get("meetings") or 0), "built_at": p.get("built_at")} for n, p in payload.items()}
    return hashlib.sha1(json.dumps(norm, sort_keys=True).encode()).hexdigest()[:12]


async def voiceprints_push(names=None) -> dict:
    """Envoie l'ensemble des empreintes aux assistants qui n'ont pas la bonne version. {assistant: état}."""
    payload = vp_payload()
    want = vp_version(payload)
    out = {}

    async def one(n):
        st = await worker_status(n)
        if not st.get("online"):
            out[n] = "injoignable"
            return
        if st.get("voiceprints") == want:
            out[n] = "à jour"
            return
        try:
            r = await http.put(wurl(n) + "/voiceprints", json={"prints": payload}, timeout=15)
            out[n] = "à jour" if r.status_code < 400 else f"erreur {r.status_code}"
        except Exception as e:
            out[n] = f"erreur : {str(e)[:80]}"
    await asyncio.gather(*(one(n) for n in (names or WORKERS)))
    return out


async def voiceprints_build(names: list[str], by: str):
    """Demande le calcul à chaque assistant libre, fait la moyenne, enregistre puis diffuse."""
    job = vp_state["job"]
    owners = USERS.names()

    async def one(n):
        try:
            r = await http.post(wurl(n) + "/voiceprints/extract", json={"names": names, "owners": owners}, timeout=15)
            if r.status_code >= 400:
                job["workers"][n] = "occupé (réunion en cours)" if r.status_code == 409 else f"erreur {r.status_code}"
                return None
            job["workers"][n] = "calcul en cours"
            for _ in range(720):                     # 1 h au plus
                await asyncio.sleep(5)
                g = (await http.get(wurl(n) + "/voiceprints/extract", timeout=15)).json()
                if g.get("state") == "done":
                    job["workers"][n] = "terminé"
                    return g.get("result") or {}
                if g.get("state") in ("error", "fetched", None):
                    job["workers"][n] = f"erreur : {g.get('error') or g.get('state')}"
                    return None
            job["workers"][n] = "trop long"
        except Exception as e:
            job["workers"][n] = f"injoignable ({str(e)[:60]})"
        return None
    results = await asyncio.gather(*(one(n) for n in WORKERS))
    ok = set(vp_consented())          # consentement retiré pendant le calcul : rien n'est gardé
    d = vp_load()
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    for name in names:
        if name not in ok:
            d.pop(name, None)
            continue
        tot, n, sec, meetings = None, 0, 0.0, set()
        kinds: dict[str, int] = {}
        for res in results:
            r = (res or {}).get(name)
            if not r or not r.get("n"):
                continue
            v = r["sum"]
            tot = list(v) if tot is None else [a + b for a, b in zip(tot, v)]
            n += r["n"]
            sec += r.get("sec") or 0
            meetings |= set(r.get("meetings") or {})
            for k, c in (r.get("kinds") or {}).items():
                kinds[k] = kinds.get(k, 0) + c
        if tot is None:
            job["people"][name] = "aucun passage sûr dans les enregistrements"
            continue
        norm = sum(x * x for x in tot) ** 0.5 or 1.0
        d[name] = {"v": [x / norm for x in tot], "n": n, "sec": round(sec, 1), "meetings": len(meetings),
                   "kinds": kinds, "built_at": now, "by": by}
        job["people"][name] = f"{round(sec / 60, 1)} min de voix, {len(meetings)} réunion(s)"
    vp_save(d)
    job["push"] = await voiceprints_push()
    job.update(state="done", ended=now)
    log.info("empreintes vocales calculées : %s", job["people"])


def vp_start_build(names: list[str], by: str) -> dict:
    if vp_state["job"] and vp_state["job"].get("state") == "running":
        return vp_state["job"]
    names = [n for n in names if n in set(vp_consented())]
    vp_state["job"] = {"state": "running", "names": names, "by": by, "workers": {}, "people": {},
                       "started": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    if names:
        asyncio.create_task(voiceprints_build(names, by))
    else:
        vp_state["job"].update(state="done", ended=vp_state["job"]["started"])
    return vp_state["job"]


@app.get("/api/voiceprints")
async def voiceprints_status(u: dict = Depends(require_admin)):
    """Empreintes des personnes cochées (durées, réunions ; jamais les vecteurs) et diffusion aux assistants."""
    d, ok = vp_load(), vp_consented()
    st = await asyncio.gather(*(worker_status(n) for n in WORKERS))
    want = vp_version(vp_payload())
    return {"people": {n: ({k: d[n].get(k) for k in ("sec", "n", "meetings", "kinds", "built_at")} if n in d else None) for n in ok},
            "workers": {x["name"]: ("injoignable" if not x.get("online") else "à jour" if x.get("voiceprints") == want
                                    else "en attente") for x in st},
            "job": vp_state["job"]}


@app.post("/api/voiceprints/build")
async def voiceprints_rebuild(u: dict = Depends(require_admin)):
    return vp_start_build(vp_consented(), u["login"])


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
    """Workers redémarrés ou recréés : ils retrouvent le lexique et les empreintes vocales au plus tard 2 minutes
    après (une empreinte effacée disparaît aussi d'un assistant qui était injoignable). Empreintes recalculées
    chaque nuit (nouvelles réunions)."""
    while True:
        try:
            await lexicon_push()
        except Exception as e:  # pragma: no cover
            log.warning("lexique : %s", e)
        try:
            await voiceprints_push()
            now = dt.datetime.now().astimezone()
            last = (vp_state.get("job") or {}).get("started") or ""
            if now.hour == 3 and not last.startswith(now.date().isoformat()) and vp_consented():
                vp_start_build(vp_consented(), "nuit")
        except Exception as e:  # pragma: no cover
            log.warning("empreintes vocales : %s", e)
        await asyncio.sleep(120)


class LexiconBody(BaseModel):
    text: str


@app.get("/api/lexicon", dependencies=AUTH)
async def lexicon_get():
    """Lexique (lecture pour tous les comptes ; modification par les administrateurs)."""
    text = lexicon_text()
    st = await asyncio.gather(*(worker_status(n) for n in WORKERS))
    mt = LEXICON_FILE.stat().st_mtime if text is not None else None
    return {"text": text or "", "version": lexicon_version(text or ""),
            "updated_at": dt.datetime.fromtimestamp(mt).astimezone().isoformat() if mt else None,
            "workers": {x["name"]: x.get("lexicon") if x.get("online") else "injoignable" for x in st}}


@app.put("/api/lexicon")
async def lexicon_put(body: LexiconBody, u: dict = Depends(require_admin)):
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
    u = session_user(ws.session)
    if not u or name not in WORKERS or not can_use_worker(u, await worker_status(name)):
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
                       site=os.environ.get("SITE_ADDRESS"), require_login=Depends(require_login),
                       users=USERS, current_user=current_user)


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
