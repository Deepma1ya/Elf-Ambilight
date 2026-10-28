/* ==========================================================================
   Elf-Ambilight — Aurora SaaS frontend v2
   Backend bridge: pywebview js_api. Events: poll every 450 ms.
   ========================================================================== */
"use strict";

const $ = (id) => document.getElementById(id);
const api = (...a) => window.pywebview.api[a[0]](...a.slice(1));

let S = null;
let dirty = false;
let page = "devices";
let booted = false;
let checked = new Set();
let logs = [];
let chan = "ALL";
let ambiMode = "center";
let color = { r: 255, g: 255, b: 255 };
let modeFilter = "";
let modeCat = "All";

const PAGES = ["devices", "color", "tune", "modes", "timing", "ambi", "settings"];
const TITLES = { devices: "Devices", color: "Colors", tune: "Calibration", modes: "Effects", timing: "Schedule", ambi: "Ambilight", settings: "Settings" };
const DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
const AMBI_PRESETS = ["Movie", "Game", "Balanced", "Chill", "Party"];
const WHEEL = 248;

/* ─────────────────────────── utils ─────────────────────────── */
const hx = (r, g, b) => "#" + [r, g, b].map(v =>
  Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0")).join("").toUpperCase();

function rgb2hsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
  const l = (mx + mn) / 2;
  let h = 0, s = 0;
  if (mx !== mn) {
    const d = mx - mn;
    s = l > .5 ? d / (2 - mx - mn) : d / (mx + mn);
    if (mx === r) h = (g - b) / d + (g < b ? 6 : 0);
    else if (mx === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60;
  }
  return [h, s * 100, l * 100];
}
function hsl2rgb(h, s, l) {
  h = ((h % 360) + 360) % 360;
  s = Math.max(0, Math.min(100, s)) / 100;
  l = Math.max(0, Math.min(100, l)) / 100;
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs((h / 60) % 2 - 1));
  const m = l - c / 2;
  let r = 0, g = 0, b = 0;
  if (h < 60) { r = c; g = x; } else if (h < 120) { r = x; g = c; }
  else if (h < 180) { g = c; b = x; } else if (h < 240) { g = x; b = c; }
  else if (h < 300) { r = x; b = c; } else { r = c; b = x; }
  return [Math.round((r + m) * 255), Math.round((g + m) * 255), Math.round((b + m) * 255)];
}
function hsv2rgb(h, s, v) {
  h = ((h % 360) + 360) % 360;
  const c = v * s;
  const x = c * (1 - Math.abs((h / 60) % 2 - 1));
  const m = v - c;
  let r = 0, g = 0, b = 0;
  if (h < 60) { r = c; g = x; } else if (h < 120) { r = x; g = c; }
  else if (h < 180) { g = c; b = x; } else if (h < 240) { g = x; b = c; }
  else if (h < 300) { r = x; b = c; } else { r = c; b = x; }
  return [Math.round((r + m) * 255), Math.round((g + m) * 255), Math.round((b + m) * 255)];
}
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

function paintRange(el) {
  if (!el) return;
  const min = +el.min || 0, max = +el.max || 100, v = +el.value;
  el.style.setProperty("--p", (((v - min) / (max - min)) * 100) + "%");
}
function paintAll(root = document) { root.querySelectorAll('input[type="range"]').forEach(paintRange); }

/* ─────────────────────────── motion ─────────────────────────── */
function prefersReduced() {
  try {
    return document.body.classList.contains("reduce") ||
      (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  } catch (e) { return false; }
}
function animateNumber(el, to) {
  if (!el) return;
  const from = Number(el.dataset.v ?? to);
  el.dataset.v = String(to);
  if (from === to || prefersReduced()) { el.textContent = String(to); return; }
  const t0 = performance.now();
  const delta = to - from;
  const step = (t) => {
    const p = Math.min(1, (t - t0) / 460);
    const e = 1 - Math.pow(1 - p, 3);
    el.textContent = String(Math.round(from + delta * e));
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
let glideReady = false;
function positionNavGlide() {
  const nav = document.querySelector(".nav");
  const glide = $("navGlide");
  const btn = document.querySelector(".nav-item.active");
  if (!nav || !glide || !btn) return;
  glide.style.height = btn.offsetHeight + "px";
  glide.style.transform = "translateY(" + btn.offsetTop + "px)";
  glide.style.opacity = "1";
  if (!glideReady) {
    glideReady = true;
    requestAnimationFrame(() => glide.classList.remove("no-anim"));
  }
}
let spotRaf = 0;
document.addEventListener("pointermove", (e) => {
  if ((e.pointerType && e.pointerType !== "mouse") || prefersReduced()) return;
  const card = e.target && e.target.closest ? e.target.closest(".card") : null;
  if (!card || spotRaf) return;
  const x = e.clientX, y = e.clientY;
  spotRaf = requestAnimationFrame(() => {
    const r = card.getBoundingClientRect();
    card.style.setProperty("--mx", (x - r.left) + "px");
    card.style.setProperty("--my", (y - r.top) + "px");
    spotRaf = 0;
  });
}, { passive: true });
document.addEventListener("pointerdown", (e) => {
  if (prefersReduced()) return;
  const btn = e.target && e.target.closest ? e.target.closest(".btn") : null;
  if (!btn) return;
  const r = btn.getBoundingClientRect();
  const size = Math.max(r.width, r.height) * 2;
  const span = document.createElement("span");
  span.className = "ripple";
  span.style.width = span.style.height = size + "px";
  span.style.left = (e.clientX - r.left - size / 2) + "px";
  span.style.top = (e.clientY - r.top - size / 2) + "px";
  btn.appendChild(span);
  setTimeout(() => span.remove(), 620);
}, { passive: true });
window.addEventListener("resize", debounce(positionNavGlide, 160));

/* ─────────────────────────── toast / modal ─────────────────────────── */
function toast(msg, kind = "ok") {
  const el = document.createElement("div");
  el.className = "toast" + (kind === "err" ? " err" : "");
  el.innerHTML = `<svg><use href="#${kind === "err" ? "i-x" : "i-check"}"/></svg><span></span>`;
  el.querySelector("span").textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 220); }, 2300);
}

let modalCancel = null;
function openModal({ title, text = "", input = false, initial = "", buttons, onCancel }) {
  $("mTitle").textContent = title;
  $("mText").textContent = text || "";
  $("mText").style.display = text ? "" : "none";
  const inp = $("mInput");
  inp.style.display = input ? "" : "none";
  inp.value = initial || "";
  const box = $("mBtns");
  box.innerHTML = "";
  const close = () => { modalCancel = null; $("modalWrap").classList.remove("on"); };
  buttons.forEach(b => {
    const el = document.createElement("button");
    el.className = "btn sm " + (b.cls || "");
    el.textContent = b.label;
    el.onclick = () => { const v = input ? inp.value.trim() : true; modalCancel = null; close(); b.fn && b.fn(v); };
    box.appendChild(el);
  });
  modalCancel = onCancel || null;
  $("modalWrap").classList.add("on");
  if (input) { inp.focus(); inp.select(); } else box.lastElementChild.focus();
}
function dismissModal() {
  if (!$("modalWrap").classList.contains("on")) return;
  const f = modalCancel; modalCancel = null;
  $("modalWrap").classList.remove("on");
  if (f) f();
}
const askName = (title, initial) => new Promise(res => {
  openModal({
    title, text: "Give it a short, memorable name.", input: true, initial,
    onCancel: () => res(null),
    buttons: [{ label: "Cancel", cls: "ghost", fn: () => res(null) },
              { label: "Save", cls: "primary", fn: v => res(v || null) }]
  });
  $("mInput").onkeydown = e => {
    if (e.key === "Enter") {
      const v = $("mInput").value.trim();
      modalCancel = null; $("modalWrap").classList.remove("on"); res(v || null);
    }
  };
});
const askConfirm = (title, text) => new Promise(res => {
  openModal({
    title, text, onCancel: () => res(false),
    buttons: [{ label: "Cancel", cls: "ghost", fn: () => res(false) },
              { label: "Confirm", cls: "danger", fn: () => res(true) }]
  });
});
$("modalWrap").addEventListener("click", e => { if (e.target.id === "modalWrap") dismissModal(); });

/* ─────────────────────── theme: light / dark / system ─────────────────────── */
const darkQuery = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
let themeMode = "light";
const THEME_MODES = ["light", "dark", "system"];

function resolvedTheme() {
  if (themeMode === "system") return darkQuery && darkQuery.matches ? "dark" : "light";
  return themeMode;
}
function applyTheme(mode, persist = true) {
  themeMode = THEME_MODES.includes(mode) ? mode : "light";
  document.documentElement.setAttribute("data-theme", resolvedTheme());
  document.documentElement.classList.add("theming");
  clearTimeout(applyTheme._t);
  applyTheme._t = setTimeout(() => document.documentElement.classList.remove("theming"), 300);
  try { localStorage.setItem("elf.theme", themeMode); } catch (e) {}
  if (persist) { try { api("set_theme", themeMode); } catch (e) {} }
  document.querySelectorAll("[data-theme-opt]").forEach(b =>
    b.classList.toggle("on", b.dataset.themeOpt === themeMode));
  renderThemeCards();
}
document.querySelectorAll("[data-theme-opt]").forEach(b => b.onclick = () => applyTheme(b.dataset.themeOpt));
if (darkQuery) {
  const onScheme = () => { if (themeMode === "system") applyTheme("system", false); };
  if (darkQuery.addEventListener) darkQuery.addEventListener("change", onScheme);
  else if (darkQuery.addListener) darkQuery.addListener(onScheme);
}

/* ─────────────────────────── switches ─────────────────────────── */
function setSwitch(el, on) { if (el) el.setAttribute("aria-checked", on ? "true" : "false"); }
function bindSwitch(el, fn) {
  if (!el) return;
  el.addEventListener("click", () => {
    const next = el.getAttribute("aria-checked") !== "true";
    setSwitch(el, next);
    fn(next);
  });
}

/* ─────────────────────────── tabs ─────────────────────────── */
function initTabs() {
  document.querySelectorAll("[data-tabs]").forEach(group => {
    const key = group.dataset.tabs;
    group.querySelectorAll("[data-tab]").forEach(btn => {
      btn.onclick = () => {
        group.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("on", b === btn));
        document.querySelectorAll(`[data-panel^="${key}:"]`).forEach(p =>
          p.classList.toggle("on", p.dataset.panel === `${key}:${btn.dataset.tab}`));
      };
    });
  });
}

/* ─────────────────────────── state ─────────────────────────── */
const RENDERERS = {
  devices: () => renderDevices(),
  color: () => renderColor(),
  tune: () => renderTune(),
  modes: () => renderModes(),
  timing: () => renderTiming(),
  ambi: () => renderAmbi(),
  settings: () => renderSettings(),
};
const stale = new Set(PAGES);

function renderPage(name) {
  if (!RENDERERS[name]) return;
  try {
    RENDERERS[name]();
    paintAll($("pg-" + name));
  } catch (e) { console.error(e); }
  stale.delete(name);
}

function applyState(st, { markDirty = false } = {}) {
  if (!st || !st.cfg) return;
  S = st;
  dirty = markDirty ? true : !!st.dirty;
  renderChrome();
  PAGES.forEach(p => { if (p !== page) stale.add(p); });
  if (page) renderPage(page);
  renderSave();
}
async function mut(p) {
  try {
    const st = await p;
    if (st && st.cfg) applyState(st, { markDirty: true });
    return st;
  } catch (e) { toast("Action failed", "err"); console.error(e); }
}
/* quiet send: merge returned state without re-rendering the active page */
async function sendQuiet(p, markDirty = true) {
  try {
    const st = await p;
    if (st && st.cfg) {
      S = st;
      if (markDirty) dirty = true;
      PAGES.forEach(pg => { if (pg !== page) stale.add(pg); });
      renderSave();
    }
    return st;
  } catch (e) { console.error(e); return null; }
}
async function sendColor(r, g, b, msg) {
  const st = await sendQuiet(api("set_color", r, g, b), true);
  if (st && st.cfg) {
    S.cfg.last_r = st.cfg.last_r; S.cfg.last_g = st.cfg.last_g; S.cfg.last_b = st.cfg.last_b;
    S.recent = st.recent || S.recent;
    renderRecent(); renderFavorites();
  }
  if (msg) toast(msg);
}
function renderAll() {
  renderChrome();
  PAGES.forEach(p => { RENDERERS[p](); stale.delete(p); });
  paintAll(); renderSave();
}
function renderChrome() {
  const n = (S.connected || []).length;
  $("sideConn").textContent = n;
  animateNumber($("statConn"), n);
  $("navDevBadge").textContent = n;
  $("chipConnText").textContent = `${n} connected`;
  $("chipConn").className = "status-chip " + (n ? "ok" : "");
  const bt = S.bt_state;
  $("sideBt").textContent = bt === "on" ? "On" : bt === "off" ? "Off" : "—";
  $("sideBt").className = "mini-val " + (bt === "on" ? "ok" : "");
  $("sideAmbi").textContent = S.ambi_running ? "Live" : "Idle";
  $("statAmbi").textContent = S.ambi_running ? "Live" : "Idle";
  $("chipAmbiText").textContent = S.ambi_running ? "Ambi live" : "Ambi idle";
  $("chipAmbi").className = "status-chip " + (S.ambi_running ? "ok" : "");
  const ab = $("navAmbiBadge");
  ab.textContent = S.ambi_running ? "Live" : "Off";
  ab.className = "badge-mini" + (S.ambi_running ? " live" : "");
  $("sideVer").textContent = "v" + S.ver;
  $("statGroup").textContent = S.selected_group || "—";
  $("appVersion").textContent = "v" + S.ver;
}
function renderSave() {
  const b = $("saveBtn");
  b.classList.toggle("dirty", dirty);
  b.querySelector("span").textContent = dirty ? "Save changes" : "Save";
}
async function doSave() { await api("save"); dirty = false; renderSave(); toast("Settings saved"); }
async function doDiscard() { const st = await api("discard"); dirty = false; applyState(st); toast("Reverted to last save"); }

/* ─────────────────────────── navigation ─────────────────────────── */
async function showPage(next) {
  if (!PAGES.includes(next) || next === page) return;
  if (dirty) {
    const r = await new Promise(res => openModal({
      title: "Unsaved changes",
      text: "You have edits that haven't been saved. What should happen?",
      onCancel: () => res("cancel"),
      buttons: [{ label: "Cancel", cls: "ghost", fn: () => res("cancel") },
                { label: "Discard", cls: "danger", fn: () => res("discard") },
                { label: "Save", cls: "primary", fn: () => res("save") }]
    }));
    if (!r || r === "cancel") return;
    if (r === "save") await doSave(); else await doDiscard();
  }
  const prevIdx = PAGES.indexOf(page);
  page = next;
  document.querySelectorAll(".page").forEach(p => p.classList.toggle("active", p.id === "pg-" + next));
  document.querySelectorAll(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.page === next));
  $("crumbTitle").textContent = TITLES[next];
  const el = $("pg-" + next);
  if (el) {
    el.classList.remove("enter-left", "enter-right");
    void el.offsetWidth;
    const idx = PAGES.indexOf(next);
    el.classList.add(prevIdx >= 0 && idx < prevIdx ? "enter-left" : "enter-right");
  }
  $("scroll").scrollTop = 0;
  if (stale.has(next)) renderPage(next);
  positionNavGlide();
}

/* ─────────────────────────── color model ─────────────────────────── */
function setColor(r, g, b, src) {
  color = { r: Math.round(r), g: Math.round(g), b: Math.round(b) };
  const h = hx(color.r, color.g, color.b);
  const [hh, ss, ll] = rgb2hsl(color.r, color.g, color.b);
  $("wheelHex").textContent = h;
  $("hexIn").value = h;
  if (src !== "hsl") { $("hSl").value = hh; $("sSl").value = ss; $("lSl").value = ll; }
  if (src !== "rgb") { $("rSl").value = color.r; $("gSl").value = color.g; $("bSl").value = color.b; }
  if (src !== "wheel") { $("wheelL").value = ll; $("wheelLVal").textContent = Math.round(ll) + "%"; }
  $("hVal").textContent = Math.round(hh);
  $("sVal").textContent = Math.round(ss) + "%";
  $("lVal").textContent = Math.round(ll) + "%";
  $("rVal").textContent = color.r;
  $("gVal").textContent = color.g;
  $("bVal").textContent = color.b;
  syncWheelPointer(hh, ss);
  ["hSl", "sSl", "lSl", "rSl", "gSl", "bSl", "wheelL"].forEach(id => {
    const el = $(id); if (el) paintRange(el);
  });
  if (page === "tune") debounceTunePreview();
  renderHarmony(hh);
}
const liveSend = debounce(() => {
  if ($("liveSw").getAttribute("aria-checked") === "true") api("set_color", color.r, color.g, color.b);
}, 140);

/* ─────────────────────────── color wheel ─────────────────────────── */
let wheelReady = false;
function drawWheel() {
  const canvas = $("wheel");
  if (!canvas || wheelReady) return;
  const ctx = canvas.getContext("2d");
  const size = canvas.width;
  const img = ctx.createImageData(size, size);
  const cx = size / 2, cy = size / 2, R = size / 2 - 1;
  const data = img.data;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const dx = x - cx + .5, dy = y - cy + .5;
      const d = Math.hypot(dx, dy);
      const i = (y * size + x) * 4;
      if (d > R) { data[i + 3] = 0; continue; }
      const hue = (Math.atan2(dy, dx) * 180 / Math.PI + 360) % 360;
      const sat = Math.min(1, d / R);
      const [r, g, b] = hsv2rgb(hue, sat, 1);
      data[i] = r; data[i + 1] = g; data[i + 2] = b;
      data[i + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  wheelReady = true;
}
function syncWheelPointer(h, s) {
  const dot = $("wheelDot");
  if (!dot) return;
  const R = WHEEL / 2 - 22;
  const rad = (h * Math.PI) / 180;
  const r = (clamp(s, 0, 100) / 100) * R;
  dot.style.left = (WHEEL / 2 + Math.cos(rad) * r) + "px";
  dot.style.top = (WHEEL / 2 - Math.sin(rad) * r) + "px";
  dot.style.background = hx(color.r, color.g, color.b);
}
function wheelPick(ev) {
  const canvas = $("wheel");
  const rect = canvas.getBoundingClientRect();
  const x = ev.clientX - rect.left, y = ev.clientY - rect.top;
  const cx = rect.width / 2, cy = rect.height / 2;
  const dx = x - cx, dy = y - cy;
  const d = Math.hypot(dx, dy);
  const R = rect.width / 2 - 1;
  const h = (Math.atan2(-dy, dx) * 180 / Math.PI + 360) % 360;
  const s = clamp((d / R) * 100, 0, 100);
  const l = +$("wheelL").value;
  setColor(...hsl2rgb(h, s, l), "wheel");
  liveSend();
}
function bindWheel() {
  const canvas = $("wheel");
  let dragging = false;
  let pending = null;
  let raf = 0;
  const flush = () => {
    raf = 0;
    if (pending) { wheelPick(pending); pending = null; }
  };
  canvas.addEventListener("pointerdown", e => {
    dragging = true;
    try { canvas.setPointerCapture(e.pointerId); } catch (err) {}
    wheelPick(e);
  });
  canvas.addEventListener("pointermove", e => {
    if (!dragging) return;
    pending = e;
    if (!raf) raf = requestAnimationFrame(flush);
  });
  const end = () => {
    if (!dragging) return;
    dragging = false;
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    if (pending) { wheelPick(pending); pending = null; }
    sendColor(color.r, color.g, color.b);
  };
  canvas.addEventListener("pointerup", end);
  canvas.addEventListener("pointercancel", end);
  canvas.addEventListener("lostpointercapture", end);
}
function renderHarmony(h) {
  const row = $("harmonyRow");
  if (!row) return;
  const l = +$("wheelL").value;
  const s = rgb2hsl(color.r, color.g, color.b)[1];
  const offsets = [0, 30, -30, 180, 120, 240];
  if (!row.dataset.ready) {
    row.dataset.ready = "1";
    offsets.forEach((off, i) => {
      const btn = document.createElement("button");
      btn.title = i === 0 ? "Base" : i === 3 ? "Complementary" : "Analogous / triadic";
      btn.onclick = () => {
        const rgb = (btn.dataset.rgb || "").split(",").map(Number);
        if (rgb.length === 3) { setColor(rgb[0], rgb[1], rgb[2]); sendColor(rgb[0], rgb[1], rgb[2]); }
      };
      row.appendChild(btn);
    });
  }
  const buttons = row.querySelectorAll("button");
  offsets.forEach((off, i) => {
    const hue = (h + off + 360) % 360;
    const [r, g, b] = hsl2rgb(hue, Math.max(35, s), clamp(l, 25, 85));
    const btn = buttons[i];
    if (!btn) return;
    btn.style.background = hx(r, g, b);
    btn.dataset.rgb = `${r},${g},${b}`;
  });
}

/* ─────────────────────────── scenes ─────────────────────────── */
const SCENES = [
  ["Cozy", 255, 170, 90, 55],
  ["Focus", 200, 225, 255, 100],
  ["Cinema", 110, 70, 255, 35],
  ["Party", 255, 0, 120, 100],
];
function renderScenes() {
  const row = $("sceneRow");
  if (!row) return;
  row.innerHTML = "";
  SCENES.forEach(([label, r, g, b, bri]) => {
    const btn = document.createElement("button");
    btn.className = "btn sm";
    btn.style.cssText = "justify-content:flex-start;gap:8px";
    btn.innerHTML = `<span style="width:12px;height:12px;border-radius:4px;background:${hx(r, g, b)};border:1px solid rgba(0,0,0,.15)"></span><span>${label}</span>`;
    btn.onclick = async () => {
      setColor(r, g, b);
      await sendColor(r, g, b);
      await sendQuiet(api("set_brightness", bri, "ALL"));
      $("briSl").value = bri; $("briVal").textContent = bri; paintRange($("briSl"));
      toast(`${label} scene applied`);
    };
    row.appendChild(btn);
  });
}

/* ─────────────────────────── devices ─────────────────────────── */
function initials(name) {
  const n = (name || "?").replace(/[^A-Za-z0-9 ]/g, " ").trim();
  const p = n.split(/\s+/);
  return ((p[0] || "?")[0] + (p[1] ? p[1][0] : (p[0] || "")[1] || "")).toUpperCase();
}
function renderDevices() {
  const box = $("foundList");
  const devs = S.found || [];
  $("scanStatus").textContent = devs.length ? `${devs.length} found` : "No devices yet";
  box.innerHTML = "";
  if (!devs.length) {
    box.innerHTML = `<div class="empty"><b>Nothing discovered yet</b>Press Scan to look for nearby strips.</div>`;
  } else {
    devs.forEach((d, di) => {
      const row = document.createElement("div");
      row.className = "list-row";
      row.style.setProperty("--i", Math.min(di, 10));
      const cb = document.createElement("label");
      cb.className = "check";
      const inp = document.createElement("input");
      inp.type = "checkbox"; inp.checked = checked.has(d.addr);
      inp.onchange = () => { inp.checked ? checked.add(d.addr) : checked.delete(d.addr); };
      cb.appendChild(inp);
      const av = document.createElement("div");
      av.className = "avatar"; av.textContent = initials(d.name);
      const main = document.createElement("div");
      main.className = "list-main";
      main.innerHTML = `<div class="list-t"></div><div class="list-s"></div>`;
      main.querySelector(".list-t").textContent = d.name || "Unknown strip";
      main.querySelector(".list-s").textContent = d.addr;
      const badge = document.createElement("span");
      badge.className = "badge " + (d.connected ? "ok" : "");
      badge.textContent = d.connected ? "Connected" : `${d.rssi} dBm`;
      const add = document.createElement("button");
      add.className = "btn sm"; add.textContent = "Add";
      add.onclick = () => mut(api("group_add_many", [d.addr]));
      const conn = document.createElement("button");
      conn.className = "btn sm " + (d.connected ? "" : "primary");
      conn.textContent = d.connected ? "Reconnect" : "Connect";
      conn.onclick = () => api("connect_one", d.addr);
      row.append(cb, av, main, badge, add, conn);
      box.appendChild(row);
    });
  }

  const sel = $("groupSel");
  sel.innerHTML = "";
  Object.keys(S.groups || {}).forEach(g => {
    const o = document.createElement("option");
    o.value = g; o.textContent = g;
    if (g === S.selected_group) o.selected = true;
    sel.appendChild(o);
  });

  const ml = $("memberList");
  ml.innerHTML = "";
  const members = S.group_members || [];
  if (!members.length) ml.innerHTML = `<div class="empty"><b>Empty group</b>Add a discovered device to start controlling it.</div>`;
  members.forEach((m, mi) => {
    const row = document.createElement("div");
    row.className = "list-row";
    row.style.setProperty("--i", Math.min(mi, 10));
    const av = document.createElement("div");
    av.className = "avatar"; av.textContent = initials(m.name);
    const main = document.createElement("div");
    main.className = "list-main";
    main.innerHTML = `<div class="list-t"></div><div class="list-s"></div>`;
    main.querySelector(".list-t").textContent = m.name || m.addr;
    main.querySelector(".list-s").textContent = m.addr;
    const badge = document.createElement("span");
    badge.className = "badge " + (m.connected ? "ok" : "");
    badge.textContent = m.connected ? "Live" : "Saved";
    const x = document.createElement("button");
    x.className = "btn sm ghost"; x.innerHTML = `<svg><use href="#i-x"/></svg>`;
    x.title = "Remove from group";
    x.onclick = () => mut(api("group_remove", m.addr));
    row.append(av, main, badge, x);
    ml.appendChild(row);
  });

  $("rememberedTxt").textContent = S.remembered_text || "—";
  const dynOn = !!S.dyn_on, lamps = S.dyn_lamps || 0;
  const db = $("dynBadge");
  db.textContent = !dynOn ? "Off" : lamps ? `${lamps} lamp${lamps > 1 ? "s" : ""}` : "No lamps";
  db.className = "badge " + (dynOn && lamps ? "ok" : "");
  $("logBox").textContent = logs.join("\n");
  $("logBox").scrollTop = 1e9;
}
function pushLog(msg) {
  const ts = new Date().toTimeString().slice(0, 8);
  logs.push(`[${ts}] ${msg}`);
  if (logs.length > 300) logs = logs.slice(-300);
  if (S) { $("logBox").textContent = logs.join("\n"); $("logBox").scrollTop = 1e9; }
}

/* ─────────────────────────── colors page ─────────────────────────── */
function renderColor() {
  const c = S.cfg;
  setSwitch($("liveSw"), !!c.live_send);
  setColor(c.last_r, c.last_g, c.last_b);
  $("briSl").value = c.last_brightness;
  $("briVal").textContent = c.last_brightness;
  renderScenes();

  const pr = $("presetRow"); pr.innerHTML = "";
  (S.color_presets || []).forEach(([r, g, b]) => {
    const b2 = document.createElement("button");
    b2.className = "sw"; b2.style.background = hx(r, g, b); b2.title = hx(r, g, b);
    b2.onclick = () => { setColor(r, g, b); sendColor(r, g, b); };
    pr.appendChild(b2);
  });
  const rc = $("recentRow"); rc.innerHTML = "";
  const recents = S.recent || [];
  if (!recents.length) rc.innerHTML = `<span class="list-s" style="font-family:var(--sans)">No recent colours yet.</span>`;
  recents.forEach(([r, g, b]) => {
    const b2 = document.createElement("button");
    b2.className = "sw"; b2.style.background = hx(r, g, b); b2.title = hx(r, g, b);
    b2.onclick = () => { setColor(r, g, b); sendColor(r, g, b); };
    rc.appendChild(b2);
  });
  renderFavorites();
}
function renderFavorites() {
  const row = $("favRow");
  if (!row) return;
  row.innerHTML = "";
  const favs = S.cfg.favorites || [];
  if (!favs.length) {
    row.innerHTML = `<span class="list-s" style="font-family:var(--sans)">No saved colours yet — press “Save colour”.</span>`;
    return;
  }
  favs.forEach(([r, g, b]) => {
    const b2 = document.createElement("button");
    b2.className = "sw"; b2.style.background = hx(r, g, b);
    b2.title = `${hx(r, g, b)} — click to apply · shift-click to remove`;
    b2.onclick = (e) => {
      if (e.shiftKey) {
        mut(api("favorite_remove", r, g, b)).then(() => { renderFavorites(); toast("Removed from favourites"); });
      } else {
        setColor(r, g, b); sendColor(r, g, b);
      }
    };
    row.appendChild(b2);
  });
}

/* ─────────────────────────── tune ─────────────────────────── */
const PRIMS = [["prim_r", "Red", "#D92D20"], ["prim_g", "Green", "#0E9F6E"],
               ["prim_b", "Blue", "#4F46E5"], ["white_pt", "White", "#0C1020"]];
function renderTune() {
  const t = S.cfg;
  setSwitch($("calEn"), !!t.cal_enabled);
  $("grSl").value = t.cal_gain_r * 100;
  $("ggSl").value = t.cal_gain_g * 100;
  $("gbSl").value = t.cal_gain_b * 100;
  $("gamSl").value = t.cal_gamma;
  $("ggrSl").value = t.cal_gamma_r;
  $("gggSl").value = t.cal_gamma_g;
  $("ggbSl").value = t.cal_gamma_b;
  $("tmpSl").value = t.cal_temp;
  $("satSl").value = t.cal_sat * 100;
  $("limSl").value = t.cal_limit * 100;
  $("ordSel").value = t.cal_order;
  tuneLabels();

  const box = $("primRows");
  box.innerHTML = "";
  PRIMS.forEach(([attr, label, colHex]) => {
    const vals = [...t[attr]];
    const block = document.createElement("div");
    block.style.cssText = "border:1px solid var(--line);border-radius:14px;padding:11px;background:var(--surface-2)";
    block.innerHTML = `
      <div class="field" style="margin:0">
        <span class="field-label" style="flex:0 0 auto;color:${colHex}">${label}</span>
        <span class="sw" style="width:26px;height:26px;background:${hx(...vals)}"></span>
        <span class="field-val" style="flex:0 0 auto">${hx(...vals)}</span>
        <div style="flex:1"></div>
        <button class="btn sm" data-show="${attr}">Show</button>
      </div>`;
    ["R", "G", "B"].forEach((c, i) => {
      const row = document.createElement("div");
      row.className = "field";
      row.innerHTML = `<span class="field-label" style="flex:0 0 20px">${c}</span>`;
      const sl = document.createElement("input");
      sl.type = "range"; sl.min = 0; sl.max = 255; sl.value = vals[i];
      const v = document.createElement("span");
      v.className = "field-val"; v.textContent = vals[i];
      sl.oninput = () => { vals[i] = +sl.value; v.textContent = sl.value; paintRange(sl); };
      sl.onchange = () => sendQuiet(api("tune_set", "prim", { attr, vals }));
      row.append(sl, v); block.appendChild(row); paintRange(sl);
    });
    block.querySelector("[data-show]").onclick = () => {
      const pure = { prim_r: [255, 0, 0], prim_g: [0, 255, 0], prim_b: [0, 0, 255], white_pt: [255, 255, 255] }[attr];
      setColor(...pure); sendColor(...pure);
    };
    box.appendChild(block);
  });

  const tt = $("tuneTests"); tt.innerHTML = "";
  [[255,255,255],[128,128,128],[255,0,0],[0,255,0],[0,80,255],[255,220,0],[0,220,220],[170,60,255]].forEach(([r,g,b]) => {
    const b2 = document.createElement("button");
    b2.className = "sw"; b2.style.background = hx(r,g,b);
    b2.onclick = () => { setColor(r, g, b); sendColor(r, g, b); };
    tt.appendChild(b2);
  });
  renderTuneProfiles();
  tunePreview();
}
function tuneLabels() {
  $("grVal").textContent = Math.round($("grSl").value) + "%";
  $("ggVal").textContent = Math.round($("ggSl").value) + "%";
  $("gbVal").textContent = Math.round($("gbSl").value) + "%";
  $("gamVal").textContent = (+$("gamSl").value).toFixed(2);
  $("ggrVal").textContent = (+$("ggrSl").value).toFixed(2);
  $("gggVal").textContent = (+$("gggSl").value).toFixed(2);
  $("ggbVal").textContent = (+$("ggbSl").value).toFixed(2);
  $("tmpVal").textContent = $("tmpSl").value;
  $("satVal").textContent = Math.round($("satSl").value) + "%";
  $("limVal").textContent = Math.round($("limSl").value) + "%";
}
async function tunePreview() {
  if (!S || page !== "tune") return;
  try {
    const o = await api("cal_preview", color.r, color.g, color.b);
    if (!o) return;
    $("tuneReq").style.background = hx(...o.req);
    $("tuneOut").style.background = hx(...o.out);
    $("tuneTxt").textContent = `(${o.req.join(", ")}) → (${o.out.join(", ")})`;
  } catch (e) {}
}
const debounceTunePreview = debounce(() => { tunePreview(); }, 120);
const tuneChanged = debounce(() => {
  tuneLabels();
  debounceTunePreview();
  sendQuiet(api("tune_set", "gains", {
    r: +$("grSl").value / 100, g: +$("ggSl").value / 100, b: +$("gbSl").value / 100,
    gamma: +$("gamSl").value, temp: +$("tmpSl").value, order: $("ordSel").value,
    gamma_r: +$("ggrSl").value, gamma_g: +$("gggSl").value, gamma_b: +$("ggbSl").value,
    sat: +$("satSl").value / 100, limit: +$("limSl").value / 100,
    enabled: $("calEn").getAttribute("aria-checked") === "true"
  }));
}, 200);
function renderTuneProfiles() {
  const sel = $("tuneProfSel");
  const names = Object.keys(S.tune_profiles || {}).sort();
  sel.innerHTML = names.length ? "" : `<option value="">No profiles yet</option>`;
  names.forEach(n => { const o = document.createElement("option"); o.value = n; o.textContent = n; sel.appendChild(o); });
}

/* ─────────────────────────── modes ─────────────────────────── */
const CATS = ["All", "Static", "Gradient", "Cross", "Strobe"];
function modeCategory(name, idx) {
  const n = name.toLowerCase();
  if (n.includes("static")) return "Static";
  if (n.includes("cross") || n.includes("jump")) return "Cross";
  if (n.includes("strobe") || n.includes("flash")) return "Strobe";
  if (n.includes("gradual") || n.includes("fade")) return "Gradient";
  return "Other";
}
function renderModes() {
  const chips = $("catChips");
  if (!chips.dataset.ready) {
    chips.dataset.ready = "1";
    CATS.forEach(c => {
      const b = document.createElement("button");
      b.className = "chip" + (c === "All" ? " on" : "");
      b.textContent = c;
      b.onclick = () => {
        modeCat = c;
        chips.querySelectorAll(".chip").forEach(x => x.classList.toggle("on", x === b));
        renderModes();
      };
      chips.appendChild(b);
    });
  }
  const grid = $("modeGrid");
  const q = modeFilter.trim().toLowerCase();
  grid.innerHTML = "";
  let shown = 0;
  (S.mode_names || []).forEach((m, i) => {
    const cat = modeCategory(m, i);
    if (modeCat !== "All" && cat !== modeCat) return;
    if (q && !m.toLowerCase().includes(q) && !String(i).includes(q)) return;
    shown++;
    const b = document.createElement("button");
    b.className = "mode-btn" + (i === S.cfg.last_mode ? " on" : "");
    b.style.setProperty("--i", Math.min(shown, 10));
    b.innerHTML = `<span class="n">${String(i).padStart(2, "0")}</span><span></span>`;
    b.querySelector("span:last-child").textContent = m;
    b.dataset.i = i;
    b.onclick = async () => {
      await api("send_mode", i);
      if (S && S.cfg) { S.cfg.last_mode = i; dirty = true; renderSave(); }
      markModeActive();
    };
    grid.appendChild(b);
  });
  if (!shown) grid.innerHTML = `<div class="empty" style="grid-column:1/-1"><b>No matches</b>Try another search or category.</div>`;
  $("modeCount").textContent = `${shown} effect${shown === 1 ? "" : "s"}`;
  $("curMode").textContent = S.mode_names[S.cfg.last_mode] || "—";
  $("spdSl").value = S.cfg.last_speed;
  $("spdVal").textContent = S.cfg.last_speed;
  $("mbSl").value = S.cfg.last_brightness;
  $("mbVal").textContent = S.cfg.last_brightness;
}

function markModeActive() {
  const cur = S && S.cfg ? S.cfg.last_mode : -1;
  document.querySelectorAll(".mode-btn").forEach(b => {
    b.classList.toggle("on", +b.dataset.i === cur);
  });
  if (S) $("curMode").textContent = (S.mode_names || [])[cur] || "—";
}

/* ─────────────────────────── timing ─────────────────────────── */
function timerCard(title, t, slot) {
  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML = `
    <div class="card-head">
      <div><h3>${title}</h3><div class="hint">On-board strip timer.</div></div>
      <div class="push"><span class="badge ${t.on ? "ok" : ""}">${t.on ? "Enabled" : "Disabled"}</span></div>
    </div>
    <div class="card-body">
      <div class="field">
        <span class="field-label">Time</span>
        <input type="time" class="time-input" value="${String(t.h).padStart(2, "0")}:${String(t.m).padStart(2, "0")}">
        <div style="flex:1"></div>
        <button class="btn sm" data-on>${t.on ? "Turn off" : "Turn on"}</button>
      </div>
      <div class="hr" style="height:1px;background:var(--line);margin:12px 0"></div>
      <div class="field" style="align-items:flex-start">
        <span class="field-label">Days</span><div class="days"></div>
      </div>
      <div class="field" style="justify-content:flex-end;margin-top:12px">
        <button class="btn sm primary" data-send>Send timer</button>
      </div>
    </div>`;
  const daysBox = card.querySelector(".days");
  const dayState = [...t.days];
  dayState.forEach((d, i) => {
    const b = document.createElement("button");
    b.className = "day" + (d ? " on" : "");
    b.textContent = DAYS[i];
    b.onclick = () => { dayState[i] = !dayState[i]; b.classList.toggle("on", dayState[i]); };
    daysBox.appendChild(b);
  });
  let on = t.on;
  const onBtn = card.querySelector("[data-on]");
  onBtn.onclick = () => {
    on = !on;
    onBtn.textContent = on ? "Turn off" : "Turn on";
    const badge = card.querySelector(".badge");
    badge.textContent = on ? "Enabled" : "Disabled";
    badge.className = "badge " + (on ? "ok" : "");
  };
  card.querySelector("[data-send]").onclick = () => {
    const [h, m] = card.querySelector("input[type=time]").value.split(":").map(Number);
    mut(api("send_timer", slot, h, m, on, dayState)).then(() => toast(`Timer ${slot + 1} sent`));
  };
  return card;
}
function renderTiming() {
  const box = $("timerCards");
  box.innerHTML = "";
  box.appendChild(timerCard("Timer 1", S.t1, 0));
  box.appendChild(timerCard("Timer 2", S.t2, 1));
  const list = $("schedList");
  list.innerHTML = "";
  const sch = S.cfg.schedules || [];
  if (!sch.length) list.innerHTML = `<div class="empty"><b>No schedules</b>Add one to automate power, colour, or effects.</div>`;
  sch.forEach((s, idx) => {
    const row = document.createElement("div");
    row.className = "list-row"; row.style.flexWrap = "wrap";
    row.style.setProperty("--i", Math.min(idx, 8));
    const name = document.createElement("input");
    name.type = "text"; name.value = s.name; name.style.width = "132px";
    name.onchange = () => mut(api("sched_set", idx, "name", name.value));
    const time = document.createElement("input");
    time.type = "time"; time.className = "time-input";
    time.value = `${String(s.hour).padStart(2, "0")}:${String(s.min).padStart(2, "0")}`;
    time.onchange = () => { const [h, m] = time.value.split(":").map(Number); mut(api("sched_time", idx, h, m)); };
    const act = document.createElement("select");
    [["power_on", "Power on"], ["power_off", "Power off"], ["color", "Colour"], ["mode", "Effect"]].forEach(([v, l]) => {
      const o = document.createElement("option"); o.value = v; o.textContent = l; act.appendChild(o);
    });
    act.value = s.action;
    act.onchange = () => mut(api("sched_set", idx, "action", act.value));
    const days = document.createElement("div");
    days.className = "days";
    (s.days || []).forEach((d, i) => {
      const b = document.createElement("button");
      b.className = "day" + (d ? " on" : "");
      b.textContent = DAYS[i];
      b.onclick = () => { const n = !b.classList.contains("on"); b.classList.toggle("on", n); mut(api("sched_day", idx, i, n)); };
      days.appendChild(b);
    });
    const en = document.createElement("button");
    en.className = "switch"; en.setAttribute("role", "switch");
    en.setAttribute("aria-checked", s.enabled ? "true" : "false");
    en.setAttribute("aria-label", "Enable schedule");
    bindSwitch(en, on => mut(api("sched_set", idx, "enabled", on)));
    const del = document.createElement("button");
    del.className = "btn sm ghost"; del.innerHTML = `<svg><use href="#i-x"/></svg>`;
    del.title = "Delete schedule";
    del.onclick = () => mut(api("sched_del", idx)).then(renderTiming);
    const top = document.createElement("div");
    top.style.cssText = "display:flex;align-items:center;gap:7px;flex:1;flex-wrap:wrap";
    top.append(name, time, act, en);
    row.append(top, days, del);
    list.appendChild(row);
  });
}

/* ─────────────────────────── ambilight ─────────────────────────── */
function renderAmbi() {
  const a = S.cfg;
  setSwitch($("ambiAuto"), !!a.ambi_on_startup);
  setSwitch($("ambiXf"), !!a.ambi_crossfade);
  setSwitch($("ambiDx"), !!a.ambi_use_dxcam);
  setSwitch($("ambiWfOn"), (+a.ambi_white_floor || 0) > 0);

  ambiMode = a.ambi_mode === "full" ? "full" : "center";
  $("ambiModeSeg").querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.m === ambiMode));

  const mon = $("ambiMon"); mon.innerHTML = "";
  (S.monitors || [[1, "Monitor 1"]]).forEach(([i, label]) => {
    const o = document.createElement("option"); o.value = i; o.textContent = label; mon.appendChild(o);
  });
  mon.value = String(a.ambi_monitor);
  $("ambiSample").value = a.ambi_sample.charAt(0).toUpperCase() + a.ambi_sample.slice(1);

  $("aFps").value = a.ambi_fps; $("aSm").value = a.ambi_smooth;
  $("aBri").value = a.ambi_brightness; $("aMd").value = a.ambi_min_delta;
  $("aWf").value = a.ambi_white_floor; $("aIv").value = a.ambi_interval;
  ambiLabels();

  const btn = $("ambiBtn");
  btn.innerHTML = S.ambi_running ? `<svg><use href="#i-stop"/></svg>Stop` : `<svg><use href="#i-play"/></svg>Start`;
  btn.className = "btn " + (S.ambi_running ? "danger" : "primary");
  const btn2 = $("ambiBtn2");
  if (btn2) {
    btn2.innerHTML = btn.innerHTML;
    btn2.className = "btn " + (S.ambi_running ? "danger" : "primary");
    btn2.style.width = "100%";
  }
  $("liveStatus").textContent = S.ambi_running ? "Live" : "Idle";
  $("liveStatus").className = "badge " + (S.ambi_running ? "ok" : "");
  $("liveSample").textContent = a.ambi_sample.charAt(0).toUpperCase() + a.ambi_sample.slice(1);

  const row = $("ambiPresetRow"); row.innerHTML = "";
  AMBI_PRESETS.forEach(n => {
    const b = document.createElement("button");
    b.className = "btn sm"; b.textContent = n;
    b.onclick = () => mut(api("ambi_preset", n)).then(renderAmbi);
    row.appendChild(b);
  });
  const sel = $("ambiCustomSel");
  const names = Object.keys(S.ambi_custom || {}).sort();
  sel.innerHTML = names.length ? "" : `<option value="">No saved presets</option>`;
  names.forEach(n => { const o = document.createElement("option"); o.value = n; o.textContent = n; sel.appendChild(o); });

  const tiles = $("candTiles"); tiles.innerHTML = "";
  ["average", "dominant", "vibrant", "brightest"].forEach(k => {
    const t = document.createElement("div");
    t.className = "tile";
    t.innerHTML = `<div class="chip-c" id="tile-${k}"></div><div class="lbl">${k.slice(0, 3)}</div>`;
    tiles.appendChild(t);
  });
}
function ambiLabels() {
  $("aFpsV").textContent = Math.round($("aFps").value);
  $("aSmV").textContent = (+$("aSm").value).toFixed(1) + "s";
  $("aBriV").textContent = $("aBri").value;
  $("aMdV").textContent = $("aMd").value;
  $("aWfV").textContent = $("aWf").value + "%";
  $("aIvV").textContent = (+$("aIv").value).toFixed(2) + "s";
}
const ambiChanged = debounce(() => {
  ambiLabels();
  sendQuiet(api("ambi_set", {
    fps: +$("aFps").value, smooth: +$("aSm").value, brightness: +$("aBri").value,
    min_delta: +$("aMd").value, white_floor: +$("aWf").value, interval: +$("aIv").value,
    mode: ambiMode, sample: $("ambiSample").value.toLowerCase(), monitor: +$("ambiMon").value,
    crossfade: $("ambiXf").getAttribute("aria-checked") === "true",
    dxcam: $("ambiDx").getAttribute("aria-checked") === "true"
  }));
}, 200);

/* ─────────────────────────── settings ─────────────────────────── */
function renderSettings() {
  const c = S.cfg;
  setSwitch($("stStartup"), !!S.startup_on);
  setSwitch($("stTask"), !!S.task_on);
  $("stTask").disabled = !S.startup_on;
  $("stTask").style.opacity = S.startup_on ? "" : ".45";
  setSwitch($("stMinimized"), !!c.start_minimized);
  setSwitch($("stBtAuto"), !!c.bt_on_startup);
  setSwitch($("stDyn"), !!c.dynlight_enabled);
  setSwitch($("stAutoConn"), !!c.auto_connect);
  setSwitch($("stAutoRe"), !!c.auto_reconnect);
  setSwitch($("stSyncTime"), !!c.sync_time_on_startup);
  setSwitch($("stQuit"), !!c.quit_on_close);
  setSwitch($("stMotion"), !!c.reduce_motion);

  const bt = S.bt_state;
  const badge = $("btState");
  badge.textContent = bt === "on" ? "On" : bt === "off" ? "Off" : "Unknown";
  badge.className = "badge " + (bt === "on" ? "ok" : bt === "off" ? "warn" : "");
  $("dynStatus").textContent = c.dynlight_enabled ? `${S.dyn_lamps || 0} lamp(s) available` : "Off";
  $("cfgPath").textContent = S.cfg_path || "—";
  $("appVersion").textContent = "v" + S.ver;
  document.body.classList.toggle("reduce", !!c.reduce_motion);
  renderThemeCards();
}
function renderThemeCards() {
  $("themeLight") && $("themeLight").classList.toggle("on", themeMode === "light");
  $("themeDark") && $("themeDark").classList.toggle("on", themeMode === "dark");
  $("themeSystem") && $("themeSystem").classList.toggle("on", themeMode === "system");
}

/* ─────────────────────────── command palette ─────────────────────────── */
let cmdItems = [], cmdSel = 0;
function cmdActions() {
  const acts = [];
  PAGES.forEach((p, i) => acts.push({
    group: "Navigate", label: TITLES[p], icon: "i-grid", hint: `Alt ${i + 1}`,
    run: () => showPage(p)
  }));
  acts.push(
    { group: "Actions", label: "Save changes", icon: "i-save", hint: "Ctrl S", run: doSave },
    { group: "Actions", label: "Discard changes", icon: "i-x", run: doDiscard },
    { group: "Actions", label: "Scan for devices", icon: "i-refresh", run: () => api("scan_start", 5) },
    { group: "Actions", label: "Quick connect", icon: "i-link", run: () => api("scan_start", 3) },
    { group: "Actions", label: "Power on", icon: "i-power", run: () => api("power", true) },
    { group: "Actions", label: "Power off", icon: "i-power", run: () => api("power", false) },
    { group: "Actions", label: (S && S.ambi_running) ? "Stop Ambilight" : "Start Ambilight", icon: "i-screen",
      run: () => api(S && S.ambi_running ? "ambi_stop" : "ambi_start") },
    { group: "Appearance", label: "Light mode", icon: "i-sun", run: () => applyTheme("light") },
    { group: "Appearance", label: "Dark mode", icon: "i-moon", run: () => applyTheme("dark") },
    { group: "Appearance", label: "Match Windows theme", icon: "i-appearance", run: () => applyTheme("system") },
    { group: "Actions", label: "Pick colour from screen", icon: "i-eyedrop", run: pickFromScreen }
  );
  AMBI_PRESETS.forEach(n => acts.push({
    group: "Ambilight presets", label: n, icon: "i-screen",
    run: () => mut(api("ambi_preset", n)).then(renderAmbi)
  }));
  const named = [["Warm white", 255, 180, 100], ["Cool white", 200, 225, 255], ["Amber", 255, 160, 40],
                 ["Cyan", 0, 220, 230], ["Violet", 150, 80, 255], ["Rose", 255, 60, 120]];
  named.forEach(([l, r, g, b]) => acts.push({
    group: "Colours", label: l, icon: "i-palette",
    run: () => { setColor(r, g, b); sendColor(r, g, b); }
  }));
  return acts;
}
function openCmdk() {
  cmdItems = cmdActions();
  cmdSel = 0;
  $("cmdkInput").value = "";
  renderCmdk("");
  $("cmdk").classList.add("on");
  setTimeout(() => $("cmdkInput").focus(), 30);
}
function closeCmdk() { $("cmdk").classList.remove("on"); }
function renderCmdk(q) {
  const list = $("cmdkList");
  const query = q.trim().toLowerCase();
  const filtered = cmdItems.filter(a => !query || a.label.toLowerCase().includes(query) || a.group.toLowerCase().includes(query));
  cmdSel = clamp(cmdSel, 0, Math.max(0, filtered.length - 1));
  list.innerHTML = "";
  if (!filtered.length) {
    list.innerHTML = `<div class="cmdk-empty">No matching commands.</div>`;
    return;
  }
  let lastGroup = "";
  filtered.forEach((a, i) => {
    if (a.group !== lastGroup) {
      lastGroup = a.group;
      const g = document.createElement("div");
      g.className = "cmdk-group"; g.textContent = a.group;
      list.appendChild(g);
    }
    const b = document.createElement("button");
    b.className = "cmdk-item" + (i === cmdSel ? " sel" : "");
    b.innerHTML = `<svg><use href="#${a.icon}"/></svg><span></span>${a.hint ? `<span class="kbd">${a.hint}</span>` : ""}`;
    b.querySelector("span").textContent = a.label;
    b.onmouseenter = () => { cmdSel = i; markCmdk(list); };
    b.onclick = () => { closeCmdk(); a.run(); };
    b.dataset.idx = i;
    b.style.setProperty("--i", Math.min(i, 10));
    list.appendChild(b);
  });
  list._filtered = filtered;
}
function markCmdk(list) {
  list.querySelectorAll(".cmdk-item").forEach((el, i) => el.classList.toggle("sel", +el.dataset.idx === cmdSel));
}
function runCmdk() {
  const list = $("cmdkList");
  const arr = list._filtered || [];
  const a = arr[cmdSel];
  if (a) { closeCmdk(); a.run(); }
}
$("cmdkInput").addEventListener("input", e => { cmdSel = 0; renderCmdk(e.target.value); });
$("cmdkInput").addEventListener("keydown", e => {
  if (e.key === "ArrowDown") { e.preventDefault(); cmdSel++; renderCmdk($("cmdkInput").value); }
  else if (e.key === "ArrowUp") { e.preventDefault(); cmdSel--; renderCmdk($("cmdkInput").value); }
  else if (e.key === "Enter") { e.preventDefault(); runCmdk(); }
  else if (e.key === "Escape") { closeCmdk(); }
});
$("cmdk").addEventListener("click", e => { if (e.target.id === "cmdk") closeCmdk(); });
$("cmdTrigger").onclick = openCmdk;

/* ─────────────────────────── eyedropper ─────────────────────────── */
async function pickFromScreen() {
  if (!window.EyeDropper) { toast("Eyedropper is not available here", "err"); return; }
  try {
    const res = await new EyeDropper().open();
    const t = res.sRGBHex.replace("#", "");
    setColor(parseInt(t.slice(0, 2), 16), parseInt(t.slice(2, 4), 16), parseInt(t.slice(4, 6), 16));
    sendColor(color.r, color.g, color.b, "Colour picked");
  } catch (e) { /* user cancelled */ }
}

/* ─────────────────────────── events ─────────────────────────── */
function handleEvent(evt) {
  try {
    switch (evt.t) {
      case "log": pushLog(evt.msg); break;
      case "found": S.found = evt.devices; renderDevices(); break;
      case "conn":
        S.connected = evt.connected || [];
        if (evt.names) S.cfg.names = evt.names;
        if (evt.last) S.cfg.last_addresses = evt.last;
        renderChrome(); renderDevices(); break;
      case "bt": S.bt_state = evt.state; renderChrome(); renderSettings(); break;
      case "dyn": S.dyn_lamps = evt.lamps; renderDevices(); renderSettings(); break;
      case "ambi_tick": {
        const [r, g, b] = evt.color;
        $("ambiPrev").style.background = hx(r, g, b);
        $("ambiStat").textContent = `${evt.frames} frames · ${evt.sends} sends · ${evt.cap} ms${evt.eco ? " · eco" : ""} · rgb(${r}, ${g}, ${b})`;
        $("liveFrames").textContent = evt.frames;
        $("liveSends").textContent = evt.sends;
        $("liveCap").textContent = evt.cap + " ms";
        $("liveEco").textContent = evt.eco ? "On" : "Off";
        $("liveStatus").textContent = "Live";
        $("liveStatus").className = "badge ok";
        ["average", "dominant", "vibrant", "brightest"].forEach(k => {
          const t = $("tile-" + k);
          if (t && evt.stats && evt.stats[k]) t.style.background = hx(...evt.stats[k]);
        });
        break;
      }
      case "ambi_state":
        S.ambi_running = evt.running; renderChrome(); renderAmbi(); break;
      case "state": applyState(evt.state); break;
    }
  } catch (e) { console.error(e); }
}
let pollTimer = null;
async function pollOnce() {
  try {
    if (!document.hidden && window.pywebview && window.pywebview.api) {
      const list = await api("poll");
      (list || []).forEach(handleEvent);
    }
  } catch (e) { /* bridge busy or closing */ }
  pollTimer = setTimeout(pollOnce, document.hidden ? 1500 : 450);
}
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
    pollOnce();
  }
});

/* ─────────────────────────── bindings ─────────────────────────── */
function bind() {
  document.querySelectorAll(".nav-item").forEach(b => b.onclick = () => showPage(b.dataset.page));
  $("pwrOn").onclick = () => { api("power", true); toast("Strip powered on"); };
  $("pwrOff").onclick = () => { api("power", false); toast("Strip powered off"); };
  $("saveBtn").onclick = doSave;
  $("cfgSave").onclick = doSave;

  // devices
  $("scanBtn").onclick = () => { $("scanStatus").textContent = "Scanning…"; api("scan_start", 5); };
  $("quickBtn").onclick = () => { $("scanStatus").textContent = "Scanning…"; api("scan_start", 3); };
  $("connAllBtn").onclick = () => api("connect_all");
  $("disconnBtn").onclick = () => api("disconnect_all");
  $("addAllBtn").onclick = () => mut(api("group_add_many", (S.found || []).map(d => d.addr))).then(() => toast("Added all found devices"));
  $("addCheckedBtn").onclick = () => mut(api("group_add_many", [...checked])).then(() => { checked.clear(); toast("Added selected devices"); });
  $("groupSel").onchange = e => mut(api("group_select", e.target.value));
  $("grpNew").onclick = async () => {
    const n = await askName("New group", `Group ${Object.keys(S.groups).length + 1}`);
    if (n) mut(api("group_new", n));
  };
  $("grpRename").onclick = async () => {
    const n = await askName("Rename group", S.selected_group);
    if (n && n !== S.selected_group) mut(api("group_rename", n));
  };
  $("grpDel").onclick = async () => {
    if (await askConfirm("Delete group", `Delete “${S.selected_group}”? Devices stay connected.`)) mut(api("group_delete"));
  };
  $("forgetBtn").onclick = async () => {
    if (await askConfirm("Forget devices", "Remove all remembered devices? You can re-add them later.")) mut(api("forget_remembered"));
  };
  $("dynRefresh").onclick = () => { api("dyn_refresh"); toast("Refreshing PC lamps"); };
  $("dynRefresh2").onclick = () => { api("dyn_refresh"); toast("Refreshing PC lamps"); };
  $("dynAdd").onclick = () => mut(api("group_add_many", ["dynlight://pc"])).then(() => toast("PC lighting added to group"));
  document.querySelectorAll("[data-group-action]").forEach(b => b.onclick = () => {
    const a = b.dataset.groupAction;
    if (a === "color") sendColor(S.cfg.last_r, S.cfg.last_g, S.cfg.last_b, "Colour sent to group");
    else if (a === "on") { api("power", true); toast("Group powered on"); }
    else if (a === "off") { api("power", false); toast("Group powered off"); }
    else { api("bt_refresh"); api("connect_all"); toast("Refreshing connections"); }
  });

  // color
  bindSwitch($("liveSw"), on => mut(api("set_pref", "live_send", on)));
  $("wheelL").oninput = () => {
    const h = rgb2hsl(color.r, color.g, color.b)[0];
    const s = rgb2hsl(color.r, color.g, color.b)[1];
    const [r, g, b] = hsl2rgb(h, s, +$("wheelL").value);
    setColor(r, g, b, "wheel");
    $("wheelLVal").textContent = $("wheelL").value + "%";
    liveSend();
  };
  $("wheelL").onchange = () => sendColor(color.r, color.g, color.b);
  const hsl = () => { setColor(...hsl2rgb(+$("hSl").value, +$("sSl").value, +$("lSl").value), "hsl"); liveSend(); };
  ["hSl", "sSl", "lSl"].forEach(id => { $(id).oninput = hsl; $(id).onchange = () => sendColor(color.r, color.g, color.b); });
  const rgb = () => { setColor(+$("rSl").value, +$("gSl").value, +$("bSl").value, "rgb"); liveSend(); };
  ["rSl", "gSl", "bSl"].forEach(id => { $(id).oninput = rgb; $(id).onchange = () => sendColor(color.r, color.g, color.b); });
  $("hexApply").onclick = () => {
    const t = $("hexIn").value.trim().replace(/^#/, "");
    if (!/^[0-9a-fA-F]{6}$/.test(t)) { toast("Enter a 6-digit hex colour", "err"); return; }
    setColor(parseInt(t.slice(0, 2), 16), parseInt(t.slice(2, 4), 16), parseInt(t.slice(4, 6), 16));
    sendColor(color.r, color.g, color.b);
  };
  $("hexIn").onkeydown = e => { if (e.key === "Enter") $("hexApply").click(); };
  $("copyHex").onclick = async () => {
    const h = hx(color.r, color.g, color.b);
    try { await navigator.clipboard.writeText(h); toast("Hex copied"); }
    catch (e) { $("hexIn").select(); document.execCommand && document.execCommand("copy"); toast("Hex copied"); }
  };
  $("eyeBtn").onclick = pickFromScreen;
  $("matchScreen").onclick = async () => {
    const res = await api("capture_screen_color");
    if (!res || !res.rgb) { toast("Could not read the screen", "err"); return; }
    const [r, g, b] = res.rgb;
    setColor(r, g, b);
    await sendColor(r, g, b, res.live ? "Matched live Ambilight colour" : "Matched screen colour");
  };
  $("saveFav").onclick = () => mut(api("favorite_add", color.r, color.g, color.b)).then(() => toast("Colour saved"));
  $("randomColor").onclick = () => {
    const hue = Math.random() * 360;
    const sat = 65 + Math.random() * 35;
    const light = 42 + Math.random() * 22;
    const [r, g, b] = hsl2rgb(hue, sat, light);
    setColor(r, g, b);
    sendColor(r, g, b, "Random colour applied");
  };
  $("applyColorBtn").onclick = () => sendColor(color.r, color.g, color.b, "Colour sent");
  $("briSl").oninput = () => { $("briVal").textContent = $("briSl").value; paintRange($("briSl")); };
  $("briSl").onchange = () => sendQuiet(api("set_brightness", +$("briSl").value, chan));
  $("briSend").onclick = () => sendQuiet(api("set_brightness", +$("briSl").value, chan)).then(() => toast("Brightness sent"));
  $("chanSeg").querySelectorAll("button").forEach(b => b.onclick = () => {
    chan = b.dataset.ch;
    $("chanSeg").querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
  });
  $("wSl").oninput = () => { $("wVal").textContent = $("wSl").value; paintRange($("wSl")); };
  $("wSend").onclick = () => sendQuiet(api("set_w", +$("wSl").value)).then(() => toast("White sent"));
  $("warmSl").oninput = () => { $("warmVal").textContent = $("warmSl").value; paintRange($("warmSl")); };
  $("coldSl").oninput = () => { $("coldVal").textContent = $("coldSl").value; paintRange($("coldSl")); };
  $("cctSend").onclick = () => sendQuiet(api("set_cct", +$("warmSl").value, +$("coldSl").value)).then(() => toast("White balance sent"));
  $("rgbwSend").onclick = () => api("set_rgbw", $("chRgb").checked, $("chW").checked, $("chCct").checked, chan).then(() => toast("Channels updated"));
  $("pinsSend").onclick = () => api("set_pins", +$("pin1").value, +$("pin2").value, +$("pin3").value).then(() => toast("Pin order sent"));

  // tune
  $("tuneReset").onclick = async () => {
    if (await askConfirm("Reset calibration", "Restore all calibration values to defaults?")) mut(api("tune_reset")).then(() => toast("Calibration reset"));
  };
  $("wbShow").onclick = () => api("wb_show");
  document.querySelectorAll("[data-wb]").forEach(b => b.onclick = () => mut(api("wb_nudge", b.dataset.wb)));
  ["grSl", "ggSl", "gbSl", "gamSl", "tmpSl", "satSl", "limSl", "ggrSl", "gggSl", "ggbSl"].forEach(id => $(id).oninput = () => { tuneLabels(); paintRange($(id)); tuneChanged(); });
  $("ordSel").onchange = tuneChanged;
  bindSwitch($("calEn"), () => tuneChanged());
  document.querySelectorAll("[data-wbtemp]").forEach(b => b.onclick = () => {
    $("tmpSl").value = b.dataset.wbtemp;
    paintRange($("tmpSl"));
    tuneLabels();
    tuneChanged();
    toast(`White balance: ${b.textContent}`);
  });
  $("tpApply").onclick = () => {
    const n = $("tuneProfSel").value;
    if (!n) return toast("No profile selected", "err");
    mut(api("tune_prof_apply", n)).then(() => toast(`Applied “${n}”`));
  };
  $("tpSave").onclick = async () => {
    const n = await askName("Save calibration profile", $("tuneProfSel").value || "My calibration");
    if (n) mut(api("tune_prof_save", n)).then(() => { toast("Profile saved"); renderTuneProfiles(); });
  };
  $("tpRename").onclick = async () => {
    const old = $("tuneProfSel").value;
    if (!old) return toast("No profile selected", "err");
    const n = await askName("Rename profile", old);
    if (n && n !== old) mut(api("tune_prof_rename", old, n)).then(() => toast("Profile renamed"));
  };
  $("tpDel").onclick = async () => {
    const n = $("tuneProfSel").value;
    if (!n) return toast("No profile selected", "err");
    if (await askConfirm("Delete profile", `Delete “${n}”?`)) mut(api("tune_prof_delete", n)).then(() => toast("Profile deleted"));
  };

  // modes
  $("modeSearch").oninput = debounce(e => { modeFilter = e.target.value; renderModes(); }, 120);
  $("randomMode").onclick = async () => {
    const names = S.mode_names || [];
    if (!names.length) return;
    const i = Math.floor(Math.random() * names.length);
    await api("send_mode", i);
    if (S && S.cfg) { S.cfg.last_mode = i; dirty = true; renderSave(); }
    markModeActive();
    toast(`Effect: ${names[i]}`);
  };
  $("spdSl").oninput = () => { $("spdVal").textContent = $("spdSl").value; paintRange($("spdSl")); };
  $("spdSl").onchange = () => sendQuiet(api("send_speed", +$("spdSl").value));
  $("spdSend").onclick = () => sendQuiet(api("send_speed", +$("spdSl").value)).then(() => toast("Speed sent"));
  $("mbSl").oninput = () => { $("mbVal").textContent = $("mbSl").value; paintRange($("mbSl")); };
  $("mbSl").onchange = () => sendQuiet(api("set_brightness", +$("mbSl").value, "ALL"));
  $("mbSend").onclick = () => sendQuiet(api("set_brightness", +$("mbSl").value, "ALL")).then(() => toast("Brightness sent"));

  // timing
  $("syncTimeBtn").onclick = () => api("sync_time").then(() => toast("Strip clock synced"));
  $("schedAdd").onclick = () => mut(api("sched_add")).then(renderTiming);

  // ambi
  $("ambiBtn").onclick = () => {
    if (S.ambi_running) api("ambi_stop").then(() => toast("Ambilight stopped"));
    else api("ambi_start").then(() => toast("Ambilight starting"));
  };
  $("ambiBtn2").onclick = () => $("ambiBtn").click();
  document.querySelectorAll("[data-quick-ambi]").forEach(b => b.onclick = async () => {
    const name = b.dataset.quickAmbi;
    const st = await api("ambi_preset", name);
    if (st && st.cfg) applyState(st, { markDirty: true });
    if (!(S && S.ambi_running)) api("ambi_start");
    toast(`Ambilight: ${name}`);
  });
  bindSwitch($("ambiAuto"), on => mut(api("ambi_auto", on)));
  bindSwitch($("ambiXf"), () => ambiChanged());
  bindSwitch($("ambiDx"), () => ambiChanged());
  bindSwitch($("ambiWfOn"), on => {
    $("aWf").value = on ? Math.max(20, +$("aWf").value || 40) : 0;
    ambiLabels(); paintRange($("aWf")); ambiChanged();
  });
  $("ambiModeSeg").querySelectorAll("button").forEach(b => b.onclick = () => {
    ambiMode = b.dataset.m;
    $("ambiModeSeg").querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
    ambiChanged();
  });
  ["aFps", "aSm", "aBri", "aMd", "aWf", "aIv"].forEach(id => $(id).oninput = () => { ambiLabels(); paintRange($(id)); ambiChanged(); });
  ["ambiMon", "ambiSample"].forEach(id => $(id).onchange = ambiChanged);
  $("acApply").onclick = () => {
    const n = $("ambiCustomSel").value;
    if (!n) return toast("No preset selected", "err");
    mut(api("ambi_custom_apply", n)).then(() => toast(`Applied “${n}”`));
  };
  $("acSave").onclick = async () => {
    const n = await askName("Save ambilight preset", "My preset");
    if (n) mut(api("ambi_custom_save", n)).then(() => { toast("Preset saved"); renderAmbi(); });
  };
  $("acRename").onclick = async () => {
    const old = $("ambiCustomSel").value;
    if (!old) return toast("No preset selected", "err");
    const n = await askName("Rename preset", old);
    if (n && n !== old) mut(api("ambi_custom_rename", old, n)).then(() => toast("Preset renamed"));
  };
  $("acDel").onclick = async () => {
    const n = $("ambiCustomSel").value;
    if (!n) return toast("No preset selected", "err");
    if (await askConfirm("Delete preset", `Delete “${n}”?`)) mut(api("ambi_custom_delete", n)).then(() => toast("Preset deleted"));
  };

  // settings
  bindSwitch($("stStartup"), on => api("set_startup", on).then(st => st && st.cfg && applyState(st)));
  bindSwitch($("stTask"), on => api("set_task", on).then(st => st && st.cfg && applyState(st)));
  bindSwitch($("stMinimized"), on => api("set_pref", "start_minimized", on).then(st => st && st.cfg && applyState(st)));
  bindSwitch($("stBtAuto"), on => api("set_pref", "bt_on_startup", on).then(st => st && st.cfg && applyState(st)));
  bindSwitch($("stDyn"), on => api("set_dyn", on).then(st => st && st.cfg && applyState(st)));
  bindSwitch($("stAutoConn"), on => api("set_pref", "auto_connect", on).then(st => st && st.cfg && applyState(st)));
  bindSwitch($("stAutoRe"), on => api("set_pref", "auto_reconnect", on).then(st => st && st.cfg && applyState(st)));
  bindSwitch($("stSyncTime"), on => api("set_pref", "sync_time_on_startup", on).then(st => st && st.cfg && applyState(st)));
  bindSwitch($("stQuit"), on => api("set_pref", "quit_on_close", on).then(st => st && st.cfg && applyState(st)));
  bindSwitch($("stMotion"), on => api("set_pref", "reduce_motion", on).then(st => st && st.cfg && applyState(st)));
  $("themeLight").onclick = () => applyTheme("light");
  $("themeDark").onclick = () => applyTheme("dark");
  $("themeSystem").onclick = () => applyTheme("system");
  $("btRefresh").onclick = () => api("bt_refresh");
  $("btOn").onclick = () => { api("bt_enable"); toast("Trying to enable Bluetooth"); };
  $("btSettings").onclick = () => api("bt_settings");
  $("cfgOpen").onclick = () => api("open_config_folder");

  bindWheel();
}

/* keyboard */
document.addEventListener("keydown", e => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openCmdk(); return; }
  if (e.key === "Escape") {
    if ($("cmdk").classList.contains("on")) { closeCmdk(); return; }
    if ($("modalWrap").classList.contains("on")) { dismissModal(); return; }
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); doSave(); }
  if (e.altKey && e.key >= "1" && e.key <= "7") { e.preventDefault(); showPage(PAGES[+e.key - 1]); }
});

/* ─────────────────────────── boot ─────────────────────────── */
async function boot() {
  if (booted) return;
  if (!(window.pywebview && window.pywebview.api)) return;
  booted = true;
  initTabs();
  drawWheel();
  applyTheme(localStorage.getItem("elf.theme") || "light", false);
  bind();
  try {
    const st = await api("get_state");
    applyState(st);
    const savedTheme = (st.cfg && st.cfg.ui_theme) || themeMode;
    if (savedTheme !== themeMode) applyTheme(savedTheme, false);
    renderAll();          // one-time full render; interactions stay page-scoped
    page = "";
    showPage("devices");
    positionNavGlide();
    const bb = $("bootBar");
    if (bb) { bb.classList.add("done"); setTimeout(() => bb.remove(), 500); }
    pushLog("UI ready.");
    pollOnce();
    api("ui_booted", {
      pages: document.querySelectorAll(".page").length,
      modes: document.querySelectorAll(".mode-btn").length,
      presets: document.querySelectorAll("#presetRow .sw").length,
      boot_ms: Math.round(performance.now())
    });
  } catch (e) {
    console.error(e);
    toast("Failed to load state", "err");
  }
}
window.addEventListener("pywebviewready", boot);
window.addEventListener("DOMContentLoaded", () => setTimeout(boot, 250));
(function wait() {
  if (booted) return;
  if (window.pywebview && window.pywebview.api) boot();
  else setTimeout(wait, 80);
})();
