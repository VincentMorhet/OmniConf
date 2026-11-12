"""Comptes multiples : connexion, droits sur les réunions et les assistants, partage, MCP, IHM.

Lancé contre une IHM de TEST (assistants libres) :
    BASE=https://<ihm> PW=<mot de passe admin> ADMIN=admin WA=w3 python tests/users_test.py
Crée deux comptes temporaires (t-alice, t-bob), puis les supprime ; laisse l'assistant WA libre.
"""
import asyncio
import json
import os
import sys

import httpx

BASE, PW, ADMIN, WA = os.environ["BASE"], os.environ["PW"], os.environ.get("ADMIN", "admin"), os.environ.get("WA", "w3")
ok = fails = 0


def check(cond, what):
    global ok, fails
    if cond:
        ok += 1
        print("  ok  ", what)
    else:
        fails += 1
        print("  ÉCHEC", what)


def client():
    return httpx.Client(base_url=BASE, verify=False, timeout=30)


def login(c, login, pw):
    r = c.post("/api/login", json={"login": login, "password": pw})
    return r


def mcp(tok, method, params=None):
    r = httpx.post(BASE + "/mcp", verify=False, timeout=60, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                   headers={"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"})
    return r.json()


def main():
    adm = client()
    check(login(adm, "", "mauvais").status_code == 401, "mot de passe faux refusé")
    check(login(adm, ADMIN, PW).status_code == 200, "connexion administrateur")
    me = adm.get("/api/me").json()
    check(me["user"]["admin"], "administrateur reconnu")
    for u in ("t-alice", "t-bob"):
        adm.delete(f"/api/users/{u}")
    a = adm.post("/api/users", json={"login": "t-alice", "name": "Alice Test", "role": "user"}).json()
    b = adm.post("/api/users", json={"login": "t-bob", "name": "Bob Test", "role": "user"}).json()
    check("password" in a and "hash" not in a["user"], "compte créé, mot de passe provisoire, pas d'empreinte renvoyée")
    check(adm.post("/api/users", json={"login": "t-alice", "name": "X"}).status_code == 400, "identifiant en double refusé")

    al, bo = client(), client()
    r = login(al, "t-alice", a["password"]).json()
    check(r.get("must_change") is True, "mot de passe provisoire à changer")
    check(al.post("/api/me/password", json={"current": a["password"], "new": "court"}).status_code == 400, "mot de passe trop court refusé")
    check(al.post("/api/me/password", json={"current": a["password"], "new": "alice-nouveau-123"}).status_code == 200, "mot de passe changé")
    check(al.get("/api/me").json()["logged_in"], "la session reste ouverte après le changement")
    login(bo, "t-bob", b["password"])

    # réunions : sans propriétaire = administrateurs seulement
    recs_adm = adm.get("/api/recordings").json()
    recs_al = al.get("/api/recordings").json()
    check(len(recs_adm) > 0 and len(recs_al) == 0, f"Alice ne voit aucune réunion ({len(recs_adm)} pour l'administrateur)")
    r0 = recs_adm[0]
    url = f"/api/recordings/{r0['worker']}/{r0['name']}"
    check(al.get(url + "/thumb").status_code == 403, "miniature d'une réunion non partagée : refusée")
    check(al.get(url + "/transcript").status_code in (403, 404), "transcription non partagée : refusée")
    check(al.put(url + "/access", json={"shared_with": ["t-alice"]}).status_code == 403, "Alice ne peut pas se partager une réunion")

    # partage par l'administrateur
    check(adm.put(url + "/access", json={"shared_with": ["t-alice"]}).status_code == 200, "partage avec Alice")
    recs_al = al.get("/api/recordings").json()
    check([x["name"] for x in recs_al] == [r0["name"]] and recs_al[0]["can_edit"] is False, "Alice voit la réunion partagée, en lecture")
    check(al.get(url + "/thumb").status_code in (200, 404), "miniature partagée accessible")
    check(al.patch(url, json={"title": "piraté"}).status_code == 403, "Alice ne peut pas renommer")
    check(al.delete(url).status_code == 403, "Alice ne peut pas supprimer")
    check(bo.get("/api/recordings").json() == [], "Bob ne voit rien")
    # propriétaire : Bob devient propriétaire, peut renommer ; Alice garde la lecture
    check(adm.put(url + "/access", json={"owner": "t-bob", "shared_with": ["t-alice"]}).status_code == 200, "Bob devient propriétaire")
    rb = bo.get("/api/recordings").json()
    check(len(rb) == 1 and rb[0]["can_edit"] and rb[0]["mine"], "Bob voit et peut modifier sa réunion")
    check(bo.put(url + "/access", json={"owner": "t-alice"}).status_code == 403, "seul l'administrateur change le propriétaire")

    # MCP : jeton d'Alice = ses réunions seulement
    ta = al.post("/api/mcp/tokens", json={"name": "test alice", "scope": "read"}).json()
    tm = adm.post("/api/mcp/tokens", json={"name": "test admin", "scope": "read"}).json()
    la = json.loads(mcp(ta["token"], "tools/call", {"name": "list_meetings", "arguments": {"per_page": 100}})["result"]["content"][0]["text"])
    lm = json.loads(mcp(tm["token"], "tools/call", {"name": "list_meetings", "arguments": {"per_page": 100}})["result"]["content"][0]["text"])
    ids_a = [m["id"] for m in la["meetings"]] if "meetings" in la else [m["id"] for m in la.get("items", [])]
    ids_m = [m["id"] for m in lm["meetings"]] if "meetings" in lm else [m["id"] for m in lm.get("items", [])]
    check(ids_a == [f"{r0['worker']}:{r0['name'][:-4]}"] and len(ids_m) >= len(ids_a), f"MCP : Alice voit {len(ids_a)} réunion, l'administrateur {len(ids_m)}")
    other = next((i for i in ids_m if i not in ids_a), None)
    if other:
        res = mcp(ta["token"], "tools/call", {"name": "get_draft_cr", "arguments": {"id": other}})["result"]
        check(res.get("isError"), "MCP : CR d'une réunion non partagée refusé")
    check(all(t["user"] == "t-alice" for t in al.get("/api/mcp/tokens").json()["tokens"]), "Alice ne voit que ses jetons")
    check(bo.delete(f"/api/mcp/tokens/{ta['id']}").status_code == 403, "Bob ne peut pas révoquer le jeton d'Alice")
    for c, t in ((al, ta), (adm, tm)):
        c.delete(f"/api/mcp/tokens/{t['id']}")

    # assistants : Alice prend WA ; Bob le voit occupé, sans accès
    check(al.post(f"/api/workers/{WA}/open", json={"url": "about:blank"}).status_code == 200, f"Alice ouvre une page sur {WA}")
    st_b = {x["name"]: x for x in bo.get("/api/workers").json()}[WA]
    st_m = {x["name"]: x for x in adm.get("/api/workers").json()}[WA]
    check(st_b["state"] == "taken" and "url" not in st_b, f"Bob voit {WA} occupé, sans détail")
    check(st_m.get("owner_name") == "Alice Test", "l'administrateur voit qui l'utilise")
    check(bo.get(f"/api/workers/{WA}/screenshot").status_code == 403, "Bob : écran refusé")
    check(bo.get(f"/api/workers/{WA}/transcript").status_code == 403, "Bob : transcription en direct refusée")
    check(bo.post(f"/api/workers/{WA}/leave").status_code == 403, "Bob : impossible de faire partir l'assistant")
    check(al.get(f"/api/workers/{WA}/screenshot").status_code == 200, "Alice : écran accessible")
    check(al.post(f"/api/workers/{WA}/reset").status_code == 200, f"Alice libère {WA}")
    st_b = {x["name"]: x for x in bo.get("/api/workers").json()}[WA]
    check(st_b["state"] == "idle", f"{WA} de nouveau libre pour Bob")
    check(bo.get(f"/api/workers/{WA}/transcript").status_code == 403, "Bob : dernière transcription d'Alice refusée")

    # lexique : lecture pour tous, modification par les administrateurs
    check(al.get("/api/lexicon").status_code == 200, "Alice lit le lexique")
    check(al.put("/api/lexicon", json={"text": "x"}).status_code == 403, "Alice ne modifie pas le lexique")
    check(al.get("/api/users").status_code == 403, "Alice n'a pas accès à la gestion des comptes")

    # réinitialisation : la session d'Alice est fermée
    pw = adm.post("/api/users/t-alice/password").json()["password"]
    check(not al.get("/api/me").json()["logged_in"], "réinitialisation : sessions d'Alice fermées")
    check(login(al, "t-alice", pw).json().get("must_change"), "connexion avec le nouveau mot de passe provisoire")
    adm.patch("/api/users/t-alice", json={"disabled": True})
    check(not al.get("/api/me").json()["logged_in"], "compte désactivé : déconnecté")
    check(adm.patch(f"/api/users/{ADMIN}", json={"role": "user"}).status_code == 400, "le dernier administrateur ne peut pas être rétrogradé")

    # remise en état : réunion rendue à l'administrateur, comptes de test supprimés
    adm.put(url + "/access", json={"owner": ADMIN, "shared_with": []})
    for u in ("t-alice", "t-bob"):
        check(adm.delete(f"/api/users/{u}").status_code == 200, f"compte {u} supprimé")


if __name__ == "__main__":
    main()
    print(f"\n{ok} vérifications réussies, {fails} échecs")
    sys.exit(1 if fails else 0)
