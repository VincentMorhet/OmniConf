"""IHM : panneau « Accès de Claude (MCP) » (liste, création affichée une fois, révocation) et menu du CR final.

  docker exec -e PW=... -e BASE=https://192.168.0.233 webconf-w4 /opt/webconf/venv/bin/python /tmp/mcp_ui_test.py
"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

BASE, PW = os.environ.get("BASE", "https://192.168.0.233"), os.environ["PW"]


async def main():
    errors = []
    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        ctx = await b.new_context(ignore_https_errors=True, viewport={"width": 1300, "height": 900})
        page = await ctx.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: m.type == "error" and errors.append(m.text))
        page.on("dialog", lambda d: asyncio.ensure_future(d.accept("test-ihm")))
        await page.goto(BASE + "/")
        await page.fill("#pw", PW)
        await page.press("#pw", "Enter")
        await page.wait_for_selector("#app:not(.hidden)", timeout=15000)
        await page.goto(BASE + "/#reglages")
        await page.wait_for_selector("#tab-set:not(.hidden)", timeout=20000)
        await page.click("#mcpBox > summary")
        await page.wait_for_selector("#mcpTokens .tok, #mcpTokens small", timeout=10000)   # aucun jeton actif : possible
        n0 = await page.locator("#mcpTokens .tok").count()
        print("url :", await page.text_content("#mcpUrl"), "| jetons actifs :", n0,
              "| certificat :", await page.is_visible("#mcpCa"), (await page.text_content("#mcpCaFp"))[:23])
        await page.click("#mcpCreate")
        await page.wait_for_selector("#mcpNew:not(.hidden)")
        tok = await page.input_value("#mcpNewTok")
        await page.wait_for_function(f"document.querySelectorAll('#mcpTokens .tok').length > {n0}")
        print("jeton créé, affiché une fois :", tok[:4] + "…", len(tok), "car.")
        await page.click("#mcpHide")
        assert await page.input_value("#mcpNewTok") == ""
        # révocation du jeton créé (le dernier de la liste)
        await page.locator("#mcpTokens .tok button[data-id]").last.click()
        await page.wait_for_function(f"document.querySelectorAll('#mcpTokens .tok').length == {n0}")
        print("révoqué, jetons actifs :", await page.locator("#mcpTokens .tok").count())
        # CR final dans le menu ⋯ d'un enregistrement qui en a un
        await page.goto(BASE + "/#rec")
        await page.wait_for_selector("#recordings .ritem", timeout=20000)
        items = page.locator("#recordings .ritem")
        found = False
        for i in range(await items.count()):
            it = items.nth(i)
            if await it.locator("a[href*='/final?fmt=md']").count():
                found = True
                href = await it.locator("a[href*='/final?fmt=md']").get_attribute("href")
                r = await ctx.request.get(BASE + href)
                print("CR final :", r.status, (await r.text())[:40].replace("\n", " "))
        print("menu CR final présent :", found)
        await page.screenshot(path="/tmp/mcp_ui.png", full_page=False)
        await b.close()
    print("erreurs JS :", errors or "aucune")
    sys.exit(1 if errors else 0)


asyncio.run(main())
