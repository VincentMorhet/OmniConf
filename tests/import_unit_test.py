"""Import d'enregistrements (worker/importer.py), sans réseau ni IA : fichiers générés par ffmpeg, transcription et
compte rendu simulés (un faux offline.py écrit une transcription fictive). Rien ne touche aux vrais enregistrements :
tout se passe dans un dossier temporaire.

    python tests/import_unit_test.py          # environnement d'un assistant (ffmpeg, venv du worker)
"""
import asyncio
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "worker"))
import importer as I  # noqa: E402
from fastapi import HTTPException  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="import-test-"))
SRC = TMP / "src"
SRC.mkdir()
ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what, flush=True)


def ff(*args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


def make_media():
    m = {}
    m["mp4"] = SRC / "Point projet-20261103_140512-Enregistrement de la réunion.mp4"
    ff("-f", "lavfi", "-i", "testsrc=s=640x360:r=10:d=12", "-f", "lavfi", "-i", "sine=f=440:d=12", "-c:v", "libx264",
       "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(m["mp4"]))
    m["webm"] = SRC / "obs 2026-10-28 09-15-00.webm"
    ff("-f", "lavfi", "-i", "testsrc=s=320x240:r=10:d=8", "-f", "lavfi", "-i", "sine=f=300:d=8", "-c:v", "libvpx",
       "-b:v", "300k", "-c:a", "libopus", "-shortest", str(m["webm"]))
    m["mp3"] = SRC / "dictaphone.mp3"
    ff("-f", "lavfi", "-i", "sine=f=500:d=9", "-c:a", "libmp3lame", "-b:a", "64k", str(m["mp3"]))
    m["mute"] = SRC / "sans-son.mp4"
    ff("-f", "lavfi", "-i", "testsrc=s=320x240:r=10:d=6", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(m["mute"]))
    m["short"] = SRC / "court.m4a"
    ff("-f", "lavfi", "-i", "sine=f=500:d=2", "-c:a", "aac", str(m["short"]))
    m["junk"] = SRC / "pas-une-video.mp4"
    m["junk"].write_bytes(os.urandom(50_000))
    return m


FAKE_OFFLINE = r'''
import json, os, sys, time
from pathlib import Path
rec = Path(os.environ["RECORDINGS_DIR"]); args = sys.argv[1:]
stem = args[0][:-4]
(rec.parent / "offline-args.json").write_text(json.dumps(args))
if os.environ.get("FAKE_FAIL"):
    print("offline ERROR modèle injoignable : 503", flush=True); sys.exit(1)
tp = rec / (stem + ".transcript.json")
if tp.exists() and "--force" not in args:
    sys.exit("transcription déjà présente (--force pour la remplacer)")
meta = json.loads((rec / (stem + ".mp4.json")).read_text())
for m in range(3):
    print(f"2026-11-16 offline INFO … image {m} min, noms lus 0/0", flush=True); time.sleep(float(os.environ.get("FAKE_SLEEP", "0.2")))
for m in range(3):
    print(f"2026-11-16 offline INFO … son {m} min, en cours 0", flush=True); time.sleep(float(os.environ.get("FAKE_SLEEP", "0.2")))
tp.write_text(json.dumps({"meta": {**meta, "offline": {"why": "essai"}}, "raw": [], "paragraphs": [], "speakers": {}}))
(rec / (stem + ".md")).write_text("# essai\n")
print("2026-11-16 offline INFO terminé en 1 s : 0 lignes", flush=True)
'''


def moov_first(p: Path) -> bool:
    return I_atoms(p)[:3].index("moov") < I_atoms(p).index("mdat")


def I_atoms(p: Path) -> list[str]:
    import struct
    out, pos, size = [], 0, p.stat().st_size
    with open(p, "rb") as f:
        while pos + 8 <= size and len(out) < 6:
            f.seek(pos)
            h = f.read(16)
            n, t = struct.unpack(">I4s", h[:8])
            if n == 1:
                n = struct.unpack(">Q", h[8:16])[0]
            out.append(t.decode("latin-1"))
            if n < 8:
                break
            pos += n
    return out


def max_volume(p: Path, start: float, end: float) -> float:
    r = subprocess.run(["ffmpeg", "-nostdin", "-ss", str(start), "-to", str(end), "-i", str(p), "-map", "0:a",
                        "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    m = [l for l in r.stderr.splitlines() if "max_volume" in l]
    return float(m[-1].split("max_volume:")[1].split("dB")[0]) if m else 0.0


def streams_of(p: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(p)],
                       capture_output=True, text=True)
    return json.loads(r.stdout)


async def send(imp, j, path: Path, chunk=300_000, owner="essai"):
    data = path.read_bytes()
    off = j["received"]
    while off < len(data):
        j = imp.write(j["id"], owner, off, data[off:off + chunk])
        off = j["received"]
    return imp.finish(j["id"], owner)


async def wait_stage(imp, jid, stages=("terminé", "erreur", "annulé"), timeout=90):
    t = time.time()
    seen = []
    while time.time() - t < timeout:
        j = imp.jobs.get(jid)
        if j and (not seen or seen[-1] != j["stage"]):
            seen.append(j["stage"])
        if not j or j["stage"] in stages:
            return j, seen
        await asyncio.sleep(0.1)
    return imp.jobs.get(jid), seen


async def main():
    media = make_media()
    rec = TMP / "rec"
    fake = TMP / "fake_offline.py"
    fake.write_text(FAKE_OFFLINE)
    reports = {}

    def launch_report(stem):
        async def run():
            await asyncio.sleep(0.5)
            (rec / (stem + ".report.json")).write_text(json.dumps({"cr": {"titre": "Essai"}}))
            reports[stem].update(state="done", step=None)
        reports[stem] = {"state": "running", "step": "rédaction"}
        asyncio.create_task(run())
        return True

    def new_imp():
        imp = I.Importer(rec, "wE", launch_report=launch_report, report_state=lambda s: reports.get(s))
        imp.offline_cmd = [sys.executable, str(fake)]
        return imp

    imp = new_imp()
    imp.start()

    # --- refus avant l'envoi
    for kw, what in (({"file": "notes.txt", "size": 10}, "extension .txt refusée"),
                     ({"file": "a.mp4", "size": 0}, "fichier vide refusé"),
                     ({"file": "a.mp4", "size": I.MAX_SIZE + 1}, "fichier trop gros refusé"),
                     ({"file": "a.mp4", "size": 10, "started_at": "hier"}, "date illisible refusée")):
        try:
            imp.create("essai", None, **kw)
            check(False, what)
        except I.UserError as e:
            check(True, f"{what} ({e})")
    real = shutil.disk_usage
    I.shutil.disk_usage = lambda p: real(p)._replace(free=3 * 1024 ** 3)
    try:
        imp.create("essai", None, "gros.mp4", 2 * 1024 ** 3)
        check(False, "place insuffisante détectée")
    except I.UserError as e:
        check("place insuffisante" in str(e), f"place insuffisante détectée ({e})")
    I.shutil.disk_usage = real

    # --- MP4 Teams (H.264/AAC) : copie sans réencodage, index en tête, rangée à sa date
    p = media["mp4"]
    start = dt.datetime(2026, 11, 3, 14, 5, 12).astimezone()
    j = imp.create("essai", "Camille Essai", p.name, p.stat().st_size, 1762178000000, "Point projet", start.isoformat(), "teams")
    jid = j["id"]
    j = imp.write(jid, "essai", 0, p.read_bytes()[:100_000])
    try:
        imp.write(jid, "essai", 50_000, b"x" * 10)
        check(False, "morceau hors séquence refusé")
    except HTTPException as e:
        check(e.status_code == 409 and e.detail["received"] == 100_000, f"morceau hors séquence refusé (reçu {e.detail})")
    try:
        imp.write(jid, "autre", 100_000, b"x")
        check(False, "envoi d'un autre compte refusé")
    except HTTPException as e:
        check(e.status_code == 403, "envoi d'un autre compte refusé")
    try:
        imp.finish(jid, "essai")
        check(False, "fin refusée tant que le fichier n'est pas complet")
    except HTTPException as e:
        check(e.status_code == 409, "fin refusée tant que le fichier n'est pas complet")
    again = imp.create("essai", "Camille Essai", p.name, p.stat().st_size, 1762178000000, "Point projet (titre revu)",
                       start.isoformat(), "teams")
    check(again["id"] == jid and again["received"] == 100_000 and again["title"] == "Point projet (titre revu)",
          "même fichier choisi de nouveau : reprise à 100 000 octets, titre mis à jour")
    other = imp.create("essai", None, p.name, p.stat().st_size, 1762178999999, None, None, "teams")
    check(other["id"] != jid, "autre fichier (autre date) : nouvel envoi")
    await imp.cancel(other["id"], "essai")
    check(other["id"] not in imp.jobs and not (imp.dir / (other["id"] + ".part")).exists(), "envoi abandonné : effacé")
    check(imp.status()["uploading"] == 1 and imp.status()["busy"], "envoi en cours signalé (pas de mise à jour de l'assistant)")
    j = await send(imp, imp.jobs[jid], p)
    check(j["stage"] == "attente", "fichier complet : en attente de conversion")
    j, seen = await wait_stage(imp, jid)
    check(j["stage"] == "terminé" and "conversion" in seen and "transcription" in seen and "compte rendu" in seen,
          f"conversion, transcription, compte rendu : {seen}")
    stem = j["stem"]
    mp4 = rec / (stem + ".mp4")
    check(stem == f"{start.astimezone():%Y%m%d-%H%M%S}_Point-projet-titre-revu", f"nom du fichier : {stem}")
    meta = json.loads(Path(str(mp4) + ".json").read_text())
    check(meta["owner"] == "essai" and meta["platform"] == "teams" and meta["imported"]["file"] == p.name
          and dt.datetime.fromisoformat(meta["started_at"]) == start, f"métadonnées : {meta}")
    dur = (dt.datetime.fromisoformat(meta["ended_at"]) - start).total_seconds()
    check(11 <= dur <= 13 and abs(mp4.stat().st_mtime - dt.datetime.fromisoformat(meta["ended_at"]).timestamp()) < 2,
          f"durée {dur:.1f} s, fichier daté de la fin de la réunion")
    s = streams_of(mp4)
    codecs = sorted(x["codec_name"] for x in s["streams"])
    check(codecs == ["aac", "h264"] and moov_first(mp4), f"MP4 lisible (codecs {codecs}), index en tête ({I_atoms(mp4)[:4]})")
    check(abs(mp4.stat().st_size - p.stat().st_size) < 20_000, "copie sans réencodage (taille quasi identique)")
    args = json.loads((TMP / "offline-args.json").read_text())
    check(args[:1] == [stem + ".mp4"] and "--me" in args and "Camille Essai" in args and "--no-video" not in args,
          f"transcription lancée sur la vidéo, avec le nom de l'utilisateur : {args}")
    check((rec / (stem + ".report.json")).exists(), "compte rendu rédigé")
    check(not (imp.dir / (jid + ".part")).exists(), "fichier reçu effacé après conversion")
    check(imp.for_stem(stem) is None and imp.status()["active"] == 0, "plus rien en cours")

    # --- WebM VP8/Opus (OBS) sans transcription : réencodé
    p = media["webm"]
    j = imp.create("essai", None, p.name, p.stat().st_size, None, None, "2026-10-28T09:15:00+01:00", "web", transcribe=False)
    j = await send(imp, j, p, chunk=50_000)
    j, seen = await wait_stage(imp, j["id"])
    mp4 = rec / (j["stem"] + ".mp4")
    s = streams_of(mp4)
    check(j["stage"] == "terminé" and "transcription" not in seen and not (rec / (j["stem"] + ".transcript.json")).exists(),
          f"sans transcription demandée : vidéo seule ({seen})")
    check(sorted(x["codec_name"] for x in s["streams"]) == ["aac", "h264"] and moov_first(mp4),
          "WebM réencodé en H.264/AAC, index en tête")
    meta = json.loads(Path(str(mp4) + ".json").read_text())
    check(meta["title"] == "obs 2026-10-28 09-15-00" and meta["platform"] == "web" and "site" not in meta,
          f"titre par défaut : le nom du fichier ({meta['title']})")

    # --- transcription après coup d'une vidéo restée sans transcription
    stem2 = j["stem"]
    j = imp.transcribe_existing(stem2, "essai", "Camille Essai")
    check(imp.for_stem(stem2) is not None, "transcription demandée : visible sur la réunion")
    try:
        imp.transcribe_existing(stem2, "essai", None)
        check(False, "deuxième demande refusée")
    except HTTPException as e:
        check(e.status_code == 409, "deuxième demande refusée")
    j, seen = await wait_stage(imp, j["id"])
    check(j["stage"] == "terminé" and (rec / (stem2 + ".transcript.json")).exists() and (rec / (stem2 + ".report.json")).exists(),
          f"vidéo existante transcrite puis résumée ({seen})")
    try:
        imp.transcribe_existing(stem2, "essai", None)
        check(False, "déjà transcrite : refusé")
    except HTTPException as e:
        check(e.status_code == 409, "déjà transcrite : refusé")
    j = imp.transcribe_existing(stem2, "essai", None, force=True)
    j, seen = await wait_stage(imp, j["id"])
    args = json.loads((TMP / "offline-args.json").read_text())
    check(j["stage"] == "terminé" and "--force" in args, f"« Refaire la transcription » : refaite par-dessus l'ancienne ({seen})")

    # --- son seul (MP3) : image fixe, transcription sans lecture d'image
    p = media["mp3"]
    j = imp.create("essai", None, p.name, p.stat().st_size, None, "Dictaphone", None, None)
    j = await send(imp, j, p)
    j, _ = await wait_stage(imp, j["id"])
    mp4 = rec / (j["stem"] + ".mp4")
    s = streams_of(mp4)
    meta = json.loads(Path(str(mp4) + ".json").read_text())
    args = json.loads((TMP / "offline-args.json").read_text())
    check(j["stage"] == "terminé" and meta.get("audio_only") and sorted(x["codec_name"] for x in s["streams"]) == ["aac", "h264"],
          "son seul : MP4 avec image fixe, marqué « son seul »")
    vd = max(float(x.get("duration") or 0) for x in s["streams"])
    md = (dt.datetime.fromisoformat(meta["ended_at"]) - dt.datetime.fromisoformat(meta["started_at"])).total_seconds()
    check(vd < 11 and 8 <= md <= 10, f"son seul : durée juste (pistes {vd:.1f} s, réunion {md:.0f} s pour 9 s de son)")
    check("--no-video" in args and "--no-slides" in args, f"transcription d'un son : sans image ni diapositives ({args})")
    started = dt.datetime.fromisoformat(meta["started_at"])
    check(abs((dt.datetime.now().astimezone() - started).total_seconds() - 9) < 60, "sans date donnée : maintenant moins la durée")

    # --- passages privés : son effacé sur une plage, fin coupée ; consignes pour le compte rendu
    for bad in ([[5, 3]], [[-1, 2]], [["a", 2]], [[1, 2]] * 51):
        try:
            imp.create("essai", None, "c.mp4", 10, cuts=bad)
            check(False, f"plage refusée : {bad[:2]}")
        except I.UserError as e:
            check(True, f"plage refusée : {bad[:1]} ({e})")
    p = media["mp4"]
    j = imp.create("essai", None, "prive.mp4", p.stat().st_size, None, "Privé", None, "teams",
                   cuts=[[9, None], [3, 5]], notes="Ne pas reprendre le détail médical.")
    j = await send(imp, j, p)
    j, _ = await wait_stage(imp, j["id"])
    mp4 = rec / (j["stem"] + ".mp4")
    meta = json.loads(Path(str(mp4) + ".json").read_text())
    dur = float(streams_of(mp4)["format"]["duration"])
    check(j["stage"] == "terminé" and 8.5 <= dur <= 9.6, f"fin coupée à 9 s (vidéo de {dur:.1f} s sur 12)")
    check(j["stem"].endswith("_Prive"), f"accents retirés du nom de fichier ({j['stem']})")
    check(max_volume(mp4, 3.3, 4.7) < -80 and max_volume(mp4, 1, 2.5) > -30 and max_volume(mp4, 6, 8) > -30,
          f"son effacé de 3 à 5 s, gardé ailleurs ({max_volume(mp4, 3.3, 4.7):.0f} dB / {max_volume(mp4, 1, 2.5):.0f} dB)")
    check(meta.get("cuts") == [{"from": 3.0, "to": 5.0}, {"from": 9.0, "to": None, "original_duration": 12.0}]
          and meta.get("cr_notes") == "Ne pas reprendre le détail médical.", f"coupures et consignes notées ({meta.get('cuts')})")
    md = (dt.datetime.fromisoformat(meta["ended_at"]) - dt.datetime.fromisoformat(meta["started_at"])).total_seconds()
    check(8 <= md <= 10, f"durée de la réunion après coupure : {md:.0f} s")
    j = imp.create("essai", None, "tout.mp4", p.stat().st_size, None, None, None, None, cuts=[[2, None]])
    j = await send(imp, j, p)
    j, _ = await wait_stage(imp, j["id"])
    check(j["stage"] == "erreur" and "presque rien" in j["error"], f"coupure qui ne laisse presque rien : refusée ({j['error']})")

    # --- refus après réception
    for key, want in (("mute", "aucune piste son"), ("short", "trop court"), ("junk", "illisible")):
        p = media[key]
        j = imp.create("essai", None, p.name, p.stat().st_size, None, None, None, None)
        j = await send(imp, j, p)
        j, _ = await wait_stage(imp, j["id"])
        check(j["stage"] == "erreur" and want in (j["error"] or "") and ".imports" not in j["error"], f"{p.name} : {j['error']}")
    check((imp.dir / (j["id"] + ".part")).exists(), "fichier en erreur gardé (pour « Réessayer »)")

    # --- transcription en échec, puis « Réessayer »
    p = media["mp4"]
    os.environ["FAKE_FAIL"] = "1"
    j = imp.create("essai", None, "echec.mp4", p.stat().st_size, None, "Échec", None, "zoom")
    j = await send(imp, j, p)
    j, _ = await wait_stage(imp, j["id"])
    check(j["stage"] == "erreur" and "modèle injoignable" in j["error"] and (rec / (j["stem"] + ".mp4")).exists(),
          f"transcription en échec : erreur montrée, vidéo gardée ({j['error']})")
    del os.environ["FAKE_FAIL"]
    imp.retry(j["id"], "essai")
    j, seen = await wait_stage(imp, j["id"])
    args = json.loads((TMP / "offline-args.json").read_text())
    check(j["stage"] == "terminé" and "conversion" not in seen and "--force" in args,
          f"« Réessayer » : transcription reprise sans reconvertir ({seen})")

    # --- annulation pendant la transcription : vidéo gardée, transcription partielle retirée
    os.environ["FAKE_SLEEP"] = "3"
    j = imp.create("essai", None, "annule.mp4", p.stat().st_size, None, "Annulé", None, None)
    j = await send(imp, j, p)
    j, _ = await wait_stage(imp, j["id"], ("transcription",))
    await asyncio.sleep(0.5)
    await imp.cancel(j["id"], "essai")
    check(j["id"] not in imp.jobs and (rec / (j["stem"] + ".mp4")).exists()
          and not (rec / (j["stem"] + ".transcript.json")).exists() and imp.current is None,
          "annulé pendant la transcription : vidéo gardée, sans transcription")
    os.environ["FAKE_SLEEP"] = "0.2"

    # --- redémarrage de l'assistant pendant la transcription : reprise
    os.environ["FAKE_SLEEP"] = "2"
    j = imp.create("essai", None, "reprise.mp4", p.stat().st_size, None, "Reprise", None, None)
    j = await send(imp, j, p)
    j, _ = await wait_stage(imp, j["id"], ("transcription",))
    await asyncio.sleep(0.5)
    imp._task.cancel()
    if imp.proc and imp.proc.returncode is None:
        imp.proc.kill()
    os.environ["FAKE_SLEEP"] = "0.1"
    imp2 = new_imp()
    check(imp2.jobs[j["id"]]["stage"] == "transcription", "travail relu sur disque après redémarrage")
    imp2.start()
    j2, seen = await wait_stage(imp2, j["id"])
    args = json.loads((TMP / "offline-args.json").read_text())
    check(j2["stage"] == "terminé" and "--force" in args and "conversion" not in seen, f"repris après redémarrage ({seen})")

    # --- nettoyage : envoi laissé en plan depuis plus de 48 h
    j = imp2.create("essai", None, "oubli.mp4", 1000, None, None, None, None)
    j["updated"] = time.time() - 49 * 3600          # en mémoire seulement : le nettoyage lit cette date
    imp2.purge()
    check(j["id"] not in imp2.jobs and not (imp2.dir / (j["id"] + ".part")).exists(), "envoi abandonné depuis 48 h : effacé")
    vis = imp2.listing("autre")
    check(vis == [], "un autre compte ne voit pas ces imports")

    shutil.rmtree(TMP, ignore_errors=True)
    print(f"{ok} vérifications réussies, {fails} échec(s)")
    sys.exit(1 if fails else 0)


asyncio.run(main())
