"""Erweiterte Statistik für einen frei wählbaren Zeitraum.

GET /api/stats/full?range=30d|90d|365d|year|all  oder  ?from=JJJJ-MM-TT&to=JJJJ-MM-TT  [&idle_days=90]

Quellen:
  unit_log     Verbrauch je Spule (AMS-Abnahme, Slicer-Korrektur, manuelle Zuordnung, alte Drucke)
  print_jobs   Drucke mit Dauer, Ergebnis und Verbrauch je Slot
  spool_units  Bestand, Wert, letzte Nutzung
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request

from bambu import conn
from insights import CONSUME_KINDS, price_per_g, setting

bp = Blueprint("stats", __name__, url_prefix="/api/stats")
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _dt(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def period(args, c) -> tuple[datetime, datetime, str]:
    now = datetime.now()
    end = now
    rng = args.get("range") or ""
    if args.get("from"):
        start = datetime.combine(date.fromisoformat(args["from"]), datetime.min.time())
        if args.get("to"):
            end = datetime.combine(date.fromisoformat(args["to"]), datetime.max.time())
        return start, end, "custom"
    if rng == "year":
        return datetime(now.year, 1, 1), end, rng
    if rng == "all":
        first = []
        for q in ("SELECT MIN(ts) FROM unit_log", "SELECT MIN(started) FROM print_jobs"):
            try:
                v = c.execute(q).fetchone()[0]
                if v:
                    first.append(_dt(v))
            except Exception:  # noqa: BLE001
                pass
        start = min([d for d in first if d] or [now - timedelta(days=30)])
        return datetime.combine(start.date(), datetime.min.time()), end, rng
    days = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}.get(rng, 90)
    return datetime.combine((now - timedelta(days=days - 1)).date(), datetime.min.time()), end, rng or "90d"


def bucket_kind(start, end) -> str:
    days = (end - start).days + 1
    return "day" if days <= 45 else "week" if days <= 200 else "month"


def bucket_key(d: datetime, kind: str) -> str:
    if kind == "day":
        return d.strftime("%Y-%m-%d")
    if kind == "week":
        monday = d - timedelta(days=d.weekday())
        return monday.strftime("%Y-%m-%d")
    return d.strftime("%Y-%m")


def buckets(start, end, kind) -> list[str]:
    out, d = [], start
    while d <= end:
        k = bucket_key(d, kind)
        if not out or out[-1] != k:
            out.append(k)
        d += timedelta(days=1)
    return out


def consumption(c, start, end, fils, units):
    rows = c.execute(f"""SELECT l.ts, -l.delta_g AS g, l.unit_id FROM unit_log l
                         WHERE l.kind IN ({','.join('?' * len(CONSUME_KINDS))}) AND l.ts BETWEEN ? AND ?""",
                     (*CONSUME_KINDS, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))).fetchall()
    out = []
    for r in rows:
        u = units.get(r["unit_id"])
        f = fils.get(u["filament_id"]) if u is not None else None
        ppg = price_per_g(u, f)
        out.append({"ts": _dt(r["ts"]), "g": r["g"] or 0, "fid": u["filament_id"] if u is not None else None,
                    "cost": (r["g"] or 0) * ppg if ppg is not None else 0.0, "priced": ppg is not None})
    return out


def jobs_in(c, start, end, fils, units):
    out = []
    try:
        rows = c.execute("""SELECT j.*, p.name AS printer FROM print_jobs j LEFT JOIN printers p ON p.id=j.printer_id
                            WHERE j.started BETWEEN ? AND ? AND j.ended IS NOT NULL ORDER BY j.started""",
                         (start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))).fetchall()
    except Exception:  # noqa: BLE001
        return out
    kwh = setting(c, "kwh_price")
    watt = setting(c, "printer_power_w")
    for j in rows:
        s, e = _dt(j["started"]), _dt(j["ended"])
        if not s or not e:
            continue
        hours = max(0.0, (e - s).total_seconds() / 3600)
        usage = [u for u in json.loads(j["usage"] or "[]") if not u.get("skipped") and (u.get("grams") or 0) > 0]
        grams, mat = 0.0, 0.0
        for u in usage:
            unit = units.get(u.get("unit_id"))
            fid = u.get("filament_ref") or (unit["filament_id"] if unit is not None else None)
            ppg = price_per_g(unit, fils.get(fid))
            grams += u["grams"]
            mat += u["grams"] * (ppg or 0)
        out.append({"id": j["id"], "name": j["name"] or "Druck", "printer": j["printer"] or f"Drucker {j['printer_id']}",
                    "printer_id": j["printer_id"], "start": s, "hours": hours, "result": j["result"] or "",
                    "ok": (j["result"] or "") == "fertig", "grams": grams, "material": mat, "energy": hours * watt / 1000 * kwh,
                    "usage": usage})
    return out


def summarize(cons, jobs):
    g = sum(x["g"] for x in cons)
    mat = sum(x["cost"] for x in cons)
    energy = sum(j["energy"] for j in jobs)
    return {"grams": round(g), "material": round(mat, 2), "energy": round(energy, 2), "cost": round(mat + energy, 2),
            "jobs": len(jobs), "hours": round(sum(j["hours"] for j in jobs), 1)}


@bp.get("/full")
def full():
    idle_days = int(request.args.get("idle_days", 90))
    with conn() as c:
        start, end, rng = period(request.args, c)
        now = datetime.now()
        fils = {r["id"]: r for r in c.execute("SELECT * FROM filaments")}
        units = {r["id"]: r for r in c.execute("SELECT * FROM spool_units")}
        cons = consumption(c, start, end, fils, units)
        jobs = jobs_in(c, start, end, fils, units)
        span = end - start
        pcons = consumption(c, start - span, start - timedelta(seconds=1), fils, units)
        pjobs = jobs_in(c, start - span, start - timedelta(seconds=1), fils, units)
        cur, prev = summarize(cons, jobs), summarize(pcons, pjobs)

        # ---------------------------------------------------------- Verlauf
        kind = bucket_kind(start, end)
        series = {k: {"key": k, "grams": 0.0, "cost": 0.0, "jobs": 0, "hours": 0.0, "failed": 0} for k in buckets(start, end, kind)}
        for x in cons:
            b = series.get(bucket_key(x["ts"], kind))
            if b:
                b["grams"] += x["g"]
                b["cost"] += x["cost"]
        for j in jobs:
            b = series.get(bucket_key(j["start"], kind))
            if b:
                b["jobs"] += 1
                b["hours"] += j["hours"]
                b["cost"] += j["energy"]
                b["failed"] += 0 if j["ok"] else 1

        # ---------------------------------------------------------- Material / Marke / Farbe
        by_mat, by_brand, by_fil = defaultdict(lambda: [0.0, 0.0]), defaultdict(lambda: [0.0, 0.0]), defaultdict(lambda: [0.0, 0.0])
        unpriced_g = 0.0
        for x in cons:
            f = fils.get(x["fid"])
            if f is None:
                continue
            for d, k in ((by_mat, f["material"]), (by_brand, f["brand"]), (by_fil, x["fid"])):
                d[k][0] += x["g"]
                d[k][1] += x["cost"]
            if not x["priced"]:
                unpriced_g += x["g"]
        rows = lambda d, n=10: [{"name": k, "grams": round(v[0]), "cost": round(v[1], 2)}  # noqa: E731
                                for k, v in sorted(d.items(), key=lambda kv: -kv[1][0])[:n] if v[0] > 0]
        top_fil = [{"id": fid, "name": fils[fid]["color_name"], "sub": f"{fils[fid]['brand']} · {fils[fid]['material']}",
                    "colors": json.loads(fils[fid]["colors"] or "[]"), "grams": round(v[0]), "cost": round(v[1], 2)}
                   for fid, v in sorted(by_fil.items(), key=lambda kv: -kv[1][0])[:10] if fid in fils and v[0] > 0]

        # ---------------------------------------------------------- Drucke
        ok = [j for j in jobs if j["ok"]]
        failed = [j for j in jobs if not j["ok"]]
        jl = lambda j: {"id": j["id"], "name": j["name"], "printer": j["printer"], "start": j["start"].isoformat(timespec="minutes"),  # noqa: E731
                        "hours": round(j["hours"], 2), "grams": round(j["grams"], 1), "cost": round(j["material"] + j["energy"], 2),
                        "ok": j["ok"], "result": j["result"]}
        multi = [j for j in jobs if len({tuple(u.get("slot") or [i]) for i, u in enumerate(j["usage"])}) > 1]
        prints = {
            "total": len(jobs), "ok": len(ok), "failed": len(failed),
            "success_rate": round(len(ok) / len(jobs) * 100) if jobs else None,
            "avg_grams": round(sum(j["grams"] for j in jobs) / len(jobs), 1) if jobs else 0,
            "avg_hours": round(sum(j["hours"] for j in jobs) / len(jobs), 2) if jobs else 0,
            "avg_cost": round(sum(j["material"] + j["energy"] for j in jobs) / len(jobs), 2) if jobs else 0,
            "waste_g": round(sum(j["grams"] for j in failed), 1),
            "waste_cost": round(sum(j["material"] + j["energy"] for j in failed), 2),
            "multi": len(multi),
            "avg_filaments": round(sum(max(1, len(j["usage"])) for j in jobs) / len(jobs), 2) if jobs else 0,
            "expensive": [jl(j) for j in sorted(jobs, key=lambda j: -(j["material"] + j["energy"]))[:5]],
            "longest": [jl(j) for j in sorted(jobs, key=lambda j: -j["hours"])[:5]],
            "recent_failed": [jl(j) for j in sorted(failed, key=lambda j: j["start"], reverse=True)[:5]],
        }

        # ---------------------------------------------------------- Drucker, Auslastung, Heatmap, Slots
        range_h = max(1.0, (min(end, now) - start).total_seconds() / 3600)
        printers = defaultdict(lambda: {"hours": 0.0, "jobs": 0, "failed": 0, "grams": 0.0})
        heat = [[0.0] * 24 for _ in range(7)]
        slots = defaultdict(float)
        for j in jobs:
            p = printers[j["printer"]]
            p["hours"] += j["hours"]
            p["jobs"] += 1
            p["failed"] += 0 if j["ok"] else 1
            p["grams"] += j["grams"]
            t, remaining = j["start"], j["hours"]
            while remaining > 1e-6:
                nxt = (t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
                step = min(remaining, (nxt - t).total_seconds() / 3600)
                heat[t.weekday()][t.hour] += step
                remaining -= step
                t = nxt
            for u in j["usage"]:
                s = u.get("slot")
                if s and len(s) >= 2:
                    aid, tid = int(s[0]), int(s[1])
                    lab = "Extern" if aid == 255 else (f"AMS HT {aid - 127}" if aid >= 128 else f"AMS {chr(65 + aid)}{tid + 1}")
                    slots[f"{j['printer']} · {lab}"] += u["grams"]
        printer_rows = [{"name": k, "hours": round(v["hours"], 1), "jobs": v["jobs"], "failed": v["failed"], "grams": round(v["grams"]),
                         "utilization": round(v["hours"] / range_h * 100, 1), "per_day": round(v["hours"] / (range_h / 24), 2)}
                        for k, v in sorted(printers.items(), key=lambda kv: -kv[1]["hours"])]

        # ---------------------------------------------------------- Lager: Wert je Marke, Ladenhüter
        last_use = {}
        for r in c.execute(f"SELECT unit_id, MAX(ts) AS ts FROM unit_log WHERE kind IN ({','.join('?' * len(CONSUME_KINDS))}) GROUP BY unit_id",
                           CONSUME_KINDS):
            last_use[r["unit_id"]] = _dt(r["ts"])
        stock_brand = defaultdict(lambda: {"kg": 0.0, "value": 0.0, "spools": 0, "unpriced": 0})
        idle = []
        for u in units.values():
            if u["status"] == "empty":
                continue
            f = fils.get(u["filament_id"])
            if f is None:
                continue
            ppg = price_per_g(u, f)
            rem = u["remaining_g"] or 0
            sb = stock_brand[f["brand"]]
            sb["kg"] += rem / 1000
            sb["spools"] += 1
            if ppg is None:
                sb["unpriced"] += 1
            else:
                sb["value"] += rem * ppg
            ref = last_use.get(u["id"]) or _dt(u["opened_at"]) or _dt(u["created_at"]) or _dt(u["purchased_at"])
            days = (now - ref).days if ref else None
            if days is not None and days >= idle_days:
                idle.append({"unit_id": u["id"], "code": f"S{u['id']:04d}", "fid": f["id"], "name": f["color_name"],
                             "sub": f"{f['brand']} · {f['material']}", "colors": json.loads(f["colors"] or "[]"),
                             "status": u["status"], "days": days, "remaining_g": round(rem), "value": round(rem * ppg, 2) if ppg else None,
                             "used": u["id"] in last_use})
        idle.sort(key=lambda x: -x["days"])

        delta = lambda a, b: None if not b else round((a - b) / b * 100)  # noqa: E731
        return jsonify({
            "currency": setting(c, "currency"), "range": rng,
            "from": start.date().isoformat(), "to": min(end, now).date().isoformat(), "bucket": kind,
            "kpi": {**cur, "prev": prev, "delta": {k: delta(cur[k], prev[k]) for k in ("grams", "cost", "jobs", "hours")},
                    "unpriced_g": round(unpriced_g)},
            "series": [{**b, "grams": round(b["grams"]), "cost": round(b["cost"], 2), "hours": round(b["hours"], 1)} for b in series.values()],
            "by_material": rows(by_mat), "by_brand": rows(by_brand), "top": top_fil,
            "prints": prints,
            "printers": printer_rows,
            "heatmap": {"days": WEEKDAYS, "hours": [[round(h, 2) for h in row] for row in heat]},
            "slots": [{"name": k, "grams": round(v)} for k, v in sorted(slots.items(), key=lambda kv: -kv[1])[:16]],
            "stock": [{"name": k, "kg": round(v["kg"], 2), "value": round(v["value"], 2), "spools": v["spools"], "unpriced": v["unpriced"]}
                      for k, v in sorted(stock_brand.items(), key=lambda kv: -kv[1]["value"] - kv[1]["kg"])],
            "idle": idle[:30], "idle_days": idle_days, "idle_total": len(idle),
            "idle_value": round(sum(x["value"] or 0 for x in idle), 2),
        })
