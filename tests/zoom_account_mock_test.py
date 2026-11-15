"""Entrée Zoom avec le compte de l'utilisateur (assistant réservé) : pages Zoom factices dans Chrome.

Aucune vraie réunion, aucun compte : on vérifie qu'en invité le nom est saisi comme avant, qu'avec le compte connecté
le nom du compte est gardé (champ grisé, prérempli ou absent), et que si Zoom propose « compagnon » ou « transférer »
(compte déjà dans la réunion sur un autre appareil), l'assistant ne clique sur rien et s'arrête. Puis la session d'un
utilisateur (cookies inventés) : chargée seule, retirée sans toucher au reste, effacée à la relance du navigateur.

    python tests/zoom_account_mock_test.py        # environnement d'un assistant (Playwright + Chrome)
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import meetings  # noqa: E402

meetings.zoom_web_url = lambda url, passcode=None: url        # pages locales au lieu de app.zoom.us
TMP = Path(tempfile.mkdtemp(prefix="zoom-mock-"))
LEAVE = """<button aria-label="Leave">Leave</button>"""
JOIN = """<button class="preview-join-button" onclick="window.join()">Join</button>"""
SCRIPT = """<script>window.clicked = [];
window.join = () => { window.clicked.push('join'); window.joinedName = (document.querySelector('#input-for-name') || {}).value;
  document.body.innerHTML = window.after; };</script>"""


def page(name, body, after=LEAVE):
    f = TMP / f"{name}.html"
    f.write_text(f"<!doctype html><html><body>{body}{SCRIPT}<script>window.after = {after!r};</script></body></html>")
    return f.as_uri()


PAGES = {
    "invite": page("invite", f'<input id="input-for-name">{JOIN}'),
    "grise": page("grise", f'<input id="input-for-name" value="Nom du Compte" disabled>{JOIN}'),
    "prerempli": page("prerempli", f'<input id="input-for-name" value="Nom du Compte">{JOIN}'),
    "sans-nom": page("sans-nom", JOIN),
    "compagnon": page("compagnon", f'<input id="input-for-name" value="Nom du Compte" disabled>{JOIN}',
                      after="""<p>You are already in this meeting on another device.</p>
                      <button onclick="window.clicked.push('companion')">Join as Companion</button>
                      <button onclick="window.clicked.push('transfer')">Transfer the meeting to this device</button>"""),
}
ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what)


async def main():
    from playwright.async_api import async_playwright
    states = []
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        pg = await b.new_page()

        async def run(key, as_me, name="Assistant Essai"):
            try:
                await asyncio.wait_for(meetings.Zoom.join(pg, PAGES[key], name, lambda s, **k: states.append(s), as_me=as_me), 60)
                return "entré", await pg.evaluate("[window.joinedName, window.clicked]")
            except meetings.AccountBusy as e:
                return "occupé", (str(e), await pg.evaluate("window.clicked"))
            except Exception as e:
                return "erreur", str(e)

        r = await run("invite", False)
        check(r[0] == "entré" and r[1][0] == "Assistant Essai", f"invité : nom de l'assistant saisi ({r})")
        r = await run("invite", True)
        check(r[0] == "entré" and r[1][0] == "Assistant Essai", f"compte connecté, champ vide : nom saisi ({r})")
        r = await run("grise", True)
        check(r[0] == "entré" and r[1][0] == "Nom du Compte", f"compte connecté, nom grisé : nom du compte gardé ({r})")
        r = await run("prerempli", True)
        check(r[0] == "entré" and r[1][0] == "Nom du Compte", f"compte connecté, nom prérempli : gardé, pas remplacé ({r})")
        r = await run("prerempli", False)
        check(r[0] == "entré" and r[1][0] == "Assistant Essai", f"invité, champ prérempli : nom de l'assistant ({r})")
        r = await run("sans-nom", True)
        check(r[0] == "entré", f"compte connecté, pas de champ nom : entrée quand même ({r})")
        r = await run("compagnon", True)
        check(r[0] == "occupé" and "pas pris votre place" in r[1][0], f"compte déjà dans la réunion : arrêt ({r[0]})")
        check(r[0] == "occupé" and r[1][1] == ["join"], f"ni « compagnon » ni « transférer » cliqués ({r[1][1] if r[0] == 'occupé' else r})")
        r = await run("grise", False)
        check(r[0] == "erreur" and "champ nom" in r[1], f"invité sans champ nom modifiable : erreur comme avant ({r})")
        await b.close()

        # session Zoom d'un utilisateur : chargée pour la réunion, seule (rien d'un autre site), puis retirée
        prof = tempfile.mkdtemp(prefix="zoom-profil-")
        br = meetings.Browser(prof, "1280x720", "chrome", kiosk=False)
        br._pw = p
        br.ctx = await p.chromium.launch_persistent_context(prof, headless=True, channel="chrome")
        await br.ctx.add_cookies([{"name": "autre_site", "value": "x", "domain": ".example.com", "path": "/"}])
        n = await br.zoom_load([
            {"name": "_zm_essai", "value": "v1", "domain": ".zoom.us", "path": "/", "expires": -1, "secure": True, "sameSite": "Lax"},
            {"name": "intrus", "value": "x", "domain": ".example.org", "path": "/"},
            {"name": "app_essai", "value": "v2", "domain": "app.zoom.us", "path": "/", "expires": 4102444800}])
        names = {c["name"] for c in await br.zoom_cookies()}
        check(n == 2 and names == {"_zm_essai", "app_essai"}, f"session chargée : seulement des cookies Zoom ({n}, {sorted(names)})")
        check(not await br.zoom_signed_in(), "faux cookie : Zoom ne reconnaît pas de session (page de profil -> connexion)")
        await br.zoom_clear()
        rest = {c["name"] for c in await br.ctx.cookies()}
        check(not await br.zoom_cookies() and "autre_site" in rest, f"session retirée, le reste du navigateur intact ({sorted(rest)})")
        await br.ctx.close()
        br.ctx = await p.chromium.launch_persistent_context(prof, headless=True, channel="chrome")
        await br.ctx.add_cookies([{"name": "_zm_reste", "value": "v", "domain": ".zoom.us", "path": "/", "expires": 4102444800}])
        await br.ctx.close()
        br.ctx = None
        orig = p.chromium.launch_persistent_context
        p.chromium.launch_persistent_context = lambda d, **kw: orig(d, headless=True, channel="chrome")
        await br._launch()                   # relance (redémarrage, plantage) : aucune session Zoom ne reste
        check(not await br.zoom_cookies(), "navigateur relancé : cookies Zoom d'avant effacés")
        await br.ctx.close()
    print(f"{ok} vérifications réussies, {fails} échec(s)")
    sys.exit(1 if fails else 0)


asyncio.run(main())
