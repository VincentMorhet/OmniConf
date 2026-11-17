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
from urllib.parse import quote

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
VOICE_Q_FILE = DATA / "voice_questions.json"  # « Est-ce X ? » : voix douteuses à confirmer, et réponses données
SESSIONS_FILE = DATA / "sessions.json"        # sessions Zoom des comptes (chiffrées), chargées le temps d'une réunion
SESSIONS_KEY = DATA / "sessions.key"          # clé de chiffrement de ces sessions (mode 600)
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
    sess_forget(login)                # sa session Zoom aussi
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
    """Assistant libre, ou utilisé par ce compte (les administrateurs ont accès à tout) ; un assistant qui porte la
    session Zoom d'une personne : seulement elle, administrateurs compris."""
    if st.get("session_of"):
        return st["session_of"] == u["login"]
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
            "zoomSession": sess_info(u["login"]),
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
    as_me: bool | None = None  # Zoom avec son compte (sa session) : True exigé, False invité, None si possible


# Sessions Zoom : chaque compte se connecte une fois à Zoom dans l'écran d'un assistant ; ses cookies Zoom sont gardés
# ici, chiffrés, puis chargés dans l'assistant qui part pour une de ses réunions Zoom et retirés à la fin (rafraîchis
# par Zoom entre-temps). Une seule réunion à la fois avec sa session ; pendant ce temps, lui seul a accès à l'assistant.
def _fernet():
    from cryptography.fernet import Fernet
    if not SESSIONS_KEY.exists():
        tmp = SESSIONS_KEY.with_suffix(".tmp")
        tmp.write_bytes(Fernet.generate_key())
        os.chmod(tmp, 0o600)
        tmp.replace(SESSIONS_KEY)
    return Fernet(SESSIONS_KEY.read_bytes().strip())


def _sess_all() -> dict:
    try:
        return json.loads(SESSIONS_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _sess_write(d: dict):
    tmp = SESSIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    os.chmod(tmp, 0o600)
    tmp.replace(SESSIONS_FILE)


def sess_info(login: str) -> dict | None:
    """Ce que l'IHM peut savoir d'une session (jamais les cookies)."""
    z = (_sess_all().get(login) or {}).get("zoom")
    return {k: z.get(k) for k in ("saved_at", "used_at", "expired", "expired_at")} if z else None


def sess_get(login: str) -> list | None:
    z = (_sess_all().get(login) or {}).get("zoom")
    if not z or z.get("expired"):
        return None
    try:
        return json.loads(_fernet().decrypt(z["enc"].encode()))
    except Exception as e:
        log.warning("session Zoom de %s illisible : %s", login, type(e).__name__)
        return None


def sess_put(login: str, cookies: list, refreshed: bool = False):
    d = _sess_all()
    old = (d.get(login) or {}).get("zoom") or {}
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    d.setdefault(login, {})["zoom"] = {"enc": _fernet().encrypt(json.dumps(cookies).encode()).decode(),
                                       "saved_at": old.get("saved_at") if refreshed else now,
                                       "used_at": now if refreshed else old.get("used_at"), "expired": False}
    _sess_write(d)


def sess_expire(login: str):
    d = _sess_all()
    z = (d.get(login) or {}).get("zoom")
    if z:
        z.update(expired=True, expired_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"), enc="")
        _sess_write(d)


def sess_forget(login: str) -> bool:
    d = _sess_all()
    gone = d.pop(login, None) is not None
    if gone:
        _sess_write(d)
    return gone


async def start_meeting(m: StartMeeting, u: dict) -> dict:
    m.url = unwrap_url(m.url)
    platform = m.platform or detect_platform(m.url)
    sts = await asyncio.gather(*(worker_status(n) for n in WORKERS))
    cookies = sess_get(u["login"]) if platform == "zoom" and m.as_me is not False else None
    note = None
    if platform == "zoom" and m.as_me is True and not cookies:
        raise HTTPException(409, "pas de session Zoom enregistrée (ou expirée) : Réglages > Votre compte Zoom > « Se connecter"
                                 " à Zoom », ou décochez « avec mon compte »")
    busy = next((st["name"] for st in sts if st.get("session_of") == u["login"]), None)
    if cookies and busy:              # une seule réunion à la fois avec sa session (limites d'appareils de Zoom)
        if m.as_me:
            raise HTTPException(409, f"votre session Zoom est déjà utilisée par l'assistant {busy} : une réunion à la "
                                     "fois avec votre compte ; décochez « avec mon compte » pour celle-ci")
        cookies, note = None, f"votre session Zoom est déjà utilisée par l'assistant {busy} : entrée en invité"
    target = m.worker if m.worker and m.worker != "auto" else None
    if target:
        await worker_for(target, u)
    else:
        # d'abord un assistant libre, sinon un des siens resté ouvert (navigation, erreur)
        for st in sorted(sts, key=lambda x: (x.get("state") != "idle", bool((x.get("imports") or {}).get("busy")))):
            if st.get("online") and st["state"] in ("idle", "manual", "error") and can_use_worker(u, st):
                target = st["name"]
                break
    if not target:
        raise HTTPException(409, "aucun assistant libre")
    body = m.model_dump(exclude={"worker"})
    body["platform"] = platform
    body.update(as_me=bool(cookies), as_me_required=m.as_me is True, session=cookies)
    body["display_name"] = m.display_name or bot_name(u, platform, DEFAULT_NAMES)
    body["owner"], body["me_name"] = u["login"], u.get("name")
    body["duration_min"] = m.duration_min or DEFAULT_DURATION
    r = await wcall(target, "POST", "/meeting/join", json=body)
    if cookies:
        log.info("réunion Zoom de %s sur %s avec sa session", u["login"], target)
    return {**r.json(), "name": target, "as_me": bool(cookies), **({"note": note} if note else {})}


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


# ----------------------------------------------------------------------------- compte Zoom (session de chacun)
async def _capture_worker(u: dict) -> str | None:
    sts = await asyncio.gather(*(worker_status(n) for n in WORKERS))
    return next((st["name"] for st in sts if st.get("session_of") == u["login"] and st.get("session_kind") == "capture"), None)


@app.get("/api/zoom-account")
async def zoom_account(u: dict = CU):
    """Session Zoom de ce compte (dates, jamais les cookies) et connexion en cours éventuelle."""
    return {"session": sess_info(u["login"]), "capture": await _capture_worker(u)}


@app.post("/api/zoom-account/signin")
async def zoom_signin(u: dict = CU):
    """Ouvre la page de connexion Zoom sur un assistant libre : la personne s'y connecte elle-même par l'écran distant
    (le mot de passe ne passe pas par OmniConf ; elle seule a accès à cet assistant pendant ce temps)."""
    w = await _capture_worker(u)
    if not w:
        sts = await asyncio.gather(*(worker_status(n) for n in WORKERS))
        w = next((st["name"] for st in sts if st.get("online") and st["state"] == "idle" and can_use_worker(u, st)), None)
    if not w:
        raise HTTPException(409, "aucun assistant libre pour se connecter à Zoom")
    await wcall(w, "POST", "/zoom/capture/start", json={"owner": u["login"], "me_name": u.get("name")}, timeout=60)
    return {"name": w}


@app.post("/api/zoom-account/finish")
async def zoom_finish(u: dict = CU):
    """Connexion terminée : la session est reprise de l'assistant (puis retirée de son navigateur) et gardée chiffrée."""
    w = await _capture_worker(u)
    if not w:
        raise HTTPException(404, "pas de connexion Zoom en cours")
    r = (await wcall(w, "POST", "/zoom/capture/finish", json={"owner": u["login"]}, timeout=60)).json()
    if not r.get("signed_in") or not r.get("cookies"):
        raise HTTPException(409, "pas encore connecté à Zoom dans l'écran de l'assistant")
    sess_put(u["login"], r["cookies"])
    log.info("session Zoom de %s enregistrée (%d cookies)", u["login"], len(r["cookies"]))
    return {"session": sess_info(u["login"])}


@app.post("/api/zoom-account/cancel")
async def zoom_cancel(u: dict = CU):
    w = await _capture_worker(u)
    if w:
        await wcall(w, "POST", "/zoom/capture/cancel", json={"owner": u["login"]}, timeout=60)
    return {"ok": True}


@app.delete("/api/zoom-account")
async def zoom_forget(u: dict = CU):
    """Oublier sa session Zoom (elle reste ouverte chez Zoom : « Se déconnecter de tous les appareils » pour la couper)."""
    sess_forget(u["login"])
    log.info("session Zoom de %s oubliée", u["login"])
    return {"ok": True}


async def sessions_collect():
    """Sessions rendues par les assistants en fin de réunion (rafraîchies par Zoom), ou expirées pendant la réunion."""
    for st in await asyncio.gather(*(worker_status(n) for n in WORKERS)):
        if not st.get("session_out"):
            continue
        try:
            o = (await wcall(st["name"], "GET", "/zoom/session/out", timeout=10)).json()
        except Exception:
            continue
        who = o.get("owner")
        if not who or who not in USERS.names():
            continue
        if o.get("cookies"):
            sess_put(who, o["cookies"], refreshed=o.get("kind") == "meeting")
            log.info("session Zoom de %s rendue par %s (%s)", who, st["name"], o.get("kind"))
        elif o.get("kind") == "meeting":           # déconnecté par Zoom pendant la réunion
            sess_expire(who)
            log.info("session Zoom de %s expirée (rendue par %s)", who, st["name"])


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


class BulkAccess(BaseModel):
    items: list[dict]              # [{"worker": "w1", "file": "….mp4"}, …]
    add: list[str] = []            # comptes à qui partager
    remove: list[str] = []         # comptes à qui retirer le partage


@app.post("/api/recordings/access-bulk")
async def recordings_access_bulk(body: BulkAccess, u: dict = CU):
    """Partager (ou ne plus partager) plusieurs réunions d'un coup ; seules celles que ce compte peut modifier."""
    known = {x["login"] for x in USERS.all()}
    bad = [x for x in body.add + body.remove if x not in known]
    if bad:
        raise HTTPException(400, "compte(s) inconnu(s) : " + ", ".join(bad))
    if not body.items or len(body.items) > 2000 or not (body.add or body.remove):
        raise HTTPException(400, "rien à faire")
    sem = asyncio.Semaphore(8)

    async def one(it: dict):
        w, f = str(it.get("worker") or ""), str(it.get("file") or "")
        async with sem:
            try:
                meta = await rec_meta(w, f, u, edit=True)
                cur = sorted(set(meta.get("shared_with") or []))
                new = sorted((set(cur) | set(body.add)) - set(body.remove) - {meta.get("owner")})
                if new != cur:
                    await wcall(w, "PATCH", f"/recordings/{f}/meta", json={"shared_with": new})
                return None
            except HTTPException as e:
                return {"worker": w, "file": f, "error": str(e.detail)}
    res = await asyncio.gather(*(one(it) for it in body.items))
    for w in {str(it.get("worker")) for it in body.items}:
        _rec_cache.pop(w, None)
    failed = [r for r in res if r]
    log.info("partage groupé par %s : %d réunion(s), +%s -%s, %d refus", u["login"], len(body.items) - len(failed),
             ",".join(body.add) or "-", ",".join(body.remove) or "-", len(failed))
    return {"done": len(body.items) - len(failed), "failed": failed}


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


# ----------------------------------------------------------------------------- import d'enregistrements
# Vidéo ou son d'une réunion passée, envoyé par morceaux à un assistant (qui convertit, transcrit après coup et
# rédige le compte rendu : worker/importer.py). Chacun ne voit et ne gère que ses imports (administrateurs : tous).
CHUNK_MAX = 64 * 1024 * 1024


class ImportStart(BaseModel):
    file: str
    size: int
    last_modified: int | None = None
    title: str | None = None
    started_at: str | None = None
    platform: str | None = None
    transcribe: bool = True
    cuts: list[list[float | None]] | None = None    # passages privés : [[début, fin], …] en s, fin vide = jusqu'au bout
    notes: str | None = None                         # consignes pour le premier compte rendu


def import_user(request: Request) -> dict:
    """Compte connecté à l'IHM, ou jeton de portée « import » ou « write » (script d'envoi : « Authorization: Bearer
    wci_… » ou « wcw_… » ; le jeton d'écriture peut importer depuis le 16/11/2026, à la demande de l'utilisateur)."""
    u = session_user(request.session)
    if u:
        return u
    auth = request.headers.get("authorization", "")
    e = mcp.tokens.check(auth[7:].strip()) if auth.lower().startswith("bearer ") else None
    if not e or e.get("scope") not in ("import", "write"):
        raise HTTPException(401, "non connecté (ou jeton d'import absent, invalide ou révoqué)")
    u = USERS.get(e.get("user") or USERS.first_admin())
    if not u or u.get("disabled"):
        raise HTTPException(401, "compte du jeton désactivé ou supprimé")
    return u


IU = Depends(import_user)


def _imp_owner(u: dict) -> dict:
    return {} if is_admin(u) else {"owner": u["login"]}


async def worker_imports(n: str, owner: str | None = None) -> list[dict]:
    try:
        r = await http.get(wurl(n) + "/imports", params={"owner": owner} if owner else None, timeout=5)
        return [{**x, "worker": n} for x in r.json()] if r.status_code == 200 else []
    except Exception:
        return []


async def _free_disk(n: str) -> int:
    try:
        return int((await http.get(wurl(n) + "/disk", timeout=3)).json().get("free") or 0)
    except Exception:
        return 0


@app.post("/api/imports")
async def import_start(body: ImportStart, u: dict = IU):
    """Nouvel import, ou reprise d'un envoi laissé en plan (même fichier : même nom, taille et date)."""
    fname = os.path.basename(body.file or "")
    mine = [x for l in await asyncio.gather(*(worker_imports(n, u["login"]) for n in WORKERS)) for x in l]
    same = next((x for x in mine if x["stage"] == "upload" and x["file"] == fname and x["size"] == body.size
                 and x.get("last_modified") == body.last_modified), None)
    if same:
        target = same["worker"]
    else:
        sts = await asyncio.gather(*(worker_status(n) for n in WORKERS))
        free = dict(zip(WORKERS, await asyncio.gather(*(_free_disk(n) for n in WORKERS))))
        cands = [st for st in sts if st.get("online") and "imports" in st]     # assistants à jour seulement
        if not cands:
            raise HTTPException(503, "aucun assistant disponible pour l'import : réessayez dans quelques minutes")
        # d'abord un assistant sans import en cours, libre (pas en réunion), avec le plus de place
        cands.sort(key=lambda st: (bool((st.get("imports") or {}).get("busy")), st.get("state") != "idle",
                                   -free.get(st["name"], 0)))
        target = cands[0]["name"]
    r = await wcall(target, "POST", "/imports", json={**body.model_dump(), "file": fname, "owner": u["login"],
                                                       "me_name": u.get("name")})
    if not same:
        log.info("import de %s par %s sur %s (%d octets)", fname, u["login"], target, body.size)
    return {**r.json(), "worker": target}


@app.get("/api/imports")
async def imports_list(u: dict = IU):
    owner = None if is_admin(u) else u["login"]
    names = USERS.names()
    out = [{**x, "owner_name": names.get(x.get("owner"), x.get("owner")), "mine": x.get("owner") == u["login"]}
           for l in await asyncio.gather(*(worker_imports(n, owner) for n in WORKERS)) for x in l]
    return sorted(out, key=lambda x: -(x.get("created") or 0))


@app.get("/api/imports/{name}/{jid}")
async def import_get(name: str, jid: str, u: dict = IU):
    return {**(await wcall(name, "GET", f"/imports/{jid}", params=_imp_owner(u))).json(), "worker": name}


@app.put("/api/imports/{name}/{jid}")
async def import_chunk(name: str, jid: str, offset: int, request: Request, u: dict = IU):
    """Un morceau du fichier (au plus 64 Mo), à sa place : le suivant n'est accepté qu'une fois celui-ci écrit."""
    data = await request.body()
    if len(data) > CHUNK_MAX:
        raise HTTPException(413, "morceau trop gros")
    r = await wcall(name, "PUT", f"/imports/{jid}", params={"offset": offset, "owner": u["login"]}, content=data,
                    headers={"Content-Type": "application/octet-stream"}, timeout=httpx.Timeout(120, connect=5))
    return {**r.json(), "worker": name}


@app.post("/api/imports/{name}/{jid}/finish")
async def import_finish(name: str, jid: str, u: dict = IU):
    return {**(await wcall(name, "POST", f"/imports/{jid}/finish", params={"owner": u["login"]})).json(), "worker": name}


@app.post("/api/imports/{name}/{jid}/retry")
async def import_retry(name: str, jid: str, u: dict = IU):
    return {**(await wcall(name, "POST", f"/imports/{jid}/retry", params=_imp_owner(u))).json(), "worker": name}


@app.delete("/api/imports/{name}/{jid}")
async def import_cancel(name: str, jid: str, u: dict = IU):
    r = await wcall(name, "DELETE", f"/imports/{jid}", params=_imp_owner(u), timeout=30)
    _rec_cache.pop(name, None)
    return r.json()


@app.post("/api/recordings/{name}/{file}/transcribe")
async def recording_transcribe(name: str, file: str, u: dict = CU):
    """Transcription après coup (puis compte rendu) d'une vidéo restée sans transcription."""
    meta = await rec_meta(name, file, u, edit=True)
    owner = meta.get("owner") or u["login"]
    r = await wcall(name, "POST", f"/recordings/{file}/transcribe",
                    json={"owner": owner, "me_name": USERS.names().get(owner) or u.get("name")})
    _rec_cache.pop(name, None)
    log.info("transcription après coup de %s/%s demandée par %s", name, file, u["login"])
    return r.json()


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
CONSENT_TEXT_MANY = ("J'atteste sur l'honneur avoir reçu le consentement explicite de chacune des {n} personnes cochées "
                     "ci-dessus à l'utilisation de son empreinte vocale pour la reconnaître dans les réunions "
                     "enregistrées par OmniConf.")


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
    return {"items": sorted(items, key=lambda x: (not x.get("consent"), x["name"].lower())), "attestation": CONSENT_TEXT,
            "attestation_many": CONSENT_TEXT_MANY}


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


class ConsentMany(BaseModel):
    ids: list[str] = []            # personnes déjà dans la liste
    names: list[str] = []          # personnes à ajouter puis attester
    attest: bool = False
    note: str | None = None


@app.post("/api/voice-consents/attest")
async def consents_attest_many(body: ConsentMany, u: dict = Depends(require_admin)):
    """Plusieurs consentements attestés d'un coup (une attestation sur l'honneur pour toutes les personnes cochées)."""
    if not body.attest:
        raise HTTPException(400, "l'attestation sur l'honneur doit être cochée")
    items = load_consents()
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    ids = set(body.ids)
    for raw in body.names:
        name = " ".join(str(raw).split())[:80]
        if not name:
            continue
        if len(name) < 3:
            raise HTTPException(400, f"nom trop court : « {name} » (Prénom Nom)")
        x = next((i for i in items if i["name"].lower() == name.lower()), None)
        if not x:
            x = {"id": uuid.uuid4().hex[:8], "name": name, "consent": False, "at": None, "by": None, "note": None,
                 "history": [{"at": now, "by": u["login"], "action": "ajout"}]}
            items.append(x)
        ids.add(x["id"])
    targets = [x for x in items if x["id"] in ids and not x["consent"]]
    if not targets:
        raise HTTPException(400, "aucune personne à attester (déjà attestées, ou aucune cochée)")
    text = CONSENT_TEXT_MANY.format(n=len(targets)) if len(targets) > 1 else CONSENT_TEXT
    note = (body.note or "").strip()[:300] or None
    for x in targets:
        x.update(consent=True, at=now, by=u["login"])
        if note:
            x["note"] = note
        x["history"].append({"at": now, "by": u["login"], "action": "consentement attesté", "texte": text,
                             "lot": len(targets)})
    save_consents(items)
    names = [x["name"] for x in targets]
    log.info("consentements (empreintes vocales) attestés d'un coup par %s : %d personne(s)", u["login"], len(names))
    vp_schedule(names, u["login"], delay=5)          # empreintes calculées ensuite (tâche de fond)
    return {"attested": names}


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
# Les assistants cherchent sur leurs enregistrements les passages sûrs des personnes cochées, regroupés en « voix »
# (une par réunion et par origine : micro « Moi », voix nommée à la main, vignette), chacune avec quelques extraits.
# Un administrateur les écoute et valide (ou refuse) chaque voix : l'empreinte est la moyenne des seules voix
# validées, puis elle est diffusée aux assistants. Personnes cochées seulement ; décocher efface tout.
vp_state: dict = {"job": None}


def vp_load() -> dict:
    """{nom: {"groups": {voix: {worker, meeting, title, kind, label, sum, n, sec, dropped, clips, found_at,
    manual?, from?}}, "review": {voix: {ok, at, by, to?}}, "clip_review": {"<voix>@<début>": {gid, t, te, text, at,
    by, to?}}, "v", "sec", "n", "meetings", "kinds", "built_at"}} (empreinte : voix validées, extraits refusés retirés ;
    voix « manual » : attribuées à la main depuis la liste d'une autre personne)."""
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
    q = vq_load()                     # questions et réponses qui la concernent : effacées aussi
    items = {k: x for k, x in q["items"].items() if x.get("name") != name}
    answers = {k: a for k, a in q["answers"].items() if name not in (a.get("name"), a.get("to"))}
    if len(items) != len(q["items"]) or len(answers) != len(q["answers"]):
        vq_save({"items": items, "answers": answers})
    return gone


# « Est-ce X ? » : pendant la recherche des voix, chaque assistant signale les voix de ses réunions qui ressemblent à
# l'empreinte d'une personne cochée sans certitude (aucun vecteur : nom, ressemblance, positions de 3 extraits). Une
# réponse « oui » (ou « c'est Y ») nomme la voix dans la réunion et l'ajoute, validée, à l'empreinte de la personne.
def vq_load() -> dict:
    """{"items": {"<assistant>/<réunion>|<voix>": {worker, meeting, title, sid, label, name, sim, sure, alt?, alt_sim?,
    n, sec, clips, found_at}}, "answers": {même clé: {answer: yes|no|other|unknown, name, to?, at, by}}}."""
    try:
        d = json.loads(VOICE_Q_FILE.read_text())
    except (FileNotFoundError, ValueError):
        d = {}
    return {"items": d.get("items") or {}, "answers": d.get("answers") or {}}


def vq_save(d: dict):
    tmp = VOICE_Q_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False))
    os.chmod(tmp, 0o600)
    tmp.replace(VOICE_Q_FILE)


def vq_pending(d: dict, ok: set) -> list[dict]:
    """Questions encore à poser : personne cochée, pas de réponse (ou « non » à un autre nom que celui proposé)."""
    out = []
    for qid, q in d["items"].items():
        a = d["answers"].get(qid)
        if q.get("name") not in ok or (a and not (a["answer"] == "no" and a.get("name") != q["name"])):
            continue
        out.append({"id": qid, **{k: q.get(k) for k in ("worker", "meeting", "title", "sid", "label", "name", "sim",
                                                        "sure", "n", "sec", "clips")},
                    **({"alt": q["alt"], "alt_sim": q.get("alt_sim")} if q.get("alt") in ok else {})})
    out.sort(key=lambda q: (-(q["sim"] or 0), q["id"]))
    return out


def vq_store(results: dict[str, dict | None], ok: set):
    """Questions renvoyées par les assistants après une recherche (assistant occupé : ses questions sont gardées)."""
    d = vq_load()
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    for w, res in results.items():
        if res is None:
            continue
        old = {k: x for k, x in d["items"].items() if x.get("worker") == w}
        for k in old:
            d["items"].pop(k)
        for stem, item in (res or {}).items():
            for q in (item or {}).get("questions") or []:
                if not isinstance(q, dict) or q.get("name") not in ok or not q.get("sid"):
                    continue
                qid = f"{w}/{stem}|{q['sid']}"
                d["items"][qid] = {**{k: q.get(k) for k in ("sid", "label", "name", "sim", "sure", "n", "sec", "clips")},
                                   **({"alt": q["alt"], "alt_sim": q.get("alt_sim")} if q.get("alt") else {}),
                                   "worker": w, "meeting": stem, "title": item.get("title"),
                                   "found_at": (old.get(qid) or {}).get("found_at") or now}
    if any(r is not None for r in results.values()):
        vq_save(d)


def vp_migrate() -> bool:
    """Empreintes calculées avant la validation à l'écoute : retirées (elles seront refaites à partir des seules
    voix validées). True si quelque chose a changé."""
    d = vp_load()
    old = [n for n, e in d.items() if "groups" not in e]
    for n in old:
        d[n] = {"groups": {}, "review": {}}
    if old:
        vp_save(d)
        log.info("empreintes vocales sans validation retirées (%s) : voix à écouter et valider", ", ".join(old))
    return bool(old)


def vp_compute(e: dict) -> dict:
    """Empreinte d'une personne : moyenne des voix validées à l'écoute (aucune voix validée : pas d'empreinte)."""
    groups, review, cr = e.get("groups") or {}, e.get("review") or {}, e.get("clip_review") or {}
    ok = []
    for gid, g in groups.items():
        if (review.get(gid) or {}).get("ok") is not True or not g.get("sum"):
            continue
        sm, n, sec = list(g["sum"]), g["n"], g["sec"]
        for c in g.get("clips") or []:          # extrait refusé à l'écoute : retiré tout de suite
            if f"{gid}@{c['t']}" in cr and c.get("v") and not g.get("manual"):
                sm, n, sec = [a - b for a, b in zip(sm, c["v"])], n - 1, sec - float(c.get("d") or c["te"] - c["t"])
        if n > 0:
            ok.append({**g, "sum": sm, "n": n, "sec": max(0.0, sec)})
    old_v, old_at = e.get("v"), e.get("built_at")
    for k in ("v", "sec", "n", "meetings", "kinds", "built_at"):
        e.pop(k, None)
    if not ok:
        return e
    tot = [sum(x) for x in zip(*(g["sum"] for g in ok))]
    norm = sum(x * x for x in tot) ** 0.5 or 1.0
    v = [round(x / norm, 6) for x in tot]
    kinds: dict[str, int] = {}
    for g in ok:
        kinds[g["kind"]] = kinds.get(g["kind"], 0) + g["n"]
    e.update(v=v, n=sum(g["n"] for g in ok), sec=round(sum(g["sec"] for g in ok), 1),
             meetings=len({g["meeting"] for g in ok}), kinds=kinds,
             built_at=old_at if v == old_v and old_at else dt.datetime.now().astimezone().isoformat(timespec="seconds"))
    return e


def vp_summary(e: dict | None) -> dict:
    """Ce que l'IHM affiche d'une personne (jamais les vecteurs)."""
    e = e or {}
    rv = e.get("review") or {}
    st = [(rv.get(gid) or {}).get("ok") for gid in (e.get("groups") or {})]
    return {**{k: e.get(k) for k in ("sec", "n", "meetings", "kinds", "built_at")},
            "voices": len(st), "pending": st.count(None), "validated": st.count(True), "rejected": st.count(False)}


def vp_payload() -> dict:
    """Empreintes envoyées aux assistants : personnes cochées au moment de l'envoi, voix validées, rien d'autre."""
    ok = set(vp_consented())
    return {n: {k: p.get(k) for k in ("v", "sec", "n", "meetings", "built_at")} for n, p in vp_load().items()
            if n in ok and p.get("v")}


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


def _vp_people(e: dict) -> str:
    s = vp_summary(e)
    txt = f"{s['voices']} voix trouvée(s), {s['pending']} à écouter"
    return txt + (f" ; empreinte : {round((s['sec'] or 0) / 60, 1)} min de voix validée" if e.get("v") else " ; aucune voix validée")


async def voiceprints_build(names: list[str], by: str):
    """Demande à chaque assistant libre les voix candidates, les enregistre (validations conservées), recalcule
    l'empreinte à partir des voix validées et la diffuse."""
    job = vp_state["job"]
    owners = USERS.names()
    d0 = vp_load()
    refs = {n: d0[n]["v"] for n in names if (d0.get(n) or {}).get("v")}

    async def one(n):
        exclude = {name: [gid.split("/", 1)[1] for gid, r in ((d0.get(name) or {}).get("review") or {}).items()
                          if r.get("ok") is False and gid.startswith(n + "/")] for name in names}
        negatives = {name: [[c["gid"].split("/", 1)[1].rsplit("|", 1)[0], c["t"]]
                            for c in ((d0.get(name) or {}).get("clip_review") or {}).values()
                            if c.get("gid", "").startswith(n + "/")] for name in names}
        try:
            r = await http.post(wurl(n) + "/voiceprints/extract", timeout=15,
                                json={"names": names, "owners": owners, "exclude": exclude, "refs": refs,
                                      "negatives": negatives, "questions": True})
            if r.status_code >= 400:
                job["workers"][n] = "occupé (réunion en cours)" if r.status_code == 409 else f"erreur {r.status_code}"
                return n, None, None
            job["workers"][n] = "recherche en cours"
            for _ in range(720):                     # 1 h au plus
                await asyncio.sleep(5)
                g = (await http.get(wurl(n) + "/voiceprints/extract", timeout=15)).json()
                if g.get("state") == "done":
                    job["workers"][n] = "terminé"
                    return n, g.get("result") or {}, g.get("questions") if isinstance(g.get("questions"), dict) else None
                if g.get("state") in ("error", "fetched", None):
                    job["workers"][n] = f"erreur : {g.get('error') or g.get('state')}"
                    return n, None, None
            job["workers"][n] = "trop long"
        except Exception as e:
            job["workers"][n] = f"injoignable ({str(e)[:60]})"
        return n, None, None
    got = await asyncio.gather(*(one(n) for n in WORKERS))
    results = {n: res for n, res, _ in got}
    ok = set(vp_consented())          # consentement retiré pendant le calcul : rien n'est gardé
    vq_store({n: q for n, _, q in got}, ok)
    job["questions"] = len(vq_pending(vq_load(), ok))
    d = vp_load()                     # validations faites pendant la recherche : conservées
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    for name in names:
        if name not in ok:
            d.pop(name, None)
            continue
        e = d.setdefault(name, {})
        e.setdefault("groups", {})
        e.setdefault("review", {})
        for w, res in results.items():
            if res is None:           # assistant occupé ou injoignable : ses voix déjà connues sont gardées
                continue
            e["groups"] = {gid: g for gid, g in e["groups"].items() if g.get("manual") or not gid.startswith(w + "/")}
            for gid, g in ((res or {}).get(name) or {}).items():
                if isinstance(g, dict) and g.get("sum") and "clips" in g:
                    e["groups"][f"{w}/{gid}"] = {**g, "worker": w, "found_at": now}
        vp_compute(e)
        job["people"][name] = _vp_people(e)
    vp_save(d)
    job["push"] = await voiceprints_push()
    job.update(state="done", ended=now)
    log.info("empreintes vocales (voix candidates) : %s", job["people"])


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


def vp_schedule(names: list[str], by: str, delay: float = 60):
    """Nouvelle recherche des voix peu après des extraits refusés ou rétablis (plusieurs clics regroupés) : les
    passages qui ressemblent à un extrait refusé sont retirés, de nouveaux extraits proposés."""
    vp_state.setdefault("pending", set()).update(names)
    t = vp_state.get("timer")
    if t and not t.done():
        t.cancel()

    async def later():
        await asyncio.sleep(delay)
        while vp_state["job"] and vp_state["job"].get("state") == "running":
            await asyncio.sleep(10)
        vp_start_build(sorted(vp_state.pop("pending", set())), by)
    vp_state["timer"] = asyncio.create_task(later())


def vp_need(name: str):
    if name not in set(vp_consented()):
        raise HTTPException(404, "personne non cochée : pas d'empreinte vocale")


@app.get("/api/voiceprints")
async def voiceprints_status(u: dict = Depends(require_admin)):
    """Empreintes des personnes cochées (durées, réunions, voix à écouter ; jamais les vecteurs) et diffusion."""
    d, ok = vp_load(), vp_consented()
    st = await asyncio.gather(*(worker_status(n) for n in WORKERS))
    want = vp_version(vp_payload())
    return {"people": {n: vp_summary(d.get(n)) for n in ok}, "scheduled": sorted(vp_state.get("pending") or []),
            "workers": {x["name"]: ("injoignable" if not x.get("online") else "à jour" if x.get("voiceprints") == want
                                    else "en attente") for x in st},
            "job": vp_state["job"]}


@app.post("/api/voiceprints/build")
async def voiceprints_rebuild(u: dict = Depends(require_admin)):
    return vp_start_build(vp_consented(), u["login"])


@app.get("/api/voiceprints/voices")
async def voiceprints_voices(name: str, u: dict = Depends(require_admin)):
    """Voix candidates d'une personne cochée, avec leurs extraits à écouter et la décision prise."""
    vp_need(name)
    e = vp_load().get(name) or {}
    rv, cr, names = e.get("review") or {}, e.get("clip_review") or {}, USERS.names()
    out = []
    for gid, g in (e.get("groups") or {}).items():
        r = rv.get(gid) or {}
        clips, seen = [], set()
        for c in g.get("clips") or []:
            k = f"{gid}@{c['t']}"
            seen.add(k)
            clips.append({**{x: c.get(x) for x in ("t", "te", "text", "sim", "odd")}, "refused": k in cr,
                          "to": (cr.get(k) or {}).get("to"), "attributable": bool(c.get("v"))})
        for k, c in cr.items():                  # refusé puis retiré de la voix par la recherche suivante
            if c.get("gid") == gid and k not in seen:
                clips.append({"t": c["t"], "te": c["te"], "text": c.get("text"), "refused": True, "gone": True, "to": c.get("to")})
        out.append({"id": gid, "worker": g.get("worker"), "meeting": g.get("meeting"), "title": g.get("title"),
                    "kind": g.get("kind"), "label": g.get("label"), "n": g.get("n"), "sec": g.get("sec"),
                    "dropped": g.get("dropped") or 0, "dropped_neg": g.get("dropped_neg") or 0, "clips": clips,
                    "manual": bool(g.get("manual")), "from": g.get("from"), "ok": r.get("ok"), "to": r.get("to"),
                    "at": r.get("at"), "by": names.get(r.get("by") or "", r.get("by"))})
    out.sort(key=lambda x: (x["meeting"] or "", x["id"]), reverse=True)   # ordre fixe : rien ne bouge sous le doigt
    return {"name": name, "summary": vp_summary(e), "voices": out}


class VoiceReview(BaseModel):
    name: str
    id: str
    ok: bool | None = None        # True : c'est bien cette personne ; False : ce n'est pas elle ; None : à réécouter


@app.post("/api/voiceprints/review")
async def voiceprints_review(body: VoiceReview, u: dict = Depends(require_admin)):
    """Décision prise à l'écoute d'une voix : l'empreinte est recalculée tout de suite et diffusée."""
    vp_need(body.name)
    d = vp_load()
    e = d.get(body.name) or {}
    if body.id not in (e.get("groups") or {}):
        raise HTTPException(404, "voix inconnue (recherche refaite entre-temps ?)")
    rv = e.setdefault("review", {})
    if body.ok is False and e["groups"][body.id].get("manual"):
        e["groups"].pop(body.id)
        rv.pop(body.id, None)
    elif body.ok is None:
        rv.pop(body.id, None)
    else:
        rv[body.id] = {"ok": body.ok, "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "by": u["login"]}
    vp_compute(e)
    vp_save(d)
    log.info("empreinte vocale de %s : voix %s %s par %s", body.name, body.id,
             {True: "validée", False: "refusée", None: "remise à écouter"}[body.ok], u["login"])
    return {"summary": vp_summary(e), "workers": await voiceprints_push()}


def _vp_clip(e: dict, gid: str, t: float) -> dict | None:
    for c in ((e.get("groups") or {}).get(gid) or {}).get("clips") or []:
        if abs(float(c["t"]) - t) < 0.01:
            return c
    c = (e.get("clip_review") or {}).get(f"{gid}@{round(t, 2)}")
    return c if c and c.get("gid") == gid else None


@app.get("/api/voiceprints/clip")
async def voiceprints_clip(name: str, id: str, t: float, u: dict = Depends(require_admin)):
    """Extrait à écouter d'une voix candidate (MP3, lu à la demande sur l'assistant, jamais conservé ici)."""
    vp_need(name)
    e = vp_load().get(name) or {}
    g = (e.get("groups") or {}).get(id) or {}
    c = _vp_clip(e, id, t)
    w, meeting = g.get("worker") or id.split("/", 1)[0], g.get("meeting") or id.split("/", 1)[-1].rsplit("|", 1)[0]
    if not c or w not in WORKERS:
        raise HTTPException(404, "extrait inconnu")
    r = await wcall(w, "GET", f"/recordings/{quote(meeting + '.mp4')}/audio", timeout=30,
                    params={"start": max(0.0, float(c["t"]) - 0.3), "end": float(c["te"]) + 0.3, "fmt": "mp3"})
    return Response(r.content, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=3600"})


class ClipReview(BaseModel):
    name: str
    id: str
    t: float
    ok: bool | None = None        # False : ce passage n'est pas cette personne ; None : rétabli


@app.post("/api/voiceprints/clip-review")
async def voiceprints_clip_review(body: ClipReview, u: dict = Depends(require_admin)):
    """Extrait refusé (ou rétabli) à l'écoute : retiré de l'empreinte tout de suite ; une minute après, une nouvelle
    recherche retire de la voix les passages qui lui ressemblent et propose d'autres extraits."""
    vp_need(body.name)
    d = vp_load()
    e = d.get(body.name) or {}
    c = _vp_clip(e, body.id, body.t)
    if not c:
        raise HTTPException(404, "extrait inconnu (recherche refaite entre-temps ?)")
    cr = e.setdefault("clip_review", {})
    k = f"{body.id}@{c['t']}"
    if body.ok is False:
        cr[k] = {"gid": body.id, "t": c["t"], "te": c["te"], "text": (c.get("text") or "")[:200],
                 "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "by": u["login"]}
    else:
        cr.pop(k, None)
    vp_compute(e)
    vp_save(d)
    log.info("empreinte vocale de %s : extrait %s %s par %s", body.name, k, "refusé" if body.ok is False else "rétabli", u["login"])
    if not (e.get("groups") or {}).get(body.id, {}).get("manual"):
        vp_schedule([body.name], u["login"])
    return {"summary": vp_summary(e), "workers": await voiceprints_push()}


class VoiceAttribute(BaseModel):
    name: str                     # personne sous laquelle la voix a été trouvée
    id: str
    t: float | None = None        # un seul extrait ; absent : toute la voix
    to: str                       # personne de la liste (cochée) à qui elle appartient en réalité


@app.post("/api/voiceprints/attribute")
async def voiceprints_attribute(body: VoiceAttribute, u: dict = Depends(require_admin)):
    """Voix (ou extrait) reconnue à l'écoute comme celle d'une autre personne de la liste : refusée pour la première,
    ajoutée (validée) à l'empreinte de l'autre. Seulement vers une personne dont le consentement est attesté."""
    vp_need(body.name)
    if body.to == body.name:
        raise HTTPException(400, "même personne")
    if body.to not in set(vp_consented()):
        raise HTTPException(400, f"{body.to} : consentement non attesté, aucune voix ne peut lui être attribuée")
    d = vp_load()
    src = d.get(body.name) or {}
    g = (src.get("groups") or {}).get(body.id)
    if not g or g.get("manual"):
        raise HTTPException(404, "voix inconnue (recherche refaite entre-temps ?)")
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    cr = src.setdefault("clip_review", {})
    if body.t is not None:
        c = _vp_clip(src, body.id, body.t)
        if not c or not c.get("v"):
            raise HTTPException(409, "extrait sans empreinte : relancez « Chercher les voix », puis recommencez")
        tid = f"{body.id}@{c['t']}"
        new = {**{k: g.get(k) for k in ("worker", "meeting", "title", "label")}, "kind": g.get("kind"), "manual": True,
               "from": body.name, "sum": list(c["v"]), "n": 1, "sec": float(c.get("d") or c["te"] - c["t"]), "dropped": 0,
               "clips": [c], "found_at": now}
        cr[tid] = {"gid": body.id, "t": c["t"], "te": c["te"], "text": (c.get("text") or "")[:200], "at": now,
                   "by": u["login"], "to": body.to}
    else:
        tid = f"{body.id}@voix"
        sm, n, sec, clips = list(g["sum"]), g["n"], g["sec"], []
        for c in g.get("clips") or []:          # extraits déjà refusés : pas transmis
            if f"{body.id}@{c['t']}" in cr:
                if c.get("v"):
                    sm, n, sec = [a - b for a, b in zip(sm, c["v"])], n - 1, sec - float(c.get("d") or c["te"] - c["t"])
            else:
                clips.append(c)
        if n <= 0:
            raise HTTPException(400, "plus aucun passage dans cette voix")
        new = {**{k: g.get(k) for k in ("worker", "meeting", "title", "label", "kind")}, "manual": True, "from": body.name,
               "sum": sm, "n": n, "sec": round(max(0.0, sec), 1), "dropped": 0, "clips": clips, "found_at": now}
        src.setdefault("review", {})[body.id] = {"ok": False, "at": now, "by": u["login"], "to": body.to}
    dst = d.setdefault(body.to, {})
    dst.setdefault("groups", {})[tid] = new
    dst.setdefault("review", {})[tid] = {"ok": True, "at": now, "by": u["login"]}
    vp_compute(src)
    vp_compute(dst)
    vp_save(d)
    log.info("empreinte vocale : %s (%s) attribué à %s par %s", tid, body.name, body.to, u["login"])
    if body.t is not None:
        vp_schedule([body.name], u["login"])
    return {"summary": vp_summary(src), "to": vp_summary(dst), "workers": await voiceprints_push()}


# ----------------------------------------------------------------------------- « Est-ce X ? »
@app.get("/api/voice-questions")
async def voice_questions(worker: str | None = None, meeting: str | None = None, u: dict = Depends(require_admin)):
    """Voix douteuses à confirmer (toutes, ou celles d'une réunion), les plus ressemblantes d'abord."""
    d, ok = vq_load(), set(vp_consented())
    qs = [q for q in vq_pending(d, ok) if (not worker or q["worker"] == worker) and (not meeting or q["meeting"] == meeting)]
    return {"questions": qs, "people": sorted(ok), "answered": len(d["answers"]),
            "searching": (vp_state["job"] or {}).get("state") == "running"}


@app.get("/api/voice-questions/clip")
async def voice_question_clip(id: str, t: float, u: dict = Depends(require_admin)):
    """Extrait d'une voix douteuse (MP3 lu à la demande sur l'assistant, jamais conservé ici)."""
    q = vq_load()["items"].get(id) or {}
    c = next((c for c in q.get("clips") or [] if abs(float(c["t"]) - t) < 0.01), None)
    if not c or q.get("worker") not in WORKERS or q.get("name") not in set(vp_consented()):
        raise HTTPException(404, "extrait inconnu")
    r = await wcall(q["worker"], "GET", f"/recordings/{quote(q['meeting'] + '.mp4')}/audio", timeout=30,
                    params={"start": max(0.0, float(c["t"]) - 0.3), "end": float(c["te"]) + 0.3, "fmt": "mp3"})
    return Response(r.content, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=3600"})


class VoiceAnswer(BaseModel):
    id: str
    answer: str | None            # yes : c'est bien la personne proposée ; other : c'est `to` ; no ; unknown ; None : annuler
    to: str | None = None
    exclude: list[float] = []     # passages écoutés qui ne sont pas cette personne (début, en s) : hors de son empreinte


async def _vq_name(q: dict, name: str | None, via: str | None):
    """Nomme (ou dénomme) la voix dans la transcription de la réunion, sur l'assistant."""
    await wcall(q["worker"], "PATCH", f"/recordings/{quote(q['meeting'] + '.mp4')}/speakers", timeout=15,
                json={"id": q["sid"], "name": name, "via": via})
    _rec_cache.pop(q["worker"], None)


@app.post("/api/voice-questions/answer")
async def voice_question_answer(body: VoiceAnswer, u: dict = Depends(require_admin)):
    """Réponse à « Est-ce X ? ». Oui (ou « c'est Y ») : la voix prend ce nom dans la réunion (compte rendu compris) et
    compte, validée, dans l'empreinte de la personne (recalculée une minute après). Non / je ne sais pas : la question
    n'est plus posée. Sans réponse : la précédente est annulée (nom retiré, voix retirée de l'empreinte)."""
    if body.answer not in ("yes", "no", "other", "unknown", None):
        raise HTTPException(400, "réponse inconnue")
    d, ok = vq_load(), set(vp_consented())
    q = d["items"].get(body.id)
    if not q:
        raise HTTPException(404, "question inconnue (recherche refaite entre-temps ?)")
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    gid = f"{q['worker']}/{q['meeting']}|confirmée"
    if body.answer is None:
        prev = d["answers"].get(body.id)
        if not prev:
            return {"ok": True}
        if prev["answer"] in ("yes", "other"):
            who = prev.get("to") or prev["name"]
            await _vq_name(q, None, None)
            d = vq_load()
            same = [k for k, a in d["answers"].items() if k != body.id and k.startswith(f"{q['worker']}/{q['meeting']}|")
                    and a["answer"] in ("yes", "other") and (a.get("to") or a["name"]) == who]
            vd = vp_load()
            rv = (vd.get(who) or {}).get("review") or {}
            cr = (vd.get(who) or {}).get("clip_review") or {}
            changed = bool([cr.pop(k) for k in prev.get("excluded") or [] if k in cr])   # passages retirés : rétablis
            if not same and (rv.get(gid) or {}).get("via") == "question":
                rv.pop(gid)
                changed = True
            if changed:
                vp_compute(vd[who])
                vp_save(vd)
                await voiceprints_push()
            if who in ok:
                vp_schedule([who], u["login"])
        d["answers"].pop(body.id, None)
        vq_save(d)
        log.info("question %s : réponse annulée par %s", body.id, u["login"])
        return {"ok": True}
    who = q["name"] if body.answer == "yes" else body.to if body.answer == "other" else None
    clips = [c for c in q.get("clips") or [] if any(abs(float(c["t"]) - float(t)) < 0.01 for t in body.exclude)]
    excluded = []
    if body.answer in ("yes", "other"):
        if who not in ok:
            raise HTTPException(400, f"{who or '?'} : consentement non attesté, cette voix ne peut pas lui être attribuée")
        if clips and len(clips) >= len(q.get("clips") or []):
            raise HTTPException(400, "tous les passages écoutés sont retirés : répondez plutôt « Non »")
        await _vq_name(q, who, "question")
        vd = vp_load()
        e = vd.setdefault(who, {})
        e.setdefault("groups", {})
        e.setdefault("review", {})[gid] = {"ok": True, "at": now, "by": u["login"], "via": "question"}
        cr = e.setdefault("clip_review", {})
        for c in clips:          # ce passage, et ceux qui lui ressemblent, restent hors de l'empreinte
            k = f"{gid}@{c['t']}"
            cr[k] = {"gid": gid, "t": c["t"], "te": c["te"], "text": (c.get("text") or "")[:200], "at": now,
                     "by": u["login"], "via": "question"}
            excluded.append(k)
        vp_compute(e)
        vp_save(vd)
        vp_schedule([who], u["login"])          # la voix confirmée rejoint l'empreinte à la recherche suivante
        d = vq_load()
    d["answers"][body.id] = {"answer": body.answer, "name": q["name"], **({"to": who} if body.answer == "other" else {}),
                             **({"excluded": excluded} if excluded else {}),
                             "label": q.get("label"), "at": now, "by": u["login"]}
    vq_save(d)
    log.info("question %s (%s ?) : %s%s%s par %s", body.id, q["name"], body.answer, f" ({who})" if body.answer == "other" else "",
             f", {len(excluded)} passage(s) retiré(s)" if excluded else "", u["login"])
    return {"ok": True, "named": who, "left": len(vq_pending(d, ok))}


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
            await sessions_collect()
        except Exception as e:  # pragma: no cover
            log.warning("sessions Zoom : %s", e)
        try:
            await voiceprints_push()
            now = dt.datetime.now().astimezone()
            last = (vp_state.get("job") or {}).get("started") or ""
            if now.hour == 3 and not last.startswith(now.date().isoformat()) and vp_consented():
                vp_start_build(vp_consented(), "nuit")
            # compte rendu d'une nouvelle réunion terminé : voix cherchées (et questions posées) quelques minutes après
            reps = {f"{n}/{_stem(x['name'])}" for n in WORKERS for x in await worker_recordings(n) if x.get("report")}
            seen = vp_state.get("reports")
            if seen is not None and reps - seen and vp_consented():
                vp_schedule(vp_consented(), "nouvelle réunion", delay=120)
            vp_state["reports"] = (seen or set()) | reps
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
    if vp_migrate() or (not VOICE_Q_FILE.exists() and vp_consented()):
        vp_start_build(vp_consented(), "mise à jour")   # voix candidates (et questions) recherchées tout de suite


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
