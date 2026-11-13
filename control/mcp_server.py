"""Serveur MCP (Model Context Protocol) : accès de Claude aux réunions enregistrées, en lecture seule.

Transport « Streamable HTTP » sans état : POST /mcp (JSON-RPC 2.0, réponse JSON), GET -> 405.
Versions du protocole : 2024-11-05 à 2025-11-25 (poignée de main « initialize ») et 2026-07-28 (sans
poignée de main, « server/discover »).

Authentification : en-tête « Authorization: Bearer <jeton> ». Les jetons sont propres à un client
(ex. Claude), révocables, et ont une portée :
  - read  : outils de lecture (list_meetings, get_meeting, get_transcript, get_draft_cr, get_slides, get_audio_url)
  - write : dépôt du compte rendu final (attach_cr), et rien d'autre (jeton distinct, à n'utiliser qu'à la demande)
Seule l'empreinte SHA-256 des jetons est conservée (DATA_DIR/mcp_tokens.json). Gestion : IHM
(Enregistrements > Accès de Claude) ou en ligne de commande (voir __main__).

Les liens audio et images renvoyés sont signés (HMAC), temporaires, et ne demandent pas le jeton.
"""
import asyncio
import base64
import contextvars
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path
from zoneinfo import ZoneInfo

import users as _users

log = logging.getLogger("mcp")
VERSION = "1.0.0"
SUPPORTED = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
LEGACY_LATEST = "2025-11-25"
TZ = ZoneInfo(os.environ.get("DISPLAY_TZ", "Europe/Paris"))
# Compte OmniConf du jeton qui appelle l'outil : ne voit que ses réunions (administrateur : toutes)
CALLER: contextvars.ContextVar = contextvars.ContextVar("caller", default=None)
PAGE_CHARS = int(os.environ.get("MCP_PAGE_CHARS", "40000"))   # Claude Code : résultats > 50 000 caractères mis en fichier
LINK_TTL = 900            # durée par défaut des liens temporaires (s)
LINK_TTL_MAX = 4 * 3600
GAP_OFF = 180             # silence (s) qui sépare une séquence hors réunion de la réunion
STEM_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")

INSTRUCTIONS = (
    "Réunions (Teams, Zoom, autres webconfs) enregistrées et transcrites par OmniConf. "
    "list_meetings pour trouver une réunion (id « w1:20261110-160042_teams »), get_meeting pour ses métadonnées "
    "(participants, intervenants, avertissements), get_transcript pour la transcription (segments horodatés, paginée), "
    "get_draft_cr pour le premier compte rendu produit par le service, get_slides pour l'écran partagé, "
    "get_audio_url pour réécouter ou retranscrire un passage douteux, get_lexicon pour le lexique du projet (noms, "
    "sigles, corrections de transcription, registre des sujets). Heures en Europe/Paris ; dans une réunion, "
    "les horodatages HH:MM:SS partent du début de l'enregistrement.")


# ============================================================================ jetons
class TokenStore:
    def __init__(self, path: Path):
        self.path = path
        self._used: dict[str, float] = {}

    def load(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return []

    def save(self, items: list[dict]):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1))
        os.chmod(tmp, 0o600)
        if os.geteuid() == 0:   # ligne de commande lancée en root : le fichier reste au compte du service
            st = self.path.parent.stat()
            os.chown(tmp, st.st_uid, st.st_gid)
        tmp.replace(self.path)

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def create(self, name: str, scope: str, user: str | None = None) -> tuple[str, dict]:
        if scope not in ("read", "write"):
            raise ValueError("portée : read ou write")
        token = ("wcr_" if scope == "read" else "wcw_") + secrets.token_urlsafe(32)
        entry = {"id": secrets.token_hex(4), "name": (name or "claude").strip()[:60], "scope": scope,
                 "hash": self._hash(token), "created": _now_iso(), "last_used": None, "revoked": None,
                 "user": user}
        items = self.load()
        items.append(entry)
        self.save(items)
        return token, entry

    def check(self, token: str) -> dict | None:
        if not token:
            return None
        h = self._hash(token)
        items = self.load()
        for e in items:
            if not e.get("revoked") and hmac.compare_digest(e["hash"], h):
                if time.time() - self._used.get(e["id"], 0) > 300:   # date de dernière utilisation (au plus toutes les 5 min)
                    self._used[e["id"]] = time.time()
                    e["last_used"] = _now_iso()
                    self.save(items)
                return e
        return None

    def revoke(self, tid: str) -> bool:
        items = self.load()
        for e in items:
            if e["id"] == tid and not e.get("revoked"):
                e["revoked"] = _now_iso()
                self.save(items)
                return True
        return False

    def public(self) -> list[dict]:
        return [{k: v for k, v in e.items() if k != "hash"} for e in self.load()]


# ============================================================================ outils communs
def _now_iso() -> str:
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hms(sec) -> str | None:
    if sec is None:
        return None
    sec = max(0, int(round(float(sec))))
    return f"{sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}"


def parse_hms(v) -> float | None:
    """« 01:02:03 », « 62:03 », « 3723 » ou 3723 -> secondes."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        parts = [float(x) for x in str(v).strip().split(":")]
    except ValueError:
        raise ValueError(f"horodatage invalide : {v!r} (attendu HH:MM:SS)")
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def local_iso(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s).astimezone(TZ).isoformat(timespec="seconds")
    except ValueError:
        return s


def parse_when(v: str | None, end: bool = False) -> dt.datetime | None:
    """Date (AAAA-MM-JJ) ou date-heure ISO ; sans fuseau = heure de Paris. Une date seule en borne haute = fin du jour."""
    if not v:
        return None
    s = str(v).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            d = dt.datetime.fromisoformat(s).replace(tzinfo=TZ)
            return d + dt.timedelta(days=1) if end else d
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=TZ)
    except ValueError:
        raise ValueError(f"date invalide : {v!r} (attendu AAAA-MM-JJ ou AAAA-MM-JJTHH:MM)")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", s))


NAME_SUFFIX = re.compile(r"\s*\((guest|invité|invitée|unverified|non vérifié|external|externe|host|hôte|co-host|"
                         r"co-hôte|me|moi|you|vous|organizer|organisateur)\)\s*$", re.I)
ORG_PAREN = re.compile(r"^(?P<name>.+?)\s*[(\[](?P<org>[^()\[\]]{2,60})[)\]]\s*$")
ORG_DASH = re.compile(r"^(?P<name>[^-–|]{3,60}?)\s+[-–|]\s+(?P<org>.{2,60})$")
DUP = re.compile(r"\s*\(\d+\)$")


def split_org(n: str) -> tuple[str, str | None]:
    n = (n or "").strip()
    while NAME_SUFFIX.search(n):
        n = NAME_SUFFIX.sub("", n).strip()
    for rx in (ORG_PAREN, ORG_DASH):
        m = rx.match(n)
        if m:
            return m.group("name").strip(), m.group("org").strip()
    return n, None


# Hallucinations de Whisper (mêmes règles que l'assistant ; appliquées à la lecture des anciennes transcriptions)
HALLU = re.compile(r"sous-titr|amara\.org|merci d'avoir regardé|abonnez-vous|thanks for watching|\bMFP\b|"
                   r"^\W*(thank you|you)\W*$", re.I)
# Boucles : 2 à 4 mots répétés au moins 4 fois de suite (« je pense que je pense que… »), ou un même mot
# 8 fois (« non, non, non, non » se dit vraiment : réglé sur de vraies réunions)
LOOP_PHRASE = re.compile(r"\b(\w+(?:\W+\w+){1,3}?)(?:\W+\1\b){3,}", re.I)
LOOP_WORD = re.compile(r"\b(\w+)\b(?:\W+\1\b){7,}", re.I)
MERCI = re.compile(r"^\W*(merci(\s+(beaucoup|à tous|à toutes))?\W*){2,}$", re.I)


def suspect_reason(text: str, dur: float) -> str | None:
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
    if MERCI.match(t) or (re.fullmatch(r"\W*merci\W*", t, re.I) and dur > 8):
        return "« Merci » en série"
    return None


# ============================================================================ lecture d'une transcription
class Meeting:
    """Vue « MCP » d'une réunion : transcription (doc), métadonnées d'enregistrement, compte rendu."""

    def __init__(self, mid: str, rec: dict | None, doc: dict | None, live: bool = False):
        self.id, self.rec, self.doc, self.live = mid, rec or {}, doc or {}, live
        self.meta = {**(self.rec.get("meta") or {}), **(self.doc.get("meta") or {})}
        self.platform = self.meta.get("platform") or "web"
        self.spk = self.doc.get("speakers") or {}
        self.me = self.meta.get("me_name") or "Moi"
        self._segments = None

    # ------------------------------------------------------------------ intervenants
    def _source_platform(self) -> str:
        return self.platform if self.platform in ("teams", "zoom") else (self.meta.get("site") or "plateforme")

    def resolve(self, sid: str | None, ui: str | None) -> tuple[str, str]:
        """(nom, source) d'une phrase : renommage manuel > nom affiché par la plateforme pendant la phrase >
        nom reconnu à la voix (diarisation, nom voté) > « Intervenant N »."""
        if sid == "ME":
            return self.me, "micro_ihm"
        v = self.spk.get(sid) or {}
        if v.get("name"):
            return v["name"], "manuel"
        if ui:
            return ui, self._source_platform()
        if v.get("source") == "ui":
            return DUP.sub("", v.get("auto") or v.get("label") or sid), "diarisation"
        return v.get("label") or sid or "?", "diarisation"

    # ------------------------------------------------------------------ segments
    def segments(self) -> list[dict]:
        if self._segments is not None:
            return self._segments
        out = []
        for e in sorted(self.doc.get("raw") or [], key=lambda e: e.get("t", 0)):
            text = (e.get("text") or "").strip()
            if not text:
                continue
            t0, t1 = float(e.get("t") or 0), float(e.get("te") or e.get("t") or 0)
            name, src = self.resolve(e.get("spk"), e.get("ui"))
            name = split_org(name)[0] if src not in ("manuel", "micro_ihm") else name   # organisation : voir get_meeting
            s = {"start": hms(t0), "end": hms(t1), "speaker": name, "speaker_source": src, "text": text,
                 "confidence": e.get("conf"), "_t": t0, "_te": t1, "_spk": e.get("spk")}
            # transcriptions récentes : l'assistant a déjà jugé (et retranscrit en tronçons courts) ; anciennes : règles ici
            why = e.get("suspect") if "asr_stats" in self.doc else suspect_reason(text, t1 - t0)
            if why:
                s["suspect"] = True
                s["suspect_reason"] = why
            if e.get("retranscrit"):
                s["retranscribed"] = True
            out.append(s)
        win = self.window(out)
        if win:
            for s in out:
                if s["_te"] < win[0] or s["_t"] > win[1]:
                    s["off_meeting"] = True
        self._segments = out
        return out

    @staticmethod
    def window(segs: list[dict]) -> tuple[float, float] | None:
        """Réunion effective : premier et dernier bloc de parole (blocs séparés par > 3 min de silence)
        comptant au moins 2 voix et 60 s de parole. Avant / après : séquences hors réunion (essais de micro,
        attente, conversation après la fin)."""
        blocks = []
        for s in segs:
            if blocks and s["_t"] - blocks[-1]["end"] <= GAP_OFF:
                b = blocks[-1]
            else:
                b = {"start": s["_t"], "end": s["_te"], "secs": 0.0, "who": set()}
                blocks.append(b)
            b["end"] = max(b["end"], s["_te"])
            b["secs"] += max(0.0, s["_te"] - s["_t"])
            b["who"].add(s["speaker"])
        good = [b for b in blocks if b["secs"] >= 60 and len(b["who"]) >= 2]
        if not good or (good[0] is blocks[0] and good[-1] is blocks[-1]):
            return None
        return good[0]["start"], good[-1]["end"]

    def paragraphs(self) -> list[dict]:
        """Paragraphes mis en forme (ponctuation, hésitations retirées) avec début / fin et locuteur résolu."""
        paras = sorted(self.doc.get("paragraphs") or [], key=lambda p: p.get("t", 0))
        segs = self.segments()
        out = []
        for i, p in enumerate(paras):
            t0 = float(p.get("t") or 0)
            nxt = float(paras[i + 1]["t"]) if i + 1 < len(paras) else float("inf")
            mine = [s for s in segs if s["_spk"] == p.get("spk") and t0 - 1.5 <= s["_t"] < nxt]
            names = {}
            for s in mine:
                names[(s["speaker"], s["speaker_source"])] = names.get((s["speaker"], s["speaker_source"]), 0) + s["_te"] - s["_t"]
            if names:
                name, src = max(names, key=names.get)
            else:
                name, src = self.resolve(p.get("spk"), None)
                name = split_org(name)[0] if src not in ("manuel", "micro_ihm") else name
            end = max((s["_te"] for s in mine), default=min(nxt, t0 + 5))
            q = {"start": hms(t0), "end": hms(end), "speaker": name, "speaker_source": src, "text": p.get("text", ""),
                 "_t": t0, "_te": end}
            if mine and all(s.get("off_meeting") for s in mine):
                q["off_meeting"] = True
            if any(s.get("suspect") for s in mine):
                q["contains_suspect"] = True
            out.append(q)
        return out

    def slides(self) -> list[dict]:
        out = []
        for s in self.doc.get("slides") or []:
            x = {"n": s.get("n"), "time": hms(s.get("t")), "title": s.get("titre") or ""}
            if s.get("repeat_of"):
                x["back_to"] = s["repeat_of"]
            else:
                x["text"] = s.get("texte") or []
                if s.get("visuel"):
                    x["visual"] = s["visuel"]
            out.append(x)
        return out

    # ------------------------------------------------------------------ participants et intervenants
    def speakers(self) -> list[dict]:
        tot: dict[str, dict] = {}
        orgs = {}
        for n in [e.get("ui") for e in self.doc.get("raw") or []] + [p.get("name") for p in self.doc.get("participants") or []] + \
                 [v.get("auto") for v in self.spk.values()]:
            if n:
                nm, org = split_org(DUP.sub("", n))
                if org:
                    orgs.setdefault(nm, org)
        for s in self.segments():
            k = s["speaker"]
            d = tot.setdefault(k, {"name": k, "sources": set(), "speaking_secs": 0.0, "segments": 0,
                                   "first": s["_t"], "last": s["_te"]})
            d["sources"].add(s["speaker_source"])
            d["speaking_secs"] += max(0.0, s["_te"] - s["_t"])
            d["segments"] += 1
            d["last"] = max(d["last"], s["_te"])
        total = sum(d["speaking_secs"] for d in tot.values()) or 1
        out = []
        for d in sorted(tot.values(), key=lambda d: -d["speaking_secs"]):
            name, org = split_org(d["name"])
            out.append({"name": name, "organization": org or orgs.get(name),
                        "speaker_source": sorted(d["sources"]), "speaking_time": hms(d["speaking_secs"]),
                        "share_pct": round(100 * d["speaking_secs"] / total), "segments": d["segments"],
                        "first_spoke": hms(d["first"]), "last_spoke": hms(d["last"])})
        return out

    def participants(self) -> list[dict]:
        """Participants vus dans la réunion (vignettes et liste de la plateforme), à défaut ceux dont le nom a
        été affiché comme orateur. Arrivée / départ = première / dernière apparition (approximatif : une
        vignette n'est visible que si la plateforme l'affiche)."""
        t0 = self.doc.get("t0")
        seen = {p["name"]: p for p in self.doc.get("participants") or []}
        if not seen:   # anciennes transcriptions : noms affichés comme orateur pendant les phrases
            for e in self.doc.get("raw") or []:
                n = e.get("ui")
                if n:
                    p = seen.setdefault(n, {"name": n, "first": e["t"], "last": e.get("te", e["t"]), "seen": 0})
                    p["first"], p["last"] = min(p["first"], e["t"]), max(p["last"], e.get("te", e["t"]))
                    p["spoke_first"], p["spoke_last"] = p["first"], p["last"]
                    p["seen"] += 1
        talk = {d["name"]: d for d in self.speakers()}
        out = []
        for n, p in sorted(seen.items(), key=lambda kv: kv[1].get("first", 0)):
            name, org = split_org(n)
            a = {"name": name, "organization": org, "display_name": n,
                 "arrival": hms(p.get("first")), "departure": hms(p.get("last")),
                 "arrival_time": _abs(t0, p.get("first")), "departure_time": _abs(t0, p.get("last")),
                 "spoke": name in talk or "spoke_first" in p}
            if name in talk:
                a["speaking_time"] = talk[name]["speaking_time"]
            out.append(a)
        return out

    def user_mic(self) -> dict:
        me_segs = [s for s in self.segments() if s["speaker_source"] == "micro_ihm"]
        key = _norm(split_org(self.me)[0])
        heard = [d for d in self.speakers() if d["speaker_source"] != ["micro_ihm"] and key and _norm(d["name"]) == key]
        captured = bool(me_segs or heard)
        r = {"user": self.me, "captured": captured, "ihm_mic_segments": len(me_segs),
             "heard_in_meeting_under_own_name": bool(heard)}
        if not captured and self.segments():
            r["warning"] = (f"Aucune parole de {self.me} n'a été captée (ni par le micro de l'IHM, ni sous son nom "
                            "dans la réunion) : s'il est intervenu depuis une salle ou un autre appareil, ses propos "
                            "manquent ou sont attribués à quelqu'un d'autre.")
        return r


def _abs(t0, rel) -> str | None:
    if t0 is None or rel is None:
        return None
    return dt.datetime.fromtimestamp(float(t0) + float(rel), TZ).isoformat(timespec="seconds")


def meeting_ids(url: str | None, platform: str) -> dict:
    """Identifiants de la réunion tirés de son lien (sans mot de passe)."""
    from urllib.parse import parse_qs, unquote, urlparse
    if not url:
        return {}
    u = urlparse(url)
    q = parse_qs(u.query)
    out = {}
    m = re.search(r"/(?:j|w|s|wc/join|wc)/(\d{9,12})", u.path)
    if m:
        out["zoom_meeting_id"] = m.group(1)
    m = re.search(r"(19:meeting_[^/@]+@thread\.v2)", unquote(u.path + "?" + u.query))
    if m:
        out["teams_thread_id"] = m.group(1)
    if q.get("context"):
        try:
            ctx = json.loads(q["context"][0])
            out.update({k2: ctx[k1] for k1, k2 in (("Tid", "teams_tenant_id"), ("Oid", "teams_organizer_object_id"))
                        if ctx.get(k1)})
        except ValueError:
            pass
    clean = {k: v for k, v in q.items() if k.lower() not in ("pwd", "passcode", "password", "p", "tk")}
    from urllib.parse import urlencode
    out["link"] = u._replace(query=urlencode(clean, doseq=True)).geturl()
    return out


# ============================================================================ serveur
class MCP:
    def __init__(self, *, http, workers: dict, token: str, data_dir: Path, secret: str, site: str | None, users=None):
        self.http, self.workers, self.agent_token = http, workers, token
        self.users = users        # comptes OmniConf (None : pas de filtrage, tout est visible)
        self.tokens = TokenStore(data_dir / "mcp_tokens.json")
        self.lexicon_file = data_dir / "lexicon.md"
        self.key = hmac.new(secret.encode(), b"omniconf-mcp-links", hashlib.sha256).digest()
        self.site = (site or "").rstrip("/")

    # ------------------------------------------------------------------ accès aux workers
    async def _get(self, w: str, path: str, timeout: float = 20):
        r = await self.http.get(self.workers[w] + path, timeout=timeout)
        if r.status_code == 404:
            raise ToolError("introuvable : " + _detail(r))
        if r.status_code >= 400:
            raise ToolError(f"{w} : {_detail(r)}")
        return r

    async def _list_all(self) -> list[dict]:
        async def one(w):
            try:
                rs, st = await asyncio.gather(self.http.get(self.workers[w] + "/recordings", timeout=8),
                                              self.http.get(self.workers[w] + "/status", timeout=4))
                st = st.json()
                live = (st.get("transcript") or {}).get("file") if (st.get("transcript") or {}).get("active") else None
                live_rec = st.get("recording_file") if st.get("recording") else None
                out = []
                for x in rs.json():
                    stem = _stem(x["name"])
                    x["worker"], x["stem"] = w, stem
                    x["live"] = stem in (live, _stem(live_rec or ""))
                    out.append(x)
                return out
            except Exception:
                return []
        lists = await asyncio.gather(*(one(w) for w in self.workers))
        u = CALLER.get()
        return [x for l in lists for x in l if u is None or _users.can_see(u, x.get("meta"))]

    async def _allowed(self, w: str, stem: str, edit: bool = False):
        """Réunion accessible au compte du jeton (modification : propriétaire ou administrateur)."""
        u = CALLER.get()
        rec = next((x for x in await self._list_all() if x["worker"] == w and x["stem"] == stem), None)
        if not rec:
            raise ToolError(f"réunion inconnue : {w}:{stem}")
        if edit and u is not None and not _users.can_edit(u, rec.get("meta")):
            raise ToolError("seul le propriétaire de la réunion (ou un administrateur) peut y déposer un compte rendu")

    def _parse_id(self, mid: str) -> tuple[str, str]:
        if not isinstance(mid, str) or ":" not in mid:
            raise ToolError("id attendu sous la forme « w1:20261110-160042_teams » (voir list_meetings)")
        w, stem = mid.split(":", 1)
        stem = _stem(stem)
        if w not in self.workers or not STEM_RE.match(stem):
            raise ToolError(f"réunion inconnue : {mid}")
        return w, stem

    async def _meeting(self, mid: str, need_doc: bool = True) -> Meeting:
        w, stem = self._parse_id(mid)
        recs = [x for x in await self._list_all() if x["worker"] == w and x["stem"] == stem]
        if not recs:
            raise ToolError(f"réunion inconnue : {mid}")
        rec = recs[0]
        doc = None
        if need_doc and rec.get("transcript"):
            doc = (await self._get(w, f"/recordings/{stem}.transcript.json/transcript", timeout=30)).json()
        return Meeting(mid, rec, doc, live=rec.get("live", False))

    # ------------------------------------------------------------------ liens signés
    def sign(self, payload: dict, ttl: int) -> tuple[str, str]:
        exp = int(time.time()) + ttl
        raw = _b64u(json.dumps({**payload, "e": exp}, separators=(",", ":")).encode())
        sig = _b64u(hmac.new(self.key, raw.encode(), hashlib.sha256).digest()[:20])
        return f"{self.site}/mcp/link/{raw}.{sig}", dt.datetime.fromtimestamp(exp, TZ).isoformat(timespec="seconds")

    def verify(self, tok: str) -> dict | None:
        try:
            raw, sig = tok.split(".", 1)
            good = _b64u(hmac.new(self.key, raw.encode(), hashlib.sha256).digest()[:20])
            if not hmac.compare_digest(good, sig):
                return None
            p = json.loads(_unb64u(raw))
            return p if p.get("e", 0) >= time.time() else None
        except Exception:
            return None

    # ------------------------------------------------------------------ protocole
    def tools(self, scope: str) -> list[dict]:
        return [t for t in TOOLS if t["_scope"] == scope]

    async def handle(self, msg: dict, entry: dict) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                return None   # réponse du client à une requête du serveur (jamais émise ici)
            return _err(msg.get("id") if isinstance(msg, dict) else None, -32600, "requête JSON-RPC invalide")
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        notification = "id" not in msg
        log.info("mcp [%s] %s%s", entry.get("name"), method,
                 f" {params.get('name')} {json.dumps(params.get('arguments') or {}, ensure_ascii=False)[:160]}"
                 if method == "tools/call" else (f" {params.get('protocolVersion')}" if method == "initialize" else ""))
        try:
            if method == "initialize":
                req = params.get("protocolVersion")
                ver = req if req in SUPPORTED and req != "2026-07-28" else LEGACY_LATEST
                res = {"protocolVersion": ver, **self._server_info()}
            elif method == "server/discover":
                res = {"supportedVersions": SUPPORTED, **self._server_info()}
            elif method == "ping":
                res = {}
            elif method == "tools/list":
                res = {"tools": [{k: v for k, v in t.items() if not k.startswith("_")} for t in self.tools(entry["scope"])]}
            elif method == "tools/call":
                res = await self.call(params.get("name"), params.get("arguments") or {}, entry)
            elif method.startswith("notifications/"):
                return None
            else:
                return None if notification else _err(mid, -32601, f"méthode inconnue : {method}")
        except RPCError as e:
            return None if notification else _err(mid, e.code, str(e))
        return None if notification else {"jsonrpc": "2.0", "id": mid, "result": res}

    def _server_info(self) -> dict:
        return {"capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "omniconf-webconf", "title": "OmniConf — réunions enregistrées", "version": VERSION},
                "instructions": INSTRUCTIONS}

    def caller(self, entry: dict) -> dict | None:
        """Compte du jeton ; jetons d'avant les comptes : premier administrateur."""
        if not self.users:
            return None
        u = self.users.get(entry.get("user") or self.users.first_admin())
        if not u or u.get("disabled"):
            raise RPCError(-32001, "compte du jeton supprimé ou désactivé")
        return u

    async def call(self, name: str, args: dict, entry: dict) -> dict:
        CALLER.set(self.caller(entry))
        tool = next((t for t in self.tools(entry["scope"]) if t["name"] == name), None)
        if not tool:
            if any(t["name"] == name for t in TOOLS):
                raise RPCError(-32602, f"outil {name} non autorisé avec ce jeton (portée {entry['scope']})")
            raise RPCError(-32602, f"outil inconnu : {name}")
        try:
            out = await getattr(self, "t_" + name)(**_args(tool, args))
        except ToolError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"paramètre invalide : {e}"}], "isError": True}
        if isinstance(out, dict) and "content" in out:
            return out
        return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=1)}]}

    # ------------------------------------------------------------------ outils
    async def t_list_meetings(self, since=None, until=None, status=None, query=None, page=1, per_page=20):
        a, b = parse_when(since), parse_when(until, end=True)
        want = _status_key(status) if status else None
        items = []
        for x in await self._list_all():
            m = x.get("meta") or {}
            start = m.get("started_at")
            try:
                sd = dt.datetime.fromisoformat(start) if start else dt.datetime.fromtimestamp(x["mtime"], TZ)
            except ValueError:
                sd = dt.datetime.fromtimestamp(x["mtime"], TZ)
            if (a and sd < a) or (b and sd >= b):
                continue
            st = _status(x)
            if want and want not in st["all"]:
                continue
            title = m.get("title") or x["stem"]
            if query and _norm(query) not in _norm(f"{title} {x.get('cr_title') or ''} {x['stem']}"):
                continue
            items.append((sd, {
                "id": f"{x['worker']}:{x['stem']}", "title": title, "cr_title": x.get("cr_title"),
                "start": sd.astimezone(TZ).isoformat(timespec="seconds"), "end": local_iso(m.get("ended_at")),
                "duration": hms(x.get("duration")), "duration_secs": x.get("duration"),
                "platform": m.get("platform") or "web", "status": st["main"], "has": st["has"]}))
        items.sort(key=lambda it: it[0], reverse=True)
        per_page = max(1, min(100, int(per_page or 20)))
        page = max(1, int(page or 1))
        pages = max(1, -(-len(items) // per_page))
        return {"total": len(items), "page": page, "pages": pages, "per_page": per_page,
                "status_values": "recording_in_progress | recorded | transcribed | draft_cr | final_cr",
                "meetings": [it[1] for it in items[(page - 1) * per_page: page * per_page]]}

    async def t_get_meeting(self, id):
        m = await self._meeting(id)
        rec, meta, doc = m.rec, m.meta, m.doc
        segs = m.segments()
        win = m.window(segs) if segs else None
        st = _status(rec)
        warnings = []
        um = m.user_mic()
        if um.get("warning"):
            warnings.append(um["warning"])
        sus = [s for s in segs if s.get("suspect")]
        if sus:
            warnings.append(f"{len(sus)} segment(s) suspect(s) (hallucination probable de la reconnaissance vocale) : "
                            "à vérifier à l'écoute (get_audio_url) avant de les citer.")
        anon = sorted({s["speaker"] for s in segs if s["speaker_source"] == "diarisation" and s["speaker"].startswith("Intervenant")})
        if anon:
            warnings.append(f"Intervenants non nommés par la plateforme : {', '.join(anon)} (voix distinguées, noms inconnus).")
        if win:
            warnings.append(f"Séquences hors réunion avant {hms(win[0])} ou après {hms(win[1])} (segments off_meeting=true).")
        if m.live:
            warnings.append("Réunion en cours : transcription incomplète (mise à jour toutes les ~20 s).")
        stats = doc.get("asr_stats") or {}
        out = {
            "id": m.id, "title": meta.get("title"), "cr_title": rec.get("cr_title"),
            "platform": m.platform, "site": meta.get("site"),
            "start": local_iso(meta.get("started_at")), "end": local_iso(meta.get("ended_at")),
            "duration": hms(rec.get("duration")), "status": st["main"], "has": st["has"],
            "worker": m.id.split(":")[0],
            "meeting": meeting_ids(meta.get("url"), m.platform),
            "calendar": meta.get("calendar") or None,
            "effective_meeting": {"start": hms(win[0]), "end": hms(win[1]),
                                  "method": "blocs de parole à ≥ 2 voix, séparés par > 3 min de silence"} if win else None,
            "participants": m.participants(),
            "speakers": m.speakers(),
            "user_mic": um,
            "transcript": {"segments": len(segs), "paragraphs": len(doc.get("paragraphs") or []),
                           "suspect_segments": len(sus), "off_meeting_segments": sum(1 for s in segs if s.get("off_meeting")),
                           "asr_model": meta.get("asr"), "format_model": meta.get("llm"), "language": meta.get("lang"),
                           "retranscribed": stats.get("retries"), "hallucinations_removed": len(stats.get("removed") or [])},
            "slides": len(doc.get("slides") or []),
            "draft_cr": {"available": bool(rec.get("report")), "job": rec.get("report_job")},
            "final_cr": {"available": bool(rec.get("final_cr"))},
            "audio": {"available": rec.get("kind") == "video"},
            "warnings": warnings,
        }
        if not out["calendar"]:
            out["calendar_note"] = ("Lien d'agenda non connu du service (réunion lancée par son lien) : rapprocher "
                                    "par l'heure de début et le lien de la réunion (meeting.link).")
        return out

    async def t_get_transcript(self, id, format="segments", start=None, end=None, cursor=None, include_off_meeting=True):
        if format not in ("segments", "text", "paragraphs"):
            raise ValueError("format : segments, paragraphs ou text")
        m = await self._meeting(id)
        if not m.doc:
            raise ToolError("pas de transcription pour cette réunion")
        a, b = parse_hms(start), parse_hms(end)
        items = m.segments() if format == "segments" else m.paragraphs()
        if format == "text":   # diapositives à leur place dans le fil
            for s in m.doc.get("slides") or []:
                t = float(s.get("t") or 0)
                body = "retour à « %s »" % (s.get("titre") or "la diapositive") if s.get("repeat_of") else \
                    (s.get("titre") or "Diapositive") + (" — " + " ; ".join(s.get("texte") or []) if s.get("texte") else "") + \
                    (f" — (visuel : {s['visuel']})" if s.get("visuel") else "")
                items.append({"_t": t - 0.01, "_te": t, "_slide": body, "start": hms(t)})
            items.sort(key=lambda x: x["_t"])
        sel = [x for x in items if (a is None or x["_te"] >= a) and (b is None or x["_t"] <= b)
               and (include_off_meeting or not x.get("off_meeting"))]
        i0 = int(cursor or 0)
        if i0 < 0 or i0 > len(sel):
            raise ValueError("cursor invalide")
        page, size = [], 0
        for x in sel[i0:]:
            if format == "text":
                if "_slide" in x:
                    line = f"[{x['start']}] [ÉCRAN PARTAGÉ] {x['_slide']}"
                else:
                    flags = (" (hors réunion)" if x.get("off_meeting") else "") + (" (passage douteux)" if x.get("contains_suspect") else "")
                    line = f"[{x['start']}] {x['speaker']}{flags} : {x['text']}"
                piece = line
            else:
                piece = {k: v for k, v in x.items() if not k.startswith("_") and v is not None}
            n = len(piece if isinstance(piece, str) else json.dumps(piece, ensure_ascii=False)) + 2
            if page and size + n > PAGE_CHARS:
                break
            page.append(piece)
            size += n
        nxt = i0 + len(page)
        head = {"id": m.id, "format": format, "start_time": local_iso(m.meta.get("started_at")),
                "items": len(page), "total": len(sel), "next_cursor": str(nxt) if nxt < len(sel) else None}
        if format == "text":
            hdr = (f"# {m.meta.get('title') or m.id} — {head['start_time'] or ''}\n"
                   f"(éléments {i0 + 1} à {nxt} sur {len(sel)}"
                   + (f" ; suite : cursor=\"{head['next_cursor']}\"" if head["next_cursor"] else "") + ")\n\n")
            return {"content": [{"type": "text", "text": hdr + "\n".join(page)}]}
        head["notes"] = ("speaker_source : teams/zoom = nom affiché par la plateforme pendant la phrase ; diarisation = "
                         "voix reconnue (nom voté sur les autres phrases de la même voix, ou « Intervenant N ») ; "
                         "manuel = renommé dans l'IHM ; micro_ihm = l'utilisateur via le micro de l'IHM. "
                         "confidence : probabilité moyenne de la reconnaissance (Whisper), absente sur les anciennes "
                         "réunions. suspect : hallucination probable, à vérifier à l'écoute.") if format == "segments" else None
        head[format] = page
        # JSON compact : la taille de la page est celle qui a été comptée (≈ PAGE_CHARS)
        return {"content": [{"type": "text", "text": json.dumps(head, ensure_ascii=False)}]}

    async def t_get_draft_cr(self, id, cursor=None):
        w, stem = self._parse_id(id)
        await self._allowed(w, stem)
        info = (await self._get(w, f"/recordings/{stem}.mp4/report?fmt=json", timeout=30)).json()
        rep, job = info.get("report"), info.get("job")
        if not rep:
            if job and job.get("state") == "running":
                raise ToolError(f"compte rendu en cours de génération ({job.get('step') or 'en cours'}) : réessayer dans quelques minutes")
            raise ToolError("pas de compte rendu brouillon pour cette réunion" + (f" ({job.get('error')})" if job and job.get("error") else ""))
        md = (await self._get(w, f"/recordings/{stem}.mp4/report?fmt=md", timeout=60)).text
        i0 = int(cursor or 0)
        part = md[i0:i0 + PAGE_CHARS]
        if i0 + PAGE_CHARS < len(md):   # coupe à une fin de ligne
            cut = part.rfind("\n")
            part = part[:cut + 1] if cut > PAGE_CHARS // 2 else part
        nxt = i0 + len(part)
        steps = rep.get("steps") or []
        models = {"drafting": [s["modele"] for s in steps if s.get("etape") == "rédaction" and s.get("ok")],
                  "arbitration": next((s["modele"] for s in steps if s.get("etape") == "confrontation" and s.get("ok")), None),
                  "quality_check": next((s["modele"] for s in steps if s.get("etape") == "contrôle" and s.get("ok")), None)}
        hdr = {"id": id, "format_version": rep.get("version") or 1,
               "generated_at": local_iso(rep.get("generated_at")), "generation_secs": rep.get("secs"),
               "models": models, "chars": len(md), "next_cursor": str(nxt) if nxt < len(md) else None}
        return {"content": [{"type": "text", "text": json.dumps(hdr, ensure_ascii=False)},
                            {"type": "text", "text": part}]}

    async def t_get_slides(self, id, images=False, from_n=1, max_images=4):
        m = await self._meeting(id)
        slides = m.slides()
        if not slides:
            raise ToolError("aucune diapositive lue pour cette réunion (pas de partage d'écran détecté, "
                            "ou lecture non faite : bouton « Lire les diapositives » de l'IHM)")
        w, stem = self._parse_id(id)
        for s in slides:
            if "back_to" not in s:
                s["image_url"], _ = self.sign({"k": "slide", "w": w, "s": stem, "n": s["n"]}, LINK_TTL_MAX)
        out = {"id": m.id, "count": len(slides), "image_links_ttl_hours": LINK_TTL_MAX // 3600,
               "note": "texte lu à l'écran par un modèle d'image (OCR + description) ; heures = depuis le début de l'enregistrement",
               "slides": slides}
        content = [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=1)}]
        if images:
            max_images = max(1, min(8, int(max_images or 4)))
            want = [s for s in slides if "back_to" not in s and s["n"] >= int(from_n or 1)][:max_images]
            for s in want:
                try:
                    r = await self._get(w, f"/slides/{stem}/{s['n']}", timeout=15)
                    content.append({"type": "text", "text": f"Diapositive {s['n']} ({s['time']}) : {s['title']}"})
                    content.append({"type": "image", "data": base64.b64encode(r.content).decode(), "mimeType": "image/jpeg"})
                except ToolError:
                    pass
        return {"content": content}

    async def t_get_audio_url(self, id, start=None, end=None, format="mp3", ttl_minutes=15):
        m = await self._meeting(id)
        if m.rec.get("kind") != "video":
            raise ToolError("pas d'enregistrement audio/vidéo pour cette réunion (transcription seule)")
        if format not in ("mp3", "wav"):
            raise ValueError("format : mp3 ou wav")
        a, b = parse_hms(start), parse_hms(end)
        if a is not None and b is not None and b <= a:
            raise ValueError("end doit suivre start")
        # horodatages de la transcription -> temps de la vidéo
        try:
            rec_t0 = dt.datetime.fromisoformat((m.rec.get("meta") or {})["started_at"]).timestamp()
            off = float(m.doc.get("t0") or rec_t0) - rec_t0 if m.doc else 0.0
        except (KeyError, ValueError):
            off = 0.0
        va = max(0.0, (a or 0.0) + off)
        vb = (b + off) if b is not None else None
        ttl = max(60, min(LINK_TTL_MAX, int(float(ttl_minutes or 15) * 60)))
        w, stem = self._parse_id(id)
        url, exp = self.sign({"k": "audio", "w": w, "s": stem, "a": round(va, 2), "b": round(vb, 2) if vb else None,
                              "f": format}, ttl)
        return {"id": m.id, "url": url, "expires_at": exp, "start": hms(a or 0), "end": hms(b) if b is not None else None,
                "format": format, "audio": "mono 16 kHz, son de la réunion + micro de l'IHM",
                "note": "lien temporaire, sans jeton ; à télécharger (curl) puis transcrire au besoin"}

    async def t_get_lexicon(self):
        try:
            text = self.lexicon_file.read_text(encoding="utf-8")
            mt = self.lexicon_file.stat().st_mtime
        except OSError:
            return {"version": None, "text": "", "note": "aucun lexique saisi dans OmniConf"}
        return {"version": hashlib.sha1(text.encode()).hexdigest()[:12] if text.strip() else None,
                "updated_at": dt.datetime.fromtimestamp(mt, TZ).isoformat(timespec="seconds"), "text": text,
                "note": "Lexique utilisé par le service (transcription, noms, compte rendu). Il se modifie dans "
                        "l'IHM (menu ⋯ → Lexique) : proposer les ajouts à l'utilisateur, au même format."}

    async def t_attach_cr(self, id, markdown, pdf_base64=None):
        w, stem = self._parse_id(id)
        await self._allowed(w, stem, edit=True)
        if not (markdown or "").strip():
            raise ValueError("markdown vide")
        if len(markdown) > 2_000_000 or (pdf_base64 and len(pdf_base64) > 30_000_000):
            raise ValueError("fichier trop volumineux")
        r = await self.http.put(self.workers[w] + f"/recordings/{stem}.mp4/final", timeout=60,
                                json={"markdown": markdown, "pdf_b64": pdf_base64, "author": "claude (MCP)"})
        if r.status_code >= 400:
            raise ToolError(f"dépôt refusé : {_detail(r)}")
        d = r.json()
        return {"id": id, "stored": True, "at": local_iso(d.get("at")), "markdown_chars": d.get("md_chars"),
                "pdf_bytes": d.get("pdf_bytes")}


class RPCError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


class ToolError(Exception):
    pass


def _err(mid, code, msg) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}}


def _detail(r) -> str:
    try:
        return str(r.json().get("detail"))
    except Exception:
        return r.text[:200] or str(r.status_code)


def _stem(name: str) -> str:
    name = os.path.basename(name or "")
    for ext in (".transcript.json", ".report.json", ".mp4", ".md"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


STATUS_ORDER = ["recording_in_progress", "final_cr", "draft_cr", "transcribed", "recorded"]


def _status(x: dict) -> dict:
    has = {"video": x.get("kind") == "video", "transcript": bool(x.get("transcript")),
           "draft_cr": bool(x.get("report")), "final_cr": bool(x.get("final_cr")),
           "in_progress": bool(x.get("live") or x.get("recording"))}
    st = []
    if has["in_progress"]:
        st.append("recording_in_progress")
    if has["final_cr"]:
        st.append("final_cr")
    if has["draft_cr"]:
        st.append("draft_cr")
    if has["transcript"]:
        st.append("transcribed")
    if has["video"]:
        st.append("recorded")
    return {"main": st[0] if st else "recorded", "all": st, "has": has}


STATUS_ALIASES = {"en_cours": "recording_in_progress", "in_progress": "recording_in_progress", "live": "recording_in_progress",
                  "enregistre": "recorded", "enregistrée": "recorded", "enregistré": "recorded",
                  "transcrit": "transcribed", "cr_brouillon": "draft_cr", "brouillon": "draft_cr", "draft": "draft_cr",
                  "cr_final": "final_cr", "final": "final_cr"}


def _status_key(s: str) -> str:
    k = _norm(s).replace(" ", "_")
    k = STATUS_ALIASES.get(k, STATUS_ALIASES.get(s, k))
    if k not in STATUS_ORDER:
        raise ValueError(f"statut inconnu : {s!r} ({' | '.join(STATUS_ORDER)})")
    return k


def _args(tool: dict, args: dict) -> dict:
    props = tool["inputSchema"].get("properties", {})
    unknown = set(args) - set(props)
    if unknown:
        raise RPCError(-32602, f"paramètre(s) inconnu(s) : {', '.join(sorted(unknown))}")
    for r in tool["inputSchema"].get("required", []):
        if r not in args:
            raise RPCError(-32602, f"paramètre manquant : {r}")
    return args


# ============================================================================ définition des outils
_RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
_ID = {"type": "string", "description": "Identifiant de la réunion, ex. « w1:20261110-160042_teams » (voir list_meetings)"}
_T = "Horodatage HH:MM:SS depuis le début de l'enregistrement (comme dans les segments)"
TOOLS = [
    {"name": "list_meetings", "title": "Lister les réunions", "_scope": "read", "annotations": _RO,
     "description": "Liste les réunions enregistrées, de la plus récente à la plus ancienne : id, titre (et titre du compte "
                    "rendu), début/fin (heure de Paris), durée, plateforme, statut (recording_in_progress, recorded, "
                    "transcribed, draft_cr, final_cr). Filtres par dates, statut et texte ; paginé.",
     "inputSchema": {"type": "object", "properties": {
         "since": {"type": "string", "description": "Début au plus tôt : AAAA-MM-JJ ou AAAA-MM-JJTHH:MM (heure de Paris par défaut)"},
         "until": {"type": "string", "description": "Début au plus tard (une date seule inclut toute la journée)"},
         "status": {"type": "string", "description": "recording_in_progress | recorded | transcribed | draft_cr | final_cr (ou en_cours, transcrit, cr_brouillon…)"},
         "query": {"type": "string", "description": "Texte cherché dans le titre (et le titre du compte rendu)"},
         "page": {"type": "integer", "minimum": 1, "default": 1},
         "per_page": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}}},
    {"name": "get_meeting", "title": "Métadonnées d'une réunion", "_scope": "read", "annotations": _RO,
     "description": "Métadonnées complètes d'une réunion : plateforme, heures, identifiants (lien sans mot de passe, "
                    "ids Teams/Zoom), participants vus dans la réunion (nom, organisation, arrivée/départ approximatifs), "
                    "intervenants (temps de parole, source du nom), indicateur de captation du micro de l'utilisateur, "
                    "réunion effective (séquences hors réunion), statistiques de transcription, disponibilité des "
                    "comptes rendus et de l'audio, avertissements à lire avant de rédiger.",
     "inputSchema": {"type": "object", "properties": {"id": _ID}, "required": ["id"]}},
    {"name": "get_transcript", "title": "Transcription d'une réunion", "_scope": "read", "annotations": _RO,
     "description": "Transcription paginée (≈40 000 caractères par page ; continuer avec next_cursor). "
                    "format=segments (défaut) : phrases telles que reconnues, {start, end, speaker, speaker_source "
                    "(teams|zoom|diarisation|manuel|micro_ihm), text, confidence, suspect, off_meeting}. "
                    "format=paragraphs : paragraphes mis en forme (ponctuation, hésitations retirées) avec début/fin. "
                    "format=text : texte lisible « [HH:MM:SS] Nom : texte », diapositives incluses à leur place.",
     "inputSchema": {"type": "object", "properties": {
         "id": _ID,
         "format": {"type": "string", "enum": ["segments", "paragraphs", "text"], "default": "segments"},
         "start": {"type": "string", "description": "Ne garder qu'à partir de cet horodatage (" + _T + ")"},
         "end": {"type": "string", "description": "Jusqu'à cet horodatage"},
         "cursor": {"type": "string", "description": "Suite de la pagination (next_cursor de la page précédente)"},
         "include_off_meeting": {"type": "boolean", "default": True,
                                 "description": "Inclure les séquences hors réunion (marquées off_meeting)"}},
         "required": ["id"]}},
    {"name": "get_draft_cr", "title": "Premier compte rendu", "_scope": "read", "annotations": _RO,
     "description": "Premier compte rendu produit par le service (Markdown), avec les modèles utilisés (rédaction, "
                    "arbitrage, contrôle) et sa date de génération. À relire : il peut contenir des erreurs "
                    "d'attribution ou de transcription.",
     "inputSchema": {"type": "object", "properties": {"id": _ID, "cursor": {"type": "string"}}, "required": ["id"]}},
    {"name": "get_slides", "title": "Écran partagé", "_scope": "read", "annotations": _RO,
     "description": "Diapositives et documents partagés à l'écran : horodatage, titre, texte lu (OCR) et description des "
                    "schémas ; retours sur une diapositive déjà vue (back_to). Liens temporaires vers les captures ; "
                    "images=true joint aussi les captures elles-mêmes (max_images, à partir de from_n).",
     "inputSchema": {"type": "object", "properties": {
         "id": _ID, "images": {"type": "boolean", "default": False},
         "from_n": {"type": "integer", "minimum": 1, "default": 1},
         "max_images": {"type": "integer", "minimum": 1, "maximum": 8, "default": 4}}, "required": ["id"]}},
    {"name": "get_audio_url", "title": "Lien audio temporaire", "_scope": "read", "annotations": _RO,
     "description": "Lien temporaire (sans jeton) vers l'audio de la réunion, en entier ou un passage (start/end), "
                    "en MP3 ou WAV mono 16 kHz : pour réécouter ou retranscrire un passage douteux.",
     "inputSchema": {"type": "object", "properties": {
         "id": _ID, "start": {"type": "string", "description": _T}, "end": {"type": "string", "description": _T},
         "format": {"type": "string", "enum": ["mp3", "wav"], "default": "mp3"},
         "ttl_minutes": {"type": "number", "minimum": 1, "maximum": 240, "default": 15}}, "required": ["id"]}},
    {"name": "get_lexicon", "title": "Lexique du projet", "_scope": "read", "annotations": _RO,
     "description": "Lexique utilisé par le service, au format Markdown : personnes (orthographe, rôle, variantes "
                    "entendues), corrections de transcription (entendu → correct), sigles et termes, registre des "
                    "sujets suivis. Version (empreinte) et date de mise à jour : la même version sert à la "
                    "transcription et aux premiers comptes rendus.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "attach_cr", "title": "Déposer le compte rendu final", "_scope": "write",
     "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
     "description": "Dépose le compte rendu FINAL à côté de la réunion (Markdown, et PDF en base64 facultatif). "
                    "Remplace le précédent dépôt. À n'utiliser qu'à la demande explicite de l'utilisateur.",
     "inputSchema": {"type": "object", "properties": {
         "id": _ID, "markdown": {"type": "string"},
         "pdf_base64": {"type": "string", "description": "PDF encodé en base64 (facultatif)"}},
         "required": ["id", "markdown"]}},
]


# ============================================================================ routes FastAPI
def mount(app, *, http, workers, token, data_dir, secret, site, require_login, users=None, current_user=None):
    from fastapi import Depends, HTTPException, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    srv = MCP(http=http, workers=workers, token=token, data_dir=data_dir, secret=secret, site=site, users=users)
    CU = Depends(current_user) if current_user else None
    allowed_origins = {site.rstrip("/")} if site else set()

    @app.post("/mcp")
    async def mcp_post(request: Request):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in allowed_origins:
            return JSONResponse(_err(None, -32600, "origine refusée"), status_code=403)
        if not srv.site:   # adresse publique des liens temporaires (SITE_ADDRESS, sinon celle de la requête)
            srv.site = f"{request.headers.get('x-forwarded-proto', request.url.scheme)}://{request.headers.get('host', request.url.netloc)}"
        auth = request.headers.get("authorization", "")
        entry = srv.tokens.check(auth[7:].strip() if auth.lower().startswith("bearer ") else "")
        if not entry:
            await asyncio.sleep(0.5)
            return JSONResponse(_err(None, -32001, "jeton absent, invalide ou révoqué"), status_code=401)
        pv = request.headers.get("mcp-protocol-version")
        if pv and pv not in SUPPORTED:
            return JSONResponse(_err(None, -32600, f"version de protocole non prise en charge : {pv}"), status_code=400)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(_err(None, -32700, "JSON invalide"), status_code=400)
        msgs = body if isinstance(body, list) else [body]
        out = [r for r in [await srv.handle(m, entry) for m in msgs] if r is not None]
        if not out:
            return Response(status_code=202)
        return JSONResponse(out if isinstance(body, list) else out[0], headers={"Cache-Control": "no-store"})

    @app.get("/mcp")
    async def mcp_get():
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.delete("/mcp")
    async def mcp_delete():
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.get("/mcp/link/{tok}")
    async def mcp_link(tok: str):
        p = srv.verify(tok)
        if not p or p.get("w") not in workers or not STEM_RE.match(p.get("s", "")):
            raise HTTPException(403, "lien invalide ou expiré")
        if p["k"] == "slide":
            r = await http.get(workers[p["w"]] + f"/slides/{p['s']}/{int(p['n'])}", timeout=15)
            if r.status_code >= 400:
                raise HTTPException(r.status_code)
            return Response(r.content, media_type="image/jpeg",
                            headers={"Content-Disposition": f'inline; filename="{p["s"]}.slide-{int(p["n"]):03d}.jpg"'})
        if p["k"] != "audio":
            raise HTTPException(403)
        q = f"?start={p['a']}&fmt={p['f']}" + (f"&end={p['b']}" if p.get("b") else "")
        req = http.build_request("GET", workers[p["w"]] + f"/recordings/{p['s']}.mp4/audio{q}", timeout=None)
        r = await http.send(req, stream=True)
        if r.status_code >= 400:
            await r.aclose()
            raise HTTPException(r.status_code)
        fn = f"{p['s']}_{hms(p['a']).replace(':', '')}" + (f"-{hms(p['b']).replace(':', '')}" if p.get("b") else "") + f".{p['f']}"

        async def body():
            try:
                async for chunk in r.aiter_raw():
                    yield chunk
            finally:
                await r.aclose()
        return StreamingResponse(body(), media_type="audio/mpeg" if p["f"] == "mp3" else "audio/wav",
                                 headers={"Content-Disposition": f'attachment; filename="{fn}"'})

    # ------------------------------------------------------------------ gestion des jetons (IHM)
    from pydantic import BaseModel

    class NewToken(BaseModel):
        name: str = "claude"
        scope: str = "read"

    ca = Path(data_dir) / "caddy-root.crt"   # copie publique du certificat racine de Caddy (déposée à l'installation)

    def ca_fingerprint() -> str | None:
        try:
            pem = ca.read_text()
            der = base64.b64decode("".join(l for l in pem.splitlines() if l and not l.startswith("-----")))
            return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())
        except (OSError, ValueError):
            return None

    @app.get("/ca.crt")
    async def ca_cert():
        """Certificat racine de l'IHM (autorité locale de Caddy), à installer sur le poste client."""
        from fastapi.responses import FileResponse
        if not ca.exists():
            raise HTTPException(404, "certificat non publié")
        return FileResponse(ca, media_type="application/x-x509-ca-cert", filename="omniconf-ca.crt")

    def owner_of(e: dict) -> str | None:
        return e.get("user") or (users.first_admin() if users else None)

    def visible(u: dict | None, e: dict) -> bool:
        return u is None or _users.is_admin(u) or owner_of(e) == u["login"]

    @app.get("/api/mcp/tokens", dependencies=[require_login])
    async def tokens_list(u: dict = CU):
        names = users.names() if users else {}
        return {"url": (site or "") + "/mcp", "ca": "/ca.crt" if ca.exists() else None,
                "ca_sha256": ca_fingerprint(),
                "tokens": [{**e, "user": owner_of(e), "user_name": names.get(owner_of(e))}
                           for e in srv.tokens.public() if visible(u, e)]}

    @app.post("/api/mcp/tokens", dependencies=[require_login])
    async def tokens_create(body: NewToken, u: dict = CU):
        try:
            tok, entry = srv.tokens.create(body.name, body.scope, u["login"] if u else None)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"token": tok, **{k: v for k, v in entry.items() if k != "hash"}}

    @app.delete("/api/mcp/tokens/{tid}", dependencies=[require_login])
    async def tokens_revoke(tid: str, u: dict = CU):
        e = next((x for x in srv.tokens.public() if x["id"] == tid), None)
        if e and not visible(u, e):
            raise HTTPException(403, "jeton d'un autre utilisateur")
        if not srv.tokens.revoke(tid):
            raise HTTPException(404, "jeton inconnu ou déjà révoqué")
        return {"ok": True}

    return srv


# ============================================================================ ligne de commande
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Jetons du serveur MCP (DATA_DIR/mcp_tokens.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="crée un jeton (affiché une seule fois)")
    c.add_argument("--name", default="claude")
    c.add_argument("--scope", choices=["read", "write"], default="read")
    c.add_argument("--user", help="compte OmniConf du jeton (ses réunions seulement ; défaut : premier administrateur)")
    c.add_argument("--out", help="écrire le jeton dans ce fichier (mode 600) au lieu de l'afficher")
    sub.add_parser("list", help="liste les jetons")
    r = sub.add_parser("revoke", help="révoque un jeton")
    r.add_argument("id")
    a = ap.parse_args()
    store = TokenStore(Path(os.environ.get("DATA_DIR", "/var/lib/webconf")) / "mcp_tokens.json")
    if a.cmd == "create":
        tok, e = store.create(a.name, a.scope, a.user)
        if a.out:
            Path(a.out).write_text(tok + "\n")
            os.chmod(a.out, 0o600)
            print(f"jeton {e['id']} ({e['scope']}) écrit dans {a.out}")
        else:
            print(tok)
    elif a.cmd == "list":
        for e in store.public():
            print(f"{e['id']}  {e['scope']:5}  {e['name']:20}  créé {e['created']}  utilisé {e['last_used'] or '-'}"
                  + (f"  RÉVOQUÉ {e['revoked']}" if e.get("revoked") else ""))
    else:
        print("révoqué" if store.revoke(a.id) else "jeton inconnu ou déjà révoqué")
