"""Réunion lancée par un compte : l'assistant porte son nom, les fichiers lui appartiennent.

    BASE=https://<ihm de test> PW=<mot de passe admin> W=w4 python tests/users_meeting_test.py
Crée un compte temporaire, lui fait rejoindre une salle Jitsi vide 30 s (enregistrement seul), vérifie la
propriété de l'enregistrement, puis supprime l'enregistrement et le compte.
"""
import os
import sys
import time

import httpx

BASE, PW, W = os.environ["BASE"], os.environ["PW"], os.environ.get("W", "w4")
ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what)


c = lambda: httpx.Client(base_url=BASE, verify=False, timeout=60)
adm, ca, cb = c(), c(), c()
adm.post("/api/login", json={"login": os.environ.get("ADMIN", "admin"), "password": PW})
for u in ("t-carol", "t-dan"):
    adm.delete(f"/api/users/{u}")
pa = adm.post("/api/users", json={"login": "t-carol", "name": "Carol Test"}).json()["password"]
pb = adm.post("/api/users", json={"login": "t-dan", "name": "Dan Test"}).json()["password"]
ca.post("/api/login", json={"login": "t-carol", "password": pa})
cb.post("/api/login", json={"login": "t-dan", "password": pb})
ca.patch("/api/me", json={"bot_names": {"web": "Assistant de Carol"}})
check(ca.get("/api/config").json()["defaultNames"]["web"] == "Assistant de Carol", "nom de l'assistant propre au compte")

room = f"https://meet.ffmuc.net/OmniconfComptes{int(time.time())}"
r = ca.post("/api/meetings", json={"url": room, "worker": W, "record": True, "transcribe": False, "title": "Test comptes"})
check(r.status_code == 200, f"Carol lance une réunion sur {W} ({r.status_code})")
st = None
for _ in range(45):
    st = {x["name"]: x for x in ca.get("/api/workers").json()}[W]
    if st["state"] in ("in_meeting", "error"):
        break
    time.sleep(2)
check(st and st["state"] == "in_meeting" and st.get("mine"), f"assistant en réunion pour Carol ({st and st['state']})")
check(st.get("owner") == "t-carol" and st.get("me_name") == "Carol Test", "propriétaire et nom dans les CR transmis à l'assistant")
other = {x["name"]: x for x in cb.get("/api/workers").json()}[W]
check(other["state"] == "taken", "Dan voit l'assistant occupé")
check(cb.post("/api/meetings", json={"url": room, "worker": W}).status_code == 403, "Dan ne peut pas prendre cet assistant")
time.sleep(25)
ca.post(f"/api/workers/{W}/leave")
time.sleep(5)
recs = ca.get("/api/recordings").json()
mine = [x for x in recs if (x.get("meta") or {}).get("title") == "Test comptes"]
check(len(mine) == 1 and mine[0]["mine"] and mine[0]["can_edit"], "Carol voit son enregistrement, modifiable")
check(not [x for x in cb.get("/api/recordings").json() if (x.get("meta") or {}).get("title") == "Test comptes"],
      "Dan ne le voit pas")
st = {x["name"]: x for x in cb.get("/api/workers").json()}[W]
check(st["state"] == "idle", "après la réunion, l'assistant est libre pour les autres")
if mine:
    check(ca.delete(f"/api/recordings/{W}/{mine[0]['name']}").status_code == 200, "Carol supprime son enregistrement")
for u in ("t-carol", "t-dan"):
    adm.delete(f"/api/users/{u}")
print(f"\n{ok} vérifications réussies, {fails} échecs")
sys.exit(1 if fails else 0)
