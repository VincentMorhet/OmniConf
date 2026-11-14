"use strict";
// Recherche dans un élément ; pour une vue de réunion, aussi dans son panneau et ses boutons d'écoute
// quand ils ont été déplacés dans l'onglet « Suivre en grand »
const inEl = (root, s) => root.matches(s) ? root : root.querySelector(s);
const $ = (s, el = document) => el.querySelector(s) || (el._side && (inEl(el._side, s) || inEl(el._actl, s))) || null;
const STATE_FR = { idle: "libre", joining: "connexion…", lobby: "salle d'attente", in_meeting: "en réunion",
  leaving: "départ…", error: "erreur", offline: "hors ligne", manual: "navigation manuelle", taken: "occupé" };
let cfg = { iceServers: [], workers: [] };
let me = null;         // compte connecté {login, name, role, admin, bot_names, must_change}
const cards = {};      // name -> element
const sessions = {};   // name -> {pc, stream, audio, micLatched}
let lastStatus = {};

function toast(msg, ms = 3000) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), ms);
}

async function api(path, opts = {}) {
  const o = { credentials: "same-origin", ...opts };
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers = { "Content-Type": "application/json" }; delete o.json; }
  const r = await fetch(path, o);
  if (r.status === 401 && !path.startsWith("/api/login")) { showLogin(); throw new Error("non connecté"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.blob();
  if (!r.ok) throw new Error((data && data.detail) || r.statusText);
  return data;
}

// ------------------------------------------------------------------ login
function showLogin() { $("#app").classList.add("hidden"); $("#login").classList.remove("hidden"); }
$("#loginForm").onsubmit = async (e) => {
  e.preventDefault(); $("#loginErr").textContent = "";
  try {
    const r = await api("/api/login", { method: "POST", json: { login: $("#loginId").value.trim(), password: $("#pw").value } });
    localStorage.lastLogin = $("#loginId").value.trim();
    const pw0 = $("#pw").value; $("#pw").value = "";
    await boot();
    if (r.must_change) askPassword(true, pw0);
  }
  catch (err) { $("#loginErr").textContent = err.message; }
};
// ------------------------------------------------------------------ thème blanc / noir
function applyTheme(t, save) {
  document.documentElement.dataset.theme = t;
  if (save) localStorage.theme = t;
  $('meta[name="theme-color"]').content = t === "light" ? "#f5f7fb" : "#0b1020";
  $("#themeIco").setAttribute("href", t === "light" ? "#i-moon" : "#i-sun");
  $("#themeLabel").textContent = t === "light" ? "Passer en thème sombre" : "Passer en thème clair";
}
applyTheme(document.documentElement.dataset.theme || "dark");
$("#themeBtn").onclick = () => applyTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light", true);
matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", (e) => { if (!localStorage.theme) applyTheme(e.matches ? "light" : "dark"); });
$("#logout").onclick = async () => { await api("/api/logout", { method: "POST" }); location.reload(); };

// ------------------------------------------------------------------ navigation : #home, #rec, #m/<assistant>
let cur = null;   // assistant affiché dans la vue « réunion »
const view = () => fName ? "focus" : cur ? "meet" : !$("#tab-rec").classList.contains("hidden") ? "rec" : "home";
function showTab(tab) {
  let w = null, recRoute = null;
  if (tab.startsWith("enr/")) { recRoute = tab; tab = "rec"; }   // page d'un enregistrement (sur la liste)
  else if (tab.startsWith("m/")) { w = decodeURIComponent(tab.slice(2)); tab = cfg.workers.includes(w) ? "meet" : "home"; }
  else if (tab.startsWith("suivre/")) { w = decodeURIComponent(tab.slice(7)); tab = cfg.workers.includes(w) ? "focus" : "home"; }
  if (tab === "reglages") tab = "set";
  if (!["home", "rec", "meet", "focus", "set"].includes(tab)) tab = "home";
  if (tab !== "meet" && tab !== "focus") w = null;
  if (tab !== "focus" || fName !== w) exitFocus();
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x.dataset.tab === (tab === "meet" ? "home" : tab)));
  ["home", "meet", "rec", "focus", "set"].forEach(t => $("#tab-" + t).classList.toggle("hidden", tab !== t));
  const want = recRoute || (tab === "meet" ? "m/" + encodeURIComponent(w) : tab === "focus" ? "suivre/" + encodeURIComponent(w) : tab);
  if (location.hash !== "#" + want) history.replaceState(null, "", "#" + want);
  if (recRoute) openRecRoute(recRoute); else if (player.url) closePlayer(true);
  setCur(w);
  if (tab === "focus") enterFocus(w);
  if (tab === "rec" || tab === "set") loadRecordings();   // réglages : stockage
  if (tab === "home") { refreshShots(); loadRecordings(); }
  if (!recRoute) window.scrollTo(0, 0);
}
const openMeeting = (name) => { location.hash = "#m/" + encodeURIComponent(name); };
// « Suivre en grand » : un onglet par assistant (réutilisé s'il est déjà ouvert)
const openFollow = (name) => { const w = window.open("/#suivre/" + encodeURIComponent(name), "omniconf-" + name); if (!w) location.hash = "#suivre/" + encodeURIComponent(name); };
function setCur(w) {
  if (cur && cur !== w && cards[cur]) { cards[cur].classList.add("hidden"); toggleVnc(cur, false); }
  cur = w;
  if (!w) return;
  cards[w].classList.remove("hidden");
  trState(w).open = true;
  if (lastStatus[w]) render(lastStatus[w]);
  refreshShots(); pollTranscripts();
}
document.querySelectorAll(".tab").forEach(b => b.onclick = () => { location.hash = "#" + b.dataset.tab; });
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));

// ------------------------------------------------------------------ lancement
function formMeeting() {
  return {
    url: $("#f-url").value.trim(), platform: $("#f-platform").value || null,
    display_name: $("#f-name").value.trim() || null, passcode: $("#f-pass").value.trim() || null,
    title: $("#f-title").value.trim() || null, record: $("#f-rec").checked, transcribe: $("#f-tr").checked,
    duration_min: parseInt($("#f-dur").value) || null, worker: $("#f-worker").value,
  };
}
$("#startForm").onsubmit = async (e) => {
  e.preventDefault();
  const btn = $("#btnStart"); btn.disabled = true;
  try {
    const r = await api("/api/meetings", { method: "POST", json: formMeeting() });
    toast(`L'assistant ${r.name} rejoint la réunion…`); $("#opts").open = false; $("#f-url").value = ""; $("#f-title").value = "";
    nameEdited = false; prefillName(); await refresh();
    location.hash = "#suivre/" + encodeURIComponent(r.name);   // réunion lancée : vue en grand directement
  } catch (err) { toast("Erreur : " + err.message, 5000); }
  btn.disabled = false;
};
$("#btnSchedule").onclick = async () => {
  const m = formMeeting(); const w = $("#f-when").value;
  if (!m.url || !w) return toast("Lien et date requis");
  try {
    await api("/api/schedule", { method: "POST", json: { ...m, when: new Date(w).toISOString() } });
    toast("Réunion planifiée"); loadSchedule();
  } catch (err) { toast("Erreur : " + err.message, 5000); }
};
// Nom affiché pré-rempli selon la plateforme (tant qu'il n'a pas été modifié à la main)
const platOf = r => ["teams", "zoom"].includes(r.meta?.platform) ? r.meta.platform : "web";
function siteOf(u) { try { return new URL(u).hostname.replace(/^www\./, ""); } catch (e) { return u; } }
function unwrapUrl(u) {   // lien Outlook « safelinks » ou redirection Google -> vrai lien
  for (let i = 0; i < 3; i++) {
    try { const x = new URL(u), h = x.hostname.toLowerCase();
      if (h.endsWith("safelinks.protection.outlook.com") && x.searchParams.get("url")) u = x.searchParams.get("url");
      else if (/^(www\.)?google\./.test(h) && x.pathname === "/url" && (x.searchParams.get("q") || x.searchParams.get("url"))) u = x.searchParams.get("q") || x.searchParams.get("url");
      else break;
    } catch (e) { break; }
  }
  return u;
}
function detectPlatform(u) {
  try { const h = new URL(unwrapUrl(u)).hostname;
    if (/teams\.(microsoft|live)/.test(h)) return "teams";
    if (/zoom\.(us|com)$/.test(h)) return "zoom";
    return "web";
  } catch (e) { }
  return "";
}
let nameEdited = false;
function prefillName() {
  if (nameEdited) return;
  const p = $("#f-platform").value || detectPlatform($("#f-url").value);
  $("#f-name").value = (cfg.defaultNames || {})[p] || "";
}
$("#f-name").oninput = () => nameEdited = $("#f-name").value !== "";
$("#f-url").addEventListener("input", prefillName);
$("#f-platform").addEventListener("change", prefillName);

// ------------------------------------------------------------------ assistants (workers)
const BUSY = ["joining", "lobby", "in_meeting", "leaving", "manual", "error"];
const isBusy = (st) => st?.online && BUSY.includes(st.state);
const platLabel = (st) => st.platform === "teams" ? "Teams" : st.platform === "zoom" ? "Zoom"
  : st.url ? siteOf(st.url) : st.platform === "open" ? "Web" : "";
const vncUrl = (name) => `/novnc/vnc.html?path=vnc/${encodeURIComponent(name)}/websockify&autoconnect=1&resize=scale&reconnect=1&show_dot=1`;
function toggleVnc(name, on) {
  const el = cards[name]; if (!el) return;
  const f = $(".vnc", el); on = on ?? f.classList.contains("hidden");
  if (on === !f.classList.contains("hidden")) return;
  if (on) f.src = vncUrl(name); else f.src = "about:blank";
  f.classList.toggle("hidden", !on); el.classList.toggle("vnc-on", on);
  const b = $(".b-screen", el); b.innerHTML = `<svg class="i"><use href="#i-hand"/></svg>${on ? "Rendre la main" : "Prendre la main"}`; b.classList.toggle("on", on);
}
// Vue « réunion » d'un assistant (une par assistant, une seule affichée)
function card(name) {
  if (cards[name]) return cards[name];
  const el = $("#tplWorker").content.firstElementChild.cloneNode(true);
  $(".wname", el).textContent = name;
  const act = (a, body) => async () => {
    try { await api(`/api/workers/${name}/${a}`, { method: "POST", json: body || {} }); refresh(); }
    catch (err) { toast("Erreur : " + err.message, 5000); }
  };
  $(".back", el).onclick = () => { location.hash = "#home"; };
  $(".b-leave", el).onclick = () => confirmLeave(name);
  $(".b-mute", el).onclick = act("mute");
  $(".b-rec", el).onclick = () => toggleRecording(name);
  $(".b-screen", el).onclick = () => toggleVnc(name);
  $(".shot", el).onclick = () => toggleVnc(name, true);
  $(".b-full", el).onclick = () => { toggleVnc(name, false); window.open(vncUrl(name), "_blank"); };
  $(".b-final", el).onclick = () => openFinal(name);
  $(".b-follow", el).onclick = () => openFollow(name);
  el._side = $(".side", el); el._actl = $(".audio-ctl", el);
  $(".b-open", el).onclick = () => {
    const u = prompt("Adresse à ouvrir dans le navigateur de l'assistant (ex : se connecter une fois à son compte, la session est conservée)",
      "https://teams.microsoft.com/");
    if (u) act("open", { url: u })().then(() => toggleVnc(name, true));
  };
  $(".tr-toggle", el).onclick = () => act(lastStatus[name]?.transcript?.active ? "tr-stop" : "tr-start")();
  $(".tr-copy", el).onclick = () => copyText($(trState(name).view === "an" ? ".an-body" : ".tr-body", el).innerText);
  el.querySelectorAll(".vt").forEach(b => b.onclick = () => {
    const t = trState(name); t.view = b.dataset.v;
    if (t.view === "an") { t.anSeen = t.anV; fetchAnalysis(name); }
    renderTr(name);
  });
  $(".an-now", el).onclick = async () => {
    try { await api(`/api/workers/${name}/analyze`, { method: "POST" }); toast("Point d'étape en cours…"); refresh(); }
    catch (err) { toast(err.message, 4000); }
  };
  bindSeek($(".an-body", el), (sec) => {   // clic sur un horodatage : retour à la transcription, au bon endroit
    const t = trState(name); t.view = "tr"; renderTr(name);
    const body = $(".tr-body", el); let best = null;
    body.querySelectorAll(":scope > p").forEach(p => { if (+p.dataset.t <= sec + 1) best = p; });
    if (best) { best.scrollIntoView({ block: "center" }); best.classList.add("hl"); setTimeout(() => best.classList.remove("hl"), 2500); }
  });
  $(".b-audio", el).onclick = () => sessions[name] ? stopAudio(name) : startAudio(name);
  $(".b-mic", el).onclick = () => { const s = sessions[name]; if (!s) return; s.micLatched = !s.micLatched; applyMic(name); };
  const ptt = $(".b-ptt", el);
  const down = (e) => { e.preventDefault(); const s = sessions[name]; if (s) { s.ptt = true; applyMic(name); } };
  const up = () => { const s = sessions[name]; if (s) { s.ptt = false; applyMic(name); } };
  ptt.addEventListener("pointerdown", down); ptt.addEventListener("pointerup", up);
  ptt.addEventListener("pointercancel", up); ptt.addEventListener("pointerleave", up);
  ptt.addEventListener("contextmenu", e => e.preventDefault());
  $(".vol", el).oninput = (e) => { const s = sessions[name]; if (s) s.audio.volume = +e.target.value; };
  $("#workers").appendChild(el);
  cards[name] = el;
  return el;
}

function fmtDur(iso) {
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso)) / 1000));
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60;
  return (h ? h + ":" : "") + String(m).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
}

function render(st) {
  const el = card(st.name), prev = lastStatus[st.name];
  lastStatus[st.name] = st;
  const t = trState(st.name), tr = st.transcript || {};
  if (st.recording_file) t.lastRec = st.recording_file;
  if (isBusy(prev) && !isBusy(st)) { t.ended = true; setTimeout(loadRecordings, 4000); if (sessions[st.name]) stopAudio(st.name); }   // fin de réunion
  if (isBusy(st)) t.ended = false;
  const b = $(".badge", el); b.className = "badge " + st.state; b.textContent = STATE_FR[st.state] || st.state;
  const taken = st.state === "taken";   // utilisé par un autre compte : rien n'en est montré
  $(".rec", el).classList.toggle("hidden", !st.recording);
  if (st.recording) $(".rectime", el).textContent = fmtDur(st.recording_since);
  const plat = platLabel(st);
  $(".wtitle", el).textContent = isBusy(st) ? (st.title || plat || "Réunion") : `Assistant ${st.name}`;
  const info = [];
  if (isBusy(st)) {
    if (plat && plat !== st.title) info.push(esc(plat));
    if (st.since) info.push("depuis " + new Date(st.since).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }));
    if (st.listeners) info.push(`🎧 ${st.listeners} à l'écoute`);
    if (st.auto_leave_at) info.push("départ auto à " + new Date(st.auto_leave_at).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }));
    if (st.owner_name && !st.mine) info.push("👤 " + esc(st.owner_name));
  } else info.push(!st.online ? "hors ligne" : taken ? "utilisé par un autre utilisateur" : "libre");
  $(".winfo", el).innerHTML = info.join(" · ");
  $(".winfo", el).title = st.url || "";
  let note = "";
  if (st.error) note = `<div class="e">⚠ ${esc(st.error)} — terminez à la main avec « Prendre la main ».</div>`;
  else if (st.note && st.state === "in_meeting") note = `<div class="n">ℹ ${esc(st.note.replace("« Écran »", "« Prendre la main »"))}</div>`;
  $(".wnote", el).innerHTML = note;
  $(".b-rec", el).innerHTML = st.recording ? "<i class=\"dot sq\"></i>Arrêter l'enregistrement" : "<i class=\"dot\"></i>Enregistrer";
  el.classList.toggle("idle", !isBusy(st) && !st.recording);    // assistant libre : seulement les actions utiles
  $(".b-leave", el).disabled = !isBusy(st);
  $(".audio-ctl", el).classList.toggle("hidden", !isBusy(st) && !sessions[st.name]);
  $(".b-mute", el).disabled = st.state !== "in_meeting";
  // écran « réunion terminée » / « assistant libre »
  const idle = st.online && !isBusy(st), endedBox = $(".ended", el);
  endedBox.classList.toggle("hidden", !(idle || !st.online));
  const final = idle && (t.ended || t.lastRec) && (tr.file || t.lastRec);
  $(".ended b", el).textContent = !st.online ? "Assistant hors ligne" : taken ? "Assistant occupé" : t.ended ? "Réunion terminée" : "Assistant libre";
  $(".ended-txt", el).textContent = !st.online ? "Il ne répond pas : vérifier le conteneur ou la VM."
    : taken ? "Il est utilisé par un autre utilisateur. Choisissez un autre assistant (ou « Automatique »)."
    : t.ended ? (tr.enabled && tr.file ? "Le compte rendu final est rédigé automatiquement (2 à 10 minutes)." : "L'enregistrement est dans « Enregistrements ».")
    : "Collez un lien sur l'accueil pour l'envoyer en réunion, ou « Ouvrir une page » pour vous connecter à un compte.";
  $(".b-final", el).classList.toggle("hidden", !final || taken);
  el.querySelectorAll(".ended button:not(.b-final)").forEach(x => x.disabled = taken);
  $(".b-final", el).textContent = tr.file ? "Voir le compte rendu final" : "Voir l'enregistrement";
  el.classList.toggle("offline", !st.online);
  $(".trb", el).classList.toggle("hidden", !tr.active);
  t.status = tr;
  if (tr.analysis && tr.analysis.v !== t.anV && !t.anLoading) fetchAnalysis(st.name);
  renderTr(st.name);
  renderTile(st);
  if (fName === st.name) renderFocus(st);
}

// ------------------------------------------------------------------ accueil : vignettes des réunions en cours
const tiles = {}, chips = {};
function tile(name) {
  if (tiles[name]) return tiles[name];
  const el = $("#tplTile").content.firstElementChild.cloneNode(true);
  el.onclick = () => openMeeting(name);
  el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openMeeting(name); } };
  $("img", el).onerror = function () { this.removeAttribute("src"); };
  $(".tfollow", el).onclick = (e) => { e.stopPropagation(); openFollow(name); };
  $("#liveGrid").appendChild(el);
  const c = document.createElement("button"); c.type = "button"; c.className = "wchip";
  c.onclick = () => openMeeting(name);
  $("#pool").appendChild(c);
  chips[name] = c;
  return tiles[name] = el;
}
function renderTile(st) {
  const el = tile(st.name), t = trState(st.name), busy = isBusy(st);
  el.classList.toggle("hidden", !busy);
  const live = st.state === "in_meeting";
  const s = $(".tstate", el); s.className = "tstate " + st.state;
  s.textContent = live ? "● EN DIRECT" : (STATE_FR[st.state] || st.state);
  $(".plat", el).textContent = platLabel(st);
  $(".ttime", el).textContent = live && (st.recording_since || st.since) ? fmtDur(st.recording_since || st.since) : "";
  $(".ttitle", el).textContent = st.title || platLabel(st) || "Réunion";
  const meta = [`Assistant ${st.name}`];
  if (st.transcript?.active) meta.push("📝 transcription");
  if (st.recording) meta.push("⏺ enregistrement");
  if (sessions[st.name]) meta.push("🎧 vous écoutez");
  $(".tmeta", el).textContent = meta.join(" · ");
  const now = t.an?.state?.en_cours, p = $(".tnow", el);
  p.classList.toggle("hidden", !now || !busy);
  if (now) p.innerHTML = "🎯 " + whoH(now, t.speakers || t.an.speakers, t.aliases || t.an.aliases);
  el.classList.toggle("err", st.state === "error");
  const c = chips[st.name];
  c.className = "wchip s-" + (st.online ? st.state : "offline");
  c.innerHTML = `<i></i>${esc(st.name)} <small>${esc(STATE_FR[st.online ? st.state : "offline"] || st.state)}</small>`;
  c.title = isBusy(st) ? (st.title || st.url || "") : "Ouvrir l'écran de cet assistant";
}
function renderHome(list) {
  const busy = list.filter(isBusy), free = list.filter(s => s.online && !isBusy(s) && s.state !== "taken").length;
  $("#liveEmpty").classList.toggle("hidden", busy.length > 0);
  const lc = $("#liveCount"); lc.textContent = busy.length; lc.classList.toggle("hidden", !busy.length);
  const lb = $("#liveBadge"); lb.textContent = busy.length; lb.classList.toggle("hidden", !busy.length);
  const h = new Date().getHours();
  $("#hello").textContent = h >= 18 || h < 5 ? "Bonsoir" : "Bonjour";
  const d = new Date().toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long" });
  $("#today").textContent = `${d.charAt(0).toUpperCase() + d.slice(1)} · ${busy.length ? busy.length + (busy.length > 1 ? " réunions en cours" : " réunion en cours") + " · " : ""}`
    + (free ? `${free} assistant${free > 1 ? "s" : ""} disponible${free > 1 ? "s" : ""} sur ${list.length}` : "aucun assistant disponible");
  $("#btnStart").disabled = !free && $("#f-worker").value === "auto";
}

// ------------------------------------------------------------------ transcription en direct
const trStates = {};
function trState(name) { return trStates[name] ||= { open: false, n: 0, file: null, status: {}, view: "an", anV: undefined, anSeen: 0 }; }
const fmtT = (sec) => { sec = Math.max(0, Math.floor(sec || 0)); const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60;
  return (h ? h + ":" : "") + String(m).padStart(2, "0") + ":" + String(sec % 60).padStart(2, "0"); };
// Intervenants : {id: {label, color, source}} ; alias = groupes de voix fusionnés (ancien id -> nouvel id)
const spkOf = (sp, al, id) => { const seen = new Set(); while (al && al[id] && !seen.has(id)) { seen.add(id); id = al[id]; }
  return { id, ...(sp?.[id] || { label: id || "?", color: 1 }) }; };
const SRC_FR = { ui: "nom affiché par la plateforme", voice: "reconnu à la voix", manual: "nommé à la main", me: "ta voix (IHM)", voiceprint: "reconnu par son empreinte vocale (consentement attesté)" };
function paraEl(p, opts = {}) {
  const el = document.createElement("p");
  if (opts.pending) el.className = "pending";
  el.dataset.spk = p.spk || ""; el.dataset.t = p.t;
  const ts = document.createElement("span"); ts.className = "ts" + (opts.seek ? " seek" : ""); ts.textContent = fmtT(p.t);
  if (opts.seek) { ts.onclick = () => opts.seek(p.t); ts.title = "Écouter ce passage"; }
  const spk = document.createElement("span"); spk.className = "spk";
  spk.onclick = () => opts.rename && opts.rename(el.dataset.spk);
  el.append(ts, spk, document.createTextNode(p.text));
  paintSpk(el, opts.speakers, opts.aliases, p.speaker);
  return el;
}
// Diapositive lue à l'écran : carte insérée dans la transcription à son heure
function slideEl(s, imgUrl, seek) {
  const el = document.createElement("div"); el.className = "slide" + (s.repeat_of ? " again" : ""); el.dataset.t = s.t;
  if (s.repeat_of) {   // retour sur une diapositive déjà montrée
    el.innerHTML = `<a class="sl-img" href="${imgUrl}" target="_blank" title="Agrandir"><img loading="lazy" alt="" src="${imgUrl}"></a>
      <div class="sl-body"><div class="sl-h"><span class="ts${seek ? " seek" : ""}">${fmtT(s.t)}</span> 🖥 retour à <b>${esc(s.titre || "la diapositive")}</b></div></div>`;
    if (seek) el.querySelector(".ts").onclick = () => seek(s.t);
    return el;
  }
  el.innerHTML = `<a class="sl-img" href="${imgUrl}" target="_blank" title="Agrandir"><img loading="lazy" alt="" src="${imgUrl}"></a>
    <div class="sl-body"><div class="sl-h"><span class="ts${seek ? " seek" : ""}">${fmtT(s.t)}</span> 🖥 <b>${esc(s.titre || "Diapositive")}</b></div>
    ${(s.texte || []).length ? "<ul>" + s.texte.map(x => `<li>${esc(String(x).replace(/^[•●▪◦·\-–*]\s*/, ""))}</li>`).join("") + "</ul>" : ""}
    ${s.visuel ? `<p class="sl-v">${esc(s.visuel)}</p>` : ""}</div>`;
  if (seek) el.querySelector(".ts").onclick = () => seek(s.t);
  return el;
}
function insertByTime(box, el) {   // avant le premier élément plus tardif (paroles et diapositives mêlées)
  const t = +el.dataset.t;
  const after = [...box.children].find(x => x.dataset.t !== undefined && +x.dataset.t > t);
  box.insertBefore(el, after || null);
}
function paintSpk(el, speakers, aliases, fallback) {
  const s = spkOf(speakers, aliases, el.dataset.spk);
  el.dataset.spk = s.id;
  el.className = el.className.replace(/\bc\d+\b/g, "").trim() + " c" + (s.color % 9);
  const n = el.querySelector(".spk"), lbl = speakers?.[s.id]?.label;
  if (lbl) n.textContent = lbl; else if (!n.textContent) n.textContent = fallback || s.label;   // anciennes transcriptions
  n.title = (SRC_FR[s.source] || "") + " — cliquer pour renommer";
}
function renderChips(box, speakers, used, rename) {
  box.innerHTML = "";
  Object.entries(speakers || {}).filter(([id]) => used.has(id)).forEach(([id, s]) => {
    const b = document.createElement("button"); b.type = "button"; b.className = "chip c" + (s.color % 9);
    b.innerHTML = `${esc(s.label)}<small>${s.source === "ui" ? "🖥" : s.source === "voice" ? "🔊" : s.source === "manual" ? "✏️" : s.source === "voiceprint" ? "🎙" : ""}</small>`;
    b.title = (SRC_FR[s.source] || "") + " — cliquer pour renommer";
    b.onclick = () => rename(id);
    box.appendChild(b);
  });
}
async function askRename(speakers, id, save) {
  const cur = speakers?.[id]; if (!cur || id === "ME") return;
  const n = prompt(`Nom de « ${cur.label} » (vide = nom automatique « ${cur.auto || cur.label} ») :`, cur.source === "manual" ? cur.label : "");
  if (n === null) return;
  try { await save(id, n.trim() || null); } catch (e) { toast("Erreur : " + e.message); }
}
function renderTr(name) {
  const el = cards[name]; if (!el) return;
  const t = trState(name), tr = t.status || {};
  const tog = $(".tr-toggle", el);
  tog.textContent = tr.active ? "⏹ Arrêter" : "▶ Démarrer";
  tog.disabled = !tr.enabled || (!tr.active && lastStatus[name]?.state !== "in_meeting");
  tog.title = !tr.enabled ? "Clé OVH AI Endpoints non configurée sur cet assistant" : tr.active ? "Arrêter la transcription" : "Démarrer la transcription";
  const an = t.view === "an", a = tr.analysis || {};
  (el._side || el).querySelectorAll(".vt").forEach(b => b.classList.toggle("on", b.dataset.v === t.view));
  $(".tr-body", el).classList.toggle("hidden", an); $(".tr-spk", el).classList.toggle("hidden", an);
  $(".an-body", el).classList.toggle("hidden", !an);
  $(".an-new", el).classList.toggle("hidden", an || !a.v || a.v === t.anSeen);
  $(".an-now", el).classList.toggle("hidden", !an || !tr.active);
  $(".an-now", el).disabled = !!a.running;
  const stEl = $(".tr-status", el);
  stEl.className = "tr-status" + (tr.active ? " on" : "") + ((an ? a.error : tr.error) ? " err" : "");
  if (!t.an || !tr.active && !tr.file) {   // rien à montrer encore : message d'accueil du panneau
    $(".an-body", el).innerHTML = !tr.enabled ? `<p class="empty">Transcription non configurée sur cet assistant (clé OVH AI Endpoints absente).</p>`
      : !tr.active && !tr.file ? `<p class="empty">Pas de transcription en cours.${lastStatus[name]?.state === "in_meeting" ? " Cliquer sur « ▶ Démarrer » pour la lancer." : ""}</p>`
      : analysisHtml(a);
    if (!tr.active && !tr.file) t.an = null;
  } else if (!t.an.state) paintAnalysis(name);   // message d'attente : suit le statut (fin de réunion…)
  if (an) {
    const at = a.at ? new Date(a.at).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }) : null;
    stEl.textContent = a.running ? "⏳ mise à jour en cours…" : a.error ? "⚠ " + a.error
      : [at ? `mis à jour à ${at}` : tr.active ? "en attente du premier point" : "",
         tr.active && a.next_in != null ? `suivant dans ~${Math.max(1, Math.round(a.next_in / 60))} min` : "",
         !tr.active && tr.file ? "réunion terminée" : ""].filter(Boolean).join(" · ");
    return;
  }
  stEl.textContent = tr.error ? "⚠ " + tr.error
    : tr.active ? `en direct · ${tr.paragraphs || 0} paragraphes · ${tr.n_speakers || 0} voix${tr.ui_speaker ? " · noms de la plateforme ✓" : ""}${tr.slides ? ` · 🖥 ${tr.slides} diapositive${tr.slides > 1 ? "s" : ""}` : ""}`
    : !tr.enabled ? "transcription non configurée" : tr.file ? "terminée" : "inactive";
}
async function pollTranscripts() {
  if (document.hidden || !cur || pollTranscripts.busy) return;
  pollTranscripts.busy = true;
  try { await pollTranscript(cur); } finally { pollTranscripts.busy = false; }
}
async function pollTranscript(name) {
  {
    const t = trState(name);
    if (!t.open || !(t.status?.active || t.status?.file)) return;
    try {
      const d = await api(`/api/workers/${name}/transcript?after=${t.n}`);
      const body = $(".tr-body", cards[name]);
      if (d.file !== t.file || d.total < t.n) {   // nouvelle transcription : on repart de zéro
        body.innerHTML = ""; t.file = d.file; t.slN = 0;
        if (t.n) { t.n = 0; return; }
      }
      const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
      t.speakers = d.speakers; t.aliases = d.aliases;
      t.capPars = (d.file === t.capFile ? (t.capPars || []) : []).concat(d.paragraphs).slice(-3); t.capFile = d.file;
      if (fName === name) renderCaps(name, d.pending);
      const rename = (id) => askRename(t.speakers, id, async (sid, nm) => {
        const r = await api(`/api/workers/${name}/speakers`, { method: "POST", json: { id: sid, name: nm } });
        t.speakers = r.speakers; repaint(); });
      const o = { speakers: d.speakers, aliases: d.aliases, rename };
      body.querySelectorAll(".pending").forEach(x => x.remove());
      d.paragraphs.forEach(p => insertByTime(body, paraEl(p, o)));
      t.n += d.paragraphs.length;
      d.pending.forEach(p => insertByTime(body, paraEl(p, { ...o, pending: true })));
      if ((d.slides || 0) > (t.slN || 0)) {   // nouvelles diapositives lues à l'écran
        try {
          const r = await api(`/api/workers/${name}/slides?after=${t.slN || 0}`);
          if (r.file === d.file) {
            r.slides.forEach(s => insertByTime(body, slideEl(s, `/api/slides/${encodeURIComponent(name)}/${encodeURIComponent(r.file)}/${s.repeat_of || s.n}`)));
            t.slN = (t.slN || 0) + r.slides.length;
          }
        } catch (e) { }
      }
      const repaint = () => {   // noms et fusions de voix peuvent changer : on repeint tout
        const used = new Set();
        body.querySelectorAll(":scope > p").forEach(el => { paintSpk(el, t.speakers, t.aliases); used.add(el.dataset.spk); });
        renderChips($(".tr-spk", cards[name]), t.speakers, used, rename);
        paintAnalysis(name);
      };
      repaint();
      if (atBottom) body.scrollTop = body.scrollHeight;
    } catch (e) { }
  }
}
// ------------------------------------------------------------------ suivre en grand : écran fluide, sous-titres, résumé de l'IA
let fName = null, fRfb = null, fLive = false, fHand = false;
let fCc = localStorage.fcc !== "0", fSideOn = localStorage.fside !== "0";
async function focusConnect(name) {
  fLive = false; $("#fVnc").innerHTML = ""; $("#fMsg").textContent = "Connexion à l'écran…"; $("#fMsg").classList.remove("hidden");
  try {
    const { default: RFB } = await import("/novnc/core/rfb.js");
    if (fName !== name) return;
    const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/vnc/${encodeURIComponent(name)}/websockify`;
    const rfb = new RFB($("#fVnc"), url, { wsProtocols: ["binary"] });
    rfb.viewOnly = !fHand; rfb.scaleViewport = true; rfb.background = "#000";
    rfb.qualityLevel = 6; rfb.compressionLevel = 2; rfb.focusOnClick = true;
    rfb.addEventListener("connect", () => { if (fRfb !== rfb) return; fLive = true; $("#fMsg").classList.add("hidden"); $("#fShot").classList.add("hidden"); });
    rfb.addEventListener("disconnect", () => {
      if (fRfb !== rfb) return;
      fRfb = null; fLive = false; $("#fShot").classList.remove("hidden");   // en attendant : images de l'écran
      $("#fMsg").textContent = "Écran fluide interrompu : reconnexion…"; $("#fMsg").classList.remove("hidden");
      setTimeout(() => { if (fName === name && !fRfb) focusConnect(name); }, 3000);
    });
    fRfb = rfb;
  } catch (e) {   // noVNC indisponible : images de l'écran rafraîchies
    $("#fShot").classList.remove("hidden"); $("#fMsg").classList.add("hidden");
  }
}
function enterFocus(name) {
  if (fName === name) return;
  fName = name; document.body.classList.add("focus");
  const el = cards[name];
  $("#fSidebox").appendChild(el._side); $("#fCtl").appendChild(el._actl);
  const t = trState(name); t.view = "an"; renderTr(name);
  $("#tab-focus").classList.toggle("noside", !fSideOn);
  $("#fFull").classList.toggle("hidden", !document.documentElement.requestFullscreen);
  fHand = false; syncFocusButtons();
  focusConnect(name);
  if (lastStatus[name]) renderFocus(lastStatus[name]);
}
function exitFocus() {
  if (!fName) return;
  const el = cards[fName];
  $(".mgrid", el).appendChild(el._side); $(".stage", el).insertBefore(el._actl, $(".b-follow", el));
  const r = fRfb; fRfb = null; fLive = false; try { r?.disconnect(); } catch (e) { }
  $("#fVnc").innerHTML = ""; $("#fCaps").classList.add("hidden");
  fName = null; document.body.classList.remove("focus"); document.title = "OmniConf";
}
function syncFocusButtons() {
  $("#fCc").classList.toggle("on", fCc); $("#fSide").classList.toggle("on", fSideOn);
  $("#fHand").classList.toggle("on", fHand); $("#fHand span").textContent = fHand ? "Rendre la main" : "Prendre la main";
  $("#fCaps").classList.toggle("off", !fCc);
}
function renderFocus(st) {
  const busy = isBusy(st), title = busy ? (st.title || platLabel(st) || "Réunion") : `Assistant ${st.name}`;
  $("#fTitle").textContent = title; document.title = title + " · OmniConf";
  $("#fInfo").textContent = busy ? [platLabel(st) !== st.title ? platLabel(st) : "", STATE_FR[st.state] || st.state].filter(Boolean).join(" · ")
    : st.online ? "libre" : "hors ligne";
  $("#fRec").classList.toggle("hidden", !st.recording);
  if (st.recording) $("#fRec").textContent = "● REC " + fmtDur(st.recording_since);
  const rb = $("#fRecBtn");
  rb.textContent = st.recording ? "⏹ Arrêter l'enregistrement" : "⏺ Enregistrer";
  rb.classList.toggle("recording", !!st.recording);
  rb.disabled = !st.online || st.state === "taken" || (!busy && !st.recording);
  $("#fLeave").disabled = !busy;
  const t = trState(st.name);
  $("#fEnded").classList.toggle("hidden", busy || !st.online || !t.ended);
  $("#fFinal").classList.toggle("hidden", !(st.transcript?.file || t.lastRec));
}
function renderCaps(name, pending) {
  const t = trState(name), box = $("#fCaps");
  // les lignes brutes en attente sont les plus récentes ; à défaut, le dernier paragraphe mis en forme
  const items = (pending.length >= 2 ? pending.slice(-2) : [...(t.capPars || []).slice(-(2 - pending.length)), ...pending]).filter(x => x?.text);
  const html = items.map(x => {
    const s = spkOf(t.speakers, t.aliases, x.spk), txt = x.text.length > 220 ? "…" + x.text.slice(-220) : x.text;
    return `<p><b class="who c${(s.color || 0) % 9}">${esc(s.label)}</b> ${esc(txt)}</p>`;
  }).join("");
  if (html && html !== box._html) { box._html = html; box.innerHTML = html; box._at = Date.now(); }
  box.classList.toggle("hidden", !html || Date.now() - (box._at || 0) > 15000);   // effacés après 15 s de silence
}
$("#fBack").onclick = () => { location.hash = "#m/" + encodeURIComponent(fName); };
$("#fCc").onclick = () => { fCc = !fCc; localStorage.fcc = fCc ? "1" : "0"; syncFocusButtons(); };
$("#fSide").onclick = () => {
  fSideOn = !fSideOn; localStorage.fside = fSideOn ? "1" : "0"; $("#tab-focus").classList.toggle("noside", !fSideOn); syncFocusButtons();
  setTimeout(() => window.dispatchEvent(new Event("resize")), 50);   // noVNC recalcule l'échelle
};
$("#fHand").onclick = () => { fHand = !fHand; if (fRfb) { fRfb.viewOnly = !fHand; if (fHand) fRfb.focus(); } syncFocusButtons(); };
$("#fFull").onclick = () => document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen().catch(() => { });
$("#fFinal").onclick = () => fName && openFinal(fName);
$("#fRecBtn").onclick = () => fName && toggleRecording(fName);
$("#fLeave").onclick = () => fName && confirmLeave(fName);

// ------------------------------------------------------------------ confirmations (arrêt de l'enregistrement, départ)
function askConfirm(title, text, okLabel, danger = false) {
  const d = $("#dlgConfirm");
  $("#cfTitle").textContent = title; $("#cfText").textContent = text;
  $("#cfYes").textContent = okLabel; $("#cfYes").className = danger ? "danger" : "primary";
  d.returnValue = "";
  return new Promise(res => {
    d.addEventListener("close", () => res(d.returnValue === "ok"), { once: true });
    d.showModal(); $("#cfNo").focus();   // choix sûr par défaut : Entrée n'arrête rien par mégarde
  });
}
async function workerAct(name, action) {
  try { await api(`/api/workers/${name}/${action}`, { method: "POST", json: {} }); await refresh(); return true; }
  catch (err) { toast("Erreur : " + err.message, 5000); return false; }
}
async function toggleRecording(name) {
  const st = lastStatus[name];
  if (!st?.recording) { if (await workerAct(name, "rec-start")) toast("Enregistrement démarré"); return; }
  const ok = await askConfirm("Arrêter l'enregistrement ?",
    `L'assistant reste dans la réunion, mais la suite ne sera plus enregistrée${st.transcript?.active ? " (la transcription continue)" : ""}. ` +
    "Vous pourrez relancer un enregistrement : ce sera un fichier séparé.", "⏹ Arrêter l'enregistrement", true);
  if (ok && await workerAct(name, "rec-stop")) toast("Enregistrement arrêté");
}
async function confirmLeave(name) {
  const st = lastStatus[name] || {};
  const what = [st.recording ? "l'enregistrement s'arrête" : "",
    st.transcript?.active ? "la transcription s'arrête et le compte rendu final est rédigé" : ""].filter(Boolean);
  const ok = await askConfirm("Quitter la réunion ?",
    `L'assistant ${name} quitte « ${st.title || platLabel(st) || "la réunion"} »` + (what.length ? ` : ${what.join(", ")}.` : ".") +
    " Il ne pourra pas revenir tout seul : il faudrait relancer la réunion.", "🚪 Quitter la réunion", true);
  if (ok && await workerAct(name, "leave")) toast("L'assistant a quitté la réunion");
}

// ------------------------------------------------------------------ points d'étape et comptes rendus
const parseT = (s) => { if (typeof s === "number") return s;
  const m = /^\s*(\d+):(\d{1,2})(?::(\d{1,2}))?\s*$/.exec(s || ""); if (!m) return null;
  return m[3] != null ? +m[1] * 3600 + +m[2] * 60 + +m[3] : +m[1] * 60 + +m[2]; };
const SEEK_LEAD = 3;   // secondes de marge avant l'horodatage cliqué
const tsH = (t) => { const v = parseT(t); return v == null ? "" : ` <span class="ts seek" data-t="${v}" title="Écouter ce passage (la vidéo démarre ${SEEK_LEAD} s avant)">${fmtT(v)}</span>`; };
// {S1} / {ME} dans les textes du LLM -> nom coloré de l'intervenant (suit les renommages)
const whoH = (text, sp, al) => esc(typeof text === "string" ? text : text?.texte ?? text ?? "").replace(/\{(S\d+|ME)\}/g, (m, id) => {
  const s = spkOf(sp, al, id); return `<b class="who c${(s.color || 0) % 9}">${esc(s.label)}</b>`; });
const secH = (title, inner, cls = "") => inner ? `<section class="an-sec ${cls}"><h4>${title}</h4>${inner}</section>` : "";
const ulH = (items, f) => { items = (items || []).filter(Boolean); return items.length ? "<ul>" + items.map(x => `<li>${f(x)}</li>`).join("") + "</ul>" : ""; };
function bindSeek(box, seek) { box.addEventListener("click", (e) => { const ts = e.target.closest(".ts.seek"); if (ts && seek) seek(+ts.dataset.t); }); }

function analysisHtml(a, sp, al) {
  const W = (x) => whoH(x, sp, al), st = a?.state;
  if (!st) return `<p class="empty">${a?.running ? "Rédaction du premier point en cours…" :
    a?.next_in != null ? `Le compte rendu en direct apparaîtra ici dans ~${Math.max(1, Math.round(a.next_in / 60))} min, puis sera mis à jour toutes les ${Math.round((a.every || 300) / 60)} minutes (ou tout de suite avec « ↻ Mettre à jour »).<br><br>En attendant, l'onglet « 📝 Transcription » défile en direct.`
      : "Aucun point d'étape pour cette réunion."}</p>`;
  const qs = (st.questions || []).slice().sort((x, y) => (x.statut === "répondue") - (y.statut === "répondue"));
  return secH("🎯 En ce moment", st.en_cours ? `<p>${W(st.en_cours)}</p>` : "")
    + secH("🙋 Pour toi", ulH(st.pour_moi, x => W(x.texte) + tsH(x.t)), "me")
    + secH("🧭 Où en est-on", st.synthese ? `<p>${W(st.synthese)}</p>` : "")
    + secH("⭐ À retenir", ulH(st.a_retenir, W))
    + secH("✅ Décisions", ulH(st.decisions, x => W(x.texte) + tsH(x.t)))
    + secH("📌 Actions", ulH(st.actions, x => `${W(x.action)} <span class="meta">— ${W(x.responsable || "non désigné")} · ${W(x.echeance || "non précisée")}</span>` + tsH(x.t)))
    + secH("❓ Questions", ulH(qs, x => `<span class="${x.statut === "répondue" ? "done" : ""}">${W(x.texte)}${x.qui ? ` <span class="meta">(${W(x.qui)})</span>` : ""}${x.statut === "répondue" ? " ✓" : ""}</span>` + tsH(x.t)))
    + secH("⚠️ Points de vigilance", ulH(st.vigilance, x => W(x.texte) + tsH(x.t)), "warn")
    + secH("🗂 Sujets abordés", (st.sujets || []).length ? `<div class="topics">${st.sujets.map(x =>
        `<span class="topic ${x.statut === "en cours" ? "cur" : x.statut === "reporté" ? "later" : ""}" title="${esc(x.statut || "")}">${W(x.titre)}${tsH(x.t)}</span>`).join("")}</div>` : "")
    + secH("🕒 Fil de la réunion", (a.flashes || []).slice().reverse().map(f =>
        `<div class="flash"><div class="fh"><span class="ts seek" data-t="${f.de}">${fmtT(f.de)} → ${fmtT(f.a)}</span> <b>${W(f.titre)}</b></div>
         <p>${W(f.resume)}</p>${ulH(f.points, W)}</div>`).join(""));
}

async function fetchAnalysis(name) {
  const t = trState(name); t.anLoading = true;
  try {
    const d = await api(`/api/workers/${name}/analysis`);
    t.an = d; t.anV = d.v;
    if (t.view === "an") t.anSeen = d.v;
    paintAnalysis(name);
  } catch (e) { } finally { t.anLoading = false; renderTr(name); }
}
function paintAnalysis(name) {
  const t = trState(name), box = $(".an-body", cards[name]);
  if (!t.an || !box) return;
  const y = box.scrollTop;
  box.innerHTML = analysisHtml({ ...t.an, ...(t.status?.analysis || {}) }, t.speakers || t.an.speakers, t.aliases || t.an.aliases);
  box.scrollTop = y;
}

// Format en note (version 2) : en-tête, intervenants, résumé par sujet, décisions, actions, points ouverts,
// réserves sur la source, annexe (temps de parole, méthode)
const DATE_FMT = { weekday: "long", day: "numeric", month: "long", year: "numeric" };
function textBlocksH(text, W) {
  return String(text || "").trim().split(/\n\s*\n/).map(chunk => {
    const lines = chunk.split("\n").filter(l => l.trim());
    const items = lines.filter(l => /^\s*[-•*]\s+/.test(l));
    if (items.length && items.length === lines.length) return "<ul>" + items.map(l => `<li>${W(l.replace(/^\s*[-•*]\s+/, ""))}</li>`).join("") + "</ul>";
    return lines.map(l => /^\s*[-•*]\s+/.test(l) ? `<ul><li>${W(l.replace(/^\s*[-•*]\s+/, ""))}</li></ul>` : `<p>${W(l)}</p>`).join("");
  }).join("");
}
function reportHtmlV2(rep, sp, meta, labels) {
  const cr = rep.cr || {};
  sp = { ...sp };
  // noms calculés par le service (lexique, « Nom (voix 2) », fusion des voix d'une même personne), sauf renommage
  for (const [id, lab] of Object.entries(labels || {})) if (sp[id] && sp[id].source !== "manual" && id !== "ME") sp[id] = { ...sp[id], label: lab };
  // voix identifiée par une présentation explicite : nom donné dans « intervenants » (sauf renommage)
  for (const it of cr.intervenants || []) {
    const id = String(it?.qui || "").replace(/[{} ]/g, "");
    if (id && sp[id] && sp[id].source !== "manual" && it.nom && !/^null$/i.test(it.nom)) sp[id] = { ...sp[id], label: it.nom };
  }
  const W = (x) => whoH(x, sp);
  const tbl = (head, rows) => rows.length ? `<div class="tw"><table><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}</table></div>` : "";
  const m = meta || {}, cov = cr.couverture || {};
  const start = m.started_at ? new Date(m.started_at) : null, end = m.ended_at ? new Date(m.ended_at) : null;
  const hm = (d) => d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }).replace(":", "h");
  const partial = cov.debut_manquant || cov.fin_manquante;
  const why = [...(cov.service || []), cov.commentaire].filter(x => x && x !== "Séance complète").join(" ; ");
  const rows = [
    ["Date", start ? start.toLocaleDateString("fr-FR", DATE_FMT).replace(/^./, c => c.toUpperCase()) + ", " + hm(start) + (end ? " → " + hm(end) : "") : ""],
    ["Créneau", cr.creneau ? W(cr.creneau) : ""],
    ["Outil", [({ teams: "Teams", zoom: "Zoom" })[m.platform] || m.site || m.platform, cr.outil ? W(cr.outil) : ""].filter(Boolean).join(", ")],
    ["Couverture", partial ? `<span class="warn-t">Partielle : ${[cov.debut_manquant && "début manquant", cov.fin_manquante && "fin manquante"].filter(Boolean).join(" et ")}.</span> ${W(why)}` : (why ? W(why) : "Séance complète.")]];
  let h = `<h2>${W(cr.titre || "Compte rendu")}</h2><div class="tw kv"><table>${rows.filter(r => r[1]).map(r => `<tr><th>${r[0]}</th><td>${r[1]}</td></tr>`).join("")}</table></div>`;
  h += secH("👥 Intervenants identifiés", ulH(cr.intervenants, it => `${W(it.qui)}${it.organisation && !/^null$/i.test(it.organisation) ? ` <span class="meta">(${W(it.organisation)})</span>` : ""}${it.role ? " : " + W(it.role) : ""}${it.justification ? `<div class="meta">${W(it.justification)}</div>` : ""}`)
    + ((cr.cites || []).length ? `<p class="meta">Cités : ${(cr.cites || []).map(W).join(", ")}</p>` : ""));
  h += secH("📝 Résumé", (cr.resume || []).map(s => `<div class="subj"><h5>${W(s.titre)}${tsH(s.debut)}</h5>${textBlocksH(s.texte, W)}</div>`).join(""));
  h += secH("✅ Décisions", (cr.decisions || []).length ? ulH(cr.decisions, x => W(x.texte) + (x.porteur ? ` <span class="meta">(porteur : ${W(x.porteur)})</span>` : "")
      + (x.statut && x.statut !== "actée" ? ` <span class="meta">— ${esc(x.statut)}</span>` : "") + tsH(x.t))
      : `<p>Aucune${cr.decisions_commentaire ? " : " + W(cr.decisions_commentaire) : "."}</p>`)
    + secH("📌 Actions", tbl(["Action", "Porteur", "Échéance", ""], (cr.actions || []).map(x =>
        [W(x.action), W(x.responsable || "non désigné"), W(x.echeance || "non fixée"), tsH(x.t)])))
    + secH("❓ Points ouverts", ulH(cr.points_ouverts, x => W(x.texte) + tsH(x.t)));
  const res = (cr.reserves || []).filter(x => x?.texte).map(x => W(x.texte) + tsH(x.t));
  if ((cr.ecarte || []).length) res.push("Non repris : " + cr.ecarte.map(W).join(" ; ") + ".");
  h += secH("🔍 Réserves sur la source", res.length ? "<ol>" + res.map(x => `<li>${x}</li>`).join("") + "</ol>" : "", "warn");
  const st = rep.stats || [];
  h += secH("⏱ Temps de parole", st.length ? `<div class="bars">${st.map(s => `<div class="bar-row"><span>${W(`{${s.spk}}`)}</span>
      <span class="b"><i class="c${(spkOf(sp, null, s.spk).color || 0) % 9}" style="width:${s.share}%"></i></span><span class="meta">${fmtT(s.secs)} · ${s.share} %</span></div>`).join("")}</div>` : "");
  return h + methodH(rep, W);
}
function methodH(rep, W) {
  const steps = rep.steps || [], ok = (e) => steps.filter(s => s.etape === e && s.ok).map(s => esc(s.modele));
  return `<details class="method"><summary>🔬 Méthode et contrôle (${ok("rédaction").length} modèles, ${(rep.arbitrages || []).length} arbitrages, ${(rep.corrections || []).length} corrections)</summary>
    <p class="meta">Rédaction indépendante : ${ok("rédaction").join(", ") || "—"}${ok("confrontation").length ? ` · confrontation : ${ok("confrontation")}` : ""}${ok("contrôle").length ? ` · contrôle qualité : ${ok("contrôle")}` : ""}
    · ${rep.secs ? Math.round(rep.secs / 60 * 10) / 10 + " min" : ""} · généré le ${new Date(rep.generated_at).toLocaleString("fr-FR")}</p>
    ${steps.filter(s => !s.ok).map(s => `<p class="meta">⚠ ${esc(s.etape)} ${esc(s.modele)} : ${esc(s.erreur || "échec")}</p>`).join("")}
    ${(rep.arbitrages || []).length ? "<h5>Arbitrages entre les projets</h5>" + ulH(rep.arbitrages, a => `<b>${W(a.point)}</b><div class="meta">${W(a.versions)}</div>Retenu : ${W(a.retenu)}${a.justification ? ` <span class="meta">(${W(a.justification)})</span>` : ""}`) : ""}
    ${(rep.corrections || []).length ? "<h5>Corrections du contrôle qualité</h5>" + ulH(rep.corrections, c => `<b>${W(c.element)}</b> : ${W(c.probleme)} → ${W(c.correction)}`) : ""}
  </details>`;
}

function reportHtml(rep, sp, meta, labels) {
  if ((rep.version || 1) >= 2) return reportHtmlV2(rep, sp, meta, labels);
  const cr = rep.cr || {}, W = (x) => whoH(x, sp);
  const tbl = (head, rows) => rows.length ? `<div class="tw"><table><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}</table></div>` : "";
  const st = rep.stats || [];
  let h = `<h2>${W(cr.titre || "Compte rendu")}</h2>`;
  if (st.length) h += `<p class="meta">Participants : ${st.map(s => W(`{${s.spk}}`)).join(", ")}</p>`;
  h += secH("Objet", cr.objet ? `<p>${W(cr.objet)}</p>` : "")
    + secH("Synthèse", cr.synthese ? `<p>${W(cr.synthese)}</p>` : "")
    + secH("⭐ À retenir", ulH(cr.a_retenir, W))
    + secH("✅ Décisions", tbl(["N°", "Décision", "Porteur", "Statut", ""], (cr.decisions || []).map((x, i) =>
        [i + 1, W(x.texte) + (x.condition ? `<div class="meta">condition : ${W(x.condition)}</div>` : ""), W(x.porteur || "—"), esc(x.statut || ""), tsH(x.t)])))
    + secH("📌 Actions", tbl(["N°", "Action", "Responsable", "Échéance", ""], (cr.actions || []).map((x, i) =>
        [i + 1, W(x.action), W(x.responsable || "non désigné"), W(x.echeance || "non précisée"), tsH(x.t)])))
    + secH(`🙋 Pour ${esc(sp?.ME?.label || "moi")}`, ulH(cr.pour_moi, x => W(x.texte) + tsH(x.t)), "me");
  if ((cr.sujets || []).length) h += secH("🗂 Déroulé de la réunion", cr.sujets.map((s, i) => `<div class="subj">
      <h5>${i + 1}. ${W(s.titre)}${tsH(s.debut)}</h5>${s.resume ? `<p>${W(s.resume)}</p>` : ""}
      ${ulH(s.positions, p => `${W(p.qui)} : ${W(p.position)}`)}
      ${s.conclusion ? `<p class="concl"><b>Conclusion :</b> ${W(s.conclusion)}</p>` : ""}</div>`).join(""));
  h += secH("❓ Questions ouvertes", ulH(cr.questions_ouvertes, x => W(x.texte) + (x.qui ? ` <span class="meta">(${W(x.qui)})</span>` : "") + (x.suite ? ` → ${W(x.suite)}` : "") + tsH(x.t)))
    + secH("⚠️ Risques et points de vigilance", ulH(cr.risques, x => W(x.texte) + tsH(x.t)), "warn")
    + secH("🔢 Chiffres cités", tbl(["Valeur", "Contexte", ""], (cr.chiffres || []).map(x => [W(x.valeur), W(x.contexte), tsH(x.t)])))
    + secH("📅 Dates et échéances", tbl(["Date", "Objet", ""], (cr.dates || []).map(x => [W(x.date), W(x.objet), tsH(x.t)])))
    + secH("➡️ Prochaine réunion", cr.prochaine_reunion ? `<p>${W(cr.prochaine_reunion)}</p>` : "")
    + secH("🔍 Points à vérifier", ulH(cr.a_verifier, x => W(x.texte) + tsH(x.t)), "warn")
    + secH("⏱ Temps de parole", st.length ? `<div class="bars">${st.map(s => `<div class="bar-row"><span>${W(`{${s.spk}}`)}</span>
        <span class="b"><i class="c${(spkOf(sp, null, s.spk).color || 0) % 9}" style="width:${s.share}%"></i></span><span class="meta">${fmtT(s.secs)} · ${s.share} %</span></div>`).join("")}</div>` : "");
  const steps = rep.steps || [], ok = (e) => steps.filter(s => s.etape === e && s.ok).map(s => esc(s.modele));
  h += `<details class="method"><summary>🔬 Méthode et contrôle (${ok("rédaction").length} modèles, ${(rep.arbitrages || []).length} arbitrages, ${(rep.corrections || []).length} corrections)</summary>
    <p class="meta">Rédaction indépendante : ${ok("rédaction").join(", ") || "—"}${ok("confrontation").length ? ` · confrontation : ${ok("confrontation")}` : ""}${ok("contrôle").length ? ` · contrôle qualité : ${ok("contrôle")}` : ""}
    · ${rep.secs ? Math.round(rep.secs / 60 * 10) / 10 + " min" : ""} · généré le ${new Date(rep.generated_at).toLocaleString("fr-FR")}</p>
    ${steps.filter(s => !s.ok).map(s => `<p class="meta">⚠ ${esc(s.etape)} ${esc(s.modele)} : ${esc(s.erreur || "échec")}</p>`).join("")}
    ${(rep.arbitrages || []).length ? "<h5>Arbitrages entre les projets</h5>" + ulH(rep.arbitrages, a => `<b>${W(a.point)}</b><div class="meta">${W(a.versions)}</div>Retenu : ${W(a.retenu)}${a.justification ? ` <span class="meta">(${W(a.justification)})</span>` : ""}`) : ""}
    ${(rep.corrections || []).length ? "<h5>Corrections du contrôle qualité</h5>" + ulH(rep.corrections, c => `<b>${W(c.element)}</b> : ${W(c.probleme)} → ${W(c.correction)}`) : ""}
  </details>`;
  return h;
}

async function copyText(txt) {
  try { await navigator.clipboard.writeText(txt); toast("Texte copié"); }
  catch (e) { toast("Copie impossible sur ce navigateur"); }
}

async function refresh() {
  let list;
  try { list = await api("/api/workers"); } catch (e) { return; }   // réseau : on réessaie au prochain tour
  for (const st of list) { try { render(st); } catch (e) { console.error("affichage de " + st.name, e); } }
  renderHome(list);
}

// Aperçus d'écran : celui de la réunion ouverte (souvent), les vignettes de l'accueil (moins souvent)
async function shotInto(name, img, width) {
  const blob = await api(`/api/workers/${name}/screenshot?w=${Math.max(320, Math.min(1600, Math.round(width * devicePixelRatio)))}`);
  const old = img.src; img.src = URL.createObjectURL(blob); if (old.startsWith("blob:")) URL.revokeObjectURL(old);
}
let shotTick = 0;
async function refreshShots() {
  if (document.hidden || refreshShots.busy) return;
  refreshShots.busy = true; shotTick++;
  try {
    const v = view();
    if (v === "focus") {
      if (!fLive && lastStatus[fName]?.online) await shotInto(fName, $("#fShot"), $("#fShot").clientWidth || 1200).catch(() => { });
    } else if (v === "meet" && lastStatus[cur]?.online && !cards[cur].classList.contains("vnc-on")) {
      const img = $(".shot", cards[cur]);
      await shotInto(cur, img, img.clientWidth || 800).catch(() => { });
    } else if (v === "home" && (shotTick % 2 === 1 || !refreshShots.homeOnce)) {
      refreshShots.homeOnce = true;
      for (const [name, el] of Object.entries(tiles)) {
        if (!isBusy(lastStatus[name])) continue;
        const img = $("img", el);
        await shotInto(name, img, img.clientWidth || 400).catch(() => { });
      }
    }
  } finally { refreshShots.busy = false; }
}

// ------------------------------------------------------------------ périphériques audio (entrée / sortie)
const canSink = "setSinkId" in HTMLMediaElement.prototype;
const MIC_OPTS = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };

async function getMic() {
  const id = localStorage.micId;
  try {
    return await navigator.mediaDevices.getUserMedia({ audio: { ...MIC_OPTS, ...(id ? { deviceId: { exact: id } } : {}) } });
  } catch (e) {
    if (id && (e.name === "OverconstrainedError" || e.name === "NotFoundError")) {   // micro débranché -> défaut
      delete localStorage.micId; return navigator.mediaDevices.getUserMedia({ audio: MIC_OPTS });
    }
    throw e;
  }
}
async function applySink(el) {
  if (!canSink || !el) return;
  try { await el.setSinkId(localStorage.sinkId || ""); } catch (e) { delete localStorage.sinkId; }
}
async function setSink(id) {
  localStorage.sinkId = id;
  for (const s of Object.values(sessions)) await applySink(s.audio);
  await applySink($("#player video"));
  await applySink(vpAudio);
}
async function setMicDevice(id) {
  localStorage.micId = id;
  // remplace le micro à chaud dans toutes les sessions audio ouvertes (sans renégociation)
  for (const [name, s] of Object.entries(sessions)) {
    try {
      const ns = await getMic();
      const tr = s.pc.getTransceivers().find(t => t.receiver.track?.kind === "audio");
      await tr.sender.replaceTrack(ns.getAudioTracks()[0]);
      s.stream?.getTracks().forEach(t => t.stop());
      s.stream = ns; applyMic(name); meter(name, ns);
      $(".b-mic", cards[name]).classList.remove("hidden");
    } catch (e) { toast(`Changement de micro (${name}) : ${e.message}`, 5000); }
  }
}

// --- panneau « Audio »
let testStream = null, testCtx = null;
async function fillDevices() {
  const devs = await navigator.mediaDevices.enumerateDevices();
  const ins = devs.filter(d => d.kind === "audioinput"), outs = devs.filter(d => d.kind === "audiooutput");
  const noLabels = ins.length && ins.every(d => !d.label);
  $("#devPerm").classList.toggle("hidden", !noLabels);
  const fill = (sel, list, cur, def) => {
    sel.innerHTML = ""; sel.add(new Option(def, ""));
    list.filter(d => d.deviceId && d.deviceId !== "default" && d.deviceId !== "communications")
      .forEach((d, i) => sel.add(new Option(d.label || `Périphérique ${i + 1}`, d.deviceId)));
    sel.value = [...sel.options].some(o => o.value === cur) ? cur : "";
  };
  fill($("#micSel"), ins, localStorage.micId || "", "Micro par défaut du système");
  fill($("#spkSel"), outs, localStorage.sinkId || "", "Sortie par défaut du système");
  $("#spkSel").disabled = !canSink;
  $("#spkNote").classList.toggle("hidden", canSink);
}
async function openAudioPanel() {
  $("#audioModal").classList.remove("hidden");
  try { await fillDevices(); } catch (e) { toast(e.message); }
}
function closeAudioPanel() { stopMicTest(); $("#audioModal").classList.add("hidden"); }
async function startMicTest() {
  stopMicTest();
  try { testStream = await getMic(); } catch (e) { return toast("Micro : " + e.message, 5000); }
  await fillDevices();   // les noms des périphériques sont visibles une fois l'accès accordé
  testCtx = new (window.AudioContext || window.webkitAudioContext)();
  const an = testCtx.createAnalyser(); an.fftSize = 512;
  testCtx.createMediaStreamSource(testStream).connect(an);
  const buf = new Float32Array(an.fftSize), ctx = testCtx;
  $("#micTestBtn").textContent = "⏹ Arrêter le test";
  const tick = () => {
    if (testCtx !== ctx) return;
    an.getFloatTimeDomainData(buf); let p = 0; for (const v of buf) p = Math.max(p, Math.abs(v));
    $("#micTestLvl").value = p; requestAnimationFrame(tick);
  };
  tick();
}
function stopMicTest() {
  testStream?.getTracks().forEach(t => t.stop()); testStream = null;
  testCtx?.close(); testCtx = null;
  $("#micTestLvl").value = 0; $("#micTestBtn").textContent = "🎙️ Tester le micro";
}
async function playBeep() {
  // bip de 0,6 s généré localement, joué sur la sortie choisie
  const sr = 48000, n = sr * 0.6, buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
  const w = (o, str) => [...str].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  w(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); w(8, "WAVEfmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
  v.setUint16(22, 1, true); v.setUint32(24, sr, true); v.setUint32(28, sr * 2, true); v.setUint16(32, 2, true);
  v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) v.setInt16(44 + i * 2, Math.sin(2 * Math.PI * 660 * i / sr) * 12000 * Math.min(1, (n - i) / 2000), true);
  const a = new Audio(URL.createObjectURL(new Blob([buf], { type: "audio/wav" })));
  await applySink(a);
  a.play().catch(e => toast("Lecture bloquée : " + e.message, 5000));
}
$("#audioBtn").onclick = openAudioPanel;
$("#audioModal .close").onclick = closeAudioPanel;
$("#audioModal").onclick = (e) => { if (e.target.id === "audioModal") closeAudioPanel(); };
$("#micSel").onchange = async () => { await setMicDevice($("#micSel").value); if (testStream) startMicTest(); toast("Micro sélectionné"); };
$("#spkSel").onchange = async () => { await setSink($("#spkSel").value); playBeep(); };
$("#micTestBtn").onclick = () => testStream ? stopMicTest() : startMicTest();
$("#beepBtn").onclick = playBeep;
$("#devPerm").onclick = startMicTest;
navigator.mediaDevices?.addEventListener?.("devicechange", () => {
  if (!$("#audioModal").classList.contains("hidden")) fillDevices();
});

// ------------------------------------------------------------------ audio WebRTC
function applyMic(name) {
  const s = sessions[name]; const el = cards[name]; if (!s) return;
  const on = s.micLatched || s.ptt;
  s.stream?.getAudioTracks().forEach(t => t.enabled = on);
  const mic = $(".b-mic", el);
  mic.className = "b-mic " + (s.micLatched ? "on" : "off");
  mic.textContent = s.micLatched ? "🎙️ Micro ouvert" : "🔇 Micro coupé";
  $(".b-ptt", el).classList.toggle("talking", !!s.ptt);
  $(".b-ptt", el).classList.toggle("hidden", s.micLatched || !s.stream);
}

function waitIce(pc, ms = 2500) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise(res => {
    const t = setTimeout(res, ms);
    pc.addEventListener("icegatheringstatechange", () => { if (pc.iceGatheringState === "complete") { clearTimeout(t); res(); } });
  });
}

async function startAudio(name) {
  const el = cards[name]; const btn = $(".b-audio", el);
  btn.disabled = true; btn.textContent = "Connexion…";
  const audio = new Audio(); audio.autoplay = true; audio.setAttribute("playsinline", "");
  audio.style.display = "none"; document.body.appendChild(audio);   // attaché au DOM (Safari)
  await applySink(audio);
  audio.play().catch(() => { });  // débloque la lecture sur iOS (geste utilisateur)
  let stream = null;
  try {
    stream = await getMic();
  } catch (e) { toast("Micro indisponible : écoute seule (" + e.message + ")", 5000); }
  const pc = new RTCPeerConnection({ iceServers: cfg.iceServers });
  const s = sessions[name] = { pc, stream, audio, micLatched: false, ptt: false };
  if (stream) pc.addTransceiver(stream.getAudioTracks()[0], { direction: "sendrecv", streams: [stream] });
  else pc.addTransceiver("audio", { direction: "sendrecv" });
  pc.ontrack = (e) => { audio.srcObject = e.streams[0] || new MediaStream([e.track]); audio.play().catch(() => { }); };
  pc.onconnectionstatechange = () => {
    if (pc.connectionState === "connected") toast(`Audio connecté à ${name}`);
    if (["failed", "closed"].includes(pc.connectionState) && sessions[name]?.pc === pc) {
      toast(`Audio ${name} : ${pc.connectionState}`); stopAudio(name);
    }
  };
  applyMic(name);
  if (stream) meter(name, stream);
  try {
    await pc.setLocalDescription(await pc.createOffer());
    await waitIce(pc);
    const ans = await api(`/api/workers/${name}/offer`, { method: "POST",
      json: { sdp: pc.localDescription.sdp, type: pc.localDescription.type } });
    await pc.setRemoteDescription(ans);
    btn.textContent = "🔇 Ne plus écouter"; btn.classList.remove("primary"); if (lastStatus[name]) renderTile(lastStatus[name]);
    ["b-mic", "vol"].forEach(c => $("." + c, el).classList.toggle("hidden", !stream && c === "b-mic"));
    $(".vol", el).classList.remove("hidden");
    if (stream) $(".lvl", el).classList.remove("hidden");
    applyMic(name);
  } catch (e) { toast("Erreur audio : " + e.message, 5000); stopAudio(name); }
  btn.disabled = false;
}

function stopAudio(name) {
  const s = sessions[name]; const el = cards[name]; if (!s) return;
  delete sessions[name];
  try { s.pc.close(); } catch (e) { }
  s.stream?.getTracks().forEach(t => t.stop());
  s.audio.srcObject = null; s.audio.remove();
  s.ctx?.close();
  const btn = $(".b-audio", el); btn.textContent = "🎧 Écouter la réunion"; btn.classList.add("primary");
  if (lastStatus[name]) renderTile(lastStatus[name]);
  ["b-mic", "b-ptt", "vol", "lvl"].forEach(c => $("." + c, el).classList.add("hidden"));
}

function meter(name, stream) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const an = ctx.createAnalyser(); an.fftSize = 512;
    ctx.createMediaStreamSource(stream).connect(an);
    sessions[name].ctx?.close();
    const buf = new Float32Array(an.fftSize); sessions[name].ctx = ctx;
    const tick = () => {
      if (sessions[name]?.ctx !== ctx) return;
      an.getFloatTimeDomainData(buf);
      let p = 0; for (const v of buf) p = Math.max(p, Math.abs(v));
      const s = sessions[name];
      $(".lvl", cards[name]).value = (s.micLatched || s.ptt) ? p : 0;
      requestAnimationFrame(tick);
    };
    tick();
  } catch (e) { }
}

// ------------------------------------------------------------------ planification
async function loadSchedule() {
  const items = await api("/api/schedule");
  const ul = $("#schedule"); ul.innerHTML = "";
  $("#schedSec").classList.toggle("hidden", !items.length);
  for (const it of items) {
    const li = document.createElement("li");
    li.innerHTML = `<div class="t"><b></b><small></small></div><button>Supprimer</button>`;
    $("b", li).textContent = it.title || it.url;
    $("small", li).textContent = `${new Date(it.when).toLocaleString()} · ${it.status}${it.result ? " (" + it.result + ")" : ""}`
      + (me?.admin && it.owner_name && it.owner !== me.login ? ` · 👤 ${it.owner_name}` : "");
    $("button", li).onclick = async () => { await api(`/api/schedule/${it.id}`, { method: "DELETE" }); loadSchedule(); };
    ul.appendChild(li);
  }
}

// ------------------------------------------------------------------ enregistrements
const fmtSize = (b) => b > 1e9 ? (b / 1e9).toFixed(1) + " Go" : b > 1e6 ? (b / 1e6).toFixed(b > 1e8 ? 0 : 1) + " Mo" : Math.max(1, Math.round(b / 1e3)) + " Ko";
const fmtSec = (s) => s == null ? "" : (s >= 3600 ? Math.floor(s / 3600) + "h" + String(Math.floor(s / 60) % 60).padStart(2, "0")
  : Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0"));
const esc = (t) => String(t ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
let recItems = [];

function dayLabel(d) {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const x = new Date(d); x.setHours(0, 0, 0, 0);
  const diff = Math.round((today - x) / 86400000);
  if (diff === 0) return "Aujourd'hui";
  if (diff === 1) return "Hier";
  return d.toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
}

// ------------------------------------------------------------------ accès de Claude (serveur MCP) : jetons
async function loadTokens() {
  const box = $("#mcpTokens"); if (!box) return;
  try {
    const d = await api("/api/mcp/tokens");
    $("#mcpUrl").textContent = d.url;
    $("#mcpCa").classList.toggle("hidden", !d.ca);
    $("#mcpCaFp").textContent = d.ca_sha256 || "";
    const live = d.tokens.filter(x => !x.revoked);
    box.innerHTML = live.length ? live.map(x => `<div class="tok"><b>${esc(x.name)}</b>
      <span class="badge ${x.scope === "write" ? "warn" : ""}">${x.scope === "write" ? "écriture (dépôt du CR final)" : "lecture"}</span>
      <small>${me?.admin && x.user && x.user !== me.login ? "👤 " + esc(x.user_name || x.user) + " · " : ""}créé le ${new Date(x.created).toLocaleString("fr-FR")} · ${x.last_used ? "utilisé le " + new Date(x.last_used).toLocaleString("fr-FR") : "jamais utilisé"}</small>
      <button class="danger" data-id="${esc(x.id)}">Révoquer</button></div>`).join("") : "<small>Aucun jeton actif.</small>";
    box.querySelectorAll("button[data-id]").forEach(b => b.onclick = async () => {
      if (!confirm("Révoquer ce jeton ? Claude ne pourra plus s'en servir (il faudra en créer un autre).")) return;
      try { await api("/api/mcp/tokens/" + b.dataset.id, { method: "DELETE" }); loadTokens(); } catch (e) { toast(e.message); }
    });
  } catch (e) { box.textContent = "Erreur : " + e.message; }
}
async function createToken(scope) {
  const name = prompt("Nom du jeton (pour le reconnaître, ex. « Claude PC bureau ») :", scope === "write" ? "Claude écriture" : "Claude");
  if (!name) return;
  try {
    const d = await api("/api/mcp/tokens", { method: "POST", json: { name, scope } });
    const out = $("#mcpNew"); out.classList.remove("hidden");
    $("#mcpNewTok").value = d.token;
    $("#mcpNewVar").textContent = scope === "write" ? "WEBCONF_WRITE_TOKEN" : "WEBCONF_TOKEN";
    loadTokens();
  } catch (e) { toast(e.message); }
}
$("#mcpCreate").onclick = () => createToken("read");
$("#mcpCreateW").onclick = () => createToken("write");
$("#mcpCopy").onclick = async () => {
  const v = $("#mcpNewTok").value;
  try { await navigator.clipboard.writeText(v); toast("Jeton copié"); } catch (e) { $("#mcpNewTok").select(); }
};
$("#mcpHide").onclick = () => { $("#mcpNewTok").value = ""; $("#mcpNew").classList.add("hidden"); };
$("#mcpBox").addEventListener("toggle", () => { if ($("#mcpBox").open) loadTokens(); });

// ------------------------------------------------------------------ lexique du projet
let lexLoaded = "";
function lexInfo(d) {
  const ws = Object.entries(d.workers || {});
  const ok = ws.filter(([, v]) => v === d.version).length;
  const when = d.updated_at ? new Date(d.updated_at).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }) : "";
  $("#lexInfo").textContent = d.version
    ? `Version ${d.version}${when ? " du " + when : ""} · à jour sur ${ok}/${ws.length} assistant(s)`
      + (ok < ws.length ? " (" + ws.filter(([, v]) => v !== d.version).map(([n, v]) => n + " : " + (v || "aucun")).join(", ") + ")" : "")
    : "Aucun lexique pour l'instant.";
}
async function loadLexicon() {
  try { const d = await api("/api/lexicon"); lexLoaded = d.text; $("#lexText").value = d.text; lexInfo(d); }
  catch (e) { toast(e.message); }
}
$("#lexBox").addEventListener("toggle", () => { if ($("#lexBox").open) loadLexicon(); });
$("#lexSave").onclick = async () => {
  const text = $("#lexText").value;
  if (text === lexLoaded && !confirm("Lexique inchangé : le renvoyer quand même aux assistants ?")) return;
  $("#lexSave").disabled = true;
  try {
    const d = await api("/api/lexicon", { method: "PUT", json: { text } });
    lexLoaded = text; toast("Lexique enregistré");
    lexInfo({ ...d, updated_at: new Date().toISOString() });
  } catch (e) { toast(e.message); }
  finally { $("#lexSave").disabled = false; }
};

async function loadRecordings() {
  try {
    const [items, disks] = await Promise.all([api("/api/recordings"), api("/api/storage")]);
    recItems = items;
    const c = $("#recCount"); c.textContent = items.length; c.classList.toggle("hidden", !items.length);
    const sel = $("#r-worker");
    cfg.workers.forEach(w => { if (![...sel.options].some(o => o.value === w)) sel.add(new Option(w, w)); });
    $("#storage").innerHTML = disks.map(d => d.error ? `<span>${esc(d.worker)} : injoignable</span>` :
      `<span>${esc(d.worker)} <span class="bar"><i class="${d.used / d.total > .85 ? "hi" : ""}" style="width:${(100 * d.used / d.total).toFixed(0)}%"></i></span>
       ${fmtSize(d.free)} libres</span>`).join("");
    renderRecordings(); renderRecent();
    clearTimeout(loadRecordings._t);   // compte rendu en cours de génération : on rafraîchit la liste
    if (items.some(r => r.report_job?.state === "running")) loadRecordings._t = setTimeout(() => {
      if (view() !== "meet") loadRecordings(); }, 10000);
  } catch (e) { if (view() === "rec") toast("Erreur : " + e.message); }
}

function renderRecordings() {
  const q = $("#r-search").value.trim().toLowerCase();
  const pf = $("#r-platform").value, wf = $("#r-worker").value;
  const items = recItems.filter(r => {
    const d = new Date(r.mtime * 1000);
    const hay = [r.meta?.title, r.name, r.meta?.url, r.worker, d.toLocaleDateString("fr-FR"), dayLabel(d)].join(" ").toLowerCase();
    return (!q || hay.includes(q)) && (!pf || platOf(r) === pf) && (!wf || r.worker === wf);
  });
  const root = $("#recordings"); root.innerHTML = "";
  if (!items.length) {
    root.innerHTML = `<div class="empty">${recItems.length ? "Aucun résultat" : "Aucun enregistrement pour l'instant"}</div>`;
    return;
  }
  let grid = null, last = null;
  for (const r of items) {
    const started = r.meta?.started_at ? new Date(r.meta.started_at) : new Date(r.mtime * 1000);
    const label = dayLabel(started);
    if (label !== last) {
      const h = document.createElement("h3"); h.className = "day"; h.textContent = label; root.appendChild(h);
      grid = document.createElement("div"); grid.className = "rgrid"; root.appendChild(grid); last = label;
    }
    grid.appendChild(recItem(r, started));
  }
}

const recUrl = (r) => `/api/recordings/${encodeURIComponent(r.worker)}/${encodeURIComponent(r.name)}`;
function playRec(r, tab) { openRec(r, { tab }); }
// Page d'un enregistrement : compte rendu et transcription ; la vidéo ne démarre que sur demande
let recOpenOpts = {}, recBack = "#rec";
function openRec(r, o = {}) {
  recOpenOpts = o;
  const h = `#enr/${encodeURIComponent(r.worker)}/${encodeURIComponent(r.name)}`;
  if (!location.hash.startsWith("#enr/")) recBack = location.hash || "#rec";
  if (location.hash === h) openRecRoute(h.slice(1)); else location.hash = h;
}
async function openRecRoute(route) {
  const parts = route.split("/"), w = decodeURIComponent(parts[1] || ""), f = decodeURIComponent(parts.slice(2).join("/"));
  let r = recItems.find(x => x.worker === w && x.name === f);
  if (!r) { await loadRecordings(); r = recItems.find(x => x.worker === w && x.name === f); }
  const o = recOpenOpts; recOpenOpts = {};
  if (!r) { toast("Enregistrement introuvable (supprimé, ou réservé à un autre utilisateur)", 4000); location.hash = "#rec"; return; }
  const url = recUrl(r);
  if (player.url === url) { if (o.tab) showPlayerView(o.tab); if (o.play) showVideo(true); return; }
  openPlayer(url, r.cr_title || r.meta?.title || r.name, r.name, { video: r.kind !== "transcript", transcript: r.transcript,
    report: r.report || !!r.report_job, tab: o.tab, recording: r.recording, play: o.play, rec: r });
}
const PLAT_FR = { teams: "Teams", zoom: "Zoom" };
function recItem(r, started, opts = {}) {
  const url = `/api/recordings/${encodeURIComponent(r.worker)}/${encodeURIComponent(r.name)}`;
  const el = document.createElement("div"); el.className = "ritem" + (opts.short ? " short" : "");
  const video = r.kind !== "transcript";
  const plat = platOf(r) === "web" ? (r.meta?.site || siteOf(r.meta?.url || "") || "Web") : (PLAT_FR[platOf(r)] || platOf(r));
  const when = (opts.short ? dayLabel(started) + " · " : "") + started.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
  const state = r.recording ? `<span class="chip live">● En cours</span>`
    : r.report ? `<span class="chip ok">Compte rendu</span>`
    : r.report_job?.state === "running" ? `<span class="chip run" title="${esc(r.report_job.step || "")}">Compte rendu en cours…</span>`
    : r.transcript ? `<span class="chip">Transcription</span>` : `<span class="chip">Vidéo seule</span>`;
  const owner = r.mine || !r.owner_name ? "" : `<span class="chip soft">${r.can_edit ? "" : "partagée par "}${esc(r.owner_name)}</span>`;
  el.innerHTML = `
    <div class="thumb ${video ? "" : "tthumb"}">${video ? '<img loading="lazy" alt="">' : '<svg class="i"><use href="#i-doc"/></svg>'}
      ${r.duration != null ? `<span class="dur">${fmtSec(r.duration)}</span>` : ""}
      ${video ? `<button class="play" title="Lire la vidéo" aria-label="Lire la vidéo"><svg class="i"><use href="#i-play"/></svg></button>` : ""}</div>
    <div class="rbody">
      <b class="rtitle"></b>
      <div class="rmeta">${esc(when)} · ${esc(plat)}${opts.short ? "" : ` · ${esc(r.worker)}`}</div>
      <div class="rtags">${state}${r.final_cr ? `<span class="chip ok">CR final</span>` : ""}${owner}</div>
    </div>
    <button class="icon more" title="Télécharger, partager, renommer, supprimer…" aria-haspopup="menu"><svg class="i"><use href="#i-more"/></svg></button>
    <div class="rmenu hidden" role="menu">
      ${video ? `<a href="${url}?download=1" download="${esc(r.name)}">Télécharger la vidéo</a>` : ""}
      ${r.transcript ? `<a href="${url}/transcript?fmt=md&download=1">Télécharger la transcription</a>` : ""}
      ${r.report ? `<a href="${url}/report?fmt=docx&download=1">Compte rendu (Word)</a>` : ""}
      ${r.final_cr ? `<a href="${url}/final?fmt=md" target="_blank" rel="noopener">Compte rendu final (Markdown)</a>
        <a href="${url}/final?fmt=pdf" target="_blank" rel="noopener">Compte rendu final (PDF, s'il a été joint)</a>` : ""}
      ${r.can_edit === false ? "" : `<button class="share">Partager avec…</button>
      <button class="ren">Renommer</button>
      <button class="del bad" ${r.recording ? "disabled title=\"Enregistrement en cours\"" : ""}>Supprimer</button>`}
    </div>`;
  const title = r.cr_title || r.meta?.title || r.name;
  $(".rtitle", el).textContent = title; $(".rtitle", el).title = title;
  const img = $("img", el);
  if (img) { img.src = url + "/thumb" + (r.recording ? "?t=" + Date.now() : ""); img.onerror = () => img.remove(); }
  const play = (tab, video) => openRec(r, { tab, play: video });
  const menu = $(".rmenu", el);
  $(".more", el).onclick = (e) => { e.stopPropagation(); const open = menu.classList.contains("hidden"); closeMenus(); menu.classList.toggle("hidden", !open); };
  menu.addEventListener("click", (e) => { e.stopPropagation(); closeMenus(); });
  el.onclick = () => play();                                     // toute la carte : compte rendu d'abord
  if ($(".play", el)) $(".play", el).onclick = (e) => { e.stopPropagation(); play(undefined, video); };   // lecture immédiate
  if ($(".share", el)) $(".share", el).onclick = () => shareRecording(r, title);
  if ($(".ren", el)) $(".ren", el).onclick = () => renameRecording(url, r.meta?.title || r.name);
  if ($(".del", el)) $(".del", el).onclick = () => deleteRecording(url, title);
  if (opts.short) el.querySelectorAll(".more, .rmenu").forEach(x => x.remove());
  return el;
}
async function renameRecording(url, title) {
  const t = prompt("Nouveau titre :", title);
  if (!t || t === title) return false;
  try { await api(url, { method: "PATCH", json: { title: t } }); loadRecordings(); return true; } catch (e) { toast(e.message); return false; }
}
// Menu « ⋯ » des vignettes : un seul ouvert à la fois, fermé au moindre clic ailleurs
function closeMenus() { document.querySelectorAll(".rmenu:not(.hidden)").forEach(m => m.classList.add("hidden")); }
document.addEventListener("click", (e) => { if (!e.target.closest(".rmenu, .more")) closeMenus(); });
async function deleteRecording(url, title) {
  if (!confirm(`Supprimer définitivement « ${title} » ?\n\nLa vidéo, la transcription et le compte rendu seront effacés. Cette action est irréversible.`)) return false;
  try { await api(url, { method: "DELETE" }); toast(`« ${title} » supprimé`); loadRecordings(); return true; }
  catch (e) { toast("Suppression impossible : " + e.message, 5000); return false; }
}
// Accueil : les dernières réunions terminées
function renderRecent() {
  const items = recItems.filter(r => !r.recording).slice(0, 4), box = $("#recent");
  $("#recentSec").classList.toggle("hidden", !items.length);
  box.innerHTML = "";
  items.forEach(r => box.appendChild(recItem(r, r.meta?.started_at ? new Date(r.meta.started_at) : new Date(r.mtime * 1000), { short: true })));
}
// Fin d'une réunion : ouvre l'enregistrement correspondant sur le compte rendu
async function openFinal(name) {
  await loadRecordings();
  const t = trState(name), f = t.status?.file, rec = t.lastRec;
  const mine = recItems.filter(x => x.worker === name);
  const r = mine.find(x => f && x.name.startsWith(f) && x.kind !== "transcript") || mine.find(x => rec && x.name === rec)
    || mine.find(x => f && x.name.startsWith(f));
  if (!r) return toast("Enregistrement introuvable : voir « Enregistrements »", 4000);
  playRec(r, r.transcript ? "cr" : undefined);
}
["#r-search", "#r-platform", "#r-worker"].forEach(s => $(s).addEventListener("input", renderRecordings));
$("#refreshRec").onclick = loadRecordings;

let player = { url: null, doc: null, rep: null, seek: null };
async function openPlayer(url, title, file, o = { video: true }) {
  const v = $("#player video"), pl = $("#player"), box = $("#playerTr");
  player = { url, doc: null, rep: null, seek: null, view: o.transcript ? (o.tab || "tr") : null, title, rec: o.rec };
  $("#playerTitle").textContent = title;
  $("#playerDel").disabled = !!o.recording; $("#playerDel").title = o.recording ? "Enregistrement en cours" : "Supprimer cet enregistrement";
  $("#playerSlides").classList.toggle("hidden", !o.video || !!o.recording);
  $("#playerSlides").disabled = false; $("#playerSlides span").textContent = "Lire les diapositives";
  $("#playerDl").classList.toggle("hidden", !o.video);
  $("#playerDl").href = url + "?download=1"; $("#playerDl").setAttribute("download", file);
  $("#playerMd").classList.toggle("hidden", !o.transcript);
  $("#playerMd").href = url + "/transcript?fmt=md&download=1";
  $("#playerDocx").classList.add("hidden"); $("#playerDocx").href = url + "/report?fmt=docx&download=1";
  pl.classList.remove("tronly", "withtr");
  box.innerHTML = ""; $("#playerCr").innerHTML = ""; $("#playerAn").innerHTML = "";
  $("#playerSpk").innerHTML = "";
  $("#playerTabs").classList.toggle("hidden", !o.transcript);
  player.view = o.transcript ? (o.tab || (o.report ? "cr" : "tr")) : null;   // le compte rendu d'abord, s'il existe
  player.hasVideo = !!o.video; player.videoOn = false;
  v.pause(); v.removeAttribute("src"); v.load(); pl.classList.add("novid");
  $("#playerPlay").classList.toggle("hidden", !o.video); $("#playerPlay").textContent = "▶ Lire la vidéo";
  const r = o.rec || {}, d = r.meta?.started_at ? new Date(r.meta.started_at) : r.mtime ? new Date(r.mtime * 1000) : null;
  $("#playerInfo").textContent = [d ? d.toLocaleDateString("fr-FR", { weekday: "long", day: "numeric", month: "long" }) + " à "
    + d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }) : "", r.duration != null ? fmtSec(r.duration) : "",
    r.worker ? "assistant " + r.worker : "", r.owner_name && !r.mine ? (r.can_edit ? "👤 " : "partagée par ") + r.owner_name : ""].filter(Boolean).join(" · ");
  showPlayerView();
  pl.classList.remove("hidden"); pl.scrollTop = 0;
  if (o.play) showVideo(true);
  if (!o.transcript) { box.classList.remove("hidden"); box.innerHTML = `<p class="empty">Pas de transcription pour cet enregistrement${o.video ? " : « ▶ Lire la vidéo » pour le regarder" : ""}.</p>`; return; }
  // horodatage cliqué : la vidéo s'ouvre et démarre SEEK_LEAD s avant, pour entendre la phrase entière ;
  // sans vidéo, on montre le passage dans la transcription
  const seek = (t) => {
    if (player.hasVideo) { showVideo(true, Math.max(0, t - SEEK_LEAD)); if (player.view !== "tr") return; }
    else showPlayerView("tr");
    let best = null; box.querySelectorAll(":scope > p").forEach(p => { if (+p.dataset.t <= t + 1) best = p; });
    if (best) { if (!player.hasVideo) best.scrollIntoView({ block: "center" }); best.classList.add("hl"); setTimeout(() => best.classList.remove("hl"), 2500); } };
  player.seek = seek;
  try {
    const doc = await api(url + "/transcript");
    if (player.url !== url) return;
    player.doc = doc;
    const repaint = () => {
      const used = new Set();
      box.querySelectorAll(":scope > p").forEach(el => { paintSpk(el, doc.speakers); used.add(el.dataset.spk); });
      renderChips($("#playerSpk"), doc.speakers, used, rename);
      paintPlayerCr(); paintPlayerAn();
    };
    const rename = (id) => askRename(doc.speakers, id, async (sid, nm) => {
      const r = await api(url + "/speakers", { method: "PATCH", json: { id: sid, name: nm } });
      doc.speakers = r.speakers; repaint(); toast("Intervenant renommé (transcription et compte rendu mis à jour)"); });
    (doc.paragraphs || []).forEach(p => box.appendChild(paraEl(p, { seek, speakers: doc.speakers, rename })));
    const [, w, f] = url.match(/^\/api\/recordings\/([^/]+)\/([^/?]+)/) || [];
    const stem = decodeURIComponent(f || "").replace(/\.(mp4|transcript\.json)$/, "");
    (doc.slides || []).forEach(s => insertByTime(box, slideEl(s, `/api/slides/${w}/${encodeURIComponent(stem)}/${s.repeat_of || s.n}`, seek)));
    repaint();
    if (!box.children.length) box.textContent = "Aucune parole transcrite.";
    $('#playerTabs [data-v="an"]').classList.toggle("hidden", !doc.analysis?.state);
    paintPlayerAn();
  } catch (e) { box.textContent = "Transcription indisponible : " + e.message; }
  loadPlayerReport(url);
}
// Page d'un enregistrement : feuille « Exporter » (exports du compte rendu, vidéo, partage, suppression)
$("#playerShare").onclick = () => {
  const d = player.rep || {}, rep = d.report, r = player.rec || {}, running = d.job?.state === "running";
  $("#exTitle").textContent = player.title || "";
  for (const id of ["#playerDocx", "#exDiff", "#exMdCr", "#exCopy"]) $(id).classList.toggle("hidden", !rep);
  $("#exDiff").classList.toggle("hidden", !rep || (rep.version || 1) < 2);
  $("#exDiff").href = player.url + "/report?fmt=docx&variant=diffusion&download=1";
  $("#exMdCr").href = player.url + "/report?fmt=md&download=1";
  $("#exGen").classList.toggle("hidden", !player.doc || running || d.live || r.can_edit === false);
  $("#exGenLabel").textContent = rep ? "Régénérer le compte rendu" : "Générer le compte rendu";
  $("#exShareRec").classList.toggle("hidden", !r.worker || r.can_edit === false);
  $("#exRename").classList.toggle("hidden", !r.worker || r.can_edit === false);
  $("#playerDel").classList.toggle("hidden", r.can_edit === false);
  $("#dlgExport").showModal();
};
$("#exCopy").onclick = () => { const c = $("#playerCr").cloneNode(true); c.querySelector(".cr-bar")?.remove(); c.querySelector(".method")?.remove(); copyText(c.innerText); $("#dlgExport").close(); };
$("#exGen").onclick = async () => {
  if (player.rep?.report && !confirm("Régénérer le compte rendu ? (l'actuel sera remplacé)")) return;
  $("#dlgExport").close();
  try { await api(player.url + "/report", { method: "POST" }); showPlayerView("cr"); loadPlayerReport(player.url); } catch (e) { toast(e.message, 4000); }
};
$("#exShareRec").onclick = () => { $("#dlgExport").close(); if (player.rec) shareRecording(player.rec, player.title); };
$("#exRename").onclick = async () => {
  $("#dlgExport").close();
  if (await renameRecording(player.url, player.rec?.meta?.title || player.title)) closePlayer();
};
for (const id of ["#playerDocx", "#exDiff", "#exMdCr", "#playerMd", "#playerDl", "#playerSlides", "#exAudio", "#playerDel"])
  $(id).addEventListener("click", () => $("#dlgExport").close());
function showPlayerView(v) {
  if (v) player.view = v;
  const cur = player.view;
  document.querySelectorAll("#playerTabs .vt").forEach(b => b.classList.toggle("on", b.dataset.v === cur));
  $("#playerTr").classList.toggle("hidden", cur !== "tr"); $("#playerSpk").classList.toggle("hidden", cur !== "tr");
  $("#playerCr").classList.toggle("hidden", cur !== "cr"); $("#playerAn").classList.toggle("hidden", cur !== "an");
}
document.querySelectorAll("#playerTabs .vt").forEach(b => b.onclick = () => showPlayerView(b.dataset.v));
bindSeek($("#playerCr"), (t) => player.seek && player.seek(t));
bindSeek($("#playerAn"), (t) => player.seek && player.seek(t));
function playerSpeakers() {
  const sp = { ...(player.doc?.speakers || player.rep?.speakers || {}) };
  sp.ME = { ...(sp.ME || { color: 0 }), label: player.rep?.me_name || sp.ME?.label || "Moi" };
  return sp;
}
function paintPlayerAn() {
  const d = player.doc;
  if (d?.analysis?.state) $("#playerAn").innerHTML = analysisHtml(d.analysis, d.speakers);
}
function paintPlayerCr() {
  const d = player.rep, box = $("#playerCr");
  if (!d) return;
  const job = d.job || {}, running = job.state === "running";
  $("#playerDocx").classList.toggle("hidden", !d.report);
  let bar = `<div class="cr-bar">`;
  if (running) bar += `<span class="run">⏳ ${esc(job.step || "génération")}…</span>`;
  else if (job.state === "error") bar += `<span class="err">⚠ ${esc(job.error || "échec")}</span>`;
  else if (job.state === "skipped") bar += `<span class="meta">${esc(job.error || "")}</span>`;
  if (!running && !d.live && !d.report) bar += `<button class="cr-gen primary">Générer le compte rendu</button>`;
  bar += "</div>";
  let body = "";
  if (d.report) body = reportHtml(d.report, playerSpeakers(), d.meta, d.labels);
  else if (running) body = `<p class="empty">Le compte rendu est rédigé séparément par plusieurs modèles, puis leurs versions sont confrontées entre elles et vérifiées contre la transcription. Comptez 2 à 10 minutes selon la durée de la réunion.</p>`;
  else if (d.live) body = `<p class="empty">Réunion en cours : le compte rendu sera rédigé automatiquement à la fin. En attendant, voir les points d'étape sur la carte du worker.</p>`;
  else if (!job.state) body = `<p class="empty">Pas encore de compte rendu pour cette réunion.</p>`;
  const y = box.scrollTop; box.innerHTML = bar + body; box.scrollTop = y;
  const gen = $(".cr-gen", box);
  if (gen) gen.onclick = async () => {
    if (d.report && !confirm("Régénérer le compte rendu ? (l'actuel sera remplacé)")) return;
    try { await api(player.url + "/report", { method: "POST" }); loadPlayerReport(player.url); } catch (e) { toast(e.message, 4000); }
  };
  const cp = $(".cr-copy", box);
  if (cp) cp.onclick = () => { const c = box.cloneNode(true); c.querySelector(".cr-bar")?.remove(); c.querySelector(".method")?.remove(); copyText(c.innerText); };
}
async function loadPlayerReport(url) {
  clearTimeout(loadPlayerReport._t);
  try {
    const d = await api(url + "/report");
    if (player.url !== url) return;
    player.rep = d;
    paintPlayerCr();
    if (d.job?.state === "running") loadPlayerReport._t = setTimeout(() => player.url === url && loadPlayerReport(url), 4000);
  } catch (e) { if (player.url === url) $("#playerCr").innerHTML = `<p class="empty">Compte rendu indisponible : ${esc(e.message)}</p>`; }
}
// at : position de départ en secondes (horodatage cliqué) ; sans at, reprise là où on en était.
// play() est appelé tout de suite (geste de l'utilisateur, exigé par iOS) ; la position est posée
// dès que la durée est connue (une vidéo pas encore chargée ignore currentTime).
function showVideo(on, at) {
  const v = $("#player video"), pl = $("#player");
  if (!player.hasVideo) return;
  player.videoOn = on;
  if (on) {
    if (!v.getAttribute("src")) { v.preload = "metadata"; v.src = player.url; applySink(v); v.load(); }
    if (at != null) {
      const url = player.url, go = () => { if (player.url !== url) return; try { v.currentTime = Math.min(at, Math.max(0, (v.duration || at + 1) - 0.5)); } catch (e) { } };
      if (v.readyState >= 1) go(); else v.addEventListener("loadedmetadata", go, { once: true });
    }
    pl.classList.remove("novid"); v.play().catch(() => { });
    if (at == null) pl.scrollTo({ top: 0, behavior: "smooth" });
  } else { v.pause(); pl.classList.add("novid"); }
  $("#playerPlay").textContent = on ? "⏸ Masquer la vidéo" : "▶ Lire la vidéo";
}
$("#playerPlay").onclick = () => showVideo(!player.videoOn);
$("#player .back").onclick = () => closePlayer();
function closePlayer(fromNav = false) {
  if (!fromNav && location.hash.startsWith("#enr/")) { location.hash = recBack || "#rec"; return; }   // la navigation ferme la page
  const v = $("#player video"); v.pause(); v.removeAttribute("src"); v.load(); $("#player").classList.add("hidden");
  $("#playerTr").innerHTML = ""; $("#playerSpk").innerHTML = ""; $("#playerCr").innerHTML = ""; $("#playerAn").innerHTML = "";
  player = { url: null }; clearTimeout(loadPlayerReport._t);
}
$("#player .close").onclick = () => closePlayer();
$("#playerDel").onclick = async () => { if (await deleteRecording(player.url, player.title)) closePlayer(); };
// Réunion passée : lecture des diapositives à partir de la vidéo (modèle d'image d'OVH)
$("#playerSlides").onclick = async () => {
  const url = player.url, b = $("#playerSlides");
  if (!confirm("Lire les diapositives et documents partagés dans cette vidéo ?\n\nL'IA retranscrit leur texte et décrit les schémas ; ils s'insèrent dans la transcription. Compter environ 1 minute par quart d'heure de réunion. Ensuite, « ↻ Régénérer » le compte rendu pour qu'il en tienne compte.")) return;
  try { await api(url + "/slides", { method: "POST" }); } catch (e) { return toast(e.message, 5000); }
  b.disabled = true;
  const [, w, f] = url.match(/^\/api\/recordings\/([^/]+)\/([^/?]+)/);
  const poll = async () => {
    if (player.url !== url) return;
    try {
      const r = (await api("/api/recordings")).find(x => x.worker === decodeURIComponent(w) && x.name === decodeURIComponent(f));
      const j = r?.slides_job || {};
      if (j.state === "running") { $("span", b).textContent = `${j.step || "Lecture"} · ${j.n || 0} diapositive${(j.n || 0) > 1 ? "s" : ""}`; return setTimeout(poll, 4000); }
      b.disabled = false; $("span", b).textContent = "Lire les diapositives";
      if (j.state === "error") return toast("Lecture des diapositives : " + (j.error || "échec"), 6000);
      toast(j.n ? `${j.n} diapositive${j.n > 1 ? "s" : ""} lue${j.n > 1 ? "s" : ""} : elles sont dans la transcription. Régénérez le compte rendu pour qu'il en tienne compte.` : "Aucune diapositive ni document partagé trouvé dans cette vidéo.", 7000);
      const r2 = recItems.find(x => x.worker === decodeURIComponent(w) && x.name === decodeURIComponent(f)) || r;
      if (r2) playRec({ ...r2, transcript: true }, "tr");
      loadRecordings();
    } catch (e) { setTimeout(poll, 6000); }
  };
  poll();
};
$("#player").onclick = (e) => { if (e.target.id === "player") closePlayer(); };
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !document.querySelector("dialog[open]")) { closeMenus(); if (player.url) closePlayer(); } });

// ------------------------------------------------------------------ démarrage
let timers = [];

// ------------------------------------------------------------------ comptes
const initials = (n) => String(n || "?").split(/[\s.@_-]+/).filter(Boolean).slice(0, 2).map(w => w[0].toUpperCase()).join("");
function applyAccount() {
  $("#accountName").textContent = me.name || me.login;
  $("#accountInitials").textContent = initials(me.name || me.login);
  $("#accountBtn").title = `Mon compte (${me.login}${me.admin ? ", administrateur" : ""})`;
  $("#usersBox").classList.toggle("hidden", !me.admin);
  $("#voiceBox").classList.toggle("hidden", !me.admin);
  $("#lexText").readOnly = !me.admin;
  $("#lexSave").classList.toggle("hidden", !me.admin);
}
function ownerLine(r) {
  if (r.mine || !r.owner_name) return r.owner ? "" : (me?.admin ? `<small class="owner">👤 sans propriétaire (d'avant les comptes)</small>` : "");
  return `<small class="owner">${r.can_edit ? "👤 " : "🔗 partagée par "}${esc(r.owner_name)}</small>`;
}
function showSecret(title, text, value) {
  $("#secTitle").textContent = title; $("#secText").textContent = text; $("#secValue").value = value;
  $("#dlgSecret").showModal(); $("#secValue").select();
}
$("#secCopy").onclick = async () => { try { await navigator.clipboard.writeText($("#secValue").value); toast("Copié"); } catch (e) { $("#secValue").select(); } };
$("#dlgSecret").addEventListener("close", () => { $("#secValue").value = ""; });

// Mon compte : nom, noms de l'assistant, mot de passe
// Mon compte : feuille (profil, mot de passe, thème, audio, déconnexion)
$("#accountBtn").onclick = () => {
  $("#meInitials").textContent = initials(me.name || me.login);
  $("#meName").textContent = me.name || me.login;
  $("#meLogin").textContent = me.login + (me.admin ? " · administrateur" : "");
  $("#dlgMe").showModal();
};
$("#mePw").onclick = () => { $("#dlgMe").close(); askPassword(false); };
$("#meProfile").onclick = () => {
  $("#dlgMe").close();
  $("#acLogin").textContent = `Identifiant : ${me.login}${me.admin ? " · administrateur" : ""}`;
  $("#acFull").value = me.name || "";
  const b = me.bot_names || {};
  $("#acBotTeams").value = b.teams || ""; $("#acBotZoom").value = b.zoom || ""; $("#acBotWeb").value = b.web || "";
  $("#dlgAccount").showModal();
};
for (const id of ["#audioBtn", "#logout"]) $(id).addEventListener("click", () => $("#dlgMe").close(), true);
$("#acPw").onclick = () => { $("#dlgAccount").close(); askPassword(false); };
$("#dlgAccount").addEventListener("close", async () => {
  if ($("#dlgAccount").returnValue !== "save") return;
  try {
    me = await api("/api/me", { method: "PATCH", json: { name: $("#acFull").value.trim(),
      bot_names: { teams: $("#acBotTeams").value.trim(), zoom: $("#acBotZoom").value.trim(), web: $("#acBotWeb").value.trim() } } });
    cfg = await api("/api/config"); nameEdited = false; prefillName(); applyAccount(); toast("Compte enregistré");
  } catch (e) { toast(e.message); }
});

// Changement de mot de passe (obligatoire après un mot de passe provisoire)
let pwForced = false;
function askPassword(forced, current = "") {
  pwForced = forced;
  $("#pwWhy").textContent = forced ? "Mot de passe provisoire : choisissez le vôtre pour continuer." : "";
  $("#pwUser").value = me?.login || "";
  $("#pwCur").value = current; $("#pwNew").value = ""; $("#pwNew2").value = ""; $("#pwErr").textContent = "";
  $("#pwCancel").classList.toggle("hidden", forced);
  $("#dlgPw").showModal();
  (current ? $("#pwNew") : $("#pwCur")).focus();
}
$("#dlgPw").addEventListener("cancel", (e) => { if (pwForced) e.preventDefault(); });   // Échap : pas tant qu'il est provisoire
$("#pwOk").onclick = async (e) => {
  e.preventDefault();
  const err = $("#pwErr"); err.textContent = "";
  if ($("#pwNew").value.length < 10) return err.textContent = "10 caractères au moins.";
  if ($("#pwNew").value !== $("#pwNew2").value) return err.textContent = "La confirmation ne correspond pas.";
  try {
    await api("/api/me/password", { method: "POST", json: { current: $("#pwCur").value, new: $("#pwNew").value } });
    pwForced = false; me.must_change = false; $("#dlgPw").close(); toast("Mot de passe changé");
  } catch (x) { err.textContent = x.message; }
};

// Partage d'une réunion (propriétaire ou administrateur)
let shareTarget = null;
async function shareRecording(r, title) {
  let dir;
  try { dir = await api("/api/users/directory"); } catch (e) { return toast(e.message); }
  shareTarget = r;
  $("#shTitle").textContent = title;
  const owner = r.owner || "";
  $("#shList").innerHTML = dir.filter(x => x.login !== owner).map(x => `<label><input type="checkbox" value="${esc(x.login)}"
    ${(r.shared_with || []).includes(x.login) ? "checked" : ""}> ${esc(x.name)} <small>(${esc(x.login)})</small></label>`).join("")
    || "<small>Aucun autre compte.</small>";
  $("#shOwnerRow").classList.toggle("hidden", !me.admin);
  if (me.admin) $("#shOwner").innerHTML = (owner ? "" : `<option value="">— aucun (administrateurs seulement) —</option>`) + dir.map(x =>
    `<option value="${esc(x.login)}" ${x.login === owner ? "selected" : ""}>${esc(x.name)} (${esc(x.login)})</option>`).join("");
  $("#dlgShare").showModal();
}
$("#dlgShare").addEventListener("close", async () => {
  if ($("#dlgShare").returnValue !== "ok" || !shareTarget) return;
  const r = shareTarget; shareTarget = null;
  const body = { shared_with: [...$("#shList").querySelectorAll("input:checked")].map(x => x.value) };
  if (me.admin && $("#shOwner").value && $("#shOwner").value !== (r.owner || "")) body.owner = $("#shOwner").value;
  try { await api(recUrl(r) + "/access", { method: "PUT", json: body }); toast("Partage enregistré"); loadRecordings(); }
  catch (e) { toast(e.message); }
});

// Utilisateurs (administrateurs)
async function loadUsers() {
  const box = $("#usersList");
  try {
    const list = await api("/api/users");
    box.innerHTML = list.map(u => `<div class="user ${u.disabled ? "off" : ""}" data-login="${esc(u.login)}">
      <div class="who"><b>${esc(u.name)}</b> <small>${esc(u.login)}${u.login === me.login ? " · vous" : ""}</small>
        <small>${u.disabled ? "désactivé" : u.last_login ? "dernière connexion le " + new Date(u.last_login).toLocaleString("fr-FR") : "jamais connecté"}${u.must_change && !u.disabled ? " · mot de passe provisoire" : ""}</small></div>
      <select class="role" ${u.login === me.login ? "disabled" : ""}><option value="user" ${u.role === "user" ? "selected" : ""}>Utilisateur</option>
        <option value="admin" ${u.role === "admin" ? "selected" : ""}>Administrateur</option></select>
      <button class="ghost reset" title="Nouveau mot de passe provisoire">🔑</button>
      ${u.login === me.login ? "" : `<button class="ghost toggle">${u.disabled ? "Réactiver" : "Désactiver"}</button>
      <button class="ghost danger remove" title="Supprimer le compte (ses réunions restent, visibles des administrateurs)">🗑</button>`}
    </div>`).join("");
    box.querySelectorAll(".user").forEach(row => {
      const login = row.dataset.login, u = list.find(x => x.login === login);
      $(".role", row).onchange = async (e) => {
        try { await api(`/api/users/${login}`, { method: "PATCH", json: { role: e.target.value } }); toast("Rôle modifié"); }
        catch (x) { toast(x.message); } loadUsers();
      };
      $(".reset", row).onclick = async () => {
        if (!confirm(`Donner un nouveau mot de passe provisoire à ${u.name} ? Ses sessions ouvertes seront fermées.`)) return;
        try { const d = await api(`/api/users/${login}/password`, { method: "POST" });
          showSecret("Nouveau mot de passe provisoire", `Pour ${u.name} (identifiant ${login}).`, d.password); loadUsers();
        } catch (x) { toast(x.message); }
      };
      if ($(".toggle", row)) $(".toggle", row).onclick = async () => {
        try { await api(`/api/users/${login}`, { method: "PATCH", json: { disabled: !u.disabled } }); } catch (x) { toast(x.message); } loadUsers();
      };
      if ($(".remove", row)) $(".remove", row).onclick = async () => {
        if (!confirm(`Supprimer le compte de ${u.name} ? Ses réunions restent, visibles des administrateurs.`)) return;
        try { await api(`/api/users/${login}`, { method: "DELETE" }); } catch (x) { toast(x.message); } loadUsers();
      };
    });
  } catch (e) { box.textContent = "Erreur : " + e.message; }
}
$("#usersBox").addEventListener("toggle", () => { if ($("#usersBox").open) loadUsers(); });

// Empreintes vocales : consentements attestés sur l'honneur par un administrateur
let consentTarget = null, consentText = "";
const fmtMin = (sec) => sec >= 90 ? `${(sec / 60).toLocaleString("fr-FR", { maximumFractionDigits: 1 })} min` : `${Math.round(sec)} s`;
const VP_KIND = { moi: "micro de l'assistant (canal « Moi »)", "nommée": "voix nommée à la main", vignette: "voix nommée d'après sa vignette" };
const stemDate = (stem) => { const m = /^(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)/.exec(stem || ""); return m ? `${m[3]}/${m[2]}/${m[1]} ${m[4]}:${m[5]}` : stem || ""; };
function vpLine(name, vp) {
  if (!vp) return "";
  const job = vp.job, p = vp.people?.[name] || {};
  const todo = p.pending ? ` · <b>${p.pending} voix à écouter</b>` : "";
  let h;
  if (p.sec) h = `<small class="vp ok">🎙 empreinte vocale active : ${fmtMin(p.sec)} de voix validée, ${p.meetings} réunion${p.meetings > 1 ? "s" : ""}`
    + (p.sec < 60 ? " (trop peu pour reconnaître : une minute au moins)" : "") + `${todo}</small>`;
  else if (p.voices) h = `<small class="vp">🎙 pas encore d'empreinte : aucune voix validée${todo}</small>`;
  else h = `<small class="vp">🎙 empreinte vocale : pas encore de passage sûr (réunion où cette personne parle avec sa propre vignette, ou nommée à la main) ; recherche chaque nuit</small>`;
  if (job?.state === "running" && job.names?.includes(name)) h += `<small class="vp">⏳ recherche des voix en cours…</small>`;
  else if (vp.scheduled?.includes(name)) h += `<small class="vp">⏳ extraits refusés : nouvelle recherche dans une minute</small>`;
  return h;
}
function vpVoices(name, vp) {
  const p = vp?.people?.[name];
  if (!p?.voices) return "";
  return `<details class="vp-voices" data-name="${esc(name)}"><summary><svg class="i"><use href="#i-audio"/></svg>Écouter et valider les voix
    <small>${p.voices} voix${p.pending ? ` · ${p.pending} à écouter` : ""}${p.validated ? ` · ${p.validated} validée${p.validated > 1 ? "s" : ""}` : ""}${p.rejected ? ` · ${p.rejected} refusée${p.rejected > 1 ? "s" : ""}` : ""}</small></summary>
    <p class="hint">Chaque voix regroupe les passages d'une réunion attribués à ${esc(name)}. Écoutez les extraits : l'empreinte n'est
      calculée qu'à partir des voix que vous validez. Un extrait qui n'est pas ${esc(name)} se refuse seul (✕) ; une voix
      ou un extrait peut aussi être attribué à une autre personne cochée (« C'est… »).</p><div class="vv-list"><small>Chargement…</small></div></details>`;
}
let vpPeople = [];      // personnes cochées (seules à pouvoir recevoir une voix)
const toOpts = (name, label) => `<option value="">${label}</option>` + vpPeople.filter(n => n !== name).map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
function clipH(name, v, c) {
  const url = `/api/voiceprints/clip?name=${encodeURIComponent(name)}&id=${encodeURIComponent(v.id)}&t=${c.t}`;
  const others = vpPeople.some(n => n !== name);
  return `<div class="clip-row${c.refused ? " refused" : ""}" data-t="${c.t}">
    <button type="button" class="clip${c.odd ? " odd" : ""}" data-url="${esc(url)}"><svg class="i"><use href="#i-play"/></svg>
      <span><b>${fmtT(c.t)}</b>${c.odd ? " · le passage le moins ressemblant" : ""}<br>« ${esc(c.text || "")} »</span></button>
    ${v.manual || v.ok === false ? "" : c.refused ? "" : `<button type="button" class="cx" title="Ce passage n'est pas ${esc(name)}" aria-label="Ce passage n'est pas ${esc(name)}"><svg class="i"><use href="#i-x"/></svg></button>`}
    ${c.refused ? `<div class="clip-ref"><small>✕ pas ${esc(name)}${c.to ? ` · attribué à <b>${esc(c.to)}</b>` : c.gone ? " · retiré de la voix, avec les passages qui lui ressemblaient" : " · retiré de l'empreinte"}</small>
      <button type="button" class="ghost cx-undo">Rétablir</button>
      ${!c.to && c.attributable && others ? `<select class="cx-to" aria-label="Attribuer ce passage à">${toOpts(name, "C'est…")}</select>` : ""}</div>` : ""}</div>`;
}
function voiceH(name, v) {
  const cls = v.ok === true ? "ok" : v.ok === false ? "no" : "todo";
  const st = { ok: "✓ validée", no: v.to ? "✕ attribuée à " + esc(v.to) : "✕ refusée", todo: "à écouter" }[cls];
  const refused = (v.clips || []).filter(c => c.refused).length;
  const origin = v.manual ? `attribuée par vous depuis les voix de « ${esc(v.from || "")} » · ${esc(VP_KIND[v.kind] || v.kind || "")}`
    : `${esc(VP_KIND[v.kind] || v.kind)}${v.kind !== "moi" && v.label ? ` « ${esc(v.label)} »` : ""}`;
  return `<div class="vv ${cls}${v.manual ? " manual" : ""}" data-id="${esc(v.id)}">
    <div class="vv-h"><b>${esc(stemDate(v.meeting))}</b><span>${esc(v.title || "réunion sans titre")}</span><span class="vv-st ${cls}">${st}</span></div>
    <div class="vv-m">${origin} · ${v.n} passage${v.n > 1 ? "s" : ""}, ${fmtMin(v.sec)}${v.dropped ? ` · ${v.dropped} écarté${v.dropped > 1 ? "s" : ""} (trop différent${v.dropped > 1 ? "s" : ""})` : ""}${v.dropped_neg ? ` · ${v.dropped_neg} retiré${v.dropped_neg > 1 ? "s" : ""} (semblable${v.dropped_neg > 1 ? "s" : ""} à un extrait refusé)` : ""}${refused ? ` · ${refused} extrait${refused > 1 ? "s" : ""} refusé${refused > 1 ? "s" : ""}` : ""}</div>
    <div class="vv-clips">${(v.clips || []).map(c => clipH(name, v, c)).join("")}</div>
    ${v.manual ? `<div class="vv-act"><button type="button" class="nope danger">Retirer cette voix</button>
      ${v.at ? `<small>attribuée le ${new Date(v.at).toLocaleDateString("fr-FR")}${v.by ? " par " + esc(v.by) : ""}</small>` : ""}</div>`
    : v.to ? `<div class="vv-act"><button type="button" class="nope">Remettre à écouter</button>
      <small>attribuée à ${esc(v.to)}${v.at ? ` le ${new Date(v.at).toLocaleDateString("fr-FR")}` : ""} : en cas d'erreur, retirez-la aussi dans les voix de ${esc(v.to)}</small></div>`
    : `<div class="vv-act"><button type="button" class="yes ${v.ok === true ? "primary" : ""}">C'est bien ${esc(name)}</button>
      <button type="button" class="nope ${v.ok === false && !v.to ? "danger" : ""}">Ce n'est pas cette personne</button>
      ${vpPeople.some(n => n !== name) && !v.to ? `<select class="vv-to" aria-label="Attribuer toute cette voix à">${toOpts(name, "C'est quelqu'un d'autre…")}</select>` : ""}
      ${v.ok != null && v.at ? `<small>${v.to ? "attribuée" : v.ok ? "validée" : "refusée"} le ${new Date(v.at).toLocaleDateString("fr-FR")}${v.by ? " par " + esc(v.by) : ""}</small>` : ""}</div>`}
    ${!v.manual && v.ok !== false ? `<p class="vv-tip">Un extrait n'est pas ${esc(name)} ? Touchez ✕ à sa droite : il est retiré, puis la voix est recalculée sans les passages qui lui ressemblent.</p>` : ""}</div>`;
}
// un seul lecteur pour les extraits (lecture lancée dans le geste de l'utilisateur, exigé par iOS)
const vpAudio = new Audio(); vpAudio.preload = "none"; let vpBtn = null, vpUrl = "";
const vpIcon = (b, on) => { const u = b && $("use", b); if (u) u.setAttribute("href", on ? "#i-pause" : "#i-play"); };
function vpStop() { if (vpBtn) { vpBtn.classList.remove("playing", "loading"); vpIcon(vpBtn, false); } vpBtn = null; vpUrl = ""; }
vpAudio.onplaying = () => vpBtn?.classList.remove("loading");
vpAudio.onended = vpAudio.onpause = vpStop;
vpAudio.onerror = () => { if (vpBtn) toast("Extrait illisible (assistant injoignable ?)"); vpStop(); };
function playClip(btn, url) {
  if (vpBtn === btn && !vpAudio.paused) { vpAudio.pause(); return; }
  vpStop(); vpBtn = btn; vpUrl = url; btn.classList.add("playing", "loading"); vpIcon(btn, true);
  vpAudio.src = url; applySink(vpAudio);
  vpAudio.play().catch(e => { if (e.name !== "AbortError") toast("Lecture impossible : " + e.message); vpStop(); });
}
const vpOpen = new Set(), vpCache = {};
function renderVoices(list, name, d) {
  list.innerHTML = d.voices.length ? d.voices.map(v => voiceH(name, v)).join("") : "<small>Aucune voix trouvée.</small>";
  const post = async (path, json, msg) => {
    try { await api(path, { method: "POST", json }); toast(msg); } catch (e) { toast(e.message); }
    loadConsents();
  };
  list.querySelectorAll(".vv").forEach(el => {
    const v = d.voices.find(x => x.id === el.dataset.id);
    el.querySelectorAll(".clip").forEach(b => {
      b.onclick = () => playClip(b, b.dataset.url);
      if (vpUrl && b.dataset.url === vpUrl && !vpAudio.paused) { vpBtn = b; b.classList.add("playing"); vpIcon(b, true); }
    });
    el.querySelectorAll(".clip-row").forEach(row => {
      const t = +row.dataset.t;
      if ($(".cx", row)) $(".cx", row).onclick = () => post("/api/voiceprints/clip-review", { name, id: v.id, t, ok: false },
        "Extrait refusé : retiré de l'empreinte ; la voix sera recalculée dans une minute sans les passages qui lui ressemblent");
      if ($(".cx-undo", row)) $(".cx-undo", row).onclick = () => post("/api/voiceprints/clip-review", { name, id: v.id, t, ok: null }, "Extrait rétabli");
      if ($(".cx-to", row)) $(".cx-to", row).onchange = (e) => {
        const to = e.target.value; if (!to) return;
        if (!confirm(`Attribuer ce passage à ${to} ? Il comptera dans l'empreinte de ${to}.`)) { e.target.value = ""; return; }
        post("/api/voiceprints/attribute", { name, id: v.id, t, to }, `Passage attribué à ${to}`);
      };
    });
    const decide = async (ok) => {
      const want = v.manual ? false : v.ok === ok ? null : ok;   // nouveau clic sur le choix déjà fait : voix remise à écouter
      if (v.manual && !confirm(`Retirer cette voix de l'empreinte de ${name} ?`)) return;
      post("/api/voiceprints/review", { name, id: v.id, ok: want },
        v.manual ? "Voix retirée" : want === true ? "Voix validée : empreinte recalculée" : want === false ? "Voix refusée : elle n'est plus utilisée" : "Voix remise à écouter");
    };
    if ($(".yes", el)) $(".yes", el).onclick = () => decide(true);
    if ($(".nope", el)) $(".nope", el).onclick = () => decide(false);
    if ($(".vv-to", el)) $(".vv-to", el).onchange = (e) => {
      const to = e.target.value; if (!to) return;
      if (!confirm(`Attribuer toute cette voix à ${to} ? Elle sera refusée pour ${name} et comptera dans l'empreinte de ${to}.`)) { e.target.value = ""; return; }
      post("/api/voiceprints/attribute", { name, id: v.id, to }, `Voix attribuée à ${to}`);
    };
  });
}
async function loadVoices(det, name) {
  const list = $(".vv-list", det);
  if (vpCache[name]) renderVoices(list, name, vpCache[name]);
  try { vpCache[name] = await api(`/api/voiceprints/voices?name=${encodeURIComponent(name)}`); renderVoices(list, name, vpCache[name]); }
  catch (e) { list.innerHTML = `<small>Erreur : ${esc(e.message)}</small>`; }
}
let vpPoll = null;
async function loadConsents() {
  const box = $("#voiceList");
  try {
    const [d, vp] = await Promise.all([api("/api/voice-consents"), api("/api/voiceprints").catch(() => null)]);
    consentText = d.attestation; vpPeople = Object.keys(vp?.people || {});
    const st = $("#vpStatus"), job = vp?.job, n = Object.keys(vp?.people || {}).length;
    const active = Object.values(vp?.people || {}).filter(p => p && p.sec >= 60).length;
    const pending = Object.values(vp?.people || {}).reduce((a, p) => a + (p?.pending || 0), 0);
    st.innerHTML = !vp || !n ? "" : `<div class="row"><small>${job?.state === "running" ? "⏳ Recherche des voix en cours sur les assistants…"
      : `${active} empreinte${active > 1 ? "s" : ""} active${active > 1 ? "s" : ""} sur ${n} personne${n > 1 ? "s" : ""} cochée${n > 1 ? "s" : ""}`
        + (pending ? `, <b>${pending} voix à écouter</b>` : "") + `. Une empreinte n'est faite que des voix validées à l'écoute. Assistants : `
        + Object.entries(vp.workers || {}).map(([w, s]) => `${esc(w)} ${esc(s)}`).join(", ")}</small>
      <button type="button" class="ghost" id="vpBuild" title="Chercher les voix des personnes cochées dans les enregistrements (fait aussi chaque nuit)" ${job?.state === "running" ? "disabled" : ""}><svg class="i"><use href="#i-refresh"/></svg>Chercher les voix</button></div>`;
    if ($("#vpBuild")) $("#vpBuild").onclick = async () => {
      try { await api("/api/voiceprints/build", { method: "POST" }); toast("Recherche des voix lancée"); } catch (e) { toast(e.message); }
      loadConsents(); };
    clearTimeout(vpPoll);
    if ((job?.state === "running" || vp?.scheduled?.length) && $("#voiceBox").open) vpPoll = setTimeout(loadConsents, 5000);
    box.innerHTML = d.items.length ? d.items.map(x => `<div class="user consent" data-id="${esc(x.id)}">
      <label class="ck"><input type="checkbox" ${x.consent ? "checked" : ""}> <b>${esc(x.name)}</b></label>
      <div class="who"><small>${x.consent ? `✅ consentement attesté le ${new Date(x.at).toLocaleString("fr-FR")} par ${esc(x.by_name || x.by)}`
        : x.at ? `retiré le ${new Date(x.at).toLocaleString("fr-FR")}` : "en attente du consentement"}</small>
        ${x.note ? `<small>📝 ${esc(x.note)}</small>` : ""}${x.consent ? vpLine(x.name, vp) : ""}</div>
      ${x.consent ? "" : `<button class="ghost danger remove" title="Retirer de la liste">🗑</button>`}${x.consent ? vpVoices(x.name, vp) : ""}</div>`).join("")
      : "<small>Personne pour l'instant.</small>";
    box.querySelectorAll(".vp-voices").forEach(det => {
      const name = det.dataset.name;
      det.addEventListener("toggle", () => { if (det.open) { vpOpen.add(name); loadVoices(det, name); } else vpOpen.delete(name); });
      if (vpOpen.has(name)) det.open = true;
    });
    box.querySelectorAll(".consent").forEach(row => {
      const x = d.items.find(i => i.id === row.dataset.id), cb = $("input[type=checkbox]", row);
      cb.onchange = async () => {
        if (cb.checked) {          // attestation sur l'honneur dans une fenêtre
          cb.checked = false; consentTarget = x;
          $("#csName").textContent = x.name; $("#csText").textContent = consentText;
          $("#csAttest").checked = false; $("#csOk").disabled = true; $("#csNote").value = x.note || "";
          $("#dlgConsent").showModal();
        } else {
          if (!confirm(`Retirer le consentement de ${x.name} ? Son empreinte vocale sera effacée.`)) { cb.checked = true; return; }
          try { await api(`/api/voice-consents/${x.id}`, { method: "PUT", json: { consent: false } }); toast("Consentement retiré"); }
          catch (e) { toast(e.message); } loadConsents();
        }
      };
      if ($(".remove", row)) $(".remove", row).onclick = async () => {
        if (!confirm(`Retirer ${x.name} de la liste ?`)) return;
        try { await api(`/api/voice-consents/${x.id}`, { method: "DELETE" }); } catch (e) { toast(e.message); } loadConsents();
      };
    });
  } catch (e) { box.textContent = "Erreur : " + e.message; }
}
$("#voiceBox").addEventListener("toggle", () => { if ($("#voiceBox").open) loadConsents(); });
$("#csAttest").onchange = () => { $("#csOk").disabled = !$("#csAttest").checked; };
$("#dlgConsent").addEventListener("close", async () => {
  const x = consentTarget; consentTarget = null;
  if ($("#dlgConsent").returnValue !== "ok" || !x || !$("#csAttest").checked) return;
  try {
    await api(`/api/voice-consents/${x.id}`, { method: "PUT", json: { consent: true, attest: true, note: $("#csNote").value.trim() } });
    toast(`Consentement de ${x.name} enregistré`);
  } catch (e) { toast(e.message); }
  loadConsents();
});
$("#voiceAdd").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/api/voice-consents", { method: "POST", json: { name: $("#va-name").value.trim(), note: $("#va-note").value.trim() || null } });
    $("#va-name").value = ""; $("#va-note").value = ""; loadConsents();
  } catch (x) { toast(x.message); }
};
$("#userAdd").onsubmit = async (e) => {
  e.preventDefault();
  try {
    const d = await api("/api/users", { method: "POST", json: { login: $("#ua-login").value.trim().toLowerCase(),
      name: $("#ua-name").value.trim(), role: $("#ua-role").value } });
    showSecret("Compte créé", `${d.user.name} — identifiant ${d.user.login}. Mot de passe provisoire :`, d.password);
    $("#ua-login").value = ""; $("#ua-name").value = ""; $("#ua-role").value = "user"; loadUsers();
  } catch (x) { toast(x.message); }
};

async function boot() {
  const m = await api("/api/me");
  if (!m.logged_in) { if (localStorage.lastLogin) $("#loginId").value = localStorage.lastLogin; return showLogin(); }
  me = m.user;
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  cfg = await api("/api/config");
  applyAccount();
  if (!$("#f-dur").value) $("#f-dur").value = cfg.defaultDuration || "";
  prefillName();
  const sel = $("#f-worker");
  cfg.workers.forEach(w => { if (![...sel.options].some(o => o.value === w)) sel.add(new Option(w, w)); card(w); tile(w); });
  timers.forEach(clearInterval);
  await refresh(); loadSchedule();
  showTab(location.hash.slice(1) || "home");   // charge aussi les enregistrements (accueil, compteur du menu)
  if (view() === "meet") loadRecordings();
  timers = [setInterval(refresh, 3000), setInterval(refreshShots, 1500), setInterval(loadSchedule, 30000),
    setInterval(pollTranscripts, 1500), setInterval(() => view() === "home" && loadRecordings(), 60000)];
}
boot();
