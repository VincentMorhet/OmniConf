"""Pilote l'IHM par son API (test de bout en bout) : start <url> <worker> | status <worker> | leave <worker>"""
import httpx, json, os, sys, time
BASE = os.environ.get("BASE", "https://192.168.0.230")  # IHM à tester
c = httpx.Client(base_url=BASE, verify=False, timeout=30)
c.post("/api/login", json={"password": os.environ["PW"]}).raise_for_status()
cmd, w = sys.argv[1], sys.argv[-1]
if cmd == "start":
    r = c.post("/api/meetings", json={"url": sys.argv[2], "worker": w, "platform": "web", "record": True,
                                       "transcribe": True, "duration_min": 20, "title": "Test CR portail client"})
    print(r.status_code, r.text[:200])
elif cmd == "wait":
    for _ in range(90):
        st = next(x for x in c.get("/api/workers").json() if x["name"] == w)
        if st["state"] == "in_meeting" and st.get("transcript", {}).get("active"):
            print("ready", st.get("note")); break
        time.sleep(2)
    else: print("timeout", st["state"], st.get("error"))
elif cmd == "status":
    st = next(x for x in c.get("/api/workers").json() if x["name"] == w)
    print(json.dumps({k: st.get(k) for k in ("state", "note", "error")} | {"tr": st.get("transcript")}, ensure_ascii=False))
elif cmd == "leave":
    print(c.post(f"/api/workers/{w}/leave", json={}).status_code)
elif cmd == "recs":
    for r in c.get("/api/recordings").json()[:3]:
        print(r["worker"], r["name"], r.get("report"), r.get("report_job"))
