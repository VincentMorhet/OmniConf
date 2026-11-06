import httpx, os, sys, time, json
BASE = os.environ.get("BASE", "https://192.168.0.230")  # IHM à tester
c = httpx.Client(base_url=BASE, verify=False, timeout=30)
c.post("/api/login", json={"password": os.environ["PW"]}).raise_for_status()
E = "20261105-212642_Test-CR-portail-client.mp4"; D = "20261120-100000_point-avancement-portail-client.transcript.json"
print("delete e2e:", c.delete(f"/api/recordings/w1/{E}").status_code)
print("rename demo:", c.patch(f"/api/recordings/w1/{D}", json={"title": "DÉMO – réunion fictive (test du compte rendu)"}).status_code)
print("regen:", c.post(f"/api/recordings/w1/{D}/report").json())
t = time.time()
while True:
    d = c.get(f"/api/recordings/w1/{D}/report").json()
    if d["job"]["state"] != "running": break
    time.sleep(10)
print(d["job"], round(time.time() - t), "s"); print(d["report"]["steps"])
cr = d["report"]["cr"]; print({k: len(cr.get(k) or []) for k in ("decisions", "actions", "a_verifier", "sujets")})
for x in cr["decisions"]: print(" D", x.get("texte"))
for x in d["report"]["corrections"]: print(" C", x)
