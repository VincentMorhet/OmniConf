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

import httpx

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
    return (doc.get("meta") or {}).get("me_name") or ME_NAME or "Moi"


def labels(doc: dict) -> dict[str, str]:
    out = {k: v.get("label") or k for k, v in (doc.get("speakers") or {}).items()}
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
    for k, v in (doc.get("speakers") or {}).items():
        if k == "ME" or k not in used:
            continue
        src = v.get("source")
        how = {"ui": "nom affiché par la plateforme de visio", "manual": "nom confirmé par l'utilisateur",
               "voice": "nom inconnu, identifié uniquement par sa voix"}.get(src, "")
        lines.append(f"- {{{k}}} = {v.get('label') or k}" + (f" ({how})" if how else ""))
    lines.append(f"- {{ME}} = {me_label(doc)} : l'utilisateur pour qui tu travailles (il assiste à la réunion)")
    return "\n".join(lines)


def transcript_text(doc: dict, mark_from: float | None = None) -> str:
    lab = labels(doc)
    out, marked = [], False
    for p in paragraphs(doc):
        if mark_from is not None and not marked and p["t"] >= mark_from:
            out.append(f"\n===== NOUVEAU DEPUIS LE DERNIER POINT ({fmt_t(mark_from)}) =====")
            marked = True
        sid = p.get("spk") or "?"
        out.append(f"[{fmt_t(p['t'])}] {{{sid}}} {lab.get(sid, sid)} : {p['text']}")
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
        return dt.datetime.fromisoformat((doc.get("meta") or {})["started_at"])
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


def context_block(doc: dict) -> str:
    meta = doc.get("meta") or {}
    parts = [f"Titre saisi : {meta.get('title')}" if meta.get("title") else "",
             f"Plateforme : {meta.get('site') or meta.get('platform')}" if (meta.get("site") or meta.get("platform")) else "",
             calendar_hint(doc), "Intervenants :\n" + speaker_table(doc)]
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
_REL = re.compile(rf"\b({_WD})\b([^()\n]{{0,40}}?)\(\s*(?:le\s+)?(?:(?:{_WD})\s+)?(\d{{1,2}})(?:er)?\s+({_MO})(?:\s+\d{{4}})?\s*\)", re.I)
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
    known = obj.get("a_verifier") or [] if isinstance(obj, dict) else []  # déjà signalé par le LLM

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
            return {k: (walk(v, tt, fix and k != "a_verifier") if k != "t" else v) for k, v in x.items()}
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
 "a_verifier": [{"texte": "passage ambigu ou inaudible, chiffre ou nom incertain, contradiction non résolue", "t": "mm:ss"}]
}"""

DRAFT_SYSTEM = """Tu rédiges le compte rendu officiel d'une réunion professionnelle à partir de sa
transcription intégrale. Ce compte rendu fera foi : il doit être exact, complet et directement exploitable.
""" + COMMON_RULES + """
- Exhaustivité : chaque décision, action, chiffre, date, question ouverte et risque mentionné doit
  figurer dans la rubrique adéquate, même s'il n'a été évoqué qu'en passant.
- « sujets » suit l'ordre chronologique de la réunion et couvre toute sa durée.
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


def apply_audit(cr: dict, audit: dict) -> list[dict]:
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
        if isinstance(a, dict) and a.get("rubrique") in LIST_KEYS and a.get("element"):
            cr.setdefault(a["rubrique"], []).append(a["element"])
            el = a["element"]
            txt = el if isinstance(el, str) else next((el.get(k) for k in ("texte", "action", "valeur", "titre", "date") if el.get(k)), "")
            log_.append({"element": a["rubrique"], "probleme": "oubli : " + str(a.get("raison") or ""), "correction": "ajouté : " + str(txt)})
    for d in audit.get("suppressions") or []:
        if isinstance(d, dict):
            box, key = _path(cr, d.get("chemin"))
            if isinstance(box, list) and box is not cr.get("sujets"):
                dels.append((id(box), box, key, d))
    for _, box, key, d in sorted(dels, key=lambda x: (x[0], -x[2])):
        removed = box.pop(key)
        txt = removed if isinstance(removed, str) else next((removed.get(k) for k in ("texte", "action", "valeur") if isinstance(removed, dict) and removed.get(k)), "")
        log_.append({"element": d.get("chemin"), "probleme": d.get("raison"), "correction": "retiré : " + str(txt)})
    return log_


LIST_KEYS = ("a_retenir", "sujets", "decisions", "actions", "questions_ouvertes", "risques", "chiffres",
             "dates", "pour_moi", "a_verifier")


def _richness(cr: dict) -> int:
    """Nombre d'éléments d'un projet de compte rendu (pour choisir le plus complet)."""
    return sum(len(cr.get(k) or []) for k in LIST_KEYS)


def _clean_cr(cr) -> dict | None:
    if not isinstance(cr, dict) or not (cr.get("synthese") or cr.get("sujets") or cr.get("decisions")):
        return None
    for k in LIST_KEYS:
        v = cr.get(k)
        cr[k] = [x for x in v if x] if isinstance(v, list) else []
    for k in ("titre", "objet", "synthese", "prochaine_reunion"):
        if cr.get(k) in ("null", "None", ""):
            cr[k] = None
    return cr


async def build_report(llm: LLM, doc: dict, progress=lambda step: None) -> dict:
    t_start = time.time()
    text = transcript_text(doc)
    if not text.strip():
        raise RuntimeError("transcription vide")
    head = context_block(doc) + "\n\nTranscription intégrale :\n" + text
    drafters = [m for m in REPORT_MODELS
                if len(text) < next((v for k, v in CTX_CHARS.items() if k in m.lower()), 800_000)]
    steps = []

    # 1. rédactions indépendantes, en parallèle
    done = 0
    progress(f"rédaction par {len(drafters)} modèles (0/{len(drafters)})")

    async def draft(spec):
        nonlocal done
        try:
            data, res = await llm.chat_json(spec, DRAFT_SYSTEM, head, reasoning="medium", max_tokens=32000, deadline=600)
            cr = _clean_cr(data.get("compte_rendu", data) if isinstance(data, dict) else None)
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
    final, arbitrages = max(drafts, key=lambda d: _richness(d[1]))[1], []
    if len(drafts) > 1:
        progress(f"confrontation des {len(drafts)} projets")
        letters = "ABCDEFGH"
        body = head + "\n\n" + "\n\n".join(
            f"===== PROJET {letters[i]} =====\n" + json.dumps(cr, ensure_ascii=False) for i, (_, cr) in enumerate(drafts))
        # Réflexion « medium » d'abord ; si OVH est trop lent, nouvel essai avec une réflexion courte
        for reasoning, deadline in (("medium", JUDGE_SECS), ("low", JUDGE_RETRY_SECS)):
            try:
                data, res = await llm.chat_json(REPORT_JUDGE, JUDGE_SYSTEM, body, reasoning=reasoning,
                                                max_tokens=32000, deadline=deadline)
                cr = _clean_cr(data.get("compte_rendu"))
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
        corrections = apply_audit(final, data)
        steps.append({"etape": "contrôle", "modele": model_name(REPORT_AUDITOR), "secs": res["secs"], "ok": True})
    except Exception as e:
        log.warning("contrôle : %s", e)
        steps.append({"etape": "contrôle", "modele": model_name(REPORT_AUDITOR), "ok": False, "erreur": str(e)[:200]})

    warn = []
    final = fix_dates(final, doc, warn)
    final["a_verifier"] = final.get("a_verifier", []) + warn
    # horodatages hors de la réunion : on les retire plutôt que d'afficher un lien faux
    last_t = max((p["t"] for p in paragraphs(doc)), default=0) + 60
    for k in LIST_KEYS:
        for x in final.get(k) or []:
            if isinstance(x, dict) and "t" in x and (parse_t(x["t"]) is None or parse_t(x["t"]) > last_t):
                x["t"] = None
    return {"version": 1, "generated_at": dt.datetime.now().astimezone().isoformat(),
            "secs": round(time.time() - t_start), "cr": final, "arbitrages": arbitrages,
            "corrections": corrections, "steps": steps, "stats": stats(doc),
            "drafts": {model_name(s): cr for s, cr in drafts}}


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


def report_sections(report: dict, doc: dict) -> list[tuple]:
    """Structure commune Markdown / Word : liste de (type, contenu)."""
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
    st = report.get("stats") or []
    if st:
        out.append(("p", "Participants : " + ", ".join(lab.get(s["spk"], s["spk"]) for s in st)))
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
        out += [("h2", "Temps de parole"), ("table", ["Intervenant", "Durée", "Part", "Interventions"],
                                            [[lab.get(s["spk"], s["spk"]), fmt_t(s["secs"]), f"{s['share']} %", str(s["turns"])] for s in st])]
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
    m += f". Généré le {dt.datetime.fromisoformat(report['generated_at']):%d/%m/%Y à %H:%M}. " \
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


def report_markdown(report: dict, doc: dict) -> str:
    lines = []
    for kind, *c in report_sections(report, doc):
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
            lines += [f"**{c[0][0].strip()}** {c[0][1]}", ""]
        elif kind == "ul":
            lines += [f"- {x}" for x in c[0]] + [""]
        elif kind == "table":
            head, rows = c
            lines += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
            lines += ["| " + " | ".join(_md_cell(x) for x in r) + " |" for r in rows] + [""]
    return "\n".join(lines)


def report_docx(report: dict, doc: dict) -> bytes:
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

    for kind, *c in report_sections(report, doc):
        if kind == "h1":
            d.add_heading(c[0], 0)
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
