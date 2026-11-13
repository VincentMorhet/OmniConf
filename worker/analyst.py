"""Analyse de réunion par LLM : points d'étape en direct (~ toutes les 5 min) et compte rendu final.

Analyse en direct (un seul modèle, rapide) : toutes les ANALYSIS_EVERY secondes, la transcription
entière est relue, avec l'analyse précédente pour rester stable. Le modèle produit :
  - un « flash » sur ce qui vient de se dire depuis le dernier point ;
  - l'état cumulé : synthèse, points à retenir, ce qui te concerne, décisions, actions,
    questions en suspens, points de vigilance, sujets abordés.

Compte rendu final (plusieurs modèles qui se contrôlent mutuellement) :
  1. rédaction : chaque modèle de REPORT_MODELS rédige seul, en parallèle, un compte rendu complet
     à partir de la transcription intégrale ;
  2. confrontation : REPORT_JUDGE compare les projets point par point. Il tranche chaque
     divergence en revenant à la transcription, ajoute les oublis et retire ce qui n'est pas étayé ;
  3. contrôle qualité : REPORT_AUDITOR, un autre modèle, revérifie chaque élément contre la
     transcription (qui, quoi, chiffres, dates, horodatages) et corrige.
Les arbitrages et corrections sont conservés en annexe pour la traçabilité.

Les personnes sont désignées par leur identifiant ({S1}, {ME}) dans le JSON. Le vrai nom est
substitué à l'affichage, donc renommer un intervenant après coup met aussi à jour le compte rendu.

Modèles : nom seul = OVH AI Endpoints. Autre API compatible OpenAI : « modele@https://api/v1#VAR_CLE »
(la clé est lue dans la variable d'environnement VAR_CLE).
"""
import asyncio
import datetime as dt
import io
import json
import logging
import os
import re
import time
from zoneinfo import ZoneInfo

import httpx
from collections import Counter

import lexicon

log = logging.getLogger("analyst")

API = os.environ.get("OVH_AI_BASE_URL", "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1")
TOKEN = os.environ.get("OVH_AI_ENDPOINTS_ACCESS_TOKEN", "")
LIVE_MODEL = os.environ.get("ANALYSIS_MODEL", os.environ.get("TRANSCRIPT_LLM_MODEL", "Qwen3.8-27B") + ":low")
LIVE_EVERY = float(os.environ.get("ANALYSIS_EVERY", "300"))
LIVE_MIN_WORDS = int(os.environ.get("ANALYSIS_MIN_WORDS", "40"))
REPORT_MODELS = [m.strip() for m in os.environ.get(
    "REPORT_MODELS", "Qwen3.8-27B:low,Qwen3.5-397B-A17B:none,gpt-oss-120b:low").split(",") if m.strip()]
REPORT_JUDGE = os.environ.get("REPORT_JUDGE", "Qwen3.8-27B:medium")
REPORT_AUDITOR = os.environ.get("REPORT_AUDITOR", "Qwen3.5-397B-A17B:low")
ME_NAME = os.environ.get("TRANSCRIPT_ME_NAME", "")
# Fuseau des dates affichées (les assistants tournent en UTC : sans conversion, une réunion de 17 h
# apparaissait à 16 h dans le compte rendu)
TZ = ZoneInfo(os.environ.get("DISPLAY_TZ", "Europe/Paris"))


def local(s) -> dt.datetime:
    d = s if isinstance(s, dt.datetime) else dt.datetime.fromisoformat(s)
    return d.astimezone(TZ) if d.tzinfo else d
STALL = float(os.environ.get("LLM_STALL_SECS", "90"))  # sans aucun octet reçu pendant ce délai : relance
JUDGE_SECS = float(os.environ.get("REPORT_JUDGE_SECS", "480"))        # arbitrage, réflexion « medium »
JUDGE_RETRY_SECS = float(os.environ.get("REPORT_JUDGE_RETRY_SECS", "300"))  # nouvel essai, réflexion « low »
# contexte approximatif (caractères de transcription acceptés) par famille de modèles
CTX_CHARS = {"gpt-oss": 380_000, "llama": 380_000, "mistral": 380_000}
TOKEN_RE = re.compile(r"\{(S\d+|ME)\}")
WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre",
          "octobre", "novembre", "décembre"]


# ============================================================================ appels LLM
class LLM:
    def __init__(self):
        self.clients: dict[str, httpx.AsyncClient] = {}

    def _client(self, base: str, key: str) -> httpx.AsyncClient:
        if base not in self.clients:
            self.clients[base] = httpx.AsyncClient(
                timeout=httpx.Timeout(STALL, connect=15), headers={"Authorization": f"Bearer {key}"})
        return self.clients[base]

    @staticmethod
    def parse(spec: str) -> tuple[str, str, str, str | None]:
        """« modele[:effort] » (OVH) ou « modele[:effort]@https://base/v1#VAR_CLE ».
        effort = niveau de réflexion des modèles qui raisonnent : none, low, medium, high."""
        m = re.match(r"^([^@#:]+)(?::(none|low|medium|high))?(?:@([^#]+))?(?:#(\w+))?$", spec.strip())
        model, effort, base, var = m.group(1).strip(), m.group(2), m.group(3) or API, m.group(4)
        key = os.environ.get(var, "") if var else (TOKEN if base == API else "")
        return model, base.rstrip("/"), key, effort

    async def chat(self, spec: str, system: str, user: str, *, reasoning: str | None = "medium",
                   max_tokens: int = 24000, temperature: float = 0.2, deadline: float = 900) -> dict:
        """Appel en streaming : un modèle qui ne renvoie plus rien pendant STALL secondes est relancé
        (OVH laisse parfois une requête pendre), et `deadline` borne la durée totale."""
        model, base, key, effort = self.parse(spec)
        reasoning = effort or reasoning
        body = {"model": model, "temperature": temperature, "max_tokens": max_tokens, "stream": True,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        low = model.lower()
        if "qwen" in low or "gpt-oss" in low:
            body["reasoning_effort"] = reasoning or ("none" if "qwen" in low else "low")
        t_end = time.time() + deadline
        last = None
        for attempt in range(3):
            t = time.time()
            try:
                content, think, usage = await asyncio.wait_for(
                    self._stream(base, key, body), max(30.0, t_end - time.time()))
                if not content.strip():
                    if body.get("reasoning_effort") not in ("none", "low") and think:
                        body["reasoning_effort"] = "low"  # tout le budget est parti en réflexion
                    raise ValueError("réponse vide")
                return {"content": content.strip(), "secs": round(time.time() - t, 1), "model": model,
                        "usage": usage, "reasoning_chars": think}
            except (httpx.HTTPError, ValueError, KeyError, asyncio.TimeoutError) as e:
                last = e
                log.warning("%s (essai %d) : %s", model, attempt + 1, repr(e)[:200])
                if time.time() > t_end - 30:
                    break
                await asyncio.sleep(3 + attempt * 7)
        raise RuntimeError(f"{model} : {repr(last)[:200]}")

    async def _stream(self, base: str, key: str, body: dict) -> tuple[str, int, dict]:
        content, think, usage = [], 0, {}
        c = self._client(base, key)
        async with c.stream("POST", f"{base}/chat/completions", json=body,
                            timeout=httpx.Timeout(STALL, connect=15)) as r:
            if r.status_code >= 400:
                await r.aread()
                raise httpx.HTTPStatusError(f"HTTP {r.status_code} {r.text[:200]}", request=r.request, response=r)
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                d = json.loads(data)
                usage = d.get("usage") or usage
                for ch in d.get("choices") or []:
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        content.append(delta["content"])
                    think += len(delta.get("reasoning_content") or delta.get("reasoning") or "")
        return "".join(content), think, usage

    async def chat_json(self, spec: str, system: str, user: str, **kw) -> tuple[dict, dict]:
        res = await self.chat(spec, system, user, **kw)
        try:
            return extract_json(res["content"]), res
        except ValueError:  # JSON tronqué ou mal formé : un second essai suffit en général
            res = await self.chat(spec, system, user + "\n\nRéponds uniquement par un objet JSON valide.", **kw)
            return extract_json(res["content"]), res

    async def close(self):
        for c in self.clients.values():
            await c.aclose()


def extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        raise ValueError("pas de JSON")
    return json.loads(text[a:b + 1])


def model_name(spec: str) -> str:
    return LLM.parse(spec)[0]


# ============================================================================ préparation du texte
def fmt_t(sec: float) -> str:
    sec = max(0, int(sec or 0))
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_t(s) -> float | None:
    if isinstance(s, (int, float)):
        return float(s)
    m = re.match(r"^\s*(\d+):(\d{1,2})(?::(\d{1,2}))?\s*$", str(s or ""))
    if not m:
        return None
    a, b, c = (int(x) if x else None for x in m.groups())
    return float(a * 3600 + b * 60 + c) if c is not None else float(a * 60 + b)


def me_label(doc: dict) -> str:
    n = (doc.get("meta") or {}).get("me_name") or ME_NAME
    return (lexicon.load().canonical(n) or n) if n else "Moi"


DUP = re.compile(r"^(?P<base>.+?) \((?P<n>\d+)\)$")
ORG = re.compile(r"^(?P<name>.+?)\s*(?:[(\[](?P<org>[^()\[\]]{2,60})[)\]]|\s[-–|]\s(?P<org2>.{2,60}))\s*$")


SHARED_MIN_WORDS = int(os.environ.get("REPORT_SHARED_MIN_WORDS", "120"))


def speakers_info(doc: dict) -> dict[str, dict]:
    """id -> {name, display, org, voice (1 = voix principale, 2… = autre voix sous le même nom), source, shared}.
    Les noms affichés par la plateforme passent par le lexique (casse, noms tronqués, variantes).
    « Nom (2) » = une autre voix captée sous le même nom : fusionnée avec Nom si elle parle peu (même
    personne, découpage de la voix), sinon affichée « Nom (voix 2) » (salle partagée possible)."""
    lex = lexicon.load()
    out = {}
    for k, v in (doc.get("speakers") or {}).items():
        if k == "ME":
            continue
        label, src = v.get("label") or k, v.get("source")
        voice = 1
        m = DUP.match(label)
        if m and src != "manual":
            label, voice = m.group("base"), int(m.group("n"))
        org = None
        if src == "ui":                      # « Intervenant 4 (sans vignette) » n'est pas une organisation
            mo = ORG.match(label)
            if mo and not re.fullmatch(r"\d+", mo.group("org") or mo.group("org2") or ""):
                label, org = mo.group("name").strip(), (mo.group("org") or mo.group("org2")).strip()
            if src == "ui":
                label = lex.canonical(label) or label
        out[k] = {"name": label, "org": org, "voice": voice, "source": src, "no_tile": bool(v.get("no_tile"))}
    words: dict[str, int] = {}
    for p in paragraphs(doc):
        words[p.get("spk")] = words.get(p.get("spk"), 0) + len(str(p.get("text") or "").split())
    extra: dict[str, int] = {}
    for k, v in out.items():
        if v["voice"] > 1:
            extra[v["name"]] = extra.get(v["name"], 0) + words.get(k, 0)
    for k, v in out.items():
        v["words"] = words.get(k, 0)
        v["shared"] = extra.get(v["name"], 0) >= SHARED_MIN_WORDS
        v["display"] = f"{v['name']} (voix {v['voice']})" if v["shared"] and v["voice"] > 1 else v["name"]
    return out


def labels(doc: dict) -> dict[str, str]:
    out = {k: v["display"] for k, v in speakers_info(doc).items()}
    out["ME"] = me_label(doc)
    return out


def names(text, lab: dict[str, str]) -> str:
    """{S1} -> nom de l'intervenant (pour Markdown / Word)."""
    return TOKEN_RE.sub(lambda m: lab.get(m.group(1), m.group(1)), str(text or ""))


def paragraphs(doc: dict) -> list[dict]:
    paras = doc.get("paragraphs") or []
    if not paras:  # mise en forme indisponible : on se rabat sur le brut
        paras = [{"t": e["t"], "spk": e.get("spk"), "text": e["text"]} for e in doc.get("raw") or [] if e.get("text")]
    return paras


def speaker_table(doc: dict) -> str:
    lines = []
    used = {p.get("spk") for p in paragraphs(doc)}
    info = speakers_info(doc)
    for k, v in info.items():
        if k not in used:
            continue
        src = v["source"]
        how = {"ui": "nom affiché par la plateforme de visio", "manual": "nom confirmé par l'utilisateur",
               "voice": "nom inconnu, identifié uniquement par sa voix",
               "voiceprint": "reconnu par son empreinte vocale de référence (consentement attesté) ; nom sûr"}.get(src, "")
        if src == "voiceprint" and (doc.get("speakers") or {}).get(k, {}).get("tile"):
            how += f" ; la vignette affichée pendant ses phrases était « {doc['speakers'][k]['tile']} » (poste d'un autre)"
        if v["voice"] > 1:
            how = (f"autre voix sur la connexion de {v['name']} : même personne ou autre personne de la salle ; "
                   f"attribution incertaine, nomme la personne si la transcription l'établit")
        if v.get("no_tile") and src not in ("manual", "voiceprint"):
            seen = ", ".join(n for n, _ in Counter((doc.get("diarizer") or {}).get(k, {}).get("votes") or {}).most_common(3))
            how = (f"voix sans vignette à son nom (plusieurs noms affichés pendant ses phrases : {seen}) : personne "
                   f"sans doute sur le poste ou dans la salle d'un autre participant ; nomme-la si la transcription l'établit")
        lines.append(f"- {{{k}}} = {v['display']}" + (f", organisation affichée : {v['org']}" if v["org"] else "")
                     + (f" ({how})" if how else ""))
    lines.append(f"- {{ME}} = {me_label(doc)} : l'utilisateur pour qui tu travailles (il assiste à la réunion)")
    return "\n".join(lines)


def transcript_text(doc: dict, mark_from: float | None = None) -> str:
    """Transcription lisible par les modèles ; les diapositives lues à l'écran y sont insérées à leur heure."""
    from slides import slides_text
    lab = labels(doc)
    items = []
    for p in paragraphs(doc):
        sid = p.get("spk") or "?"
        items.append((p["t"], f"[{fmt_t(p['t'])}] {{{sid}}} {lab.get(sid, sid)} : {p['text']}"))
    items += [(t - 0.01, line) for t, line in slides_text(doc.get("slides"), fmt_t)]
    out, marked = [], False
    for t, line in sorted(items, key=lambda x: x[0]):
        if mark_from is not None and not marked and t >= mark_from:
            out.append(f"\n===== NOUVEAU DEPUIS LE DERNIER POINT ({fmt_t(mark_from)}) =====")
            marked = True
        out.append(line)
    return "\n".join(out)


def stats(doc: dict) -> list[dict]:
    """Temps de parole et nombre de mots par intervenant (calculés, pas estimés par le LLM)."""
    tot: dict[str, dict] = {}
    for e in doc.get("raw") or []:
        if not e.get("text") or not e.get("spk"):
            continue
        s = tot.setdefault(e["spk"], {"secs": 0.0, "words": 0, "turns": 0})
        s["secs"] += max(0.0, float(e.get("te") or e["t"]) - float(e["t"]))
        s["words"] += len(e["text"].split())
    for p in paragraphs(doc):
        if p.get("spk") in tot:
            tot[p["spk"]]["turns"] += 1
    total = sum(s["secs"] for s in tot.values()) or 1
    return sorted(({"spk": k, **{**v, "secs": round(v["secs"])}, "share": round(100 * v["secs"] / total)}
                   for k, v in tot.items()), key=lambda x: -x["secs"])


def meeting_date(doc: dict) -> dt.datetime | None:
    try:
        return local((doc.get("meta") or {})["started_at"])
    except (KeyError, ValueError, TypeError):
        return None


def calendar_hint(doc: dict) -> str:
    """Date de la réunion + 6 semaines de calendrier, pour convertir « vendredi », « mercredi prochain »."""
    d = meeting_date(doc)
    if not d:
        return ""
    days = []
    for i in range(0, 42):
        x = d + dt.timedelta(days=i)
        days.append(f"{WEEKDAYS[x.weekday()]} {x.day} {MONTHS[x.month - 1]}")
    return (f"La réunion a lieu le {WEEKDAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]} {d.year} à "
            f"{d:%H:%M}. Calendrier des jours suivants : " + " ; ".join(days) + ".")


def coverage_hints(doc: dict) -> list[str]:
    """Ce que le service sait de la couverture de l'enregistrement (début manquant…)."""
    meta, out = doc.get("meta") or {}, []
    late = meta.get("late_start") or {}
    if late:
        out.append("L'assistant n'a pas pu entrer seul dans la réunion : l'enregistrement n'a commencé qu'après "
                   "une entrée terminée à la main" + (f" ({late['why']})" if late.get("why") else "")
                   + ". Le début de la réunion manque probablement.")
    if meta.get("offline"):
        out.append("Transcription faite après coup, à partir de la vidéo enregistrée.")
    cal = meta.get("calendar") or {}
    try:
        start, cs = local(meta["started_at"]), local(cal["start"])
        if (start - cs).total_seconds() > 300:
            out.append(f"Créneau prévu à {cs:%H:%M} ; enregistrement commencé à {start:%H:%M} : le début manque peut-être.")
    except (KeyError, ValueError, TypeError):
        pass
    try:
        end, ce = local(meta["ended_at"]), local(cal["end"])
        if (ce - end).total_seconds() > 300:
            out.append(f"Fin prévue à {ce:%H:%M} ; enregistrement arrêté à {end:%H:%M}.")
    except (KeyError, ValueError, TypeError):
        pass
    return out


def context_block(doc: dict) -> str:
    meta = doc.get("meta") or {}
    lex = lexicon.load().llm_block()
    cov = coverage_hints(doc)
    parts = [f"Titre saisi : {meta.get('title')}" if meta.get("title") else "",
             f"Plateforme : {meta.get('site') or meta.get('platform')}" if (meta.get("site") or meta.get("platform")) else "",
             calendar_hint(doc), "Intervenants :\n" + speaker_table(doc),
             ("Les lignes « [ÉCRAN PARTAGÉ] » décrivent ce qui était affiché à l'écran à ce moment (diapositive, "
              "document) : utilise-les pour comprendre et compléter ce qui est dit (titres, chiffres, schémas), "
              "sans les attribuer à un intervenant." if doc.get("slides") else ""),
             ("Couverture de l'enregistrement (connue du service) :\n" + "\n".join(f"- {c}" for c in cov)) if cov else "",
             ("Lexique du projet (référence pour l'orthographe des noms, des sigles et des sujets ; ne cite que ce "
              "qui a été dit, le lexique ne sert qu'à comprendre et à bien écrire) :\n" + lex) if lex else ""]
    return "\n".join(p for p in parts if p)


COMMON_RULES = """Règles impératives :
- Fidélité absolue : uniquement ce qui a été dit dans la transcription. N'invente rien, ne suppose rien,
  ne complète pas avec des connaissances générales. En cas de doute, signale-le plutôt que d'affirmer.
- Désigne les intervenants UNIQUEMENT par leur identifiant entre accolades : {S1}, {S2}, {ME}… (jamais par
  leur nom). Les personnes citées qui ne parlent pas (ex. « mon contact à la DSS ») restent en toutes lettres.
- Reprends exactement les chiffres, montants, dates, noms de projets, sigles et noms propres.
- Distingue proposition, avis et décision : une décision n'existe que si elle est actée ou acceptée.
  Une tâche confiée à quelqu'un (« X envoie… », « Y prépare… ») est une ACTION, pas une décision : ne la
  recopie pas dans les décisions.
  Si une position évolue pendant la réunion, c'est la dernière qui compte (mentionne l'évolution).
- Chaque élément porte l'horodatage "t" (mm:ss ou h:mm:ss) du passage qui l'établit.
- La transcription est automatique : mots mal reconnus possibles, [inaudible] = passage perdu, et
  l'attribution des intervenants peut être imparfaite. Corrige les mots manifestement mal reconnus
  d'après le contexte, sans changer le sens.
- Dates : garde toujours la formulation entendue. Pour une expression RELATIVE seulement (« vendredi »,
  « mercredi prochain », « lundi », « la semaine prochaine »), ajoute entre parenthèses la date lue dans le
  calendrier fourni, ex. « vendredi (27 novembre) ». N'ajoute jamais rien à une date déjà explicite
  (« le 5 décembre ») et ne lui attribue pas de jour de la semaine.
- Rédige à la troisième personne (jamais « je », « mon », « nous ») ; {ME} est désigné par son
  identifiant comme les autres intervenants.
- Français professionnel, phrases claires et précises, sans jargon inutile ni formules creuses."""


# ============================================================================ contrôle des dates
_WD = "|".join(WEEKDAYS)
_MO = "janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre"
# l'écart entre le jour et la parenthèse ne doit pas contenir un autre jour : dans « d'ici vendredi, alors que X
# sera absent mardi (13 novembre) », c'est « mardi » qui porte la date (sinon « vendredi » la validait)
_REL = re.compile(rf"\b({_WD})\b((?:(?!\b(?:{_WD})\b)[^()\n]){{0,40}}?)\(\s*(?:le\s+)?(?:(?:{_WD})\s+)?(\d{{1,2}})(?:er)?\s+({_MO})(?:\s+\d{{4}})?\s*\)", re.I)
_EXPL_PAREN = re.compile(rf"\b(\d{{1,2}})(?:er)?\s+({_MO})(\s+\d{{4}})?\s*\(\s*(?:{_WD})?\s*(?:\d{{1,2}}(?:er)?\s+(?:{_MO}))?\s*\)", re.I)
_EXPL = re.compile(rf"\b({_WD})\s+(\d{{1,2}})(?:er)?\s+({_MO})(?:\s+(\d{{4}}))?", re.I)


def _month(m: str) -> int:
    m = m.lower().replace("é", "e").replace("û", "u")
    return [x.replace("é", "e").replace("û", "u") for x in MONTHS].index(m) + 1


def _resolve(day: int, month: int, ref: dt.date, year: int | None = None) -> dt.date | None:
    try:
        d = dt.date(year or ref.year, month, day)
        if not year and d < ref - dt.timedelta(days=120):  # « 15 janvier » dit en novembre = l'année suivante
            d = dt.date(ref.year + 1, month, day)
        return d
    except ValueError:
        return None


def fix_dates(obj, doc: dict, warn: list | None = None):
    """Corrige les dates calculées par le LLM (jour de semaine incohérent) et signale les incohérences
    dans ce qui a été dit (« jeudi 28 novembre » alors que le 28 tombe un samedi)."""
    start = meeting_date(doc)
    if not start:
        return obj
    ref = start.date()
    known = (obj.get("a_verifier") or []) + (obj.get("reserves") or []) if isinstance(obj, dict) else []  # déjà signalé

    def fix_rel(m):  # « vendredi (22 novembre) » : la date calculée doit être un vendredi
        wd, mid, day, mon = m.group(1).lower(), m.group(2), int(m.group(3)), _month(m.group(4))
        d = _resolve(day, mon, ref)
        # Le jour même n'est gardé que pour « ce vendredi » / « aujourd'hui » ; « d'ici vendredi »
        # dit un vendredi désigne le vendredi suivant
        today_ok = d == ref and re.search(r"\b(ce|cet|aujourd['’]hui|ce jour)\s*$", m.string[max(0, m.start() - 15):m.start()], re.I)
        if d and d.weekday() == WEEKDAYS.index(wd) and (d > ref or today_ok):
            return m.group(0)
        n = (WEEKDAYS.index(wd) - ref.weekday()) % 7 or 7
        nd = ref + dt.timedelta(days=n)
        return f"{m.group(1)}{mid}({nd.day} {MONTHS[nd.month - 1]})"

    def walk(x, t=None, fix=True):
        if isinstance(x, str):
            if fix:  # les « points à vérifier » expliquent souvent un calcul : on n'y touche pas
                x = _EXPL_PAREN.sub(lambda m: f"{m.group(1)} {m.group(2)}{m.group(3) or ''}", x)
                x = _REL.sub(fix_rel, x)
            for m in _EXPL.finditer(x):
                d = _resolve(int(m.group(2)), _month(m.group(3)), ref, int(m.group(4)) if m.group(4) else None)
                if d and d.weekday() != WEEKDAYS.index(m.group(1).lower()) and warn is not None:
                    msg = (f"« {m.group(0)} » : le {d.day} {MONTHS[d.month - 1]} {d.year} tombe un "
                           f"{WEEKDAYS[d.weekday()]}. Jour ou date à confirmer.")
                    said = f"{int(m.group(2))} {m.group(3)}".lower()
                    if not any(said in w["texte"].lower() for w in warn) and \
                            not any(said in json.dumps(v, ensure_ascii=False).lower() for v in known):
                        warn.append({"texte": msg, "t": t})
            return x
        if isinstance(x, list):
            return [walk(v, t, fix) for v in x]
        if isinstance(x, dict):
            tt = x.get("t", t)
            return {k: (walk(v, tt, fix and k not in ("a_verifier", "reserves")) if k != "t" else v) for k, v in x.items()}
        return x
    return walk(obj)


# ============================================================================ analyse en direct
LIVE_SYSTEM = """Tu es l'assistant personnel de <<ME>> pendant une réunion EN COURS. Environ toutes les
5 minutes, tu relis la transcription pour l'aider à suivre et à comprendre ce qui se dit et ce qui compte.
""" + COMMON_RULES + """
Tu reçois l'analyse précédente : garde ses formulations quand elles restent justes (stabilité de
l'affichage), corrige-les si la suite de la réunion les contredit, complète avec le nouveau.
Réponds UNIQUEMENT par ce JSON :
{"flash": {"titre": "titre court de ce qui vient de se dire",
            "resume": "ce qui s'est dit depuis le dernier point (marqueur NOUVEAU) : 2 à 5 phrases factuelles",
            "points": ["faits marquants de cette tranche : annonces, chiffres, désaccords, engagements"]},
 "en_cours": "le sujet discuté en ce moment, en une phrase",
 "synthese": "où en est la réunion depuis le début : 3 à 6 phrases",
 "a_retenir": ["3 à 8 points essentiels depuis le début, du plus important au moins important"],
 "pour_moi": [{"texte": "ce qui concerne directement {ME} : question qui lui est posée, action ou engagement qui lui revient, sujet où il est attendu, information qui le touche", "t": "mm:ss"}],
 "decisions": [{"texte": "décision actée (qui, quoi, quand)", "t": "mm:ss"}],
 "actions": [{"action": "à faire", "responsable": "{S1} ou non désigné", "echeance": "telle que dite ou non précisée", "t": "mm:ss"}],
 "questions": [{"texte": "question soulevée", "qui": "{S2}", "statut": "ouverte|répondue", "t": "mm:ss"}],
 "vigilance": [{"texte": "risque, désaccord, tension, contradiction, chiffre qui change, sujet esquivé", "t": "mm:ss"}],
 "sujets": [{"titre": "sujet abordé", "t": "mm:ss", "statut": "traité|en cours|reporté"}]}
Listes vides [] si rien. Le flash ne porte que sur la partie NOUVEAU ; tout le reste couvre la réunion entière."""

LIVE_KEYS = ("en_cours", "synthese", "a_retenir", "pour_moi", "decisions", "actions", "questions", "vigilance", "sujets")


async def live_analysis(llm: LLM, doc: dict, prev: dict | None, since: float) -> dict:
    user = (context_block(doc) + "\n\n" +
            ("Analyse précédente :\n" + json.dumps({k: prev.get(k) for k in LIVE_KEYS}, ensure_ascii=False) + "\n\n"
             if prev else "") +
            "Transcription depuis le début :\n" + transcript_text(doc, mark_from=since if prev else None))
    data, res = await llm.chat_json(LIVE_MODEL, LIVE_SYSTEM.replace("<<ME>>", me_label(doc)), user,
                                    reasoning="low", max_tokens=12000, deadline=240)
    out = {k: data.get(k) for k in LIVE_KEYS}
    for k in LIVE_KEYS:
        if k not in ("en_cours", "synthese"):
            out[k] = [x for x in (out[k] or []) if x]
    flash = data.get("flash") or {}
    out["flash"] = {"titre": flash.get("titre") or "", "resume": flash.get("resume") or "",
                    "points": [x for x in flash.get("points") or [] if x]}
    out["_secs"] = res["secs"]
    return fix_dates(out, doc)


# ============================================================================ compte rendu final
REPORT_SCHEMA = """{
 "titre": "titre explicite de la réunion",
 "objet": "objet / ordre du jour tel qu'il ressort des échanges (1 à 3 phrases)",
 "synthese": "synthèse exécutive : l'essentiel pour quelqu'un qui n'était pas là (5 à 10 phrases)",
 "a_retenir": ["3 à 8 points essentiels, du plus important au moins important"],
 "sujets": [{"titre": "sujet", "debut": "mm:ss", "fin": "mm:ss",
             "resume": "déroulé fidèle des échanges sur ce sujet : constats, arguments, chiffres, options envisagées",
             "positions": [{"qui": "{S1}", "position": "ce que cette personne a défendu, apporté ou demandé"}],
             "conclusion": "issue du sujet : ce qui est décidé, à trancher ou reporté"}],
 "decisions": [{"texte": "décision formulée précisément (quoi, qui, quand)", "porteur": "{S1} ou null",
                "statut": "actée|conditionnelle|à confirmer", "condition": "condition éventuelle ou null", "t": "mm:ss"}],
 "actions": [{"action": "verbe à l'infinitif + objet précis", "responsable": "{S2}, nom cité, ou \\"non désigné\\"",
              "echeance": "telle que dite + date calculée, ou \\"non précisée\\"", "t": "mm:ss"}],
 "questions_ouvertes": [{"texte": "question restée sans réponse ou point en suspens", "qui": "{S4} ou null",
                         "suite": "qui doit y répondre / prochaine étape, ou null", "t": "mm:ss"}],
 "risques": [{"texte": "risque, point de vigilance, désaccord persistant, dépendance", "t": "mm:ss"}],
 "chiffres": [{"valeur": "valeur exacte avec unité", "contexte": "à quoi elle se rapporte", "t": "mm:ss"}],
 "dates": [{"date": "date ou échéance citée", "objet": "ce qui est prévu", "t": "mm:ss"}],
 "pour_moi": [{"texte": "ce que {ME} doit faire, ce qu'il a promis, ce qu'on attend de lui", "t": "mm:ss"}],
 "prochaine_reunion": "date, heure et modalités si annoncées, sinon null",
 "a_verifier": [{"texte": "passage ambigu ou inaudible, chiffre ou nom incertain, contradiction non résolue", "t": "mm:ss"}],
 "couverture": {"debut_manquant": false, "fin_manquante": false,
                "commentaire": "indices, ex. « commence au milieu d'un échange sur X, sans accueil ni ordre du jour »"}
}"""

REPORT_VERSION = 2
# Format de note (version 2) : un résumé par sujet, décisions, actions, points ouverts ; intervenants justifiés et
# réserves sur la source à part ; une version « diffusion » en est dérivée (sans intervenants, réserves ni méthode).
REPORT_SCHEMA_V2 = """{
 "titre": "titre explicite, comme celui d'une note : objet de la réunion et sujets principaux",
 "creneau": "nom de la réunion et créneau prévu s'ils sont connus (agenda, titre saisi, annonce en séance : « Point de situation, 9h30 → 10h00 »), sinon null ; jamais la seule date",
 "outil": "particularités utiles de la connexion (ex. « avec une salle équipée », « une connexion pour trois personnes »), sinon null",
 "couverture": {"debut_manquant": false, "fin_manquante": false,
                "commentaire": "« Séance complète », ou les indices d'une couverture partielle"},
 "intervenants": [{"qui": "{S1}", "nom": "Prénom Nom seul, si l'identité de cette voix est établie avec certitude autrement que par son étiquette (présentation explicite) ; sinon null (le doute va dans la justification)",
                   "organisation": "organisation, ville, si connues, sinon null",
                   "role": "ce que la personne apporte à la réunion, en une ligne",
                   "justification": "comment l'identité est établie (nom affiché par la plateforme, propos « … » à mm:ss) ou pourquoi elle est incertaine"}],
 "cites": ["personnes citées qui ne parlent pas, Prénom Nom"],
 "resume": [{"titre": "intertitre court du sujet", "debut": "mm:ss", "fin": "mm:ss",
             "texte": "résumé factuel du sujet : paragraphes courts séparés par une ligne vide, listes avec « - »"}],
 "decisions": [{"texte": "décision formulée précisément (quoi, qui, quand)", "porteur": "{S1} ou null",
                "statut": "actée|conditionnelle|à confirmer", "t": "mm:ss"}],
 "decisions_commentaire": "si aucune décision : pourquoi (ex. « séance de présentation »), sinon null",
 "actions": [{"action": "verbe à l'infinitif + objet précis", "responsable": "{S2}, nom cité, ou \"non désigné\"",
              "echeance": "telle que dite + date calculée, ou \"non fixée\"", "t": "mm:ss"}],
 "points_ouverts": [{"texte": "question en suspens, risque ou désaccord persistant", "t": "mm:ss"}],
 "reserves": [{"texte": "réserve sur la source : passage ambigu ou inaudible, nom ou chiffre incertain, terme non élucidé, attribution douteuse, contradiction", "t": "mm:ss"}],
 "ecarte": ["ce qui n'est volontairement pas repris, décrit sans le détail (ex. « une remarque personnelle de {S3} »)"]
}"""

REPORT_STYLE = """- Une note, pas un procès-verbal : chaque information n'apparaît qu'une fois. Pas de synthèse qui
  répète les sujets, pas de liste « à retenir ». Le résumé suit l'ordre des sujets ; chaque sujet a un
  intertitre court et un texte factuel (présent ou passé composé), avec les chiffres, dates, noms et
  sigles exacts. Citations courtes entre guillemets seulement quand la formule porte le sens.
- Longueur : respecte la longueur cible donnée (résumé, décisions, actions et points ouverts). Condense ce
  qui est secondaire ; ne perds aucune décision, action, échéance ni chiffre important.
- Faits seulement : aucun jugement sur des personnes ou des équipes, aucune anecdote personnelle (santé,
  carrière, situation familiale), aucune remarque sur le ton. Ces passages vont dans « ecarte », décrits
  sans leur contenu.
- Vocabulaire du titre, du résumé, des décisions, des actions et des points ouverts : jamais
  « enregistrement », « transcription », « vidéo », « micro », « audio », « diarisation », « IA »,
  « Speaker », « Intervenant N » ni « probable » ; écris « sans doute ». Ces mots restent permis dans
  « intervenants », « couverture » et « reserves ».
- Intervenants : une entrée par voix ({S1}, {S2}…). Quand une voix « Nom (voix 2) » est identifiée par
  une présentation explicite (« c'est Éric qui présente »), donne son nom dans « nom » et justifie-le.
  Pour {ME} sans parole, écris « aucune parole captée » dans « role »."""

REPORT_RULES = """- Couverture : dis si l'enregistrement commence en cours de réunion (pas d'accueil ni d'ordre du jour,
  sujet déjà engagé, renvois à ce qui « vient d'être dit ») ou s'arrête avant la fin (coupure en pleine
  phrase, ni conclusion ni salutations). Tiens compte de ce que le service sait de la couverture.
- Noms : orthographe du lexique et de la liste des intervenants. Une personne citée qui n'y figure pas :
  garde le nom entendu et signale-la dans les réserves (« reserves », ou « a_verifier » selon le format). Un
  sigle ou un numéro de sujet (« le 120 ») se comprend avec le lexique ; si l'écran ou les propos
  contredisent l'état du registre, signale-le dans les réserves.
- Les lignes de l'écran partagé peuvent contenir des erreurs de lecture : un numéro de ticket ou une valeur
  qui diffère entre l'écran et l'oral (unité, chiffre) va dans les réserves.
- Rétractations : quand un intervenant revient sur ce qu'il a dit (« j'ai fait une erreur », « je me suis
  trompé », « pardon, c'est… », « non, en fait… »), seule la version corrigée figure dans le compte rendu,
  partout (résumé, sujets, chiffres, dates) ; mentionne la correction et son horodatage. L'affirmation
  retirée ne doit jamais rester présentée comme un fait.
- Attribution : l'étiquetage des voix peut se tromper. Quand l'écran affiche le porteur d'un sujet
  (« Référent », « Titulaire », « Porteur », « Pilote : X ») et que les propos qui le commentent sont
  attribués à quelqu'un d'autre, préfère le porteur affiché si le contenu le confirme, et signale
  l'incertitude dans les réserves."""

DRAFT_SYSTEM_V2 = """Tu rédiges le compte rendu d'une réunion professionnelle à partir de sa transcription
intégrale, sous forme de note de travail : exacte, complète sur l'essentiel, concise et directement exploitable.
""" + COMMON_RULES + """
""" + REPORT_STYLE + """
""" + REPORT_RULES + """
Réponds UNIQUEMENT par un JSON de cette forme (listes vides [] si rien) :
""" + REPORT_SCHEMA_V2

JUDGE_SYSTEM_V2 = """Tu es le rédacteur en chef chargé du compte rendu DÉFINITIF d'une réunion. Tu reçois sa
transcription intégrale et plusieurs projets rédigés indépendamment par des modèles différents
(projet A, B, C…). Méthode :
1. Confronte les projets point par point (décisions, actions, responsables, échéances, chiffres, dates,
   identification des intervenants, réserves).
2. Pour chaque divergence ou élément présent dans un seul projet, reviens à la transcription et tranche :
   garde la version exacte, corrige ou supprime ce qui n'est pas étayé.
3. Conserve tout fait important étayé par la transcription, même s'il n'est que dans un projet (décision,
   action, échéance, chiffre, engagement, dépendance), mais réécris l'ensemble en une note concise : fusionne
   les doublons, condense le secondaire, respecte la longueur cible.
4. Réunis toutes les réserves étayées des projets, sans doublon.
""" + COMMON_RULES + """
""" + REPORT_STYLE + """
""" + REPORT_RULES + """
Réponds UNIQUEMENT par ce JSON :
{"compte_rendu": <objet au format ci-dessous>,
 "arbitrages": [{"point": "élément discuté", "versions": "ce que disaient les projets (A : …, B : …)",
                 "retenu": "ce qui est retenu", "justification": "[mm:ss] ce que dit la transcription"}]}
Format du compte rendu :
""" + REPORT_SCHEMA_V2

DRAFT_SYSTEM = """Tu rédiges le compte rendu officiel d'une réunion professionnelle à partir de sa
transcription intégrale. Ce compte rendu fera foi : il doit être exact, complet et directement exploitable.
""" + COMMON_RULES + """
- Exhaustivité : chaque décision, action, chiffre, date, question ouverte et risque mentionné doit
  figurer dans la rubrique adéquate, même s'il n'a été évoqué qu'en passant.
- « sujets » suit l'ordre chronologique de la réunion et couvre toute sa durée.
""" + REPORT_RULES + """
Réponds UNIQUEMENT par un JSON de cette forme (listes vides [] si rien) :
""" + REPORT_SCHEMA

JUDGE_SYSTEM = """Tu es le rédacteur en chef chargé du compte rendu DÉFINITIF d'une réunion. Tu reçois sa
transcription intégrale et plusieurs projets rédigés indépendamment par des modèles différents
(projet A, B, C…). Méthode :
1. Confronte les projets point par point (décisions, actions, responsables, échéances, chiffres, dates,
   positions des intervenants, conclusions).
2. Pour chaque divergence ou élément présent dans un seul projet, reviens à la transcription et tranche :
   garde la version exacte, corrige ou supprime ce qui n'est pas étayé.
3. Fais l'UNION, pas l'intersection : tout élément étayé par la transcription qui figure dans au moins
   un projet doit être conservé (action secondaire, chiffre, date, engagement, dépendance…). Fusionne
   les doublons. Un élément n'est retiré que s'il est faux ou non étayé.
4. Ajoute ce qu'aucun projet n'a relevé mais qui est important.
5. Prends la meilleure formulation : la plus précise et la plus claire. Garde le détail des « sujets ».
""" + COMMON_RULES + """
""" + REPORT_RULES + """
Réponds UNIQUEMENT par ce JSON :
{"compte_rendu": <objet au format ci-dessous>,
 "arbitrages": [{"point": "élément discuté", "versions": "ce que disaient les projets (A : …, B : …)",
                 "retenu": "ce qui est retenu", "justification": "[mm:ss] ce que dit la transcription"}]}
Format du compte rendu :
""" + REPORT_SCHEMA

AUDIT_SYSTEM = """Tu es contrôleur qualité. Tu reçois la transcription intégrale d'une réunion et son compte
rendu (JSON). Vérifie CHAQUE élément contre la transcription :
- exactitude des faits, des personnes (qui a dit quoi, qui fait quoi), des chiffres, des dates et
  échéances, des horodatages ;
- décision présentée comme actée alors qu'elle ne l'est pas, ou l'inverse ;
- contresens, attribution à la mauvaise personne ;
- noms de personnes absents de la liste des intervenants et du lexique : à ajouter aux réserves
  (rubrique « reserves » si elle existe, sinon « a_verifier ») ;
- format en note (rubrique « resume ») : jugements sur des personnes, anecdotes personnelles ou remarques de
  ton restés dans le texte : à supprimer (et à mentionner dans « ecarte ») ;
- OUBLIS : relis la transcription ligne par ligne et repère toute action (même secondaire : fournir un
  document, vérifier auprès de quelqu'un, prévenir une équipe), tout engagement, chiffre, date, question
  ou risque qui manque au compte rendu.
""" + COMMON_RULES + """
Ne réécris pas le compte rendu : renvoie uniquement les modifications, au format JSON suivant.
"chemin" désigne un champ du compte rendu : rubrique.indice.champ (indices à partir de 0),
ex. "actions.2.echeance", "decisions.0.texte", "synthese".
{"corrections": [{"chemin": "actions.2.echeance", "valeur": "nouvelle valeur", "probleme": "ce qui était faux"}],
 "ajouts": [{"rubrique": "actions", "element": {objet au format de la rubrique}, "raison": "[mm:ss] ce qui a été dit"}],
 "suppressions": [{"chemin": "risques.3", "raison": "non étayé par la transcription"}]}
Réfléchis AVANT de répondre : le JSON ne contient que des problèmes confirmés, décrits en une phrase.
Listes vides [] si tout est juste. Ne signale que des problèmes réels, pas des préférences de style."""


def _path(cr, path: str):
    """'actions.2.echeance' -> (conteneur, clé) ou (None, None)."""
    cur, parts = cr, [p for p in str(path or "").replace("[", ".").replace("]", "").split(".") if p]
    if not parts:
        return None, None
    for p in parts[:-1]:
        try:
            cur = cur[int(p)] if isinstance(cur, list) else cur[p]
        except (KeyError, IndexError, ValueError, TypeError):
            return None, None
    last = parts[-1]
    if isinstance(cur, list):
        try:
            last = int(last)
            cur[last]
        except (ValueError, IndexError):
            return None, None
    elif not isinstance(cur, dict):
        return None, None
    return cur, last


def apply_audit(cr: dict, audit: dict, keys: tuple | None = None) -> list[dict]:
    """Applique les corrections du contrôleur ; renvoie le journal de ce qui a été modifié."""
    log_, dels = [], []
    retracted = re.compile(r"pas d'erreur|aucune erreur|est correct|était correct|je retire|ne nécessite pas|"
                           r"rien à corriger|attendez|vérifions", re.I)
    for c in audit.get("corrections") or []:
        if not isinstance(c, dict) or "valeur" not in c or retracted.search(str(c.get("probleme") or "")):
            continue
        box, key = _path(cr, c.get("chemin"))
        if box is None or (isinstance(box, dict) and key not in box and key not in ("condition", "porteur", "qui", "suite")):
            continue
        old = box[key] if (isinstance(box, list) or key in box) else None
        if old == c["valeur"]:
            continue
        box[key] = c["valeur"]
        log_.append({"element": c.get("chemin"), "probleme": c.get("probleme"),
                     "correction": c["valeur"] if isinstance(c["valeur"], str) else json.dumps(c["valeur"], ensure_ascii=False)})
    for a in audit.get("ajouts") or []:
        if isinstance(a, dict) and a.get("rubrique") in (keys or LIST_KEYS) and a.get("element"):
            cr.setdefault(a["rubrique"], []).append(a["element"])
            el = a["element"]
            txt = el if isinstance(el, str) else next((el.get(k) for k in ("texte", "action", "valeur", "titre", "date") if el.get(k)), "")
            log_.append({"element": a["rubrique"], "probleme": "oubli : " + str(a.get("raison") or ""), "correction": "ajouté : " + str(txt)})
    for d in audit.get("suppressions") or []:
        if isinstance(d, dict):
            box, key = _path(cr, d.get("chemin"))
            if isinstance(box, list) and box is not cr.get("sujets") and box is not cr.get("resume") \
                    and box is not cr.get("intervenants"):
                dels.append((id(box), box, key, d))
    for _, box, key, d in sorted(dels, key=lambda x: (x[0], -x[2])):
        removed = box.pop(key)
        txt = removed if isinstance(removed, str) else next((removed.get(k) for k in ("texte", "action", "valeur") if isinstance(removed, dict) and removed.get(k)), "")
        log_.append({"element": d.get("chemin"), "probleme": d.get("raison"), "correction": "retiré : " + str(txt)})
    return log_


LIST_KEYS = ("a_retenir", "sujets", "decisions", "actions", "questions_ouvertes", "risques", "chiffres",
             "dates", "pour_moi", "a_verifier")
LIST_KEYS_V2 = ("intervenants", "cites", "resume", "decisions", "actions", "points_ouverts", "reserves", "ecarte")
WARN_KEY = {1: "a_verifier", 2: "reserves"}     # où vont les avertissements calculés (dates, salle partagée)
# Longueur cible du corps (résumé, décisions, actions, points ouverts) : environ une page par 20 minutes
WORDS_PER_MIN = float(os.environ.get("REPORT_WORDS_PER_MIN", "25"))


def list_keys(version: int) -> tuple:
    return LIST_KEYS_V2 if version >= 2 else LIST_KEYS


def _richness(cr: dict, version: int = REPORT_VERSION) -> int:
    """Nombre d'éléments d'un projet de compte rendu (pour choisir le plus complet)."""
    return sum(len(cr.get(k) or []) for k in list_keys(version))


def _clean_cr(cr, version: int = 1) -> dict | None:
    if version >= 2:
        if not isinstance(cr, dict) or not (cr.get("resume") or cr.get("decisions") or cr.get("actions")):
            return None
        scalars = ("titre", "creneau", "outil", "decisions_commentaire")
    else:
        if not isinstance(cr, dict) or not (cr.get("synthese") or cr.get("sujets") or cr.get("decisions")):
            return None
        scalars = ("titre", "objet", "synthese", "prochaine_reunion")
    for k in list_keys(version):
        v = cr.get(k)
        cr[k] = [x for x in v if x] if isinstance(v, list) else []
    for k in scalars:
        if cr.get(k) in ("null", "None", ""):
            cr[k] = None
    return cr


def target_words(doc: dict) -> int:
    """Longueur cible du corps du compte rendu, d'après la durée de parole."""
    last = max((p["t"] for p in paragraphs(doc)), default=0) / 60
    return int(min(3500, max(350, round(last * WORDS_PER_MIN / 50) * 50)))


async def build_report(llm: LLM, doc: dict, progress=lambda step: None, version: int = REPORT_VERSION) -> dict:
    t_start = time.time()
    text = transcript_text(doc)
    if not text.strip():
        raise RuntimeError("transcription vide")
    draft_sys, judge_sys = (DRAFT_SYSTEM_V2, JUDGE_SYSTEM_V2) if version >= 2 else (DRAFT_SYSTEM, JUDGE_SYSTEM)
    head = context_block(doc)
    if version >= 2:
        head += (f"\nLongueur cible : environ {target_words(doc)} mots pour le résumé, les décisions, les actions et "
                 f"les points ouverts (intervenants et réserves en plus).")
    head += "\n\nTranscription intégrale :\n" + text
    drafters = [m for m in REPORT_MODELS
                if len(text) < next((v for k, v in CTX_CHARS.items() if k in m.lower()), 800_000)]
    steps = []

    # 1. rédactions indépendantes, en parallèle
    done = 0
    progress(f"rédaction par {len(drafters)} modèles (0/{len(drafters)})")

    async def draft(spec):
        nonlocal done
        try:
            data, res = await llm.chat_json(spec, draft_sys, head, reasoning="medium", max_tokens=32000, deadline=600)
            cr = _clean_cr(data.get("compte_rendu", data) if isinstance(data, dict) else None, version)
            if not cr:
                raise ValueError("compte rendu vide ou mal formé")
            steps.append({"etape": "rédaction", "modele": model_name(spec), "secs": res["secs"], "ok": True})
            return spec, cr
        except Exception as e:
            log.warning("projet %s : %s", spec, e)
            steps.append({"etape": "rédaction", "modele": model_name(spec), "ok": False, "erreur": str(e)[:200]})
            return spec, None
        finally:
            done += 1
            progress(f"rédaction par {len(drafters)} modèles ({done}/{len(drafters)})")

    drafts = [(s, cr) for s, cr in await asyncio.gather(*(draft(m) for m in drafters)) if cr]
    if not drafts:
        raise RuntimeError("aucun modèle n'a pu rédiger le compte rendu")

    # 2. confrontation des projets
    # À défaut d'arbitrage, on garde le projet le plus complet (et non le premier arrivé)
    final, arbitrages = max(drafts, key=lambda d: _richness(d[1], version))[1], []
    if len(drafts) > 1:
        progress(f"confrontation des {len(drafts)} projets")
        letters = "ABCDEFGH"
        body = head + "\n\n" + "\n\n".join(
            f"===== PROJET {letters[i]} =====\n" + json.dumps(cr, ensure_ascii=False) for i, (_, cr) in enumerate(drafts))
        # Réflexion « medium » d'abord ; si OVH est trop lent, nouvel essai avec une réflexion courte
        for reasoning, deadline in (("medium", JUDGE_SECS), ("low", JUDGE_RETRY_SECS)):
            try:
                data, res = await llm.chat_json(REPORT_JUDGE, judge_sys, body, reasoning=reasoning,
                                                max_tokens=32000, deadline=deadline)
                cr = _clean_cr(data.get("compte_rendu"), version)
                if cr:
                    final = cr
                    arbitrages = [a for a in data.get("arbitrages") or [] if isinstance(a, dict)]
                steps.append({"etape": "confrontation", "modele": model_name(REPORT_JUDGE), "secs": res["secs"],
                              "ok": bool(cr), "reflexion": reasoning})
                if cr:
                    break
            except Exception as e:
                log.warning("confrontation (%s) : %s", reasoning, e)
                steps.append({"etape": "confrontation", "modele": model_name(REPORT_JUDGE), "ok": False,
                              "reflexion": reasoning, "erreur": str(e)[:200]})
            progress(f"confrontation des {len(drafts)} projets (nouvel essai)")

    # 3. contrôle qualité par un autre modèle
    corrections = []
    progress("contrôle qualité")
    try:
        body = head + "\n\n===== COMPTE RENDU À CONTRÔLER =====\n" + json.dumps(final, ensure_ascii=False)
        data, res = await llm.chat_json(REPORT_AUDITOR, AUDIT_SYSTEM, body, reasoning="low", max_tokens=16000, deadline=420)
        corrections = apply_audit(final, data, list_keys(version))
        steps.append({"etape": "contrôle", "modele": model_name(REPORT_AUDITOR), "secs": res["secs"], "ok": True})
    except Exception as e:
        log.warning("contrôle : %s", e)
        steps.append({"etape": "contrôle", "modele": model_name(REPORT_AUDITOR), "ok": False, "erreur": str(e)[:200]})

    warn = []
    final = fix_dates(final, doc, warn)
    wk = WARN_KEY[2 if version >= 2 else 1]
    final[wk] = final.get(wk, []) + warn + shared_voices(doc)
    final["couverture"] = coverage(final.get("couverture"), doc)
    # horodatages hors de la réunion : on les retire plutôt que d'afficher un lien faux
    last_t = max((p["t"] for p in paragraphs(doc)), default=0) + 60
    for k in list_keys(version):
        for x in final.get(k) or []:
            if isinstance(x, dict) and "t" in x and (parse_t(x["t"]) is None or parse_t(x["t"]) > last_t):
                x["t"] = None
    return {"version": version, "generated_at": dt.datetime.now().astimezone().isoformat(),
            "secs": round(time.time() - t_start), "cr": final, "arbitrages": arbitrages,
            "corrections": corrections, "steps": steps, "stats": stats(doc),
            "drafts": {model_name(s): cr for s, cr in drafts}}


def shared_voices(doc: dict) -> list[dict]:
    """Plusieurs voix distinctes sous un même nom de plateforme, ou voix sans vignette : avertissements."""
    info = speakers_info(doc)
    out = []
    for k, v in info.items():
        if v.get("no_tile") and v["source"] not in ("manual", "voiceprint") and v["words"] >= 50:
            votes = Counter((doc.get("diarizer") or {}).get(k, {}).get("votes") or {})
            tot = sum(votes.values()) or 1
            seen = ", ".join(f"{n} ({round(100 * w / tot)} %)" for n, w in votes.most_common(3))
            first = next((p.get("t") for p in paragraphs(doc) if p.get("spk") == k), None)
            out.append({"texte": f"« {v['display']} » parle sans vignette à son nom (poste partagé, salle, ou vignette non "
                                 f"visible) ; noms affichés pendant ses phrases : {seen}. À nommer dans l'IHM (renommer la "
                                 f"voix) : le compte rendu suivra.", "t": fmt_t(first) if first is not None else None})
    first = {}
    for p in paragraphs(doc):
        first.setdefault(p.get("spk"), p.get("t"))
    by_name: dict[str, list[str]] = {}
    for k, v in info.items():
        if v["words"]:
            by_name.setdefault(v["name"], []).append(k)
    for name, ids in by_name.items():
        extra = [k for k in ids if info[k]["voice"] > 1]
        if len(ids) > 1 and extra and info[ids[0]]["shared"]:
            t = min((first[k] for k in extra if first.get(k) is not None), default=None)
            out.append({"texte": f"{len(ids)} voix distinctes ont été captées sur la connexion de {name} (« {name} (voix 2) »…) : "
                                 f"soit la même personne, soit plusieurs personnes dans la même salle. Les propos attribués "
                                 f"à {name} peuvent venir d'une autre personne présente.",
                        "t": fmt_t(t) if t is not None else None})
    return out


def coverage(cov, doc: dict) -> dict:
    """Couverture de l'enregistrement : avis des modèles + ce que le service sait (entrée tardive…)."""
    cov = cov if isinstance(cov, dict) else {}
    out = {"debut_manquant": bool(cov.get("debut_manquant")), "fin_manquante": bool(cov.get("fin_manquante")),
           "commentaire": str(cov.get("commentaire") or "").strip() or None}
    meta = doc.get("meta") or {}
    if meta.get("late_start"):
        out["debut_manquant"] = True
    known = [h for h in coverage_hints(doc) if not h.startswith("Transcription faite après coup")]
    if known:
        out["service"] = known
    return out


# ============================================================================ rendu Markdown / Word
def _dur(doc: dict) -> str:
    meta = doc.get("meta") or {}
    try:
        a, b = dt.datetime.fromisoformat(meta["started_at"]), dt.datetime.fromisoformat(meta["ended_at"])
        m = round((b - a).total_seconds() / 60)
        return f"{m // 60} h {m % 60:02d}" if m >= 60 else f"{m} min"
    except (KeyError, ValueError, TypeError):
        return ""


def _ts(x) -> str:
    t = parse_t(x.get("t")) if isinstance(x, dict) else None
    return f" [{fmt_t(t)}]" if t is not None else ""


DIFFUSION_FOOTER = os.environ.get("REPORT_DIFFUSION_FOOTER", "Document interne — diffusion restreinte")
VARIANTS = ("travail", "diffusion")
PLATFORM_FR = {"teams": "Teams", "zoom": "Zoom"}


def report_labels(report: dict, doc: dict) -> dict[str, str]:
    """Noms des intervenants pour le rendu ; format en note : une voix identifiée par une présentation explicite
    (« Nom (voix 2) » = Éric) prend ce nom, sauf si l'utilisateur l'a renommée lui-même."""
    lab = labels(doc)
    if (report.get("version") or 1) >= 2:
        manual = {k for k, v in (doc.get("speakers") or {}).items() if v.get("source") == "manual"}
        for it in (report.get("cr") or {}).get("intervenants") or []:
            sid = str((it or {}).get("qui") or "").strip("{} ")
            nom = str((it or {}).get("nom") or "").strip()
            if sid in lab and sid not in manual and nom and nom.lower() not in ("null", "none"):
                lab[sid] = nom
    return lab


def report_sections(report: dict, doc: dict, variant: str = "travail") -> list[tuple]:
    """Structure commune Markdown / Word : liste de (type, contenu)."""
    if (report.get("version") or 1) >= 2:
        return report_sections_v2(report, doc, variant)
    cr, lab = report["cr"], labels(doc)
    N = lambda s: names(s, lab)
    meta = doc.get("meta") or {}
    out = [("h1", N(cr.get("titre") or meta.get("title") or "Compte rendu de réunion"))]
    d = meeting_date(doc)
    info = []
    if d:
        info.append(f"{WEEKDAYS[d.weekday()].capitalize()} {d.day} {MONTHS[d.month - 1]} {d.year}, {d:%H:%M}"
                    + (f" ({_dur(doc)})" if _dur(doc) else ""))
    if meta.get("site") or meta.get("platform"):
        plat = meta.get("site") or meta.get("platform")
        info.append(f"Visioconférence {({'teams': 'Teams', 'zoom': 'Zoom'}).get(plat, plat)}")
    if info:
        out.append(("meta", " · ".join(info)))
    cov = cr.get("couverture") or {}
    if cov.get("debut_manquant") or cov.get("fin_manquante"):
        what = " et ".join(x for x, k in (("début manquant", "debut_manquant"), ("fin manquante", "fin_manquante")) if cov.get(k))
        why = "; ".join(x for x in [*(cov.get("service") or []), cov.get("commentaire")] if x)
        out.append(("pb", ("Couverture partielle : ", N(what + (f". {why}" if why else "")))))
    st = report.get("stats") or []
    if st:
        base = {k: v["name"] for k, v in speakers_info(doc).items()}   # « Nom (voix 2) » -> Nom
        out.append(("p", "Participants : " + ", ".join(dict.fromkeys(base.get(s["spk"]) or lab.get(s["spk"], s["spk"]) for s in st))))
    if cr.get("objet"):
        out += [("h2", "Objet"), ("p", N(cr["objet"]))]
    if cr.get("synthese"):
        out += [("h2", "Synthèse"), ("p", N(cr["synthese"]))]
    if cr.get("a_retenir"):
        out += [("h2", "À retenir"), ("ul", [N(x) for x in cr["a_retenir"]])]
    if cr.get("decisions"):
        rows = [[str(i), N(x.get("texte")) + (f" — condition : {N(x['condition'])}" if x.get("condition") else ""),
                 N(x.get("porteur") or "—"), x.get("statut") or "", _ts(x).strip(" []")]
                for i, x in enumerate(cr["decisions"], 1)]
        out += [("h2", "Décisions"), ("table", ["N°", "Décision", "Porteur", "Statut", "Réf."], rows)]
    if cr.get("actions"):
        rows = [[str(i), N(x.get("action")), N(x.get("responsable") or "non désigné"),
                 N(x.get("echeance") or "non précisée"), _ts(x).strip(" []")]
                for i, x in enumerate(cr["actions"], 1)]
        out += [("h2", "Actions"), ("table", ["N°", "Action", "Responsable", "Échéance", "Réf."], rows)]
    if cr.get("pour_moi"):
        out += [("h2", f"Pour {lab['ME']}"), ("ul", [N(x.get("texte")) + _ts(x) for x in cr["pour_moi"]])]
    if cr.get("sujets"):
        out.append(("h2", "Déroulé de la réunion"))
        for i, s in enumerate(cr["sujets"], 1):
            rng = " – ".join(fmt_t(parse_t(s[k])) for k in ("debut", "fin") if parse_t(s.get(k)) is not None)
            out.append(("h3", f"{i}. {N(s.get('titre'))}" + (f" [{rng}]" if rng else "")))
            if s.get("resume"):
                out.append(("p", N(s["resume"])))
            pos = [f"{N(p.get('qui'))} : {N(p.get('position'))}" for p in s.get("positions") or [] if isinstance(p, dict)]
            if pos:
                out.append(("ul", pos))
            if s.get("conclusion"):
                out.append(("pb", ("Conclusion : ", N(s["conclusion"]))))
    if cr.get("questions_ouvertes"):
        out += [("h2", "Questions ouvertes"), ("ul", [
            N(x.get("texte")) + (f" (soulevée par {N(x['qui'])})" if x.get("qui") else "")
            + (f" → {N(x['suite'])}" if x.get("suite") else "") + _ts(x) for x in cr["questions_ouvertes"]])]
    if cr.get("risques"):
        out += [("h2", "Risques et points de vigilance"), ("ul", [N(x.get("texte")) + _ts(x) for x in cr["risques"]])]
    if cr.get("chiffres"):
        out += [("h2", "Chiffres cités"), ("table", ["Valeur", "Contexte", "Réf."],
                                            [[N(x.get("valeur")), N(x.get("contexte")), _ts(x).strip(" []")] for x in cr["chiffres"]])]
    if cr.get("dates"):
        out += [("h2", "Dates et échéances"), ("table", ["Date", "Objet", "Réf."],
                                               [[N(x.get("date")), N(x.get("objet")), _ts(x).strip(" []")] for x in cr["dates"]])]
    if cr.get("prochaine_reunion"):
        out += [("h2", "Prochaine réunion"), ("p", N(cr["prochaine_reunion"]))]
    if cr.get("a_verifier"):
        out += [("h2", "Points à vérifier"),
                ("note", "Passages ambigus ou incertains dans la transcription automatique : à confirmer avant diffusion."),
                ("ul", [N(x.get("texte")) + _ts(x) for x in cr["a_verifier"]])]
    if st:
        agg: dict[str, list] = {}       # voix multiples sous un même nom : additionnées
        for s in st:
            a = agg.setdefault(lab.get(s["spk"], s["spk"]), [0, 0, 0])
            a[0] += s["secs"]; a[1] += s["share"]; a[2] += s["turns"]
        out += [("h2", "Temps de parole"), ("table", ["Intervenant", "Durée", "Part", "Interventions"],
                                            [[n, fmt_t(a[0]), f"{a[1]} %", str(a[2])]
                                             for n, a in sorted(agg.items(), key=lambda kv: -kv[1][0])])]
    # traçabilité
    out.append(("h2", "Méthode et contrôle"))
    steps = report.get("steps") or []
    drafted = [s["modele"] for s in steps if s["etape"] == "rédaction" and s.get("ok")]
    judge = next((s["modele"] for s in steps if s["etape"] == "confrontation" and s.get("ok")), None)
    audit = next((s["modele"] for s in steps if s["etape"] == "contrôle" and s.get("ok")), None)
    m = (f"Compte rendu généré automatiquement depuis la transcription (Whisper). Rédaction indépendante par "
         f"{len(drafted)} modèle(s) : {', '.join(drafted)}")
    if judge:
        m += f" ; confrontation et arbitrage : {judge}"
    elif len(drafted) > 1:  # arbitrage impossible (délai dépassé) : signalé au lecteur
        m += " ; confrontation non aboutie (délai dépassé) : le projet le plus complet a été retenu"
    m += f" ; contrôle qualité : {audit}" if audit else ""
    m += f". Généré le {local(report['generated_at']):%d/%m/%Y à %H:%M}. " \
         "Les références [mm:ss] renvoient à l'enregistrement et à la transcription."
    out.append(("note", m))
    if report.get("arbitrages"):
        out += [("h3", "Arbitrages entre les projets"), ("ul", [
            f"{N(a.get('point'))} — retenu : {N(a.get('retenu'))}" + (f" ({N(a['justification'])})" if a.get("justification") else "")
            for a in report["arbitrages"]])]
    if report.get("corrections"):
        out += [("h3", "Corrections du contrôle qualité"), ("ul", [
            f"{N(c.get('element'))} : {N(c.get('probleme'))} → {N(c.get('correction'))}" for c in report["corrections"]])]
    return out


def _blocks(text: str) -> list[tuple]:
    """Texte d'un sujet -> paragraphes (« p ») et listes (« ul »)."""
    out = []
    for chunk in re.split(r"\n\s*\n", str(text or "").strip()):
        lines = [l.rstrip() for l in chunk.splitlines() if l.strip()]
        if not lines:
            continue
        items, para = [], []
        for l in lines:
            m = re.match(r"^\s*[-•*]\s+(.*)$", l)
            if m:
                if para:
                    out.append(("p", " ".join(para))); para = []
                items.append(m.group(1))
            elif items and l.startswith("  "):          # suite d'un élément de liste
                items[-1] += " " + l.strip()
            else:
                if items:
                    out.append(("ul", items)); items = []
                para.append(l.strip())
        if para:
            out.append(("p", " ".join(para)))
        if items:
            out.append(("ul", items))
    return out


def _date_line(doc: dict, variant: str) -> str:
    meta, d = doc.get("meta") or {}, meeting_date(doc)
    if not d:
        return ""
    out = f"{WEEKDAYS[d.weekday()].capitalize()} {d.day} {MONTHS[d.month - 1]} {d.year}, {d:%Hh%M}"
    try:
        out += f" → {local(meta['ended_at']):%Hh%M}"
    except (KeyError, ValueError, TypeError):
        pass
    if variant == "travail" and _dur(doc):
        out += f" ({_dur(doc)} enregistrées)"
    return out


def _source_line(report: dict, doc: dict) -> str:
    meta = doc.get("meta") or {}
    srcs = {v.get("source") for v in (doc.get("speakers") or {}).values()}
    plat = PLATFORM_FR.get(meta.get("platform"), "plateforme")
    how = ", ".join(x for x, k in ((f"noms affichés par {plat}", "ui"), ("voix reconnues", "voice"),
                                   ("empreintes vocales (consentement attesté)", "voiceprint"),
                                   ("noms saisis à la main", "manual")) if k in srcs) or "voix"
    n_slides = len(doc.get("slides") or [])
    drafted = [s["modele"] for s in report.get("steps") or [] if s["etape"] == "rédaction" and s.get("ok")]
    ident = report.get("id") or meta.get("worker") or ""
    parts = [f"transcription ({how})"]
    if n_slides:
        parts.append(f"{n_slides} écran{'s' if n_slides > 1 else ''} partagé{'s' if n_slides > 1 else ''} relu{'s' if n_slides > 1 else ''}")
    if meta.get("offline"):
        parts.append("transcription faite après coup")
    parts.append(f"rédaction par {len(drafted)} modèle{'s' if len(drafted) > 1 else ''} puis confrontation et contrôle")
    return f"OmniConf{' ' + ident if ident else ''} : " + ", ".join(parts)


def report_sections_v2(report: dict, doc: dict, variant: str = "travail") -> list[tuple]:
    """Format en note : en-tête, intervenants, résumé par sujet, décisions, actions, points ouverts ;
    puis (fichier de travail) réserves sur la source et annexe technique. « diffusion » : sans les éléments
    sur la source, les intervenants ni les réserves."""
    cr, lab = report["cr"], report_labels(report, doc)
    N = lambda x: names(x, lab)
    meta, work = doc.get("meta") or {}, variant != "diffusion"
    out = [("h1", N(cr.get("titre") or meta.get("title") or "Compte rendu de réunion"))]
    rows = [("Date", _date_line(doc, variant))]
    cal = meta.get("calendar") or {}
    creneau = cr.get("creneau") or cal.get("title") or cal.get("subject")
    if creneau:
        rows.append(("Créneau", N(creneau)))
    plat = PLATFORM_FR.get(meta.get("platform")) or meta.get("site") or meta.get("platform")
    if plat or cr.get("outil"):
        rows.append(("Outil", ", ".join(x for x in (plat, N(cr.get("outil") or "")) if x)))
    if work:
        rows.append(("Source", _source_line(report, doc)))
        cov = cr.get("couverture") or {}
        partial = cov.get("debut_manquant") or cov.get("fin_manquante")
        what = " et ".join(x for x, k in (("début manquant", "debut_manquant"), ("fin manquante", "fin_manquante")) if cov.get(k))
        why = "; ".join(x for x in [*(cov.get("service") or []), N(cov.get("commentaire") or "")] if x and x != "Séance complète")
        rows.append(("Couverture", f"Partielle : {what}." + (f" {why}" if why else "") if partial
                     else (why or "Séance complète.")))
    out.append(("kv", [(k, v) for k, v in rows if v]))

    # participants
    inter = [x for x in cr.get("intervenants") or [] if isinstance(x, dict) and x.get("qui")]
    cites = [N(x) for x in cr.get("cites") or [] if x]
    if work:
        if inter:
            items = []
            for it in inter:
                who = N(it["qui"])
                org = f" ({N(it['organisation'])})" if it.get("organisation") and str(it["organisation"]).lower() != "null" else ""
                txt = f"{who}{org}" + (f" : {N(it['role'])}" if it.get("role") else "")
                if it.get("justification"):
                    txt += f". {N(it['justification'])}"
                items.append(txt.replace("..", "."))
            out += [("pb", ("Intervenants identifiés", "")), ("ul", items)]
        if cites:
            out.append(("pb", ("Cités : ", ", ".join(cites))))
    else:
        people = list(dict.fromkeys(N(it["qui"]) for it in inter if not re.match(r"^Intervenant \d+$", N(it["qui"]))))
        if people:
            out.append(("pb", ("Participants : ", ", ".join(people))))
        if cites:
            out.append(("pb", ("Cités : ", ", ".join(cites))))

    # résumé par sujet
    if cr.get("resume"):
        out.append(("h2", "Résumé"))
        for sj in cr["resume"]:
            if not isinstance(sj, dict):
                continue
            title = N(sj.get("titre") or "").strip().rstrip(".")
            blocks = _blocks(N(sj.get("texte") or ""))
            if blocks and blocks[0][0] == "p":
                out.append(("pb", (title + ". ", blocks[0][1])))
                blocks = blocks[1:]
            else:
                out.append(("pb", (title + " :", "")))
            out += blocks
    out.append(("h2", "Décisions"))
    if cr.get("decisions"):
        out.append(("ul", [N(x.get("texte")) + (f" (porteur : {N(x['porteur'])})" if x.get("porteur") else "")
                           + (f" — {x['statut']}" if x.get("statut") and x["statut"] != "actée" else "")
                           + (_ts(x) if work else "") for x in cr["decisions"] if isinstance(x, dict)]))
    else:
        out.append(("p", "Aucune" + (f" : {N(cr['decisions_commentaire'])}" if cr.get("decisions_commentaire") else ".")))
    if cr.get("actions"):
        head = ["Action", "Porteur", "Échéance"] + (["Réf."] if work else [])
        out += [("h2", "Actions"), ("table", head, [
            [N(x.get("action")), N(x.get("responsable") or "non désigné"), N(x.get("echeance") or "non fixée")]
            + ([_ts(x).strip(" []")] if work else []) for x in cr["actions"] if isinstance(x, dict)])]
    if cr.get("points_ouverts"):
        out += [("h2", "Points ouverts"), ("ul", [N(x.get("texte")) + (_ts(x) if work else "")
                                                  for x in cr["points_ouverts"] if isinstance(x, dict)])]
    if not work:
        out.append(("footer", DIFFUSION_FOOTER))
        return out

    # réserves sur la source, puis annexe technique
    res = [N(x.get("texte")) + _ts(x) for x in cr.get("reserves") or [] if isinstance(x, dict) and x.get("texte")]
    ecarte = [N(x) for x in cr.get("ecarte") or [] if x]
    if ecarte:
        res.append("Non repris : " + " ; ".join(ecarte) + ".")
    if res:
        out += [("hr",), ("h3", "Réserves sur la source"), ("ol", res)]
    st = report.get("stats") or []
    out.append(("h2", "Annexe"))
    if st:
        agg: dict[str, list] = {}
        for x in st:
            a = agg.setdefault(lab.get(x["spk"], x["spk"]), [0, 0, 0])
            a[0] += x["secs"]; a[1] += x["share"]; a[2] += x["turns"]
        out += [("h3", "Temps de parole"), ("table", ["Intervenant", "Durée", "Part", "Interventions"],
                                             [[n, fmt_t(a[0]), f"{a[1]} %", str(a[2])]
                                              for n, a in sorted(agg.items(), key=lambda kv: -kv[1][0])])]
    out += _method_sections(report, N)
    return out


def _method_sections(report: dict, N) -> list[tuple]:
    out = [("h3", "Méthode et contrôle")]
    steps = report.get("steps") or []
    drafted = [s["modele"] for s in steps if s["etape"] == "rédaction" and s.get("ok")]
    judge = next((s["modele"] for s in steps if s["etape"] == "confrontation" and s.get("ok")), None)
    audit = next((s["modele"] for s in steps if s["etape"] == "contrôle" and s.get("ok")), None)
    m = (f"Compte rendu généré automatiquement depuis la transcription (Whisper). Rédaction indépendante par "
         f"{len(drafted)} modèle(s) : {', '.join(drafted)}")
    if judge:
        m += f" ; confrontation et arbitrage : {judge}"
    elif len(drafted) > 1:
        m += " ; confrontation non aboutie (délai dépassé) : le projet le plus complet a été retenu"
    m += f" ; contrôle qualité : {audit}" if audit else ""
    m += f". Généré le {local(report['generated_at']):%d/%m/%Y à %H:%M}. " \
         "Les références [mm:ss] renvoient à l'enregistrement et à la transcription."
    out.append(("note", m))
    if report.get("arbitrages"):
        out += [("h3", "Arbitrages entre les projets"), ("ul", [
            f"{N(a.get('point'))} — retenu : {N(a.get('retenu'))}" + (f" ({N(a['justification'])})" if a.get("justification") else "")
            for a in report["arbitrages"]])]
    if report.get("corrections"):
        out += [("h3", "Corrections du contrôle qualité"), ("ul", [
            f"{N(c.get('element'))} : {N(c.get('probleme'))} → {N(c.get('correction'))}" for c in report["corrections"]])]
    return out


def _md_cell(s) -> str:
    return str(s or "").replace("|", "\\|").replace("\n", " ")


def report_markdown(report: dict, doc: dict, variant: str = "travail") -> str:
    lines = []
    for kind, *c in report_sections(report, doc, variant):
        if kind == "h1":
            lines += [f"# {c[0]}", ""]
        elif kind == "h2":
            lines += [f"## {c[0]}", ""]
        elif kind == "h3":
            lines += [f"### {c[0]}", ""]
        elif kind == "meta":
            lines += [f"*{c[0]}*", ""]
        elif kind == "note":
            lines += [f"> {c[0]}", ""]
        elif kind == "p":
            lines += [c[0], ""]
        elif kind == "pb":
            lines += [f"**{c[0][0].strip()}** {c[0][1]}".rstrip(), ""]
        elif kind == "ul":
            lines += [f"- {x}" for x in c[0]] + [""]
        elif kind == "ol":
            lines += [f"{i}. {x}" for i, x in enumerate(c[0], 1)] + [""]
        elif kind == "kv":
            lines += ["| | |", "|---|---|"] + [f"| **{k}** | {_md_cell(v)} |" for k, v in c[0]] + [""]
        elif kind == "hr":
            lines += ["---", ""]
        elif kind == "footer":
            lines += ["---", "", f"*{c[0]}*", ""]
        elif kind == "table":
            head, rows = c
            lines += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
            lines += ["| " + " | ".join(_md_cell(x) for x in r) + " |" for r in rows] + [""]
    return "\n".join(lines)


def report_docx(report: dict, doc: dict, variant: str = "travail") -> bytes:
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    d = Document()
    st = d.styles["Normal"]
    st.font.name, st.font.size = "Calibri", Pt(10.5)
    for s in d.sections:
        s.left_margin = s.right_margin = s.top_margin = s.bottom_margin = Pt(54)

    def shade(cell, color):
        tc = cell._tc.get_or_add_tcPr()
        sh = OxmlElement("w:shd")
        sh.set(qn("w:val"), "clear"), sh.set(qn("w:color"), "auto"), sh.set(qn("w:fill"), color)
        tc.append(sh)

    for kind, *c in report_sections(report, doc, variant):
        if kind == "h1":
            d.add_heading(c[0], 0)
        elif kind == "kv":
            t = d.add_table(rows=0, cols=2)
            t.style = "Table Grid"
            for k, v in c[0]:
                cells = t.add_row().cells
                cells[0].text = ""
                cells[0].paragraphs[0].add_run(k).bold = True
                shade(cells[0], "DCE6F0")
                cells[1].text = str(v or "")
            for row in t.rows:
                row.cells[0].width, row.cells[1].width = Pt(90), Pt(400)
                for cell in row.cells:
                    for p in cell.paragraphs:
                        for run in p.runs:
                            run.font.size = Pt(9.5)
            d.add_paragraph()
        elif kind == "ol":
            for x in c[0]:
                d.add_paragraph(x, style="List Number")
        elif kind == "hr":
            d.add_paragraph("―" * 30).alignment = 1
        elif kind == "footer":
            for s in d.sections:
                fp = s.footer.paragraphs[0]
                fp.text = c[0]
                fp.alignment = 1
                for run in fp.runs:
                    run.font.size = Pt(8.5)
                    run.font.color.rgb = RGBColor(0x55, 0x5F, 0x6D)
        elif kind in ("h2", "h3"):
            d.add_heading(c[0], 1 if kind == "h2" else 2)
        elif kind in ("meta", "note"):
            r = d.add_paragraph().add_run(c[0])
            r.italic = True
            r.font.color.rgb = RGBColor(0x55, 0x5F, 0x6D)
            if kind == "note":
                r.font.size = Pt(9)
        elif kind == "p":
            d.add_paragraph(c[0])
        elif kind == "pb":
            p = d.add_paragraph()
            p.add_run(c[0][0]).bold = True
            if c[0][1]:
                p.add_run(c[0][1])
        elif kind == "ul":
            for x in c[0]:
                d.add_paragraph(x, style="List Bullet")
        elif kind == "table":
            head, rows = c
            t = d.add_table(rows=1, cols=len(head))
            t.style = "Table Grid"
            t.alignment = WD_TABLE_ALIGNMENT.CENTER
            for i, h in enumerate(head):
                cell = t.rows[0].cells[i]
                cell.text = ""
                cell.paragraphs[0].add_run(h).bold = True
                shade(cell, "DCE6F0")
            for r in rows:
                cells = t.add_row().cells
                for i, x in enumerate(r):
                    cells[i].text = str(x or "")
            for row in t.rows:
                for cell in row.cells:
                    for p in cell.paragraphs:
                        for run in p.runs:
                            run.font.size = Pt(9.5)
            d.add_paragraph()
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
