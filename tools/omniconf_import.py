#!/usr/bin/env python3
"""Envoi d'enregistrements passés à OmniConf, reprenable, à un rythme qui laisse la main aux réunions en direct.

Python 3.8 ou plus, sans dépendance. Le jeton est créé dans l'IHM d'OmniConf (Réglages > Accès de Claude >
« + Jeton d'import (vidéos) ») et rangé dans la variable d'environnement WEBCONF_IMPORT_TOKEN ; l'adresse de l'IHM
dans WEBCONF_URL (ou --url). Le jeton ne permet que d'envoyer des enregistrements, au nom de son propriétaire.

    python omniconf_import.py manifeste.json [--parallel 2] [--limit 20] [--dry-run]
    python omniconf_import.py "C:\\Videos\\2026-10-06 09-36-42.mp4" …        (fichiers, sans manifeste)
    python omniconf_import.py --status [manifeste.json]                    (avancement côté OmniConf)

Manifeste : liste JSON d'objets, dans l'ordre d'envoi voulu :
    {"path": "C:\\\\Videos\\\\2026-10-06 09-36-42.mp4",     obligatoire
     "started_at": "2026-10-06T09:36:42+02:00",          début (ISO avec fuseau) ; sinon lu dans le nom du fichier
                                                         « AAAA-MM-JJ HH-MM-SS » (heure locale du poste)
     "title": "Comité de suivi",                         facultatif (sinon : nom du fichier ; le CR propose un titre)
     "platform": "teams" | "zoom" | "web",               facultatif
     "cuts": [[1680, null], [600, 660.5]],               passages privés, en secondes depuis le début du fichier :
                                                         son effacé sur [début, fin] ; fin null = fin du fichier coupée
     "notes": "Ne pas reprendre le détail médical.",     consignes pour le premier compte rendu
     "transcribe": true}                                 false : vidéo seule
Avancement noté dans <manifeste>.etat.json (ou omniconf-import.etat.json) : relancer reprend où on en était ;
un fichier à moitié envoyé reprend au morceau près. --status y ajoute la réunion créée (« assistant:fichier.mp4 »),
utile pour déposer ensuite un compte rendu final (outil MCP attach_cr).

Rythme : un fichier n'est envoyé que si moins de --parallel imports sont en cours de traitement chez OmniConf
(conversion, transcription, compte rendu). 2 en journée ménage la transcription des réunions en direct.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CHUNK = 16 * 1024 * 1024
ACTIVE = ("attente", "conversion", "transcription", "compte rendu")
NAME_DATE = re.compile(r"((?:19|20)\d{2})-(\d{2})-(\d{2})[ _T](\d{2})[-.:h](\d{2})[-.:m](\d{2})")


class Api:
    def __init__(self, url: str, token: str, cafile: str | None = None):
        self.url, self.token = url.rstrip("/"), token
        self.ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()

    def call(self, method: str, path: str, body=None, raw: bytes | None = None, timeout: float = 120):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Authorization": "Bearer " + self.token, "User-Agent": "omniconf-import/1"}
        if body is not None:
            h["Content-Type"] = "application/json"
        elif raw is not None:
            h["Content-Type"] = "application/octet-stream"
        rq = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(rq, timeout=timeout, context=self.ctx) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}").get("detail")
            except ValueError:
                detail = None
            raise ApiError(e.code, detail) from None


class ApiError(Exception):
    def __init__(self, status: int, detail):
        self.status, self.detail = status, detail
        msg = detail.get("message") if isinstance(detail, dict) else detail
        super().__init__(f"HTTP {status} : {msg or '?'}")


def start_of(e: dict, path: Path) -> str | None:
    if e.get("started_at"):
        return e["started_at"]
    m = NAME_DATE.search(path.name)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups())
    return dt.datetime(y, mo, d, h, mi, s).astimezone().isoformat()     # heure locale du poste (Paris)


def human(n: float) -> str:
    return f"{n / 1e9:.2f} Go" if n >= 1e9 else f"{n / 1e6:.0f} Mo"


def load_state(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(p: Path, st: dict):
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def active_jobs(api: Api) -> list[dict]:
    return [j for j in api.call("GET", "/api/imports") if j.get("stage") in ACTIVE]


def wait_slot(api: Api, parallel: int):
    shown = False
    while True:
        try:
            n = len(active_jobs(api))
        except (ApiError, OSError) as e:
            print(f"  (OmniConf injoignable : {e} ; nouvel essai dans 1 min)", flush=True)
            time.sleep(60)
            continue
        if n < parallel:
            return
        if not shown:
            print(f"  {n} import(s) en cours de traitement : attente d'une place (au plus {parallel})…", flush=True)
            shown = True
        time.sleep(60)


def send(api: Api, e: dict, path: Path) -> dict:
    size = path.stat().st_size
    body = {"file": path.name, "size": size, "last_modified": int(path.stat().st_mtime * 1000),
            "title": e.get("title"), "started_at": start_of(e, path), "platform": e.get("platform"),
            "transcribe": e.get("transcribe", True), "cuts": e.get("cuts"), "notes": e.get("notes")}
    j = api.call("POST", "/api/imports", body)
    w, jid = j["worker"], j["id"]
    if j["stage"] != "upload":
        return j
    off = j.get("received") or 0
    if off:
        print(f"  reprise à {human(off)} sur {human(size)}", flush=True)
    base = f"/api/imports/{w}/{jid}"
    t0, b0, last, fails = time.time(), off, -1, 0
    with open(path, "rb") as f:
        while off < size:
            f.seek(off)
            data = f.read(CHUNK)
            try:
                off = api.call("PUT", f"{base}?offset={off}", raw=data)["received"]
                fails = 0
            except ApiError as err:
                if err.status == 409 and isinstance(err.detail, dict) and err.detail.get("received") is not None:
                    off = err.detail["received"]
                    continue
                if err.status < 500 and err.status != 429:
                    raise
                fails += 1
            except OSError as err:                    # réseau coupé, délai dépassé
                fails += 1
                print(f"  coupure ({err}), nouvel essai…", flush=True)
            if fails:
                if fails > 12:
                    raise RuntimeError("envoi interrompu : trop d'échecs de suite ; relancez plus tard, il reprendra")
                time.sleep(min(60, 2 ** fails))
                try:
                    off = api.call("GET", base)["received"]
                except (ApiError, OSError):
                    pass
                continue
            pct = int(100 * off / size)
            if pct // 10 != last // 10 or off >= size:
                last = pct
                rate = (off - b0) / max(1e-3, time.time() - t0)
                print(f"  {pct:3d} %  {human(off)} / {human(size)}  {rate / 1e6:.1f} Mo/s", flush=True)
    return api.call("POST", base + "/finish")


def status(api: Api, state: dict, state_path: Path | None):
    jobs = api.call("GET", "/api/imports")
    by_id = {j["id"]: j for j in jobs}
    for p, s in state.items():
        j = by_id.get(s.get("id"))
        if j and j.get("stem"):
            s["meeting"] = f"{j['worker']}:{j['stem']}.mp4"
        if j:
            s["stage"] = j["stage"]
            if j.get("error") or j.get("note"):
                s["message"] = j.get("error") or j.get("note")
    if state_path:
        save_state(state_path, state)
    for j in jobs:
        pct = f"{j['pct']:.0f} %" if j.get("pct") is not None else ""
        print(f"{j['stage']:13} {pct:>6}  {j['worker']}:{j.get('stem') or j['file']}  {j.get('step') or j.get('error') or j.get('note') or ''}")
    counts = {}
    for j in jobs:
        counts[j["stage"]] = counts.get(j["stage"], 0) + 1
    print("total :", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "aucun import")


def main():
    ap = argparse.ArgumentParser(description="Envoi d'enregistrements passés à OmniConf (reprenable).")
    ap.add_argument("inputs", nargs="*", help="manifeste .json, ou fichiers vidéo / son")
    ap.add_argument("--url", default=os.environ.get("WEBCONF_URL"), help="adresse de l'IHM (sinon WEBCONF_URL)")
    ap.add_argument("--parallel", type=int, default=2, help="imports traités en même temps chez OmniConf (2)")
    ap.add_argument("--limit", type=int, default=0, help="nombre de fichiers à envoyer cette fois (0 : tous)")
    ap.add_argument("--status", action="store_true", help="avancement côté OmniConf, réunions créées")
    ap.add_argument("--dry-run", action="store_true", help="vérifie le manifeste sans rien envoyer")
    ap.add_argument("--cafile", help="certificat racine de l'IHM, si elle n'a pas de certificat public")
    a = ap.parse_args()
    token = os.environ.get("WEBCONF_IMPORT_TOKEN", "").strip()
    if not a.dry_run and (not token or not a.url):
        sys.exit("WEBCONF_IMPORT_TOKEN et WEBCONF_URL (ou --url) sont nécessaires")
    api = Api(a.url or "", token, a.cafile)
    manifest = next((Path(x) for x in a.inputs if x.lower().endswith(".json")), None)
    if manifest:
        entries = json.loads(manifest.read_text(encoding="utf-8-sig"))
        entries = entries.get("items", entries) if isinstance(entries, dict) else entries
        state_path = manifest.with_name(manifest.stem + ".etat.json")
    else:
        entries = [{"path": x} for x in a.inputs]
        state_path = Path("omniconf-import.etat.json")
    state = load_state(state_path)
    if a.status:
        return status(api, state, state_path)
    todo, bad = [], 0
    for e in entries:
        p = Path(e["path"])
        if not p.is_file():
            print(f"introuvable : {p}")
            bad += 1
            continue
        if not start_of(e, p):
            print(f"début inconnu (ni started_at ni date dans le nom) : {p.name}")
        if str(p) in state and state[str(p)].get("sent"):
            continue
        todo.append((e, p))
    total = sum(p.stat().st_size for _, p in todo)
    print(f"{len(todo)} fichier(s) à envoyer ({human(total)}), {len(entries) - len(todo) - bad} déjà envoyé(s), {bad} introuvable(s)")
    if a.dry_run:
        for e, p in todo[: a.limit or None]:
            print(f"  {p.name}  début {start_of(e, p)}  titre {e.get('title')!r}  coupures {e.get('cuts') or '-'}")
        return
    sent = 0
    for e, p in todo:
        if a.limit and sent >= a.limit:
            break
        wait_slot(api, a.parallel)
        print(f"[{sent + 1}/{min(len(todo), a.limit or len(todo))}] {p.name} ({human(p.stat().st_size)})", flush=True)
        try:
            j = send(api, e, p)
        except (ApiError, RuntimeError) as err:
            print(f"  ÉCHEC : {err}", flush=True)
            state[str(p)] = {**state.get(str(p), {}), "error": str(err)}
            save_state(state_path, state)
            if isinstance(err, ApiError) and err.status in (401, 403):
                sys.exit("jeton refusé : arrêt")
            continue
        state[str(p)] = {"sent": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "worker": j["worker"],
                         "id": j["id"], "stage": j["stage"]}
        save_state(state_path, state)
        print(f"  envoyé : {j['worker']}, {j['stage']}", flush=True)
        sent += 1
    print(f"terminé : {sent} fichier(s) envoyé(s) ; « --status » pour suivre le traitement")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrompu : relancez la même commande pour reprendre")
