"""Partage en direct gardé après la réunion, côté assistant : les spectateurs reçoivent l'accès à l'enregistrement, à la
transcription et au compte rendu (« shared_with ») de la réunion, y compris s'ils sont ajoutés avant l'entrée ou entre
deux enregistrements ; retirés du direct pendant la réunion, ils perdent cet accès (pas un partage fait à la main) ;
rien ne change pour la réunion suivante. Fichiers fictifs dans un dossier temporaire, ni navigateur ni ffmpeg.

    python tests/live_share_keep_test.py      # dans un conteneur d'assistant (dépendances du worker)
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="lsk-"))
os.environ.update(RECORD_DIR=str(TMP / "rec"), PROFILE_DIR=str(TMP / "profile"), AGENT_TOKEN="essai",
                  WORKER_NAME="wT", OVH_AI_ENDPOINTS_ACCESS_TOKEN="test", REPORT_AUTO="0")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import agent as A  # noqa: E402

ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what, flush=True)


R, T = A.recorder, A.transcriber
stems = iter(["20261126-100000_point", "20261126-104000_point", "20261126-140000_autre"])


async def fake_rec_start(title, meta):          # enregistrement simulé : un fichier, un « processus » qui tourne
    stem = next(stems)
    R.file = R.dir / (stem + ".mp4")
    R.file.write_bytes(b"\0")
    R.meta = {**meta, "title": title}
    R.proc = types.SimpleNamespace(returncode=None)
    R._write_meta()
    return R.file.name


async def fake_tr_start(title, meta):           # transcription simulée, à côté de la vidéo en cours
    T.active, T.base, T.meta = True, R.file.with_suffix(""), {**meta, "title": title}


def rec_stop():                                  # comme Recorder.stop : métadonnées en mémoire écrites sur disque
    R.meta["ended_at"] = "fin"
    R._write_meta()
    R.proc = None


async def noop(*a, **k):
    return {}

R.start, A._start_transcript = fake_rec_start, fake_tr_start
A.browser.blank, A._zoom_release, A.status = noop, noop, noop
disk = lambda stem: json.loads((R.dir / (stem + ".mp4.json")).read_text())
V = lambda *v: A.meeting_viewers(A.ViewersReq(viewers=list(v)))


async def main():
    s1, s2 = "20261126-100000_point", "20261126-104000_point"
    A.set_state("joining", joined_at="t1", url="https://teams.example/x", owner="essai")
    A.state["owner"] = "essai"
    r = await V("bruno", "essai")
    check(r["viewers"] == ["bruno"] and r["files"] == 0, "partage en salle d'attente : aucun fichier encore, propriétaire écarté")
    await A._after_join(A.JoinReq(url="https://teams.example/x", owner="essai", title="Point"), "teams")
    check(R.meta.get("shared_with") == ["bruno"] and T.meta.get("shared_with") == ["bruno"] and disk(s1)["shared_with"] == ["bruno"],
          "entrée en réunion : enregistrement et transcription déjà partagés avec le spectateur")
    check(A.meeting_files == {s1}, "fichiers de la réunion suivis")

    await A.recording_meta(s1 + ".mp4", A.MetaReq(shared_with=["bruno", "chloe"]))
    check(T.meta["shared_with"] == ["bruno", "chloe"] and R.meta["shared_with"] == ["bruno", "chloe"],
          "« Partager avec… » pendant la réunion : aussi dans la transcription en cours (plus écrasé à la sauvegarde suivante)")
    r = await V("dora")
    check(R.meta["shared_with"] == ["chloe", "dora"] and R.meta["live_viewers"] == ["dora"] and T.meta["shared_with"] == ["chloe", "dora"]
          and disk(s1)["shared_with"] == ["chloe", "dora"] and r["files"] == 1,
          "Bruno retiré du direct : il perd l'accès ; Chloé (partage à la main) le garde ; Dora l'obtient")

    rec_stop()
    T.active = False
    await A.rec_start(A.RecReq())                 # nouvel enregistrement dans la même réunion
    check(R.meta.get("shared_with") == ["dora"] and A.meeting_files == {s1, s2}, "nouvel enregistrement : partagé d'emblée avec les spectateurs")
    await V("dora", "eve")
    rec_stop()
    check(disk(s1)["shared_with"] == ["chloe", "dora", "eve"] and disk(s2)["shared_with"] == ["dora", "eve"],
          "spectatrice ajoutée ensuite : les deux enregistrements de la réunion lui sont partagés")

    A.set_state("idle", owner=None)
    check(A.state["viewers"] == [], "fin de réunion : plus de spectateurs en direct")
    listed = {x["name"]: x["meta"] for x in R.list()}
    check(listed[s1 + ".mp4"]["shared_with"] == ["chloe", "dora", "eve"] and listed[s2 + ".mp4"]["shared_with"] == ["dora", "eve"],
          "après la réunion : l'enregistrement reste partagé (liste lue par le contrôle)")

    A.set_state("joining", joined_at="t2", url="https://zoom.example/y", owner="essai")
    A.state["owner"] = "essai"
    check(A.meeting_files == set(), "réunion suivante : ses fichiers seulement")
    r = await V("fred")
    check(r["files"] == 0 and disk(s1)["shared_with"] == ["chloe", "dora", "eve"], "partage de la réunion suivante : l'ancienne n'est pas touchée")
    A.set_state("idle", owner=None)

    s3 = "20261126-160000_web"                    # réunion transcrite sans vidéo
    (R.dir / (s3 + ".transcript.json")).write_text(json.dumps({"meta": {"owner": "essai"}, "paragraphs": []}))
    await A.recording_meta(s3 + ".transcript.json", A.MetaReq(shared_with=["gil"]))
    check(json.loads((R.dir / (s3 + ".transcript.json")).read_text())["meta"]["shared_with"] == ["gil"],
          "transcription sans vidéo : partage enregistré")
    try:
        await A.recording_meta("inconnue.mp4", A.MetaReq(shared_with=["gil"]))
        check(False, "réunion inconnue refusée")
    except A.HTTPException as e:
        check(e.status_code == 404, "réunion inconnue refusée (404)")

asyncio.run(main())
print(f"\n{ok} ok, {fails} échec(s)")
sys.exit(1 if fails else 0)
