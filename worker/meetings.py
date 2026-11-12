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


def unwrap_url(url: str) -> str:
    """Lien réel derrière un lien de protection (Outlook « safelinks ») ou une redirection Google."""
    for _ in range(3):
        u = urlparse(url)
        host = (u.hostname or "").lower()
        q = parse_qs(u.query)
        if host.endswith("safelinks.protection.outlook.com") and q.get("url"):
            url = q["url"][0]
        elif (host.startswith("www.google.") or host.startswith("google.")) and u.path == "/url" and (q.get("q") or q.get("url")):
            url = (q.get("q") or q.get("url"))[0]
        else:
            break
    return url


def detect_platform(url: str) -> str:
    host = urlparse(unwrap_url(url)).hostname or ""
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
    """Clique sur le premier élément visible ; réessaie jusqu'à `timeout` si le clic échoue (élément
    recouvert par une fenêtre, encore désactivé…)."""
    deadline = time.monotonic() + timeout
    while True:
        loc = await _find(page, selectors, max(0.0, deadline - time.monotonic()))
        if loc:
            try:
                await loc.click(timeout=3000)
                return True
            except Exception as e:
                log.debug("click failed %s", e)
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.5)


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
    # Teams « light meetings » (nov. 2026) : sans caméra, une fenêtre « Are you sure you don't want audio or
    # video? » recouvre la pré-connexion et bloque « Join now ». La fermer garde l'audio de l'ordinateur
    # (micro VirtualMic + haut-parleur), vérifié sur la page réelle.
    NO_MEDIA = _btn("Continue without audio or video", "Continuer sans audio ni vidéo", "Continuer sans audio ou vidéo")
    JOIN = ["button[data-tid='prejoin-join-button']", "#prejoin-join-button",
            *_btn("^Join now", "^Rejoindre maintenant", "^Join$", "^Rejoindre$")]

    @staticmethod
    async def join(page: Page, url: str, name: str, set_state):
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        names = ["input[data-tid='prejoin-display-name-input']", "input[placeholder*='name' i]",
                 "input[placeholder*='nom' i]"]
        # Page "Comment voulez-vous rejoindre ?" -> rester dans le navigateur ; Teams l'affiche de moins en
        # moins (pré-connexion directe) : on n'attend pas si la pré-connexion est déjà là
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if await _click(page, _btn("Continue on this browser", "Join on the web", "Use the web app",
                                       "Continuer sur ce navigateur", "Participer sur le web")) or await _find(page, names):
                break
            await asyncio.sleep(0.5)
        # Écran de pré-connexion
        name_input = await _find(page, names, timeout=40)
        await _click(page, Teams.NO_MEDIA, timeout=3)
        if name_input:
            await name_input.fill(name)
        # Couper la caméra si elle est active (pas de caméra sur la VM de toute façon)
        await _click(page, ["[data-tid='toggle-video'][aria-checked='true']",
                            "input[title*='camera' i][aria-checked='true']"])
        deadline = time.monotonic() + 30
        while True:
            await _click(page, Teams.NO_MEDIA)          # la fenêtre peut s'afficher avec un peu de retard
            if await _click(page, Teams.JOIN, timeout=2):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("bouton 'Join now' introuvable ou inaccessible")
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
# Fonctions communes aux scripts ci-dessous (insérées en tête de chacun)
_JS_COMMON = r"""
  const STATUS = /^(mute[d]?|unmute[d]?|micro.*|mic.*|vid[ée]o.*|cam[ée]ra.*|more|plus|…|\.\.\.|\d+|pin|épingler|spotlight|raise hand|.*hand raised|main lev[ée]e.*|you|vous|me|moi|host|h[ôo]te|guest|invit[ée]e?)$/i;
  const clean = (t) => (t || "").split("\n").map(s => s.trim()).find(s => s && s.length < 80 && !STATUS.test(s)) || "";
  const label = (t) => clean((t || "").replace(/,?\s*(is )?(speaking|talking|en train de parler|parle)\b.*$/i, "").split(",")[0]);
  const NAME_SEL = '[data-tid*="display-name" i], [class*="displayname" i], [class*="display-name" i], [class*="displayName"], ' +
    '[class*="avatar-footer" i], [class*="participant-name" i], [class*="name-label" i], [data-self-name], [data-participant-name]';
  const nameIn = (el) => {
    if (!el) return null;
    const n = el.querySelector(NAME_SEL);
    return (n && (n.getAttribute("data-participant-name") || clean(n.innerText))) || label(el.getAttribute("aria-label")) || null;
  };
  // Nom d'une vignette à partir d'un élément intérieur (contour de voix…) : on remonte les ancêtres sans
  // dépasser la vignette (un ancêtre qui contient plusieurs vignettes donnerait le nom d'un autre)
  const tileName = (el, marker) => {
    let a = el;
    for (let i = 0; i < 9 && a && a !== document.body; i++, a = a.parentElement) {
      if (i && marker && a.querySelectorAll(marker).length > 1) return null;
      const n = a.querySelector && a.querySelector(NAME_SEL);
      const v = (n && (n.getAttribute("data-participant-name") || clean(n.innerText))) || label(a.getAttribute && a.getAttribute("aria-label"));
      if (v) return v;
      if (i >= 1) { const txt = clean(a.innerText); if (txt) return txt; }
    }
    return null;
  };
  const visible = (el) => el.offsetWidth > 0 || el.offsetHeight > 0;
"""

SPEAKER_JS = r"""() => {
  const out = new Set();""" + _JS_COMMON + r"""
  const add = (t) => { t = label(t); if (t && t.length < 80) out.add(t); };
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
  // Zoom, partage d'écran en cours (bandeau des vignettes) : indicateur de niveau audio dans une vignette
  document.querySelectorAll('.audio-level-indicator, [class*="audio-level-indicator"]').forEach(el => {
    if (!visible(el) || !el.closest('[class*="video-frame"]')) return;
    add(tileName(el, '[class*="video-frame"]'));
  });
  // Teams : contour du niveau de voix visible autour de la vignette
  document.querySelectorAll('[data-tid="voice-level-stream-outline"]').forEach(o => {
    const st = getComputedStyle(o);
    const on = (o.className || "").toString().match(/speaking|active|vdi-frame-occlusion/i) || (st.opacity !== "0" && st.visibility !== "hidden" && st.borderStyle !== "none" && st.display !== "none" && o.offsetWidth > 0 && parseFloat(st.borderWidth) > 0);
    if (!on) return;
    add(tileName(o, '[data-tid="voice-level-stream-outline"]'));
  });
  // Générique (Teams, Meet, Webex, BBB…) : attributs « en train de parler »
  document.querySelectorAll('[data-is-speaking="true"], [data-speaking="true"], [data-audio-active="true"], ' +
    '[class*="is-speaking" i], [class*="isSpeaking"], [class*="active-speaker" i], [class*="activeSpeaker"], ' +
    '[class*="dominant-speaker" i], [aria-label*="speaking" i], [aria-label*="en train de parler" i]').forEach(el => {
      if (!visible(el)) return;
      const t = el.getAttribute("aria-label");
      add(t && /speaking|parler/i.test(t) ? t : nameIn(el.closest('[data-participant-id], [data-tid*="participant" i], [class*="tile" i], [class*="participant" i]') || el));
  });
  return [...out];
}"""

# Participants visibles (vignettes, liste des participants si elle est ouverte) : relevé toutes les ~10 s
PARTICIPANTS_JS = r"""() => {
  const out = new Set();""" + _JS_COMMON + r"""
  const add = (t) => { t = label(t); if (t && t.length < 80) out.add(t); };
  // Zoom : pied de vignette, bandeau, galerie, liste des participants
  document.querySelectorAll('.video-avatar__avatar-footer, .participants-item__display-name').forEach(el => { if (visible(el)) add(el.innerText); });
  document.querySelectorAll('.speaker-bar-container__video-frame, .gallery-video-container__video-frame, .speaker-active-container__video-frame').forEach(el => {
    if (visible(el) && !el.querySelector('.video-avatar__avatar-footer')) add(el.innerText);
  });
  // Teams : une vignette par contour de voix ; liste « Personnes » si elle est ouverte
  document.querySelectorAll('[data-tid="voice-level-stream-outline"]').forEach(o => add(tileName(o, '[data-tid="voice-level-stream-outline"]')));
  document.querySelectorAll('[data-tid^="participantsInCall"] [data-tid*="name" i], [data-cid="roster-participant"] [class*="name" i]').forEach(el => { if (visible(el)) add(el.innerText); });
  // Générique (Meet, Jitsi…) : attributs explicites
  document.querySelectorAll('[data-participant-name], [data-self-name], [data-tid*="display-name" i]').forEach(el => {
    if (visible(el)) add(el.getAttribute("data-participant-name") || el.getAttribute("data-self-name") || el.innerText);
  });
  try {  // Jitsi
    if (window.APP && APP.store) {
      const p = APP.store.getState()["features/base/participants"];
      const r = p && p.remote;
      (r ? (r.values ? [...r.values()] : Object.values(r)) : []).forEach(x => x && x.name && add(x.name));
      const me = p && p.local; if (me && me.name) add(me.name);
    }
  } catch (e) {}
  return [...out];
}"""

# Diagnostic : éléments candidats pour adapter les sélecteurs à une nouvelle version d'interface,
# avec leurs ancêtres (pour retrouver où la plateforme affiche le nom de la vignette) et leur style
SPEAKER_DEBUG_JS = r"""() => [...document.querySelectorAll("*")].filter(el => {
  const a = [...el.attributes].map(x => x.name + "=" + x.value).join(" ");
  return /speak|talk|dominant|voice-level|audio-level|parle/i.test(a);
}).slice(0, 40).map(el => {
  const st = getComputedStyle(el);
  const up = [];
  for (let a = el.parentElement, i = 0; a && a !== document.body && i < 8; a = a.parentElement, i++)
    up.push({tag: a.tagName, cls: (a.className || "").toString().slice(0, 80), tid: a.getAttribute("data-tid") || a.getAttribute("data-testid"),
             cid: a.getAttribute("data-cid"), role: a.getAttribute("role"), aria: (a.getAttribute("aria-label") || "").slice(0, 100),
             text: (a.innerText || "").slice(0, 80)});
  return {tag: el.tagName, cls: (el.className || "").toString().slice(0, 160),
    tid: el.getAttribute("data-tid") || el.getAttribute("data-testid"), aria: el.getAttribute("aria-label"),
    visible: el.offsetWidth > 0, text: (el.innerText || "").slice(0, 60),
    style: {border: st.borderStyle + " " + st.borderWidth + " " + st.borderColor, opacity: st.opacity, shadow: (st.boxShadow || "").slice(0, 60)},
    up};
})"""

NAME_SUFFIX = re.compile(r"\s*\((guest|invité|invitée|unverified|non vérifié|external|externe|host|hôte|co-host|"
                         r"co-hôte|me|moi|you|vous|organizer|organisateur)\)\s*$", re.I)


def clean_name(n: str) -> str:
    n = (n or "").strip()
    while NAME_SUFFIX.search(n):
        n = NAME_SUFFIX.sub("", n).strip()
    return n


ORG_PAREN = re.compile(r"^(?P<name>.+?)\s*[(\[](?P<org>[^()\[\]]{2,60})[)\]]\s*$")
ORG_DASH = re.compile(r"^(?P<name>[^-–|]{3,60}?)\s+[-–|]\s+(?P<org>.{2,60})$")


def split_org(n: str) -> tuple[str, str | None]:
    """« Prénom NOM (Société / Ville) » ou « Prénom NOM - Société » -> (nom, organisation)."""
    n = clean_name(n)
    for rx in (ORG_PAREN, ORG_DASH):
        m = rx.match(n)
        if m:
            return m.group("name").strip(), m.group("org").strip()
    return n, None


async def visible_participants(page: Page) -> list[str]:
    names = set()
    for frame in page.frames:
        try:
            for n in await frame.evaluate(PARTICIPANTS_JS) or []:
                n = clean_name(n)
                if n:
                    names.add(n)
        except Exception:
            pass
    return sorted(names)


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
