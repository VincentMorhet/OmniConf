"""Claude (clé d'API Anthropic de chaque compte), côté assistant : vrai SDK `anthropic` face à un faux serveur Anthropic
local (aucun appel réseau, aucune vraie clé, données fictives). Vérifie les requêtes envoyées (modèles, effort, schéma
JSON, cache du texte système, bascule en cas de refus, streaming), les erreurs (clé refusée, crédit épuisé, refus),
le retour aux modèles OVH, et la clé gardée en mémoire seulement.

    python tests/claude_unit_test.py      # dans un conteneur d'assistant (dépendances du worker + anthropic)
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="cl-"))
FAKE_KEY = "sk-ant-essai-" + "x" * 40                       # fausse clé : jamais une vraie
SEEN, MODE = [], {"now": "ok", "report": "ok"}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, d):
        b = json.dumps(d).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("request-id", "req_essai")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        SEEN.append({"path": self.path, "beta": self.headers.get("anthropic-beta"), "auth": self.headers.get("authorization"),
                     "xkey": self.headers.get("x-api-key"), "body": body})
        model = body["model"]
        kind = "report" if "fable" in model else "now"
        mode = MODE[kind]
        if mode == "401":
            return self._json(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
        if mode == "402":
            return self._json(402, {"type": "error", "error": {"type": "billing_error", "message": "credit balance too low"}})
        if kind == "now":
            text = json.dumps({"garde": [False], "chemin": ["Budget 2027", "Ventilation par lot"], "depuis": "00:40",
                               "en_cours": "{S2} présente le budget"})
        elif body["messages"][0]["content"].startswith("Réponds seulement"):
            text = "OK"
        else:
            text = json.dumps({"compte_rendu": {"titre": "Comité (essai)", "resume": [{"texte": "Le budget est validé.", "t": "00:05"}],
                                                "decisions": [{"texte": "Budget 2027 validé", "t": "00:40"}], "actions": []}})
        stop = "refusal" if mode == "refusal" else "end_turn"
        details = {"type": "refusal", "category": "cyber", "explanation": None} if stop == "refusal" else None
        msg = {"id": "msg_essai", "type": "message", "role": "assistant", "model": model, "stop_reason": stop,
               "stop_sequence": None, "stop_details": details, "content": [] if stop == "refusal" else [{"type": "text", "text": text}],
               "usage": {"input_tokens": 1200, "output_tokens": 80, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0}}
        if not body.get("stream"):
            return self._json(200, msg)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()

        def ev(name, d):
            self.wfile.write(f"event: {name}\ndata: {json.dumps(d)}\n\n".encode())
        ev("message_start", {"type": "message_start", "message": {**msg, "content": [], "stop_reason": None, "stop_details": None,
                                                                 "usage": {"input_tokens": 1200, "output_tokens": 1}}})
        if stop != "refusal":
            ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            for i in range(0, len(text), 40):
                ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[i:i + 40]}})
            ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None, "stop_details": details},
                             "usage": {"output_tokens": 80}})
        ev("message_stop", {"type": "message_stop"})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ.update(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{srv.server_address[1]}", RECORD_DIR=str(TMP / "rec"),
                  PROFILE_DIR=str(TMP / "profile"), AGENT_TOKEN="essai", WORKER_NAME="wT",
                  OVH_AI_ENDPOINTS_ACCESS_TOKEN="test", REPORT_AUTO="0")
for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import analyst  # noqa: E402
import claude_api as C  # noqa: E402

ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what, flush=True)


HAIKU = {"key": FAKE_KEY, "report": "claude-fable-5-2", "now": "claude-haiku-5-5"}
SONNET = {**HAIKU, "now": "claude-sonnet-5-5"}
LINES = [(5.0, "S1", "bonjour à tous"), (40.0, "S2", "passons maintenant au budget deux mille vingt-sept, ventilation par lot")]
LAB = {"S1": "Alice", "S2": "Bruno"}
ovh_calls = []


async def fake_chat_json(self, spec, system, user, **kw):        # modèles OVH simulés
    ovh_calls.append(spec)
    if system is analyst.NOW_SYSTEM:
        return {"chemin": ["Budget (OVH)"], "changement": 1, "depuis": "00:40", "en_cours": "{S2} parle"}, {"secs": 0.3}
    if "PROJET A" in user:
        return {"compte_rendu": {"titre": "Comité", "resume": [{"texte": "Synthèse OVH."}], "decisions": [], "actions": []}}, {"secs": 1}
    return {"compte_rendu": {"titre": "Comité", "resume": [{"texte": "Projet OVH."}], "decisions": [], "actions": []}}, {"secs": 1}

analyst.LLM.chat_json = fake_chat_json
DOC = {"meta": {"title": "Comité (essai)", "started_at": "2026-11-26T10:00:00+01:00", "owner": "essai", "platform": "teams"},
       "t0": time.time() - 600, "speakers": {"S1": {"label": "Alice Martin"}, "S2": {"label": "Bruno Petit"}},
       "paragraphs": [{"id": 1, "t": 5.0, "spk": "S1", "text": "Bonjour à tous, on commence par le budget."},
                      {"id": 2, "t": 40.0, "spk": "S2", "text": "Le budget 2027 est validé, avec la ventilation par lot."}]}


async def main():
    llm = analyst.LLM()
    # ---- « En ce moment » par Claude Haiku 5.5
    SEEN.clear()
    out = await analyst.live_now(llm, LINES, LAB, None, 30.0, ["Ordre du jour"], ai=HAIKU)
    r = SEEN[-1]
    b = r["body"]
    check(out["_model"] == "Claude Haiku 5.5" and out["chemin"] == ["Budget 2027", "Ventilation par lot"] and not out["_claude_error"],
          f"« En ce moment » par Claude Haiku 5.5 ({out['_model']}, {out['chemin']})")
    check(r["path"] == "/v1/messages" and b["model"] == "claude-haiku-5-5" and b["output_config"]["effort"] == "low"
          and b["output_config"]["format"]["type"] == "json_schema" and b["output_config"]["format"]["schema"] == C.NOW_SCHEMA,
          "requête : Haiku 5.5, effort « low », réponse JSON contrainte par un schéma")
    check(b["system"][0]["cache_control"] == {"type": "ephemeral"} and b["system"][0]["text"] == analyst.NOW_SYSTEM
          and "▶ NOUVEAU" in b["messages"][0]["content"], "texte système fixe mis en cache ; fin de transcription dans le message")
    check(not r["beta"] and "fallbacks" not in b and not any(k in b for k in ("temperature", "top_p", "top_k", "thinking")),
          "Haiku : ni bascule (non prise en charge), ni température, ni réglage de réflexion")
    check(FAKE_KEY in (r["auth"] or "") + (r["xkey"] or ""), "la clé du compte est bien celle envoyée à Anthropic")
    check(not ovh_calls, "aucun appel OVH quand Claude répond")

    out = await analyst.live_now(llm, LINES, LAB, None, 30.0, [], ai=SONNET)
    r = SEEN[-1]
    check(out["_model"] == "Claude Sonnet 5.5" and r["body"]["model"] == "claude-sonnet-5-5" and "server-side-fallback-2026-07-01" in (r["beta"] or "")
          and r["body"].get("fallbacks") == "default", "Sonnet 5.5 : bascule côté serveur activée en cas de refus")

    MODE["now"] = "401"
    out = await analyst.live_now(llm, LINES, LAB, None, 30.0, [], ai=HAIKU)
    check(out["_model"] == "Qwen3.8-27B" and out["_claude_error"] == "clé d'API refusée par Anthropic" and ovh_calls,
          f"clé refusée : modèle OVH à la place, erreur notée ({out['_claude_error']})")
    MODE["now"] = "refusal"
    out = await analyst.live_now(llm, LINES, LAB, None, 30.0, [], ai=HAIKU)
    check(out["_claude_error"] and "refusé" in out["_claude_error"] and "cyber" in out["_claude_error"], f"refus du modèle : OVH ({out['_claude_error']})")
    MODE["now"] = "ok"
    n = len(ovh_calls)
    out = await analyst.live_now(llm, LINES, LAB, None, 30.0, [], ai=None)
    check(len(ovh_calls) == n + 1 and out["_model"] == "Qwen3.8-27B", "sans clé : modèle OVH comme avant")

    # ---- compte rendu final par Claude Fable 5.2 (streaming)
    SEEN.clear()
    rep = await analyst.build_report(llm, json.loads(json.dumps(DOC)), ai=HAIKU)
    r = SEEN[-1]
    b = r["body"]
    check(rep["cr"]["decisions"][0]["texte"] == "Budget 2027 validé" and rep["steps"][0]["modele"] == "Claude Fable 5.2"
          and rep["steps"][0]["ok"] and len(rep["steps"]) == 1 and list(rep["drafts"]) == ["Claude Fable 5.2"],
          f"compte rendu rédigé par Claude Fable 5.2 seul ({[s['modele'] for s in rep['steps']]})")
    check(b["model"] == "claude-fable-5-2" and b.get("stream") is True and b["max_tokens"] == 64000
          and b["output_config"] == {"effort": "high"} and "thinking" not in b and "temperature" not in b,
          "requête : Fable 5.2 en streaming, effort « high », réflexion laissée par défaut (toujours active)")
    check("server-side-fallback-2026-07-01" in (r["beta"] or "") and b.get("fallbacks") == "default",
          "Fable 5.2 : bascule côté serveur en cas de refus")
    check("Transcription intégrale" in b["messages"][0]["content"] and b["system"] == analyst.DRAFT_SYSTEM_V2,
          "consignes du compte rendu et transcription intégrale envoyées")
    check("couverture" in rep["cr"] and isinstance(rep["cr"].get("reserves"), list) and "stats" in rep and "generated_at" in rep,
          "vérifications communes appliquées (couverture, réserves, statistiques)")

    MODE["report"] = "402"
    rep = await analyst.build_report(llm, json.loads(json.dumps(DOC)), ai=HAIKU)
    st = rep["steps"]
    check(st[0]["modele"] == "Claude Fable 5.2" and not st[0]["ok"] and "crédit" in st[0]["erreur"]
          and any(s["etape"] == "rédaction" and s["ok"] for s in st[1:]) and rep["cr"]["resume"],
          f"crédit épuisé : chaîne OVH à la place, erreur notée ({st[0]['erreur']})")
    MODE["report"] = "refusal"
    rep = await analyst.build_report(llm, json.loads(json.dumps(DOC)), ai=HAIKU)
    check(not rep["steps"][0]["ok"] and "refusé" in rep["steps"][0]["erreur"] and rep["cr"], "refus : chaîne OVH")
    MODE["report"] = "ok"
    n = len(SEEN)
    rep = await analyst.build_report(llm, json.loads(json.dumps(DOC)), ai={**HAIKU, "report": None})
    check(len(SEEN) == n and rep["steps"][0]["modele"] != "Claude Fable 5.2", "« Modèles OVH » choisi pour le compte rendu : pas d'appel à Claude")

    # ---- essai de la clé
    MODE.update(now="ok", report="ok")
    res = await C.check(FAKE_KEY, ["claude-fable-5-2", "claude-haiku-5-5", "inconnu"])
    check(res == {"claude-fable-5-2": "ok", "claude-haiku-5-5": "ok"}, f"essai de la clé : chaque modèle répond ({res})")
    MODE["now"] = "401"
    res = await C.check(FAKE_KEY, ["claude-haiku-5-5"])
    check(res == {"claude-haiku-5-5": "clé d'API refusée par Anthropic"}, "essai : clé refusée expliquée")
    MODE["now"] = "ok"

    # ---- côté assistant : clé reçue du contrôle, en mémoire seulement
    import agent as A
    await A.ai_remember(A.AiReq(owner="essai", ai={"key": FAKE_KEY, "report": "claude-fable-5-2", "now": "claude-sonnet-5-5"}))
    check(A.ai_for("essai") == {"key": FAKE_KEY, "report": "claude-fable-5-2", "now": "claude-sonnet-5-5"} and A.ai_for("autre") is None,
          "réglage Claude gardé pour ce compte seulement")
    check(FAKE_KEY not in repr(A.AiReq(owner="essai", ai={"key": FAKE_KEY})) and FAKE_KEY not in repr(A.AiCheckReq(key=FAKE_KEY)),
          "la clé n'apparaît pas dans la représentation des requêtes (journaux)")
    A.state["owner"] = "essai"
    check(A.transcriber.ai_lookup()["now"] == "claude-sonnet-5-5", "« En ce moment » de la réunion en cours : réglage du propriétaire")
    await A.ai_remember(A.AiReq(owner="essai", ai={"key": FAKE_KEY, "report": "inconnu", "now": "autre"}))
    check(A.ai_for("essai") is None, "modèles inconnus : rien gardé (modèles OVH)")
    await A.ai_remember(A.AiReq(owner="essai", ai={"key": FAKE_KEY, "report": "claude-fable-5-2", "now": None}))
    old = A.AI_TTL
    A.AI_TTL = -1
    check(A.ai_for("essai") is None, "réglage oublié au bout du délai (72 h par défaut) sans nouvel envoi")
    A.AI_TTL = old
    await A.ai_remember(A.AiReq(owner="essai", ai={"key": FAKE_KEY, "report": "claude-fable-5-2", "now": None}))
    await A.ai_remember(A.AiReq(owner="essai", ai=None))
    check(A.ai_for("essai") is None, "clé retirée par le compte : effacée de la mémoire de l'assistant")
    on_disk = [p for p in TMP.rglob("*") if p.is_file() and FAKE_KEY.encode() in p.read_bytes()]
    check(not on_disk, f"la clé n'est écrite dans aucun fichier de l'assistant ({on_disk})")
    await llm.close()

asyncio.run(main())
srv.shutdown()
print(f"\n{ok} ok, {fails} échec(s)")
sys.exit(1 if fails else 0)
