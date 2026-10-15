"use strict";
/* ---------- Statistik-Reiter: Zeitraum, Drucke, Kosten, Lager, Drucker & AMS ---------- */
const ST = { range: store.get("stRange", "90d"), from: "", to: "", metric: "grams", jobs: "expensive", idle: store.get("stIdle", 90), data: null };
const fmtH = h => h == null ? "–" : h >= 10 ? `${Math.round(h).toLocaleString("de-DE")} h` : `${Number(h).toLocaleString("de-DE", { maximumFractionDigits: 1 })} h`;
const fmtHM = h => { const m = Math.round(h * 60); return m < 60 ? `${m} min` : `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")} h`; };
const fmtD = iso => new Date(iso).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "2-digit" });
const fmtPct = v => v == null ? "–" : `${v} %`;

async function loadStats() {
  const q = ST.from ? `from=${ST.from}${ST.to ? "&to=" + ST.to : ""}` : `range=${ST.range}`;
  if (!state.settings || !state.settings.currency) try { state.settings = await api("/api/settings"); } catch {}
  ST.data = await api(`/api/stats/full?${q}&idle_days=${ST.idle}`);
  renderStatsTab();
}
function renderStatsTab() {
  const d = ST.data; if (!d) return;
  $$("#stRange button").forEach(b => b.classList.toggle("active", !ST.from && b.dataset.range === ST.range));
  $("#stFrom").value = ST.from || d.from; $("#stTo").value = ST.to || d.to;
  $("#stPeriod").textContent = `${fmtD(d.from)} – ${fmtD(d.to)}`;
  stKpis(d); stSeries(d); stCosts(d); stPrints(d); stPrinters(d); stHeat(d); stSlots(d); stStock(d); stIdle(d);
}

/* KPI-Kacheln mit Vergleich zum gleich langen Vorzeitraum */
function delta(v, colored) {
  if (v == null) return `<em class="dl">kein Vergleich</em>`;
  const up = v > 0;
  return `<em class="dl ${v === 0 || !colored ? "" : up ? "bad" : "good"}" title="gegenüber dem vorherigen gleich langen Zeitraum">${up ? "▲" : v < 0 ? "▼" : "●"} ${Math.abs(v)} %</em>`;
}
function stKpis(d) {
  const k = d.kpi, p = d.prints;
  const tiles = [
    [fmtG(k.grams), "Verbrauch", delta(k.delta.grams)],
    [fmtMoney(k.cost), `Kosten · Material ${fmtMoney(k.material)} + Strom ${fmtMoney(k.energy)}`, delta(k.delta.cost, true)],
    [String(k.jobs), "Drucke", delta(k.delta.jobs)],
    [fmtH(k.hours), "Druckzeit", delta(k.delta.hours)],
    [fmtPct(p.success_rate), `Erfolgsquote · ${p.ok} fertig, ${p.failed} abgebrochen`, ""],
    [p.total ? `${fmtG(p.avg_grams)} · ${fmtMoney(p.avg_cost)}` : "–", `Ø je Druck · ${fmtHM(p.avg_hours)}`, ""],
    [p.failed ? `${fmtG(p.waste_g)} · ${fmtMoney(p.waste_cost)}` : "0 g", "Ausschuss durch Abbrüche", ""],
  ];
  $("#stKpi").innerHTML = tiles.map(([v, l, x]) => `<div class="kpi"><b>${esc(v)}</b><span>${esc(l)}</span>${x}</div>`).join("") +
    (k.unpriced_g ? `<p class="muted small st-note">${fmtG(k.unpriced_g)} Verbrauch ohne Preis – Preise lassen sich per Massenbearbeitung nachtragen.</p>` : "");
}

/* Verlauf: eine Kennzahl je Diagramm, umschaltbar */
const ST_METRIC = { grams: ["Verbrauch", fmtG], cost: ["Kosten", v => fmtMoney(v)], jobs: ["Drucke", v => `${v} Drucke`], hours: ["Stunden", fmtH], failed: ["Abbrüche", v => `${v} abgebrochen`] };
function bucketLabel(key, kind, long) {
  if (kind === "month") return long ? `${MONTHS[+key.slice(5, 7) - 1]} ${key.slice(0, 4)}` : MONTHS[+key.slice(5, 7) - 1];
  const dt = new Date(key + "T00:00");
  const s = dt.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" });
  return kind === "week" ? (long ? `Woche ab ${s}` : s) : (long ? dt.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" }) : s);
}
function stSeries(d) {
  const m = ST.metric, [title, fmt] = ST_METRIC[m];
  const n = d.series.length, step = Math.max(1, Math.ceil(n / (innerWidth < 700 ? 6 : 12)));
  const rows = d.series.map((r, i) => ({ ...r, _i: i }));
  const per = { day: "Tag", week: "Woche", month: "Monat" }[d.bucket];
  $("#stSeries").innerHTML = `<div class="row-head"><h3>Verlauf</h3><span class="muted small">je ${per}</span><span class="spacer"></span>
      <div class="seg mini">${Object.entries(ST_METRIC).map(([k, [l]]) => `<button data-stm="${k}" class="${k === m ? "active" : ""}">${l}</button>`).join("")}</div></div>` +
    barChart(rows, m, (r, long) => long ? bucketLabel(r.key, d.bucket, true) : (r._i % step ? "" : bucketLabel(r.key, d.bucket)), fmt) +
    `<details class="tblview"><summary>Als Tabelle</summary><div class="table-wrap"><table class="tbl"><thead><tr><th>${per}</th><th>Verbrauch</th><th>Kosten</th><th>Drucke</th><th>Abgebr.</th><th>Stunden</th></tr></thead><tbody>${
      d.series.filter(r => r.grams || r.jobs).map(r => `<tr><td>${esc(bucketLabel(r.key, d.bucket, true))}</td><td>${fmtG(r.grams)}</td><td>${fmtMoney(r.cost)}</td><td>${r.jobs}</td><td>${r.failed}</td><td>${r.hours}</td></tr>`).join("") || `<tr><td colspan="6" class="muted">Keine Daten im Zeitraum</td></tr>`}</tbody></table></div></details>`;
}

/* Kosten */
function splitBar(parts) {
  const total = parts.reduce((a, p) => a + p.v, 0) || 1;
  return `<div class="splitbar">${parts.map(p => `<i class="${p.cls}" style="width:${(p.v / total) * 100}%" title="${esc(p.l)}: ${esc(fmtMoney(p.v))}"></i>`).join("")}</div>
    <div class="splitleg">${parts.map(p => `<span><i class="${p.cls}"></i>${esc(p.l)} <b>${esc(fmtMoney(p.v))}</b> <small class="muted">${Math.round(p.v / total * 100)} %</small></span>`).join("")}</div>`;
}
const empty = t => `<p class="muted">${t}</p>`;
function stCosts(d) {
  const k = d.kpi, p = d.prints;
  $("#stCostSplit").innerHTML = `<div class="row-head"><h3>Kostenaufteilung</h3></div>` +
    (k.cost ? splitBar([{ v: k.material - (p.waste_cost > 0 ? Math.min(k.material, p.waste_cost) : 0), l: "Material", cls: "s1" },
      { v: Math.min(k.material, p.waste_cost || 0), l: "davon Ausschuss", cls: "s3" }, { v: k.energy, l: "Strom", cls: "s2" }])
      + `<p class="muted small">Strom: Druckzeit × Leistung × Strompreis (⋯ → Einstellungen).</p>` : empty("Keine Kosten im Zeitraum."));
  const costRows = (rows, title, el) => {
    $(el).innerHTML = `<div class="row-head"><h3>${title}</h3><span class="muted small">Kosten · Verbrauch</span></div>` +
      (rows.length ? `<div class="hbars">${rows.map(r => {
        const max = Math.max(1, ...rows.map(x => x.cost || x.grams / 1000));
        return `<div class="hbar" title="${esc(r.name)}: ${esc(fmtMoney(r.cost))} · ${esc(fmtG(r.grams))}"><span class="hl">${esc(r.name)}</span>
          <span class="ht"><i style="width:${((r.cost || r.grams / 1000) / max) * 100}%"></i></span><span class="hv">${esc(fmtMoney(r.cost))} · ${esc(fmtG(r.grams))}</span></div>`;
      }).join("")}</div>` : empty("Noch kein Verbrauch im Zeitraum."));
  };
  costRows(d.by_material, "Nach Material", "#stByMat");
  costRows(d.by_brand, "Nach Marke", "#stByBrand");
  $("#stTop").innerHTML = `<div class="row-head"><h3>Meistgenutzte Farben</h3></div>` +
    (d.top.length ? hbars(d.top, r => `${r.name} · ${r.sub}`, r => r.grams, fmtG, true) : empty("Noch kein Verbrauch im Zeitraum."));
}

/* Drucke */
function stPrints(d) {
  const p = d.prints;
  const ring = pct => { const r = 34, c = 2 * Math.PI * r, v = (pct ?? 0) / 100;
    return `<svg class="st-ring" viewBox="0 0 84 84" role="img" aria-label="Erfolgsquote ${pct ?? "–"} %"><circle cx="42" cy="42" r="${r}" class="tr"/>
      <circle cx="42" cy="42" r="${r}" class="ok" stroke-dasharray="${c * v} ${c}" transform="rotate(-90 42 42)"/>
      <text x="42" y="47" text-anchor="middle">${pct == null ? "–" : pct + "%"}</text></svg>`; };
  $("#stPrints").innerHTML = `<div class="row-head"><h3>Druckauswertung</h3></div>` + (p.total ? `<div class="st-prints">${ring(p.success_rate)}
    <dl class="st-dl">
      <div><dt>Fertig</dt><dd>${p.ok}</dd></div><div><dt>Abgebrochen</dt><dd class="${p.failed ? "warn" : ""}">${p.failed}</dd></div>
      <div><dt>Ø Dauer</dt><dd>${fmtHM(p.avg_hours)}</dd></div><div><dt>Ø Verbrauch</dt><dd>${fmtG(p.avg_grams)}</dd></div>
      <div><dt>Ø Kosten</dt><dd>${fmtMoney(p.avg_cost)}</dd></div><div><dt>Ausschuss</dt><dd>${fmtG(p.waste_g)} · ${fmtMoney(p.waste_cost)}</dd></div>
      <div><dt>Mehrfarbig</dt><dd>${p.multi} von ${p.total}</dd></div><div><dt>Ø Filamente/Druck</dt><dd>${String(p.avg_filaments).replace(".", ",")}</dd></div>
    </dl></div>` : empty("Keine abgeschlossenen Drucke im Zeitraum."));
  const lists = { expensive: ["Teuerste", p.expensive], longest: ["Längste", p.longest], failed: ["Abgebrochen", p.recent_failed] };
  const [, list] = lists[ST.jobs];
  $("#stJobs").innerHTML = `<div class="row-head"><h3>Drucke</h3><span class="spacer"></span><div class="seg mini">${Object.entries(lists).map(([k, [l]]) =>
      `<button data-stj="${k}" class="${k === ST.jobs ? "active" : ""}">${l}</button>`).join("")}</div></div>` +
    (list.length ? `<div class="st-jobs">${list.map(j => `<div class="st-job">
        <span class="st-jn"><b>${esc(j.name)}</b><small class="muted">${esc(fmtD(j.start))} · ${esc(j.printer)}</small></span>
        <span class="pill ${j.ok ? "done" : "bad"}">${j.ok ? "fertig" : "abgebr."}</span>
        <span class="st-jv">${fmtMoney(j.cost)}<small class="muted">${fmtHM(j.hours)} · ${fmtG(j.grams)}</small></span></div>`).join("")}</div>`
      : empty(ST.jobs === "failed" ? "Keine abgebrochenen Drucke 🎉" : "Keine Drucke im Zeitraum."));
}

/* Drucker */
function stPrinters(d) {
  $("#stPrinters").innerHTML = `<div class="row-head"><h3>Auslastung je Drucker</h3><span class="muted small">Anteil der Zeit im Zeitraum, in der gedruckt wurde</span></div>` +
    (d.printers.length ? `<div class="table-wrap"><table class="tbl st-ptbl"><thead><tr><th>Drucker</th><th class="num">Drucke</th><th class="num">Abgebr.</th><th class="num">Druckzeit</th><th class="num">Ø h/Tag</th><th class="num">Verbrauch</th><th>Auslastung</th></tr></thead><tbody>${
      d.printers.map(p => `<tr><td class="strong">${esc(p.name)}</td><td class="num">${p.jobs}</td><td class="num">${p.failed}</td><td class="num">${fmtH(p.hours)}</td>
        <td class="num">${String(p.per_day).replace(".", ",")}</td><td class="num">${fmtG(p.grams)}</td>
        <td><span class="st-util"><span class="bar"><i style="width:${Math.min(100, p.utilization)}%"></i></span><em>${String(p.utilization).replace(".", ",")} %</em></span></td></tr>`).join("")}</tbody></table></div>`
      : empty("Keine Drucke im Zeitraum."));
}
function stHeat(d) {
  const h = d.heatmap.hours, max = Math.max(0, ...h.flat());
  const lvl = v => v <= 0 ? 0 : Math.min(5, Math.ceil((v / max) * 5));
  $("#stHeat").innerHTML = `<div class="row-head"><h3>Wann gedruckt wird</h3><span class="muted small">Druckstunden je Wochentag und Uhrzeit</span></div>` +
    (max ? `<div class="heat" role="img" aria-label="Heatmap Druckstunden">
      <span></span>${Array.from({ length: 24 }, (_, i) => `<span class="hh">${i % 3 ? "" : i}</span>`).join("")}
      ${h.map((row, di) => `<span class="hd">${d.heatmap.days[di]}</span>${row.map((v, hi) =>
        `<i class="l${lvl(v)}" title="${d.heatmap.days[di]} ${hi}–${hi + 1} Uhr: ${fmtH(v)}"></i>`).join("")}`).join("")}
    </div><div class="heat-leg"><span class="muted small">wenig</span>${[1, 2, 3, 4, 5].map(l => `<i class="l${l}"></i>`).join("")}<span class="muted small">viel (max. ${fmtH(max)})</span></div>`
      : empty("Keine Drucke im Zeitraum."));
}
function stSlots(d) {
  $("#stSlots").innerHTML = `<div class="row-head"><h3>Verbrauch je AMS-Slot</h3></div>` +
    (d.slots.length ? hbars(d.slots, r => d.printers.length > 1 ? r.name : r.name.replace(/^.* · /, ""), r => r.grams, fmtG) : empty("Keine Slot-Daten im Zeitraum."));
}

/* Lager */
function stStock(d) {
  const total = d.stock.reduce((a, s) => a + s.value, 0);
  $("#stStock").innerHTML = `<div class="row-head"><h3>Lagerwert je Marke</h3><span class="muted small">aktuell · gesamt ${fmtMoney(total)}</span></div>` +
    (d.stock.length ? `<div class="hbars">${d.stock.map(s => {
      const max = Math.max(1, ...d.stock.map(x => x.value || x.kg));
      return `<div class="hbar" title="${esc(s.name)}: ${s.spools} Spulen · ${esc(fmtG(s.kg * 1000))}${s.unpriced ? ` · ${s.unpriced} ohne Preis` : ""}">
        <span class="hl">${esc(s.name)} <small class="muted">${s.spools}×</small></span><span class="ht"><i style="width:${((s.value || s.kg) / max) * 100}%"></i></span>
        <span class="hv">${esc(fmtMoney(s.value))} · ${esc(fmtG(s.kg * 1000))}</span></div>`;
    }).join("")}</div>` : empty("Kein Bestand."));
}
function stIdle(d) {
  $("#stIdle").innerHTML = `<div class="row-head"><h3>Ladenhüter</h3><span class="muted small">${d.idle_total} Spulen · ${fmtMoney(d.idle_value)} gebunden</span><span class="spacer"></span>
      <select id="stIdleDays" aria-label="Unbenutzt seit">${[60, 90, 180, 365].map(n => `<option value="${n}" ${n === d.idle_days ? "selected" : ""}>≥ ${n} Tage</option>`).join("")}</select></div>` +
    (d.idle.length ? `<div class="st-idle ${ST.idleAll ? "" : "short"}">${d.idle.map(x => `<button type="button" class="st-iu" data-unit="${x.unit_id}" title="Spule öffnen">
        ${swatchHTML({ colors: x.colors }, { hex: false, img: false })}
        <span class="st-jn"><b>${esc(x.name)} <small class="mono muted">${esc(x.code)}</small></b><small class="muted">${esc(x.sub)} · ${x.status === "sealed" ? "OVP" : "offen"} · ${fmtG(x.remaining_g)}</small></span>
        <span class="st-jv">${x.days} Tage<small class="muted">${x.used ? "zuletzt benutzt" : "nie benutzt"}${x.value != null ? " · " + fmtMoney(x.value) : ""}</small></span></button>`).join("")}</div>` +
      (d.idle.length > 8 ? `<button type="button" class="ghost mini st-more" data-idle-all>${ST.idleAll ? "weniger anzeigen" : `alle ${d.idle.length} anzeigen`}</button>` : "") +
      (d.idle_total > d.idle.length ? `<p class="muted small">… und ${d.idle_total - d.idle.length} weitere</p>` : "")
      : empty(`Keine Spule länger als ${d.idle_days} Tage unbenutzt.`));
}

/* Bedienung */
$("#stRange").addEventListener("click", e => {
  const b = e.target.closest("[data-range]"); if (!b) return;
  ST.range = b.dataset.range; ST.from = ST.to = ""; store.set("stRange", ST.range); loadStats();
});
["#stFrom", "#stTo"].forEach(s => $(s).addEventListener("change", () => {
  const f = $("#stFrom").value, t = $("#stTo").value;
  if (f && t && f > t) return toast("„von“ liegt nach „bis“");
  ST.from = f; ST.to = t; loadStats();
}));
$("#tab-stats").addEventListener("click", e => {
  const m = e.target.closest("[data-stm]"); if (m) { ST.metric = m.dataset.stm; stSeries(ST.data); return; }
  const j = e.target.closest("[data-stj]"); if (j) { ST.jobs = j.dataset.stj; stPrints(ST.data); return; }
  if (e.target.closest("[data-idle-all]")) { ST.idleAll = !ST.idleAll; stIdle(ST.data); return; }
  const u = e.target.closest("[data-unit]");
  if (u) {
    const uid = +u.dataset.unit, f = state.fil.find(x => (x.units || []).some(y => y.id === uid));
    if (f) { setTab("filaments"); openFil(f); setTimeout(() => { const r = $(`.unitrow[data-uid="${uid}"]`); r?.classList.add("flash"); r?.scrollIntoView({ block: "center" }); }, 150); }
  }
});
$("#tab-stats").addEventListener("change", e => {
  if (e.target.id === "stIdleDays") { ST.idle = +e.target.value; store.set("stIdle", ST.idle); loadStats(); }
});
addEventListener("resize", () => { if (state.tab === "stats" && ST.data) stSeries(ST.data); });
if (state.tab === "stats") loadStats().catch(() => {});
