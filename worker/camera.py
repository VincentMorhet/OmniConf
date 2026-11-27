"""Caméra « image » de l'assistant (27/11) : une fausse caméra qui montre une image fixe (celle du compte qui lance la
réunion, sinon une carte avec le nom de l'assistant) au lieu d'une vidéo.

Elle n'est JAMAIS allumée par l'assistant : il entre toujours sans caméra, exactement comme avant (pendant l'entrée, la
page ne voit aucune caméra). Une fois l'entrée confirmée, la caméra « OmniConf (image) » apparaît dans la liste des
périphériques de la réunion (événement « devicechange ») ; c'est l'utilisateur qui l'allume, via « Prendre la main » et le
bouton caméra de Teams, Zoom ou de la webconf. Sortie de réunion : elle disparaît, l'image est oubliée.

Mécanisme : un script injecté dans chaque page remplace getUserMedia / enumerateDevices ; l'image (JPEG 1280×720 en
base64, préparée par l'IHM) vient de l'assistant par une liaison Playwright (pas de requête réseau : aucune politique
CSP de la page ne s'y oppose). La piste vidéo est celle d'un canevas repeint 5 fois par seconde.
"""
import logging

log = logging.getLogger("camera")

W, H = 1280, 720
MAX_IMAGE_B64 = 3_000_000          # ~2,2 Mo de JPEG

CAM_JS = r"""
(() => {
  const MD = window.MediaDevices, md = navigator.mediaDevices;
  if (!MD || !md || !MD.prototype.getUserMedia || MD.prototype.__omniCam) return;
  try { Object.defineProperty(MD.prototype, "__omniCam", { value: true }); } catch (e) { return; }
  const ID = "omniconf-image-camera", GROUP = "omniconf-image", LABEL = "OmniConf (image)", W = %W%, H = %H%;
  const gum = MD.prototype.getUserMedia, enumerate = MD.prototype.enumerateDevices;
  const ask = async (what) => {
    try { return typeof window.__omniCam === "function" ? await window.__omniCam(what) : null; } catch (e) { return null; }
  };
  const caps = () => ({ deviceId: ID, groupId: GROUP, width: { min: 1, max: W }, height: { min: 1, max: H },
    frameRate: { min: 1, max: 30 }, aspectRatio: { min: 1 / H, max: W }, facingMode: ["user"],
    resizeMode: ["none", "crop-and-scale"] });
  const proto = (window.InputDeviceInfo || window.MediaDeviceInfo || Object).prototype;
  const device = () => Object.create(proto, {
    deviceId: { value: ID, enumerable: true }, kind: { value: "videoinput", enumerable: true },
    label: { value: LABEL, enumerable: true }, groupId: { value: GROUP, enumerable: true },
    getCapabilities: { value: caps },
    toJSON: { value: () => ({ deviceId: ID, kind: "videoinput", label: LABEL, groupId: GROUP }) } });

  MD.prototype.enumerateDevices = async function () {
    const list = await enumerate.call(this);
    return (await ask("on")) ? [...list.filter(d => d.deviceId !== ID), device()] : list;
  };

  function card(g, name) {               // sans image choisie : carte avec le nom de l'assistant
    const grd = g.createLinearGradient(0, 0, W, H);
    grd.addColorStop(0, "#4f46e5"); grd.addColorStop(1, "#7c3aed");
    g.fillStyle = grd; g.fillRect(0, 0, W, H);
    g.fillStyle = "#fff"; g.textAlign = "center"; g.textBaseline = "middle";
    g.font = "600 64px sans-serif";
    const full = name || "Assistant";
    let t = full;
    while (t.length > 4 && g.measureText(t === full ? t : t + "…").width > W - 120) t = t.slice(0, -1);
    if (t !== full) t = t.trimEnd() + "…";
    g.fillText(t, W / 2, H / 2 - 24);
    g.globalAlpha = 0.8; g.font = "400 34px sans-serif"; g.fillText("Assistant OmniConf", W / 2, H / 2 + 56);
    g.globalAlpha = 1;
  }

  function imageTrack(info) {
    const cv = document.createElement("canvas"); cv.width = W; cv.height = H;
    const g = cv.getContext("2d");
    let bmp = null;
    const paint = () => { if (bmp) g.drawImage(bmp, 0, 0, W, H); else card(g, info.name); };
    if (info.img) {
      try {
        const bin = atob(info.img), u = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
        createImageBitmap(new Blob([u], { type: "image/jpeg" })).then(b => { bmp = b; paint(); }, () => {});
      } catch (e) {}
    }
    paint();
    const track = cv.captureStream(15).getVideoTracks()[0];
    // image fixe, mais un flux continu pour l'encodeur : on repeint tant qu'une piste (ou un clone) est active
    const live = new Set([track]), clone = track.clone.bind(track);
    const timer = setInterval(() => {
      for (const t of live) if (t.readyState !== "live") live.delete(t);
      if (!live.size) { clearInterval(timer); return; }
      paint();
    }, 200);
    const def = (k, v) => { try { Object.defineProperty(track, k, { value: v, configurable: true }); } catch (e) {} };
    def("label", LABEL);
    def("clone", () => { const c = clone(); live.add(c); return c; });
    def("getSettings", () => ({ deviceId: ID, groupId: GROUP, width: W, height: H, frameRate: 15, aspectRatio: W / H,
                                facingMode: "user", resizeMode: "none" }));
    def("getCapabilities", caps);
    def("getConstraints", () => ({ deviceId: ID }));
    def("applyConstraints", async () => {});
    return track;
  }

  MD.prototype.getUserMedia = async function (c) {
    if (!c || !c.video) return gum.call(this, c);
    const info = await ask("info");
    if (!info || !info.on) return gum.call(this, c);           // pas encore en réunion : comme sans caméra
    const out = new MediaStream();
    if (c.audio) (await gum.call(this, { audio: c.audio })).getTracks().forEach(t => out.addTrack(t));
    out.addTrack(imageTrack(info));
    return out;
  };
})();
""".replace("%W%", str(W)).replace("%H%", str(H))


class ImageCamera:
    """État de la caméra « image » : disponible (réunion confirmée) ou non, image du compte, nom de l'assistant."""

    def __init__(self):
        self.ready = False
        self.img: str | None = None      # JPEG en base64, 1280×720
        self.name: str | None = None

    def reset(self):
        self.ready, self.img, self.name = False, None, None

    def set(self, img: str | None, name: str | None):
        self.ready = False
        self.img = img if img and len(img) <= MAX_IMAGE_B64 else None
        self.name = name

    def _binding(self, source, what="on"):
        if what == "info":
            return {"on": self.ready, "img": self.img if self.ready else None, "name": self.name}
        return self.ready

    async def install(self, ctx):
        """À l'ouverture du navigateur : liaison (avant le script, qui l'appelle) puis script dans chaque page."""
        await ctx.expose_binding("__omniCam", self._binding)
        await ctx.add_init_script(CAM_JS)

    async def make_ready(self, page):
        """Entrée en réunion confirmée : la caméra « OmniConf (image) » apparaît (éteinte) dans la réunion."""
        if self.ready:
            return
        self.ready = True
        for f in page.frames:
            try:
                await f.evaluate("() => navigator.mediaDevices && navigator.mediaDevices.dispatchEvent(new Event('devicechange'))")
            except Exception:
                pass
        log.info("caméra « image » disponible (éteinte) : %s", "image du compte" if self.img else "carte avec le nom")
