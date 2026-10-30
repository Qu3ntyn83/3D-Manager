"""Einzelne physische Spulen (spool_units) je Inventar-Eintrag.

Ein Inventar-Eintrag (filaments) beschreibt das Produkt („Sunlu PLA+ Cherry Red“),
jede Zeile in spool_units eine echte Spule davon: OVP / offen / leer, Restgewicht,
Öffnungs- und Trocknungsdatum, Preis, RFID-Tag.
filaments.qty wird automatisch aus den nicht-leeren Spulen berechnet.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from flask import Blueprint, abort, jsonify, request

from bambu import conn, get_setting, set_setting

bp = Blueprint("units", __name__, url_prefix="/api/units")

SCHEMA = """
CREATE TABLE IF NOT EXISTS spool_units (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filament_id INTEGER NOT NULL,
  status TEXT DEFAULT 'sealed',            -- sealed | open | empty
  initial_g REAL,                          -- Netto-Gewicht voll
  remaining_g REAL,                        -- aktueller Rest (netto)
  remaining_src TEXT,                      -- full | ams | weighed | job | manual
  remaining_at TEXT,
  opened_at TEXT, emptied_at TEXT, dried_at TEXT,
  price REAL, purchased_at TEXT, shop TEXT,
  location TEXT, notes TEXT, created_at TEXT,
  spool INTEGER DEFAULT 1,                 -- mit Spule geliefert
  refill INTEGER DEFAULT 0                 -- Refill (ohne Spule)
);
CREATE INDEX IF NOT EXISTS spool_units_fil ON spool_units(filament_id, status);
CREATE TABLE IF NOT EXISTS unit_log (unit_id INTEGER, ts TEXT, kind TEXT, delta_g REAL, remaining_g REAL, ref TEXT);
CREATE INDEX IF NOT EXISTS unit_log_unit ON unit_log(unit_id, ts);
"""
UNIT_FIELDS = ("status", "initial_g", "remaining_g", "opened_at", "emptied_at", "dried_at",
               "price", "purchased_at", "shop", "location", "notes", "spool", "refill")
UNIT_BOOL = ("spool", "refill")


def _b(v) -> int:
    return 1 if v in (True, 1, "1", "true", "ja", "on") else 0


def now():
    return datetime.now().isoformat(timespec="seconds")


def today():
    return date.today().isoformat()


def init_schema():
    with conn() as c:
        c.executescript(SCHEMA)
        ucols = {r[1] for r in c.execute("PRAGMA table_info(spool_units)")}
        if "refill" not in ucols:
            # Spule/Refill gilt jetzt je Box – bisherigen Wert der Sorte auf alle ihre Spulen übertragen
            c.execute("ALTER TABLE spool_units ADD COLUMN spool INTEGER DEFAULT 1")
            c.execute("ALTER TABLE spool_units ADD COLUMN refill INTEGER DEFAULT 0")
            c.execute("""UPDATE spool_units SET
                           spool = COALESCE((SELECT f.spool FROM filaments f WHERE f.id = spool_units.filament_id), 1),
                           refill = COALESCE((SELECT f.refill FROM filaments f WHERE f.id = spool_units.filament_id), 0)""")
        have = {r[1] for r in c.execute("PRAGMA table_info(tags)")}
        if have and "unit_id" not in have:
            c.execute("ALTER TABLE tags ADD COLUMN unit_id INTEGER")
        if not get_setting(c, "units_v1"):
            migrate(c)
            set_setting(c, "units_v1", now())


def _fil(c, fid):
    return c.execute("SELECT * FROM filaments WHERE id=?", (fid,)).fetchone()


def default_initial(f) -> float:
    try:
        return float(f["net_weight_g"] or 1000)
    except (KeyError, IndexError, TypeError):
        return 1000.0


def migrate(c):
    """Einmalig: aus „Anzahl“ einzelne Spulen machen, vorhandene Tags verteilen, alte Drucke ins Verbrauchsprotokoll."""
    fresh = not c.execute("SELECT 1 FROM spool_units LIMIT 1").fetchone()
    for f in c.execute("SELECT * FROM filaments").fetchall():
        if c.execute("SELECT 1 FROM spool_units WHERE filament_id=?", (f["id"],)).fetchone():
            continue
        init = default_initial(f)
        status = "sealed" if f["sealed"] else "open"
        for i in range(int(f["qty"] or 0)):
            rem = init
            if status == "open" and i == 0 and f["weight_g"]:
                rem = float(f["weight_g"])
            c.execute("""INSERT INTO spool_units (filament_id, status, initial_g, remaining_g, remaining_src, remaining_at,
                         opened_at, price, location, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                      (f["id"], status, init, rem, "full" if rem == init else "manual", now(),
                       None, f["price"] if "price" in f.keys() else None, f["location"], now()))
    try:
        for t in c.execute("SELECT * FROM tags WHERE filament_ref IS NOT NULL AND unit_id IS NULL").fetchall():
            uid = pick_unit_for_tag(c, t["filament_ref"], t["tag_key"])
            if uid:
                c.execute("UPDATE tags SET unit_id=? WHERE tag_key=?", (uid, t["tag_key"]))
                if t["remain_pct"] is not None and t["remain_pct"] >= 0:
                    u = c.execute("SELECT initial_g FROM spool_units WHERE id=?", (uid,)).fetchone()
                    set_remaining(c, uid, (u["initial_g"] or 1000) * t["remain_pct"] / 100, "ams", "Migration aus AMS", kind="init")
    except Exception:  # noqa: BLE001 – tags-Tabelle evtl. noch nicht vorhanden
        pass
    if fresh:
        backfill_history(c)


def backfill_history(c):
    """Verbrauch der Drucke aus der Zeit vor der Spulen-Verwaltung für die Statistik übernehmen.

    Ändert keine Restmengen (die stecken schon im Bestand), Art „history“.
    """
    if get_setting(c, "history_backfill_v1"):
        return
    set_setting(c, "history_backfill_v1", now())
    try:
        jobs = c.execute("SELECT * FROM print_jobs WHERE usage IS NOT NULL AND usage!='[]'").fetchall()
    except Exception:  # noqa: BLE001
        return
    for j in jobs:
        try:
            usage = json.loads(j["usage"] or "[]")
        except ValueError:
            continue
        for e in usage:
            g = e.get("grams")
            if not isinstance(g, (int, float)) or g <= 0:
                continue
            t = c.execute("SELECT unit_id FROM tags WHERE tag_key=?", (e.get("tag_key"),)).fetchone() if e.get("tag_key") else None
            uid = t["unit_id"] if t and t["unit_id"] else None
            if not uid and e.get("filament_ref"):
                u = c.execute("SELECT id FROM spool_units WHERE filament_id=? ORDER BY status='empty', id LIMIT 1",
                              (e["filament_ref"],)).fetchone()
                uid = u["id"] if u else None
            if uid:
                c.execute("INSERT INTO unit_log (unit_id, ts, kind, delta_g, remaining_g, ref) VALUES (?,?,?,?,?,?)",
                          (uid, j["ended"] or j["started"] or now(), "history", -round(float(g), 1), None,
                           f"Druck „{j['name']}“ (vor Spulen-Verwaltung)"))


# ---------------------------------------------------------------- Kernfunktionen
def sync_qty(c, fid):
    n = c.execute("SELECT COUNT(*) FROM spool_units WHERE filament_id=? AND status!='empty'", (fid,)).fetchone()[0]
    sealed = c.execute("SELECT COUNT(*) FROM spool_units WHERE filament_id=? AND status='sealed'", (fid,)).fetchone()[0]
    c.execute("UPDATE filaments SET qty=?, sealed=? WHERE id=?", (n, 1 if sealed and sealed == n else 0, fid))
    # Spule/Refill der Sorte = Zusammenfassung der vorhandenen Boxen (für Suche, Filter, Kachel)
    if n > 0:
        r = c.execute("""SELECT MAX(spool) AS sp, MAX(refill) AS rf FROM spool_units
                         WHERE filament_id=? AND status!='empty'""", (fid,)).fetchone()
        c.execute("UPDATE filaments SET spool=?, refill=? WHERE id=?", (r["sp"] or 0, r["rf"] or 0, fid))
    if n > 0:
        c.execute("UPDATE filaments SET ordered_at=NULL WHERE id=? AND ordered_at IS NOT NULL AND ordered_at < ?",
                  (fid, c.execute("SELECT MAX(created_at) FROM spool_units WHERE filament_id=?", (fid,)).fetchone()[0] or ""))


def add_unit(c, fid, status="sealed", **kw) -> int:
    f = _fil(c, fid)
    if not f:
        abort(404)
    init = float(kw.get("initial_g") or default_initial(f))
    rem = kw.get("remaining_g")
    rem = init if rem in (None, "") else float(rem)
    # Spule/Refill: übergeben, sonst wie die zuletzt angelegte Box der Sorte, sonst Wert der Sorte
    last = c.execute("SELECT spool, refill FROM spool_units WHERE filament_id=? ORDER BY id DESC LIMIT 1", (fid,)).fetchone()
    ref = last or f
    spool = _b(kw["spool"]) if kw.get("spool") not in (None, "") else (ref["spool"] if ref["spool"] is not None else 1)
    refill = _b(kw["refill"]) if kw.get("refill") not in (None, "") else (ref["refill"] or 0)
    cur = c.execute("""INSERT INTO spool_units (filament_id, status, initial_g, remaining_g, remaining_src, remaining_at,
                       opened_at, price, purchased_at, shop, location, notes, created_at, spool, refill)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (fid, status, init, rem, "full", now(), today() if status == "open" else None,
                     kw.get("price") if kw.get("price") not in ("", None) else f["price"], kw.get("purchased_at") or today(),
                     kw.get("shop"), kw.get("location") or f["location"], kw.get("notes"), now(), spool, refill))
    sync_qty(c, fid)
    return cur.lastrowid


def log(c, uid, kind, delta, remaining, ref=None):
    c.execute("INSERT INTO unit_log (unit_id, ts, kind, delta_g, remaining_g, ref) VALUES (?,?,?,?,?,?)",
              (uid, now(), kind, round(delta, 1) if delta is not None else None, round(remaining, 1), ref))


def set_remaining(c, uid, grams, src, ref=None, kind=None):
    u = c.execute("SELECT * FROM spool_units WHERE id=?", (uid,)).fetchone()
    if not u:
        return
    grams = max(0.0, float(grams))
    log(c, uid, kind or src, grams - (u["remaining_g"] or 0), grams, ref)
    c.execute("UPDATE spool_units SET remaining_g=?, remaining_src=?, remaining_at=? WHERE id=?", (grams, src, now(), uid))


def consume(c, uid, grams, src, ref=None):
    """Verbrauch abbuchen (positiv = weniger Filament)."""
    u = c.execute("SELECT * FROM spool_units WHERE id=?", (uid,)).fetchone()
    if not u or not grams:
        return
    rem = max(0.0, (u["remaining_g"] if u["remaining_g"] is not None else (u["initial_g"] or 1000)) - float(grams))
    log(c, uid, src, -float(grams), rem, ref)
    upd = {"remaining_g": rem, "remaining_src": src, "remaining_at": now()}
    if u["status"] == "sealed":
        upd.update(status="open", opened_at=today())
    c.execute(f"UPDATE spool_units SET {','.join(k + '=?' for k in upd)} WHERE id=?", [*upd.values(), uid])
    if u["status"] == "sealed":
        sync_qty(c, u["filament_id"])


def pick_unit_for_tag(c, fid, tag_key) -> int | None:
    """Spule für einen Tag wählen: bisherige, sonst offene ohne Tag, sonst OVP öffnen, sonst neu."""
    cur = c.execute("""SELECT u.id FROM tags t JOIN spool_units u ON u.id=t.unit_id
                       WHERE t.tag_key=? AND u.filament_id=? AND u.status!='empty'""", (tag_key, fid)).fetchone()
    if cur:
        return cur["id"]
    taken = {r[0] for r in c.execute("SELECT unit_id FROM tags WHERE unit_id IS NOT NULL AND tag_key!=?", (tag_key,))}
    for status in ("open", "sealed"):
        for u in c.execute("SELECT * FROM spool_units WHERE filament_id=? AND status=? ORDER BY remaining_g, id", (fid, status)):
            if u["id"] not in taken:
                if status == "sealed":
                    c.execute("UPDATE spool_units SET status='open', opened_at=? WHERE id=?", (today(), u["id"]))
                    sync_qty(c, fid)
                return u["id"]
    return add_unit(c, fid, status="open")


def link_tag(c, tag_key, fid, unit_id=None):
    """Tag einer Spule zuordnen (oder lösen). Wird von ams.rfid_link aufgerufen."""
    if fid is None:
        c.execute("UPDATE tags SET unit_id=NULL WHERE tag_key=? OR upper(tray_uuid)=upper(?)", (tag_key, tag_key))
        return None
    uid = unit_id or pick_unit_for_tag(c, fid, tag_key)
    u = c.execute("SELECT initial_g FROM spool_units WHERE id=?", (uid,)).fetchone()
    c.execute("UPDATE tags SET unit_id=?, full_weight_g=? WHERE tag_key=? OR upper(tray_uuid)=upper(?)",
              (uid, int(u["initial_g"] or 1000), tag_key, tag_key))
    t = c.execute("SELECT remain_pct FROM tags WHERE tag_key=? OR upper(tray_uuid)=upper(?)", (tag_key, tag_key)).fetchone()
    if t and t["remain_pct"] is not None and t["remain_pct"] >= 0:
        set_remaining(c, uid, (u["initial_g"] or 1000) * t["remain_pct"] / 100, "ams", f"Tag {tag_key[:8]} zugeordnet", kind="init")
    return uid


def ams_delta(c, tag_key, delta_pct, full_g):
    """AMS meldet Abnahme um delta_pct % → Spule anteilig belasten."""
    t = c.execute("SELECT unit_id FROM tags WHERE tag_key=?", (tag_key,)).fetchone()
    if t and t["unit_id"] and delta_pct > 0:
        u = c.execute("SELECT initial_g FROM spool_units WHERE id=?", (t["unit_id"],)).fetchone()
        if u:
            consume(c, t["unit_id"], delta_pct / 100 * (u["initial_g"] or full_g or 1000), "ams", f"AMS −{delta_pct} %")


def adjust_filament(c, fid, delta, status="sealed", **kw):
    """+/- im Inventar: + legt eine OVP-Spule an, − markiert eine Spule als leer."""
    if delta > 0:
        for _ in range(delta):
            add_unit(c, fid, status, **kw)
    elif delta < 0:
        for _ in range(-delta):
            u = c.execute("""SELECT * FROM spool_units WHERE filament_id=? AND status!='empty'
                             ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, remaining_g, id LIMIT 1""", (fid,)).fetchone()
            if not u:
                break
            mark_empty(c, u["id"])
    sync_qty(c, fid)


def reconcile_qty(c, fid, target, status="sealed", **kw):
    cur = c.execute("SELECT COUNT(*) FROM spool_units WHERE filament_id=? AND status!='empty'", (fid,)).fetchone()[0]
    if target is not None and int(target) != cur:
        adjust_filament(c, fid, int(target) - cur, status, **kw)


def set_pack(c, fids, vals: dict):
    """Spule/Refill für alle vorhandenen (nicht leeren) Boxen der Sorten setzen."""
    vals = {k: _b(v) for k, v in vals.items() if k in UNIT_BOOL}
    if not vals or not fids:
        return
    q = ",".join("?" * len(fids))
    c.execute(f"UPDATE spool_units SET {','.join(k + '=?' for k in vals)} WHERE filament_id IN ({q}) AND status!='empty'",
              [*vals.values(), *fids])
    for fid in fids:
        sync_qty(c, fid)


def mark_empty(c, uid):
    u = c.execute("SELECT * FROM spool_units WHERE id=?", (uid,)).fetchone()
    if not u:
        return
    log(c, uid, "empty", -(u["remaining_g"] or 0), 0)
    c.execute("UPDATE spool_units SET status='empty', remaining_g=0, emptied_at=?, remaining_at=? WHERE id=?", (today(), now(), uid))
    c.execute("UPDATE tags SET unit_id=NULL, filament_ref=NULL, link_mode=NULL WHERE unit_id=?", (uid,))
    sync_qty(c, u["filament_id"])


def unit_out(r, tags=None) -> dict:
    d = dict(r)
    d["code"] = f"S{d['id']:04d}"
    init = d.get("initial_g") or 1000
    d["percent"] = round((d["remaining_g"] or 0) / init * 100) if d.get("remaining_g") is not None and init else None
    d["tag"] = (tags or {}).get(d["id"])
    return d


def units_by_filament(c) -> dict[int, list]:
    tags = {}
    try:
        for t in c.execute("SELECT tag_key, unit_id, in_ams, ams_id, tray_id FROM tags WHERE unit_id IS NOT NULL"):
            tags[t["unit_id"]] = {"key": t["tag_key"], "in_ams": bool(t["in_ams"]), "slot": [t["ams_id"], t["tray_id"]] if t["in_ams"] else None}
    except Exception:  # noqa: BLE001
        pass
    out: dict[int, list] = {}
    for r in c.execute("SELECT * FROM spool_units ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'sealed' THEN 1 ELSE 2 END, id"):
        out.setdefault(r["filament_id"], []).append(unit_out(r, tags))
    return out


# ---------------------------------------------------------------- API
@bp.get("")
def list_units():
    fid = request.args.get("filament_id", type=int)
    with conn() as c:
        allu = units_by_filament(c)
        if fid:
            return jsonify(allu.get(fid, []))
        return jsonify([u for lst in allu.values() for u in lst])


@bp.get("/<int:uid>")
def get_unit(uid):
    with conn() as c:
        for lst in units_by_filament(c).values():
            for u in lst:
                if u["id"] == uid:
                    log_rows = [dict(r) for r in c.execute("SELECT * FROM unit_log WHERE unit_id=? ORDER BY ts DESC LIMIT 50", (uid,))]
                    return jsonify({**u, "log": log_rows})
    abort(404)


@bp.post("")
def create_unit():
    b = request.get_json(force=True)
    with conn() as c:
        uid = add_unit(c, int(b["filament_id"]), b.get("status") or "sealed",
                       **{k: b.get(k) for k in ("initial_g", "remaining_g", "price", "purchased_at", "shop", "location", "notes",
                                                "spool", "refill")})
    return jsonify({"ok": True, "id": uid}), 201


@bp.route("/<int:uid>", methods=["PUT", "PATCH"])
def edit_unit(uid):
    b = request.get_json(force=True)
    with conn() as c:
        u = c.execute("SELECT * FROM spool_units WHERE id=?", (uid,)).fetchone()
        if not u:
            abort(404)
        vals = {k: (b[k] if b[k] != "" else None) for k in UNIT_FIELDS if k in b}
        for k in UNIT_BOOL:
            if k in vals:
                vals[k] = _b(vals[k])
        if vals.get("status") == "sealed" and u["status"] == "open":
            vals["opened_at"] = None  # versehentlich geöffnet → wieder OVP
        if "remaining_g" in vals and vals["remaining_g"] is not None:
            set_remaining(c, uid, vals.pop("remaining_g"), "manual", "manuell gesetzt")
        if vals.get("status") == "empty" and u["status"] != "empty":
            vals.pop("status")
            mark_empty(c, uid)
        if vals.get("status") == "open" and u["status"] == "sealed" and not vals.get("opened_at"):
            vals["opened_at"] = today()
        if vals:
            c.execute(f"UPDATE spool_units SET {','.join(k + '=?' for k in vals)} WHERE id=?", [*vals.values(), uid])
        sync_qty(c, u["filament_id"])
    return jsonify({"ok": True})


@bp.delete("/<int:uid>")
def delete_unit(uid):
    with conn() as c:
        u = c.execute("SELECT filament_id FROM spool_units WHERE id=?", (uid,)).fetchone()
        if not u:
            abort(404)
        c.execute("UPDATE tags SET unit_id=NULL WHERE unit_id=?", (uid,))
        c.execute("DELETE FROM spool_units WHERE id=?", (uid,))
        c.execute("DELETE FROM unit_log WHERE unit_id=?", (uid,))
        sync_qty(c, u["filament_id"])
    return "", 204


@bp.post("/<int:uid>/<action>")
def unit_action(uid, action):
    b = request.get_json(silent=True) or {}
    with conn() as c:
        u = c.execute("SELECT * FROM spool_units WHERE id=?", (uid,)).fetchone()
        if not u:
            abort(404)
        if action == "open":
            c.execute("UPDATE spool_units SET status='open', opened_at=COALESCE(opened_at, ?) WHERE id=?", (today(), uid))
        elif action == "empty":
            mark_empty(c, uid)
        elif action == "dry":
            c.execute("UPDATE spool_units SET dried_at=? WHERE id=?", (b.get("date") or today(), uid))
            log(c, uid, "dry", 0, u["remaining_g"] or 0, "getrocknet")
        elif action == "weigh":
            # gross_g = Spule inkl. Leerspule (Tara aus dem Inventar-Eintrag) oder direkt net_g
            f = _fil(c, u["filament_id"])
            if b.get("net_g") not in (None, ""):
                net = float(b["net_g"])
            elif b.get("gross_g") not in (None, ""):
                tare = float(b.get("tare_g") or f["tare_g"] or 0)
                net = float(b["gross_g"]) - tare
                if b.get("tare_g") not in (None, "") and not f["tare_g"]:
                    c.execute("UPDATE filaments SET tare_g=? WHERE id=?", (int(float(b["tare_g"])), f["id"]))
            else:
                abort(400)
            set_remaining(c, uid, net, "weighed", "gewogen")
            if u["status"] == "sealed" and net < (u["initial_g"] or 1000) - 5:
                c.execute("UPDATE spool_units SET status='open', opened_at=? WHERE id=?", (today(), uid))
        else:
            abort(404)
        sync_qty(c, u["filament_id"])
    return jsonify({"ok": True})
