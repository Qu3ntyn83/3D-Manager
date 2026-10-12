"""Grammgenauer Verbrauch aus den Bambu-Druckaufträgen (Cloud: /v1/user-service/my/tasks).

Jeder Auftrag enthält pro AMS-Slot die geplanten Gramm aus dem Slicer (amsDetailMapping).
Das ist genauer als die AMS-Restmenge (1-%-Schritte) und funktioniert auch für Spulen ohne Tag.

Ablauf je abgeschlossenem Auftrag (einmalig, nur Aufträge nach Aktivierung):
  1. passenden lokalen Druck (print_jobs) finden – gleicher Drucker, Start ±20 min
  2. Gramm × Faktor (abgebrochen: Fortschritt beim Abbruch)
  3. Slot → Spule über den Slot-Schnappschuss bei Druckstart
     - Spule mit Tag: Differenz Slicer − AMS-Schätzung nachbuchen (Korrektur)
     - Spule ohne Tag: als „nicht zugeordnet“ markieren, Zuordnung per Klick
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, abort, jsonify, request

from bambu import conn, get_setting, set_setting

bp = Blueprint("tasks", __name__, url_prefix="/api/jobs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cloud_tasks (
  id INTEGER PRIMARY KEY, device_id TEXT, title TEXT, status INTEGER, start TEXT, end TEXT,
  weight REAL, mapping TEXT, cover TEXT, job_id INTEGER, booked INTEGER DEFAULT 0, note TEXT, raw TEXT
);
"""
TASK_FINISHED, TASK_FAILED = 2, 3


def init_schema():
    with conn() as c:
        c.executescript(SCHEMA)


def _local(ts: str | None) -> datetime | None:
    """Cloud-Zeit (UTC, ...Z) → lokale naive Zeit wie in print_jobs."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    except ValueError:
        return None


def slot_of(item) -> str | None:
    """amsDetailMapping.ams ist ein flacher Index: Einheit*4+Slot (0–15), AMS HT 16–23, extern 254/255."""
    a = item.get("ams")
    if isinstance(a, bool) or not isinstance(a, int):
        return None
    if 0 <= a < 16:
        return f"{a // 4}:{a % 4}"
    if 16 <= a < 24:
        return f"{128 + a - 16}:0"
    if a in (254, 255):
        return "255:255"
    return None


def fetch_tasks(client) -> list[dict]:
    mock = os.environ.get("BAMBU_MOCK_TASKS")
    if mock:
        return json.loads(Path(mock).read_text(encoding="utf-8"))
    r = client._req("get", f"{client.api}/v1/user-service/my/tasks?limit=50")
    if r.status_code != 200:
        return []
    return [h for h in (client._json(r).get("hits") or []) if isinstance(h, dict)]


def sync(c, client) -> dict:
    """Aufträge laden, speichern und buchen. Wird vom Bambu-Sync aufgerufen."""
    c.executescript(SCHEMA)
    tasks = fetch_tasks(client)
    since = get_setting(c, "tasks_since")
    if not since:  # erst ab jetzt buchen – alte Aufträge nicht rückwirkend abziehen
        since = datetime.now().isoformat(timespec="seconds")
        set_setting(c, "tasks_since", since)
    for t in tasks:
        if not isinstance(t.get("id"), int):
            continue
        c.execute("""INSERT INTO cloud_tasks (id, device_id, title, status, start, end, weight, mapping, cover, raw)
                     VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, end=excluded.end,
                     weight=excluded.weight, mapping=excluded.mapping, title=excluded.title""",
                  (t["id"], (t.get("deviceId") or "").upper(), t.get("title") or t.get("designTitle"), t.get("status"),
                   t.get("startTime"), t.get("endTime"), t.get("weight"), json.dumps(t.get("amsDetailMapping") or []),
                   t.get("cover"), json.dumps(t, ensure_ascii=False)[:20000]))
    booked = 0
    for t in c.execute("SELECT * FROM cloud_tasks WHERE booked=0 AND status IN (?,?)", (TASK_FINISHED, TASK_FAILED)).fetchall():
        end = _local(t["end"])
        if not end or end.isoformat() <= since:
            continue
        if book(c, t):
            booked += 1
    return {"tasks": len(tasks), "booked": booked}


def book(c, t) -> bool:
    printer = c.execute("SELECT id FROM printers WHERE upper(serial)=?", (t["device_id"],)).fetchone()
    if not printer:
        c.execute("UPDATE cloud_tasks SET booked=1, note='Drucker nicht eingerichtet' WHERE id=?", (t["id"],))
        return False
    start, end = _local(t["start"]), _local(t["end"])
    job = c.execute("""SELECT * FROM print_jobs WHERE printer_id=? AND started BETWEEN ? AND ? AND task_id IS NULL
                       ORDER BY ABS(julianday(started) - julianday(?)) LIMIT 1""",
                    (printer["id"], (start - timedelta(minutes=20)).isoformat(), (start + timedelta(minutes=20)).isoformat(),
                     start.isoformat())).fetchone() if start else None
    if job and job["ended"] is None:
        return False  # lokaler Druck noch offen → beim nächsten Sync
    if not job:
        cur = c.execute("""INSERT INTO print_jobs (printer_id, job_key, name, started, ended, result, usage, usage_src, end_percent)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (printer["id"], f"task:{t['id']}", t["title"], start.isoformat() if start else None,
                         end.isoformat() if end else None, "fertig" if t["status"] == TASK_FINISHED else "abgebrochen",
                         "[]", "slicer", 100 if t["status"] == TASK_FINISHED else None))
        job = c.execute("SELECT * FROM print_jobs WHERE id=?", (cur.lastrowid,)).fetchone()
    factor = 1.0
    if t["status"] != TASK_FINISHED:
        pct = job["end_percent"] if job["end_percent"] is not None else job["last_percent"]
        if pct is None:
            c.execute("UPDATE cloud_tasks SET booked=1, job_id=?, note='abgebrochen, Fortschritt unbekannt' WHERE id=?", (job["id"], t["id"]))
            return False
        factor = max(0.0, min(1.0, pct / 100))
    slots = json.loads(job["slots"] or "{}") if "slots" in job.keys() else {}
    ams_usage = {u.get("tag_key"): u for u in json.loads(job["usage"] or "[]") if u.get("tag_key")}
    # Ohne Schnappschuss (ältere Drucke): Slot aus dem AMS-Verbrauch ableiten
    ams_by_slot = {f"{u['slot'][0]}:{u['slot'][1]}": u for u in ams_usage.values() if u.get("slot")}
    import units
    usage = []
    for item in json.loads(t["mapping"] or "[]"):
        try:
            grams = round(float(item.get("weight") or 0) * factor, 1)
        except (TypeError, ValueError):
            continue
        if grams <= 0:
            continue
        key = slot_of(item)
        snap = slots.get(key or "") or {k: ams_by_slot.get(key or "", {}).get(k) for k in ("tag_key", "unit_id", "filament_ref")}
        entry = {"slot": [int(x) for x in key.split(":")] if key else None, "grams": grams, "source": "slicer",
                 "bambu": {"type": item.get("filamentType") or item.get("targetFilamentType"), "color": item.get("sourceColor"),
                           "filament_id": item.get("filamentId")},
                 "tag_key": snap.get("tag_key"), "unit_id": snap.get("unit_id"), "filament_ref": snap.get("filament_ref")}
        ams = ams_usage.pop(snap.get("tag_key"), None) if snap.get("tag_key") else None
        if entry["unit_id"]:
            ams_g = ams["grams"] if ams else 0.0
            entry["ams_g"] = ams_g
            corr = round(grams - ams_g, 1)
            if abs(corr) >= 0.5:
                units.consume(c, entry["unit_id"], corr, "slicer", f"Druck „{t['title']}“: Slicer {grams} g, AMS {ams_g} g")
        else:
            entry["unassigned"] = True
        usage.append(entry)
    # AMS-Verbrauch ohne Slicer-Eintrag (sollte selten sein) behalten
    usage += list(ams_usage.values())
    c.execute("UPDATE print_jobs SET usage=?, usage_src='slicer', task_id=? WHERE id=?", (json.dumps(usage), t["id"], job["id"]))
    c.execute("UPDATE cloud_tasks SET booked=1, job_id=? WHERE id=?", (job["id"], t["id"]))
    return True


@bp.post("/<int:job_id>/assign")
def assign(job_id):
    """Slicer-Gramm eines Slots ohne Tag manuell einer Spule zuordnen (und abbuchen)."""
    b = request.get_json(force=True)
    idx = int(b.get("index", -1))
    import units
    with conn() as c:
        job = c.execute("SELECT * FROM print_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            abort(404)
        usage = json.loads(job["usage"] or "[]")
        if not 0 <= idx < len(usage):
            abort(400)
        e = usage[idx]
        if e.get("unit_id") and not e.get("unassigned"):
            abort(409)
        uid = b.get("unit_id")
        fid = b.get("filament_id")
        if not uid and fid:
            u = c.execute("""SELECT id FROM spool_units WHERE filament_id=? AND status!='empty'
                             ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, remaining_g LIMIT 1""", (fid,)).fetchone()
            uid = u["id"] if u else units.add_unit(c, int(fid), "open")
        if not uid:
            abort(400)
        u = c.execute("SELECT filament_id FROM spool_units WHERE id=?", (uid,)).fetchone()
        units.consume(c, uid, e["grams"], "job", f"Druck „{job['name']}“ (manuell zugeordnet)")
        e.update(unit_id=uid, filament_ref=u["filament_id"], unassigned=False)
        c.execute("UPDATE print_jobs SET usage=? WHERE id=?", (json.dumps(usage), job_id))
    return jsonify({"ok": True})


@bp.post("/<int:job_id>/skip")
def skip(job_id):
    """Nicht zugeordnete Einträge verwerfen (z. B. Spule nicht im Inventar)."""
    with conn() as c:
        job = c.execute("SELECT usage FROM print_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            abort(404)
        usage = [dict(e, unassigned=False, skipped=True) if e.get("unassigned") else e for e in json.loads(job["usage"] or "[]")]
        c.execute("UPDATE print_jobs SET usage=? WHERE id=?", (json.dumps(usage), job_id))
    return jsonify({"ok": True})
