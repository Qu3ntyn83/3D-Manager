"use strict";
/* ---------- Bibliothek: Druckdateien (3MF/STL), Tags, Sammlungen, MakerWorld ---------- */
const LIB = { q: "", col: store.get("libCol", ""), sort: store.get("libSort", "updated"), src: "", tags: [], models: [], cols: [], allTags: [],
  sel: new Set(), cur: null, curTags: [], mw: null, poll: null };
const libSize = b => b == null ? "–" : b >= 1e9 ? `${(b / 1e9).toLocaleString("de-DE", { maximumFractionDigits: 1 })} GB` : b >= 1e6 ? `${(b / 1e6).toLocaleString("de-DE", { maximumFractionDigits: 1 })} MB` : `${Math.max(1, Math.round(b / 1e3))} kB`;
const libTime = s => { if (!s) return null; const m = Math.round(s / 60); return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`; };
const libApi = async (url, opt = {}) => {   // wie api(), aber Fehlertext des Servers anzeigen
  const r = await fetch(url, { headers: opt.body && !(opt.body instanceof FormData) ? { "Content-Type": "application/json" } : {}, ...opt });
  if (!r.ok) {
    let msg = r.statusText;
    try { const t = await r.text(); const j = JSON.parse(t); msg = j.error || j.message || msg; } catch {}
    throw new Error(`${r.status} ${msg}`.replace(/<[^>]+>/g, " ").trim().slice(0, 160));
  }
  return r.status === 204 ? null : r.json();
};

async function libLoad() {
  const qs = new URLSearchParams({ sort: LIB.sort });
  if (LIB.q) qs.set("q", LIB.q);
  if (LIB.col) qs.set("collection", LIB.col);
  if (LIB.src) qs.set("source", LIB.src);
  LIB.tags.forEach(t => qs.append("tag", t));
  const [models, cols, tags] = await Promise.all([libApi(`/api/library/models?${qs}`), libApi("/api/library/collections"), libApi("/api/library/tags")]);
  Object.assign(LIB, { models, cols, allTags: tags });
  libRenderFilters(); libRender(); libMwStatus(false);
}
function libRenderFilters() {
  const sel = $("#libCol");
  sel.innerHTML = `<option value="">Alle Sammlungen</option>` + LIB.cols.map(k => `<option value="${k.id}" ${String(k.id) === String(LIB.col) ? "selected" : ""}>${k.source === "makerworld" ? "◆ " : ""}${esc(k.name)} (${k.count})</option>`).join("");
  if (LIB.col && !LIB.cols.some(k => String(k.id) === String(LIB.col))) { LIB.col = ""; sel.value = ""; }
  $("#libSort").value = LIB.sort;
  $$("#libSrc button").forEach(b => b.classList.toggle("active", b.dataset.src === LIB.src));
  const top = LIB.allTags.slice(0, innerWidth < 700 ? 10 : 14), extra = LIB.tags.filter(t => !top.some(x => x.tag === t)).map(t => ({ tag: t, n: 0 }));
  $("#libTags").innerHTML = [...extra, ...top].map(t => `<button class="chip ${LIB.tags.includes(t.tag) ? "active" : ""}" data-libtag="${esc(t.tag)}">#${esc(t.tag)}${t.n ? ` <b>${t.n}</b>` : ""}</button>`).join("") +
    (LIB.allTags.length > top.length ? `<button class="chip ghost" data-alltags>Alle ${LIB.allTags.length} Tags …</button>` : "") +
    (LIB.tags.length ? `<button class="chip ghost" data-libtag="">✕ Tag-Filter</button>` : "");
  $("#libTagList").innerHTML = LIB.allTags.map(t => `<option value="${esc(t.tag)}">`).join("");
}
function libCard(m) {
  const badge = m.source === "makerworld" ? `<span class="lib-badge mw" title="Von MakerWorld">MakerWorld</span>` : "";
  const dl = m.source === "makerworld" && m.files === 0
    ? `<span class="lib-badge ${String(m.mw_download || "").startsWith("Fehler") ? "bad" : ""}" title="${esc(m.mw_download || "")}">${m.mw_download === "pending" ? "⏳ Datei folgt" : String(m.mw_download || "").startsWith("Fehler") ? "⚠ Download" : "nur Link"}</span>` : "";
  return `<article class="lib-card ${LIB.sel.has(m.id) ? "selected" : ""}" data-mid="${m.id}" tabindex="0">
    <input type="checkbox" class="sel" ${LIB.sel.has(m.id) ? "checked" : ""} aria-label="auswählen">
    <div class="lib-thumb">${m.cover ? `<img src="${esc(m.cover)}" alt="" loading="lazy">` : `<span class="lib-noimg">${m.kinds[0] ? m.kinds[0].toUpperCase() : "?"}</span>`}
      <span class="lib-badges">${badge}${dl}</span></div>
    <div class="lib-body">
      <div class="lib-name" title="${esc(m.title)}">${esc(m.title)}</div>
      <div class="lib-sub">${m.creator ? esc(m.creator) + " · " : ""}${m.files} Datei${m.files === 1 ? "" : "en"}${m.kinds.length ? " · " + m.kinds.map(k => k.toUpperCase()).join("/") : ""}${m.size ? " · " + libSize(m.size) : ""}</div>
      ${m.tags.length ? `<div class="lib-ctags">${m.tags.slice(0, 5).map(t => `<span>#${esc(t)}</span>`).join("")}${m.tags.length > 5 ? `<span>+${m.tags.length - 5}</span>` : ""}</div>` : ""}
    </div></article>`;
}
function libRender() {
  $("#libGrid").innerHTML = LIB.models.map(libCard).join("");
  const filtered = LIB.q || LIB.col || LIB.src || LIB.tags.length;
  $("#libEmpty").hidden = LIB.models.length > 0;
  $("#libEmpty").innerHTML = filtered ? `<p class="muted">Keine Treffer für diese Auswahl.</p>` :
    `<p><b>Noch keine Druckdateien.</b></p><p class="muted">3MF- und STL-Dateien hierher ziehen oder über „+ Dateien“ hochladen. Deine MakerWorld-Sammlungen holst du über „MakerWorld“.</p>`;
  $("#libBulk").hidden = !LIB.sel.size;
  $("#libBulkCount").textContent = `${LIB.sel.size} ausgewählt`;
  $("#libGrid").classList.toggle("selecting", LIB.sel.size > 0);
}

/* Filter & Suche */
let libQT;
$("#libQ").addEventListener("input", e => { clearTimeout(libQT); libQT = setTimeout(() => { LIB.q = e.target.value.trim(); libLoad(); }, 250); });
$("#libCol").onchange = e => { LIB.col = e.target.value; store.set("libCol", LIB.col); libLoad(); };
$("#libSort").onchange = e => { LIB.sort = e.target.value; store.set("libSort", LIB.sort); libLoad(); };
$("#libSrc").addEventListener("click", e => { const b = e.target.closest("[data-src]"); if (!b) return; LIB.src = b.dataset.src; libLoad(); });
$("#libTags").addEventListener("click", e => {
  if (e.target.closest("[data-alltags]")) {
    return openPicker({ title: "Nach Tag filtern", items: LIB.allTags.map(t => ({ value: t.tag, label: "#" + t.tag, sub: `${t.n} Modell${t.n === 1 ? "" : "e"}`, colors: ["#7a7a7aFF"], _hay: norm(t.tag) })),
      onPick: v => { if (v && !LIB.tags.includes(v)) { LIB.tags.push(v); libLoad(); } } });
  }
  const b = e.target.closest("[data-libtag]"); if (!b) return;
  const t = b.dataset.libtag;
  LIB.tags = !t ? [] : LIB.tags.includes(t) ? LIB.tags.filter(x => x !== t) : [...LIB.tags, t];
  libLoad();
});

/* Auswahl & Öffnen */
$("#libGrid").addEventListener("click", e => {
  const card = e.target.closest(".lib-card"); if (!card) return;
  const id = +card.dataset.mid;
  if (e.target.matches(".sel") || LIB.sel.size && !e.target.closest("a")) {
    if (!e.target.matches(".sel")) card.querySelector(".sel").checked = !LIB.sel.has(id);
    LIB.sel.has(id) ? LIB.sel.delete(id) : LIB.sel.add(id);
    card.classList.toggle("selected", LIB.sel.has(id)); libRender(); return;
  }
  libOpen(id);
});
$("#libGrid").addEventListener("keydown", e => { if (e.key === "Enter" && e.target.matches(".lib-card")) libOpen(+e.target.dataset.mid); });
$("#libBulkClear").onclick = () => { LIB.sel.clear(); libRender(); };
$("#libBulkAll").onclick = () => { LIB.models.forEach(m => LIB.sel.add(m.id)); libRender(); };
$("#libBulkDel").onclick = async () => {
  if (!confirm(`${LIB.sel.size} Modelle mit allen Dateien löschen?`)) return;
  await libApi("/api/library/models/bulk", { method: "POST", body: JSON.stringify({ ids: [...LIB.sel], delete: true }) });
  LIB.sel.clear(); toast("Gelöscht"); libLoad();
};
$("#libBulkTag").onclick = async () => {
  const t = prompt("Tags hinzufügen (mit Komma trennen):"); if (!t) return;
  await libApi("/api/library/models/bulk", { method: "POST", body: JSON.stringify({ ids: [...LIB.sel], add_tags: t.split(",") }) });
  toast("Tags ergänzt"); libLoad();
};
$("#libBulkCol").onclick = () => {
  const local = LIB.cols.filter(k => k.source === "local");
  if (!local.length) return toast("Erst unter „Sammlungen“ eine eigene Sammlung anlegen");
  openPicker({ title: "Zu Sammlung hinzufügen", items: local.map(k => ({ value: k.id, label: k.name, sub: `${k.count} Modelle`, colors: ["#888888FF"], _hay: norm(k.name) })),
    onPick: async cid => { if (!cid) return; await libApi("/api/library/models/bulk", { method: "POST", body: JSON.stringify({ ids: [...LIB.sel], add_collection: cid }) }); toast("Hinzugefügt"); libLoad(); } });
};

/* Upload (Knopf und Ziehen & Ablegen) */
async function libUpload(files, mid) {
  files = [...files].filter(f => /\.(3mf|stl)$/i.test(f.name));
  if (!files.length) return toast("Nur 3MF- und STL-Dateien");
  const fd = new FormData();
  files.forEach(f => fd.append("files", f, f.name));
  let separate = false;
  if (!mid && files.length > 1) separate = !confirm(`${files.length} Dateien als EIN Modell anlegen?\n\nOK = ein Modell mit allen Dateien\nAbbrechen = jede Datei als eigenes Modell`);
  if (separate) fd.append("separate", "1");
  if (!mid && LIB.col && LIB.cols.find(k => String(k.id) === String(LIB.col) && k.source === "local")) fd.append("collection", LIB.col);
  if (!mid && LIB.tags.length) fd.append("tags", LIB.tags.join(","));
  const total = files.reduce((a, f) => a + f.size, 0);
  toast(`Lade ${files.length} Datei${files.length > 1 ? "en" : ""} hoch (${libSize(total)}) …`);
  try {
    const r = await libApi(mid ? `/api/library/models/${mid}/files` : "/api/library/models", { method: "POST", body: fd });
    toast("Hochgeladen – Vorschau wird erstellt");
    await libLoad();
    if (mid) libOpen(mid); else if (r.ids && r.ids.length === 1) libOpen(r.ids[0]);
    setTimeout(() => state.tab === "library" && libLoad().catch(() => {}), 2500);
  } catch (e) { toast("Upload fehlgeschlagen: " + e.message); }
}
$("#libFiles").onchange = e => { libUpload(e.target.files); e.target.value = ""; };
$("#libAddFiles").onchange = e => { if (LIB.cur) libUpload(e.target.files, LIB.cur.id); e.target.value = ""; };
let libDragN = 0;
const libTab = $("#tab-library");
libTab.addEventListener("dragenter", e => { if ([...(e.dataTransfer?.types || [])].includes("Files")) { libDragN++; $("#libDrop").hidden = false; } });
libTab.addEventListener("dragleave", () => { if (--libDragN <= 0) { libDragN = 0; $("#libDrop").hidden = true; } });
libTab.addEventListener("dragover", e => e.preventDefault());
libTab.addEventListener("drop", e => { e.preventDefault(); libDragN = 0; $("#libDrop").hidden = true; if (e.dataTransfer.files.length) libUpload(e.dataTransfer.files); });

/* Detail-Dialog */
async function libOpen(id) {
  const m = await libApi(`/api/library/models/${id}`);
  LIB.cur = m; LIB.curTags = [...m.tags];
  const f = $("#libForm");
  f.title.value = m.title; f.creator.value = m.creator || ""; f.description.value = m.description || ""; f.notes.value = m.notes || "";
  $("#libTitleH").textContent = m.source === "makerworld" ? "MakerWorld-Modell" : "Modell";
  $("#libCover").innerHTML = m.cover ? `<img src="${esc(m.cover)}" alt="">` : `<span class="lib-noimg">${(m.kinds[0] || "?").toUpperCase()}</span>`;
  $("#libMeta").innerHTML = [m.license ? `Lizenz: ${esc(m.license)}` : "", `angelegt ${fmtTime(m.created_at)}`].filter(Boolean).join(" · ");
  libRenderTagEd(); libRenderColEd(); libRenderFiles(); libRenderMw();
  if (!$("#libDlg").open) $("#libDlg").showModal();
  if (m.file_list.some(x => x.analyzing)) setTimeout(() => LIB.cur && LIB.cur.id === id && $("#libDlg").open && libRefreshFiles(id), 2000);
}
async function libRefreshFiles(id) {
  const m = await libApi(`/api/library/models/${id}`);
  LIB.cur.file_list = m.file_list; LIB.cur.cover = m.cover; libRenderFiles();
  $("#libCover").innerHTML = m.cover ? `<img src="${esc(m.cover)}" alt="">` : $("#libCover").innerHTML;
  if (m.file_list.some(x => x.analyzing)) setTimeout(() => LIB.cur && LIB.cur.id === id && $("#libDlg").open && libRefreshFiles(id), 2000);
}
function libRenderTagEd() {
  const ed = $("#libTagEd"), inp = $("#libTagIn");
  ed.querySelectorAll(".tag-chip").forEach(x => x.remove());
  inp.insertAdjacentHTML("beforebegin", LIB.curTags.map(t => `<span class="tag-chip">#${esc(t)}<button type="button" data-rmtag="${esc(t)}" aria-label="Tag ${esc(t)} entfernen">✕</button></span>`).join(""));
}
function libAddTag(v) {
  v.split(",").map(x => x.trim().toLowerCase().replace(/^#/, "")).filter(Boolean).forEach(t => { if (!LIB.curTags.includes(t)) LIB.curTags.push(t); });
  $("#libTagIn").value = ""; libRenderTagEd();
}
$("#libTagIn").addEventListener("keydown", e => {
  if (e.key === "Enter" || e.key === ",") { e.preventDefault(); if (e.target.value.trim()) libAddTag(e.target.value); }
  else if (e.key === "Backspace" && !e.target.value && LIB.curTags.length) { LIB.curTags.pop(); libRenderTagEd(); }
});
$("#libTagIn").addEventListener("change", e => { if (e.target.value.trim()) libAddTag(e.target.value); });
$("#libTagEd").addEventListener("click", e => {
  const b = e.target.closest("[data-rmtag]"); if (b) { LIB.curTags = LIB.curTags.filter(t => t !== b.dataset.rmtag); libRenderTagEd(); return; }
  $("#libTagIn").focus();
});
function libRenderColEd() {
  const m = LIB.cur, mine = new Set(m.collections.map(k => k.id));
  const local = LIB.cols.filter(k => k.source === "local"), mw = m.collections.filter(k => k.source === "makerworld");
  $("#libColsEd").innerHTML = (local.map(k => `<label class="inline"><input type="checkbox" data-col="${k.id}" ${mine.has(k.id) ? "checked" : ""}> ${esc(k.name)}</label>`).join("") ||
      `<span class="muted small">Noch keine eigenen Sammlungen – unter „Sammlungen“ anlegen.</span>`) +
    (mw.length ? `<div class="muted small lib-mwcols">MakerWorld: ${mw.map(k => esc(k.name)).join(", ")}</div>` : "");
}
function libFileInfo(x) {
  const m = x.meta || {}, bits = [];
  if (m.plates && m.plates.length) bits.push(`${m.plates.length} Platte${m.plates.length > 1 ? "n" : ""}`);
  if (x.grams) bits.push(`${String(x.grams).replace(".", ",")} g`);
  if (x.seconds) bits.push(libTime(x.seconds));
  if (m.size_mm) bits.push(m.size_mm.map(v => Math.round(v)).join(" × ") + " mm");
  if (m.filaments && m.filaments.length) bits.push(`<span class="lib-fils">${m.filaments.slice(0, 8).map(f => `<i style="background:${esc((f.color || "#888").slice(0, 7))}" title="${esc(f.type || "")}"></i>`).join("")}</span>`);
  if (m.error) bits.push(`<span class="err">${esc(m.error)}</span>`);
  return bits.join(" · ");
}
function studioLink(x) {
  const abs = new URL(x.url, location.href).href;
  const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
  return mac ? `bambustudioopen://${encodeURIComponent(abs)}` : `bambustudio://open?file=${encodeURIComponent(abs)}`;
}
function libRenderFiles() {
  const fl = LIB.cur.file_list;
  $("#libFileList").innerHTML = fl.length ? fl.map(x => `<div class="lib-file" data-fid="${x.id}">
      <div class="lib-fthumb">${x.thumb ? `<img src="${esc(x.thumb)}" alt="" loading="lazy">` : `<span>${x.analyzing ? "…" : x.kind.toUpperCase()}</span>`}</div>
      <div class="lib-finfo"><b title="${esc(x.name)}">${esc(x.name)}</b>
        <small class="muted">${x.kind.toUpperCase()} · ${libSize(x.size)}${x.source === "makerworld" ? " · von MakerWorld" : ""}${x.analyzing ? " · wird ausgewertet …" : ""}</small>
        <small class="muted">${libFileInfo(x)}</small></div>
      <div class="lib-facts">
        <button type="button" class="ghost mini" data-fact="view" title="3D-Vorschau">3D</button>
        <a class="ghost mini btnlike" href="${esc(studioLink(x))}" title="In Bambu Studio öffnen (Bambu Studio muss installiert sein)">Studio</a>
        <a class="ghost mini btnlike" href="${esc(x.url)}" download title="Herunterladen">⤓</a>
        ${x.thumb ? `<button type="button" class="ghost mini" data-fact="cover" title="Als Titelbild verwenden">★</button>` : ""}
        <button type="button" class="ghost mini danger" data-fact="del" title="Datei löschen">✕</button>
      </div></div>`).join("")
    : `<p class="muted small">${LIB.cur.source === "makerworld" ? "Noch keine Datei geladen – siehe MakerWorld oben." : "Keine Dateien."}</p>`;
}
$("#libFileList").addEventListener("click", async e => {
  const b = e.target.closest("[data-fact]"); if (!b) return;
  const fid = +b.closest(".lib-file").dataset.fid, x = LIB.cur.file_list.find(f => f.id === fid);
  if (b.dataset.fact === "view") return libView(x);
  if (b.dataset.fact === "cover") {
    await libApi(`/api/library/models/${LIB.cur.id}`, { method: "PUT", body: JSON.stringify({ cover_file: fid }) });
    toast("Titelbild gesetzt"); return libOpen(LIB.cur.id);
  }
  if (b.dataset.fact === "del") {
    if (!confirm(`„${x.name}“ löschen?`)) return;
    await libApi(`/api/library/files/${fid}`, { method: "DELETE" }); toast("Datei gelöscht"); libOpen(LIB.cur.id); libLoad();
  }
});
$("#libCoverFile").onchange = async e => {
  const file = e.target.files[0]; e.target.value = ""; if (!file || !LIB.cur) return;
  const fd = new FormData(); fd.append("file", file);
  await libApi(`/api/library/models/${LIB.cur.id}/cover`, { method: "POST", body: fd }); toast("Titelbild gespeichert"); libOpen(LIB.cur.id); libLoad();
};
function libRenderMw() {
  const m = LIB.cur, box = $("#libMwBox");
  box.hidden = m.source !== "makerworld";
  if (box.hidden) return;
  const st = m.mw_download || "none";
  $("#libMwInfo").innerHTML = `<p><a href="${esc(m.mw_url)}" target="_blank" rel="noopener noreferrer">Auf MakerWorld öffnen ↗</a>${m.mw_synced_at ? ` <span class="muted small">· abgeglichen ${fmtTime(m.mw_synced_at)}</span>` : ""}</p>
    <p class="small">Druckprofil: <b>${esc(m.mw_instance_title || "Standard")}</b></p>
    <p class="small">${st === "done" ? "✓ 3MF geladen" : st === "pending" ? "⏳ Datei wird beim nächsten Sync geladen" : st.startsWith("Fehler") ? `<span class="err">⚠ ${esc(st)}</span>` : "Datei nicht geladen"}</p>
    <div class="urlrow"><button type="button" class="ghost" id="libMwProfiles">Anderes Druckprofil …</button><button type="button" class="primary" id="libMwDl">${st === "done" ? "Erneut laden" : "3MF jetzt laden"}</button></div>
    <div id="libMwProfList"></div>`;
}
$("#libMwInfo").addEventListener("click", async e => {
  const m = LIB.cur; if (!m) return;
  if (e.target.id === "libMwDl" || e.target.dataset.prof) {
    const body = e.target.dataset.prof ? { profile_id: +e.target.dataset.prof } : {};
    try { await libApi(`/api/makerworld/download/${m.id}`, { method: "POST", body: JSON.stringify(body) }); toast("Download gestartet"); libMwStatus(true); }
    catch (err) { toast("Fehler: " + err.message); }
    return;
  }
  if (e.target.id === "libMwProfiles") {
    const box = $("#libMwProfList"); box.innerHTML = `<p class="muted small">lädt …</p>`;
    try {
      const r = await libApi(`/api/makerworld/profiles/${m.id}`);
      box.innerHTML = `<div class="lib-profs">${r.profiles.map(p => `<div class="lib-prof ${p.selected ? "sel" : ""}">
        <span><b>${esc(p.title || "Profil")}</b><small class="muted">${esc((p.printers || []).slice(0, 4).join(", "))}${p.printers && p.printers.length > 4 ? " …" : ""}${p.ams ? " · AMS" : ""}${p.grams ? ` · ${p.grams} g` : ""}${p.seconds ? " · " + libTime(p.seconds) : ""}</small></span>
        ${p.mine ? `<span class="pill done">dein Drucker</span>` : ""}${p.default ? `<span class="pill">Standard</span>` : ""}
        <button type="button" class="ghost mini" data-prof="${p.profile_id}">laden</button></div>`).join("")}</div>`;
    } catch (err) { box.innerHTML = `<p class="err small">${esc(err.message)}</p>`; }
  }
});
$("#libForm").addEventListener("submit", async e => {
  e.preventDefault();
  if ($("#libTagIn").value.trim()) libAddTag($("#libTagIn").value);
  const f = e.target;
  const body = { title: f.title.value, creator: f.creator.value, description: f.description.value, notes: f.notes.value, tags: LIB.curTags,
    collections: $$("#libColsEd [data-col]").filter(x => x.checked).map(x => +x.dataset.col) };
  try { await libApi(`/api/library/models/${LIB.cur.id}`, { method: "PUT", body: JSON.stringify(body) }); $("#libDlg").close(); toast("Gespeichert"); libLoad(); }
  catch (err) { toast("Fehler: " + err.message); }
});
$("#libDel").onclick = async () => {
  if (!LIB.cur || !confirm(`„${LIB.cur.title}“ mit allen Dateien löschen?`)) return;
  await libApi(`/api/library/models/${LIB.cur.id}`, { method: "DELETE" }); $("#libDlg").close(); toast("Gelöscht"); libLoad();
};

/* 3D-Vorschau (three.js wird erst bei Bedarf geladen) */
let libThree = null;
function libScript(src) { return new Promise((ok, fail) => { const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = () => fail(new Error(src)); document.head.appendChild(s); }); }
async function libEnsureThree() {
  if (libThree) return libThree;
  libThree = (async () => {
    await libScript("/static/vendor/three/three.min.js");
    await Promise.all(["fflate.min.js", "OrbitControls.js", "STLLoader.js"].map(n => libScript("/static/vendor/three/" + n)));
    await libScript("/static/vendor/three/3MFLoader.js");
  })();
  return libThree;
}
let libViewer = null;
async function libView(x) {
  $("#viewTitle").textContent = x.name; $("#viewMsg").textContent = "lädt …"; $("#viewMsg").hidden = false;
  $("#viewDlg").showModal();
  try {
    await libEnsureThree();
    const buf = await (await fetch(x.url + "?inline=1")).arrayBuffer();
    const T = window.THREE, host = $("#viewer");
    if (libViewer) { libViewer.stop(); libViewer = null; }
    host.querySelectorAll("canvas").forEach(c => c.remove());
    const w = host.clientWidth, h = host.clientHeight;
    const renderer = new T.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(2, devicePixelRatio)); renderer.setSize(w, h); host.appendChild(renderer.domElement);
    const scene = new T.Scene();
    scene.add(new T.HemisphereLight(0xffffff, 0x444444, 0.9));
    const dl = new T.DirectionalLight(0xffffff, 0.8); dl.position.set(1, 1.5, 2); scene.add(dl);
    const cam = new T.PerspectiveCamera(40, w / h, 0.1, 10000);
    let obj;
    const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#f07a35";
    if (x.kind === "stl") {
      const geo = new T.STLLoader().parse(buf); geo.computeVertexNormals();
      obj = new T.Mesh(geo, new T.MeshStandardMaterial({ color: accent, roughness: 0.55, metalness: 0.05 }));
    } else {
      obj = new T.ThreeMFLoader().parse(buf);
      obj.traverse(o => { if (o.isMesh && (!o.material || !o.material.color || o.material.color.getHex() === 0xffffff)) o.material = new T.MeshStandardMaterial({ color: accent, roughness: 0.55 }); });
    }
    obj.rotation.x = -Math.PI / 2;            // Druckbett liegt in Z → Y-oben für three.js
    const box = new T.Box3().setFromObject(obj), size = box.getSize(new T.Vector3()), center = box.getCenter(new T.Vector3());
    obj.position.sub(center); scene.add(obj);
    const grid = new T.GridHelper(Math.max(size.x, size.z) * 1.6, 16, 0x777777, 0x444444); grid.position.y = -size.y / 2; scene.add(grid);
    const r = Math.max(size.x, size.y, size.z);
    cam.position.set(r * 1.1, r * 0.9, r * 1.4); cam.lookAt(0, 0, 0);
    const ctl = new T.OrbitControls(cam, renderer.domElement); ctl.enableDamping = true;
    let run = true;
    const loop = () => { if (!run) return; ctl.update(); renderer.render(scene, cam); requestAnimationFrame(loop); };
    loop();
    const ro = new ResizeObserver(() => { const W = host.clientWidth, H = host.clientHeight; renderer.setSize(W, H); cam.aspect = W / H; cam.updateProjectionMatrix(); });
    ro.observe(host);
    libViewer = { stop() { run = false; ro.disconnect(); ctl.dispose(); renderer.dispose(); renderer.forceContextLoss?.(); renderer.domElement.remove(); } };
    $("#viewMsg").hidden = true;
  } catch (err) { $("#viewMsg").textContent = "Vorschau nicht möglich: " + err.message; }
}
$("#viewDlg").addEventListener("close", () => { if (libViewer) { libViewer.stop(); libViewer = null; } });

/* Sammlungen verwalten */
function libRenderCols() {
  $("#colList").innerHTML = LIB.cols.length ? LIB.cols.map(k => `<div class="col-row" data-cid="${k.id}">
      <span><b>${k.source === "makerworld" ? "◆ " : ""}${esc(k.name)}</b><small class="muted">${k.count} Modelle${k.source === "makerworld" ? ` · MakerWorld${k.private ? " (privat)" : ""}${k.synced_at ? " · " + fmtTime(k.synced_at) : ""}` : ""}</small></span>
      ${k.source === "makerworld" ? `<label class="inline small"><input type="checkbox" data-colsync ${k.sync ? "checked" : ""}> synchron.</label>` : `<button type="button" class="ghost mini" data-colren>umbenennen</button>`}
      <button type="button" class="ghost mini danger" data-coldel title="Sammlung entfernen (Modelle bleiben)">✕</button></div>`).join("")
    : `<p class="muted small">Noch keine Sammlungen.</p>`;
}
$("#libColsBtn").onclick = () => { libRenderCols(); $("#colDlg").showModal(); };
$("#colAdd").onclick = async () => {
  const n = $("#colNew").value.trim(); if (!n) return;
  await libApi("/api/library/collections", { method: "POST", body: JSON.stringify({ name: n }) });
  $("#colNew").value = ""; await libLoad(); libRenderCols(); toast("Sammlung angelegt");
};
$("#colNew").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); $("#colAdd").click(); } });
$("#colList").addEventListener("click", async e => {
  const row = e.target.closest(".col-row"); if (!row) return;
  const cid = +row.dataset.cid, k = LIB.cols.find(x => x.id === cid);
  if (e.target.matches("[data-coldel]")) {
    if (!confirm(`Sammlung „${k.name}“ entfernen? Die Modelle bleiben in der Bibliothek.${k.source === "makerworld" ? "\n\nDiese MakerWorld-Sammlung wird danach nicht mehr synchronisiert." : ""}`)) return;
    await libApi(`/api/library/collections/${cid}`, { method: "DELETE" });
  } else if (e.target.matches("[data-colren]")) {
    const n = prompt("Neuer Name:", k.name); if (!n) return;
    await libApi(`/api/library/collections/${cid}`, { method: "PUT", body: JSON.stringify({ name: n }) });
  } else return;
  await libLoad(); libRenderCols();
});
$("#colList").addEventListener("change", async e => {
  if (!e.target.matches("[data-colsync]")) return;
  const cid = +e.target.closest(".col-row").dataset.cid;
  await libApi(`/api/library/collections/${cid}`, { method: "PUT", body: JSON.stringify({ sync: e.target.checked }) });
  toast(e.target.checked ? "Wird synchronisiert" : "Wird nicht mehr synchronisiert");
});

/* MakerWorld */
async function libMwStatus(watch) {
  try { LIB.mw = await libApi("/api/makerworld/status"); } catch { return; }
  const s = LIB.mw, dot = $("#libMwDot");
  dot.className = "mw-dot " + (s.running ? "run" : s.error ? "bad" : s.last_sync ? "ok" : "");
  dot.title = s.running ? `${s.phase} ${s.done}/${s.total}` : s.error || (s.last_sync ? "zuletzt " + fmtTime(s.last_sync) : "");
  if ($("#mwDlg").open) libRenderMwDlg();
  clearTimeout(LIB.poll);
  if (s.running || watch) LIB.poll = setTimeout(() => libMwStatus(false).then(() => { if (!LIB.mw.running) { state.tab === "library" && libLoad(); if (LIB.cur && $("#libDlg").open) libOpen(LIB.cur.id); } }), 2000);
}
function libRenderMwDlg() {
  const s = LIB.mw; if (!s) return;
  const sum = s.summary;
  const parts = [];
  if (!s.connected) parts.push(`<p class="warn-box">Nicht mit dem Bambu-Konto verbunden. Unter <b>Bambu &amp; AMS → Drucker &amp; Konto</b> anmelden – dann werden deine Sammlungen (auch private) synchronisiert und 3MF-Dateien geladen. Öffentliche Sammlungen lassen sich auch ohne Anmeldung übernehmen (ohne Dateien).</p>`);
  parts.push(s.running ? `<p><b>${esc(s.phase)}</b> ${s.total ? `${s.done} / ${s.total}` : ""}</p><div class="mw-bar"><i style="width:${s.total ? s.done / s.total * 100 : 10}%"></i></div>`
    : `<p class="small">${s.last_sync ? `Zuletzt synchronisiert: ${fmtTime(s.last_sync)}` : "Noch nicht synchronisiert."}${s.pending ? ` · ${s.pending} Dateien ausstehend` : ""}${s.failed ? ` · <span class="err">${s.failed} Downloads fehlgeschlagen</span>` : ""}</p>`);
  if (s.error) parts.push(`<p class="err small">⚠ ${esc(s.error)}</p>`);
  if (sum && !s.running) {
    parts.push(`<p class="muted small">Letzter Lauf: ${sum.collections} Sammlungen, ${sum.models} Modelle, ${sum.details} neu abgeglichen, ${sum.downloaded} Dateien geladen</p>`);
    if (sum.errors && sum.errors.length) parts.push(`<details class="small"><summary>${sum.errors.length} Hinweise</summary><ul>${sum.errors.slice(0, 20).map(x => `<li>${esc(x)}</li>`).join("")}</ul></details>`);
  }
  $("#mwStatus").innerHTML = parts.join("");
  $("#mwSyncNow").disabled = s.running || !s.connected;
}
$("#libMwBtn").onclick = async () => {
  await libMwStatus(false);
  const st = LIB.mw.settings;
  $("#mwAuto").checked = st.mw_auto_download; $("#mwTags").checked = st.mw_import_tags;
  $("#mwLimit").value = st.mw_download_limit; $("#mwHours").value = st.mw_sync_hours;
  libRenderMwDlg(); $("#mwDlg").showModal();
};
$("#mwForm").addEventListener("change", async e => {
  if (!e.target.matches("#mwAuto, #mwTags, #mwLimit, #mwHours")) return;
  await libApi("/api/makerworld/settings", { method: "PUT", body: JSON.stringify({ mw_auto_download: $("#mwAuto").checked, mw_import_tags: $("#mwTags").checked,
    mw_download_limit: +$("#mwLimit").value || 0, mw_sync_hours: +$("#mwHours").value || 0 }) });
  toast("Gespeichert");
});
$("#mwSyncNow").onclick = async () => {
  try { await libApi("/api/makerworld/sync", { method: "POST" }); toast("Sync gestartet"); libMwStatus(true); }
  catch (e) { toast("Fehler: " + e.message); }
};
$("#mwImport").onclick = async () => {
  const url = $("#mwUrl").value.trim(); if (!url) return;
  try { const r = await libApi("/api/makerworld/import", { method: "POST", body: JSON.stringify({ url }) });
    toast(r.kind === "collection" ? "Sammlung wird übernommen …" : "Modell wird übernommen …"); $("#mwUrl").value = ""; libMwStatus(true); }
  catch (e) { toast("Fehler: " + e.message); }
};
$("#mwDlg").addEventListener("close", () => { if (!LIB.mw?.running) clearTimeout(LIB.poll); });

if (state.tab === "library") libLoad().catch(() => {});
