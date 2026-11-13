"""Page d'un enregistrement : compte rendu et transcription d'abord, vidéo seulement sur demande.

    BASE=https://<ihm> PW=<mot de passe> [ADMIN=admin] python tests/recording_page_ui_test.py
Il faut au moins un enregistrement vidéo avec compte rendu visible par ce compte.
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

BASE, PW, ADMIN = os.environ["BASE"], os.environ["PW"], os.environ.get("ADMIN", "admin")
ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what, flush=True)


async def vstate(pg):
    return await pg.evaluate("""() => { const v = document.querySelector('#player video');
      return {shown: getComputedStyle(v).display !== 'none', src: !!v.getAttribute('src'), paused: v.paused, t: v.currentTime}; }""")


async def seek_check(pg, box, w, where, nth=0, viewport_h=None):
    """Clic sur un horodatage : vidéo affichée, lancée, à t − 3 s (marge pour entendre la phrase)."""
    ts = pg.locator(f"{box} .ts.seek")
    n = await ts.count()
    if not n:
        check(False, f"[{w}] horodatage ({where}) : aucun dans la page")
        return
    el = ts.nth(nth if nth >= 0 else n + nth)
    t = await el.evaluate("e => +(e.dataset.t || e.closest('[data-t]')?.dataset.t || NaN)")
    await el.scroll_into_view_if_needed(); await el.click()
    try:
        await pg.wait_for_function("() => { const v = document.querySelector('#player video'); return !v.paused && v.currentTime > 0 && v.readyState >= 2; }", timeout=15000)
    except Exception:
        pass
    v = await vstate(pg)
    want = max(0, t - 3)
    check(v["shown"] and not v["paused"] and want - 0.5 <= v["t"] <= want + 2.5,
          f"[{w}] horodatage {t:.0f} s ({where}) : vidéo lancée à {v['t']:.1f} s (attendu ≈ {want:.0f} s)")
    if viewport_h:
        bb = await pg.locator("#player video").bounding_box()
        check(bb and bb["y"] >= -1 and bb["y"] + bb["height"] <= viewport_h, f"[{w}] la vidéo reste visible en haut ({bb and round(bb['y'])})")


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True, args=["--ignore-certificate-errors", "--autoplay-policy=no-user-gesture-required"])
        for w, h in ((1280, 860), (390, 844)):
            pg = await (await b.new_context(viewport={"width": w, "height": h}, ignore_https_errors=True)).new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            await pg.goto(BASE + "/")
            await pg.fill("#loginId", ADMIN); await pg.fill("#pw", PW); await pg.click("#loginForm button")
            await pg.wait_for_selector("#app:not(.hidden)")
            await pg.goto(BASE + "/#rec"); await pg.wait_for_selector("#recordings .ritem")
            items = await pg.evaluate("recItems.map(r => ({name: r.name, worker: r.worker, report: !!r.report, video: r.kind !== 'transcript'}))")
            idx = next((i for i, r in enumerate(items) if r["report"] and r["video"]), None)
            check(idx is not None, f"[{w}] un enregistrement vidéo avec compte rendu ({len(items)} au total)")
            if idx is None:
                continue
            target = items[idx]
            card = pg.locator("#recordings .ritem").nth(idx)
            await card.locator(".thumb").click()
            await pg.wait_for_selector("#player:not(.hidden)")
            await pg.wait_for_timeout(1500)
            v = await vstate(pg)
            check(not v["shown"] and not v["src"], f"[{w}] clic sur l'enregistrement : la vidéo n'est ni affichée ni chargée")
            check((await pg.evaluate("location.hash")).startswith("#enr/"), f"[{w}] la page a sa propre adresse")
            check(await pg.is_visible("#playerCr") and await pg.locator("#playerTabs .vt.on").get_attribute("data-v") == "cr",
                  f"[{w}] le compte rendu s'affiche d'abord")
            await pg.wait_for_function("document.querySelector('#playerCr').textContent.length > 200", timeout=20000)
            check(await pg.is_visible("#playerPlay"), f"[{w}] bouton « ▶ Lire la vidéo »")
            if w == 1280:
                await pg.screenshot(path="recpage-cr.png")
                # horodatage du compte rendu, vidéo fermée : la vidéo s'ouvre et démarre 3 s avant ce temps
                await seek_check(pg, "#playerCr", w, "compte rendu")
                # second horodatage, vidéo ouverte : saut au nouveau temps
                await seek_check(pg, "#playerCr", w, "compte rendu, vidéo déjà ouverte", nth=-1)
                # horodatage de la transcription
                await pg.click('#playerTabs .vt[data-v="tr"]')
                await seek_check(pg, "#playerTr", w, "transcription", nth=3)
                await pg.click('#playerTabs .vt[data-v="cr"]')
                await pg.click("#playerPlay"); await pg.wait_for_timeout(500)   # masque la vidéo
                await pg.click("#playerPlay"); await pg.wait_for_timeout(3000)
                v = await vstate(pg)
                check(v["shown"] and v["src"] and not v["paused"], f"« ▶ Lire la vidéo » : lecteur dans la page, lecture lancée ({v})")
                box = await pg.locator("#player video").bounding_box()
                check(box and box["height"] < 860 * 0.5, f"la vidéo ne prend pas tout l'écran (hauteur {box and round(box['height'])})")
                await pg.screenshot(path="recpage-video.png")
                await pg.click("#playerPlay"); await pg.wait_for_timeout(500)
                v = await vstate(pg)
                check(not v["shown"] and v["paused"], "« ⏸ Masquer la vidéo » : arrêtée et masquée")
                await pg.go_back(); await pg.wait_for_timeout(800)
                check(await pg.is_hidden("#player") and (await pg.evaluate("location.hash")) == "#rec", "Retour du navigateur : page fermée, liste des enregistrements")
                await card.locator(".play").click(); await pg.wait_for_timeout(3000)
                v = await vstate(pg)
                check(v["shown"] and not v["paused"], "bouton « ▶ Lire » de la vignette : page avec la vidéo lancée")
                await pg.keyboard.press("Escape"); await pg.wait_for_timeout(800)
                check(await pg.is_hidden("#player") and (await vstate(pg))["paused"], "Échap : page fermée, vidéo arrêtée")
                # adresse directe de la page
                await pg.goto(BASE + f"/#enr/{target['worker']}/{target['name']}"); await pg.wait_for_selector("#player:not(.hidden)", timeout=15000)
                check(not (await vstate(pg))["shown"], "adresse directe : page ouverte, vidéo non lancée")
            else:
                await pg.screenshot(path="recpage-phone.png")
                # téléphone : horodatage plus bas dans le compte rendu → la vidéo démarre et reste visible en haut
                await seek_check(pg, "#playerCr", w, "compte rendu (téléphone)", nth=-1, viewport_h=h)
                await pg.screenshot(path="recpage-phone-seek.png")
                sw = await pg.evaluate("document.querySelector('#player').scrollWidth")
                check(sw <= w, f"[{w}] page sans défilement horizontal ({sw})")
            check(not errs, f"[{w}] aucune erreur JavaScript {errs[:2]}")
        await b.close()
    print(f"\n{ok} vérifications réussies, {fails} échecs")
    sys.exit(1 if fails else 0)


asyncio.run(main())
