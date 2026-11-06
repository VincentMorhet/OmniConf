"use strict";
const $ = (s, el = document) => el.querySelector(s);
const STATE_FR = { idle: "libre", joining: "connexion…", lobby: "salle d'attente", in_meeting: "en réunion",
  leaving: "départ…", error: "erreur", offline: "hors ligne", manual: "navigation manuelle" };
let cfg = { iceServers: [], workers: [] };
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
  try { await api("/api/login", { method: "POST", json: { password: $("#pw").value } }); boot(); }
  catch (err) { $("#loginErr").textContent = err.message; }
};
$("#logout").onclick = async () => { await api("/api/logout", { method: "POST" }); location.reload(); };

// ------------------------------------------------------------------ onglets (menu)
function showTab(tab) {
  if (!["live", "rec"].includes(tab)) tab = "live";
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x.dataset.tab === tab));
  $("#tab-live").classList.toggle("hidden", tab !== "live");
  $("#tab-rec").classList.toggle("hidden", tab !== "rec");
  if (location.hash !== "#" + tab) history.replaceState(null, "", "#" + tab);
  if (tab === "rec") loadRecordings();
  window.scrollTo(0, 0);
}
document.querySelectorAll(".tab").forEach(b => b.onclick = () => showTab(b.dataset.tab));
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
    toast(`Connexion en cours sur ${r.name}…`); $("#f-url").value = ""; nameEdited = false; prefillName(); refresh();
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
function detectPlatform(u) {
  try { const h = new URL(u).hostname;
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

// ------------------------------------------------------------------ workers
function card(name) {
  if (cards[name]) return cards[name];
  const el = $("#tplWorker").content.firstElementChild.cloneNode(true);
  $(".wname", el).textContent = name;
  const act = (a, body) => async () => {
    try { await api(`/api/workers/${name}/${a}`, { method: "POST", json: body || {} }); refresh(); }
    catch (err) { toast("Erreur : " + err.message, 5000); }
  };
  $(".b-leave", el).onclick = () => confirm(`Quitter la réunion sur ${name} ?`) && act("leave")();
  $(".b-mute", el).onclick = act("mute");
  $(".b-rec", el).onclick = () => act(lastStatus[name]?.recording ? "rec-stop" : "rec-start")();
  const openScreen = () => window.open(`/novnc/vnc.html?path=vnc/${name}/websockify&autoconnect=1&resize=scale&reconnect=1&show_dot=1`, "_blank");
  $(".b-screen", el).onclick = openScreen;
  $(".shot", el).onclick = openScreen;
  $(".b-open", el).onclick = () => {
    const u = prompt("URL à ouvrir dans le navigateur du worker (ex : se connecter une fois à son compte, la session est conservée)",
      "https://teams.microsoft.com/");
    if (u) act("open", { url: u })().then(() => openScreen());
  };
  $(".b-devs", el).onclick = openAudioPanel;
  $(".b-tr", el).onclick = () => { const t = trState(name); t.open = !t.open; t.userToggled = true; renderTr(name); };
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
    body.querySelectorAll("p").forEach(p => { if (+p.dataset.t <= sec + 1) best = p; });
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
  const el = card(st.name);
  lastStatus[st.name] = st;
  const b = $(".badge", el); b.className = "badge " + st.state; b.textContent = STATE_FR[st.state] || st.state;
  $(".rec", el).classList.toggle("hidden", !st.recording);
  if (st.recording) $(".rectime", el).textContent = fmtDur(st.recording_since);
  const info = [];
  if (st.platform) info.push(["teams", "zoom"].includes(st.platform) || !st.url ? st.platform.toUpperCase() : siteOf(st.url));
  if (st.title) info.push(st.title);
  if (st.url) info.push(`<span title="${st.url}">${st.url.slice(0, 60)}${st.url.length > 60 ? "…" : ""}</span>`);
  if (st.listeners) info.push(`🎧 ${st.listeners}`);
  if (st.auto_leave_at) info.push("fin auto " + new Date(st.auto_leave_at).toLocaleTimeString().slice(0, 5));
  let html = info.join(" · ");
  if (st.error) html += `<div class="e">⚠ ${st.error} — terminez à la main via « Écran ».</div>`;
  else if (st.note && st.state === "in_meeting") html += `<div class="n">ℹ ${st.note}</div>`;
  $(".winfo", el).innerHTML = html;
  $(".b-rec", el).textContent = st.recording ? "⏹ Arrêter rec" : "⏺ Enregistrer";
  const busy = ["joining", "lobby", "in_meeting"].includes(st.state);
  $(".b-leave", el).disabled = !busy && st.state !== "manual" && st.state !== "error";
  $(".b-mute", el).disabled = st.state !== "in_meeting";
  el.style.opacity = st.online ? 1 : .55;
  const tr = st.transcript || {}, t = trState(st.name);
  $(".trb", el).classList.toggle("hidden", !tr.active);
  if (tr.active && !t.userToggled) t.open = true;           // s'ouvre tout seul au début d'une transcription
  t.status = tr;
  if (tr.analysis && tr.analysis.v !== t.anV && !t.anLoading) fetchAnalysis(st.name);
  renderTr(st.name);
}

// ------------------------------------------------------------------ transcription en direct
const trStates = {};
function trState(name) { return trStates[name] ||= { open: false, n: 0, file: null, status: {}, userToggled: false, view: "tr", anV: undefined, anSeen: 0 }; }
const fmtT = (sec) => { sec = Math.max(0, Math.floor(sec || 0)); const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60;
  return (h ? h + ":" : "") + String(m).padStart(2, "0") + ":" + String(sec % 60).padStart(2, "0"); };
// Intervenants : {id: {label, color, source}} ; alias = groupes de voix fusionnés (ancien id -> nouvel id)
const spkOf = (sp, al, id) => { const seen = new Set(); while (al && al[id] && !seen.has(id)) { seen.add(id); id = al[id]; }
  return { id, ...(sp?.[id] || { label: id || "?", color: 1 }) }; };
const SRC_FR = { ui: "nom affiché par la plateforme", voice: "reconnu à la voix", manual: "nommé à la main", me: "ta voix (IHM)" };
function paraEl(p, opts = {}) {
  const el = document.createElement("p");
  if (opts.pending) el.className = "pending";
  el.dataset.spk = p.spk || ""; el.dataset.t = p.t;
  const ts = document.createElement("span"); ts.className = "ts" + (opts.seek ? " seek" : ""); ts.textContent = fmtT(p.t);
  if (opts.seek) ts.onclick = () => opts.seek(p.t);
  const spk = document.createElement("span"); spk.className = "spk";
  spk.onclick = () => opts.rename && opts.rename(el.dataset.spk);
  el.append(ts, spk, document.createTextNode(p.text));
  paintSpk(el, opts.speakers, opts.aliases, p.speaker);
  return el;
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
    b.innerHTML = `${esc(s.label)}<small>${s.source === "ui" ? "🖥" : s.source === "voice" ? "🔊" : s.source === "manual" ? "✏️" : ""}</small>`;
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
  $(".tr-panel", el).classList.toggle("hidden", !t.open);
  const b = $(".b-tr", el); b.classList.toggle("on", t.open);
  const tog = $(".tr-toggle", el);
  tog.textContent = tr.active ? "⏹ Arrêter" : "▶ Démarrer";
  tog.disabled = !tr.enabled || (!tr.active && lastStatus[name]?.state !== "in_meeting");
  tog.title = tr.enabled ? "" : "Clé OVH AI Endpoints non configurée sur ce worker";
  const an = t.view === "an", a = tr.analysis || {};
  el.querySelectorAll(".tr-panel .vt").forEach(b => b.classList.toggle("on", b.dataset.v === t.view));
  $(".tr-body", el).classList.toggle("hidden", an); $(".tr-spk", el).classList.toggle("hidden", an);
  $(".an-body", el).classList.toggle("hidden", !an);
  $(".an-new", el).classList.toggle("hidden", an || !a.v || a.v === t.anSeen);
  $(".an-now", el).classList.toggle("hidden", !an || !tr.active);
  $(".an-now", el).disabled = !!a.running;
  const stEl = $(".tr-status", el);
  stEl.className = "tr-status" + (tr.active ? " on" : "") + ((an ? a.error : tr.error) ? " err" : "");
  if (an) {
    const at = a.at ? new Date(a.at).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }) : null;
    stEl.textContent = a.running ? "⏳ point d'étape en cours…" : a.error ? "⚠ " + a.error
      : (at ? `point n°${a.count || a.v} à ${at}` : "aucun point d'étape")
        + (tr.active && a.next_in != null ? ` · prochain dans ~${Math.max(1, Math.round(a.next_in / 60))} min` : "")
        + (!tr.active && tr.file ? " · compte rendu final : 📁 Enregistrements" : "");
    return;
  }
  stEl.textContent = tr.error ? "⚠ " + tr.error
    : tr.active ? `en direct · ${tr.paragraphs || 0} paragraphes · ${tr.n_speakers || 0} voix${tr.ui_speaker ? " · noms de la plateforme ✓" : ""}`
    : !tr.enabled ? "transcription non configurée" : tr.file ? "terminée (voir 📁 Enregistrements)" : "inactive";
}
async function pollTranscripts() {
  if (document.hidden || $("#tab-live").classList.contains("hidden")) return;
  for (const name of Object.keys(cards)) {
    const t = trState(name);
    if (!t.open || !(t.status?.active || t.status?.file)) continue;
    try {
      const d = await api(`/api/workers/${name}/transcript?after=${t.n}`);
      const body = $(".tr-body", cards[name]);
      if (d.file !== t.file || d.total < t.n) {   // nouvelle transcription : on repart de zéro
        body.innerHTML = ""; t.file = d.file;
        if (t.n) { t.n = 0; continue; }
      }
      const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
      t.speakers = d.speakers; t.aliases = d.aliases;
      const rename = (id) => askRename(t.speakers, id, async (sid, nm) => {
        const r = await api(`/api/workers/${name}/speakers`, { method: "POST", json: { id: sid, name: nm } });
        t.speakers = r.speakers; repaint(); });
      const o = { speakers: d.speakers, aliases: d.aliases, rename };
      body.querySelectorAll(".pending").forEach(x => x.remove());
      d.paragraphs.forEach(p => body.appendChild(paraEl(p, o)));
      t.n += d.paragraphs.length;
      d.pending.forEach(p => body.appendChild(paraEl(p, { ...o, pending: true })));
      const repaint = () => {   // noms et fusions de voix peuvent changer : on repeint tout
        const used = new Set();
        body.querySelectorAll("p").forEach(el => { paintSpk(el, t.speakers, t.aliases); used.add(el.dataset.spk); });
        renderChips($(".tr-spk", cards[name]), t.speakers, used, rename);
        paintAnalysis(name);
      };
      repaint();
      if (atBottom) body.scrollTop = body.scrollHeight;
    } catch (e) { }
  }
}
// ------------------------------------------------------------------ points d'étape et comptes rendus
const parseT = (s) => { if (typeof s === "number") return s;
  const m = /^\s*(\d+):(\d{1,2})(?::(\d{1,2}))?\s*$/.exec(s || ""); if (!m) return null;
  return m[3] != null ? +m[1] * 3600 + +m[2] * 60 + +m[3] : +m[1] * 60 + +m[2]; };
const tsH = (t) => { const v = parseT(t); return v == null ? "" : ` <span class="ts seek" data-t="${v}" title="Aller à ce passage">${fmtT(v)}</span>`; };
// {S1} / {ME} dans les textes du LLM -> nom coloré de l'intervenant (suit les renommages)
const whoH = (text, sp, al) => esc(typeof text === "string" ? text : text?.texte ?? text ?? "").replace(/\{(S\d+|ME)\}/g, (m, id) => {
  const s = spkOf(sp, al, id); return `<b class="who c${(s.color || 0) % 9}">${esc(s.label)}</b>`; });
const secH = (title, inner, cls = "") => inner ? `<section class="an-sec ${cls}"><h4>${title}</h4>${inner}</section>` : "";
const ulH = (items, f) => { items = (items || []).filter(Boolean); return items.length ? "<ul>" + items.map(x => `<li>${f(x)}</li>`).join("") + "</ul>" : ""; };
function bindSeek(box, seek) { box.addEventListener("click", (e) => { const ts = e.target.closest(".ts.seek"); if (ts && seek) seek(+ts.dataset.t); }); }

function analysisHtml(a, sp, al) {
  const W = (x) => whoH(x, sp, al), st = a?.state;
  if (!st) return `<p class="empty">${a?.running ? "Premier point d'étape en cours de rédaction…" :
    a?.next_in != null ? `Premier point d'étape dans ~${Math.max(1, Math.round(a.next_in / 60))} min (ou « ↻ Analyser »).` : "Aucun point d'étape pour cette réunion."}</p>`;
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

function reportHtml(rep, sp) {
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
  try { (await api("/api/workers")).forEach(render); } catch (e) { }
}

async function refreshShots() {
  if (document.hidden || $("#tab-live").classList.contains("hidden")) return;
  for (const [name, el] of Object.entries(cards)) {
    if (!lastStatus[name]?.online) continue;
    try {
      const blob = await api(`/api/workers/${name}/screenshot?w=${Math.min(1280, Math.round(el.clientWidth * devicePixelRatio))}`);
      const img = $(".shot", el); const old = img.src;
      img.src = URL.createObjectURL(blob); if (old.startsWith("blob:")) URL.revokeObjectURL(old);
    } catch (e) { }
  }
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
    btn.textContent = "🎧 Couper l'audio"; btn.classList.remove("primary");
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
  const btn = $(".b-audio", el); btn.textContent = "🎧 Rejoindre l'audio"; btn.classList.add("primary");
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
  $("#schedCard").classList.toggle("hidden", !items.length);
  for (const it of items) {
    const li = document.createElement("li");
    li.innerHTML = `<div class="t"><b></b><small></small></div><button>Supprimer</button>`;
    $("b", li).textContent = it.title || it.url;
    $("small", li).textContent = `${new Date(it.when).toLocaleString()} · ${it.status}${it.result ? " (" + it.result + ")" : ""}`;
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
    renderRecordings();
    clearTimeout(loadRecordings._t);   // compte rendu en cours de génération : on rafraîchit la liste
    if (items.some(r => r.report_job?.state === "running")) loadRecordings._t = setTimeout(() => {
      if (!$("#tab-rec").classList.contains("hidden")) loadRecordings(); }, 10000);
  } catch (e) { toast("Erreur : " + e.message); }
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
    const url = `/api/recordings/${encodeURIComponent(r.worker)}/${encodeURIComponent(r.name)}`;
    const el = document.createElement("div"); el.className = "ritem";
    const video = r.kind !== "transcript";
    el.innerHTML = `
      <div class="thumb ${video ? "" : "tthumb"}">${video ? '<img loading="lazy" alt="">' : "📝"}
        ${r.recording ? '<span class="live">● EN COURS</span>' : ""}
        ${r.transcript && video ? `<span class="tr" title="Transcription${r.report ? " et compte rendu" : ""} disponible">📝${r.report ? "📋" : ""}</span>` : ""}
        <span class="plat">${esc(platOf(r) === "web" ? (r.meta?.site || siteOf(r.meta?.url || "") || "web") : platOf(r))}</span>
        ${r.duration != null ? `<span class="dur">${fmtSec(r.duration)}</span>` : ""}</div>
      <div class="rbody">
        <b class="rtitle"></b>
        <small>${started.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })} · ${fmtSize(r.size)} · ${esc(r.worker)}</small>
        <div class="ractions">
          <button class="play">${video ? "▶ Lire" : "📝 Lire"}</button>
          ${video ? `<a class="btn" href="${url}?download=1" download="${esc(r.name)}" title="Télécharger la vidéo">⬇</a>` : ""}
          ${r.transcript ? `<a class="btn" href="${url}/transcript?fmt=md&download=1" title="Télécharger la transcription (Markdown)">📝⬇</a>` : ""}
          ${r.report ? `<button class="cr" title="Compte rendu">📋 CR</button><a class="btn" href="${url}/report?fmt=docx&download=1" title="Télécharger le compte rendu (Word)">W⬇</a>`
            : r.report_job?.state === "running" ? `<button class="cr run" title="${esc(r.report_job.step || "")}">⏳ CR</button>` : ""}
          <button class="icon ren" title="Renommer">✏️</button>
          <button class="icon del" title="Supprimer" ${r.recording ? "disabled" : ""}>🗑</button>
        </div>
      </div>`;
    const title = r.meta?.title || r.name;
    $(".rtitle", el).textContent = title; $(".rtitle", el).title = title;
    const img = $("img", el);
    if (img) { img.src = url + "/thumb" + (r.recording ? "?t=" + Date.now() : ""); img.onerror = () => img.remove(); }
    const play = (tab) => openPlayer(url, title, r.name, { video, transcript: r.transcript, report: r.report || !!r.report_job, tab });
    $(".thumb", el).onclick = () => play(); $(".play", el).onclick = () => play();
    if ($(".cr", el)) $(".cr", el).onclick = () => play("cr");
    $(".ren", el).onclick = async () => {
      const t = prompt("Nouveau titre :", title);
      if (!t || t === title) return;
      try { await api(url, { method: "PATCH", json: { title: t } }); loadRecordings(); } catch (e) { toast(e.message); }
    };
    $(".del", el).onclick = async () => {
      if (!confirm(`Supprimer définitivement « ${title} » ?`)) return;
      try { await api(url, { method: "DELETE" }); toast("Supprimé"); loadRecordings(); } catch (e) { toast(e.message); }
    };
    grid.appendChild(el);
  }
}
["#r-search", "#r-platform", "#r-worker"].forEach(s => $(s).addEventListener("input", renderRecordings));
$("#refreshRec").onclick = loadRecordings;

let player = { url: null, doc: null, rep: null, seek: null };
async function openPlayer(url, title, file, o = { video: true }) {
  const v = $("#player video"), pl = $("#player"), box = $("#playerTr");
  player = { url, doc: null, rep: null, seek: null, view: o.transcript ? (o.tab || "tr") : null };
  $("#playerTitle").textContent = title;
  $("#playerDl").classList.toggle("hidden", !o.video);
  $("#playerDl").href = url + "?download=1"; $("#playerDl").setAttribute("download", file);
  $("#playerMd").classList.toggle("hidden", !o.transcript);
  $("#playerMd").href = url + "/transcript?fmt=md&download=1";
  $("#playerDocx").classList.add("hidden"); $("#playerDocx").href = url + "/report?fmt=docx&download=1";
  pl.classList.toggle("tronly", !o.video); pl.classList.toggle("withtr", !!o.transcript && o.video);
  box.innerHTML = ""; $("#playerCr").innerHTML = ""; $("#playerAn").innerHTML = "";
  $("#playerSpk").innerHTML = "";
  $("#playerTabs").classList.toggle("hidden", !o.transcript);
  showPlayerView();
  pl.classList.remove("hidden");
  if (o.video) { v.src = url; applySink(v).then(() => v.play().catch(() => { })); }
  if (!o.transcript) return;
  const seek = o.video ? (t) => { v.currentTime = Math.max(0, t - 1); v.play().catch(() => { }); }
    : (t) => { showPlayerView("tr"); let best = null; box.querySelectorAll("p").forEach(p => { if (+p.dataset.t <= t + 1) best = p; });
               if (best) { best.scrollIntoView({ block: "center" }); best.classList.add("hl"); setTimeout(() => best.classList.remove("hl"), 2500); } };
  player.seek = seek;
  try {
    const doc = await api(url + "/transcript");
    if (player.url !== url) return;
    player.doc = doc;
    const repaint = () => {
      const used = new Set();
      box.querySelectorAll("p").forEach(el => { paintSpk(el, doc.speakers); used.add(el.dataset.spk); });
      renderChips($("#playerSpk"), doc.speakers, used, rename);
      paintPlayerCr(); paintPlayerAn();
    };
    const rename = (id) => askRename(doc.speakers, id, async (sid, nm) => {
      const r = await api(url + "/speakers", { method: "PATCH", json: { id: sid, name: nm } });
      doc.speakers = r.speakers; repaint(); toast("Intervenant renommé (transcription et compte rendu mis à jour)"); });
    (doc.paragraphs || []).forEach(p => box.appendChild(paraEl(p, { seek, speakers: doc.speakers, rename })));
    repaint();
    if (!box.children.length) box.textContent = "Aucune parole transcrite.";
    $('#playerTabs [data-v="an"]').classList.toggle("hidden", !doc.analysis?.state);
    paintPlayerAn();
  } catch (e) { box.textContent = "Transcription indisponible : " + e.message; }
  loadPlayerReport(url);
}
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
  if (!running && !d.live) bar += `<button class="cr-gen" title="Relance la rédaction multi-modèles (ex. après avoir renommé les intervenants : les noms se mettent déjà à jour tout seuls)">${d.report ? "↻ Régénérer" : "📋 Générer le compte rendu"}</button>`;
  if (d.report) bar += `<a class="btn" href="${player.url}/report?fmt=docx&download=1">⬇ Word</a><a class="btn" href="${player.url}/report?fmt=md&download=1">⬇ Markdown</a><button class="cr-copy">📋 Copier</button>`;
  bar += "</div>";
  let body = "";
  if (d.report) body = reportHtml(d.report, playerSpeakers());
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
function closePlayer() {
  const v = $("#player video"); v.pause(); v.removeAttribute("src"); v.load(); $("#player").classList.add("hidden");
  $("#playerTr").innerHTML = ""; $("#playerSpk").innerHTML = ""; $("#playerCr").innerHTML = ""; $("#playerAn").innerHTML = "";
  player = { url: null }; clearTimeout(loadPlayerReport._t);
}
$("#player .close").onclick = closePlayer;
$("#player").onclick = (e) => { if (e.target.id === "player") closePlayer(); };
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePlayer(); });

// ------------------------------------------------------------------ démarrage
let timers = [];
async function boot() {
  const me = await api("/api/me");
  if (!me.logged_in) return showLogin();
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  cfg = await api("/api/config");
  if (!$("#f-dur").value) $("#f-dur").value = cfg.defaultDuration || "";
  prefillName();
  const sel = $("#f-worker");
  cfg.workers.forEach(w => { if (![...sel.options].some(o => o.value === w)) sel.add(new Option(w, w)); card(w); });
  timers.forEach(clearInterval);
  await refresh(); refreshShots(); loadSchedule();
  showTab(location.hash.slice(1) || "live");
  if (location.hash !== "#rec") loadRecordings();   // pour le compteur du menu
  timers = [setInterval(refresh, 3000), setInterval(refreshShots, 2500), setInterval(loadSchedule, 30000),
    setInterval(pollTranscripts, 2500)];
}
boot();
