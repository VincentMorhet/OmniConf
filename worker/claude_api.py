"""Claude (API d'Anthropic) pour les comptes qui ont enregistré leur propre clé dans OmniConf (Réglages).

- Compte rendu final : Claude Fable 5.2 (réflexion « high », en streaming), à la place des modèles OVH.
- « En ce moment » : Claude Haiku 5.5 (rapide) ou Claude Sonnet 5.5 (plus précis), réponse JSON contrainte par un schéma.

La clé vient du contrôle avec chaque demande qui peut en avoir besoin (réunion, compte rendu, import) : elle n'est
gardée qu'en mémoire, jamais écrite sur disque ni dans un journal. En cas d'échec (clé refusée, crédit épuisé, refus du
modèle…), l'appelant revient aux modèles OVH et l'erreur est notée.
SDK officiel : https://github.com/anthropics/anthropic-sdk-python
"""
import hashlib
import json
import logging
import time

import anthropic

log = logging.getLogger("claude")

REPORT_MODELS = {"claude-fable-5-2": "Claude Fable 5.2"}
NOW_MODELS = {"claude-haiku-5-5": "Claude Haiku 5.5", "claude-sonnet-5-5": "Claude Sonnet 5.5"}
LABELS = {**REPORT_MODELS, **NOW_MODELS}
# Bascule côté serveur si le modèle refuse (classifieurs de sécurité) : Fable 5.2 et Sonnet 5.5 ; pas Haiku 5.5
FALLBACK_BETA = "server-side-fallback-2026-07-01"
WITH_FALLBACK = {"claude-fable-5-2", "claude-sonnet-5-5"}

NOW_SCHEMA = {
    "type": "object",
    "properties": {
        "garde": {"type": "array", "items": {"type": "boolean"}},
        "chemin": {"type": "array", "items": {"type": "string"}},
        "depuis": {"type": "string"},
        "en_cours": {"type": "string"},
    },
    "required": ["garde", "chemin", "depuis", "en_cours"],
    "additionalProperties": False,
}

_clients: dict[str, anthropic.AsyncAnthropic] = {}


class ClaudeError(RuntimeError):
    pass


def label(model: str | None) -> str:
    return LABELS.get(model or "", model or "")


def _client(key: str) -> anthropic.AsyncAnthropic:
    h = hashlib.sha256(key.encode()).hexdigest()[:16]       # jamais la clé elle-même comme repère
    c = _clients.get(h)
    if c is None:
        c = _clients[h] = anthropic.AsyncAnthropic(api_key=key, max_retries=2, timeout=900.0)
    return c


def explain(e: Exception) -> str:
    """Message court en français, sans la clé ni le contenu de la réunion."""
    if isinstance(e, ClaudeError):
        return str(e)
    if isinstance(e, anthropic.AuthenticationError):
        return "clé d'API refusée par Anthropic"
    if isinstance(e, anthropic.PermissionDeniedError):
        return "cette clé n'a pas accès à ce modèle"
    if isinstance(e, anthropic.NotFoundError):
        return "modèle introuvable pour cette clé"
    if isinstance(e, anthropic.PaymentRequiredError):
        return "crédit Anthropic épuisé ou facturation à régler"
    if isinstance(e, anthropic.RateLimitError):
        return "limite de débit Anthropic atteinte"
    if isinstance(e, anthropic.BadRequestError):
        return f"requête refusée par Anthropic : {getattr(e, 'message', '')[:160]}"
    if isinstance(e, anthropic.APITimeoutError):
        return "Anthropic n'a pas répondu à temps"
    if isinstance(e, anthropic.APIConnectionError):
        return "connexion à Anthropic impossible"
    if isinstance(e, anthropic.APIStatusError):
        return f"erreur Anthropic {e.status_code}"
    return f"{type(e).__name__} : {str(e)[:160]}"


def _text(msg) -> str:
    """Texte de la réponse, après avoir vérifié pourquoi le modèle s'est arrêté (jamais le contenu d'un refus)."""
    if msg.stop_reason == "refusal":
        cat = getattr(msg.stop_details, "category", None) if msg.stop_details else None
        raise ClaudeError(f"le modèle a refusé de répondre ({cat or 'sans catégorie'})")
    if msg.stop_reason == "max_tokens":
        raise ClaudeError("réponse tronquée (longueur maximale atteinte)")
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    if not text:
        raise ClaudeError("réponse vide")
    return text


def _usage(msg) -> dict:
    u = msg.usage
    return {k: getattr(u, k, None) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                              "cache_creation_input_tokens")}


async def now_json(ai: dict, system: str, user: str) -> tuple[dict, dict]:
    """« En ce moment » : petite réponse JSON (schéma imposé), réflexion courte. Le texte système est fixe : mis en
    cache chez Anthropic (appel toutes les ~20 s), ce qui réduit le coût et le délai."""
    model = ai["now"]
    kw = dict(model=model, max_tokens=4000,
              system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
              messages=[{"role": "user", "content": user}],
              output_config={"effort": "low", "format": {"type": "json_schema", "schema": NOW_SCHEMA}})
    c, t = _client(ai["key"]), time.time()
    if model in WITH_FALLBACK:
        msg = await c.with_options(timeout=60.0).beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kw)
    else:
        msg = await c.with_options(timeout=60.0).messages.create(**kw)
    data = json.loads(_text(msg))
    return data, {"secs": round(time.time() - t, 1), "model": msg.model, "usage": _usage(msg)}


async def report_text(ai: dict, system: str, user: str) -> dict:
    """Compte rendu final : Claude Fable 5.2, réflexion « high » (toujours active sur ce modèle), en streaming
    (réponses longues). Renvoie le texte (JSON attendu dans le texte, comme pour les modèles OVH)."""
    model = ai["report"]
    c, t = _client(ai["key"]), time.time()
    async with c.beta.messages.stream(
            model=model, max_tokens=64000, betas=[FALLBACK_BETA], fallbacks="default",
            output_config={"effort": "high"}, system=system,
            messages=[{"role": "user", "content": user}]) as stream:
        msg = await stream.get_final_message()
    served = msg.model
    return {"content": _text(msg), "secs": round(time.time() - t, 1), "model": served, "usage": _usage(msg),
            "fallback": served != model}


async def check(key: str, models: list[str]) -> dict[str, str]:
    """Essai de la clé sur chaque modèle choisi : une question minuscule (coût négligeable). « ok » ou l'erreur."""
    c, out = _client(key), {}
    for m in dict.fromkeys(x for x in models if x in LABELS):
        try:
            msg = await c.with_options(timeout=120.0, max_retries=1).messages.create(
                model=m, max_tokens=512, output_config={"effort": "low"},
                messages=[{"role": "user", "content": "Réponds seulement : OK"}])
            _text(msg)
            out[m] = "ok"
        except Exception as e:
            out[m] = explain(e)
    return out
