"use strict";
/* 3D Manager – Erweiterungen: Spulen, Profile, Kosten, Übersicht, Etiketten, Scanner, Einstellungen.
   Läuft nach app.js und nutzt dessen globale Helfer ($, $$, esc, api, state, swatchHTML, toast …). */

state.settings = { currency: "€" };
const fmtMoney = v => v == null ? "–" : `${Number(v).toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${state.settings.currency || "€"}`;
const fmtG = g => g == null ? "–" : g >= 1000 ? `${(g / 1000).toLocaleString("de-DE", { maximumFractionDigits: 2 })} kg` : `${Math.round(g)} g`;
const fmtDay = d => d ? new Date(d.length > 10 ? d : d + "T00:00").toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "2-digit" }) : "–";
const daysSince = d => d ? Math.floor((Date.now() - new Date(d.slice(0, 10) + "T00:00").getTime()) / 864e5) : null;
const STATUS = { sealed: "OVP", open: "offen", empty: "leer" };

async function afterLoad() {
  try { state.settings = await api("/api/settings"); } catch {}
  try { state.profiles = await api("/api/profiles"); } catch { state.profiles = []; }
  try { state.drying = await api("/api/drying"); } catch { state.drying = { spools: [], ams: [] }; }
  renderFil();
  if (state.openUnit) {
    const f = state.fil.find(x => (x.units || []).some(u => u.id === state.openUnit));
    if (f) { setTab("filaments"); openFil(f); setTimeout(() => $(`.unitrow[data-uid="${state.openUnit}"]`)?.scrollIntoView({ block: "center" }), 150); }
    else toast("Spule nicht gefunden");
    state.openUnit = null;
  }
  if (state.openFil) { const f = state.fil.find(x => x.id === state.openFil); if (f) openFil(f); state.openFil = null; }
}

/* ---------- Materialfamilie (wie im Backend) ---------- */
function familyOf(material) {
  const toks = (material || "").toUpperCase().split(/[^A-Z0-9]+/).filter(Boolean);
  if (toks.includes("SUPPORT")) return "SUPPORT";
  for (const t of toks) {
    if (t.startsWith("PVA") || t === "BVOH") return "PVA";
    if (t.startsWith("TPU") || t.startsWith("TPE")) return "TPU";
    if (t.startsWith("PETG") || t.startsWith("PCTG") || t === "PET") return "PETG";
    if (t.startsWith("ASA")) return "ASA";
    if (t.startsWith("ABS")) return "ABS";
    if (t.startsWith("PLA")) return "PLA";
    if (t.startsWith("PA") || t.startsWith("PPA") || t === "NYLON") return "PA";
    if (t.startsWith("PC")) return "PC";
  }
  return "SONSTIGE";
}
const famProfile = material => (state.profiles || []).find(p => p.family === familyOf(material)) || {};
const dryInfo = uid => (state.drying?.spools || []).find(d => d.unit_id === uid);

/* ---------- Kachel / Tabelle ---------- */
function unitSummary(f) {
  const us = (f.units || []).filter(u => u.status !== "empty");
  const sealed = us.filter(u => u.status === "sealed").length, open = us.filter(u => u.status === "open").length;
  const rem = us.reduce((a, u) => a + (u.remaining_g || 0), 0);
  const openRem = us.filter(u => u.status === "open").reduce((a, u) => a + (u.remaining_g || 0), 0);
  const dry = us.map(u => dryInfo(u.id)).filter(Boolean);
  return { us, sealed, open, rem, openRem, dry };
}
function unitTags(f) {
  const s = unitSummary(f);
  if (!s.us.length) return "";
  const rf = s.us.filter(u => u.refill).length;
  const parts = [s.sealed && `${s.sealed}× OVP`, s.open && `${s.open}× offen · ${fmtG(s.openRem)}`,
    rf && rf < s.us.length && `${rf}× Refill`].filter(Boolean);
  return `<span class="tag" title="Restgewicht gesamt ${fmtG(s.rem)}">${esc(parts.join(" · "))}</span>` +
    (s.dry.length ? `<span class="tag ${s.dry.some(d => d.state === "due") ? "low" : "warn"}" title="${esc(s.dry.map(d => `${d.code}: ${d.days} Tage offen`).join("\n"))}">💧 trocknen</span>` : "") +
    (f.ordered_at ? `<span class="tag ams" title="bestellt am ${fmtDay(f.ordered_at)}">bestellt</span>` : "");
}
function unitCell(f) {
  const s = unitSummary(f);
  const ams = rfidOf(f).filter(x => x.loc).map(x => `<b class="amsmini">${esc(x.loc.split(" · ")[0])}</b>`).join("");
  if (!s.us.length) return `<span class="muted">–</span>`;
  return `<span class="spoolpill" title="${s.sealed}× OVP, ${s.open}× offen">${barI({ grams: Math.round(s.rem), total: s.us.reduce((a, u) => a + (u.initial_g || 1000), 0) })}</span>${ams}${s.dry.length ? ` <span class="tag warn">💧</span>` : ""}`;
}

/* ---------- Filament-Dialog: Spulen + Profil ---------- */
function onOpenFil(f) {
  $("#unitBox").hidden = !f;
  $("#unitLabels").hidden = !f;
  renderUnits(f);
  renderProfile(f);
  delete $("#studioPick").dataset.touched;
  renderStudioBox(f);
}
function renderUnits(f) {
  if (!f) { $("#unitList").innerHTML = ""; return; }
  const us = f.units || [];
  const showEmpty = state.showEmptyUnits;
  const list = us.filter(u => showEmpty || u.status !== "empty");
  const emptyN = us.length - us.filter(u => u.status !== "empty").length;
  $("#unitList").innerHTML = (list.map(u => {
    const d = dryInfo(u.id);
    const since = u.dried_at && u.dried_at >= (u.opened_at || "") ? `getrocknet ${fmtDay(u.dried_at)}` : u.opened_at ? `offen seit ${fmtDay(u.opened_at)} (${daysSince(u.opened_at)} T.)` : u.status === "open" ? "Öffnungsdatum unbekannt" : "";
    return `<div class="unitrow ${u.status}" data-uid="${u.id}">
      <span class="ucode mono">${u.code}</span>
      <span class="upack"><span class="pill ${u.status === "open" ? "run" : u.status === "sealed" ? "done" : ""}">${STATUS[u.status]}</span>${u.status !== "empty" ? `
        <button type="button" class="chip ${u.spool ? "on" : ""}" data-up="spool" aria-pressed="${!!u.spool}" title="Mit Spule geliefert">Spule</button>
        <button type="button" class="chip ${u.refill ? "on" : ""}" data-up="refill" aria-pressed="${!!u.refill}" title="Refill (ohne Spule)">Refill</button>` : ""}</span>
      <span class="ubar">${barI({ grams: u.remaining_g != null ? Math.round(u.remaining_g) : null, total: u.initial_g, percent: u.percent })}<small>${{ ams: "AMS", weighed: "gewogen", slicer: "Slicer", job: "Druck", manual: "manuell", full: "voll" }[u.remaining_src] || ""}</small></span>
      <span class="umeta">${u.tag ? `<span class="tag ${u.tag.in_ams ? "ams" : "rfid"}" title="RFID ${esc(u.tag.key)}">◉ ${u.tag.slot ? esc(slotName(u.tag.slot[0], u.tag.slot[1])) : "Tag"}</span>` : ""}
        ${since ? `<small class="${d ? (d.state === "due" ? "err" : "warnc") : "muted"}">${esc(since)}${d ? " – trocknen empfohlen" : ""}</small>` : ""}
        ${u.price ? `<small class="muted">${fmtMoney(u.price)}</small>` : ""}</span>
      <span class="uacts">
        ${u.status === "sealed" ? `<button type="button" class="ghost" data-ua="open">öffnen</button>` : ""}
        ${u.status === "open" && (u.remaining_g == null || u.remaining_g >= (u.initial_g || 1000) - 1) ? `<button type="button" class="ghost" data-ua="reseal" title="Doch noch originalverpackt">OVP</button>` : ""}
        ${u.status !== "empty" ? `<button type="button" class="ghost" data-ua="weigh" title="Restgewicht wiegen">⚖</button>
          <button type="button" class="ghost" data-ua="dry" title="Heute getrocknet">💧</button>
          <button type="button" class="ghost" data-ua="empty" title="Spule ist leer">leer</button>` : ""}
        <button type="button" class="ghost" data-ua="label" title="Etikett drucken">⌗</button>
        <button type="button" class="ghost danger" data-ua="del" title="Spule löschen">✕</button>
      </span></div>`;
  }).join("") || `<p class="muted">Keine Spulen. „+ Spule“ legt eine OVP-Spule an.</p>`) +
    (emptyN ? `<button type="button" class="linkbtn" id="unitShowEmpty">${showEmpty ? "leere ausblenden" : `${emptyN} leere anzeigen`}</button>` : "");
}
async function reloadEditing() {
  const id = editing?.id; await refreshAll();
  if (id) { editing = state.fil.find(f => f.id === id); if (editing) { renderUnits(editing); filForm.qty.value = editing.qty; } }
}
$("#unitList").addEventListener("click", async e => {
  if (e.target.id === "unitShowEmpty") { state.showEmptyUnits = !state.showEmptyUnits; return renderUnits(editing); }
  const pk = e.target.closest("[data-up]");
  if (pk) {
    const uid = +pk.closest(".unitrow").dataset.uid, k = pk.dataset.up, u = editing.units.find(x => x.id === uid);
    const body = { [k]: !u[k] };
    await api(`/api/units/${uid}`, { method: "PATCH", body: JSON.stringify(body) });
    await reloadEditing(); return toast(`${u.code}: ${body[k] ? "" : "kein "}${k === "spool" ? "Spule" : "Refill"}`);
  }
  const b = e.target.closest("[data-ua]"); if (!b) return;
  const uid = +b.closest(".unitrow").dataset.uid, a = b.dataset.ua;
  const u = editing.units.find(x => x.id === uid);
  if (a === "weigh") return openWeigh(u, editing);
  if (a === "label") return openLabels({ units: [uid] });
  if (a === "del" && !confirm(`Spule ${u.code} löschen? (Für aufgebrauchte Spulen besser „leer“.)`)) return;
  if (a === "empty" && !confirm(`Spule ${u.code} ist leer? Ein zugeordneter RFID-Tag wird freigegeben.`)) return;
  if (a === "reseal") { await api(`/api/units/${uid}`, { method: "PATCH", body: JSON.stringify({ status: "sealed" }) }); await reloadEditing(); return toast(`${u.code} wieder OVP`); }
  if (a === "del") await api(`/api/units/${uid}`, { method: "DELETE" });
  else await api(`/api/units/${uid}/${a}`, { method: "POST", body: "{}" });
  if (a === "dry") state.drying = await api("/api/drying");
  await reloadEditing();
  toast({ open: "Spule geöffnet", empty: "Spule als leer markiert", dry: "Trocknung vermerkt", del: "Spule gelöscht" }[a] || "OK");
});
$("#unitAdd").onclick = async () => {
  if (!editing) return;
  await api("/api/units", { method: "POST", body: JSON.stringify({ filament_id: editing.id, status: "sealed" }) });
  await reloadEditing(); toast("OVP-Spule hinzugefügt");
};
$("#unitLabels").onclick = () => editing && openLabels({ units: (editing.units || []).filter(u => u.status !== "empty").map(u => u.id) });

/* Wiegen */
let weighUnit = null;
function openWeigh(u, f) {
  weighUnit = u;
  const fm = $("#weighForm");
  fm.gross_g.value = ""; fm.net_g.value = ""; fm.tare_g.value = f.tare_g || "";
  $("#weighTitle").textContent = `${u.code} wiegen – ${f.color_name}`;
  $("#weighHint").textContent = `Netto voll: ${fmtG(u.initial_g)} · aktuell gespeichert: ${fmtG(u.remaining_g)}. Die Tara (Leerspule) wird für diese Sorte gemerkt.`;
  $("#weighDlg").showModal();
}
$("#weighForm").onsubmit = async e => {
  e.preventDefault();
  const fm = e.target, body = { gross_g: fm.gross_g.value, tare_g: fm.tare_g.value, net_g: fm.net_g.value };
  if (!body.gross_g && !body.net_g) return toast("Gewicht eingeben");
  if (body.gross_g && !body.tare_g && !body.net_g) return toast("Tara (Leerspule) eingeben");
  await api(`/api/units/${weighUnit.id}/weigh`, { method: "POST", body: JSON.stringify(body) });
  $("#weighDlg").close(); await reloadEditing(); toast("Restgewicht übernommen");
};

/* Profil */
function renderProfile(f) {
  const mat = f?.material || filForm.material.value;
  const fp = famProfile(mat), own = f?.profile || {};
  $("#profFam").textContent = `– Familie: ${fp.label || familyOf(mat)}`;
  $$("[data-prof]").forEach(inp => {
    const k = inp.dataset.prof;
    inp.value = own[k] ?? "";
    inp.placeholder = fp[k] ?? "";
  });
}
filForm.material.addEventListener("input", () => { if (!editing) renderProfile(null); else renderProfile({ ...editing, material: filForm.material.value }); });
function beforeSaveFil(body) {
  const prof = {};
  $$("[data-prof]").forEach(inp => { if (inp.value !== "") prof[inp.dataset.prof] = inp.type === "number" ? +inp.value : inp.value; });
  body.profile = prof;
  if ($("#studioPick").dataset.touched) body.studio = JSON.parse($("#studioPick").dataset.val || "null");
}

/* ---------- Druckhistorie: Verbrauchs-Chips ---------- */
function usageChip(j, u, i) {
  const slot = u.slot ? slotName(u.slot[0], u.slot[1] ?? 0) : "";
  if (u.skipped) return "";
  if (u.unassigned) {
    const b = u.bambu || {};
    return `<span class="useg unassigned" title="Slot ohne zugeordnete Spule – Bambu meldet ${esc(b.type || "?")}">
      <span class="claimdot" style="background:${esc((b.color || "#888888").slice(0, 7))}"></span>${esc(slot)} <b>${u.grams} g</b>
      <button type="button" class="ghost mini" data-assign="${j.id}" data-idx="${i}">zuordnen</button></span>`;
  }
  return `<span class="useg" title="${u.source === "slicer" ? `Slicer: ${u.grams} g${u.ams_g != null ? ` · AMS: ${u.ams_g} g` : ""}` : "AMS-Schätzung"}">${u.filament ? swatchHTML({ colors: u.filament.colors }, { hex: false, img: false }) : ""}${esc(u.filament ? u.filament.color_name : u.tag_key ? shortKey(u.tag_key) : slot)} <b>${u.grams} g</b></span>`;
}
$("#jobBody").addEventListener("click", e => {
  const b = e.target.closest("[data-assign]"); if (!b) return;
  pickFilament("Verbrauch einer Spule zuordnen", null, async fid => {
    if (!fid) return;
    await api(`/api/jobs/${b.dataset.assign}/assign`, { method: "POST", body: JSON.stringify({ index: +b.dataset.idx, filament_id: fid }) });
    await loadJobs(); await refreshFilQuiet(); toast("Verbrauch abgebucht");
  });
});

/* ---------- Übersicht ---------- */
async function loadOverview() {
  const [st, shop, dry] = await Promise.all([api("/api/stats"), api("/api/shopping"), api("/api/drying")]);
  state.drying = dry;
  renderKpis(st); renderShop(shop); renderDry(dry); renderMonths(st); renderMatStats(st); renderTop(st); renderForecast(st);
}
function renderKpis(st) {
  const k = st.kpi;
  const tile = (v, l, t = "") => `<div class="kpi" title="${esc(t)}"><b>${v}</b><span>${l}</span></div>`;
  $("#ovKpi").innerHTML = [
    tile(`${k.spools_sealed + k.spools_open}`, `Spulen (${k.spools_sealed} OVP · ${k.spools_open} offen)`),
    tile(`${k.remaining_kg.toLocaleString("de-DE")} kg`, "Filament auf Lager"),
    tile(fmtMoney(k.stock_value), "Lagerwert", k.stock_value_unpriced ? `${k.stock_value_unpriced} Sorten ohne Preis` : ""),
    tile(fmtG(k.used_30d_g), "Verbrauch 30 Tage"),
    tile(fmtMoney(k.cost_30d), "Kosten 30 Tage (inkl. Strom)"),
    tile(`${k.jobs_30d}`, "Drucke 30 Tage"),
  ].join("");
}
function renderShop(items) {
  const groups = {};
  items.forEach(it => (groups[it.shop || "ohne Shop-Link"] ??= []).push(it));
  const open = items.filter(i => !i.ordered_at).length;
  $("#ovShop").innerHTML = `<div class="row-head"><h3>Einkaufsliste</h3><span class="pill ${open ? "pause" : "done"}">${open} offen</span><span class="spacer"></span>
      ${items.length ? `<button class="ghost" id="shopCopy">Liste kopieren</button>` : ""}</div>` +
    (items.length ? Object.entries(groups).map(([shop, list]) => `<div class="shopgrp"><div class="shophead">${esc(shop)}</div>${list.map(it => `
      <div class="shopitem ${it.ordered_at ? "ordered" : ""}" data-type="${it.type}" data-id="${it.id}">
        ${it.colors ? swatchHTML({ colors: it.colors }, { hex: false, img: false }) : `<span class="pkicon">▣</span>`}
        <div class="shoptext"><b>${esc(it.name)}</b><small>${esc(it.sub)}${it.sku ? " · " + esc(it.sku) : ""} – ${esc(it.reasons.join("; "))}</small></div>
        ${it.buy_url ? `<a class="lnk buy" href="${esc(safeUrl(it.buy_url))}" target="_blank" rel="noopener noreferrer">🛒</a>` : ""}
        <label class="chk inline" title="als bestellt markieren"><input type="checkbox" data-ordered ${it.ordered_at ? "checked" : ""}> ${it.ordered_at ? "bestellt " + fmtDay(it.ordered_at) : "bestellt"}</label>
      </div>`).join("")}</div>`).join("")
      : `<p class="muted">Nichts nachzukaufen. Mindestbestände setzt du im Bearbeiten-Dialog; Spulen ohne Reserve unter ${fmtG(state.settings.low_stock_g)} erscheinen automatisch.</p>`);
  $("#shopCopy")?.addEventListener("click", () => {
    const txt = Object.entries(groups).map(([shop, l]) => `${shop}:\n` + l.filter(i => !i.ordered_at).map(i => `- ${i.name} (${i.sub})${i.buy_url ? " " + safeUrl(i.buy_url) : ""}`).join("\n")).join("\n\n");
    navigator.clipboard?.writeText(txt).then(() => toast("Einkaufsliste kopiert"), () => toast("Kopieren nicht möglich"));
  });
}
$("#ovShop").addEventListener("change", async e => {
  if (!e.target.matches("[data-ordered]")) return;
  const it = e.target.closest(".shopitem");
  await api("/api/shopping/ordered", { method: "POST", body: JSON.stringify({ type: it.dataset.type, id: +it.dataset.id, ordered: e.target.checked }) });
  renderShop(await api("/api/shopping")); refreshFilQuiet();
});
function renderDry(d) {
  const sp = d.spools || [], ams = d.ams || [];
  $("#ovDry").innerHTML = `<div class="row-head"><h3>Trocknung &amp; Feuchte</h3><span class="pill ${sp.some(x => x.state === "due") || ams.length ? "bad" : sp.length ? "pause" : "done"}">${sp.length + ams.length || "alles ok"}</span></div>` +
    ams.map(a => `<div class="pmsg serious"><b>AMS-Feuchte</b> ${esc(a.printer || "")} ${esc(slotName(a.ams_id, 0).replace(/\d$/, ""))}: ${a.humidity} % (Grenze ${a.limit} %) – Trockenmittel wechseln oder AMS trocknen</div>`).join("") +
    (sp.length ? sp.map(x => `<div class="shopitem" data-uid="${x.unit_id}">
        ${swatchHTML({ colors: x.colors }, { hex: false, img: false })}
        <div class="shoptext"><b>${esc(x.name)} <span class="mono muted">${x.code}</span></b>
          <small>${esc(x.sub)} – ${x.days} Tage ${x.dried ? "seit Trocknung" : "offen"} (empfohlen max. ${x.limit})${x.dry_temp ? ` · trocknen ${x.dry_temp} °C / ${x.dry_hours} h` : ""}</small></div>
        <span class="pill ${x.state === "due" ? "bad" : "pause"}">${x.state === "due" ? "fällig" : "bald"}</span>
        <button class="ghost" data-dried>getrocknet</button></div>`).join("")
      : ams.length ? "" : `<p class="muted">Keine offene Spule über der empfohlenen Liegezeit. Grenzen je Material unter ⋯ → Einstellungen.</p>`);
}
$("#ovDry").addEventListener("click", async e => {
  if (!e.target.matches("[data-dried]")) return;
  await api(`/api/units/${e.target.closest("[data-uid]").dataset.uid}/dry`, { method: "POST", body: "{}" });
  toast("Trocknung vermerkt"); loadOverview(); refreshFilQuiet();
});
/* Charts: eine Reihe → Akzentfarbe, keine Legende; Werte als Tooltip */
function barChart(rows, valueKey, labelFn, fmt) {
  const max = Math.max(1, ...rows.map(r => r[valueKey] || 0));
  const W = 600, H = 180, pad = 22, bw = (W - pad) / rows.length;
  const bars = rows.map((r, i) => {
    const h = Math.round(((r[valueKey] || 0) / max) * (H - 30));
    const x = pad + i * bw + 3, y = H - 18 - h;
    return `<g><title>${esc(labelFn(r, true))}: ${esc(fmt(r[valueKey] || 0))}</title>
      <rect class="hit" x="${pad + i * bw}" y="0" width="${bw}" height="${H - 18}"/>
      ${h ? `<path class="bar" d="M${x},${H - 18} V${y + 4} q0,-4 4,-4 h${bw - 14} q4,0 4,4 V${H - 18} Z"/>` : ""}
      <text x="${x + (bw - 6) / 2}" y="${H - 4}" class="axl">${esc(labelFn(r))}</text></g>`;
  }).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" preserveAspectRatio="none">
    <line x1="${pad}" x2="${W}" y1="${H - 18}" y2="${H - 18}" class="axis"/>
    <text x="0" y="12" class="axl start">${esc(fmt(max))}</text>${bars}</svg>`;
}
const MONTHS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"];
function renderMonths(st) {
  const sel = state.ovMetric || "grams";
  const fmt = { grams: fmtG, cost: fmtMoney, jobs: v => `${v} Drucke`, hours: v => `${v} h` }[sel];
  $("#ovMonths").innerHTML = `<div class="row-head"><h3>Verlauf 12 Monate</h3><span class="spacer"></span>
      <div class="seg mini">${[["grams", "Gramm"], ["cost", "Kosten"], ["jobs", "Drucke"], ["hours", "Stunden"]].map(([k, l]) => `<button data-metric="${k}" class="${k === sel ? "active" : ""}">${l}</button>`).join("")}</div></div>` +
    barChart(st.months, sel, (r, long) => long ? `${MONTHS[+r.month.slice(5) - 1]} ${r.month.slice(0, 4)}` : MONTHS[+r.month.slice(5) - 1], fmt) +
    `<details class="tblview"><summary>Als Tabelle</summary><table class="tbl"><thead><tr><th>Monat</th><th>Verbrauch</th><th>Material</th><th>Drucke</th><th>Stunden</th></tr></thead><tbody>${st.months.map(m => `<tr><td>${m.month}</td><td>${fmtG(m.grams)}</td><td>${fmtMoney(m.cost)}</td><td>${m.jobs}</td><td>${m.hours}</td></tr>`).join("")}</tbody></table></details>`;
  state.lastStats = st;
}
$("#ovMonths").addEventListener("click", e => { const b = e.target.closest("[data-metric]"); if (b) { state.ovMetric = b.dataset.metric; renderMonths(state.lastStats); } });
function hbars(rows, labelFn, valueFn, fmt, swatch) {
  const max = Math.max(1, ...rows.map(valueFn));
  return `<div class="hbars">${rows.map(r => `<div class="hbar" title="${esc(labelFn(r))}: ${esc(fmt(valueFn(r)))}">
    <span class="hl">${swatch ? swatchHTML({ colors: r.colors }, { hex: false, img: false }) : ""}${esc(labelFn(r))}</span>
    <span class="ht"><i style="width:${(valueFn(r) / max) * 100}%"></i></span><span class="hv">${esc(fmt(valueFn(r)))}</span></div>`).join("")}</div>`;
}
function renderMatStats(st) {
  $("#ovMat").innerHTML = `<div class="row-head"><h3>Verbrauch nach Material</h3></div>` +
    (st.by_material.length ? hbars(st.by_material, r => r.material, r => r.grams, fmtG) : `<p class="muted">Noch kein Verbrauch erfasst.</p>`);
}
function renderTop(st) {
  $("#ovTop").innerHTML = `<div class="row-head"><h3>Meistgenutzte Farben</h3></div>` +
    (st.top.length ? hbars(st.top, r => `${r.name} · ${r.sub}`, r => r.grams, fmtG, true) : `<p class="muted">Noch kein Verbrauch erfasst.</p>`);
}
function renderForecast(st) {
  $("#ovForecast").innerHTML = `<div class="row-head"><h3>Reicht noch …</h3><span class="muted small">Basis: Verbrauch der letzten 90 Tage</span></div>` +
    (st.forecast.length ? `<div class="hbars">${st.forecast.map(r => `<div class="hbar fc">
        <span class="hl">${swatchHTML({ colors: r.colors }, { hex: false, img: false })}${esc(r.name)} <small class="muted">${esc(r.sub)}</small></span>
        <span class="hv">${fmtG(r.remaining_g)} · ${r.per_week} g/Woche</span>
        <span class="pill ${r.days_left != null && r.days_left < 21 ? "bad" : r.days_left < 60 ? "pause" : "done"}">${r.days_left == null ? "–" : r.days_left > 365 ? "> 1 Jahr" : `~${r.days_left} Tage`}</span></div>`).join("")}</div>`
      : `<p class="muted">Sobald gedruckt wurde, steht hier, wie lange jede Farbe noch reicht.</p>`);
}

/* ---------- Einstellungen ---------- */
async function openSettings() {
  const fm = $("#setForm");
  state.settings = await api("/api/settings");
  for (const [k, v] of Object.entries(state.settings)) if (fm[k]) fm[k].value = v;
  state.profiles = await api("/api/profiles");
  renderProfTable();
  renderBackups(await api("/api/backups"));
  studioPresets(true).then(st => {
    $("#studioAuto").checked = st.auto;
    const own = st.presets.filter(p => p.source === "eigenes").length;
    $("#studioCloudAt").textContent = st.cloud_at ? `${own} eigene Profile, geladen ${fmtTime(st.cloud_at)}.` : "Eigene Profile noch nicht geladen.";
  }).catch(() => {});
  $(".menu").open = false;
  $("#setDlg").showModal();
}
function renderProfTable() {
  const cols = [["nozzle_min", "Düse min"], ["nozzle_max", "Düse max"], ["bed_min", "Bett min"], ["bed_max", "Bett max"], ["dry_temp", "Trocknen °C"], ["dry_hours", "h"], ["open_days", "offen max. Tage"]];
  $("#profTable").innerHTML = `<table class="tbl proftbl"><thead><tr><th>Familie</th>${cols.map(c => `<th>${c[1]}</th>`).join("")}<th></th></tr></thead><tbody>` +
    state.profiles.map(p => `<tr data-fam="${p.family}"><td class="strong">${esc(p.label)}</td>${cols.map(([k]) => `<td><input type="number" data-k="${k}" value="${p[k] ?? ""}"></td>`).join("")}
      <td><button type="button" class="ghost mini" data-reset title="Richtwerte wiederherstellen">↺</button></td></tr>`).join("") + `</tbody></table>`;
}
$("#profTable").addEventListener("change", async e => {
  const tr = e.target.closest("tr[data-fam]"); if (!tr) return;
  const body = {}; $$("input[data-k]", tr).forEach(i => body[i.dataset.k] = i.value === "" ? null : +i.value);
  await api(`/api/profiles/${tr.dataset.fam}`, { method: "PUT", body: JSON.stringify(body) });
  state.profiles = await api("/api/profiles"); toast("Profil gespeichert");
});
$("#profTable").addEventListener("click", async e => {
  if (!e.target.matches("[data-reset]")) return;
  await api(`/api/profiles/${e.target.closest("tr").dataset.fam}`, { method: "DELETE" });
  state.profiles = await api("/api/profiles"); renderProfTable(); toast("Richtwerte wiederhergestellt");
});
function renderBackups(b) {
  $("#backupList").innerHTML = `<div class="row"><span class="muted small">Ziel: <code>${esc(b.dir)}</code> · letzte Sicherung: ${b.last ? fmtTime(b.last) : "noch keine"}</span><span class="spacer"></span><button type="button" class="ghost" id="backupNow">Jetzt sichern</button></div>` +
    (b.files.length ? `<ul class="bklist">${b.files.slice(0, 10).map(f => `<li><a href="/api/backups/${encodeURIComponent(f.name)}" download>${esc(f.name)}</a> <small class="muted">${fmtTime(f.date)} · ${Math.max(1, Math.round(f.size / 1024))} KB</small></li>`).join("")}</ul>` : "");
}
$("#backupList").addEventListener("click", async e => {
  if (e.target.id !== "backupNow") return;
  e.target.disabled = true;
  try { const r = await api("/api/backups", { method: "POST" }); toast(`Gesichert: ${r.name}`); } catch (err) { toast("Backup fehlgeschlagen: " + err.message); }
  renderBackups(await api("/api/backups"));
});
$("#setForm").onsubmit = async e => {
  e.preventDefault();
  const fm = e.target, body = {};
  for (const k of Object.keys(state.settings)) if (fm[k]) body[k] = fm[k].type === "number" ? (fm[k].value === "" ? null : +fm[k].value) : fm[k].value;
  state.settings = await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
  $("#setDlg").close(); toast("Einstellungen gespeichert"); renderFil();
};
$("#menuSettings").onclick = e => { e.preventDefault(); openSettings(); };

/* ---------- Etiketten ---------- */
const LABEL_FORMATS = {
  "a4-3x8": { page: "A4", cols: 3, rows: 8, w: 70, h: 37, mt: 0.5, ml: 0, gx: 0, gy: 0 },
  "a4-2x7": { page: "A4", cols: 2, rows: 7, w: 99.1, h: 38.1, mt: 15.1, ml: 4.65, gx: 2.5, gy: 0 },
  "roll-62x29": { roll: true, w: 62, h: 29 },
  "roll-50x30": { roll: true, w: 50, h: 30 },
};
let labelSel = new Set();
function labelItems(kind) {
  if (kind === "filaments") return state.fil.map(f => ({ id: f.id, f, title: f.color_name, sub: `${f.brand} · ${f.material}`, code: `F${f.id}`, hay: norm(`${f.brand} ${f.material} ${f.color_name} ${f.sku} ${f.location}`) }));
  return state.fil.flatMap(f => (f.units || []).filter(u => u.status !== "empty").map(u => ({
    id: u.id, f, u, title: f.color_name, sub: `${f.brand} · ${f.material}`, code: u.code,
    hay: norm(`${f.brand} ${f.material} ${f.color_name} ${f.sku} ${u.code} ${STATUS[u.status]} ${u.status === "sealed" ? "ovp" : ""} ${f.location}`),
  })));
}
function openLabels(pre = {}) {
  const fm = $("#labelForm");
  fm.kind.value = pre.filaments ? "filaments" : "units";
  labelSel = new Set(pre.units || pre.filaments || []);
  $("#labelQ").value = "";
  renderLabelPick();
  $(".menu").open = false;
  $("#labelDlg").showModal();
}
function renderLabelPick() {
  const kind = $("#labelForm").kind.value, terms = norm($("#labelQ").value).split(/\s+/).filter(Boolean);
  const items = labelItems(kind).filter(it => terms.every(t => it.hay.includes(t)));
  state.labelVisible = items;
  $("#labelPick").innerHTML = items.map(it => `<label class="lp ${labelSel.has(it.id) ? "on" : ""}"><input type="checkbox" data-id="${it.id}" ${labelSel.has(it.id) ? "checked" : ""}>
    ${swatchHTML({ colors: it.f.colors }, { hex: false, img: false })}<span><b>${esc(it.title)}</b><small>${esc(it.sub)} · ${esc(it.code)}${it.u ? " · " + STATUS[it.u.status] : ""}</small></span></label>`).join("") || `<p class="muted">Keine Treffer.</p>`;
  $("#labelCount").textContent = `${labelSel.size} ausgewählt`;
}
$("#labelQ").oninput = renderLabelPick;
$("#labelForm").kind.onchange = () => { labelSel.clear(); renderLabelPick(); };
$("#labelPick").addEventListener("change", e => { const id = +e.target.dataset.id; e.target.checked ? labelSel.add(id) : labelSel.delete(id); e.target.closest(".lp").classList.toggle("on", e.target.checked); $("#labelCount").textContent = `${labelSel.size} ausgewählt`; });
$("#labelAll").onclick = () => { state.labelVisible.forEach(i => labelSel.add(i.id)); renderLabelPick(); };
$("#labelNone").onclick = () => { labelSel.clear(); renderLabelPick(); };
$("#menuLabels").onclick = e => { e.preventDefault(); openLabels(); };
function qrSvg(text) {
  if (typeof qrcode !== "function") return "";
  const q = qrcode(0, "M"); q.addData(text); q.make();
  return q.createSvgTag({ cellSize: 2, margin: 0, scalable: true });
}
$("#labelForm").onsubmit = e => {
  e.preventDefault();
  const fm = e.target, fmt = LABEL_FORMATS[fm.format.value], kind = fm.kind.value;
  const items = labelItems(kind).filter(it => labelSel.has(it.id));
  if (!items.length) return toast("Nichts ausgewählt");
  const base = (state.settings.public_url || location.origin).replace(/\/+$/, "");
  const labels = items.map(it => {
    const f = it.f, p = { ...famProfile(f.material), ...(f.profile || {}) };
    const url = kind === "units" ? `${base}/s/${it.id}` : `${base}/f/${it.id}`;
    const col = (f.colors[0] || "#888888").slice(0, 7);
    const temp = p.nozzle_min ? `${p.nozzle_min}–${p.nozzle_max}\u00a0°C · Bett\u00a0${p.bed_min}–${p.bed_max}\u00a0°C` : "";
    return `<div class="lbl"><div class="qr">${qrSvg(url)}</div><div class="txt">
      <div class="nm"><span class="dot" style="background:${col}"></span>${esc(f.color_name)}</div>
      <div class="mt">${esc(f.brand)} · ${esc(f.material)}</div>
      ${temp ? `<div class="sm">${esc(temp)}</div>` : ""}
      <div class="sm">${esc(it.code)}${f.sku ? " · " + esc(f.sku) : ""}${it.u?.opened_at ? " · offen " + fmtDay(it.u.opened_at) : ""}</div></div></div>`;
  });
  const skip = fmt.roll ? 0 : Math.max(0, (+fm.skip.value || 1) - 1);
  const cells = [...Array(skip).fill(`<div class="lbl blank"></div>`), ...labels];
  const css = fmt.roll
    ? `@page{size:${fmt.w}mm ${fmt.h}mm;margin:0}.sheet{display:block}.lbl{width:${fmt.w}mm;height:${fmt.h}mm;page-break-after:always}`
    : `@page{size:A4;margin:0}.sheet{display:grid;grid-template-columns:repeat(${fmt.cols},${fmt.w}mm);grid-auto-rows:${fmt.h}mm;column-gap:${fmt.gx}mm;row-gap:${fmt.gy}mm;padding:${fmt.mt}mm 0 0 ${fmt.ml}mm}.lbl{width:${fmt.w}mm;height:${fmt.h}mm}.sheet>:nth-child(${fmt.cols * fmt.rows}n){page-break-after:always}`;
  const w = window.open("", "_blank");
  if (!w) return toast("Pop-up blockiert – bitte erlauben");
  w.document.write(`<!doctype html><html lang="de"><head><meta charset="utf-8"><title>Etiketten – 3D Manager</title><style>
    *{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#000;background:#fff}
    ${css}
    .lbl{display:flex;gap:2.2mm;align-items:center;padding:2mm 2.5mm;overflow:hidden}
    .lbl.blank{visibility:hidden}.qr{height:100%;aspect-ratio:1;flex:none}.qr svg{width:100%;height:100%;display:block}
    .txt{min-width:0;line-height:1.15}.nm{font-weight:700;font-size:10pt;display:flex;align-items:center;gap:1.5mm;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .dot{width:3.2mm;height:3.2mm;border-radius:50%;border:.2mm solid #000;flex:none}.mt{font-size:8pt}.sm{font-size:6.5pt;color:#333}
    .bar{padding:10px;font:14px system-ui;background:#eee;display:flex;gap:10px;align-items:center}@media print{.bar{display:none}}
  </style></head><body><div class="bar"><b>${labels.length} Etikett(en)</b> · Druckskalierung 100 % / „Tatsächliche Größe“, Ränder: keine <button onclick="print()">Drucken</button></div>
  <div class="sheet">${cells.join("")}</div></body></html>`);
  w.document.close();
  $("#labelDlg").close();
};

/* ---------- Scanner ---------- */
let scanStream = null, scanTimer = null, scanDetector = null, scanLast = "";
async function openScanner() {
  $("#scanLog").innerHTML = ""; scanLast = "";
  $("#scanCheck").checked = state.view === "check";
  $("#scanDlg").showModal();
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    $("#scanHint").innerHTML = "Die Kamera braucht HTTPS. Alternativ den QR-Code mit der normalen Handy-Kamera scannen – er öffnet die Spule direkt in der App.";
    return;
  }
  try {
    scanStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
  } catch (err) { $("#scanHint").textContent = "Kein Kamerazugriff: " + err.message; return; }
  const v = $("#scanVideo"); v.srcObject = scanStream; await v.play();
  if ("BarcodeDetector" in window) { try { scanDetector = new BarcodeDetector({ formats: ["qr_code"] }); } catch { scanDetector = null; } }
  if (!scanDetector && typeof jsQR !== "function") await new Promise(r => { const s = document.createElement("script"); s.src = "/static/vendor/jsQR.js"; s.onload = r; s.onerror = r; document.head.appendChild(s); });
  $("#scanHint").textContent = "Etikett vor die Kamera halten.";
  const cv = document.createElement("canvas"), ctx = cv.getContext("2d", { willReadFrequently: true });
  const tick = async () => {
    if (!scanStream) return;
    let text = null;
    try {
      if (scanDetector) { const r = await scanDetector.detect(v); text = r[0]?.rawValue; }
      else if (typeof jsQR === "function" && v.videoWidth) {
        cv.width = v.videoWidth; cv.height = v.videoHeight; ctx.drawImage(v, 0, 0);
        text = jsQR(ctx.getImageData(0, 0, cv.width, cv.height).data, cv.width, cv.height)?.data;
      }
    } catch {}
    if (text && text !== scanLast) { scanLast = text; handleScan(text); }
    scanTimer = setTimeout(tick, 250);
  };
  tick();
}
function stopScanner() {
  clearTimeout(scanTimer);
  scanStream?.getTracks().forEach(t => t.stop()); scanStream = null;
}
$("#scanDlg").addEventListener("close", stopScanner);
$("#scanBtn").onclick = openScanner;
async function handleScan(text) {
  const mu = text.match(/\/s\/(\d+)/), mf = text.match(/\/f\/(\d+)/);
  if (navigator.vibrate) try { navigator.vibrate(30); } catch {}
  if (mu) {
    const uid = +mu[1], f = state.fil.find(x => (x.units || []).some(u => u.id === uid));
    if (!f) { $("#scanLog").insertAdjacentHTML("afterbegin", `<div class="err">S${String(uid).padStart(4, "0")} nicht gefunden</div>`); return; }
    if ($("#scanCheck").checked) {
      if (f.last_check !== today()) await api(`/api/filaments/${f.id}`, { method: "PATCH", body: JSON.stringify({ last_check: today() }) });
      f.last_check = today();
      $("#scanLog").insertAdjacentHTML("afterbegin", `<div class="okc">✓ S${String(uid).padStart(4, "0")} · ${esc(f.color_name)} · ${esc(f.material)}</div>`);
      renderFil(); return;
    }
    $("#scanDlg").close(); setTab("filaments"); openFil(f);
    setTimeout(() => $(`.unitrow[data-uid="${uid}"]`)?.classList.add("flash"), 100);
  } else if (mf) {
    const f = state.fil.find(x => x.id === +mf[1]);
    if (f) { $("#scanDlg").close(); setTab("filaments"); openFil(f); }
  } else {
    $("#scanLog").insertAdjacentHTML("afterbegin", `<div class="muted">Unbekannter Code: ${esc(text.slice(0, 60))}</div>`);
  }
}

/* ---------- Handy-NFC (Android-App „3D Manager“) ----------
   Die App liest den Bambu-Tag und ruft window.on3dmNfc({uid, tray_uuid, type, detail, color, weight, …}) auf. */
const inApp = typeof window.Android3DM !== "undefined";
if (inApp) {
  document.documentElement.classList.add("in-app");
  const m = $("#menuApp"); m.hidden = false;
  m.onclick = e => { e.preventDefault(); window.Android3DM.changeServer(); };
}
if (!inApp && /Android/i.test(navigator.userAgent)) $("#menuApk").hidden = false;
let nfcLast = null;
window.on3dmNfc = async d => {
  if (typeof d === "string") try { d = JSON.parse(d); } catch { return; }
  try {
    const r = await api("/api/nfc/scan", { method: "POST", body: JSON.stringify(d) });
    state.rfid = await api("/api/ams/rfid");
    const t = state.rfid.find(x => x.key === r.key) || { key: r.key };
    if ($("#scanDlg").open && t.unit_id) return handleScan(`/s/${t.unit_id}`);   // Inventur per NFC
    if ($("#scanDlg").open && t.filament_ref) return handleScan(`/f/${t.filament_ref}`);
    nfcLast = { t, d, r }; renderNfc();
  } catch (e) { toast("NFC: " + e.message); }
};
window.on3dmBack = () => {
  const open = [...document.querySelectorAll("dialog[open]")];
  if (open.length) { open[open.length - 1].close(); return true; }
  if (state.tab !== "overview") { setTab("overview"); return true; }
  return false;
};
function renderNfc() {
  const { t, d, r } = nfcLast, f = t.filament_ref ? state.fil.find(x => x.id === t.filament_ref) : null;
  const u = f && t.unit_id ? (f.units || []).find(x => x.id === t.unit_id) : null;
  const claimCol = d.color || t.claim_color;
  const facts = [
    d.weight ? `${d.weight} g` : null, d.diameter ? `${(+d.diameter).toFixed(2)} mm` : null,
    d.nozzle_min ? `Düse\u00a0${d.nozzle_min}–${d.nozzle_max}\u00a0°C` : null, d.dry_temp ? `Trocknen\u00a0${d.dry_temp}\u00a0°C\u00a0/\u00a0${d.dry_hours}\u00a0h` : null,
    d.prod_date ? `produziert ${esc(String(d.prod_date).replace(/_/g, "-").replace(/-(\d\d)-(\d\d)$/, " $1:$2"))}` : null,
  ].filter(Boolean);
  $("#nfcTitle").innerHTML = `NFC-Tag ${r.created ? `<span class="pill done">neu erfasst</span>` : ""}`;
  $("#nfcBody").innerHTML = `
    <div class="nfc-claim">
      ${claimCol ? swatchHTML({ colors: ["#" + claimCol.slice(0, 8)] }, { hex: false, img: false }) : `<span class="sw0"></span>`}
      <div><small class="muted">Tag meldet (nur Info)</small>
        <b>${esc(d.detail || t.claims || d.type || "unbekannt")}</b>
        <small class="mono">UID ${esc(d.uid || "–")}${(d.tray_uuid || t.rfid) ? " · " + esc((d.tray_uuid || t.rfid).slice(0, 12)) + "…" : ""}</small>
        ${facts.length ? `<small class="muted">${facts.join(" · ")}</small>` : ""}</div>
    </div>
    ${d.mifare === false ? `<p class="muted small nfc-note">Dieses Handy kann die verschlüsselten Daten von MIFARE-Classic-Tags nicht lesen – nur die UID. Für die Zuordnung reicht das; Material und Farbe ergänzt das AMS, sobald die Spule einmal eingelegt war.</p>` : ""}
    ${d.auth === false ? `<p class="muted small nfc-note">Kein Bambu-Tag (oder Schlüssel passen nicht) – nur die UID wird verwendet.</p>` : ""}
    <div class="nfc-inv">
      <small class="muted">Ist im Inventar</small>
      ${f ? `<div class="nfc-fil">${swatchHTML({ colors: f.colors }, { hex: false, img: false })}
          <div><b>${esc(f.color_name)}</b><small>${esc(f.brand)} · ${esc(f.material)}${u ? " · " + esc(u.code) : ""}</small>
          ${u && u.remaining_g != null ? `<small>${barI({ grams: Math.round(u.remaining_g), total: u.initial_g })}</small>` : ""}</div></div>`
        : `<p class="muted">Noch keiner Inventar-Spule zugeordnet.</p>`}
    </div>`;
  $("#nfcFoot").innerHTML = f
    ? `<button type="button" class="ghost" data-nfc="relink">Anders zuordnen</button><span class="spacer"></span><button type="button" class="primary" data-nfc="open">Spule öffnen</button>`
    : `<span class="spacer"></span><button type="button" class="ghost" data-close>Schließen</button><button type="button" class="primary" data-nfc="link">Inventar-Spule zuordnen</button>`;
  $("#nfcFoot [data-close]")?.addEventListener("click", () => $("#nfcDlg").close());
  if (!$("#nfcDlg").open) $("#nfcDlg").showModal();
}
$("#nfcFoot").addEventListener("click", e => {
  const b = e.target.closest("[data-nfc]"); if (!b || !nfcLast) return;
  const { t } = nfcLast;
  if (b.dataset.nfc === "open") {
    const f = state.fil.find(x => x.id === t.filament_ref); if (!f) return;
    $("#nfcDlg").close(); setTab("filaments"); openFil(f);
    if (t.unit_id) setTimeout(() => { const row = $(`.unitrow[data-uid="${t.unit_id}"]`); row?.classList.add("flash"); row?.scrollIntoView({ block: "center" }); }, 150);
  } else {
    pickFilament(`Tag ${shortKey(t.key)} zuordnen`, t.filament_ref || null, async fid => {
      await linkTag(t.key, fid);
      nfcLast.t = state.rfid.find(x => x.key === t.key) || t; renderNfc();
    });
  }
});

/* ---------- Massenbearbeitung (Filament) ---------- */
const BULK_NUM = new Set(["min_qty", "price", "net_weight_g", "tare_g"]);
function bulkSel() { return state.fil.filter(f => state.sel.has(f.id)); }
function commonVal(list, get) {
  const vals = list.map(get).map(v => v === undefined ? null : v);
  return vals.every(v => v === vals[0]) ? vals[0] : undefined;   // undefined = verschieden
}
$("#bulkAll").onclick = () => {
  const vis = $$("#filGrid [data-id]").map(e => +e.dataset.id);
  vis.forEach(id => state.sel.add(id)); renderFil();
  toast(`${state.sel.size} ausgewählt`);
};
$("#bulkEdit").onclick = () => {
  const list = bulkSel(); if (!list.length) return;
  const f = $("#bulkForm"); f.reset();
  $("#bulkTitle").textContent = `${list.length} Filament${list.length > 1 ? "e" : ""} bearbeiten`;
  const names = list.map(x => `${x.color_name} (${x.brand} ${x.material})`);
  $("#bulkWho").textContent = names.slice(0, 6).join(", ") + (names.length > 6 ? ` und ${names.length - 6} weitere` : "");
  const ph = (inp, v) => { inp.value = ""; inp.placeholder = v === undefined ? "verschieden – unverändert" : v === null || v === "" ? "leer – unverändert" : `alle: ${String(v).replace(".", ",")}`; };
  $$("[data-bs]", f).forEach(i => ph(i, commonVal(list, x => x[i.dataset.bs])));
  const fam = commonVal(list, x => familyOf(x.material));
  $$("[data-bp]", f).forEach(i => {
    const k = i.dataset.bp, v = commonVal(list, x => (x.profile || {})[k] ?? null);
    ph(i, v);
    if (v === null && fam !== undefined && famProfile(list[0].material)[k] != null && famProfile(list[0].material)[k] !== "") i.placeholder = `Richtwert ${fam}: ${String(famProfile(list[0].material)[k]).replace(".", ",")}`;
  });
  $$("[data-bb]", f).forEach(sel => {
    const v = commonVal(list, x => !!x[sel.dataset.bb]);
    sel.value = ""; sel.options[0].textContent = v === undefined ? "unverändert" : `unverändert: ${v ? "ja" : "nein"}`; sel.title = v === undefined ? "gemischt" : "";
  });
  $("#bulkNote").value = "";
  const bs = $("#bulkStudio"); delete bs.dataset.touched; bs.dataset.val = "null";
  const cs = commonVal(list, x => x.studio ? x.studio.filament_id : null);
  bs.querySelector("span").textContent = cs === undefined ? "unverändert (verschieden)" : cs ? `unverändert: ${list[0].studio.name}` : "unverändert (automatisch)";
  bulkSummary();
  $("#bulkDlg").showModal();
};
function bulkPayload() {
  const f = $("#bulkForm"), set = {}, profile = {};
  const val = (i, num) => {
    const v = i.value.trim(); if (!v) return undefined;
    if (v === "-") return null;
    if (!num) return v;
    const n = Number(v.replace(",", ".")); return Number.isFinite(n) ? n : NaN;
  };
  let bad = null;
  $$("[data-bs]", f).forEach(i => { const v = val(i, BULK_NUM.has(i.dataset.bs)); if (v !== undefined) { if (Number.isNaN(v)) bad = i; else set[i.dataset.bs] = v; } });
  $$("[data-bp]", f).forEach(i => { const v = val(i, i.dataset.bp !== "notes"); if (v !== undefined) { if (Number.isNaN(v)) bad = i; else profile[i.dataset.bp] = v; } });
  $$("[data-bb]", f).forEach(s => { if (s.value !== "") set[s.dataset.bb] = s.value === "1"; });
  if ($("#bulkStudio").dataset.touched) set.studio = JSON.parse($("#bulkStudio").dataset.val || "null");
  return { set, profile, note: $("#bulkNote").value.trim(), bad };
}
const BULK_LABEL = { brand: "Marke", material: "Material", location: "Lagerort", min_qty: "Mindestbestand", price: "Preis", net_weight_g: "Netto",
  tare_g: "Tara", studio: "Studio-Profil", manufacturer_url: "Hersteller-Link", buy_url: "Shop-Link", sealed: "OVP", spool: "Spule", refill: "Refill", sample: "Muster" };
function bulkSummary() {
  const { set, profile, note } = bulkPayload();
  const parts = [...Object.keys(set).map(k => BULK_LABEL[k] || k), ...(Object.keys(profile).length ? ["Profil (" + Object.keys(profile).length + ")"] : []), ...(note ? ["Notiz"] : [])];
  $("#bulkSummary").textContent = parts.length ? "Ändert: " + parts.join(", ") : "Noch nichts geändert";
  $("#bulkSave").disabled = !parts.length;
}
$("#bulkForm").addEventListener("input", bulkSummary);
$("#bulkForm").addEventListener("submit", async e => {
  e.preventDefault();
  const { set, profile, note, bad } = bulkPayload();
  if (bad) { bad.focus(); toast("Bitte eine Zahl eingeben"); return; }
  const ids = [...state.sel];
  try {
    const r = await api("/api/filaments/bulk", { method: "POST", body: JSON.stringify({
      ids, set, profile: Object.keys(profile).length ? profile : undefined, notes_append: note || undefined,
      unit_price: $("#bulkUnitPrice").checked, sealed_weight: $("#bulkSealedW").checked }) });
    $("#bulkDlg").close();
    await refreshAll();
    toast(`${r.count} Filament${r.count > 1 ? "e" : ""} geändert`);
  } catch (err) { toast("Fehler: " + err.message); }
});

/* ---------- Bambu Studio: Profil des Fremdfilaments im AMS-Slot ---------- */
const STUDIO_ST = {
  ok: ["ok", "✓", "Bambu Studio zeigt dieses Profil, Farbe und Temperatur für den Slot"],
  sent: ["run", "…", "An den Drucker gesendet – warte auf Rückmeldung"],
  rejected: ["bad", "⚠", "Der Drucker meldet weiter die Werte des Bambu-Tags. Die Firmware übernimmt für Slots mit Bambu-Tag offenbar keine Änderung."],
  pending: ["", "○", "Noch nicht gesendet"],
  off: ["", "○", "Automatisches Senden ist ausgeschaltet"],
};
function studioLine(p, t) {
  const s = t.studio; if (!s) return "";
  const [cls, ico, tip] = STUDIO_ST[s.status] || STUDIO_ST.pending;
  return `<div class="studio-line ${cls}" title="${esc(tip)}">
    <span>${ico} Studio: <b>${esc(s.preset || s.filament_id || "?")}</b></span>
    ${s.status !== "ok" ? `<button type="button" class="ghost mini" data-studio-push="${p.id}:${t.ams_id}:${t.tray_id}" title="Werte jetzt an den Drucker senden">↻</button>` : ""}
  </div>`;
}
$("#amsLive").addEventListener("click", async e => {
  const b = e.target.closest("[data-studio-push]"); if (!b) return;
  const [pid, aid, tid] = b.dataset.studioPush.split(":").map(Number);
  b.disabled = true;
  try {
    const res = await fetch("/api/studio/push", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ printer_id: pid, ams_id: aid, tray_id: tid }) });
    const r = await res.json().catch(() => ({}));
    toast(res.ok ? "An den Drucker gesendet – Ergebnis in ca. 20 s" : "Fehler: " + (r.error || res.status));
    setTimeout(() => loadAms().catch(() => {}), 4000); setTimeout(() => loadAms().catch(() => {}), 24000);
  } finally { b.disabled = false; }
});

/* Profilauswahl (Filament-Dialog und Massenbearbeitung) */
async function studioPresets(force) {
  if (!state.studio || force) state.studio = await api("/api/studio/presets");
  return state.studio;
}
async function pickStudioPreset(title, current, onPick) {
  const { presets } = await studioPresets();
  openPicker({
    title, selected: current ?? null,
    clearLabel: "Automatisch (passend zu Marke und Material)",
    items: presets.map(p => ({
      value: p.filament_id, label: p.name, colors: ["#7a7a7aFF"],
      html: `${esc(p.name)}`,
      sub: `${p.vendor || ""}${p.type ? " · " + p.type : ""}${p.nozzle_min ? ` · ${p.nozzle_min}–${p.nozzle_max} °C` : ""} · ${p.filament_id}`,
      badge: p.source === "eigenes" ? "eigenes" : "", badgeClass: "ams",
      _hay: norm(`${p.name} ${p.vendor} ${p.type} ${p.filament_id}`),
    })),
    onPick: v => {
      const p = presets.find(x => x.filament_id === v);
      onPick(v ? { filament_id: v, name: p ? p.name : v } : null);
    },
  });
}
async function renderStudioBox(f) {
  const box = $("#studioBox"); if (!box) return;
  const btn = $("#studioPick"), hint = $("#studioHint");
  btn.dataset.val = JSON.stringify(f && f.studio || null);
  btn.querySelector("span").textContent = "…";
  if (!f || !f.id) { btn.querySelector("span").textContent = "Automatisch (nach dem Speichern)"; hint.textContent = ""; return; }
  try {
    const d = await api(`/api/studio/filament/${f.id}`);
    btn.querySelector("span").innerHTML = `${esc(d.preset)} <small>${d.auto ? "automatisch" : "gewählt"} · ${esc(d.tray_info_idx)}</small>`;
    hint.textContent = `Ins AMS gesendet werden: ${d.preset}, Farbe #${d.tray_color.slice(0, 6)}, Düse ${d.nozzle_temp_min}–${d.nozzle_temp_max} °C, Typ ${d.tray_type}.`;
  } catch { btn.querySelector("span").textContent = "Automatisch"; }
}
document.addEventListener("click", e => {
  const b = e.target.closest("#studioPick, #bulkStudio"); if (!b) return;
  e.preventDefault();
  const cur = JSON.parse(b.dataset.val || "null");
  pickStudioPreset("Bambu-Studio-Profil wählen", cur ? cur.filament_id : null, v => {
    b.dataset.val = JSON.stringify(v); b.dataset.touched = "1";
    b.querySelector("span").innerHTML = v ? `${esc(v.name)} <small>gewählt · ${esc(v.filament_id)}</small>` : "Automatisch";
    if (b.id === "bulkStudio") bulkSummary();
  });
});

$("#studioAuto").addEventListener("change", async e => {
  await api("/api/studio/auto", { method: "PUT", body: JSON.stringify({ on: e.target.checked }) });
  toast(e.target.checked ? "Fremdfilament wird automatisch ans AMS gemeldet" : "Automatisches Melden aus");
});
$("#studioRefresh").onclick = async () => {
  const res = await fetch("/api/studio/presets/refresh", { method: "POST" });
  const r = await res.json().catch(() => ({}));
  if (!res.ok) return toast("Fehler: " + (r.error || res.status));
  const st = await studioPresets(true);
  $("#studioCloudAt").textContent = `${st.presets.filter(p => p.source === "eigenes").length} eigene Profile, geladen ${fmtTime(st.cloud_at)}.`;
  toast(`${r.count} Profile aus der Bambu-Cloud geladen`);
};
