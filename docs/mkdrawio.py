"""Génère docs/architecture.drawio (3 pages) : vue d'ensemble, circuit du son, compte rendu."""
import sys
from xml.sax.saxutils import escape

OUT = sys.argv[1] if len(sys.argv) > 1 else "docs/architecture.drawio"

BLUE = "fillColor=#DCE6F0;strokeColor=#2F5597;"
GREEN = "fillColor=#E2F0D9;strokeColor=#548235;"
YELLOW = "fillColor=#FFF2CC;strokeColor=#BF9000;"
ORANGE = "fillColor=#FCE4D6;strokeColor=#C55A11;"
GREY = "fillColor=#F5F5F5;strokeColor=#7F7F7F;"
BOX = "rounded=1;whiteSpace=wrap;html=1;arcSize=8;fontSize=12;verticalAlign=middle;"
GROUP = "rounded=1;whiteSpace=wrap;html=1;arcSize=3;dashed=1;fontSize=13;fontStyle=1;verticalAlign=top;align=left;spacingLeft=8;fillColor=none;"
EDGE = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=11;endArrow=block;endFill=1;labelBackgroundColor=#FFFFFF;"
DASH = EDGE + "dashed=1;"
BOTH = EDGE + "startArrow=block;startFill=1;"


class Page:
    def __init__(self, name, pid):
        self.name, self.pid, self.cells, self.n = name, pid, [], 0

    def _id(self):
        self.n += 1
        return f"{self.pid}{self.n}"

    def box(self, label, x, y, w, h, style=BLUE, base=BOX, parent="1"):
        i = self._id()
        self.cells.append(f'<mxCell id="{i}" value="{escape(label, {chr(34): "&quot;"})}" style="{base}{style}" '
                          f'vertex="1" parent="{parent}"><mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>')
        return i

    def group(self, label, x, y, w, h, style="strokeColor=#7F7F7F;"):
        return self.box(label, x, y, w, h, style, GROUP)

    def edge(self, a, b, label="", style=EDGE, extra="", pts=()):
        i = self._id()
        geo = ('<mxGeometry relative="1" as="geometry"><Array as="points">'
               + "".join(f'<mxPoint x="{x}" y="{y}"/>' for x, y in pts) + '</Array></mxGeometry>') if pts \
            else '<mxGeometry relative="1" as="geometry"/>'
        self.cells.append(f'<mxCell id="{i}" value="{escape(label, {chr(34): "&quot;"})}" style="{style}{extra}" edge="1" '
                          f'parent="1" source="{a}" target="{b}">{geo}</mxCell>')
        return i

    def xml(self):
        body = "".join(self.cells)
        return (f'<diagram name="{escape(self.name)}" id="{self.pid}"><mxGraphModel dx="1400" dy="900" grid="1" '
                f'gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
                f'pageWidth="1654" pageHeight="1169" math="0" shadow="0"><root><mxCell id="0"/>'
                f'<mxCell id="1" parent="0"/>{body}</root></mxGraphModel></diagram>')


def title(p, text, sub):
    p.box(f"<b>{text}</b><br/><font style='font-size:12px' color='#595959'>{sub}</font>", 40, 20, 1100, 50,
          "fillColor=none;strokeColor=none;", "text;html=1;align=left;verticalAlign=middle;fontSize=20;")


# ---------- Page 1 : vue d'ensemble ----------
p1 = Page("1. Vue d'ensemble", "ov")
title(p1, "Omniconf : vue d'ensemble", "3 VM Proxmox · le contrôle pilote, les workers rejoignent et enregistrent")
p1.group("👤 Toi", 40, 110, 200, 300)
pc = p1.box("💻 <b>PC</b><br/>navigateur", 70, 150, 140, 70, GREY)
ip = p1.box("📱 <b>iPad / iPhone</b><br/>navigateur (PWA)", 70, 250, 140, 70, GREY)

p1.group("pve-01", 330, 110, 300, 300, "strokeColor=#2F5597;")
ctl = p1.box("<b>webconf-ctl</b> · 192.168.0.230<hr/>Caddy (HTTPS 443, TLS interne)<br/>↓<br/>FastAPI :8000<br/>"
             "IHM · planification · choix du worker<br/>relais WebRTC (signalisation) et noVNC<br/>liste / lecture des MP4",
             350, 150, 260, 220, BLUE)

p1.group("pve-02", 720, 90, 340, 210, "strokeColor=#548235;")
w1 = p1.box("<b>webconf-w1</b> · 192.168.0.231<hr/>agent FastAPI :8001 · noVNC :6080<br/>Chrome (Playwright) + Xvfb<br/>"
            "PulseAudio virtuel · ffmpeg<br/>transcription · intervenants<br/>points d'étape · compte rendu",
            740, 125, 300, 155, GREEN)
p1.group("pve-03", 720, 330, 340, 130, "strokeColor=#548235;")
w2 = p1.box("<b>webconf-w2</b> · 192.168.0.232<hr/>identique à w1<br/>(2ᵉ réunion en parallèle)", 740, 365, 300, 75, GREEN)

p1.group("☁️ Internet", 1150, 90, 330, 370, "strokeColor=#BF9000;")
wc = p1.box("<b>Webconférences</b><br/>Teams · Zoom · Google Meet<br/>Jitsi · Webex · BigBlueButton…", 1175, 130, 280, 90, YELLOW)
ovh = p1.box("<b>OVH AI Endpoints</b> (France)<hr/>Whisper large v3 (voix → texte)<br/>Qwen3.8-27B · Qwen3.5-397B<br/>GPT-OSS-120B",
             1175, 290, 280, 130, YELLOW)

p1.edge(pc, ctl, "HTTPS")
p1.edge(ip, ctl, "HTTPS")
p1.edge(ctl, w1, "API + AGENT_TOKEN", extra="exitX=1;exitY=0.3;entryX=0;entryY=0.5;")
p1.edge(ctl, w2, "API + AGENT_TOKEN", extra="exitX=1;exitY=0.8;entryX=0;entryY=0.5;")
p1.edge(w1, wc, "client web", BOTH)
p1.edge(w2, wc, "", BOTH, "exitX=1;exitY=0.3;entryX=0;entryY=0.8;")
p1.edge(w1, ovh, "son, texte", extra="exitX=1;exitY=0.8;entryX=0;entryY=0.3;")
p1.edge(w2, ovh, "", extra="exitX=1;exitY=0.7;entryX=0;entryY=0.7;")
p1.edge(pc, w1, "🎧 audio WebRTC direct (UDP) · LAN ou Tailscale", BOTH + "dashed=1;strokeColor=#C55A11;fontColor=#C55A11;",
        "exitX=0.5;exitY=0;entryX=0.5;entryY=0;")
p1.box("<b>À retenir</b><br/>• ctl ne rejoint aucune réunion : il pilote.<br/>• 1 worker = 1 réunion à la fois.<br/>"
       "• Le son va directement de ton navigateur au worker, sans passer par ctl.<br/>"
       "• Les workers n'acceptent l'API (8001) et le VNC (6080) que depuis ctl (nftables).",
       40, 500, 560, 120, GREY, BOX + "align=left;spacingLeft=10;")

# ---------- Page 2 : circuit du son ----------
p2 = Page("2. Circuit du son (worker)", "au")
title(p2, "Intérieur d'un worker : le circuit du son", "PulseAudio virtuel · ta voix et celle des participants sont des flux séparés")
p2.group("Chrome (Playwright) sur Xvfb :99 · 1920x1200", 40, 110, 260, 380, "strokeColor=#2F5597;")
page = p2.box("<b>Page de la webconf</b><br/>(Teams, Zoom, Jitsi…)", 70, 240, 200, 90, BLUE)
pulse = p2.group("PulseAudio virtuel", 380, 110, 260, 380, "strokeColor=#C55A11;")
mo = p2.box("<b>meeting_out</b><br/>son de la réunion", 410, 150, 200, 60, ORANGE)
ui = p2.box("<b>user_in</b><br/>ta voix", 410, 280, 200, 60, ORANGE)
vm = p2.box("<b>virtmic</b><br/>micro vu par Chrome", 410, 400, 200, 60, ORANGE)
br = p2.box("<b>audio_bridge</b><br/>WebRTC", 780, 150, 180, 60, GREEN)
toi = p2.box("🎧🎙️ <b>ton navigateur</b>", 1080, 150, 200, 60, GREY)
rec = p2.box("<b>ffmpeg</b> · enregistrement<br/>écran 2 i/s + les 2 flux son<br/>→ MP4", 780, 330, 200, 70, GREEN)
tr = p2.box("<b>transcriber</b><br/>meeting_out → participants<br/>user_in → « Moi »<br/>silences → Whisper → Qwen", 780, 450, 200, 90, GREEN)
spk = p2.box("<b>speakers</b><br/>empreintes vocales<br/>+ noms affichés", 1080, 460, 200, 70, GREEN)
p2.edge(page, mo, "haut-parleur", extra="exitX=0.5;exitY=0;entryX=0;entryY=0.5;")
p2.edge(mo, br, "", extra="exitX=1;exitY=0.5;entryX=0;entryY=0.5;")
p2.edge(br, toi, "tu entends", extra="exitX=1;exitY=0.3;entryX=0;entryY=0.3;")
p2.edge(toi, br, "tu parles", extra="exitX=0;exitY=0.7;entryX=1;entryY=0.7;")
p2.edge(br, ui, "", extra="exitX=0.5;exitY=1;entryX=1;entryY=0.5;", pts=[(870, 310)])
p2.edge(ui, vm, "")
p2.edge(vm, page, "micro", extra="exitX=0;exitY=0.5;entryX=0.5;entryY=1;")
p2.edge(pulse, rec, "lit les 2 flux", DASH, "exitX=1;exitY=0.62;entryX=0;entryY=0.5;")
p2.edge(pulse, tr, "lit les 2 flux", DASH, "exitX=1;exitY=0.9;entryX=0;entryY=0.5;", pts=[(700, 452), (700, 495)])
p2.edge(tr, spk, "", BOTH)
p2.edge(page, spk, "orateur actif affiché (4 fois/s)", DASH, "exitX=0.5;exitY=1;entryX=0.5;entryY=1;", pts=[(170, 590), (1180, 590)])

# ---------- Page 3 : compte rendu ----------
p3 = Page("3. Compte rendu multi-modèles", "cr")
title(p3, "Compte rendu final : plusieurs modèles se contrôlent", "lancé automatiquement à la fin de la réunion · ~6 à 15 min")
t = p3.box("<b>Transcription complète</b><br/>intervenants notés {S1}, {S2}, {ME}", 470, 90, 300, 60, GREY)
p3.group("1 · Trois rédactions indépendantes, en parallèle", 160, 190, 920, 120, "strokeColor=#2F5597;")
d1 = p3.box("<b>Qwen3.8-27B</b><br/>raisonnement bas", 200, 230, 240, 60, BLUE)
d2 = p3.box("<b>Qwen3.5-397B</b><br/>sans raisonnement", 500, 230, 240, 60, BLUE)
d3 = p3.box("<b>GPT-OSS-120B</b><br/>raisonnement bas", 800, 230, 240, 60, BLUE)
j = p3.box("<b>2 · Arbitre</b> : Qwen3.8-27B (réflexion)<br/>confronte les 3 versions point par point<br/>"
           "revient à la transcription en cas de divergence<br/>garde l'<b>union</b> de ce qui est étayé", 420, 360, 400, 100, GREEN)
q = p3.box("<b>3 · Contrôleur</b> : Qwen3.5-397B<br/>revérifie chaque élément, cherche les oublis<br/>"
           "→ corrections appliquées par le programme", 420, 510, 400, 90, GREEN)
dt = p3.box("<b>4 · Contrôle des dates</b> (programme)<br/>jour de semaine ↔ date · « vendredi » → date réelle<br/>"
            "incohérences → « Points à vérifier »", 420, 650, 400, 80, YELLOW)
st = p3.box("<b>Temps de parole</b><br/>calculé (pas par un LLM)", 900, 650, 220, 80, YELLOW)
cr = p3.box("<b>report.json</b>", 520, 780, 200, 50, ORANGE)
out = p3.box("📋 IHM · Word · Markdown<br/>noms réels substitués à l'affichage", 470, 880, 300, 60, GREY)
for d in (d1, d2, d3):
    p3.edge(t, d)
    p3.edge(d, j)
p3.edge(t, j, "relit", DASH, "exitX=0;exitY=0.4;entryX=0;entryY=0.5;", pts=[(130, 114), (130, 410)])
p3.edge(t, q, "relit", DASH, "exitX=0;exitY=0.7;entryX=0;entryY=0.5;", pts=[(100, 132), (100, 555)])
p3.edge(j, q)
p3.edge(q, dt)
p3.edge(dt, cr)
p3.edge(t, st, "", DASH, "exitX=1;exitY=0.5;entryX=1;entryY=0.5;", pts=[(1160, 120), (1160, 690)])
p3.edge(st, cr, extra="exitX=0.5;exitY=1;entryX=1;entryY=0.5;")
p3.edge(cr, out)
p3.box("<b>Contenu</b><br/>objet · synthèse · à retenir<br/>tableau des décisions<br/>tableau des actions (qui, quand)<br/>"
       "« Pour moi »<br/>déroulé par sujet<br/>questions ouvertes · risques<br/>chiffres · dates<br/>points à vérifier<br/>"
       "temps de parole<br/>annexe méthode et arbitrages", 1220, 650, 230, 250, GREY, BOX + "align=left;spacingLeft=10;")

with open(OUT, "w", encoding="utf-8") as f:
    f.write('<mxfile host="Omniconf" agent="mkdrawio.py">' + p1.xml() + p2.xml() + p3.xml() + "</mxfile>\n")
print("ok", OUT)
