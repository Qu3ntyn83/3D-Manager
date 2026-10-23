"""Bibliothek für Druckdateien (3MF, STL): Modelle mit Dateien, Tags und Sammlungen.

Ablage: LIBRARY_DIR (Standard <DATA_DIR>/library) – je Datei <id>.<ext>, Vorschau <id>.png.
Der Originalname steht in der Datenbank; Dateinamen vom Browser werden nie als Pfad verwendet.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from flask import Blueprint, abort, jsonify, request, send_file

from bambu import DATA_DIR, conn

bp = Blueprint("library", __name__, url_prefix="/api/library")
LIB_DIR = Path(os.environ.get("LIBRARY_DIR", DATA_DIR / "library"))
FILE_DIR, THUMB_DIR = LIB_DIR / "files", LIB_DIR / "thumbs"
KINDS = {".3mf": "3mf", ".stl": "stl"}
MAX_FILE = int(os.environ.get("LIBRARY_MAX_MB", "500")) * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS lib_models (
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, description TEXT, notes TEXT,
  source TEXT DEFAULT 'upload', creator TEXT, license TEXT, cover TEXT,
  mw_design_id INTEGER UNIQUE, mw_model_id TEXT, mw_url TEXT, mw_cover_url TEXT, mw_profile_id INTEGER, mw_instance_title TEXT,
  mw_synced_at TEXT, mw_download TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS lib_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT, model_id INTEGER NOT NULL, name TEXT NOT NULL, kind TEXT, size INTEGER,
  sha1 TEXT, meta TEXT, has_thumb INTEGER DEFAULT 0, source TEXT DEFAULT 'upload', created_at TEXT
);
CREATE INDEX IF NOT EXISTS lib_files_model ON lib_files(model_id);
CREATE TABLE IF NOT EXISTS lib_tags (model_id INTEGER, tag TEXT, PRIMARY KEY (model_id, tag));
CREATE INDEX IF NOT EXISTS lib_tags_tag ON lib_tags(tag);
CREATE TABLE IF NOT EXISTS lib_collections (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT, source TEXT DEFAULT 'local',
  mw_collection_id INTEGER UNIQUE, mw_cover_url TEXT, mw_private INTEGER DEFAULT 0, sync INTEGER DEFAULT 1,
  synced_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS lib_collection_models (collection_id INTEGER, model_id INTEGER, position INTEGER DEFAULT 0,
  PRIMARY KEY (collection_id, model_id));
"""


def now():
    return datetime.now().isoformat(timespec="seconds")


def init_schema():
    FILE_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)


def norm_tag(t: str) -> str:
    t = re.sub(r"\s+", " ", str(t or "").strip().lower().lstrip("#"))
    return t[:40]


def set_tags(c, mid: int, tags, replace=True):
    clean = []
    for t in tags or []:
        t = norm_tag(t)
        if t and t not in clean:
            clean.append(t)
    if replace:
        c.execute("DELETE FROM lib_tags WHERE model_id=?", (mid,))
    for t in clean:
        c.execute("INSERT OR IGNORE INTO lib_tags (model_id, tag) VALUES (?,?)", (mid, t))


def file_path(fid: int, kind: str) -> Path:
    return FILE_DIR / f"{fid}.{kind}"


def thumb_path(fid: int) -> Path:
    return THUMB_DIR / f"{fid}.png"


def cover_path(mid: int) -> Path:
    return THUMB_DIR / f"m{mid}.png"


# ---------------------------------------------------------------- Dateien
def add_file(c, mid: int, name: str, data: bytes, source="upload") -> int:
    ext = Path(name).suffix.lower()
    kind = KINDS.get(ext)
    if not kind:
        abort(400, f"Nur 3MF und STL: {name}")
    if len(data) > MAX_FILE:
        abort(413, f"{name} ist größer als {MAX_FILE // 1024 // 1024} MB")
    sha = hashlib.sha1(data).hexdigest()
    dup = c.execute("SELECT id FROM lib_files WHERE model_id=? AND sha1=?", (mid, sha)).fetchone()
    if dup:
        return dup["id"]
    safe = re.sub(r"[\\/\x00-\x1f]", "_", Path(name).name)[:180] or f"datei.{kind}"
    cur = c.execute("INSERT INTO lib_files (model_id, name, kind, size, sha1, source, created_at) VALUES (?,?,?,?,?,?,?)",
                    (mid, safe, kind, len(data), sha, source, now()))
    fid = cur.lastrowid
    file_path(fid, kind).write_bytes(data)
    c.execute("UPDATE lib_models SET updated_at=? WHERE id=?", (now(), mid))
    _analyze_later(fid)
    return fid


_jobs: list[int] = []
_cv = threading.Condition()


def _analyze_later(fid: int):
    with _cv:
        _jobs.append(fid)
        _cv.notify()


def analyze_now(fid: int):
    import meshthumb
    with conn() as c:
        f = c.execute("SELECT * FROM lib_files WHERE id=?", (fid,)).fetchone()
    if not f or not file_path(fid, f["kind"]).exists():
        return
    try:
        meta, png = meshthumb.analyze(file_path(fid, f["kind"]).read_bytes(), f["kind"])
    except Exception as e:  # noqa: BLE001
        meta, png = {"error": str(e)[:200]}, None
    if png:
        thumb_path(fid).write_bytes(png)
    with conn() as c:
        c.execute("UPDATE lib_files SET meta=?, has_thumb=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), 1 if png else 0, fid))
        m = c.execute("SELECT * FROM lib_models WHERE id=?", (f["model_id"],)).fetchone()
        if m and not m["cover"] and png:
            c.execute("UPDATE lib_models SET cover=? WHERE id=?", (f"f{fid}", m["id"]))
        # Titel/Designer aus der 3MF übernehmen, wenn der Titel nur der Dateiname war
        if m and m["source"] == "upload" and meta.get("title") and m["title"] == Path(f["name"]).stem:
            c.execute("UPDATE lib_models SET title=? WHERE id=?", (meta["title"][:200], m["id"]))
        if m and not m["creator"] and meta.get("designer"):
            c.execute("UPDATE lib_models SET creator=? WHERE id=?", (meta["designer"][:120], m["id"]))


def worker():
    def loop():
        while True:
            with _cv:
                while not _jobs:
                    _cv.wait()
                fid = _jobs.pop(0)
            try:
                analyze_now(fid)
            except Exception as e:  # noqa: BLE001
                print("Bibliothek-Analyse:", e, flush=True)
    threading.Thread(target=loop, daemon=True, name="library-analyze").start()
    with conn() as c:  # nach Neustart: nicht ausgewertete Dateien nachholen
        for r in c.execute("SELECT id FROM lib_files WHERE meta IS NULL"):
            _analyze_later(r["id"])


def delete_model_files(c, mid):
    for f in c.execute("SELECT id, kind FROM lib_files WHERE model_id=?", (mid,)).fetchall():
        file_path(f["id"], f["kind"]).unlink(missing_ok=True)
        thumb_path(f["id"]).unlink(missing_ok=True)
    cover_path(mid).unlink(missing_ok=True)
    c.execute("DELETE FROM lib_files WHERE model_id=?", (mid,))


# ---------------------------------------------------------------- Ausgabe
def file_out(f) -> dict:
    meta = json.loads(f["meta"] or "{}") if f["meta"] else None
    plates = (meta or {}).get("plates") or []
    return {"id": f["id"], "name": f["name"], "kind": f["kind"], "size": f["size"], "source": f["source"],
            "created_at": f["created_at"], "thumb": f"/api/library/files/{f['id']}/thumb" if f["has_thumb"] else None,
            "url": f"/api/library/files/{f['id']}/{_urlname(f['name'])}", "meta": meta, "analyzing": f["meta"] is None,
            "seconds": sum(p.get("seconds") or 0 for p in plates) or None, "grams": round(sum(p.get("grams") or 0 for p in plates), 1) or None}


def _urlname(name: str) -> str:
    from urllib.parse import quote
    return quote(name)


def cover_url(m) -> str | None:
    cv = m["cover"] or ""
    if cv.startswith("f"):
        return f"/api/library/files/{cv[1:]}/thumb"
    if cv == "own":
        return f"/api/library/models/{m['id']}/cover?v={(m['updated_at'] or '')[-8:]}"
    return m["mw_cover_url"] or None


def model_out(c, m, full=False) -> dict:
    tags = [r["tag"] for r in c.execute("SELECT tag FROM lib_tags WHERE model_id=? ORDER BY tag", (m["id"],))]
    cols = [dict(r) for r in c.execute("""SELECT k.id, k.name, k.source FROM lib_collection_models cm JOIN lib_collections k ON k.id=cm.collection_id
                                         WHERE cm.model_id=? ORDER BY k.name""", (m["id"],))]
    files = c.execute("SELECT * FROM lib_files WHERE model_id=? ORDER BY kind, name", (m["id"],)).fetchall()
    d = {"id": m["id"], "title": m["title"], "source": m["source"], "creator": m["creator"], "cover": cover_url(m),
         "tags": tags, "collections": cols, "files": len(files), "kinds": sorted({f["kind"] for f in files}),
         "size": sum(f["size"] or 0 for f in files), "mw_url": m["mw_url"], "mw_download": m["mw_download"],
         "created_at": m["created_at"], "updated_at": m["updated_at"]}
    if full:
        d.update(description=m["description"], notes=m["notes"], license=m["license"], mw_design_id=m["mw_design_id"],
                 mw_instance_title=m["mw_instance_title"], mw_synced_at=m["mw_synced_at"],
                 file_list=[file_out(f) for f in files])
    return d


# ---------------------------------------------------------------- API: Modelle
@bp.get("/models")
def list_models():
    q = (request.args.get("q") or "").strip().lower()
    tags = [norm_tag(t) for t in request.args.getlist("tag") if norm_tag(t)]
    col = request.args.get("collection", type=int)
    src = request.args.get("source")
    sort = request.args.get("sort", "updated")
    with conn() as c:
        sql, args = "SELECT m.* FROM lib_models m WHERE 1=1", []
        if col:
            sql += " AND m.id IN (SELECT model_id FROM lib_collection_models WHERE collection_id=?)"
            args.append(col)
        if src:
            sql += " AND m.source=?"
            args.append(src)
        for t in tags:
            sql += " AND m.id IN (SELECT model_id FROM lib_tags WHERE tag=?)"
            args.append(t)
        for word in q.split():
            like = f"%{word}%"
            sql += """ AND (lower(m.title) LIKE ? OR lower(coalesce(m.creator,'')) LIKE ? OR lower(coalesce(m.description,'')) LIKE ?
                       OR lower(coalesce(m.notes,'')) LIKE ? OR m.id IN (SELECT model_id FROM lib_tags WHERE tag LIKE ?)
                       OR m.id IN (SELECT model_id FROM lib_files WHERE lower(name) LIKE ?))"""
            args += [like] * 6
        sql += {"title": " ORDER BY lower(m.title)", "created": " ORDER BY m.created_at DESC"}.get(sort, " ORDER BY m.updated_at DESC")
        rows = c.execute(sql, args).fetchall()
        return jsonify([model_out(c, m) for m in rows])


@bp.get("/models/<int:mid>")
def get_model(mid):
    with conn() as c:
        m = c.execute("SELECT * FROM lib_models WHERE id=?", (mid,)).fetchone()
        if not m:
            abort(404)
        return jsonify(model_out(c, m, full=True))


@bp.post("/models")
def create_model():
    """Upload (multipart): files[], title?, tags (komma-getrennt), collection?, separate=1 → je Datei ein Modell."""
    files = request.files.getlist("files")
    if not files:
        abort(400, "Keine Dateien")
    tags = [t for t in (request.form.get("tags") or "").split(",") if t.strip()]
    col = request.form.get("collection", type=int)
    separate = request.form.get("separate") == "1"
    groups = [[f] for f in files] if separate else [files]
    created = []
    with conn() as c:
        for grp in groups:
            title = (request.form.get("title") or "").strip() if not separate else ""
            title = title or Path(grp[0].filename or "Modell").stem
            cur = c.execute("INSERT INTO lib_models (title, source, created_at, updated_at) VALUES (?,?,?,?)", (title[:200], "upload", now(), now()))
            mid = cur.lastrowid
            for f in grp:
                add_file(c, mid, f.filename or "datei", f.read())
            set_tags(c, mid, tags)
            if col:
                c.execute("INSERT OR IGNORE INTO lib_collection_models (collection_id, model_id) VALUES (?,?)", (col, mid))
            created.append(mid)
    return jsonify({"ok": True, "ids": created}), 201


@bp.put("/models/<int:mid>")
def update_model(mid):
    b = request.get_json(force=True) or {}
    with conn() as c:
        if not c.execute("SELECT 1 FROM lib_models WHERE id=?", (mid,)).fetchone():
            abort(404)
        sets = {k: (b[k] or "").strip() or None for k in ("title", "description", "notes", "creator", "license") if k in b}
        if "title" in sets and not sets["title"]:
            abort(400, "Titel fehlt")
        if "cover_file" in b:
            sets["cover"] = f"f{int(b['cover_file'])}" if b["cover_file"] else None
        sets["updated_at"] = now()
        c.execute(f"UPDATE lib_models SET {','.join(k + '=?' for k in sets)} WHERE id=?", [*sets.values(), mid])
        if "tags" in b:
            set_tags(c, mid, b["tags"])
        if "collections" in b:
            c.execute("""DELETE FROM lib_collection_models WHERE model_id=? AND collection_id IN
                         (SELECT id FROM lib_collections WHERE source='local')""", (mid,))
            for cid in b["collections"] or []:
                k = c.execute("SELECT source FROM lib_collections WHERE id=?", (cid,)).fetchone()
                if k and k["source"] == "local":
                    c.execute("INSERT OR IGNORE INTO lib_collection_models (collection_id, model_id) VALUES (?,?)", (cid, mid))
        m = c.execute("SELECT * FROM lib_models WHERE id=?", (mid,)).fetchone()
        return jsonify(model_out(c, m, full=True))


@bp.delete("/models/<int:mid>")
def delete_model(mid):
    with conn() as c:
        delete_model_files(c, mid)
        c.execute("DELETE FROM lib_tags WHERE model_id=?", (mid,))
        c.execute("DELETE FROM lib_collection_models WHERE model_id=?", (mid,))
        c.execute("DELETE FROM lib_models WHERE id=?", (mid,))
    return "", 204


@bp.post("/models/<int:mid>/files")
def upload_files(mid):
    with conn() as c:
        if not c.execute("SELECT 1 FROM lib_models WHERE id=?", (mid,)).fetchone():
            abort(404)
        ids = [add_file(c, mid, f.filename or "datei", f.read()) for f in request.files.getlist("files")]
    return jsonify({"ok": True, "ids": ids}), 201


@bp.post("/models/<int:mid>/cover")
def upload_cover(mid):
    f = request.files.get("file")
    if not f:
        abort(400)
    from PIL import Image
    import io
    try:
        img = Image.open(io.BytesIO(f.read()))
        img.thumbnail((1024, 1024))
        img.save(cover_path(mid), "PNG")
    except Exception:  # noqa: BLE001
        abort(400, "Kein Bild")
    with conn() as c:
        c.execute("UPDATE lib_models SET cover='own', updated_at=? WHERE id=?", (now(), mid))
    return jsonify({"ok": True})


@bp.get("/models/<int:mid>/cover")
def get_cover(mid):
    p = cover_path(mid)
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="image/png", max_age=86400)


@bp.delete("/files/<int:fid>")
def delete_file(fid):
    with conn() as c:
        f = c.execute("SELECT * FROM lib_files WHERE id=?", (fid,)).fetchone()
        if not f:
            abort(404)
        file_path(fid, f["kind"]).unlink(missing_ok=True)
        thumb_path(fid).unlink(missing_ok=True)
        c.execute("DELETE FROM lib_files WHERE id=?", (fid,))
        c.execute("UPDATE lib_models SET cover=NULL WHERE id=? AND cover=?", (f["model_id"], f"f{fid}"))
    return "", 204


@bp.get("/files/<int:fid>/thumb")
def file_thumb(fid):
    p = thumb_path(fid)
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="image/png", max_age=86400)


@bp.get("/files/<int:fid>/<path:name>")
def file_download(fid, name):
    """Download unter dem Originalnamen (auch für „In Bambu Studio öffnen“). ?inline=1 für die 3D-Vorschau."""
    with conn() as c:
        f = c.execute("SELECT * FROM lib_files WHERE id=?", (fid,)).fetchone()
    if not f or not file_path(fid, f["kind"]).exists():
        abort(404)
    mime = "model/3mf" if f["kind"] == "3mf" else "model/stl"
    return send_file(file_path(fid, f["kind"]), mimetype=mime, as_attachment=not request.args.get("inline"),
                     download_name=f["name"], max_age=3600)


# ---------------------------------------------------------------- API: Tags & Sammlungen
@bp.get("/tags")
def list_tags():
    with conn() as c:
        return jsonify([dict(r) for r in c.execute("SELECT tag, COUNT(*) AS n FROM lib_tags GROUP BY tag ORDER BY n DESC, tag")])


@bp.post("/tags/rename")
def rename_tag():
    b = request.get_json(force=True) or {}
    old, new = norm_tag(b.get("old")), norm_tag(b.get("new"))
    with conn() as c:
        if new:
            c.execute("INSERT OR IGNORE INTO lib_tags (model_id, tag) SELECT model_id, ? FROM lib_tags WHERE tag=?", (new, old))
        c.execute("DELETE FROM lib_tags WHERE tag=?", (old,))
    return jsonify({"ok": True})


@bp.post("/models/bulk")
def bulk():
    """{ids, add_tags, remove_tags, add_collection, remove_collection, delete}"""
    b = request.get_json(force=True) or {}
    ids = [int(i) for i in b.get("ids") or []]
    with conn() as c:
        for mid in ids:
            if b.get("delete"):
                delete_model_files(c, mid)
                for t in ("lib_tags", "lib_collection_models"):
                    c.execute(f"DELETE FROM {t} WHERE model_id=?", (mid,))
                c.execute("DELETE FROM lib_models WHERE id=?", (mid,))
                continue
            set_tags(c, mid, b.get("add_tags") or [], replace=False)
            for t in b.get("remove_tags") or []:
                c.execute("DELETE FROM lib_tags WHERE model_id=? AND tag=?", (mid, norm_tag(t)))
            if b.get("add_collection"):
                c.execute("INSERT OR IGNORE INTO lib_collection_models (collection_id, model_id) VALUES (?,?)", (int(b["add_collection"]), mid))
            if b.get("remove_collection"):
                c.execute("DELETE FROM lib_collection_models WHERE collection_id=? AND model_id=?", (int(b["remove_collection"]), mid))
    return jsonify({"ok": True, "count": len(ids)})


@bp.get("/collections")
def list_collections():
    with conn() as c:
        rows = c.execute("""SELECT k.*, (SELECT COUNT(*) FROM lib_collection_models WHERE collection_id=k.id) AS n
                            FROM lib_collections k ORDER BY k.source DESC, lower(k.name)""").fetchall()
        return jsonify([{"id": r["id"], "name": r["name"], "description": r["description"], "source": r["source"], "count": r["n"],
                         "mw_collection_id": r["mw_collection_id"], "private": bool(r["mw_private"]), "sync": bool(r["sync"]),
                         "cover": r["mw_cover_url"], "synced_at": r["synced_at"]} for r in rows])


@bp.post("/collections")
def create_collection():
    b = request.get_json(force=True) or {}
    name = (b.get("name") or "").strip()
    if not name:
        abort(400, "Name fehlt")
    with conn() as c:
        cur = c.execute("INSERT INTO lib_collections (name, description, source, created_at) VALUES (?,?,?,?)",
                        (name[:120], (b.get("description") or "").strip() or None, "local", now()))
        return jsonify({"ok": True, "id": cur.lastrowid}), 201


@bp.put("/collections/<int:cid>")
def update_collection(cid):
    b = request.get_json(force=True) or {}
    with conn() as c:
        k = c.execute("SELECT * FROM lib_collections WHERE id=?", (cid,)).fetchone()
        if not k:
            abort(404)
        sets = {}
        if "sync" in b:
            sets["sync"] = 1 if b["sync"] else 0
        if k["source"] == "local":
            for f in ("name", "description"):
                if f in b:
                    sets[f] = (b[f] or "").strip() or None
        if sets:
            c.execute(f"UPDATE lib_collections SET {','.join(x + '=?' for x in sets)} WHERE id=?", [*sets.values(), cid])
    return jsonify({"ok": True})


@bp.delete("/collections/<int:cid>")
def delete_collection(cid):
    """Löscht nur die Sammlung, nicht die Modelle. MakerWorld-Sammlungen werden beim nächsten Sync nicht mehr geladen."""
    with conn() as c:
        k = c.execute("SELECT source, mw_collection_id FROM lib_collections WHERE id=?", (cid,)).fetchone()
        if k and k["source"] == "makerworld":
            from bambu import get_setting, set_setting
            ign = set(json.loads(get_setting(c, "mw_ignored_collections") or "[]")) | {k["mw_collection_id"]}
            set_setting(c, "mw_ignored_collections", json.dumps(sorted(ign)))
        c.execute("DELETE FROM lib_collection_models WHERE collection_id=?", (cid,))
        c.execute("DELETE FROM lib_collections WHERE id=?", (cid,))
    return "", 204


@bp.get("/stats")
def lib_stats():
    with conn() as c:
        r = c.execute("SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM lib_files").fetchone()
        return jsonify({"models": c.execute("SELECT COUNT(*) FROM lib_models").fetchone()[0], "files": r["n"], "bytes": r["bytes"],
                        "makerworld": c.execute("SELECT COUNT(*) FROM lib_models WHERE source='makerworld'").fetchone()[0],
                        "dir": str(LIB_DIR)})
