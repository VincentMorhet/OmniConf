"""Relevé des noms (orateur actif + participants visibles) sur des pages qui imitent Zoom et Teams.

À lancer dans un conteneur worker (navigateur sans affichage, indépendant du bot) :
  docker cp tests/participants_mock_test.py webconf-w4:/tmp/ &&
  docker exec webconf-w4 /opt/webconf/venv/bin/python /tmp/participants_mock_test.py
"""
import asyncio
import sys

sys.path.insert(0, "/opt/webconf/worker")
from playwright.async_api import async_playwright  # noqa: E402

from meetings import PARTICIPANTS_JS, SPEAKER_JS, clean_name, split_org  # noqa: E402

# Zoom, partage d'écran : bandeau de vignettes (structure relevée sur de vraies réunions), indicateur audio
ZOOM_SHARE = """<div class="speaker-bar-container__horizontal-view-wrap">
 <div class="speaker-bar-container__video-frame" style="width:200px;height:110px">
   <div class="video-avatar__avatar"><span>ALICE TEST (Org A / Lyon)</span>
   <div class="video-avatar__avatar-footer"><span>ALICE TEST (Org A / Lyon)</span></div></div></div>
 <div class="speaker-bar-container__video-frame" style="width:200px;height:110px">
   <div class="video-avatar__avatar"><span>bob test (Org B)</span>
   <div class="video-avatar__avatar-footer"><span>bob test (Org B)</span>
     <div class="audio-level-indicator" style="width:8px;height:8px"></div></div></div></div>
</div><div class="sharee-container" style="width:800px;height:500px">écran partagé</div>"""

# Zoom, vue Intervenant : cadre de l'orateur actif
ZOOM_SPEAKER = """<div class="speaker-active-container__wrap"><div class="speaker-active-container__video-frame" style="width:600px;height:400px">
  <div class="video-avatar__avatar"><div class="video-avatar__avatar-footer"><span>ALICE TEST (Org A / Lyon)</span></div></div></div></div>"""

# Teams : un contour de niveau de voix par vignette ; le nom est quelque part dans la vignette
TEAMS = """<div data-tid="stage">
 <div role="group" style="width:300px;height:170px"><div data-tid="voice-level-stream-outline" class="vdi-frame-occlusion ___x" style="width:300px;height:170px"></div>
   <div><div><span>Carol Test</span></div></div></div>
 <div role="group" aria-label="Dan Test (Externe), vidéo désactivée" style="width:300px;height:170px">
   <div data-tid="voice-level-stream-outline" class="___y" style="width:300px;height:170px"></div></div>
</div>"""


async def main():
    ok = True
    async with async_playwright() as pw:
        b = await pw.chromium.launch(channel="chrome", headless=True)
        page = await b.new_page()
        for name, html, want_spk, want_part in (
                ("zoom-partage", ZOOM_SHARE, {"bob test (Org B)"}, {"ALICE TEST (Org A / Lyon)", "bob test (Org B)"}),
                ("zoom-intervenant", ZOOM_SPEAKER, {"ALICE TEST (Org A / Lyon)"}, {"ALICE TEST (Org A / Lyon)"}),
                ("teams", TEAMS, {"Carol Test"}, {"Carol Test", "Dan Test (Externe)"})):
            await page.set_content(f"<html><body>{html}</body></html>")
            spk = set(await page.evaluate(SPEAKER_JS))
            part = set(await page.evaluate(PARTICIPANTS_JS))
            good = spk == want_spk and part == want_part
            ok &= good
            print(f"{'OK ' if good else 'ÉCHEC'} {name}: orateur={sorted(spk)} participants={sorted(part)}")
        print("split_org :", [split_org(clean_name(n)) for n in ("Dan Test (Externe)", "bob test (Org B)")])
        await b.close()
    sys.exit(0 if ok else 1)


asyncio.run(main())
