/* Elf-Ambilight WebUI — SPA. Backend: pywebview js_api. Push: Elf.push(evt). */
"use strict";
const $ = (id) => document.getElementById(id);
const api = (...a) => window.pywebview.api[a[0]](...a.slice(1));
let S = null;            // full state from backend
let dirty = false;
let curPage = "devices";
let checked = new Set(); // found-list bulk selection
let logLines = [];

const PAGES = [["devices","Devices"],["color","Colors"],["tune","Tune"],["modes","Modes"],["timing","Timer"],["ambi","Ambi"],["settings","Setup"]];

/* ── color utils ── */
const hx = (r,g,b) => "#" + [r,g,b].map(v => Math.max(0,Math.min(255,Math.round(v))).toString(16).padStart(2,"0")).join("");
function rgb2hsl(r,g,b){r/=255;g/=255;b/=255;const mx=Math.max(r,g,b),mn=Math.min(r,g,b);let h=0,s=0;const l=(mx+mn)/2;if(mx!==mn){const d=mx-mn;s=l>0.5?d/(2-mx-mn):d/(mx+mn);if(mx===r)h=(g-b)/d+(g<b?6:0);else if(mx===g)h=(b-r)/d+2;else h=(r-g)/d+4;h*=60;}return[h,s*100,l*100];}
function hsl2rgb(h,s,l){h=((h%360)+360)%360;s=Math.max(0,Math.min(100,s))/100;l=Math.max(0,Math.min(100,l))/100;const c=(1-Math.abs(2*l-1))*s,x=c*(1-Math.abs((h/60)%2-1)),m=l-c/2;let r=0,g=0,b=0;if(h<60){r=c;g=x;}else if(h<120){r=x;g=c;}else if(h<180){g=c;b=x;}else if(h<240){g=x;b=c;}else if(h<300){r=x;b=c;}else{r=c;b=x;}return[Math.round((r+m)*255),Math.round((g+m)*255),Math.round((b+m)*255)];}
function debounce(fn, ms){ let t=null; return (...a)=>{ clearTimeout(t); t=setTimeout(()=>fn(...a),ms); }; }

/* ── modal ── */
function modal({title, text="", input=false, buttons}){
  $("mTitle").textContent = title; $("mText").textContent = text;
  const inp = $("mInput"); inp.style.display = input ? "" : "none"; inp.value = "";
  const box = $("mBtns"); box.innerHTML = "";
  const close = (val) => { $("modalWrap").classList.remove("on"); if(val!==undefined&&cb)cb(val); };
  let cb = null;
  buttons.forEach(b => {
    const el = document.createElement("button");
    el.className = "btn small " + (b.cls||""); el.textContent = b.label;
    el.onclick = () => { const v = input ? inp.value.trim() : true; close(); b.fn && b.fn(v); };
    box.appendChild(el);
  });
  $("modalWrap").classList.add("on");
  (input ? inp : box.querySelector("button")).focus();
  return { onClose: (f)=>cb=f };
}
$("modalWrap").addEventListener("click", e => { if (e.target.id === "modalWrap") e.currentTarget.classList.remove("on"); });
const askName = (title, initial) => new Promise(res => {
  modal({title, text:"", input:true, buttons:[{label:"OK",cls:"primary",fn:v=>res(v||null)},{label:"Cancel",cls:"ghost",fn:()=>res(null)}]});
  $("mInput").value = initial || "";
  $("mInput").onkeydown = e => { if(e.key==="Enter"){$("modalWrap").classList.remove("on");res($("mInput").value.trim()||null);} };
});
const askConfirm = (title, text) => new Promise(res => {
  modal({title, text, buttons:[{label:"Yes",cls:"primary",fn:()=>res(true)},{label:"Cancel",cls:"ghost",fn:()=>res(false)}]});
});

/* ── nav ── */
function buildNav(){
  const nav = $("nav"); nav.innerHTML = "";
  PAGES.forEach(([key,label],i)=>{
    const col = document.createElement("div"); col.className = "navcol";
    const tick = document.createElement("div"); tick.className = "navtick"; tick.id = "tick-"+key;
    const b = document.createElement("button"); b.className = "navbtn"; b.id = "nav-"+key;
    b.innerHTML = `<span class="n">${i+1}</span>${label}`;
    b.onclick = () => showPage(key);
    col.appendChild(tick); col.appendChild(b); nav.appendChild(col);
  });
  document.addEventListener("keydown", e => {
    if (e.altKey && e.key >= "1" && e.key <= "7") { e.preventDefault(); showPage(PAGES[+e.key-1][0]); }
  });
}
async function showPage(key){
  if (key === curPage) return;
  if (dirty) {
    const names = "unsaved changes";
    const r = await new Promise(res => modal({title:"Unsaved changes", text:"Save keeps the new values. Discard reverts to the last save. ("+names+")",
      buttons:[{label:"Save",cls:"primary",fn:()=>res("save")},{label:"Discard",cls:"danger",fn:()=>res("discard")},{label:"Cancel",cls:"ghost",fn:()=>res("cancel")}]}));
    if (r === "cancel") return;
    if (r === "save") await doSave();
    else await doDiscard();
  }
  curPage = key;
  document.querySelectorAll(".page").forEach(p => p.classList.remove("on"));
  $("pg-"+key).classList.add("on");
  PAGES.forEach(([k]) => { $("nav-"+k).classList.toggle("on", k===key); $("tick-"+k).classList.toggle("on", k===key); });
}
function setDirty(){ dirty = true; renderSave(); }
async function doSave(){ await api("save"); dirty = false; S.cfg && (S.dirty=false); renderSave(); }
async function doDiscard(){ S = await api("discard"); dirty = false; renderAll(); }
function renderSave(){ const b=$("saveBtn"); b.textContent = dirty ? "Save ●" : "Save"; b.classList.toggle("clean", !dirty); }

/* ── header / status ── */
function setOrb(r,g,b){
  const h = hx(r,g,b);
  $("orb").style.background = h; $("orbHex").textContent = h;
  $("glow").style.background = h;
}
function renderConn(){
  const n = (S.connected||[]).length;
  const c = $("connCount"); c.textContent = n; c.className = n>0 ? "on" : "off";
}
function setBt(st){
  const d = $("btDot");
  if (st === "on") { d.textContent="BT"; d.className="ok"; $("btState").textContent="Bluetooth: ON"; $("ftBt").textContent="BT: on"; $("ftBt").className="ok"; }
  else if (st === "off") { d.textContent="BT!"; d.className="bad"; $("btState").textContent="Bluetooth: OFF"; $("ftBt").textContent="BT: off"; $("ftBt").className="bad"; }
  else { d.textContent="BT?"; d.className="unk"; $("btState").textContent="Bluetooth: …"; $("ftBt").textContent="BT: …"; $("ftBt").className=""; }
}
function logMsg(msg){
  const d = new Date();
  const ts = d.toTimeString().slice(0,8);
  logLines.push(`[${ts}] ${msg}`);
  if (logLines.length > 300) logLines = logLines.slice(-300);
  $("logBox").textContent = logLines.join("\n");
  $("logBox").scrollTop = 1e9;
  $("ftMsg").textContent = msg;
}

/* ══ DEVICES ══ */
function renderFound(){
  const box = $("foundList"); box.innerHTML = "";
  const devs = S.found || [];
  $("devStatus").textContent = devs.length ? `${devs.length} found` : "idle";
  if (!devs.length) { box.innerHTML = `<div style="color:var(--muted);font-size:12px">No devices — press Scan.</div>`; return; }
  devs.forEach(d => {
    const row = document.createElement("div"); row.className = "devrow";
    const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = checked.has(d.addr);
    cb.onchange = () => { cb.checked ? checked.add(d.addr) : checked.delete(d.addr); };
    const nm = document.createElement("span"); nm.className = "nm"; nm.textContent = d.name || "?";
    const ad = document.createElement("span"); ad.className = "ad"; ad.textContent = d.addr;
    const badge = document.createElement("span"); badge.className = "badge " + (d.connected ? "on" : "off");
    badge.textContent = d.connected ? "connected" : "found";
    const rssi = document.createElement("span"); rssi.className = "rssi"; rssi.textContent = d.rssi + " dBm";
    const add = document.createElement("button"); add.className = "btn small"; add.textContent = "+ Add";
    add.onclick = () => api("group_add_many", [d.addr]).then(afterMut);
    const conn = document.createElement("button"); conn.className = "btn small ghost"; conn.textContent = d.connected ? "Re" : "Connect";
    conn.onclick = () => api("connect_one", d.addr);
    row.append(cb, nm, ad, badge, rssi, add, conn);
    box.appendChild(row);
  });
}
function renderGroups(){
  const sel = $("groupSel"); sel.innerHTML = "";
  Object.keys(S.groups).forEach(g => { const o = document.createElement("option"); o.value = g; o.textContent = g; sel.appendChild(o); });
  sel.value = S.selected_group;
  const box = $("memberList"); box.innerHTML = "";
  (S.group_members || []).forEach(m => {
    const row = document.createElement("div"); row.className = "mrow";
    const nm = document.createElement("span"); nm.className = "nm"; nm.textContent = m.name || m.addr;
    const ad = document.createElement("span"); ad.className = "ad"; ad.textContent = m.addr;
    const badge = document.createElement("span"); badge.className = "badge " + (m.connected ? "on" : "off");
    badge.textContent = m.connected ? "connected" : "saved";
    const x = document.createElement("button"); x.className = "btn small danger"; x.textContent = "×";
    x.onclick = () => api("group_remove", m.addr).then(afterMut);
    row.append(nm, ad, badge, x); box.appendChild(row);
  });
  if (!(S.group_members||[]).length) box.innerHTML = `<div style="color:var(--muted);font-size:12px">Group is empty.</div>`;
  $("rememberedTxt").textContent = S.remembered_text || "";
  const dyn = S.dyn_lamps > 0;
  $("dynBadge").textContent = S.dyn_on ? (dyn ? `${S.dyn_lamps} lamps` : "no lamps") : "off";
  $("dynBadge").className = "badge " + (S.dyn_on && dyn ? "on" : "off");
}
async function afterMut(st){ if (st) { S = st; dirty = true; renderSave(); renderFound(); renderGroups(); renderConn(); } }

/* ══ COLOR ══ */
let COL = {r:255,g:255,b:255};
function setCOL(r,g,b, from){
  COL = {r:Math.round(r),g:Math.round(g),b:Math.round(b)};
  const h = hx(COL.r,COL.g,COL.b);
  $("cPrev").style.background = h; $("cHex").textContent = h;
  $("cRgb").textContent = `${COL.r}, ${COL.g}, ${COL.b}`;
  setOrb(COL.r,COL.g,COL.b);
  if (from !== "hsl") { const [hh,ss,ll] = rgb2hsl(COL.r,COL.g,COL.b);
    $("hSl").value=hh; $("sSl").value=ss; $("lSl").value=ll; }
  if (from !== "rgb") { $("rSl").value=COL.r; $("gSl").value=COL.g; $("bSl").value=COL.b; }
  updColorLabels();
}
function updColorLabels(){
  const [hh,ss,ll] = rgb2hsl(COL.r,COL.g,COL.b);
  $("hVal").textContent=Math.round(hh); $("sVal").textContent=Math.round(ss)+"%"; $("lVal").textContent=Math.round(ll)+"%";
  $("rVal").textContent=COL.r; $("gVal").textContent=COL.g; $("bVal").textContent=COL.b;
  $("hexIn").value = hx(COL.r,COL.g,COL.b);
  tunePreview();
}
const liveColor = debounce(() => { if ($("liveChk").checked) api("set_color", COL.r, COL.g, COL.b); }, 150);
function renderColor(){
  setCOL(S.cfg.last_r, S.cfg.last_g, S.cfg.last_b);
  $("liveChk").checked = !!S.cfg.live_send;
  $("briSl").value = S.cfg.last_brightness; $("briVal").textContent = S.cfg.last_brightness;
  $("wSl").value = S.w||0; $("wVal").textContent = S.w||0;
  const pr = $("presetRow"); pr.innerHTML = "";
  (S.color_presets||[]).forEach(([r,g,b]) => {
    const b2 = document.createElement("button"); b2.className="sw"; b2.style.background=hx(r,g,b); b2.title=hx(r,g,b);
    b2.onclick = () => { setCOL(r,g,b); api("set_color",r,g,b); setDirty(); };
    pr.appendChild(b2);
  });
  renderRecent();
}
function renderRecent(){
  const rc = $("recentRow"); rc.innerHTML = "";
  (S.recent||[]).forEach(([r,g,b]) => {
    const b2 = document.createElement("button"); b2.className="sw"; b2.style.background=hx(r,g,b);
    b2.onclick = () => { setCOL(r,g,b); api("set_color",r,g,b); setDirty(); };
    rc.appendChild(b2);
  });
}

/* ══ TUNE ══ */
const PRIM_ROWS = [["prim_r","Red"],["prim_g","Green"],["prim_b","Blue"],["white_pt","White"]];
function renderTune(){
  const t = S.cfg;
  $("calEn").checked = !!t.cal_enabled;
  $("grSl").value=t.cal_gain_r*100; $("ggSl").value=t.cal_gain_g*100; $("gbSl").value=t.cal_gain_b*100;
  $("gamSl").value=t.cal_gamma; $("tmpSl").value=t.cal_temp;
  $("satSl").value=t.cal_sat*100; $("limSl").value=t.cal_limit*100;
  $("ordSel").value=t.cal_order;
  updTuneLabels();
  const box = $("primRows"); box.innerHTML = "";
  PRIM_ROWS.forEach(([attr,label]) => {
    const [r,g,b] = t[attr];
    const wrap = document.createElement("div"); wrap.style.marginBottom = "10px";
    wrap.innerHTML = `<div class="row"><b style="min-width:52px">${label}</b>
      <div class="sw" style="background:${hx(r,g,b)};width:56px"></div>
      <span class="val">${hx(r,g,b)}</span>
      <button class="btn small ghost" data-show="${attr}">Show</button></div>`;
    ["r","g","b"].forEach((c,i) => {
      const vals=[r,g,b];
      const row = document.createElement("div"); row.className="row";
      row.innerHTML = `<label class="lbl" style="min-width:20px">${c.toUpperCase()}</label>`;
      const sl = document.createElement("input"); sl.type="range"; sl.min=0; sl.max=255; sl.value=vals[i];
      const vv = document.createElement("span"); vv.className="val"; vv.textContent=vals[i];
      sl.oninput = () => { vals[i]=+sl.value; vv.textContent=sl.value; };
      sl.onchange = () => { api("tune_set","prim",{attr,vals}).then(s=>{S=s;setDirty();renderTune();}); };
      row.append(sl,vv); wrap.appendChild(row);
    });
    box.appendChild(wrap);
  });
  box.querySelectorAll("[data-show]").forEach(b => b.onclick = () => {
    const pure = {prim_r:[255,0,0],prim_g:[0,255,0],prim_b:[0,0,255],white_pt:[255,255,255]}[b.dataset.show];
    api("set_color", ...pure);
  });
  const tt = $("tuneTests"); tt.innerHTML = "";
  [[255,255,255],[128,128,128],[255,0,0],[0,255,0],[0,80,255],[255,220,0],[0,220,220],[170,60,255]].forEach(([r,g,b]) => {
    const b2 = document.createElement("button"); b2.className="sw"; b2.style.background=hx(r,g,b);
    b2.onclick = () => { setCOL(r,g,b); api("set_color",r,g,b); };
    tt.appendChild(b2);
  });
  renderTuneProfs();
  tunePreview();
}
function updTuneLabels(){
  $("grVal").textContent=Math.round($("grSl").value)+"%"; $("ggVal").textContent=Math.round($("ggSl").value)+"%"; $("gbVal").textContent=Math.round($("gbSl").value)+"%";
  $("gamVal").textContent=(+$("gamSl").value).toFixed(2); $("tmpVal").textContent=$("tmpSl").value;
  $("satVal").textContent=Math.round($("satSl").value)+"%"; $("limVal").textContent=Math.round($("limSl").value)+"%";
}
function tunePreview(){
  if (!S) return;
  api("cal_preview", COL.r, COL.g, COL.b).then(o => {
    if (!o) return;
    $("tuneReq").style.background = hx(...o.req); $("tuneOut").style.background = hx(...o.out);
    $("tuneTxt").textContent = `(${o.req}) → (${o.out})`;
  });
}
const tuneChanged = debounce(() => {
  api("tune_set","gains",{r:+$("grSl").value/100,g:+$("ggSl").value/100,b:+$("gbSl").value/100,
    gamma:+$("gamSl").value,temp:+$("tmpSl").value,order:$("ordSel").value,
    sat:+$("satSl").value/100,limit:+$("limSl").value/100,enabled:$("calEn").checked})
    .then(s=>{S=s;setDirty();tunePreview();});
  updTuneLabels();
}, 200);
function renderTuneProfs(){
  const sel = $("tuneProfSel"); sel.innerHTML = "";
  const names = Object.keys(S.tune_profiles||{});
  if (!names.length) sel.innerHTML = "<option>(none yet)</option>";
  names.sort().forEach(n => { const o=document.createElement("option"); o.value=n; o.textContent=n; sel.appendChild(o); });
}

/* ══ MODES ══ */
function renderModes(){
  const g = $("modeGrid"); g.innerHTML = "";
  (S.mode_names||[]).forEach((m,i) => {
    const b = document.createElement("button"); b.className = "modebtn" + (i===S.cfg.last_mode?" cur":"");
    b.textContent = `${String(i).padStart(2,"0")}  ${m}`;
    b.onclick = () => { api("send_mode",i).then(s=>{S=s;setDirty();renderModes();}); };
    g.appendChild(b);
  });
  $("spdSl").value=S.cfg.last_speed; $("spdVal").textContent=S.cfg.last_speed;
  $("mbSl").value=S.cfg.last_brightness; $("mbVal").textContent=S.cfg.last_brightness;
}

/* ══ TIMING ══ */
const DAYS = ["Mo","Tu","We","Th","Fr","Sa","Su"];
function timerCard(parent, title, t, slot){
  const card = document.createElement("div"); card.className="card";
  card.innerHTML = `<h2>${title}</h2>`;
  const row = document.createElement("div"); row.className="row";
  row.innerHTML = `<span style="color:var(--muted)">H</span>`;
  const h = document.createElement("input"); h.type="number"; h.min=0; h.max=23; h.value=t.h; h.style.width="52px";
  const m = document.createElement("input"); m.type="number"; m.min=0; m.max=59; m.value=t.m; m.style.width="52px";
  const on = document.createElement("input"); on.type="checkbox"; on.checked=t.on;
  const onLbl = document.createElement("label"); onLbl.className="chk"; onLbl.append(on,"ON");
  row.append(h,document.createTextNode("M"),m,onLbl);
  t.days.forEach((d,i) => {
    const c = document.createElement("input"); c.type="checkbox"; c.checked=d; c.dataset.day=i;
    const l = document.createElement("label"); l.className="chk"; l.append(c, DAYS[i]); row.appendChild(l);
  });
  const send = document.createElement("button"); send.className="btn small"; send.textContent="Send";
  send.onclick = () => {
    const days = [...row.querySelectorAll("[data-day]")].map(c=>c.checked);
    api("send_timer",slot,+h.value,+m.value,on.checked,days).then(s=>{S=s;setDirty();});
  };
  row.appendChild(send); card.appendChild(row); parent.appendChild(card);
}
function renderTiming(){
  const box = $("timerCards"); box.innerHTML = "";
  timerCard(box, "Timer 1", S.t1, 0); timerCard(box, "Timer 2", S.t2, 1);
  const list = $("schedList"); list.innerHTML = "";
  if (!(S.cfg.schedules||[]).length) list.innerHTML = `<div style="color:var(--muted)">No schedules — add one to automate lights.</div>`;
  (S.cfg.schedules||[]).forEach((s,idx) => {
    const card = document.createElement("div"); card.className="mrow"; card.style.alignItems="flex-start"; card.style.flexWrap="wrap";
    const nm = document.createElement("input"); nm.type="text"; nm.value=s.name; nm.style.width="130px";
    nm.onchange = () => api("sched_set",idx,"name",nm.value).then(s2=>{S=s2;setDirty();});
    const h = document.createElement("input"); h.type="number"; h.min=0;h.max=23; h.value=s.hour; h.style.width="52px";
    const m = document.createElement("input"); m.type="number"; m.min=0;h.max=59; m.value=s.min; m.style.width="52px";
    h.onchange = m.onchange = () => api("sched_time",idx,+h.value,+m.value).then(s2=>{S=s2;setDirty();});
    const act = document.createElement("select");
    ["power_on","power_off","color","mode"].forEach(a=>{const o=document.createElement("option");o.value=a;o.textContent=a;act.appendChild(o);});
    act.value = s.action; act.onchange = () => api("sched_set",idx,"action",act.value).then(s2=>{S=s2;setDirty();});
    const en = document.createElement("input"); en.type="checkbox"; en.checked=s.enabled;
    en.onchange = () => api("sched_set",idx,"enabled",en.checked).then(s2=>{S=s2;setDirty();});
    const del = document.createElement("button"); del.className="btn small danger"; del.textContent="Delete";
    del.onclick = () => api("sched_del",idx).then(s2=>{S=s2;setDirty();renderTiming();});
    card.append(nm,h,document.createTextNode(":"),m,act,en,del);
    const drow = document.createElement("div"); drow.className="row"; drow.style.width="100%";
    (s.days||[]).forEach((d,i) => {
      const c=document.createElement("input"); c.type="checkbox"; c.checked=d;
      c.onchange=()=>api("sched_day",idx,i,c.checked).then(s2=>{S=s2;setDirty();});
      const l=document.createElement("label"); l.className="chk"; l.append(c,DAYS[i]); drow.appendChild(l);
    });
    card.appendChild(drow); list.appendChild(card);
  });
}

/* ══ AMBI ══ */
const AMBI_PRESETS = ["Movie","Game","Balanced","Chill","Party"];
function renderAmbi(){
  const a = S.cfg;
  $("ambiAuto").checked = !!a.ambi_on_startup;
  $("ambiMode").value = a.ambi_mode;
  const mon = $("ambiMon"); mon.innerHTML = "";
  (S.monitors||[[1,"Monitor 1"]]).forEach(([i,lb]) => { const o=document.createElement("option"); o.value=i; o.textContent=lb; mon.appendChild(o); });
  mon.value = String(a.ambi_monitor);
  $("ambiSample").value = a.ambi_sample[0].toUpperCase()+a.ambi_sample.slice(1);
  $("ambiXf").checked = !!a.ambi_crossfade; $("ambiDx").checked = !!a.ambi_use_dxcam;
  $("aFps").value=a.ambi_fps; $("aSm").value=a.ambi_smooth; $("aBri").value=a.ambi_brightness;
  $("aMd").value=a.ambi_min_delta; $("aWf").value=a.ambi_white_floor; $("aIv").value=a.ambi_interval;
  updAmbiLabels();
  const pr = $("ambiPresetRow"); pr.innerHTML = "";
  AMBI_PRESETS.forEach(n => {
    const b = document.createElement("button"); b.className="btn small"; b.textContent=n;
    b.onclick = () => api("ambi_preset",n).then(s=>{S=s;setDirty();renderAmbi();});
    pr.appendChild(b);
  });
  renderAmbiCustom();
  const tiles = $("candTiles"); tiles.innerHTML = "";
  ["average","dominant","vibrant","brightest"].forEach(k => {
    const t = document.createElement("div"); t.className="tile"; t.id="tile-"+k; t.textContent=k.slice(0,3);
    tiles.appendChild(t);
  });
}
function updAmbiLabels(){
  $("aFpsV").textContent=Math.round($("aFps").value); $("aSmV").textContent=(+$("aSm").value).toFixed(1)+"s";
  $("aBriV").textContent=$("aBri").value; $("aMdV").textContent=$("aMd").value;
  $("aWfV").textContent=$("aWf").value+"%"; $("aIvV").textContent=(+$("aIv").value).toFixed(2)+"s";
}
const ambiChanged = debounce(() => {
  updAmbiLabels();
  api("ambi_set",{fps:+$("aFps").value,smooth:+$("aSm").value,brightness:+$("aBri").value,
    min_delta:+$("aMd").value,white_floor:+$("aWf").value,interval:+$("aIv").value,
    mode:$("ambiMode").value,sample:$("ambiSample").value.toLowerCase(),monitor:+$("ambiMon").value,
    crossfade:$("ambiXf").checked,dxcam:$("ambiDx").checked}).then(s=>{S=s;setDirty();});
}, 200);
function renderAmbiCustom(){
  const sel = $("ambiCustomSel"); sel.innerHTML = "";
  const names = Object.keys(S.ambi_custom||{});
  if (!names.length) sel.innerHTML = "<option>(none yet)</option>";
  names.sort().forEach(n => { const o=document.createElement("option"); o.value=n; o.textContent=n; sel.appendChild(o); });
}

/* ══ SETUP ══ */
function renderSetup(){
  const c = S.cfg;
  $("stStartup").checked = !!S.startup_on; $("stTask").checked = !!S.task_on;
  $("stTask").disabled = !S.startup_on;
  $("stMinimized").checked = !!c.start_minimized;
  $("stBtAuto").checked = !!c.bt_on_startup;
  $("stDyn").checked = !!c.dynlight_enabled;
  $("dynStatus").textContent = c.dynlight_enabled ? `${S.dyn_lamps} lamp(s) open` : "off";
  $("stAutoConn").checked = !!c.auto_connect; $("stAutoRe").checked = !!c.auto_reconnect;
  $("stSyncTime").checked = !!c.sync_time_on_startup;
  $("stQuit").checked = !!c.quit_on_close;
  $("stMotion").checked = !!c.reduce_motion;
  document.body.classList.toggle("reduce-motion", !!c.reduce_motion);
  $("cfgPath").textContent = S.cfg_path;
  setBt(S.bt_state);
}

/* ══ render all / boot ══ */
function renderAll(){
  renderSave(); renderConn();
  setOrb(S.cfg.last_r, S.cfg.last_g, S.cfg.last_b);
  renderFound(); renderGroups(); renderColor(); renderTune(); renderModes(); renderTiming(); renderAmbi(); renderSetup();
  $("appVer").textContent = "v"+S.ver; $("ftVer").textContent = "v"+S.ver;
  setBt(S.bt_state);
  $("ftAmbi").textContent = "Ambi: " + (S.ambi_running ? "live" : "idle");
  $("ambiBtn").textContent = S.ambi_running ? "Stop Ambilight" : "Start Ambilight";
  $("ambiBtn").className = S.ambi_running ? "btn danger" : "btn green";
}
function bindStatic(){
  $("orbWrap").onclick = () => showPage("color");
  $("pwrOn").onclick = () => api("power", true);
  $("pwrOff").onclick = () => api("power", false);
  $("saveBtn").onclick = doSave;
  $("scanBtn").onclick = () => { $("devStatus").textContent = "scanning…"; api("scan_start", 5); };
  $("quickBtn").onclick = () => { $("devStatus").textContent = "scanning…"; api("scan_start", 3); };
  $("connAllBtn").onclick = () => api("connect_all");
  $("disconnBtn").onclick = () => api("disconnect_all");
  $("addAllBtn").onclick = () => api("group_add_many", (S.found||[]).map(d=>d.addr)).then(afterMut);
  $("addCheckedBtn").onclick = () => api("group_add_many", [...checked]).then(s=>{checked.clear();afterMut(s);});
  $("groupSel").onchange = e => api("group_select", e.target.value).then(afterMut);
  $("grpNew").onclick = async () => { const n = await askName("New group","Group "+(Object.keys(S.groups).length+1)); if(n) api("group_new",n).then(afterMut); };
  $("grpRename").onclick = async () => { const n = await askName("Rename group", S.selected_group); if(n&&n!==S.selected_group) api("group_rename",n).then(afterMut); };
  $("grpDel").onclick = async () => { if(await askConfirm("Delete group",`Delete '${S.selected_group}'?`)) api("group_delete").then(afterMut); };
  $("forgetBtn").onclick = () => api("forget_remembered").then(s=>{S=s;renderGroups();});
  $("dynRefresh").onclick = () => api("dyn_refresh"); $("dynRefresh2").onclick = () => api("dyn_refresh");
  $("dynAdd").onclick = () => api("group_add_many", ["dynlight://pc"]).then(afterMut);
  // color
  $("hSl").oninput = $("sSl").oninput = $("lSl").oninput = () => {
    setCOL(...hsl2rgb(+$("hSl").value,+$("sSl").value,+$("lSl").value),"hsl"); liveColor(); };
  $("hSl").onchange = $("sSl").onchange = $("lSl").onchange = () => api("set_color",COL.r,COL.g,COL.b).then(s=>{S=s;setDirty();renderRecent();});
  $("rSl").oninput = $("gSl").oninput = $("bSl").oninput = () => {
    setCOL(+$("rSl").value,+$("gSl").value,+$("bSl").value,"rgb"); liveColor(); };
  $("rSl").onchange = $("gSl").onchange = $("bSl").onchange = () => api("set_color",COL.r,COL.g,COL.b).then(s=>{S=s;setDirty();renderRecent();});
  $("hexApply").onclick = () => { let t=$("hexIn").value.trim().replace(/^#/,""); if(/^[0-9a-fA-F]{6}$/.test(t)){
    setCOL(parseInt(t.slice(0,2),16),parseInt(t.slice(2,4),16),parseInt(t.slice(4,6),16)); api("set_color",COL.r,COL.g,COL.b).then(s=>{S=s;setDirty();renderRecent();}); } };
  $("applyColorBtn").onclick = () => api("set_color",COL.r,COL.g,COL.b).then(s=>{S=s;setDirty();renderRecent();});
  $("liveChk").onchange = e => api("set_pref","live_send",e.target.checked).then(s=>{S=s;setDirty();});
  $("briSl").oninput = () => { $("briVal").textContent=$("briSl").value; };
  $("briSl").onchange = () => api("set_brightness",+$("briSl").value,$("chanSel").value).then(s=>{S=s;setDirty();});
  $("briSend").onclick = () => api("set_brightness",+$("briSl").value,$("chanSel").value).then(s=>{S=s;setDirty();});
  $("wSend").onclick = () => api("set_w",+$("wSl").value).then(s=>{S=s;setDirty();});
  $("wSl").oninput = () => $("wVal").textContent=$("wSl").value;
  $("cctSend").onclick = () => api("set_cct",+$("warmSl").value,+$("coldSl").value).then(s=>{S=s;setDirty();});
  $("rgbwSend").onclick = () => api("set_rgbw",$("chRgb").checked,$("chW").checked,$("chCct").checked,$("chanSel").value);
  $("pinsSend").onclick = () => api("set_pins",+$("pin1").value,+$("pin2").value,+$("pin3").value);
  $("warmSl").oninput = () => $("warmVal").textContent=$("warmSl").value;
  $("coldSl").oninput = () => $("coldVal").textContent=$("coldSl").value;
  // tune
  ["grSl","ggSl","gbSl","gamSl","tmpSl","satSl","limSl"].forEach(id=>$(id).oninput=tuneChanged);
  $("calEn").onchange = tuneChanged; $("ordSel").onchange = tuneChanged;
  $("wbShow").onclick = () => api("wb_show");
  document.querySelectorAll("[data-wb]").forEach(b=>b.onclick=()=>api("wb_nudge",b.dataset.wb).then(s=>{S=s;setDirty();renderTune();}));
  $("tpApply").onclick = () => api("tune_prof_apply",$("tuneProfSel").value).then(s=>{S=s;setDirty();renderTune();});
  $("tpSave").onclick = async () => { const n = await askName("Save tune profile",$("tuneProfSel").value!=="(none yet)"?$("tuneProfSel").value:"Profile 1"); if(n) api("tune_prof_save",n).then(s=>{S=s;renderTune();}); };
  $("tpRename").onclick = async () => { const n = await askName("Rename tune profile",$("tuneProfSel").value); if(n) api("tune_prof_rename",$("tuneProfSel").value,n).then(s=>{S=s;renderTune();}); };
  $("tpDel").onclick = async () => { if(await askConfirm("Delete profile",`Delete '${$("tuneProfSel").value}'?`)) api("tune_prof_delete",$("tuneProfSel").value).then(s=>{S=s;renderTune();}); };
  // modes
  $("spdSl").oninput = () => $("spdVal").textContent=$("spdSl").value;
  $("spdSl").onchange = () => api("send_speed",+$("spdSl").value).then(s=>{S=s;setDirty();});
  $("spdSend").onclick = () => api("send_speed",+$("spdSl").value).then(s=>{S=s;setDirty();});
  $("mbSl").oninput = () => $("mbVal").textContent=$("mbSl").value;
  $("mbSl").onchange = () => api("set_brightness",+$("mbSl").value,"ALL").then(s=>{S=s;setDirty();});
  $("mbSend").onclick = () => api("set_brightness",+$("mbSl").value,"ALL").then(s=>{S=s;setDirty();});
  // timing
  $("syncTimeBtn").onclick = () => api("sync_time");
  $("schedAdd").onclick = () => api("sched_add").then(s=>{S=s;setDirty();renderTiming();});
  // ambi
  $("ambiBtn").onclick = () => api(S.ambi_running ? "ambi_stop" : "ambi_start").then(s=>{ if(s){S=s;} });
  $("ambiAuto").onchange = e => api("ambi_auto",e.target.checked).then(s=>{S=s;});
  ["aFps","aSm","aBri","aMd","aWf","aIv"].forEach(id=>$(id).oninput=ambiChanged);
  ["ambiMode","ambiSample","ambiMon","ambiXf","ambiDx"].forEach(id=>$(id).onchange=ambiChanged);
  $("acApply").onclick = () => api("ambi_custom_apply",$("ambiCustomSel").value).then(s=>{S=s;setDirty();renderAmbi();});
  $("acSave").onclick = async () => { const n = await askName("Save ambi preset","Custom 1"); if(n) api("ambi_custom_save",n).then(s=>{S=s;renderAmbi();}); };
  $("acRename").onclick = async () => { const n = await askName("Rename preset",$("ambiCustomSel").value); if(n) api("ambi_custom_rename",$("ambiCustomSel").value,n).then(s=>{S=s;renderAmbi();}); };
  $("acDel").onclick = async () => { if(await askConfirm("Delete preset",`Delete '${$("ambiCustomSel").value}'?`)) api("ambi_custom_delete",$("ambiCustomSel").value).then(s=>{S=s;renderAmbi();}); };
  // setup
  $("stStartup").onchange = e => api("set_startup",e.target.checked).then(s=>{S=s;renderSetup();});
  $("stTask").onchange = e => api("set_task",e.target.checked).then(s=>{S=s;renderSetup();});
  $("stMinimized").onchange = e => api("set_pref","start_minimized",e.target.checked).then(s=>{S=s;});
  $("stBtAuto").onchange = e => api("set_pref","bt_on_startup",e.target.checked).then(s=>{S=s;});
  $("btRefresh").onclick = () => api("bt_refresh");
  $("btOn").onclick = () => api("bt_enable");
  $("btSettings").onclick = () => api("bt_settings");
  $("stDyn").onchange = e => api("set_dyn",e.target.checked).then(s=>{S=s;renderSetup();renderGroups();});
  $("stAutoConn").onchange = e => api("set_pref","auto_connect",e.target.checked).then(s=>{S=s;});
  $("stAutoRe").onchange = e => api("set_pref","auto_reconnect",e.target.checked).then(s=>{S=s;});
  $("stSyncTime").onchange = e => api("set_pref","sync_time_on_startup",e.target.checked).then(s=>{S=s;});
  $("stQuit").onchange = e => api("set_pref","quit_on_close",e.target.checked).then(s=>{S=s;});
  $("stMotion").onchange = e => api("set_pref","reduce_motion",e.target.checked).then(s=>{S=s;renderSetup();});
  $("cfgOpen").onclick = () => api("open_config_folder");
  $("cfgSave").onclick = doSave;
}

/* ── backend events: drained by poll (never pushed cross-thread) ── */
function handlePush(evt){
  try {
    if (evt.t === "log") logMsg(evt.msg);
    else if (evt.t === "found") { S.found = evt.devices; renderFound(); }
    else if (evt.t === "conn") { S.connected = evt.connected; if(evt.names) S.cfg.names = evt.names;
      if(evt.last) S.cfg.last_addresses = evt.last; renderConn(); renderFound(); renderGroups(); }
    else if (evt.t === "bt") { S.bt_state = evt.state; setBt(evt.state); }
    else if (evt.t === "dyn") { S.dyn_lamps = evt.lamps; renderGroups(); renderSetup(); }
    else if (evt.t === "ambi_tick") {
      const c = evt.color;
      $("ambiPrev").style.background = hx(...c);
      $("ambiStat").textContent = `f=${evt.frames} s=${evt.sends} cap=${evt.cap}ms${evt.eco?" eco":""} rgb=(${c})`;
      setOrb(...c);
      ["average","dominant","vibrant","brightest"].forEach(k => {
        const t = $("tile-"+k); if (t && evt.stats[k]) { t.style.background = hx(...evt.stats[k]); }
      });
    }
    else if (evt.t === "ambi_state") { S.ambi_running = evt.running;
      $("ftAmbi").textContent = "Ambi: " + (evt.running ? "live" : "idle");
      $("ambiBtn").textContent = evt.running ? "Stop Ambilight" : "Start Ambilight";
      $("ambiBtn").className = evt.running ? "btn danger" : "btn green"; }
    else if (evt.t === "state") { S = evt.state; dirty = !!evt.state.dirty; renderAll(); }
  } catch(e) { console.error(e); }
}
window.__POLL_MS = 500;
setInterval(() => {
  try { api("poll").then(evts => (evts||[]).forEach(handlePush)).catch(()=>{}); }
  catch(e) {}
}, window.__POLL_MS);

let booted = false;
async function boot(){
  if (booted) return; booted = true;
  buildNav(); bindStatic();
  S = await api("get_state");
  dirty = !!S.dirty;
  renderAll();
  $("pg-devices").classList.add("on");
  PAGES.forEach(([k],i) => { $("nav-"+k).classList.toggle("on", i===0); $("tick-"+k).classList.toggle("on", i===0); });
  logMsg("WebUI ready.");
  try { api("ui_booted", { pages: document.querySelectorAll(".page").length,
    modes: document.querySelectorAll(".modebtn").length,
    presets: document.querySelectorAll("#presetRow .sw").length }); } catch(e) {}
}
window.addEventListener("pywebviewready", boot);
window.addEventListener("DOMContentLoaded", () => setTimeout(() => { if (!booted) boot(); }, 300));
(function waitBridge(){ // fallback if the ready event was missed entirely
  if (booted) return;
  if (window.pywebview && window.pywebview.api) boot();
  else setTimeout(waitBridge, 100);
})();
