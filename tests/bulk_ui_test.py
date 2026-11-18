"""Plusieurs réunions partagées d'un coup, plusieurs consentements attestés d'un coup : API et IHM.

Contrôle d'essai lancé à part, assistants simulés (liste de réunions en mémoire, aucun appel à un vrai assistant),
comptes et personnes fictifs. La recherche des voix est neutralisée (on note seulement les noms demandés).

    # sur un hôte qui a le venv du contrôle (aucun fichier d'environnement réel : tout est fictif)
    python tests/bulk_ui_test.py serve                      # port 8099
    # dans un conteneur qui a Chrome (réseau de l'hôte)
    BASE=http://127.0.0.1:8099 PW=… python tests/bulk_ui_test.py
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("TEST_DIR", "/tmp/wk"))
PEOPLE = ["Alice Fictive", "Bruno Exemple", "Chloé Essai", "David Témoin", "Emma Modèle"]


def serve():
    data = Path(os.environ.get("TEST_DATA_DIR", "/tmp/ctltest/data"))     # jamais le DATA_DIR du vrai contrôle
    data.mkdir(parents=True, exist_ok=True)
    (data / "voice_questions.json").write_text(json.dumps({"items": {}, "answers": {}}))
    now = "2026-11-10T10:00:00+01:00"
    (data / "voice_consents.json").write_text(json.dumps([
        {"id": f"p{i}", "name": n, "consent": i == 0, "at": now if i == 0 else None, "by": "essai" if i == 0 else None,
         "note": None, "history": [{"at": now, "by": "essai", "action": "ajout"}]} for i, n in enumerate(PEOPLE)]))
    os.environ.update(DATA_DIR=str(data), ADMIN_LOGIN="essai", ADMIN_NAME="Compte Essai", COOKIE_SECURE="0",
                      SECRET_KEY=os.urandom(16).hex(), AGENT_TOKEN="essai-sans-secret",
                      WORKERS="wA=http://127.0.0.1:9,wB=http://127.0.0.1:9")
    sys.path.insert(0, str(ROOT / "control"))
    os.chdir(ROOT / "control")
    import app as A
    import uvicorn
    store = {"wA": [], "wB": []}
    t0 = time.time()
    for i in range(6):                                   # 6 réunions d'essai, 1 d'un autre compte
        w = "wA" if i % 2 == 0 else "wB"
        owner = "autre" if i == 5 else "essai"
        store[w].append({"name": f"2026110{i}-100000_essai-{i}.mp4", "kind": "video", "size": 1000, "mtime": t0 - i * 86400,
                         "meta": {"title": f"Réunion d'essai {i}", "owner": owner, "shared_with": ["tiers"] if i == 1 else [],
                                  "started_at": f"2026-11-0{i + 1}T10:00:00+01:00", "platform": "teams"},
                         "recording": False, "duration": 1800, "transcript": True, "report": True})
    asked = {"vp": [], "patches": 0}

    async def worker_recordings(n, fresh=False):
        return [{**x, "worker": n} for x in store.get(n, [])]

    class R:
        status_code, content = 200, b""

        def __init__(self, d):
            self.d = d

        def json(self):
            return self.d

    async def wcall(name, method, path, **kw):
        if method == "PATCH" and path.endswith("/meta"):
            f = path.split("/")[2]
            x = next(x for x in store[name] if x["name"] == f)
            x["meta"].update(kw["json"])
            asked["patches"] += 1
            return R({"ok": True, **kw["json"]})
        if path.endswith("/thumb"):
            return R({})
        return R({})

    async def no_push(names=None):
        return {}
    A.worker_recordings, A.wcall, A.voiceprints_push = worker_recordings, wcall, no_push
    A.vp_start_build = lambda names, by: {"state": "done"}
    A.vp_schedule = lambda names, by, delay=60: asked["vp"].extend(names)

    @A.app.get("/test/state")
    async def test_state():
        return {"store": store, "vp": asked["vp"], "patches": asked["patches"],
                "consents": json.loads((data / "voice_consents.json").read_text())}
    uvicorn.run(A.app, host="127.0.0.1", port=int(os.environ.get("TEST_PORT", "8099")), log_level="warning")


ok = fails = 0


def check(cond, what):
    global ok, fails
    ok, fails = (ok + 1, fails) if cond else (ok, fails + 1)
    print("  ok  " if cond else "  ÉCHEC", what, flush=True)


def shares(st):
    return {x["meta"]["title"]: sorted(x["meta"]["shared_with"]) for w in st["store"].values() for x in w}


async def main():
    import httpx
    from playwright.async_api import async_playwright
    BASE, PW = os.environ["BASE"], os.environ["PW"]
    me = httpx.Client(base_url=BASE, timeout=60)
    check(me.post("/api/login", json={"login": "essai", "password": PW}).status_code == 200, "connexion (essai)")
    pws = {}
    for login, name in (("autre", "Compte Autre"), ("tiers", "Compte Tiers"), ("quart", "Compte Quart")):
        pws[login] = me.post("/api/users", json={"login": login, "name": name, "role": "user"}).json().get("password")
    other = httpx.Client(base_url=BASE, timeout=60)
    check(other.post("/api/login", json={"login": "autre", "password": pws["autre"]}).status_code == 200, "connexion (autre)")
    S = lambda: me.get("/test/state").json()
    recs = me.get("/api/recordings").json()
    items = lambda *idx: [{"worker": r["worker"], "file": r["name"]} for r in recs if int(r["name"][7]) in idx]

    # --- API : partage groupé
    r = me.post("/api/recordings/access-bulk", json={"items": items(0, 1, 2), "add": ["autre", "quart"]})
    sh = shares(S())
    check(r.status_code == 200 and r.json()["done"] == 3 and sh["Réunion d'essai 0"] == ["autre", "quart"]
          and sh["Réunion d'essai 1"] == ["autre", "quart", "tiers"] and sh["Réunion d'essai 3"] == [],
          f"3 réunions partagées d'un coup, partages existants gardés ({r.json()})")
    r = me.post("/api/recordings/access-bulk", json={"items": items(0, 1), "remove": ["quart"]})
    sh = shares(S())
    check(r.json()["done"] == 2 and sh["Réunion d'essai 0"] == ["autre"] and sh["Réunion d'essai 1"] == ["autre", "tiers"],
          "partage retiré d'un coup")
    check(me.post("/api/recordings/access-bulk", json={"items": items(0), "add": ["inconnu"]}).status_code == 400, "compte inconnu refusé")
    check(me.post("/api/recordings/access-bulk", json={"items": items(0)}).status_code == 400, "rien à faire : refusé")
    r = other.post("/api/recordings/access-bulk", json={"items": items(2, 3, 5), "add": ["tiers"]})
    d = r.json()
    check(r.status_code == 200 and d["done"] == 1 and len(d["failed"]) == 2 and shares(S())["Réunion d'essai 5"] == ["tiers"],
          f"autre : seulement sa propre réunion, les deux d'essai refusées ({[f['error'] for f in d['failed']]})")
    me.post("/api/recordings/access-bulk", json={"items": items(5), "add": ["autre"]})
    check(shares(S())["Réunion d'essai 5"] == ["tiers"], "le propriétaire n'est jamais ajouté à son propre partage")

    # --- API : plusieurs consentements
    cs = me.get("/api/voice-consents").json()
    check("{n}" in cs.get("attestation_many", ""), "texte d'attestation pour plusieurs personnes fourni")
    ids = {x["name"]: x["id"] for x in cs["items"]}
    check(me.post("/api/voice-consents/attest", json={"ids": [ids["Bruno Exemple"]], "attest": False}).status_code == 400,
          "sans attestation cochée : refusé")
    check(other.post("/api/voice-consents/attest", json={"ids": [ids["Bruno Exemple"]], "attest": True}).status_code == 403,
          "compte non administrateur : refusé")
    check(me.post("/api/voice-consents/attest", json={"ids": [ids["Alice Fictive"]], "attest": True}).status_code == 400,
          "déjà attestée : rien à faire")
    r = me.post("/api/voice-consents/attest", json={"ids": [ids["Bruno Exemple"], ids["Chloé Essai"], ids["Alice Fictive"]],
                                                     "names": ["Félix Nouveau", "chloé essai", "  "], "attest": True, "note": "courriels du 16/11"})
    st = S()
    c = {x["name"]: x for x in st["consents"]}
    check(r.status_code == 200 and sorted(r.json()["attested"]) == ["Bruno Exemple", "Chloé Essai", "Félix Nouveau"],
          f"3 consentements d'un coup, dont une personne ajoutée ; doublon et déjà attestée ignorés ({r.json()})")
    h = c["Bruno Exemple"]["history"][-1]
    check(c["Bruno Exemple"]["consent"] and c["Bruno Exemple"]["note"] == "courriels du 16/11" and h.get("lot") == 3
          and "chacune des 3 personnes" in h.get("texte", "") and h["by"] == "essai", "attestation notée dans l'historique de chacun")
    check(sorted(st["vp"]) == ["Bruno Exemple", "Chloé Essai", "Félix Nouveau"], "recherche des voix demandée pour eux seuls")
    check(not c["David Témoin"]["consent"] and not c["Emma Modèle"]["consent"], "les autres personnes ne changent pas")

    async with async_playwright() as p:
        b = await p.chromium.launch(channel="chrome", headless=True)
        errs = []
        for vw, vh, tag in ((1280, 900, "pc"), (390, 844, "tel")):
            ctx = await b.new_context(viewport={"width": vw, "height": vh}, locale="fr-FR", timezone_id="Europe/Paris",
                                      is_mobile=tag == "tel", has_touch=tag == "tel")
            pg = await ctx.new_page()
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
            await pg.goto(BASE + "/#rec")
            await pg.fill("#loginId", "essai")
            await pg.fill("#pw", PW)
            await pg.click("#loginForm button")
            await pg.wait_for_selector("#recordings .ritem")
            await pg.evaluate("() => { window.__toasts = []; const o = toast; toast = (m, ms) => { window.__toasts.push(String(m)); return o(m, ms); }; }")
            if tag == "pc":
                # --- page d'une réunion : un clic dans la marge ne la ferme pas
                await pg.locator("#recordings .ritem .rtitle").first.click()
                await pg.wait_for_selector("#player:not(.hidden)")
                h = await pg.evaluate("() => location.hash")
                await pg.mouse.click(1265, 450)
                await asyncio.sleep(0.5)
                still = await pg.evaluate("() => !document.querySelector('#player').classList.contains('hidden') && location.hash")
                check(still == h and h.startswith("#enr/"), f"clic dans la marge de la page d'une réunion : elle reste ouverte ({still})")
                await pg.click("#player .back")
                await pg.wait_for_selector("#player", state="hidden")
                await pg.goto(BASE + "/#rec")
                await pg.wait_for_selector("#recordings .ritem")
                # --- menu « ⋯ » : entièrement visible (première et dernière réunion)
                for i in (0, 5):
                    await pg.locator("#recordings .ritem").nth(i).locator(".more").click()
                    v = await pg.evaluate("""() => { const m = document.querySelector('.rmenu:not(.hidden)'); if (!m) return 'pas de menu';
                    const r = m.getBoundingClientRect(), d = m.querySelector('.del');
                    if (r.top < 0 || r.left < 0 || r.bottom > innerHeight + 1 || r.right > innerWidth + 1) return 'hors écran ' + JSON.stringify(r);
                    const b = d.getBoundingClientRect(), hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
                    return hit === d || d.contains(hit) ? 'ok' : 'Supprimer caché par ' + (hit && hit.className); }""")
                    check(v == "ok", f"ordinateur : menu ⋯ de la réunion {i} entier, « Supprimer » accessible ({v})")
                    await pg.keyboard.press("Escape")
                    await pg.mouse.click(5, 5)
                # --- sélection de plusieurs réunions et partage
                await pg.click("#selBtn")
                n_box = await pg.locator("#recordings .ritem .pickbox").count()
                check(n_box == 6 and await pg.is_visible("#selBar") and not await pg.locator("#recordings .ritem .play").first.is_visible(),
                      f"mode sélection : une case par réunion, barre d'actions, pas de lecture ({n_box})")
                check(await pg.locator("#recordings .ritem.nopick").count() == 0, "administrateur : toutes les réunions sélectionnables")
                await pg.locator("#recordings .ritem", has_text="Réunion d'essai 3").click()
                await pg.locator("#recordings .ritem", has_text="Réunion d'essai 4").click()
                check(await pg.text_content("#selCount") == "2 réunions sélectionnées" and not await pg.is_disabled("#selShare"),
                      "deux réunions choisies d'un clic chacune")
                await pg.locator("#recordings .ritem", has_text="Réunion d'essai 4").click()
                check(await pg.text_content("#selCount") == "1 réunion sélectionnée", "un deuxième clic la retire")
                await pg.fill("#r-search", "essai 2")
                await pg.click("#selAll")
                await pg.fill("#r-search", "")
                check(await pg.text_content("#selCount") == "2 réunions sélectionnées", "« Tout sélectionner » : les réunions affichées après recherche")
                await pg.click("#selAll")
                check(await pg.text_content("#selCount") == "6 réunions sélectionnées", "« Tout sélectionner » sans recherche : les 6")
                await pg.click("#selShare")
                await pg.wait_for_selector("#dlgShareMany[open]")
                txt = await pg.text_content("#smList")
                check("Compte Autre" in txt and "Compte Tiers" in txt and "Compte Essai" not in txt and "déjà partagées : 2" in txt,
                      f"comptes proposés, avec les partages déjà faits ({txt.strip()[:120]})")
                check(await pg.is_disabled("#smOk"), "« Enregistrer » grisé tant que personne n'est coché")
                await pg.check("#smList input[value=quart]")
                await pg.screenshot(path=str(OUT / "bulk-share.png"))
                await pg.click("#smOk")
                await pg.wait_for_selector("#selBar", state="hidden")
                sh = shares(S())
                check(all("quart" in sh[f"Réunion d'essai {i}"] for i in range(6)) and sh["Réunion d'essai 1"] == ["autre", "quart", "tiers"],
                      f"IHM : les 6 réunions partagées avec « quart » d'un coup, partages existants gardés ({sh})")
                t = await pg.evaluate("() => window.__toasts.join(' | ')")
                check("Partage enregistré pour 6 réunions" in t, f"message de confirmation ({t})")
                await pg.click("#selBtn")
                await pg.click("#selAll")
                await pg.click("#selShare")
                await pg.wait_for_selector("#dlgShareMany[open]")
                await pg.check("input[name=smMode][value=remove]")
                await pg.check("#smList input[value=quart]")
                await pg.click("#smOk")
                await pg.wait_for_selector("#selBar", state="hidden")
                check(not any("quart" in v for v in shares(S()).values()), "IHM : partage retiré d'un coup")
                # --- plusieurs consentements
                await pg.goto(BASE + "/#reglages")
                await pg.click("#voiceBox summary")
                await pg.click("#vaMany")
                await pg.wait_for_selector("#dlgConsentMany[open]")
                names = await pg.evaluate("() => [...document.querySelectorAll('#cmList label')].map(l => l.textContent.trim())")
                check(sorted(names) == ["David Témoin", "Emma Modèle"], f"seules les personnes sans consentement sont proposées ({names})")
                check(await pg.is_disabled("#cmOk"), "« Attester » grisé au départ")
                await pg.click("#cmAll")
                await pg.fill("#cmNew", "Gaëlle Ajoutée\nHugo Ajouté")
                check(await pg.is_disabled("#cmOk") and "chacune des 4 personnes" in await pg.text_content("#cmText"),
                      "4 personnes : attestation à cocher, texte au pluriel")
                await pg.check("#cmAttest")
                check(not await pg.is_disabled("#cmOk") and await pg.text_content("#cmOk") == "Attester 4 consentements", "« Attester 4 consentements »")
                await pg.uncheck("#cmList input >> nth=0")
                check(not await pg.is_checked("#cmAttest") and await pg.is_disabled("#cmOk"),
                      "liste modifiée après coup : l'attestation est à recocher")
                await pg.check("#cmList input >> nth=0")
                await pg.fill("#cmFilter", "emma")
                check(await pg.locator("#cmList label:not(.hidden)").count() == 1, "filtre des noms")
                await pg.fill("#cmFilter", "")
                await pg.check("#cmAttest")
                await pg.screenshot(path=str(OUT / "bulk-consents.png"))
                await pg.click("#cmOk")
                t = await wait_toast(pg, "consentements enregistrés")
                c = {x["name"]: x for x in S()["consents"]}
                check(t and all(c[n]["consent"] for n in ("David Témoin", "Emma Modèle", "Gaëlle Ajoutée", "Hugo Ajouté")),
                      f"IHM : 4 consentements attestés d'un coup ({t})")
                await pg.wait_for_function("() => document.querySelectorAll('#voiceList .consent input:checked').length === 8")
                check(True, "liste des consentements rafraîchie (8 cochées)")
            else:
                # --- menu « ⋯ » sur téléphone : panneau en bas de l'écran (la réunion seule de son jour était coupée)
                await pg.locator("#recordings .ritem").first.locator(".more").click()
                await asyncio.sleep(0.3)
                v = await pg.evaluate("""() => { const m = document.querySelector('.rmenu:not(.hidden)'); if (!m) return 'pas de menu';
                    const r = m.getBoundingClientRect(), d = m.querySelector('.del');
                    if (r.top < 0 || r.left < 0 || r.bottom > innerHeight + 1 || r.right > innerWidth + 1) return 'hors écran ' + JSON.stringify(r);
                    const b = d.getBoundingClientRect(), hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
                    return hit === d || d.contains(hit) ? 'ok' : 'Supprimer caché par ' + (hit && hit.className); }""")
                head = await pg.evaluate("() => document.querySelector('.rmenu:not(.hidden) .rmenu-h')?.textContent")
                check(v == "ok" and head == "Réunion d'essai 0", f"téléphone : menu ⋯ entier, avec le titre, « Supprimer » accessible ({v}, {head})")
                await pg.screenshot(path=str(OUT / "menu-phone.png"))
                await pg.evaluate("() => renderRecordings()")      # rafraîchissement périodique pendant que le menu est ouvert
                await pg.tap(".rmenu:not(.hidden) .share")
                await pg.wait_for_selector("#dlgShare[open]", timeout=8000)
                check("Compte Autre" in await pg.text_content("#shList"), "téléphone : menu toujours là après un rafraîchissement, « Partager avec… » ouvre le choix")
                await pg.click("#dlgShare button[value=cancel]")
                await asyncio.sleep(0.3)
                check(not await pg.evaluate("() => renderRecordings.later"), "menu fermé : rafraîchissement reporté fait")
                await pg.locator("#recordings .ritem").first.locator(".more").click()
                await asyncio.sleep(0.3)
                await pg.mouse.click(195, 120)                    # à côté du panneau
                await asyncio.sleep(0.3)
                closed = await pg.evaluate("() => !document.querySelector('.rmenu:not(.hidden)') && !document.querySelector('.rmenu-back') && location.hash")
                check(closed == "#rec", f"toucher à côté : menu fermé, sans ouvrir de réunion ({closed})")
                await pg.click("#selBtn")
                await pg.locator("#recordings .ritem", has_text="Réunion d'essai 0").click()
                await asyncio.sleep(0.3)
                box = await pg.evaluate("() => { const r = document.querySelector('#selBar').getBoundingClientRect(); return [r.left, r.right, r.bottom, innerWidth, innerHeight, document.documentElement.scrollWidth]; }")
                check(box[0] >= 0 and box[1] <= box[3] + 1 and box[2] <= box[4] and box[5] <= box[3] + 1,
                      f"téléphone : barre de sélection visible, sans défilement horizontal ({box})")
                await pg.screenshot(path=str(OUT / "bulk-phone.png"))
            await ctx.close()
        check(not errs, f"aucune erreur JavaScript ({errs[:3]})")
        await b.close()
    for login in ("autre", "tiers", "quart"):
        me.delete(f"/api/users/{login}")
    print(f"{ok} vérifications réussies, {fails} échec(s)")
    sys.exit(1 if fails else 0)


async def wait_toast(pg, text, timeout=10):
    t = time.time()
    while time.time() - t < timeout:
        r = await pg.evaluate("(t) => (window.__toasts || []).find(x => x.includes(t)) || ''", text)
        if r:
            return r
        await asyncio.sleep(0.2)
    return ""


if __name__ == "__main__":
    if sys.argv[1:2] == ["serve"]:
        serve()
    else:
        asyncio.run(main())
