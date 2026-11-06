"""Pilotage du navigateur (Playwright + Chrome) pour rejoindre Teams / Zoom en client web.

Les interfaces web de Teams et Zoom changent souvent : chaque étape essaie plusieurs
sélecteurs (anglais + français). Si une étape échoue, on peut toujours finir à la main
via l'écran distant (noVNC) depuis l'IHM.
"""
import asyncio
import logging
import os
import re
import time
from urllib.parse import parse_qs, urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

log = logging.getLogger("meetings")


def detect_platform(url: str) -> str:
    host = urlparse(url).hostname or ""
    if "teams.microsoft" in host or "teams.live" in host:
        return "teams"
    if "zoom.us" in host or "zoom.com" in host:
        return "zoom"
    return "web"


def zoom_web_url(url: str, passcode: str | None) -> str:
    u = urlparse(url)
    m = re.search(r"/(?:j|w|s|wc/join|wc)/(\d{9,12})", u.path)
    if not m:
        return url
    pwd = passcode or (parse_qs(u.query).get("pwd") or [None])[0]
    out = f"https://app.zoom.us/wc/{m.group(1)}/join"
    return out + (f"?pwd={pwd}" if pwd else "")


async def _find(page: Page, selectors: list[str], timeout: float = 0.0):
    """Renvoie le premier locator visible parmi `selectors`, dans la page ou ses iframes."""
    deadline = time.monotonic() + timeout
    while True:
        for frame in page.frames:
            for sel in selectors:
                try:
                    loc = frame.locator(sel).first
                    if await loc.is_visible():
                        return loc
                except Exception:
                    pass
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.5)


async def _click(page: Page, selectors: list[str], timeout: float = 0.0) -> bool:
    loc = await _find(page, selectors, timeout)
    if loc:
        try:
            await loc.click(timeout=3000)
            return True
        except Exception as e:
            log.debug("click failed %s", e)
    return False


def _btn(*names: str) -> list[str]:
    rx = "|".join(names)
    return [f"role=button[name=/{rx}/i]", f'button:text-matches("{rx}", "i")']


class Browser:
    def __init__(self, profile_dir: str, display_size: str, channel: str | None, kiosk: bool = True):
        self.profile_dir = profile_dir
        self.w, self.h = display_size.split("x")
        self.channel = channel
        self.kiosk = kiosk
        self._pw = None
        self.ctx: BrowserContext | None = None
        self.lock = asyncio.Lock()

    async def page(self) -> Page:
        if self.ctx is None:
            await self._launch()
        pages = self.ctx.pages
        page = pages[0] if pages else await self.ctx.new_page()
        for extra in pages[1:]:
            await extra.close()
        if self.kiosk:
            await self._fullscreen(page)
        return page

    async def _fullscreen(self, page: Page):
        """Plein écran (sans barre d'onglets) pour des enregistrements propres."""
        try:
            cdp = await self.ctx.new_cdp_session(page)
            win = await cdp.send("Browser.getWindowForTarget")
            wid = win["windowId"]
            b = (await cdp.send("Browser.getWindowBounds", {"windowId": wid}))["bounds"]
            w, h = int(self.w), int(self.h)
            if b.get("windowState") != "fullscreen" or b.get("width") != w or b.get("height") != h:
                # taille mémorisée dans le profil différente de l'écran : on repasse en normal pour la réajuster
                if b.get("windowState") != "normal":
                    await cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
                await cdp.send("Browser.setWindowBounds",
                               {"windowId": wid, "bounds": {"left": 0, "top": 0, "width": w, "height": h}})
                await cdp.send("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "fullscreen"}})
            await cdp.detach()
        except Exception as e:
            log.debug("fullscreen: %s", e)

    async def _launch(self):
        self._pw = self._pw or await async_playwright().start()
        args = [
            "--use-fake-ui-for-media-stream",  # autorise micro/caméra sans popup
            "--autoplay-policy=no-user-gesture-required",
            "--no-first-run", "--no-default-browser-check", "--password-store=basic",
            "--disable-features=Translate,MediaRouter,ExternalProtocolDialog",
            "--disable-blink-features=AutomationControlled",
            "--disable-notifications", "--lang=en-US",
            f"--window-size={self.w},{self.h}", "--window-position=0,0",
        ]
        if self.kiosk:
            args.append("--kiosk")
        self.ctx = await self._pw.chromium.launch_persistent_context(
            self.profile_dir, headless=False, channel=self.channel or None,
            args=args, ignore_default_args=["--enable-automation"],
            no_viewport=True, locale="en-US",  # la page suit la taille réelle de la fenêtre
            permissions=["microphone", "camera", "notifications"],
        )
        await self.ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        self.ctx.on("close", lambda *_: setattr(self, "ctx", None))

    async def goto(self, url: str):
        page = await self.page()
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        return page

    async def blank(self):
        if self.ctx:
            try:
                await (await self.page()).goto("about:blank")
            except Exception:
                pass


# --------------------------------------------------------------------------- Teams
class Teams:
    LEAVE = ["button[data-tid='hangup-main-btn']", "#hangup-button", "button[data-tid='call-hangup']",
             *_btn("^Leave", "^Quitter", "Hang up", "Raccrocher")]
    LOBBY = ["text=/let you in soon|will let you in|vous laisser entrer|vous admettre/i"]

    @staticmethod
    async def join(page: Page, url: str, name: str, set_state):
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        # Page "Comment voulez-vous rejoindre ?" -> rester dans le navigateur
        await _click(page, _btn("Continue on this browser", "Join on the web", "Use the web app",
                                "Continuer sur ce navigateur", "Participer sur le web"), timeout=20)
        # Écran de pré-connexion
        name_input = await _find(page, ["input[data-tid='prejoin-display-name-input']",
                                        "input[placeholder*='name' i]", "input[placeholder*='nom' i]"], timeout=40)
        if name_input:
            await name_input.fill(name)
        # Couper la caméra si elle est active (pas de caméra sur la VM de toute façon)
        cam = await _find(page, ["[data-tid='toggle-video'][aria-checked='true']",
                                 "input[title*='camera' i][aria-checked='true']"])
        if cam:
            await cam.click()
        if not await _click(page, ["button[data-tid='prejoin-join-button']", "#prejoin-join-button",
                                   *_btn("^Join now", "^Rejoindre maintenant", "^Join$", "^Rejoindre$")], timeout=30):
            raise RuntimeError("bouton 'Join now' introuvable")
        # Attente salle d'attente / réunion
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            if await _find(page, Teams.LEAVE):
                return
            if await _find(page, Teams.LOBBY):
                set_state("lobby")
            await asyncio.sleep(2)
        raise RuntimeError("pas admis dans la réunion")

    @staticmethod
    async def in_meeting(page: Page) -> bool:
        return await _find(page, Teams.LEAVE) is not None

    @staticmethod
    async def leave(page: Page):
        await _click(page, Teams.LEAVE, timeout=3)

    @staticmethod
    async def toggle_mute(page: Page):
        await page.keyboard.press("Control+Shift+M")


# --------------------------------------------------------------------------- Zoom
class Zoom:
    LEAVE = ["button[aria-label*='Leave' i]", "button.footer__leave-btn", *_btn("^Leave$", "^Quitter$")]
    WAITING = ["text=/host will let you in|waiting for the host|l'hôte va vous admettre|attendre l'hôte/i"]

    @staticmethod
    async def join(page: Page, url: str, name: str, set_state, passcode: str | None = None):
        await page.goto(zoom_web_url(url, passcode), wait_until="domcontentloaded", timeout=45000)
        await _click(page, ["#onetrust-accept-btn-handler", *_btn("Accept Cookies", "Accepter")], timeout=5)
        name_input = await _find(page, ["#input-for-name", "input[placeholder*='name' i]",
                                        "input[placeholder*='nom' i]"], timeout=40)
        if not name_input:
            raise RuntimeError("champ nom introuvable")
        await name_input.fill(name)
        if passcode:
            pwd = await _find(page, ["#input-for-pwd", "input[type='password']"])
            if pwd:
                await pwd.fill(passcode)
        await _click(page, ["button.preview-join-button", *_btn("^Join$", "^Rejoindre$", "^Participer$")], timeout=20)
        await _click(page, _btn("^I Agree$", "^J'accepte$"), timeout=3)
        deadline = time.monotonic() + 3600
        audio_done = False
        while time.monotonic() < deadline:
            if not audio_done and await _click(page, ["button.join-audio-by-voip__join-btn",
                                                      *_btn("Join Audio by Computer", "Join with Computer Audio",
                                                            "Rejoindre l'audio par ordinateur")]):
                audio_done = True
            if await _find(page, Zoom.LEAVE):
                if not audio_done:  # dernière chance pour l'audio
                    await _click(page, _btn("Join Audio by Computer", "Join with Computer Audio"), timeout=5)
                return
            if await _find(page, Zoom.WAITING):
                set_state("lobby")
            await asyncio.sleep(2)
        raise RuntimeError("pas admis dans la réunion")

    @staticmethod
    async def in_meeting(page: Page) -> bool:
        # la barre d'outils Zoom se masque : on la fait réapparaître
        try:
            await page.mouse.move(400, 500)
            await page.mouse.move(420, 520)
        except Exception:
            pass
        return await _find(page, Zoom.LEAVE, timeout=2) is not None

    @staticmethod
    async def leave(page: Page):
        if await _click(page, Zoom.LEAVE, timeout=3):
            await _click(page, _btn("Leave Meeting", "Quitter la réunion"), timeout=3)

    @staticmethod
    async def toggle_mute(page: Page):
        await page.keyboard.press("Alt+A")


# --------------------------------------------------------------------------- Autre webconf
class Generic:
    """Webconf quelconque (Google Meet, Jitsi, Webex, BigBlueButton, Whereby, GoTo, Livestorm…).

    Heuristiques : cookies → nom → code → bouton « Rejoindre » → audio par l'ordinateur, puis
    détection d'un bouton « Quitter / Raccrocher ». Si rien n'est reconnu, la page reste ouverte et
    on termine à la main via l'écran distant ; l'enregistrement démarre quand même.
    """
    COOKIES = ["#onetrust-accept-btn-handler", "#didomi-notice-agree-button", "#axeptio_btn_acceptAll",
               "#tarteaucitronPersonalize2", "button[id*='accept' i][id*='cookie' i]",
               *_btn("^Accept all", "^Accept cookies", "^Accept$", "^Tout accepter", "^Accepter",
                     "^J'accepte", "^I agree", "^Agree$", "^OK$", "^Got it")]
    NAME = ["#premeeting-name-input",                                    # Jitsi
            "input[aria-label='Your name' i]", "input[aria-label*='Votre nom' i]",  # Google Meet
            "input[autocomplete='name']", "input[name='name' i]", "input[name*='display' i]",
            "input[id*='name' i]:not([type='hidden']):not([type='search'])",
            "input[aria-label*='name' i]", "input[aria-label*='nom' i]",
            "input[placeholder*='name' i]", "input[placeholder*='nom' i]", "input[placeholder*='pseudo' i]"]
    PASS = ["input[type='password']", "input[name*='pass' i]", "input[placeholder*='code' i]",
            "input[placeholder*='password' i]", "input[placeholder*='mot de passe' i]"]
    JOIN = ["[data-testid='prejoin.joinMeeting']",                       # Jitsi
            *_btn("^Join now", "^Ask to join", "^Join meeting", "^Join the meeting", "^Join room",
                  "^Join as a guest", "^Join as guest", "^Join$", "^Join call", "^Enter room", "^Enter$",
                  "^Rejoindre", "^Demander à participer", "^Participer", "^Entrer", "^Accéder à la réunion"),
            "input[type='submit'][value*='join' i]", "input[type='submit'][value*='rejoindre' i]"]
    AUDIO = [*_btn("^Microphone$", "^Micro$",                            # BigBlueButton : choix audio
                   "Use computer audio", "Use computer for audio", "Join with computer audio",
                   "Join audio", "Computer audio", "Audio de l'ordinateur", "Utiliser l'audio de l'ordinateur"),
             "button[aria-label='Echo is audible' i]", "button[aria-label*=\"l'écho est audible\" i]",  # BBB
             *_btn("^Got it", "^Dismiss", "^Compris", "^Fermer$")]
    LEAVE = ["button[aria-label*='Leave call' i]", "button[aria-label*='Quitter l' i]",       # Meet
             "[aria-label*='hang up' i]", "[aria-label*='hangup' i]", "[aria-label*='raccrocher' i]",
             "button[aria-label*='leave' i]", "button[aria-label*='quitter' i]",
             "button[aria-label*='end call' i]", "button[aria-label*='leave meeting' i]",
             "button[data-testid*='leave' i]", "button[data-test*='leave' i]", "[class*='hangup' i] button",
             *_btn("^Leave", "^Quitter", "^Hang up", "^Raccrocher", "^End call")]
    LOBBY = ["text=/asking to join|ask to join\\.\\.\\.|someone in the call|let you in|waiting for the (host|moderator|organizer)|not yet started|no moderators|en attente du modérateur"
             "|waiting room|lobby|salle d'attente|en attente (de|d'admission|du modérateur|de l'hôte)|vous laisser entrer"
             "|guest approval|attente d'approbation/i"]
    MUTE = ["button[aria-label*='mute' i]", "button[aria-label*='microphone (' i]",
            "button[aria-label*='turn off microphone' i]", "button[aria-label*='turn on microphone' i]",
            "button[aria-label*='désactiver le micro' i]", "button[aria-label*='activer le micro' i]",
            "button[aria-label*='micro' i]", "[data-testid*='mute' i]", "[data-tid*='microphone' i]"]

    @staticmethod
    def prepare_url(url: str, name: str) -> str:
        u = urlparse(url)
        if "jit" in (u.hostname or "") and not u.fragment:  # Jitsi : nom passé dans l'URL
            return url + f'#userInfo.displayName="{name}"'
        return url

    @staticmethod
    async def join(page: Page, url: str, name: str, set_state, passcode: str | None = None) -> bool:
        """Renvoie True si la barre de réunion est reconnue, False si on est entré « à l'aveugle »."""
        await page.goto(Generic.prepare_url(url, name), wait_until="domcontentloaded", timeout=45000)
        await asyncio.sleep(3)
        await _click(page, Generic.COOKIES, timeout=4)
        start = time.monotonic()
        name_done = pass_done = False
        clicks = 0
        quiet_since = None
        while time.monotonic() - start < 3600:
            if not name_done:
                inp = await _find(page, Generic.NAME)
                if inp:
                    try:
                        if (await inp.input_value()) != name:
                            await inp.fill(name)
                        name_done = True
                    except Exception:
                        pass
            if passcode and not pass_done:
                pw = await _find(page, Generic.PASS)
                if pw:
                    try:
                        await pw.fill(passcode)
                        pass_done = True
                    except Exception:
                        pass
            # « Rejoindre » d'abord : certaines pages d'accueil (Jitsi) affichent déjà un bouton raccrocher
            if clicks < 8 and await _click(page, Generic.JOIN):
                clicks += 1
                quiet_since = None
                await asyncio.sleep(3)
                continue
            if await _find(page, Generic.LOBBY):
                set_state("lobby")
                quiet_since = None
                await asyncio.sleep(2)
                continue
            if await _find(page, Generic.LEAVE) and not await _find(page, Generic.JOIN):
                await _click(page, Generic.AUDIO, timeout=4)  # BBB : « Microphone » puis test d'écho
                await _click(page, Generic.AUDIO, timeout=8)
                return True
            await _click(page, Generic.AUDIO)
            # plus rien à cliquer, pas de salle d'attente : on est probablement dans la réunion
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since > (40 if clicks else 90):
                log.info("generic: interface non reconnue (clics=%s), on enregistre quand même", clicks)
                return False
            await asyncio.sleep(2)
        raise RuntimeError("pas admis dans la réunion")

    @staticmethod
    async def in_meeting(page: Page) -> bool:
        try:
            await page.mouse.move(800, 600)
            await page.mouse.move(820, 620)
        except Exception:
            pass
        return await _find(page, Generic.LEAVE, timeout=2) is not None

    @staticmethod
    async def leave(page: Page):
        if await _click(page, Generic.LEAVE, timeout=3):
            await _click(page, _btn("^Leave meeting", "^Leave call", "^Leave$", "^Quitter la réunion",
                                    "^Quitter$", "^Yes", "^Oui"), timeout=3)

    @staticmethod
    async def toggle_mute(page: Page):
        if not await _click(page, Generic.MUTE, timeout=2):
            await page.keyboard.press("Control+D")  # Google Meet ; Jitsi = « m »


# --------------------------------------------------------------------------- orateur actif
# Renvoie les noms affichés comme « en train de parler ». Exécuté ~4 fois/s dans chaque frame.
SPEAKER_JS = r"""() => {
  const out = new Set();
  const clean = (t) => (t || "").split("\n")[0].replace(/,?\s*(is )?(speaking|talking|en train de parler|parle)\b.*$/i, "").trim();
  const add = (t) => { t = clean(t); if (t && t.length < 80) out.add(t); };
  const nameIn = (el) => {
    if (!el) return null;
    const n = el.querySelector('[data-tid*="display-name" i], [class*="displayname" i], [class*="display-name" i], ' +
      '[class*="avatar-footer" i], [class*="participant-name" i], [class*="name-label" i], [data-self-name], [data-participant-name]');
    return (n && (n.getAttribute("data-participant-name") || n.innerText)) || el.getAttribute("aria-label") || null;
  };
  // Jitsi : état interne de l'application (fiable)
  try {
    if (window.APP && APP.store) {
      const p = APP.store.getState()["features/base/participants"];
      const id = p && p.dominantSpeaker;
      const r = id && p.remote && (p.remote.get ? p.remote.get(id) : p.remote[id]);
      if (r && r.name) add(r.name);
      return [...out];
    }
  } catch (e) {}
  // Zoom (client web) : cadre de l'orateur actif (vue Intervenant) ou vignette active (vue Galerie)
  document.querySelectorAll('.speaker-active-container__video-frame, .speaker-bar-container__video-frame--active, ' +
    '.gallery-video-container__video-frame--active, [class*="video-frame--active"]').forEach(el => {
      const n = el.querySelector('.video-avatar__avatar-footer, [class*="avatar-footer"], [class*="avatar-name"]');
      if (n) add(n.innerText);
  });
  // Teams : contour du niveau de voix visible autour de la vignette
  document.querySelectorAll('[data-tid="voice-level-stream-outline"]').forEach(o => {
    const st = getComputedStyle(o);
    const on = (o.className || "").toString().match(/speaking|active|vdi-frame-occlusion/i) || (st.opacity !== "0" && st.visibility !== "hidden" && st.borderStyle !== "none" && st.display !== "none" && o.offsetWidth > 0 && parseFloat(st.borderWidth) > 0);
    if (!on) return;
    const tile = o.closest('[data-tid*="participant" i], [data-cid*="participant" i], [data-stream-type], [role="menuitem"], [aria-label]');
    add(nameIn(tile));
  });
  // Générique (Teams, Meet, Webex, BBB…) : attributs « en train de parler »
  document.querySelectorAll('[data-is-speaking="true"], [data-speaking="true"], [data-audio-active="true"], ' +
    '[class*="is-speaking" i], [class*="isSpeaking"], [class*="active-speaker" i], [class*="activeSpeaker"], ' +
    '[class*="dominant-speaker" i], [aria-label*="speaking" i], [aria-label*="en train de parler" i]').forEach(el => {
      if (el.offsetWidth === 0 && el.offsetHeight === 0) return;
      const t = el.getAttribute("aria-label");
      add(t && /speaking|parler/i.test(t) ? t : nameIn(el.closest('[data-participant-id], [data-tid*="participant" i], [class*="tile" i], [class*="participant" i]') || el));
  });
  return [...out];
}"""

# Diagnostic : éléments candidats pour adapter les sélecteurs à une nouvelle version d'interface
SPEAKER_DEBUG_JS = r"""() => [...document.querySelectorAll("*")].filter(el => {
  const a = [...el.attributes].map(x => x.name + "=" + x.value).join(" ");
  return /speak|talk|dominant|voice-level|audio-level|parle/i.test(a);
}).slice(0, 60).map(el => ({tag: el.tagName, cls: (el.className || "").toString().slice(0, 120),
  tid: el.getAttribute("data-tid") || el.getAttribute("data-testid"), aria: el.getAttribute("aria-label"),
  visible: el.offsetWidth > 0, text: (el.innerText || "").slice(0, 60)}))"""

NAME_SUFFIX = re.compile(r"\s*\((guest|invité|invitée|unverified|non vérifié|external|externe|host|hôte|co-host|"
                         r"co-hôte|me|moi|you|vous|organizer|organisateur)\)\s*$", re.I)


def clean_name(n: str) -> str:
    n = (n or "").strip()
    while NAME_SUFFIX.search(n):
        n = NAME_SUFFIX.sub("", n).strip()
    return n


async def active_speakers(page: Page) -> list[str]:
    names = set()
    for frame in page.frames:
        try:
            for n in await frame.evaluate(SPEAKER_JS) or []:
                n = clean_name(n)
                if n:
                    names.add(n)
        except Exception:
            pass
    return sorted(names)


PLATFORMS = {"teams": Teams, "zoom": Zoom, "web": Generic}
