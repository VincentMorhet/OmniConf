"""IHM multi-comptes : connexion, création d'un compte, mot de passe provisoire, Mon compte, partage.

    BASE=https://<ihm de test> PW=<mot de passe admin> python tests/users_ui_test.py  (captures dans le dossier courant)
"""
import asyncio
import os
import sys

import httpx
from playwright.async_api import async_playwright

BASE, PW, ADMIN = os.environ["BASE"], os.environ["PW"], os.environ.get("ADMIN", "admin")
ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what)


async def login(pg, user, pw):
    await pg.goto(BASE + "/#rec")
    await pg.wait_for_selector("#loginForm")
    await pg.fill("#loginId", user)
    await pg.fill("#pw", pw)
    await pg.click("#loginForm button")


async def main():
    adm = httpx.Client(base_url=BASE, verify=False)
    adm.post("/api/login", json={"login": ADMIN, "password": PW})
    adm.delete("/api/users/t-eve")
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True, args=["--ignore-certificate-errors"])
        for w, h in ((1280, 900), (390, 844)):
            ctx = await b.new_context(viewport={"width": w, "height": h}, ignore_https_errors=True)
            pg = await ctx.new_page()
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            await login(pg, ADMIN, PW)
            await pg.wait_for_selector("#app:not(.hidden)")
            check(await pg.is_visible("#accountBtn"), f"[{w}] administrateur connecté, bouton « Mon compte »")
            await pg.goto(BASE + "/#reglages"); await pg.wait_for_selector("#tab-set:not(.hidden)")
            check(await pg.is_visible("#usersBox"), f"[{w}] encadré Utilisateurs visible pour l'administrateur")
            if w == 1280:
                await pg.click("#usersBox summary")
                await pg.wait_for_selector("#usersList .user")
                await pg.fill("#ua-login", "t-eve"); await pg.fill("#ua-name", "Ève Test")
                await pg.click("#userAdd button")
                await pg.wait_for_selector("#dlgSecret[open]")
                prov = await pg.input_value("#secValue")
                check(len(prov) >= 12, "mot de passe provisoire affiché une fois")
                await pg.locator("#dlgSecret").screenshot(path="users-secret.png")
                await pg.click("#dlgSecret button.primary")
                await pg.wait_for_selector("#usersList .user[data-login='t-eve']")
                await pg.locator("#usersBox").screenshot(path="users-box.png")
                # partage d'une réunion depuis le menu ⋯
                await pg.goto(BASE + "/#rec"); await pg.wait_for_selector("#tab-rec:not(.hidden)"); await pg.wait_for_timeout(1500)
                if await pg.locator("#recordings .ritem .more").count():
                    await pg.locator("#recordings .ritem .more").first.click()
                    await pg.locator("#recordings .ritem .share").first.click()
                    await pg.wait_for_selector("#dlgShare[open]")
                    check(await pg.locator("#shList input[value='t-eve']").count() == 1, "Ève proposée dans le partage")
                    await pg.locator("#dlgShare").screenshot(path="users-share.png")
                    await pg.click("#dlgShare button[value=cancel]")
            await pg.click("#accountBtn")
            await pg.wait_for_selector("#dlgMe[open]")
            await pg.click("#meProfile")
            await pg.wait_for_selector("#dlgAccount[open]")
            await pg.locator("#dlgAccount").screenshot(path=f"users-account-{w}.png")
            box = await pg.locator("#dlgAccount").bounding_box()
            check(box and box["width"] <= w, f"[{w}] fenêtre « Mon compte » dans l'écran")
            await pg.click("#dlgAccount button[value=cancel]")
            sw = await pg.evaluate("document.documentElement.scrollWidth")
            check(sw <= w, f"[{w}] pas de défilement horizontal ({sw})")
            if w == 1280:
                await pg.click("#accountBtn"); await pg.wait_for_selector("#dlgMe[open]")
                await pg.click("#logout")
                await pg.wait_for_selector("#loginForm")
                await login(pg, "t-eve", prov)
                await pg.wait_for_selector("#dlgPw[open]")
                check(await pg.is_hidden("#pwCancel"), "mot de passe provisoire : changement obligatoire (pas d'Annuler)")
                await pg.keyboard.press("Escape")
                check(await pg.is_visible("#dlgPw"), "Échap ne ferme pas la fenêtre")
                await pg.fill("#pwNew", "eve-mot-de-passe-1"); await pg.fill("#pwNew2", "eve-mot-de-passe-1")
                await pg.click("#pwOk")
                await pg.wait_for_selector("#dlgPw", state="hidden")
                check(True, "mot de passe changé depuis l'IHM")
                await pg.goto(BASE + "/#reglages"); await pg.wait_for_selector("#tab-set:not(.hidden)")
                await pg.wait_for_timeout(1500)
                check(await pg.is_hidden("#usersBox"), "Ève : pas d'encadré Utilisateurs")
                check(await pg.is_hidden("#lexSave"), "Ève : lexique en lecture seule")
                await pg.goto(BASE + "/#rec"); await pg.wait_for_timeout(1500)
                n = await pg.locator("#recordings .ritem").count()
                check(n == 0, f"Ève ne voit aucune réunion ({n})")
                await pg.screenshot(path="users-eve.png")
            check(not errs, f"[{w}] aucune erreur JavaScript {errs[:2]}")
            await ctx.close()
        await b.close()
    adm.delete("/api/users/t-eve")
    print(f"\n{ok} vérifications réussies, {fails} échecs")
    sys.exit(1 if fails else 0)


asyncio.run(main())
