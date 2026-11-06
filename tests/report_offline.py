"""Test hors réunion : analyse en direct (2 points d'étape) + compte rendu multi-modèles sur une
transcription enregistrée. Usage (sur un worker) :
  OVH_AI_ENDPOINTS_ACCESS_TOKEN=… python report_offline.py fichier.transcript.json sortie"""
import asyncio, json, os, sys, time
sys.path.insert(0, "/opt/webconf/worker")
import analyst

async def main(path, out):
    doc = json.load(open(path))
    doc["meta"].setdefault("me_name", "Camille Durand")
    llm = analyst.LLM()
    cut = 180.0
    part = {**doc, "paragraphs": [p for p in doc["paragraphs"] if p["t"] < cut]}
    if not os.environ.get("NO_LIVE"):
        t = time.time(); a1 = await analyst.live_analysis(llm, part, None, 0); print("live1", round(time.time() - t), "s", flush=True)
        t = time.time(); a2 = await analyst.live_analysis(llm, doc, a1, cut); print("live2", round(time.time() - t), "s", flush=True)
        json.dump({"a1": a1, "a2": a2}, open(out + ".live.json", "w"), ensure_ascii=False, indent=1)
    t = time.time()
    rep = await analyst.build_report(llm, doc, lambda s: print("  …", s, round(time.time() - t), "s", flush=True))
    json.dump(rep, open(out + ".report.json", "w"), ensure_ascii=False, indent=1)
    open(out + ".report.md", "w").write(analyst.report_markdown(rep, doc))
    open(out + ".report.docx", "wb").write(analyst.report_docx(rep, doc))
    print("rapport", rep["secs"], "s", rep["steps"])
    await llm.close()

asyncio.run(main(sys.argv[1], sys.argv[2]))
