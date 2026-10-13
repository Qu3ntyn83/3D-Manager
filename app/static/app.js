"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const api = async (url, opt = {}) => {
  const r = await fetch(url, { headers: opt.body && !(opt.body instanceof FormData) ? { "Content-Type": "application/json" } : {}, ...opt });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.status === 204 ? null : r.json();
};
const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem("finv." + k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("finv." + k, JSON.stringify(v)); } catch {} },
};
const toast = msg => { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2200); };

const SRC = { bambu: "Hersteller", community: "Community", estimated: "geschätzt", manual: "manuell" };
const SRC_LONG = {
  bambu: "Farbwert offiziell vom Hersteller (Bambu Studio Farbdatenbank)",
  community: "Farbwert aus Community-Messung (color-tools DB)",
  estimated: "Farbwert geschätzt – bei Bedarf mit Foto/Pipette korrigieren",
  manual: "Farbwert manuell gepflegt",
};

let state = {
  fil: [], mat: [], spools: [], bstat: null, live: [], tags: [], sub: store.get("sub", "ams"), tab: store.get("tab", "overview"), view: ["grid", "table", "check"].includes(store.get("view", "grid")) ? store.get("view", "grid") : "grid",
  tsort: store.get("tsort", { key: "", dir: 1 }),
  q: "", brand: "", material: store.get("material", ""), sort: store.get("sort", "material"), sel: new Set(),
};

/* ---------- Farbmuster ---------- */
function hexToRgb(h) { h = h.replace("#", ""); return [0, 2, 4].map(i => parseInt(h.substr(i, 2), 16) || 0); }
function alphaOf(h) { h = h.replace("#", ""); return h.length >= 8 ? parseInt(h.substr(6, 2), 16) / 255 : 1; }
function hue(h) {
  let [r, g, b] = hexToRgb(h).map(v => v / 255);
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn, l = (mx + mn) / 2;
  if (d < 0.08) return 1000 + (1 - l); // Graustufen ans Ende, hell → dunkel
  let H = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return ((H * 60) + 360) % 360;
}
function swatchClass(item) {
  const m = (item.material || item.type || "").toLowerCase();
  return [m.includes("matte") ? "matte" : "", m.includes("silk") ? "silk" : "", /galaxy|sparkle|glitter|galaxie/.test(m) ? "sparkle" : ""].join(" ");
}
function swatchHTML(item, { hex = true, img = true } = {}) {
  const cols = (item.colors && item.colors.length ? item.colors : ["#888888FF"]);
  // Bambu speichert transparente Supports als #00000000 → als "natur" darstellen
  const c = cols.map(x => /^#?0{8}$/.test(x.replace("#", "")) ? "#EDE8DC40" : x);
  let bg;
  if (c.length === 1) bg = c[0];
  else if (item.pattern === "multi") bg = `linear-gradient(90deg, ${c.map((x, i) => `${x} ${i / c.length * 100}% ${(i + 1) / c.length * 100}%`).join(",")})`;
  else bg = `linear-gradient(135deg, ${c.join(",")})`;
  const label = cols[0].slice(0, 7).toUpperCase() + (alphaOf(cols[0]) < 1 ? " · transl." : "");
  return `<div class="swatch ${swatchClass(item)}">
    <div class="fill" style="background:${bg}"></div>
    ${img && item.image ? `<img src="/uploads/${esc(item.image)}" alt="Foto Farbmuster" loading="lazy">` : ""}
    <div class="shine"></div>
    ${hex ? `<span class="hex">${label}</span>` : ""}
  </div>`;
}

/* ---------- Filament ---------- */
function filtered() {
  const terms = state.q.toLowerCase().split(/\s+/).filter(Boolean);
  let list = state.fil.filter(f => {
    if (state.brand && f.brand !== state.brand) return false;
    if (state.material && f.material !== state.material) return false;
    const hay = `${f.brand} ${f.material} ${f.color_name} ${f.official_name} ${f.sku} ${f.location} ${f.notes} ${f.colors.join(" ")} ${(f.spools || []).map(s => s.rfid + " " + s.tray_id_name).join(" ")} ${rfidOf(f).map(s => (s.rfid || "") + " " + (s.loc || "")).join(" ")}`.toLowerCase();
    const hasTag = rfidOf(f).length > 0;
    return terms.every(t => t === "rfid" ? hasTag : t === "ohne-rfid" || t === "!rfid" ? !hasTag : hay.includes(t));
  });
  const by = {
    material: (a, b) => a.material.localeCompare(b.material) || hue(a.colors[0]) - hue(b.colors[0]),
    color: (a, b) => hue(a.colors[0]) - hue(b.colors[0]),
    brand: (a, b) => a.brand.localeCompare(b.brand) || a.material.localeCompare(b.material),
    qty: (a, b) => a.qty - b.qty || a.material.localeCompare(b.material),
    check: (a, b) => (a.last_check || "").localeCompare(b.last_check || ""),
  }[state.sort];
  return list.sort(by);
}
const amsLabel = s => {
  if (!s.in_printer) return "";
  const a = +s.ams_id, slot = parseInt(s.slot_id, 10);
  if (isNaN(a) || a >= 128) return "Ext. Spule" + (s.device_name ? " · " + s.device_name : "");
  return `AMS ${String.fromCharCode(65 + a)}${isNaN(slot) ? "" : slot + 1}` + (s.device_name ? " · " + s.device_name : "");
};
const slotName = (aid, tid) => aid == null ? "" : aid === 255 ? "Ext." + (tid > 255 ? "" : "") : aid >= 128 ? `AMS HT ${aid - 127}` : `AMS ${String.fromCharCode(65 + aid)}${tid + 1}`;
function rfidOf(f) {
  const tags = (f.tags || []).map(t => ({
    kind: "tag", key: t.tag_key, rfid: t.tray_uuid && !/^0+$/.test(t.tray_uuid) ? t.tray_uuid : t.tag_uid,
    grams: t.remain_g, total: t.full_weight_g, percent: t.remain_pct >= 0 ? t.remain_pct : null,
    loc: t.in_ams ? slotName(t.ams_id, t.tray_id) + (t.printer_name ? " · " + t.printer_name : "") : "", live: true, consumed: t.consumed_g,
  }));
  const seen = new Set(tags.map(t => (t.rfid || "").toUpperCase()));
  const cloud = (f.spools || []).filter(s => s.present && !(s.rfid && seen.has(s.rfid.toUpperCase()))).map(s => ({
    kind: "cloud", id: s.cloud_id, rfid: s.rfid, grams: s.eff_grams ?? s.net_weight, total: s.eff_total ?? s.total_net_weight, percent: s.percent, loc: amsLabel(s), live: false,
  }));
  return [...tags, ...cloud];
}
function spoolTags(f) {
  const sp = rfidOf(f);
  return sp.length > f.qty ? `<span class="tag low" title="Mehr verlinkte Spulen als Bestand">Bestand prüfen</span>` : "";
}
/* Markierung „RFID zugewiesen“ – im AMS mit Slot */
function rfidFlag(f, mini = false) {
  const sp = rfidOf(f);
  if (!sp.length) return mini ? `<span class="rflag none" title="Kein RFID-Tag zugewiesen">–</span>` : "";
  const ams = [...new Set(sp.filter(s => s.loc).map(s => s.loc.split(" · ")[0]))];
  const known = sp.filter(s => s.grams != null), g = known.reduce((a, s) => a + s.grams, 0);
  const title = `RFID zugewiesen (${sp.length})${known.length ? ` · ${g} g` : ""}\n` +
    sp.map(s => `${s.kind === "tag" ? "Tag" : "Cloud"} ${(s.rfid || "–").slice(0, 12)}${s.loc ? " · " + s.loc : ""}${s.grams != null ? " · " + s.grams + " g" : ""}`).join("\n");
  const label = ams.length ? ams.join(" · ") : mini ? "RFID" : `RFID${sp.length > 1 ? " ×" + sp.length : ""}`;
  return `<span class="rflag ${ams.length ? "live" : ""}" title="${esc(title)}"><i aria-hidden="true">◉</i>${esc(label)}</span>`;
}
const barI = s => {
  const p = s.percent ?? (s.total && s.grams != null ? Math.round(s.grams / s.total * 100) : null);
  return `<span class="bar" title="${s.grams ?? "?"} / ${s.total ?? "?"} g${p != null ? ` · ${p} %` : ""}"><i style="width:${Math.max(0, Math.min(100, p ?? 0))}%"></i></span><em>${s.grams != null ? s.grams + " g" : p != null ? p + " %" : "?"}</em>`;
};
const fmtDate = d => d ? new Date(d + "T00:00").toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "2-digit" }) : "–";

function renderFil() {
  const list = filtered();
  const grid = $("#filGrid");
  if (state.view === "table" || state.view === "check") {
    state.view === "table" ? renderTable(list) : renderCheck(list);
    $("#filEmpty").hidden = list.length > 0; renderChips(); renderStats(); renderBulk(); return;
  }
  grid.className = "grid" + (state.sel.size ? " selecting" : "");
  grid.innerHTML = list.map(f => {
    const low = f.qty <= (f.min_qty || 0) && f.min_qty > 0 || f.qty === 0;
    const tags = [
      f.refill && "Refill", f.sample && "Muster ✓", f.location && "📍 " + f.location,
    ].filter(Boolean).map(t => `<span class="tag">${esc(t)}</span>`).join("") + (typeof unitTags === "function" ? unitTags(f) : "") + spoolTags(f) +
    (safeUrl(f.buy_url) ? `<a class="tag lnk buy" href="${esc(safeUrl(f.buy_url))}" target="_blank" rel="noopener noreferrer" title="Nachkaufen: ${esc(hostOf(safeUrl(f.buy_url)))}">🛒</a>` : "");
    return `<article class="card" data-id="${f.id}">
      <input type="checkbox" class="sel" ${state.sel.has(f.id) ? "checked" : ""} aria-label="auswählen">
      ${swatchHTML(f)}${rfidFlag(f)}
      <div class="body">
        <div class="name">${esc(f.color_name)}</div>
        <div class="meta">${esc(f.material)} · ${esc(f.brand)}${f.sku ? ` · <code>${esc(f.sku)}</code>` : ""}</div>
        <div class="tags">
          <span class="tag src-${f.swatch_source}" title="${esc(SRC_LONG[f.swatch_source] || "")}">${SRC[f.swatch_source] || f.swatch_source}</span>
          ${low ? `<span class="tag low">nachkaufen</span>` : ""}${tags}
        </div>
      </div>
      <div class="qty">
        <button data-d="-1" aria-label="weniger">−</button>
        <span class="n ${f.qty === 0 ? "zero" : ""}">${f.qty}</span>
        <button data-d="1" aria-label="mehr">+</button>
        <span class="chk" title="Letzte Prüfung">✓ ${fmtDate(f.last_check)}</span>
      </div>
    </article>`;
  }).join("");
  $("#filEmpty").hidden = list.length > 0;
  renderChips(); renderStats(); renderBulk();
}

function renderChips() {
  const counts = {};
  state.fil.filter(f => !state.brand || f.brand === state.brand).forEach(f => counts[f.material] = (counts[f.material] || 0) + f.qty);
  const mats = Object.keys(counts).sort();
  $("#matChips").innerHTML = `<button class="chip ${state.material ? "" : "active"}" data-m="">Alle<b>${Object.values(counts).reduce((a, b) => a + b, 0)}</b></button>` +
    mats.map(m => `<button class="chip ${state.material === m ? "active" : ""}" data-m="${esc(m)}">${esc(m)}<b>${counts[m]}</b></button>`).join("");
}
function renderStats() {
  const spools = state.fil.reduce((a, f) => a + f.qty, 0);
  const colors = new Set(state.fil.filter(f => f.qty > 0).map(f => f.brand + f.sku + f.color_name)).size;
  const low = state.fil.filter(f => f.qty === 0 || (f.min_qty > 0 && f.qty <= f.min_qty)).length;
  const mats = state.mat.filter(m => kindOf(m) === "material").reduce((a, m) => a + m.qty, 0);
  const acc = state.mat.filter(m => kindOf(m) === "zubehoer").reduce((a, m) => a + m.qty, 0);
  $("#stats").textContent = `${spools} Spulen · ${colors} Farben · ${low} nachkaufen · ${mats} Material · ${acc} Zubehör`;
}
function renderBulk() {
  $("#bulk").hidden = state.sel.size === 0;
  $("#bulkCount").textContent = `${state.sel.size} ausgewählt`;
}
function fillFilters() {
  const brands = [...new Set(state.fil.map(f => f.brand))].sort();
  $("#fBrand").innerHTML = `<option value="">Alle Marken</option>` + brands.map(b => `<option ${b === state.brand ? "selected" : ""}>${esc(b)}</option>`).join("");
  api("/api/catalog/brands").then(cb => { $("#brandList").innerHTML = [...new Set([...brands, ...cb])].map(b => `<option value="${esc(b)}">`).join(""); });
  $("#matList").innerHTML = [...new Set(state.fil.map(f => f.material))].sort().map(m => `<option value="${esc(m)}">`).join("");
  $("#catList").innerHTML = [...new Set(state.mat.map(m => m.category).filter(Boolean))].sort().map(m => `<option value="${esc(m)}">`).join("");
  $("#mfrList").innerHTML = [...new Set([...state.mat.map(m => m.manufacturer), ...state.fil.map(f => f.brand)].filter(Boolean))].sort().map(m => `<option value="${esc(m)}">`).join("");
}


/* ---------- Tabellenansicht ---------- */
const TCOLS = [
  { k: "swatch", t: "", s: f => hue(f.colors[0]) },
  { k: "color_name", t: "Farbe", s: f => f.color_name.toLowerCase() },
  { k: "material", t: "Material", s: f => f.material.toLowerCase() },
  { k: "brand", t: "Marke", s: f => f.brand.toLowerCase() },
  { k: "sku", t: "ID", s: f => f.sku || "~" },
  { k: "hex", t: "Hex", s: f => f.colors[0] },
  { k: "qty", t: "Anz.", s: f => f.qty, num: true },
  { k: "sealed", t: "OVP", s: f => +f.sealed, tog: true },
  { k: "spool", t: "Spule", s: f => +f.spool, tog: true },
  { k: "refill", t: "Refill", s: f => +f.refill, tog: true },
  { k: "sample", t: "Muster", s: f => +f.sample, tog: true },
  { k: "rfidf", t: "RFID", s: f => rfidOf(f).length ? (rfidOf(f).some(s => s.loc) ? 2 : 1) : 0, c: true },
  { k: "location", t: "Lagerort", s: f => (f.location || "~").toLowerCase() },
  { k: "rfid", t: "Spulen / Rest", s: f => (f.units || []).reduce((a, u) => a + (u.status !== "empty" ? u.remaining_g || 0 : 0), 0), num: true },
  { k: "last_check", t: "Geprüft", s: f => f.last_check || "" },
];
function renderTable(list) {
  const col = TCOLS.find(c => c.k === state.tsort.key);
  if (col) list = [...list].sort((a, b) => { const x = col.s(a), y = col.s(b); return (x > y ? 1 : x < y ? -1 : 0) * state.tsort.dir; });
  const grid = $("#filGrid");
  grid.className = "tablemode";
  const allSel = list.length && list.every(f => state.sel.has(f.id));
  grid.innerHTML = `<div class="table-wrap"><table class="tbl fil">
    <thead><tr><th><input type="checkbox" class="tsel-all" ${allSel ? "checked" : ""} aria-label="alle auswählen"></th>${TCOLS.map(c =>
      `<th data-k="${c.k}" class="${c.num ? "num" : ""} ${c.tog || c.c ? "c" : ""} ${state.tsort.key === c.k ? "sorted" : ""}">${c.t}${state.tsort.key === c.k ? (state.tsort.dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr></thead>
    <tbody>${list.map(f => {
      const sp = rfidOf(f);
      const low = f.qty === 0 || (f.min_qty > 0 && f.qty <= f.min_qty);
      return `<tr data-id="${f.id}" class="${low ? "low" : ""}">
        <td class="nolink"><input type="checkbox" class="sel" ${state.sel.has(f.id) ? "checked" : ""}></td>
        <td>${swatchHTML(f, { hex: false })}</td>
        <td class="strong">${esc(f.color_name)}${safeUrl(f.buy_url) ? ` <a class="lnk buy mini" href="${esc(safeUrl(f.buy_url))}" target="_blank" rel="noopener noreferrer" title="Nachkaufen">🛒</a>` : ""}${f.official_name && f.official_name !== f.color_name ? `<small>${esc(f.official_name)}</small>` : ""}</td>
        <td>${esc(f.material)}</td><td>${esc(f.brand)}</td>
        <td class="mono">${esc(f.sku || "")}</td>
        <td class="mono">${f.colors[0].slice(0, 7)}<span class="src src-${f.swatch_source}" title="${esc(SRC_LONG[f.swatch_source] || "")}"></span></td>
        <td class="num nolink"><div class="qty"><button data-d="-1">−</button><span class="n ${f.qty === 0 ? "zero" : ""}">${f.qty}</span><button data-d="1">+</button></div></td>
        ${["sealed", "spool", "refill", "sample"].map(k => `<td class="c nolink"><input type="checkbox" class="tog" data-f="${k}" ${f[k] ? "checked" : ""}></td>`).join("")}
        <td class="c">${rfidFlag(f, true)}</td>
        <td>${esc(f.location || "")}</td>
        <td>${typeof unitCell === "function" ? unitCell(f) : ""}</td>
        <td class="mono">${fmtDate(f.last_check)}</td>
      </tr>`;
    }).join("")}</tbody></table></div>`;
}
const bar = s => {
  const p = s.percent ?? 0;
  return `<span class="bar" title="${s.net_weight ?? "?"} / ${s.total_net_weight ?? "?"} g"><i style="width:${Math.max(0, Math.min(100, p))}%"></i></span><em>${s.net_weight ?? "?"} g</em>`;
};
async function toggleField(id, field, val) {
  const upd = await api(`/api/filaments/${id}`, { method: "PATCH", body: JSON.stringify({ [field]: val }) });
  Object.assign(state.fil.find(f => f.id === id), upd);
  renderFil();
}

/* ---------- Bambu Filament Manager ---------- */
const fmtTime = t => t ? new Date(t).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "nie";
let loginStep = null, loginError = null, diagResult = null, lastAccount = "", lastRegion = "global";
async function loadBambu() {
  [state.bstat, state.spools] = await Promise.all([api("/api/bambu/status"), api("/api/bambu/spools")]);
  renderBambu();
}
function renderBambu() {
  const st = state.bstat; if (!st) return;
  const dot = $("#bambuDot");
  dot.className = "dot " + (!st.connected ? "" : st.error || !st.token_valid ? "bad" : "ok");
  dot.title = st.error || "";
  const conn = $("#bbConn");
  if (st.connected && !loginStep) {
    conn.innerHTML = `<h3>Bambu Filament Manager</h3>
      <p class="muted">${st.mock ? "Demo-Modus (BAMBU_MOCK)" : `Verbunden als <b>${esc(st.account || "?")}</b> · ${st.region === "china" ? "China" : "Global"}`}</p>
      ${st.error ? `<p class="err">⚠ ${esc(st.error)}</p>` : ""}
      ${!st.token_valid ? `<p class="err">Token abgelaufen – bitte neu anmelden.</p>` : ""}
      <div class="row">
        <label class="inline">Auto-Sync
          <select id="bInterval">${[0, 5, 15, 30, 60, 180].map(m => `<option value="${m}" ${st.interval === m ? "selected" : ""}>${m ? `alle ${m} min` : "aus"}</option>`).join("")}</select>
        </label>
        <label class="inline chk" title="Nur sinnvoll, wenn alle Tags auf originalen Bambu-Spulen sitzen"><input type="checkbox" id="bAutoLink" ${st.auto_link ? "checked" : ""}> neue Original-Bambu-Spulen nach Bambu-Angabe zuordnen</label>
        <span class="spacer"></span>
        ${!st.token_valid ? `<button id="bRelogin" class="primary">Neu anmelden</button>` : ""}
        ${st.mock ? "" : `<button id="bLogout" class="ghost danger">Abmelden</button>`}
      </div>`;
  } else {
    const step = loginStep || "login";
    conn.innerHTML = `<h3>Mit Bambu-Account verbinden</h3>
      <p class="muted">Liest die Spulen aus dem Filament Manager (Bambu Studio / Handy) inkl. RFID, Restgewicht und AMS-Slot. Nur lesend; das Passwort wird nicht gespeichert, nur das Token (ca. 90 Tage gültig).</p>
      ${step === "login" ? `<form id="bLoginForm" class="row">
          <input name="account" type="email" placeholder="E-Mail" required autocomplete="username" value="${esc(lastAccount || st.account || "")}">
          <input name="password" type="password" placeholder="Passwort" autocomplete="current-password">
          <select name="region"><option value="global" ${lastRegion === "global" ? "selected" : ""}>Global</option><option value="china" ${lastRegion === "china" ? "selected" : ""}>China</option></select>
          <button class="primary">Anmelden</button>
          <button type="button" class="ghost" id="bCodeOnly" title="Ohne Passwort: Login-Code per E-Mail">Code per Mail</button>
        </form>
        <details class="tokenbox"><summary>Token direkt einfügen</summary>
          <form id="bTokenForm" class="row"><input name="token" placeholder="Bearer-Token (accessToken)" required><button>Übernehmen</button></form>
        </details>`
      : `<form id="bCodeForm" class="row">
          <input name="code" inputmode="numeric" autocomplete="one-time-code" placeholder="${step === "tfa" ? "2FA-Code (Authenticator)" : "Code aus der E-Mail"}" required autofocus>
          <button class="primary">Bestätigen</button><button type="button" class="ghost" id="bCancel">Abbrechen</button>
        </form>`}
      ${loginError ? `<p class="err">⚠ ${esc(loginError)}</p>` : st.error ? `<p class="err">⚠ ${esc(st.error)}</p>` : ""}
      <div class="row"><button type="button" class="ghost" id="bDiag">Verbindung zur Bambu-Cloud testen</button></div>
      ${diagResult ? `<div class="diag">${diagResult.map(d => `<div class="${d.ok ? "ok" : "bad"}">${d.ok ? "✓" : "✗"} ${esc(d.step)} – ${esc(d.detail || "")}</div>`).join("")}</div>` : ""}`;
  }
  $("#bbStats").innerHTML = `<div class="kpis">
      <div><b>${st.spools}</b><span>Spulen in der Cloud</span></div>
      <div><b>${st.linked}</b><span>verlinkt</span></div>
      <div><b>${st.spools - st.linked}</b><span>offen</span></div>
      <div><b>${st.in_printer}</b><span>im AMS</span></div>
    </div><p class="muted">Letzter Sync: ${fmtTime(st.last_sync)}</p>`;
  renderSpools();
}
function renderSpools() {
  const terms = ($("#bq").value || "").toLowerCase().split(/\s+/).filter(Boolean);
  const flt = $("#bFilter").value;
  const list = state.spools.filter(s => {
    if (flt === "unlinked" && (s.filament_ref || !s.present)) return false;
    if (flt === "linked" && !s.filament_ref) return false;
    if (flt === "printer" && !s.in_printer) return false;
    if (flt === "gone" && s.present) return false;
    if (flt !== "gone" && !s.present && flt) return false;
    const hay = `${s.vendor} ${s.name} ${s.color} ${s.rfid} ${s.tray_id_name} ${s.note} ${(s.catalog || []).map(c => c.name + " " + c.sku).join(" ")} ${amsLabel(s)} ${s.linked ? s.linked.color_name : ""}`.toLowerCase();
    return terms.every(t => hay.includes(t));
  });
  const opts = fil => `<option value="">– verlinken mit … –</option>` + fil.map(f => `<option value="${f.id}">${esc(f.color_name)} · ${esc(f.material)} · ${esc(f.brand)}${f.sku ? " · " + f.sku : ""}</option>`).join("");
  const sortedFil = [...state.fil].sort((a, b) => a.material.localeCompare(b.material) || a.color_name.localeCompare(b.color_name));
  $("#bBody").innerHTML = list.map(s => {
    const cat = s.catalog?.[0];
    const title = cat ? cat.name : (s.note || s.color);
    const linkCell = pickBtn(s.filament_ref, `data-cid="${s.cloud_id}" data-fid="${s.filament_ref || ""}"`);
    return `<tr data-cid="${s.cloud_id}" class="${s.present ? "" : "gone"}">
      <td>${swatchHTML({ colors: s.colors.length ? s.colors : [s.color || "#888888FF"], pattern: s.colors.length > 1 ? "gradient" : "solid", material: s.name }, { hex: false, img: false })}</td>
      <td class="strong">${esc(title)}<small>${esc(s.vendor || "")} · ${esc(s.name || s.type || "")}${cat ? " · " + cat.sku : ""}${s.create_type === "manual" ? " · manuell erfasst" : ""}</small></td>
      <td class="mono">${s.rfid ? `<span class="rfidv" title="${esc(s.rfid)}">${esc(s.rfid.slice(0, 8))}…</span>` : `<span class="muted">kein RFID</span>`}<small>${esc(s.tray_id_name || "")}</small></td>
      <td>${bar(s)}${s.percent != null ? `<small>${s.percent} %</small>` : ""}</td>
      <td>${s.in_printer ? `<span class="tag ams">${esc(amsLabel(s))}</span>` : s.present ? `<span class="muted">Lager</span>` : `<span class="tag low">nicht mehr in Cloud</span>`}</td>
      <td>${linkCell}</td>
      <td class="acts">${s.filament_ref ? `<button class="ghost" data-act="open">Öffnen</button><button class="ghost" data-act="unlink" title="Verknüpfung lösen">✕</button>` : `<button class="ghost" data-act="create" title="Neuen Inventar-Eintrag aus dieser Spule anlegen">+ Neu</button>`}</td>
    </tr>`;
  }).join("");
  $("#bEmpty").hidden = list.length > 0 || state.spools.length > 0 && !!flt;
}
async function linkSpool(cid, fid) {
  await api(`/api/bambu/spools/${cid}/link`, { method: "POST", body: JSON.stringify({ filament_id: fid }) });
  await refreshAll(); toast(fid ? "RFID-Spule verlinkt" : "Verknüpfung gelöst");
}
async function refreshAll() {
  [state.fil, state.mat] = await Promise.all([api("/api/filaments"), api("/api/materials")]);
  await Promise.all([loadBambu(), loadAms()]); fillFilters(); renderFil(); renderMat();
}
$("#bBody").addEventListener("click", async e => {
  const tr = e.target.closest("tr[data-cid]"); if (!tr) return;
  const cid = +tr.dataset.cid, s = state.spools.find(x => x.cloud_id === cid);
  const sug = e.target.closest(".sug"); if (sug) return linkSpool(cid, +sug.dataset.link);
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act === "unlink") return linkSpool(cid, null);
  if (act === "open") { setTab("filaments"); return openFil(state.fil.find(f => f.id === s.filament_ref)); }
  if (act === "create") { const r = await api(`/api/bambu/spools/${cid}/create`, { method: "POST" }); await refreshAll(); toast("Inventar-Eintrag angelegt und verlinkt"); return; }
});
$("#bBody").addEventListener("change", e => { if (e.target.matches(".linksel")) linkSpool(+e.target.closest("tr").dataset.cid, e.target.value ? +e.target.value : null); });
$("#bq").oninput = renderSpools; $("#bFilter").onchange = renderSpools;
$("#bSync").onclick = async () => {
  $("#bSync").disabled = true;
  try { const r = await api("/api/bambu/sync", { method: "POST" }); toast(`${r.total} Spulen · ${r.new} neu · ${r.auto_linked} automatisch verlinkt`); }
  catch { toast("Sync fehlgeschlagen – siehe Hinweis oben"); }
  $("#bSync").disabled = false; await refreshAll();
};

const post = async (url, body) => {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.ok === false) throw new Error(j.error || r.statusText);
  return j;
};
$("#bbConn").addEventListener("submit", async e => {
  e.preventDefault();
  const fd = Object.fromEntries(new FormData(e.target));
  if (fd.account) lastAccount = fd.account;
  if (fd.region) lastRegion = fd.region;
  try {
    let r;
    if (e.target.id === "bLoginForm") r = await post("/api/bambu/login", fd);
    else if (e.target.id === "bCodeForm") r = await post("/api/bambu/code", fd);
    else if (e.target.id === "bTokenForm") r = await post("/api/bambu/token", fd);
    loginStep = r.step && r.step !== "done" ? r.step : null;
    if (r.message) toast(r.message);
    if (!loginStep) toast("Mit Bambu verbunden");
    loginError = null;
  } catch (err) { loginError = err.message; toast("Anmeldung fehlgeschlagen"); }
  await refreshAll();
});
$("#bbConn").addEventListener("click", async e => {
  const id = e.target.id;
  if (id === "bCancel") { loginStep = null; loginError = null; renderBambu(); }
  if (id === "bDiag") {
    e.target.disabled = true; e.target.textContent = "teste …";
    const region = $("#bLoginForm")?.region?.value || "global";
    try { diagResult = await api("/api/bambu/diag?region=" + region); } catch (err) { diagResult = [{ step: "App-Server", ok: false, detail: err.message }]; }
    renderBambu();
  }
  if (id === "bRelogin") { loginStep = "login"; renderBambu(); }
  if (id === "bLogout" && confirm("Bambu-Verbindung trennen? Verlinkungen bleiben erhalten.")) { await post("/api/bambu/logout"); await refreshAll(); }
  if (id === "bCodeOnly") {
    const f = $("#bLoginForm"); if (!f.account.value) return toast("E-Mail eingeben");
    try { await post("/api/bambu/request-code", { account: f.account.value, region: f.region.value }); loginStep = "code"; renderBambu(); toast("Code wurde verschickt"); }
    catch (err) { loginError = err.message; renderBambu(); }
  }
});
$("#bbConn").addEventListener("change", async e => {
  if (e.target.id === "bInterval") state.bstat = await api("/api/bambu/settings", { method: "PUT", body: JSON.stringify({ interval: +e.target.value }) });
  if (e.target.id === "bAutoLink") state.bstat = await api("/api/bambu/settings", { method: "PUT", body: JSON.stringify({ auto_link: e.target.checked }) });
});

/* RFID-Bereich im Filament-Dialog */
function renderRfidBox() {
  const box = $("#rfidBox");
  const all = state.rfid || [];
  if (!editing || !all.length) { box.hidden = true; return; }
  box.hidden = false;
  const mine = all.filter(t => t.filament_ref === editing.id);
  $("#rfidList").innerHTML = mine.length ? mine.map(t => `<div class="rfidrow" data-key="${esc(t.key)}">
      <span class="mono" title="${esc(t.key)}">◉ ${esc(shortKey(t.key))}</span>
      ${barI({ grams: t.grams, total: t.full_weight_g, percent: t.percent })}
      ${t.slot ? `<span class="tag ams">${esc(slotName(t.slot[0], t.slot[1]))}</span>` : ""}
      <small class="muted" title="Was Bambu über den Tag meldet – nur Info">Bambu: ${esc(t.claims || "–")}</small>
      <button type="button" class="ghost" data-unlink-tag>lösen</button></div>`).join("")
    : `<p class="muted">Noch kein RFID-Tag zugeordnet. Jeder Tag lässt sich zuordnen – egal, was Bambu darüber meldet.</p>`;
  $("#rfidAdd").hidden = !tagItems(editing.id).length;
}
const shortKey = k => /^(UID|NFC):/.test(k) ? "UID " + k.slice(4, 12) + "…" : k.slice(0, 10) + "…";
$("#rfidList").addEventListener("click", async e => {
  if (!e.target.matches("[data-unlink-tag]")) return;
  await linkTag(e.target.closest(".rfidrow").dataset.key, null);
  editing = state.fil.find(f => f.id === editing.id); renderRfidBox();
});
$("#rfidAdd").onclick = () => openPicker({
  title: "RFID-Tag zuordnen", items: tagItems(editing.id),
  onPick: async key => { if (!key) return; await linkTag(key, editing.id); editing = state.fil.find(f => f.id === editing.id); renderRfidBox(); },
});

/* ---------- AMS live / Tags / Drucker ---------- */
let amsTimer;
async function loadAms() {
  [state.live, state.tags, state.rfid] = await Promise.all([api("/api/ams/live"), api("/api/ams/tags"), api("/api/ams/rfid")]);
  renderAms(); renderTags(); renderPrinters();
  const open = (state.rfid || []).filter(t => !t.filament_ref && !t.gone).length;
  $("#tagsOpen").textContent = open ? open : "";
  clearTimeout(amsTimer);
  if (state.tab === "bambu") amsTimer = setTimeout(() => loadAms().then(() => { state.fil.length && refreshFilQuiet(); }).catch(() => {}), 10000);
}
async function refreshFilQuiet() { state.fil = await api("/api/filaments"); if (state.tab === "filaments") renderFil(); }
function setSub(sub) {
  state.sub = sub; store.set("sub", sub);
  $$(".subnav button").forEach(b => b.classList.toggle("active", b.dataset.sub === sub));
  $$("#tab-bambu .subview").forEach(d => d.hidden = d.id !== "sub-" + sub);
}
$$(".subnav button").forEach(b => b.onclick = () => setSub(b.dataset.sub));
setSub(state.sub);

const filOpts = (sel, placeholder = "– Inventar-Spule wählen –") => {
  const groups = {};
  [...state.fil].sort((a, b) => a.material.localeCompare(b.material) || a.color_name.localeCompare(b.color_name))
    .forEach(f => (groups[f.brand] ??= []).push(f));
  return `<option value="">${placeholder}</option>` + Object.keys(groups).sort().map(b => `<optgroup label="${esc(b)}">` +
    groups[b].map(f => `<option value="${f.id}" ${sel === f.id ? "selected" : ""}>${esc(f.color_name)} · ${esc(f.material)}${f.sku ? " · " + f.sku : ""}${f.qty !== 1 ? " · " + f.qty + "×" : ""}${f.sealed ? " OVP" : ""}</option>`).join("") + `</optgroup>`).join("");
};

function renderAms() {
  const el = $("#amsLive");
  if (!state.live.length) {
    el.innerHTML = `<div class="panel empty-panel"><h3>Noch kein Drucker eingerichtet</h3>
      <p class="muted">Unter „Drucker &amp; Konto“ deinen H2S per LAN (IP + Zugangscode) oder über den Bambu-Account hinzufügen. Danach erscheinen hier die AMS-Slots live.</p>
      <button class="primary" onclick="setSub('setup')">Drucker einrichten</button></div>`;
    return;
  }
  el.innerHTML = state.live.map(p => {
    const units = {};
    p.trays.forEach(t => (units[t.ams_id] ??= []).push(t));
    const st = !p.enabled ? `<span class="pill">deaktiviert</span>` : p.connected ? `<span class="pill ok">verbunden</span>` : `<span class="pill bad">getrennt</span>`;
    const unitHtml = Object.entries(units).filter(([aid, trays]) => +aid !== 255 || trays.some(t => !t.empty)).map(([aid, trays]) => {
      aid = +aid;
      const h = trays[0];
      const title = aid === 255 ? "Externe Spule" : aid >= 128 ? `AMS HT ${aid - 127}` : `AMS ${String.fromCharCode(65 + aid)}`;
      return `<div class="ams-unit">
        <div class="ams-head"><b>${title}</b>${h.ams_humidity ? `<span title="Luftfeuchte">💧 ${esc(h.ams_humidity)}${/^\d{2,}$/.test(h.ams_humidity) ? " %" : ""}</span>` : ""}${h.ams_temp ? `<span>🌡 ${esc(h.ams_temp)} °C</span>` : ""}</div>
        <div class="slots">${trays.map(t => slotHtml(p, t)).join("")}</div></div>`;
    }).join("");
    return `<section class="printer panel">
      <div class="row-head"><h3>${esc(p.name || p.serial)}</h3>${st}<span class="muted small">${p.mode === "cloud" ? "Cloud" : "LAN " + esc(p.host || "")} · ${esc(p.serial)}${p.last_msg ? " · Daten " + fmtTime(p.last_msg) : ""}</span></div>
      ${p.error ? `<p class="err">⚠ ${esc(p.error)}</p>` : ""}
      ${unitHtml || `<p class="muted">Noch keine AMS-Daten empfangen.</p>`}
    </section>`;
  }).join("");
}
function slotHtml(p, t) {
  const label = slotName(t.ams_id, t.tray_id);
  if (t.empty) return `<div class="slot empty"><span class="slot-no">${label}</span><div class="slot-body muted">leer</div></div>`;
  const tag = t.tag, f = tag?.filament;
  const colors = f ? f.colors : (t.cols.length ? t.cols.map(c => "#" + c.replace("#", "")) : ["#" + (t.color || "888888FF")]);
  const pct = t.remain >= 0 ? t.remain : null;
  const grams = tag?.remain_g;
  let body;
  if (f) body = `<div class="slot-name">${esc(f.color_name)}</div><div class="slot-sub">${esc(f.brand)} · ${esc(f.material)}</div>
      ${pickBtn(f.id, `data-key="${esc(tag.tag_key)}" data-fid="${f.id}" title="Zuordnung ändern"`)}`;
  else if (tag) body = `<div class="slot-name warn">Tag nicht zugeordnet</div><div class="slot-sub" title="Was der Tag meldet">Tag: ${esc(t.claims || t.tray_type || "?")}</div>
      ${pickBtn(null, `data-key="${esc(tag.tag_key)}" data-fid=""`, "Spule zuordnen …")}`;
  else body = `<div class="slot-name">${esc(t.claims || t.sub_brands || t.tray_type || "unbekannt")}</div><div class="slot-sub">ohne RFID-Tag – kein Füllstand</div>`;
  return `<div class="slot ${tag && !f ? "unlinked" : ""}">
    <span class="slot-no">${label}</span>
    ${swatchHTML({ colors, pattern: colors.length > 1 ? "gradient" : "solid", material: f?.material || "" }, { hex: false, img: false })}
    <div class="slot-body">${body}
      ${pct != null ? `<div class="slot-fill">${barI({ grams, total: tag?.full_weight_g, percent: pct })}<small>${pct} %</small></div>` : ""}
      ${tag ? `<div class="slot-key mono" title="${esc(tag.tag_key)}">◉ ${esc(shortKey(tag.tag_key))}</div>` : ""}
      ${f ? `<button class="ghost slot-empty" data-empty="${esc(tag.tag_key)}" data-fid="${f.id}" title="Spule ist leer: Bestand −1 und Tag für die nächste Spule freigeben">Spule leer</button>` : ""}
    </div></div>`;
}
$("#amsLive").addEventListener("click", async e => {
  const b = e.target.closest("[data-empty]"); if (!b) return;
  const f = state.fil.find(x => x.id === +b.dataset.fid);
  if (!confirm(`„${f ? f.color_name + " · " + f.material : "Spule"}“ ist leer?\n\nBestand wird um 1 reduziert und der Tag freigegeben, damit du ihn der nächsten Spule zuordnen kannst.`)) return;
  await api(`/api/ams/rfid/${encodeURIComponent(b.dataset.empty)}/empty`, { method: "POST" });
  await refreshAll(); toast("Bestand −1, Tag ist frei");
});
$("#amsLive").addEventListener("change", e => { if (e.target.matches(".slotlink")) linkTag(e.target.dataset.key, e.target.value ? +e.target.value : null); });
$("#amsRefresh").onclick = async () => { await api("/api/ams/refresh", { method: "POST" }); setTimeout(loadAms, 1500); toast("Daten vom Drucker angefordert"); };

function renderTags() {
  const terms = ($("#tq").value || "").toLowerCase().split(/\s+/).filter(Boolean);
  const flt = $("#tFilter").value;
  const all = state.rfid || [];
  const list = all.filter(t => {
    if (flt === "unlinked" && t.filament_ref) return false;
    if (flt === "ams" && !t.in_ams) return false;
    const f = t.filament;
    const hay = `${t.key} ${t.tag_uid || ""} ${t.claims} ${f ? f.color_name + " " + f.material + " " + f.brand : ""}`.toLowerCase();
    return terms.every(x => hay.includes(x));
  });
  $("#tBody").innerHTML = list.map(t => {
    const f = t.filament;
    const colors = f ? f.colors : ["#" + (t.claim_color || "888888FF")];
    const src = t.sources.map(x => `<span class="tag ${x === "ams" ? "ams" : ""}">${x === "ams" ? "AMS" : "Cloud"}</span>`).join(" ");
    return `<tr data-key="${esc(t.key)}" class="${t.gone ? "gone" : ""}">
      <td>${swatchHTML({ colors, material: f?.material || "" }, { hex: false, img: false })}</td>
      <td class="mono"><span title="${esc(t.key)}">${esc(shortKey(t.key))}</span><small>${src}</small></td>
      <td class="claim"><span class="claimdot" style="background:#${esc((t.claim_color || "888888").slice(0, 6))}"></span>${esc(t.claims || "–")}<small>laut Bambu – nur Info</small></td>
      <td>${pickBtn(t.filament_ref, `data-key="${esc(t.key)}" data-fid="${t.filament_ref || ""}"`)}</td>
      <td><input class="fullw" type="number" min="0" step="50" value="${t.full_weight_g ?? ""}" title="Netto-Gewicht der vollen Spule (z. B. 1000 bei Sunlu 1 kg)"> <small>g netto</small></td>
      <td>${barI({ grams: t.grams, total: t.full_weight_g, percent: t.percent })}</td>
      <td class="num">${t.consumed_g ? t.consumed_g + " g" : "–"}</td>
      <td>${t.slot ? `<span class="tag ams">${esc(slotName(t.slot[0], t.slot[1]))}</span>` : ""}<small>${fmtTime(t.last_seen)}${t.printer ? " · " + esc(t.printer) : ""}</small></td>
      <td class="acts">${t.sources.includes("ams") ? `<button class="ghost" data-del title="Tag vergessen">✕</button>` : ""}</td>
    </tr>`;
  }).join("");
  $("#tEmpty").hidden = all.length > 0;
}
$("#tq").oninput = renderTags; $("#tFilter").onchange = renderTags;
$("#tBody").addEventListener("change", async e => {
  const key = e.target.closest("tr").dataset.key;
  if (e.target.matches(".fullw")) { await api(`/api/ams/rfid/${encodeURIComponent(key)}`, { method: "PUT", body: JSON.stringify({ full_weight_g: e.target.value }) }); await refreshAll(); toast("Vollgewicht gespeichert"); }
});
$("#tBody").addEventListener("click", async e => {
  if (!e.target.matches("[data-del]")) return;
  const key = e.target.closest("tr").dataset.key;
  if (!confirm("Tag vergessen? Er wird beim nächsten Einlegen neu erkannt (ohne Verlinkung).")) return;
  await api(`/api/ams/tags/${encodeURIComponent(key)}`, { method: "DELETE" }); await refreshAll();
});
async function linkTag(key, fid) {
  await api(`/api/ams/rfid/${encodeURIComponent(key)}/link`, { method: "POST", body: JSON.stringify({ filament_id: fid }) });
  await refreshAll(); toast(fid ? "Tag zugeordnet" : "Zuordnung gelöst");
}

/* Drucker */
function renderPrinters() {
  $("#prList").innerHTML = state.live.length ? `<table class="tbl"><thead><tr><th>Name</th><th>Seriennummer</th><th>Verbindung</th><th>Status</th><th></th></tr></thead><tbody>` +
    state.live.map(p => `<tr data-pid="${p.id}"><td class="strong">${esc(p.name || "")}</td><td class="mono">${esc(p.serial)}</td>
      <td>${p.mode === "cloud" ? "Bambu Cloud" : "LAN · " + esc(p.host || "?")}</td>
      <td>${!p.enabled ? "deaktiviert" : p.connected ? `<span class="pill ok">verbunden</span>` : `<span class="pill bad">getrennt</span>`}${p.error ? `<small class="err">${esc(p.error)}</small>` : ""}</td>
      <td class="acts"><button class="ghost" data-edit>Bearbeiten</button></td></tr>`).join("") + `</tbody></table>`
    : `<p class="muted">Noch kein Drucker. LAN ist am robustesten: IP-Adresse und 8-stelligen Zugangscode findest du am Druckerdisplay unter Einstellungen → Netzwerk.</p>`;
}
const prDlg = $("#prDlg"), prForm = $("#prForm"); let editingP = null;
function prHint() {
  const lan = prForm.mode.value === "lan";
  $$(".lan", prForm).forEach(l => l.hidden = !lan);
  $("#prHint").textContent = lan
    ? "LAN: Verbindung direkt zum Drucker (Port 8883, TLS). Funktioniert ohne Internet. Falls abgelehnt: Zugangscode prüfen bzw. am Drucker LAN-Modus/Entwicklermodus aktivieren."
    : "Cloud: nutzt das Token aus der Bambu-Anmeldung (Kasten oben). Kein Zugangscode nötig; der Drucker muss online sein.";
}
prForm.mode.onchange = prHint;
function openPrinter(p) {
  editingP = p || null;
  $("#prTitle").textContent = p ? p.name || p.serial : "Drucker hinzufügen";
  prForm.name.value = p?.name || ""; prForm.serial.value = p?.serial || ""; prForm.mode.value = p?.mode || "lan";
  prForm.host.value = p?.host || ""; prForm.access_code.value = p ? p.access_code : ""; prForm.enabled.checked = p ? !!p.enabled : true;
  $("#prDel").hidden = !p; prHint(); prDlg.showModal();
}
$("#prAdd").onclick = () => openPrinter(null);
$("#prList").addEventListener("click", e => { if (e.target.matches("[data-edit]")) openPrinter(state.live.find(p => p.id === +e.target.closest("tr").dataset.pid)); });
prForm.onsubmit = async e => {
  e.preventDefault();
  const body = { name: prForm.name.value, serial: prForm.serial.value, mode: prForm.mode.value, host: prForm.host.value, access_code: prForm.access_code.value, enabled: prForm.enabled.checked ? 1 : 0 };
  try {
    if (editingP) await api(`/api/ams/printers/${editingP.id}`, { method: "PUT", body: JSON.stringify(body) });
    else await post("/api/ams/printers", body);
    prDlg.close(); toast("Drucker gespeichert – verbinde …"); setTimeout(loadAms, 3000); await loadAms();
  } catch (err) { toast(err.message); }
};
$("#prDel").onclick = async () => { if (!editingP || !confirm("Drucker entfernen?")) return; await api(`/api/ams/printers/${editingP.id}`, { method: "DELETE" }); prDlg.close(); await loadAms(); };
$("#prImport").onclick = async () => {
  try { const r = await post("/api/ams/printers/import-cloud"); toast(`${r.found} Drucker im Account, ${r.added} hinzugefügt`); await loadAms(); }
  catch (err) { toast(err.message); }
};

/* ---------- Such-Auswahl (Volltext) ---------- */
const norm = s => (s || "").toString().toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/ß/g, "ss").replace(/\s+\+/g, "+");
const compact = s => s.replace(/[^a-z0-9+]/g, "");
const hit = (it, t) => it._hay.includes(t) || (it._hayc ??= compact(it._hay)).includes(compact(t));
const pick = { items: [], onPick: null, idx: 0, list: [] };
function openPicker({ title, items, selected = null, clearLabel = null, onPick }) {
  pick.items = items; pick.onPick = onPick; pick.selected = selected; pick.clearLabel = clearLabel;
  $("#pickTitle").textContent = title;
  $("#pickQ").value = "";
  renderPick();
  $("#pickDlg").showModal();
  setTimeout(() => $("#pickQ").focus(), 30);
}
function renderPick() {
  const terms = norm($("#pickQ").value).split(/\s+/).filter(Boolean);
  pick.list = pick.items.filter(it => terms.every(t => hit(it, t)));
  // Treffer am Wortanfang nach vorn
  if (terms.length) pick.list.sort((a, b) => (b._hay.startsWith(terms[0]) - a._hay.startsWith(terms[0])) || 0);
  pick.idx = Math.max(0, pick.list.findIndex(it => it.value === pick.selected));
  const max = 150;
  $("#pickList").innerHTML =
    (pick.clearLabel && !terms.length ? `<li class="pk clear" data-v="">${esc(pick.clearLabel)}</li>` : "") +
    pick.list.slice(0, max).map((it, i) => `<li class="pk ${it.value === pick.selected ? "sel" : ""} ${i === pick.idx ? "hi" : ""}" data-i="${i}">
      ${it.colors ? swatchHTML({ colors: it.colors, material: it.material || "" }, { hex: false, img: false }) : `<span class="pkicon">◉</span>`}
      <span class="pktext"><b>${it.html || esc(it.label)}</b><small>${esc(it.sub || "")}</small></span>
      ${it.badge ? `<span class="tag ${it.badgeClass || ""}">${esc(it.badge)}</span>` : ""}
    </li>`).join("") +
    (pick.list.length > max ? `<li class="pkmore muted">… ${pick.list.length - max} weitere – Suche verfeinern</li>` : "") +
    (!pick.list.length ? `<li class="pkmore muted">Keine Treffer für „${esc($("#pickQ").value)}“</li>` : "");
  $("#pickCount").textContent = `${pick.list.length} von ${pick.items.length}`;
  $("#pickList .hi")?.scrollIntoView({ block: "nearest" });
}
function choosePick(it) {
  $("#pickDlg").close();
  if (pick.onPick) pick.onPick(it ? it.value : null);
}
$("#pickQ").addEventListener("input", renderPick);
$("#pickQ").addEventListener("keydown", e => {
  const n = Math.min(pick.list.length, 150);
  if (e.key === "ArrowDown") { e.preventDefault(); pick.idx = Math.min(n - 1, pick.idx + 1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); pick.idx = Math.max(0, pick.idx - 1); }
  else if (e.key === "Enter") { e.preventDefault(); if (pick.list[pick.idx]) choosePick(pick.list[pick.idx]); return; }
  else return;
  $$("#pickList .pk[data-i]").forEach(li => li.classList.toggle("hi", +li.dataset.i === pick.idx));
  $("#pickList .hi")?.scrollIntoView({ block: "nearest" });
});
$("#pickList").addEventListener("click", e => {
  const li = e.target.closest(".pk"); if (!li) return;
  if (li.classList.contains("clear")) return choosePick(null);
  choosePick(pick.list[+li.dataset.i]);
});

/* Inventar-Spulen als Such-Einträge */
function filItems() {
  return [...state.fil].sort((a, b) => a.brand.localeCompare(b.brand) || a.material.localeCompare(b.material) || a.color_name.localeCompare(b.color_name))
    .map(f => {
      const tags = rfidOf(f).length;
      return {
        value: f.id, colors: f.colors, material: f.material,
        label: `${f.color_name}`, html: `${esc(f.color_name)} <span class="muted">· ${esc(f.material)}</span>`,
        sub: [f.brand, f.sku, f.qty + "×" + (f.sealed ? " OVP" : "") + (f.refill ? " Refill" : ""), f.location && "📍 " + f.location, f.official_name && f.official_name !== f.color_name && f.official_name].filter(Boolean).join(" · "),
        badge: tags ? `◉ ${tags}` : "", badgeClass: "rfid",
        _hay: norm(`${f.brand} ${f.material} ${f.color_name} ${f.official_name} ${f.sku} ${f.location} ${f.notes} ${f.colors.join(" ")} ${f.sealed ? "ovp" : ""} ${f.refill ? "refill" : ""}`),
      };
    });
}
function pickFilament(title, current, onPick) {
  openPicker({ title, items: filItems(), selected: current ?? null, clearLabel: current ? "✕ Zuordnung lösen" : null, onPick });
}
/* Tags als Such-Einträge (für den Bearbeiten-Dialog) */
function tagItems(excludeFid) {
  return (state.rfid || []).filter(t => t.filament_ref !== excludeFid && !t.gone)
    .sort((a, b) => (!!a.filament_ref - !!b.filament_ref) || (b.in_ams - a.in_ams))
    .map(t => ({
      value: t.key, colors: ["#" + (t.claim_color || "888888FF")],
      label: shortKey(t.key),
      html: `${t.slot ? `<span class="tag ams">${esc(slotName(t.slot[0], t.slot[1]))}</span> ` : ""}${esc(shortKey(t.key))}`,
      sub: `Bambu meldet: ${t.claims || "?"}${t.percent != null ? " · " + t.percent + " %" : ""}${t.filament ? " · jetzt: " + t.filament.color_name + " " + t.filament.material : " · frei"}`,
      badge: t.filament ? "umhängen" : "frei", badgeClass: t.filament ? "" : "ams",
      _hay: norm(`${t.key} ${t.tag_uid || ""} ${t.claims} ${t.slot ? slotName(t.slot[0], t.slot[1]) : ""} ${t.printer || ""} ${t.filament ? t.filament.color_name + " " + t.filament.material + " " + t.filament.brand : "frei"}`),
    }));
}
/* Auswahl-Knopf, der eine Inventar-Spule anzeigt */
function pickBtn(fid, attrs, emptyLabel = "– nicht zugeordnet –") {
  const f = fid ? state.fil.find(x => x.id === fid) : null;
  return `<button type="button" class="pickbtn ${f ? "" : "empty"}" ${attrs}>${f ? swatchHTML({ colors: f.colors }, { hex: false, img: false }) + `<span>${esc(f.color_name)} <small>${esc(f.brand)} · ${esc(f.material)}</small></span>` : `<span>${esc(emptyLabel)}</span>`}<i>🔍</i></button>`;
}
document.addEventListener("click", e => {
  const b = e.target.closest(".pickbtn"); if (!b || !(b.dataset.key || b.dataset.cid)) return;
  e.preventDefault(); e.stopPropagation();
  const cur = b.dataset.fid ? +b.dataset.fid : null;
  if (b.dataset.key) pickFilament(`Tag ${shortKey(b.dataset.key)} zuordnen`, cur, v => linkTag(b.dataset.key, v));
  else if (b.dataset.cid) pickFilament("Cloud-Spule zuordnen", cur, v => linkSpool(+b.dataset.cid, v));
}, true);

/* ---------- Material ---------- */
const KIND = { material: { body: "#matBody", q: "#mq", label: "Material" }, zubehoer: { body: "#accBody", q: "#aq", label: "Zubehör" } };
const kindOf = m => m.kind === "zubehoer" ? "zubehoer" : "material";
function safeUrl(u) {
  u = (u || "").trim();
  if (!u) return "";
  if (!/^https?:\/\//i.test(u)) u = "https://" + u.replace(/^\/+/, "");
  try { const x = new URL(u); return /^https?:$/.test(x.protocol) ? x.href : ""; } catch { return ""; }
}
const hostOf = u => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return ""; } };
function linkBtns(x) {
  const b = safeUrl(x.buy_url), m = safeUrl(x.manufacturer_url);
  return (b ? `<a class="lnk buy" href="${esc(b)}" target="_blank" rel="noopener noreferrer" title="Nachkaufen: ${esc(hostOf(b))}">🛒 <span>${esc(hostOf(b))}</span></a>` : "") +
         (m ? `<a class="lnk mfr" href="${esc(m)}" target="_blank" rel="noopener noreferrer" title="Hersteller: ${esc(hostOf(m))}">↗ <span>Hersteller</span></a>` : "");
}
function renderMat() { renderMatKind("material"); renderMatKind("zubehoer"); renderStats(); }
function renderMatKind(kind) {
  const cfg = KIND[kind];
  const terms = ($(cfg.q).value || "").toLowerCase().split(/\s+/).filter(Boolean);
  const list = state.mat.filter(m => kindOf(m) === kind)
    .filter(m => terms.every(t => `${m.category} ${m.description} ${m.thickness} ${m.location} ${m.notes} ${m.manufacturer || ""}`.toLowerCase().includes(t)))
    .sort((a, b) => (a.category || "").localeCompare(b.category || "") || a.description.localeCompare(b.description));
  $(cfg.body).innerHTML = list.map(m => `<tr class="row" data-id="${m.id}">
      <td><div class="qty"><button data-d="-1">−</button><span class="n ${m.qty === 0 ? "zero" : ""}">${m.qty}</span><button data-d="1">+</button></div></td>
      <td>${m.category ? `<span class="cat">${esc(m.category)}</span>` : ""}</td>
      <td>${esc(m.description)}${m.manufacturer ? `<small class="muted"> · ${esc(m.manufacturer)}</small>` : ""}${m.notes ? `<br><small class="muted">${esc(m.notes)}</small>` : ""}</td>
      <td>${esc(m.thickness)}</td><td>${esc(m.location)}</td>
      <td class="links">${linkBtns(m)}</td>
      <td>${m.min_qty > 0 && m.qty <= m.min_qty ? `<span class="tag low">nachkaufen</span>` : ""}</td>
    </tr>`).join("") || `<tr><td colspan="7" class="empty">${terms.length ? "Nichts gefunden." : `Noch kein ${cfg.label} erfasst.`}</td></tr>`;
}

/* ---------- Laden ---------- */
async function load() {
  [state.fil, state.mat] = await Promise.all([api("/api/filaments"), api("/api/materials")]);
  fillFilters(); renderFil(); renderMat();
  loadBambu().catch(() => {}); loadAms().catch(() => {});
  if (typeof afterLoad === "function") afterLoad();
}

/* ---------- Tabs / Toolbar ---------- */
function setTab(t) {
  state.tab = t; store.set("tab", t);
  $$(".tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === t));
  $$(".tab").forEach(s => s.hidden = s.id !== "tab-" + t);
  if (t === "bambu" && state.fil.length) { loadBambu().catch(() => {}); loadAms().catch(() => {}); }
  if (t === "overview" && typeof loadOverview === "function") loadOverview().catch(() => {});
  if (t === "printer") { loadStatus().catch(() => {}); loadJobs().catch(() => {}); if (!state.live.length) loadAms().catch(() => {}); }
}
$$(".tabs button").forEach(b => b.onclick = () => setTab(b.dataset.tab));
$$("button[data-view]").forEach(b => {
  b.classList.toggle("active", b.dataset.view === state.view);
  b.onclick = () => { state.view = b.dataset.view; store.set("view", state.view); $$("button[data-view]").forEach(x => x.classList.toggle("active", x === b)); renderFil(); };
});
$("#q").oninput = e => { state.q = e.target.value; renderFil(); };
$("#fBrand").onchange = e => { state.brand = e.target.value; renderFil(); };
$("#fSort").value = state.sort;
$("#fSort").onchange = e => { state.sort = e.target.value; store.set("sort", state.sort); renderFil(); };
$("#matChips").onclick = e => { const c = e.target.closest(".chip"); if (!c) return; state.material = c.dataset.m; store.set("material", state.material); renderFil(); };
$("#mq").oninput = () => renderMatKind("material");

async function adjust(kind, id, d, el) {
  const upd = await api(`/api/${kind}/${id}/adjust`, { method: "POST", body: JSON.stringify({ delta: d }) });
  const arr = kind === "filaments" ? state.fil : state.mat;
  Object.assign(arr.find(x => x.id === id), upd);
  kind === "filaments" ? renderFil() : renderMat();
}
$("#filGrid").onclick = e => {
  if (e.target.closest("a")) return;
  if (e.target.closest("th[data-k]")) { const k = e.target.closest("th").dataset.k; state.tsort = { key: k, dir: state.tsort.key === k ? -state.tsort.dir : 1 }; store.set("tsort", state.tsort); return renderFil(); }
  if (e.target.matches(".tsel-all")) { filtered().forEach(f => e.target.checked ? state.sel.add(f.id) : state.sel.delete(f.id)); return renderFil(); }
  const tg = e.target.closest("input.tog");
  if (tg) { const id = +tg.closest("[data-id]").dataset.id; return toggleField(id, tg.dataset.f, tg.checked); }
  const ok = e.target.closest("[data-ok]");
  if (ok) { const id = +ok.closest("[data-id]").dataset.id; return markChecked(id); }
  const card = e.target.closest(".card, tr[data-id], .ckrow"); if (!card) return;
  const id = +card.dataset.id;
  if (e.target.matches(".sel")) { e.target.checked ? state.sel.add(id) : state.sel.delete(id); renderBulk(); $("#filGrid").classList.toggle("selecting", state.sel.size > 0); return; }
  if (e.target.closest("td.nolink")) return;
  const b = e.target.closest("button[data-d]"); if (b) return adjust("filaments", id, +b.dataset.d);
  if (e.target.closest(".qty")) return;
  openFil(state.fil.find(f => f.id === id));
};
const matClick = e => {
  if (e.target.closest("a")) return;
  const row = e.target.closest("tr.row"); if (!row) return;
  const id = +row.dataset.id;
  const b = e.target.closest("button[data-d]"); if (b) return adjust("materials", id, +b.dataset.d);
  if (e.target.closest(".qty")) return;
  openMat(state.mat.find(m => m.id === id));
};
$("#matBody").onclick = matClick; $("#accBody").onclick = matClick;
$("#bulkCheck").onclick = async () => {
  const ids = [...state.sel]; const r = await api("/api/inventur", { method: "POST", body: JSON.stringify({ ids }) });
  state.fil.forEach(f => { if (state.sel.has(f.id)) f.last_check = r.date; });
  state.sel.clear(); renderFil(); toast(`${ids.length} Spulen als geprüft markiert`);
};
$("#bulkClear").onclick = () => { state.sel.clear(); renderFil(); };

/* ---------- Filament-Dialog ---------- */
const filDlg = $("#filDlg"), filForm = $("#filForm");
let editing = null, draftColors = [];

function colorRow(hex, i) {
  const a = Math.round(alphaOf(hex) * 100);
  return `<div class="cin" data-i="${i}">
    <input type="color" value="${hex.slice(0, 7)}">
    <input type="text" value="${hex.slice(0, 7).toUpperCase()}" maxlength="7" spellcheck="false">
    <input type="range" min="10" max="100" value="${a}" title="Deckkraft ${a}%">
    ${draftColors.length > 1 ? `<button type="button" title="entfernen">✕</button>` : ""}
  </div>`;
}
function renderColorInputs() { $("#colorInputs").innerHTML = draftColors.map(colorRow).join(""); updateDlgSwatch(); }
function updateDlgSwatch() {
  const item = { colors: draftColors, pattern: filForm.pattern.value, material: filForm.material.value, image: editing?.image };
  $("#dlgSwatch").outerHTML = swatchHTML(item, { hex: false }).replace('class="swatch', 'id="dlgSwatch" class="swatch big');
  $("#dlgSource").textContent = SRC_LONG[filForm.swatch_source.value] || "";
}
const toHexA = (rgb, a) => rgb.toUpperCase() + Math.round(a / 100 * 255).toString(16).padStart(2, "0").toUpperCase();
$("#colorInputs").addEventListener("input", e => {
  const row = e.target.closest(".cin"); const i = +row.dataset.i;
  const [cp, tx, rg] = $$("input", row);
  if (e.target === cp) tx.value = cp.value.toUpperCase();
  if (e.target === tx) { if (!/^#[0-9a-f]{6}$/i.test(tx.value)) return; cp.value = tx.value; }
  draftColors[i] = toHexA(cp.value, +rg.value);
  if (filForm.swatch_source.value === "bambu" && e.target !== rg) filForm.swatch_source.value = "manual";
  updateDlgSwatch();
});
$("#colorInputs").addEventListener("click", e => { if (e.target.matches("button")) { draftColors.splice(+e.target.closest(".cin").dataset.i, 1); renderColorInputs(); } });
$("#addColor").onclick = () => { draftColors.push(draftColors.at(-1) || "#888888FF"); if (filForm.pattern.value === "solid") filForm.pattern.value = "gradient"; renderColorInputs(); };
filForm.pattern.onchange = filForm.swatch_source.onchange = filForm.material.oninput = updateDlgSwatch;

function openFil(f) {
  editing = f || null;
  const d = f || { brand: "Bambu Lab", material: "", color_name: "", official_name: "", sku: "", qty: 1, min_qty: 0, sealed: true, spool: true, refill: false, sample: false,
    last_check: new Date().toISOString().slice(0, 10), location: "", notes: "", colors: ["#888888FF"], pattern: "solid", swatch_source: "manual", weight_g: null };
  $("#filDlgTitle").textContent = f ? `${f.color_name}` : "Neues Filament";
  for (const el of filForm.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") el.checked = !!d[el.name]; else el.value = d[el.name] ?? "";
  }
  draftColors = [...d.colors];
  renderColorInputs();
  $("#filDel").hidden = !f;
  $("#catQ").value = ""; $("#catRes").innerHTML = "";
  setPhoto(f?.image);
  $(".photo .file-btn").hidden = !f;
  renderRfidBox(); updLinks(filForm);
  if (typeof onOpenFil === "function") onOpenFil(f);
  filDlg.showModal();
  if (!f) $("#catQ").focus();
}
function setPhoto(name) {
  $("#photoPrev").hidden = !name; $("#photoDel").hidden = !name;
  if (name) $("#photoPrev").src = "/uploads/" + name;
}

let catTimer;
$("#catQ").oninput = e => {
  clearTimeout(catTimer);
  const q = e.target.value.trim();
  if (q.length < 2) { $("#catRes").innerHTML = ""; return; }
  catTimer = setTimeout(async () => {
    const res = /^\d{5}$/.test(q) ? await api(`/api/catalog?sku=${q}`) : await api(`/api/catalog?q=${encodeURIComponent(q)}`);
    $("#catRes").innerHTML = res.map((c, i) => `<button type="button" class="cat-item" data-i="${i}">
        ${swatchHTML({ colors: c.colors, pattern: c.pattern, material: c.type }, { hex: false, img: false })}
        <span>${esc(c.name_de || c.name_en)}<small>${esc(c.brand)} · ${esc(c.type)}${c.sku ? " · " + c.sku : ""}${c.source === "community" ? " · Community" : ""}</small></span>
      </button>`).join("") || `<p class="muted">Kein Treffer im Katalog – Farbe unten manuell setzen.</p>`;
    $("#catRes")._data = res;
  }, 180);
};
$("#catRes").onclick = e => {
  const b = e.target.closest(".cat-item"); if (!b) return;
  const c = $("#catRes")._data[+b.dataset.i];
  filForm.brand.value = c.brand; filForm.material.value = c.type; filForm.sku.value = c.sku || "";
  filForm.official_name.value = c.name_de || c.name_en;
  if (!filForm.color_name.value) filForm.color_name.value = c.name_de || c.name_en;
  filForm.pattern.value = c.pattern; filForm.swatch_source.value = c.source === "bambu" ? "bambu" : "community";
  draftColors = [...c.colors]; renderColorInputs();
  $("#catRes").innerHTML = ""; $("#catQ").value = "";
  toast("Farbmuster aus Katalog übernommen");
};

filForm.onsubmit = async e => {
  e.preventDefault();
  const body = { colors: draftColors };
  for (const el of filForm.elements) {
    if (!el.name) continue;
    body[el.name] = el.type === "checkbox" ? el.checked : el.value;
  }
  if (body.last_check === "") body.last_check = null;
  body.buy_url = safeUrl(body.buy_url); body.manufacturer_url = safeUrl(body.manufacturer_url);
  if (typeof beforeSaveFil === "function") beforeSaveFil(body);
  if (editing) Object.assign(editing, await api(`/api/filaments/${editing.id}`, { method: "PUT", body: JSON.stringify(body) }));
  else state.fil.push(await api("/api/filaments", { method: "POST", body: JSON.stringify(body) }));
  filDlg.close(); fillFilters(); renderFil(); toast("Gespeichert");
};
$("#filDel").onclick = async () => {
  if (!editing || !confirm(`„${editing.color_name}“ (${editing.material}) löschen?`)) return;
  await api(`/api/filaments/${editing.id}`, { method: "DELETE" });
  state.fil = state.fil.filter(f => f !== editing); filDlg.close(); renderFil(); toast("Gelöscht");
};
$("#photoFile").onchange = async e => {
  const file = e.target.files[0]; if (!file || !editing) return;
  const fd = new FormData(); fd.append("file", file);
  const r = await api(`/api/filaments/${editing.id}/image`, { method: "POST", body: fd });
  editing.image = r.image; setPhoto(r.image); updateDlgSwatch(); renderFil(); e.target.value = "";
};
$("#photoDel").onclick = async () => {
  await api(`/api/filaments/${editing.id}/image`, { method: "DELETE" });
  editing.image = null; setPhoto(null); updateDlgSwatch(); renderFil();
};
$("#addFil").onclick = () => openFil(null);

/* ---------- Material-Dialog ---------- */
const matDlg = $("#matDlg"), matForm = $("#matForm"); let editingM = null;
function openMat(m, kind) {
  editingM = m || null;
  const d = m || { qty: 1, min_qty: 0, category: "", description: "", thickness: "", location: "", notes: "", kind: kind || "material", manufacturer: "", manufacturer_url: "", buy_url: "" };
  $("#matDlgTitle").textContent = m ? m.description : (d.kind === "zubehoer" ? "Neues Zubehör" : "Neues Material");
  for (const el of matForm.elements) if (el.name) el.value = el.name === "kind" ? kindOf(d) : (d[el.name] ?? "");
  updLinks(matForm);
  $("#matDel").hidden = !m; matDlg.showModal();
}
matForm.onsubmit = async e => {
  e.preventDefault();
  const body = Object.fromEntries([...matForm.elements].filter(el => el.name).map(el => [el.name, el.value]));
  body.buy_url = safeUrl(body.buy_url); body.manufacturer_url = safeUrl(body.manufacturer_url);
  if (editingM) Object.assign(editingM, await api(`/api/materials/${editingM.id}`, { method: "PUT", body: JSON.stringify(body) }));
  else state.mat.push(await api("/api/materials", { method: "POST", body: JSON.stringify(body) }));
  matDlg.close(); fillFilters(); renderMat(); toast("Gespeichert");
};
$("#matDel").onclick = async () => {
  if (!editingM || !confirm(`„${editingM.description}“ löschen?`)) return;
  await api(`/api/materials/${editingM.id}`, { method: "DELETE" });
  state.mat = state.mat.filter(m => m !== editingM); matDlg.close(); renderMat(); toast("Gelöscht");
};
$("#addMat").onclick = () => openMat(null, "material");
$("#addAcc").onclick = () => openMat(null, "zubehoer");
$("#aq").oninput = () => renderMatKind("zubehoer");
/* Link-Felder: ↗ zum Testen des eingegebenen Links */
function updLinks(form) {
  $$(".urlrow", form).forEach(r => { const u = safeUrl($("input", r).value), a = $("a", r); a.hidden = !u; if (u) a.href = u; });
}
document.addEventListener("input", e => { if (e.target.closest(".urlrow")) updLinks(e.target.closest("form")); });

$$("[data-close]").forEach(b => b.onclick = () => b.closest("dialog").close());

/* ---------- Import ---------- */
$("#importFile").onchange = async e => {
  const file = e.target.files[0]; if (!file) return;
  const data = JSON.parse(await file.text());
  const replace = confirm("Bestehende Daten ERSETZEN?\nOK = ersetzen, Abbrechen = hinzufügen");
  await api("/api/import" + (replace ? "?replace=1" : ""), { method: "POST", body: JSON.stringify(data) });
  e.target.value = ""; $(".menu").open = false; await load(); toast("Backup eingespielt");
};


/* ---------- Druckerstatus ---------- */
let statusTimer, statusTick = 0, lastActive = null;
const fmtDur = m => m == null ? "–" : m < 60 ? `${m} min` : `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")} h`;
const fmtClock = iso => iso ? new Date(iso).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }) : "";
const sameDay = iso => iso && new Date(iso).toDateString() === new Date().toDateString();
async function loadStatus() {
  try { state.pstatus = await api("/api/ams/status"); } catch { state.pstatus = []; }
  if (state.tab === "printer") {
    try { state.live = await api("/api/ams/live"); } catch {}
    statusTick = (statusTick + 1) % 6;
    const anyActive = (state.pstatus || []).some(p => p.status?.active);
    if (statusTick === 0 || anyActive !== lastActive) loadJobs().catch(() => {});
    lastActive = anyActive;
  }
  renderHeaderStatus();
  if (state.tab === "printer") renderPrinterTab();
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => loadStatus(), state.tab === "printer" ? 5000 : 20000);
}
async function loadJobs() { state.jobs = await api("/api/ams/jobs?limit=30"); renderJobs(); }
function stateClass(st) {
  if (!st) return "off";
  return { RUNNING: "run", PREPARE: "run", PAUSE: "pause", FAILED: "bad", FINISH: "done" }[st.state] || "idle";
}
function renderHeaderStatus() {
  const el = $("#pstat"), list = state.pstatus || [];
  el.hidden = !list.length;
  if (!list.length) return;
  el.innerHTML = list.map(p => {
    const st = p.status, cls = stateClass(st);
    const warn = st?.messages?.some(m => m.severity !== "info");
    const txt = !st ? (p.connected ? "verbunden" : "offline") : st.active ? `${st.state_de} ${st.percent ?? 0} % · ${fmtDur(st.remaining_min)}` : st.state_de;
    return `<span class="ps ${cls}"><i></i><b>${esc(p.name || p.serial)}</b> ${esc(txt)}${warn ? ` <span class="warn">⚠</span>` : ""}${st?.active ? `<span class="psbar"><em style="width:${st.percent || 0}%"></em></span>` : ""}</span>`;
  }).join("");
}
$("#pstat").onclick = () => setTab("printer");

function ring(pct, cls) {
  const r = 52, c = 2 * Math.PI * r, off = c * (1 - (pct || 0) / 100);
  return `<svg class="ring ${cls}" viewBox="0 0 120 120" aria-hidden="true">
    <circle cx="60" cy="60" r="${r}" class="track"/><circle cx="60" cy="60" r="${r}" class="val" stroke-dasharray="${c}" stroke-dashoffset="${off}"/>
    <text x="60" y="58" class="pct">${pct ?? 0}<tspan class="unit">%</tspan></text></svg>`;
}
function renderPrinterTab() {
  const el = $("#prStatus"), list = state.pstatus || [];
  if (!list.length) {
    el.innerHTML = `<div class="panel empty-panel"><h3>Kein Drucker eingerichtet</h3>
      <p class="muted">Unter „Bambu &amp; AMS → Drucker &amp; Konto“ den Drucker per LAN oder Bambu-Account verbinden.</p>
      <button class="primary" onclick="setTab('bambu'); setSub('setup')">Drucker einrichten</button></div>`;
    return;
  }
  el.innerHTML = list.map(p => {
    const st = p.status, cls = stateClass(st);
    if (!st) return `<section class="panel pcard off"><div class="row-head"><h3>${esc(p.name || p.serial)}</h3><span class="pill ${p.connected ? "" : "bad"}">${p.connected ? "verbunden" : "offline"}</span></div>
      <p class="muted">${p.error ? "⚠ " + esc(p.error) : p.connected ? "Verbunden – warte auf Statusdaten vom Drucker …" : "Keine Verbindung zum Drucker – Netzwerk/Zugangsdaten prüfen."}</p></section>`;
    const tn = st.tray_now, f = tn?.filament;
    const live = (state.live || []).find(x => x.id === p.id);
    const slots = live ? live.trays.filter(t => t.ams_id !== 255 || !t.empty) : [];
    const fil = tn ? `<div class="pfil">
        ${swatchHTML({ colors: f ? f.colors : ["#" + (tn.color || "888888FF")], material: f?.material || "" }, { hex: false, img: false })}
        <div><small>Aktives Filament · ${esc(slotName(tn.ams_id, tn.tray_id ?? 0))}</small>
          <b>${f ? esc(f.color_name) : esc(tn.claims || tn.tray_type || "unbekannt")}</b>
          <span class="muted">${f ? esc(f.brand + " · " + f.material) : tn.tag ? "Tag nicht zugeordnet" : "ohne Tag"}${tn.tag?.remain_g != null ? ` · noch ${tn.tag.remain_g} g` : tn.remain >= 0 ? ` · ${tn.remain} %` : ""}</span></div></div>` : "";
    const job = st.active || ["FINISH", "FAILED"].includes(st.state) ? `
      <div class="pjob">${ring(st.percent, cls)}
        <div class="pjob-info">
          <div class="pjob-name" title="${esc(st.job || "")}">${esc(st.job || "Druckauftrag")}</div>
          <div class="pjob-stage">${esc(st.stage || st.state_de)}</div>
          <dl>
            <div><dt>Schicht</dt><dd>${st.layer ?? "–"} / ${st.layers ?? "–"}</dd></div>
            <div><dt>Restzeit</dt><dd>${st.active ? fmtDur(st.remaining_min) : "–"}</dd></div>
            <div><dt>Fertig ca.</dt><dd>${st.active && st.eta ? fmtClock(st.eta) + (sameDay(st.eta) ? "" : " (+1)") : "–"}</dd></div>
            ${p.job?.started ? `<div><dt>Gestartet</dt><dd>${fmtClock(p.job.started)}</dd></div>` : ""}
          </dl>
        </div></div>` : `<div class="pjob idle"><div class="pidle">${esc(st.state_de)}</div><p class="muted">Kein aktiver Druck.</p></div>`;
    const temps = st.temps.map(t => `<div class="ptemp ${t.target && t.cur != null && Math.abs(t.target - t.cur) > 3 ? "heating" : ""}">
        <small>${t.label}</small><b>${t.cur ?? "–"}<span>°C</span></b><em>${t.target ? "Soll " + t.target + " °C" : "aus"}</em></div>`).join("");
    const info = [
      st.speed && ["Geschwindigkeit", `${st.speed}${st.speed_pct && st.speed_pct !== 100 ? " (" + st.speed_pct + " %)" : ""}`],
      ...st.fans.map(x => ["Lüfter " + x.label, x.pct + " %"]),
      st.nozzle && ["Düse", st.nozzle], st.light && ["Kammerlicht", st.light === "on" ? "an" : st.light === "off" ? "aus" : st.light],
      st.wifi && ["WLAN", st.wifi],
    ].filter(Boolean).map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("");
    const msgs = st.messages.length ? `<div class="pmsgs">${st.messages.map(m => `<div class="pmsg ${m.severity}"><b>${{ fatal: "Kritisch", serious: "Fehler", common: "Hinweis", info: "Info" }[m.severity] || "Info"}</b> ${esc(m.text)} <code>${esc(m.code)}</code></div>`).join("")}</div>` : "";
    const amsRow = slots.length ? `<div class="pams">${slots.map(t => {
        const lf = t.tag?.filament, act = tn && tn.ams_id === t.ams_id && (tn.tray_id == null || tn.tray_id === t.tray_id);
        const col = lf ? lf.colors : t.cols.length ? t.cols.map(c => "#" + c) : ["#" + (t.color || "00000000")];
        return `<div class="pslot ${t.empty ? "empty" : ""} ${act ? "act" : ""}" title="${esc(lf ? lf.color_name + " · " + lf.material : t.claims || t.tray_type || "leer")}">
          ${t.empty ? `<span class="sw0"></span>` : swatchHTML({ colors: col }, { hex: false, img: false })}
          <small>${slotName(t.ams_id, t.tray_id)}</small><em>${t.empty ? "leer" : t.remain >= 0 ? t.remain + " %" : "–"}</em></div>`;
      }).join("")}</div>` : "";
    return `<section class="panel pcard ${cls}">
      <div class="row-head"><h3>${esc(p.name || p.serial)}</h3><span class="pill ${cls}">${esc(st.state_de)}</span>
        <span class="muted small">${p.mode === "cloud" ? "Cloud" : "LAN"} · ${esc(p.serial)}${p.last_msg ? " · aktualisiert " + fmtClock(p.last_msg) : ""}</span></div>
      ${msgs}
      <div class="pgrid">
        ${job}
        <div class="pside">
          <div class="ptemps">${temps}</div>
          ${fil}
          ${amsRow}
          <dl class="pinfo">${info}</dl>
        </div>
      </div>
    </section>`;
  }).join("");
}
function renderJobs() {
  const jobs = state.jobs || [];
  $("#jobBody").innerHTML = jobs.length ? jobs.map(j => `<tr>
      <td class="strong">${esc(j.name || "–")}</td><td>${esc(j.printer || "")}</td>
      <td>${fmtTime(j.started)}</td><td>${j.ended ? fmtDur(j.minutes) : `<span class="pill run">läuft</span>`}</td>
      <td>${j.result ? `<span class="pill ${{ fertig: "done", fehlgeschlagen: "bad", abgebrochen: "pause" }[j.result] || ""}">${esc(j.result)}</span>` : "–"}</td>
      <td>${j.usage.length ? j.usage.map((u, i) => typeof usageChip === "function" ? usageChip(j, u, i) : "").join("") : `<span class="muted">${j.ended ? "–" : ""}</span>`}${j.usage_src === "slicer" ? `<small class="srcnote">Slicer-Gramm</small>` : j.usage.length ? `<small class="srcnote">AMS-Schätzung</small>` : ""}</td>
      <td class="num">${j.cost ? `<span title="Material ${j.cost.material} · Strom ${j.cost.energy}${j.cost.grams_without_price ? ` · ${j.cost.grams_without_price} g ohne Preis` : ""}">${fmtMoney(j.cost.total)}</span>` : "–"}</td>
    </tr>`).join("") : `<tr><td colspan="7" class="muted">Noch keine Drucke aufgezeichnet – die Historie beginnt mit dem ersten Druck nach der Einrichtung.</td></tr>`;
}
loadStatus();


/* ---------- Inventur-Modus ---------- */
const today = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
function renderCheck(list) {
  const t = today(), done = list.filter(f => f.last_check === t).length;
  const grid = $("#filGrid");
  grid.className = "checkmode";
  grid.innerHTML = `<div class="ckhead"><div><b>${done}</b> von ${list.length} heute geprüft</div>
      <span class="ckbar"><i style="width:${list.length ? done / list.length * 100 : 0}%"></i></span>
      <label class="ckopen"><input type="checkbox" id="ckOpen" ${state.ckOpen ? "checked" : ""}> nur offene</label></div>` +
    list.filter(f => !state.ckOpen || f.last_check !== t).map(f => {
      const ok = f.last_check === t, sp = rfidOf(f), g = sp.filter(x => x.grams != null).reduce((a, x) => a + x.grams, 0);
      return `<div class="ckrow ${ok ? "ok" : ""}" data-id="${f.id}">
        ${swatchHTML(f, { hex: false })}
        <div class="ckinfo"><b>${esc(f.color_name)}</b><small>${esc(f.material)} · ${esc(f.brand)}${f.sku ? " · " + esc(f.sku) : ""}</small>
          <small>${[f.location && "📍 " + esc(f.location), f.sealed && "OVP", f.refill && "Refill", sp.length && `◉ ${g} g`].filter(Boolean).join(" · ")}</small></div>
        <div class="qty"><button data-d="-1" aria-label="weniger">−</button><span class="n ${f.qty === 0 ? "zero" : ""}">${f.qty}</span><button data-d="1" aria-label="mehr">+</button></div>
        <button class="ckbtn ${ok ? "on" : ""}" data-ok aria-label="geprüft">${ok ? "✓" : "○"}</button>
      </div>`;
    }).join("");
}
async function markChecked(id) {
  const f = state.fil.find(x => x.id === id);
  let d;
  if (f.last_check === today()) d = f._prevCheck ?? null;
  else { f._prevCheck = f.last_check; d = today(); }
  const upd = await api(`/api/filaments/${id}`, { method: "PATCH", body: JSON.stringify({ last_check: d }) });
  Object.assign(f, upd);
  if (navigator.vibrate) try { navigator.vibrate(15); } catch {}
  renderFil();
}
document.addEventListener("change", e => { if (e.target.id === "ckOpen") { state.ckOpen = e.target.checked; renderFil(); } });

/* ---------- Tabellen → Karten (Labels aus dem Tabellenkopf) ---------- */
function labelize(root = document) {
  root.querySelectorAll("table.tbl, table.mat").forEach(tb => {
    const heads = [...tb.querySelectorAll("thead th")].map(th => th.textContent.replace(/[▲▼]/g, "").trim());
    tb.querySelectorAll("tbody tr").forEach(tr => [...tr.children].forEach((td, i) => { if (td.dataset.label === undefined) td.dataset.label = heads[i] || ""; }));
  });
}
let lblTimer;
new MutationObserver(() => { clearTimeout(lblTimer); lblTimer = setTimeout(labelize, 30); }).observe(document.querySelector("main"), { childList: true, subtree: true });

/* ---------- PWA ---------- */
if ("serviceWorker" in navigator && window.isSecureContext) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
}

/* ---------- Theme ---------- */
function applyTheme(t) {
  document.documentElement.dataset.theme = t; store.set("theme", t);
  const dark = t === "dark" || (t === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  $("#themeBtn").textContent = dark ? "☀" : "☾";
  $("#themeBtn").title = dark ? "Hell anzeigen" : "Dunkel anzeigen";
  $$("[data-theme-set]").forEach(b => b.classList.toggle("active", b.dataset.themeSet === t));
  $$('meta[name="theme-color"]').forEach(m => m.content = dark ? "#131210" : "#f4f2ee");
}
$("#themeBtn").onclick = () => {
  const dark = document.documentElement.dataset.theme === "dark" ||
    (document.documentElement.dataset.theme === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  applyTheme(dark ? "light" : "dark");
};
$$("[data-theme-set]").forEach(b => b.onclick = () => applyTheme(b.dataset.themeSet));
applyTheme(document.documentElement.dataset.theme || "dark");

{ // Start-Parameter (PWA-Kurzbefehle): ?tab=printer&view=check
  const q = new URLSearchParams(location.search);
  if (q.get("tab")) state.tab = q.get("tab");
  if (q.get("unit")) state.openUnit = +q.get("unit");
  if (q.get("fil")) state.openFil = +q.get("fil");
  if (q.get("unit") || q.get("fil")) { state.tab = "filaments"; history.replaceState(null, "", "/"); }
  if (["grid", "table", "check"].includes(q.get("view"))) {
    state.view = q.get("view");
    $$("button[data-view]").forEach(x => x.classList.toggle("active", x.dataset.view === state.view));
  }
}
setTab(state.tab);
load().catch(err => { $("#stats").textContent = "Fehler beim Laden: " + err.message; });
