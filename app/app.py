"""3D Manager – kleine Homelab-Webapp (Flask + SQLite)."""
import csv
import io
import json
import os
import sqlite3
import uuid
from datetime import date
from pathlib import Path

from flask import Flask, abort, g, jsonify, request, send_from_directory, Response

BASE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE / "data"))
DB_PATH = DATA_DIR / "inventar.db"
UPLOAD_DIR = DATA_DIR / "uploads"
ALLOWED_IMG = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

FIL_FIELDS = ["brand", "material", "color_name", "official_name", "sku", "qty", "sealed", "spool",
              "refill", "sample", "last_check", "location", "notes", "colors", "pattern",
              "swatch_source", "image", "weight_g", "min_qty", "buy_url", "manufacturer_url",
              "net_weight_g", "tare_g", "price", "profile", "ordered_at"]
MAT_FIELDS = ["qty", "category", "description", "thickness", "location", "notes", "min_qty",
              "kind", "manufacturer", "manufacturer_url", "buy_url", "price", "ordered_at"]
BOOL = {"sealed", "spool", "refill", "sample"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS filaments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  brand TEXT, material TEXT, color_name TEXT, official_name TEXT, sku TEXT,
  qty INTEGER DEFAULT 1, sealed INTEGER DEFAULT 0, spool INTEGER DEFAULT 1, refill INTEGER DEFAULT 0,
  sample INTEGER DEFAULT 0, last_check TEXT, location TEXT, notes TEXT,
  colors TEXT DEFAULT '["#888888FF"]', pattern TEXT DEFAULT 'solid', swatch_source TEXT DEFAULT 'manual',
  image TEXT, weight_g INTEGER, min_qty INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS spools (
  cloud_id INTEGER PRIMARY KEY, rfid TEXT, tray_id_name TEXT, filament_id TEXT, vendor TEXT, type TEXT, name TEXT,
  color TEXT, colors TEXT, net_weight INTEGER, total_net_weight INTEGER, status INTEGER, create_type TEXT, note TEXT,
  in_printer INTEGER DEFAULT 0, device_name TEXT, ams_id INTEGER, slot_id TEXT, cloud_updated INTEGER,
  filament_ref INTEGER REFERENCES filaments(id) ON DELETE SET NULL, link_mode TEXT, present INTEGER DEFAULT 1, last_seen TEXT, raw TEXT
);
CREATE INDEX IF NOT EXISTS spools_rfid ON spools(rfid);
CREATE TABLE IF NOT EXISTS materials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  qty INTEGER DEFAULT 1, category TEXT, description TEXT, thickness TEXT,
  location TEXT, notes TEXT, min_qty INTEGER DEFAULT 0
);
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    # Migrationen (neue Spalten in bestehenden Datenbanken)
    for table, cols in (("materials", {"kind": "TEXT DEFAULT 'material'", "manufacturer": "TEXT", "manufacturer_url": "TEXT", "buy_url": "TEXT"}),
                        ("materials", {"price": "REAL", "ordered_at": "TEXT"}),
                        ("filaments", {"buy_url": "TEXT", "manufacturer_url": "TEXT", "net_weight_g": "INTEGER DEFAULT 1000",
                                       "tare_g": "INTEGER", "price": "REAL", "profile": "TEXT", "ordered_at": "TEXT"})):
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    empty = conn.execute("SELECT COUNT(*) FROM filaments").fetchone()[0] == 0 and \
        conn.execute("SELECT COUNT(*) FROM materials").fetchone()[0] == 0
    seed = BASE / "seed.json"
    if empty and seed.exists() and os.environ.get("SEED", "0") == "1":
        data = json.loads(seed.read_text(encoding="utf-8"))
        _import(conn, data)
    conn.commit()
    conn.close()


def _clean(payload, fields):
    out = {}
    for f in fields:
        if f not in payload:
            continue
        v = payload[f]
        if f in BOOL:
            v = 1 if v in (True, 1, "1", "true", "ja") else 0
        elif f == "colors":
            v = json.dumps(v if isinstance(v, list) else [v])
        elif f == "profile":
            v = json.dumps(v) if isinstance(v, dict) else (v or None)
        elif f in ("price", "net_weight_g", "tare_g"):
            try:
                v = None if v in ("", None) else (float(v) if f == "price" else int(float(v)))
            except (TypeError, ValueError):
                v = None
        elif f in ("qty", "weight_g", "min_qty"):
            v = None if v in ("", None) else int(v)
        elif isinstance(v, str):
            v = v.strip()
        out[f] = v
    return out


def _row(r, kind):
    d = dict(r)
    if kind == "f":
        d["colors"] = json.loads(d["colors"] or "[]")
        for b in BOOL:
            d[b] = bool(d[b])
        try:
            d["profile"] = json.loads(d.get("profile") or "{}")
        except (TypeError, ValueError):
            d["profile"] = {}
    return d


def _import(conn, data, replace=False):
    if replace:
        conn.execute("DELETE FROM filaments")
        conn.execute("DELETE FROM materials")
    unit_cols = ("status", "initial_g", "remaining_g", "opened_at", "emptied_at", "dried_at", "price", "purchased_at", "shop", "location", "notes")
    for f in data.get("filaments", []):
        c = _clean(f, FIL_FIELDS)
        cur = conn.execute(f"INSERT INTO filaments ({','.join(c)}) VALUES ({','.join('?' * len(c))})", list(c.values()))
        for u in f.get("units") or []:
            vals = {k: u.get(k) for k in unit_cols if k in u}
            vals.update(filament_id=cur.lastrowid, remaining_src="manual", created_at=date.today().isoformat())
            conn.execute(f"INSERT INTO spool_units ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})", list(vals.values()))
    for m in data.get("materials", []):
        c = _clean(m, MAT_FIELDS)
        conn.execute(f"INSERT INTO materials ({','.join(c)}) VALUES ({','.join('?' * len(c))})", list(c.values()))


# ---------- generische CRUD-Helfer ----------
def _crud(table, fields, kind):
    def list_():
        rows = db().execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        out = [_row(r, kind) for r in rows]
        if table == "filaments":
            linked, tg = linked_spools(), linked_tags()
            with uconn() as c:
                ub = units.units_by_filament(c)
            for f in out:
                f["spools"] = linked.get(f["id"], [])
                f["tags"] = tg.get(f["id"], [])
                f["units"] = ub.get(f["id"], [])
        return jsonify(out)

    def create():
        c = _clean(request.get_json(force=True), fields)
        cur = db().execute(f"INSERT INTO {table} ({','.join(c)}) VALUES ({','.join('?' * len(c))})", list(c.values()))
        db().commit()
        if table == "filaments":
            with uconn() as uc:
                units.reconcile_qty(uc, cur.lastrowid, c.get("qty", 1))
        return get(cur.lastrowid), 201

    def get(item_id):
        r = db().execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        if not r:
            abort(404)
        d = _row(r, kind)
        if table == "filaments":
            d["spools"] = linked_spools(item_id).get(item_id, [])
            d["tags"] = linked_tags().get(item_id, [])
            with uconn() as c:
                d["units"] = units.units_by_filament(c).get(item_id, [])
        return jsonify(d)

    def update(item_id):
        c = _clean(request.get_json(force=True), fields)
        qty = c.pop("qty", None) if table == "filaments" else None
        if c:
            db().execute(f"UPDATE {table} SET {','.join(k + '=?' for k in c)} WHERE id=?", [*c.values(), item_id])
            db().commit()
        if qty is not None:
            with uconn() as uc:
                units.reconcile_qty(uc, item_id, qty)
        return get(item_id)

    def delete(item_id):
        if table == "filaments":
            r = db().execute("SELECT image FROM filaments WHERE id=?", (item_id,)).fetchone()
            if r and r["image"]:
                (UPLOAD_DIR / r["image"]).unlink(missing_ok=True)
        if table == "filaments":
            db().execute("UPDATE spools SET filament_ref=NULL, link_mode=NULL WHERE filament_ref=?", (item_id,))
            db().execute("UPDATE tags SET filament_ref=NULL, link_mode=NULL, unit_id=NULL WHERE filament_ref=?", (item_id,))
            db().execute("DELETE FROM unit_log WHERE unit_id IN (SELECT id FROM spool_units WHERE filament_id=?)", (item_id,))
            db().execute("DELETE FROM spool_units WHERE filament_id=?", (item_id,))
        db().execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
        db().commit()
        return "", 204

    def adjust(item_id):
        delta = int(request.get_json(force=True).get("delta", 0))
        if table == "filaments":
            with uconn() as uc:
                units.adjust_filament(uc, item_id, delta)
        else:
            db().execute(f"UPDATE {table} SET qty = MAX(0, qty + ?) WHERE id=?", (delta, item_id))
            if delta > 0:
                db().execute(f"UPDATE {table} SET ordered_at=NULL WHERE id=?", (item_id,))
            db().commit()
        return get(item_id)

    base = f"/api/{table}"
    app.add_url_rule(base, f"{table}_list", list_, methods=["GET"])
    app.add_url_rule(base, f"{table}_create", create, methods=["POST"])
    app.add_url_rule(f"{base}/<int:item_id>", f"{table}_get", get, methods=["GET"])
    app.add_url_rule(f"{base}/<int:item_id>", f"{table}_update", update, methods=["PUT", "PATCH"])
    app.add_url_rule(f"{base}/<int:item_id>", f"{table}_delete", delete, methods=["DELETE"])
    app.add_url_rule(f"{base}/<int:item_id>/adjust", f"{table}_adjust", adjust, methods=["POST"])


_crud("filaments", FIL_FIELDS, "f")
_crud("materials", MAT_FIELDS, "m")


# ---------- Farbkatalog ----------
_CATALOG = None


def catalog():
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = []
        from bambu import bambu_catalog
        _CATALOG += bambu_catalog()
        for name in ("community.json", "custom.json"):
            for p in (BASE / "catalog" / name, DATA_DIR / name):
                if p.exists():
                    _CATALOG += json.loads(p.read_text(encoding="utf-8"))
    return _CATALOG


@app.get("/api/catalog")
def catalog_search():
    q = (request.args.get("q") or "").lower().split()
    brand = (request.args.get("brand") or "").lower()
    sku = request.args.get("sku")
    res = []
    for e in catalog():
        if sku:
            if e["sku"] == sku:
                res.append(e)
            continue
        if brand and brand not in e["brand"].lower():
            continue
        hay = f'{e["brand"]} {e["type"]} {e["name_de"]} {e["name_en"]} {e["sku"]}'.lower()
        if all(t in hay for t in q):
            res.append(e)
        if len(res) >= 60:
            break
    return jsonify(res)


@app.get("/api/catalog/brands")
def catalog_brands():
    return jsonify(sorted({e["brand"] for e in catalog()}))


# ---------- Bilder (eigene Fotos vom Farbmuster) ----------
@app.post("/api/filaments/<int:item_id>/image")
def upload_image(item_id):
    f = request.files.get("file")
    if not f:
        abort(400)
    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_IMG:
        abort(415)
    old = db().execute("SELECT image FROM filaments WHERE id=?", (item_id,)).fetchone()
    if not old:
        abort(404)
    if old["image"]:
        (UPLOAD_DIR / old["image"]).unlink(missing_ok=True)
    name = f"{item_id}-{uuid.uuid4().hex[:8]}{ext}"
    f.save(UPLOAD_DIR / name)
    db().execute("UPDATE filaments SET image=? WHERE id=?", (name, item_id))
    db().commit()
    return jsonify({"image": name})


@app.delete("/api/filaments/<int:item_id>/image")
def delete_image(item_id):
    r = db().execute("SELECT image FROM filaments WHERE id=?", (item_id,)).fetchone()
    if r and r["image"]:
        (UPLOAD_DIR / r["image"]).unlink(missing_ok=True)
        db().execute("UPDATE filaments SET image=NULL WHERE id=?", (item_id,))
        db().commit()
    return "", 204


@app.get("/uploads/<path:name>")
def uploads(name):
    return send_from_directory(UPLOAD_DIR, name)


# ---------- Inventur, Export, Import ----------
@app.post("/api/inventur")
def inventur():
    body = request.get_json(force=True)
    ids = body.get("ids") or []
    day = body.get("date") or date.today().isoformat()
    table = "materials" if body.get("kind") == "materials" else "filaments"
    if table == "filaments":
        db().executemany("UPDATE filaments SET last_check=? WHERE id=?", [(day, i) for i in ids])
        db().commit()
    return jsonify({"updated": len(ids), "date": day})


@app.get("/api/export")
def export_json():
    data = {
        "filaments": [_row(r, "f") for r in db().execute("SELECT * FROM filaments ORDER BY id")],
        "materials": [_row(r, "m") for r in db().execute("SELECT * FROM materials ORDER BY id")],
    }
    unit_cols = ("status", "initial_g", "remaining_g", "opened_at", "emptied_at", "dried_at", "price", "purchased_at", "shop", "location", "notes")
    for f in data["filaments"]:
        f["units"] = [{k: r[k] for k in unit_cols} for r in db().execute("SELECT * FROM spool_units WHERE filament_id=? ORDER BY id", (f["id"],))]
    for f in data["filaments"] + data["materials"]:
        f.pop("id", None)
    return Response(json.dumps(data, ensure_ascii=False, indent=1), mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename=inventar-{date.today()}.json"})


@app.get("/api/export.csv")
def export_csv():
    kind = request.args.get("kind", "filaments")
    table = "materials" if kind == "materials" else "filaments"
    rows = [_row(r, "f" if table == "filaments" else "m") for r in db().execute(f"SELECT * FROM {table} ORDER BY id")]
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), delimiter=";")
        w.writeheader()
        for r in rows:
            if "colors" in r:
                r["colors"] = " ".join(r["colors"])
            w.writerow(r)
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={table}-{date.today()}.csv"})


@app.post("/api/import")
def import_json():
    data = request.get_json(force=True)
    replace = request.args.get("replace") == "1"
    conn = db()
    if replace:
        conn.execute("DELETE FROM spool_units")
    _import(conn, data, replace=replace)
    conn.commit()
    with uconn() as uc:
        units.migrate(uc)
    return jsonify({"ok": True})


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/s/<int:uid>")
def short_unit(uid):
    """Ziel der QR-Codes auf Spulen-Etiketten."""
    from flask import redirect
    return redirect(f"/?unit={uid}")


@app.get("/f/<int:fid>")
def short_filament(fid):
    from flask import redirect
    return redirect(f"/?fil={fid}")


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(app.static_folder, "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    r = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
    r.headers["Cache-Control"] = "no-cache"
    r.headers["Service-Worker-Allowed"] = "/"
    return r


@app.get("/healthz")
def health():
    return "ok"


from bambu import bp as bambu_bp, linked_spools, start_scheduler  # noqa: E402
from ams import bp as ams_bp, linked_tags, start as start_ams  # noqa: E402
from bambu import conn as uconn  # noqa: E402
import units  # noqa: E402
import insights  # noqa: E402
import tasks  # noqa: E402
import backup  # noqa: E402
import nfc  # noqa: E402
from bambu import ensure_catalog  # noqa: E402

app.register_blueprint(bambu_bp)
app.register_blueprint(ams_bp)
app.register_blueprint(units.bp)
app.register_blueprint(insights.bp)
app.register_blueprint(tasks.bp)
app.register_blueprint(backup.bp)
app.register_blueprint(nfc.bp)
init_db()
start_ams(app)
units.init_schema()
tasks.init_schema()
ensure_catalog(app)
backup.scheduler()
start_scheduler(app)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
