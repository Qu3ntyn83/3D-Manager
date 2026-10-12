"""Materialprofile, Einstellungen, Kosten, Einkaufsliste, Trocknung, Statistik."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from flask import Blueprint, abort, jsonify, request

from bambu import conn, get_setting, set_setting

bp = Blueprint("insights", __name__, url_prefix="/api")

# ---------------------------------------------------------------- Einstellungen
SETTINGS = {  # key: (Standard, Typ)
    "currency": ("€", str),
    "kwh_price": (0.35, float),
    "printer_power_w": (250, float),
    "low_stock_g": (250, float),
    "ams_humidity_warn": (40, float),
    "public_url": ("", str),
    "backup_hour": (3, int),
    "backup_keep_days": (14, int),
}


def setting(c, key):
    default, typ = SETTINGS[key]
    v = get_setting(c, "app_" + key)
    if v in (None, ""):
        return default
    try:
        return typ(v)
    except (TypeError, ValueError):
        return default


@bp.get("/settings")
def get_settings():
    with conn() as c:
        return jsonify({k: setting(c, k) for k in SETTINGS})


@bp.put("/settings")
def put_settings():
    b = request.get_json(force=True)
    with conn() as c:
        for k, v in b.items():
            if k in SETTINGS:
                set_setting(c, "app_" + k, "" if v is None else v)
        return jsonify({k: setting(c, k) for k in SETTINGS})


# ---------------------------------------------------------------- Materialprofile
PROFILE_FIELDS = ("nozzle_min", "nozzle_max", "bed_min", "bed_max", "dry_temp", "dry_hours", "open_days", "notes")
DEFAULT_PROFILES = {
    # Richtwerte – im Zweifel gilt das Datenblatt des Herstellers
    "PLA":     (190, 230, 55, 65, 55, 8, 90, ""),
    "PETG":    (230, 260, 70, 80, 65, 8, 30, "hygroskopisch"),
    "ABS":     (240, 270, 90, 100, 80, 8, 60, "Gehäuse empfohlen"),
    "ASA":     (240, 270, 90, 100, 80, 8, 60, "Gehäuse empfohlen"),
    "TPU":     (210, 240, 30, 50, 70, 8, 14, "langsam drucken, stark hygroskopisch"),
    "PA":      (260, 290, 80, 100, 80, 12, 7, "sehr hygroskopisch – nur trocken drucken"),
    "PC":      (260, 290, 100, 110, 80, 8, 14, "Gehäuse nötig"),
    "PVA":     (190, 220, 45, 60, 55, 8, 3, "extrem hygroskopisch, luftdicht lagern"),
    "SUPPORT": (210, 240, 55, 70, 55, 8, 30, ""),
    "SONSTIGE": (None, None, None, None, None, None, 60, ""),
}
FAMILY_LABEL = {"PLA": "PLA", "PETG": "PETG / PCTG", "ABS": "ABS", "ASA": "ASA", "TPU": "TPU / TPE", "PA": "PA / Nylon",
                "PC": "PC", "PVA": "PVA", "SUPPORT": "Support", "SONSTIGE": "Sonstige"}


def family(material: str | None) -> str:
    """Materialfamilie aus der Bezeichnung, z. B. „PETG-CF“ → PETG, „PA6-GF“ → PA."""
    m = (material or "").upper()
    toks = [t for t in re.split(r"[^A-Z0-9]+", m) if t]
    if "SUPPORT" in toks:
        return "SUPPORT"
    for t in toks:
        if t.startswith("PVA") or t == "BVOH":
            return "PVA"
        if t.startswith("TPU") or t.startswith("TPE"):
            return "TPU"
        if t.startswith("PETG") or t.startswith("PCTG") or t == "PET":
            return "PETG"
        if t.startswith("ASA"):
            return "ASA"
        if t.startswith("ABS"):
            return "ABS"
        if t.startswith("PLA"):
            return "PLA"
        if t.startswith("PA") or t.startswith("PPA") or t == "NYLON":
            return "PA"
        if t.startswith("PC"):
            return "PC"
    return "SONSTIGE"


PROFILES_SCHEMA = "CREATE TABLE IF NOT EXISTS material_profiles (family TEXT PRIMARY KEY, data TEXT)"


def profiles(c) -> dict:
    c.execute(PROFILES_SCHEMA)
    out = {}
    for fam, vals in DEFAULT_PROFILES.items():
        out[fam] = dict(zip(PROFILE_FIELDS, vals), family=fam, label=FAMILY_LABEL[fam])
    for r in c.execute("SELECT family, data FROM material_profiles"):
        if r["family"] in out:
            try:
                out[r["family"]].update({k: v for k, v in json.loads(r["data"]).items() if k in PROFILE_FIELDS})
            except ValueError:
                pass
    return out


def effective_profile(c, f, profs=None) -> dict:
    profs = profs or profiles(c)
    fam = family(f["material"])
    p = dict(profs.get(fam, profs["SONSTIGE"]))
    try:
        own = json.loads(f["profile"] or "{}") if "profile" in f.keys() else {}
    except (TypeError, ValueError):
        own = {}
    p.update({k: v for k, v in own.items() if v not in (None, "")})
    p["family"] = fam
    return p


@bp.get("/profiles")
def get_profiles():
    with conn() as c:
        return jsonify(list(profiles(c).values()))


@bp.put("/profiles/<fam>")
def put_profile(fam):
    if fam not in DEFAULT_PROFILES:
        abort(404)
    b = request.get_json(force=True)
    data = {k: b.get(k) for k in PROFILE_FIELDS if k in b}
    with conn() as c:
        c.execute(PROFILES_SCHEMA)
        c.execute("INSERT INTO material_profiles(family, data) VALUES(?,?) ON CONFLICT(family) DO UPDATE SET data=excluded.data",
                  (fam, json.dumps(data)))
        return jsonify(profiles(c)[fam])


@bp.delete("/profiles/<fam>")
def reset_profile(fam):
    with conn() as c:
        c.execute(PROFILES_SCHEMA)
        c.execute("DELETE FROM material_profiles WHERE family=?", (fam,))
    return "", 204


@bp.get("/profiles/family")
def family_of():
    return jsonify({"family": family(request.args.get("material"))})


# ---------------------------------------------------------------- Kosten
def price_per_g(unit, fil) -> float | None:
    if unit is not None and unit["price"] and unit["initial_g"]:
        return float(unit["price"]) / float(unit["initial_g"])
    if fil is not None and fil["price"]:
        return float(fil["price"]) / float(fil["net_weight_g"] or 1000)
    return None


def job_costs(c, job: dict) -> dict:
    """Material- und Stromkosten eines Druckauftrags (usage = Liste mit grams/unit_id/filament_ref)."""
    mat, unknown = 0.0, 0.0
    for u in job.get("usage") or []:
        unit = c.execute("SELECT * FROM spool_units WHERE id=?", (u.get("unit_id"),)).fetchone() if u.get("unit_id") else None
        fid = u.get("filament_ref") or (unit["filament_id"] if unit else None)
        fil = c.execute("SELECT * FROM filaments WHERE id=?", (fid,)).fetchone() if fid else None
        ppg = price_per_g(unit, fil)
        if ppg is None:
            unknown += u.get("grams") or 0
        else:
            mat += (u.get("grams") or 0) * ppg
    minutes = job.get("minutes") or 0
    energy = minutes / 60 * setting(c, "printer_power_w") / 1000 * setting(c, "kwh_price")
    return {"material": round(mat, 2), "energy": round(energy, 2), "total": round(mat + energy, 2),
            "grams_without_price": round(unknown, 1)}


# ---------------------------------------------------------------- Bestandskennzahlen
def unit_stats(c):
    """Je Inventar-Eintrag: Anzahl OVP/offen, Restgewicht gesamt, Wert."""
    out = defaultdict(lambda: {"sealed": 0, "open": 0, "remaining_g": 0.0, "value": 0.0, "value_known": True})
    fils = {r["id"]: r for r in c.execute("SELECT * FROM filaments")}
    for u in c.execute("SELECT * FROM spool_units WHERE status!='empty'"):
        s = out[u["filament_id"]]
        s[u["status"]] += 1
        s["remaining_g"] += u["remaining_g"] or 0
        ppg = price_per_g(u, fils.get(u["filament_id"]))
        if ppg is None:
            s["value_known"] = False
        else:
            s["value"] += (u["remaining_g"] or 0) * ppg
    return out, fils


def host(url):
    try:
        h = urlparse(url if "://" in (url or "") else "https://" + (url or "")).hostname or ""
        return h[4:] if h.startswith("www.") else h
    except ValueError:
        return ""


@bp.get("/shopping")
def shopping():
    with conn() as c:
        low_g = setting(c, "low_stock_g")
        stats, fils = unit_stats(c)
        items = []
        for fid, f in fils.items():
            s = stats.get(fid, {"sealed": 0, "open": 0, "remaining_g": 0})
            active = s["sealed"] + s["open"]
            reasons = []
            if f["min_qty"] and active <= f["min_qty"]:
                reasons.append(f"Bestand {active} ≤ Mindestbestand {f['min_qty']}")
            if s["sealed"] == 0 and s["open"] > 0 and s["remaining_g"] < low_g:
                reasons.append(f"nur noch {round(s['remaining_g'])} g, keine Reserve")
            if not reasons:
                continue
            items.append({"type": "filament", "id": fid, "name": f"{f['color_name']}", "sub": f"{f['brand']} · {f['material']}",
                          "colors": json.loads(f["colors"] or "[]"), "reasons": reasons, "remaining_g": round(s["remaining_g"]),
                          "buy_url": f["buy_url"], "shop": host(f["buy_url"]) if f["buy_url"] else "", "price": f["price"],
                          "ordered_at": f["ordered_at"], "sku": f["sku"]})
        for m in c.execute("SELECT * FROM materials WHERE min_qty > 0 AND qty <= min_qty"):
            items.append({"type": "material", "id": m["id"], "name": m["description"],
                          "sub": f"{'Zubehör' if m['kind'] == 'zubehoer' else 'Material'} · {m['category'] or ''}".strip(" ·"),
                          "colors": None, "reasons": [f"Bestand {m['qty']} ≤ Mindestbestand {m['min_qty']}"],
                          "buy_url": m["buy_url"], "shop": host(m["buy_url"]) if m["buy_url"] else "", "price": m["price"],
                          "ordered_at": m["ordered_at"]})
        items.sort(key=lambda x: (x["shop"] or "~", x["name"]))
        return jsonify(items)


@bp.post("/shopping/ordered")
def mark_ordered():
    b = request.get_json(force=True)
    table = "filaments" if b.get("type") == "filament" else "materials"
    with conn() as c:
        c.execute(f"UPDATE {table} SET ordered_at=? WHERE id=?",
                  (date.today().isoformat() if b.get("ordered") else None, int(b["id"])))
    return jsonify({"ok": True})


# ---------------------------------------------------------------- Trocknung
@bp.get("/drying")
def drying():
    with conn() as c:
        profs = profiles(c)
        out = []
        today = date.today()
        for u in c.execute("""SELECT u.*, f.material, f.color_name, f.brand, f.colors, f.profile
                              FROM spool_units u JOIN filaments f ON f.id=u.filament_id WHERE u.status='open'"""):
            p = effective_profile(c, u, profs)
            limit = p.get("open_days")
            ref = max([d for d in (u["opened_at"], u["dried_at"]) if d] or [None]) if (u["opened_at"] or u["dried_at"]) else None
            if not limit or not ref:
                continue
            days = (today - date.fromisoformat(ref[:10])).days
            state = "due" if days >= limit else "soon" if days >= limit * 0.8 else "ok"
            if state == "ok":
                continue
            out.append({"unit_id": u["id"], "code": f"S{u['id']:04d}", "filament_id": u["filament_id"],
                        "name": u["color_name"], "sub": f"{u['brand']} · {u['material']}", "colors": json.loads(u["colors"] or "[]"),
                        "days": days, "limit": limit, "state": state, "since": ref, "dried": bool(u["dried_at"]) and u["dried_at"] >= (u["opened_at"] or ""),
                        "dry_temp": p.get("dry_temp"), "dry_hours": p.get("dry_hours"), "family": p["family"]})
        humid = []
        warn = setting(c, "ams_humidity_warn")
        try:
            for r in c.execute("""SELECT DISTINCT a.printer_id, a.ams_id, a.ams_humidity, p.name FROM ams_trays a
                                  LEFT JOIN printers p ON p.id=a.printer_id WHERE a.ams_id < 128 OR a.ams_id BETWEEN 128 AND 200"""):
                try:
                    h = float(r["ams_humidity"])
                except (TypeError, ValueError):
                    continue
                if h >= 10 and h >= warn:  # Werte < 10 sind Stufen (1–5) älterer Firmware
                    humid.append({"printer": r["name"], "ams_id": r["ams_id"], "humidity": h, "limit": warn})
        except Exception:  # noqa: BLE001
            pass
        out.sort(key=lambda x: (x["state"] != "due", -x["days"]))
        return jsonify({"spools": out, "ams": humid})


# ---------------------------------------------------------------- Statistik
CONSUME_KINDS = ("ams", "job", "slicer", "history")


@bp.get("/stats")
def stats():
    with conn() as c:
        now = datetime.now()
        stats_u, fils = unit_stats(c)
        cur = setting(c, "currency")
        # Verbrauch aus dem Spulen-Protokoll
        rows = c.execute(f"""SELECT l.ts, -l.delta_g AS g, u.filament_id, u.price, u.initial_g FROM unit_log l
                             JOIN spool_units u ON u.id=l.unit_id
                             WHERE l.kind IN ({','.join('?' * len(CONSUME_KINDS))})""", CONSUME_KINDS).fetchall()
        months = {}
        for i in range(11, -1, -1):
            y, m = now.year, now.month - i
            while m <= 0:
                m += 12
                y -= 1
            months[f"{y}-{m:02d}"] = {"month": f"{y}-{m:02d}", "grams": 0.0, "cost": 0.0, "jobs": 0, "hours": 0.0}
        by_mat, by_fil = defaultdict(float), defaultdict(float)
        last30 = {"grams": 0.0, "cost": 0.0}
        recent = defaultdict(float)  # 90 Tage je Filament
        for r in rows:
            g = r["g"] or 0
            f = fils.get(r["filament_id"])
            ppg = (r["price"] / r["initial_g"]) if r["price"] and r["initial_g"] else (
                (f["price"] / (f["net_weight_g"] or 1000)) if f is not None and f["price"] else 0)
            key = r["ts"][:7]
            if key in months:
                months[key]["grams"] += g
                months[key]["cost"] += g * ppg
            if f is not None:
                by_mat[f["material"]] += g
                by_fil[r["filament_id"]] += g
            ts = datetime.fromisoformat(r["ts"])
            if (now - ts).days < 30:
                last30["grams"] += g
                last30["cost"] += g * ppg
            if (now - ts).days < 90:
                recent[r["filament_id"]] += g
        jobs30 = 0
        try:
            for j in c.execute("SELECT started, ended FROM print_jobs WHERE ended IS NOT NULL"):
                key = j["started"][:7]
                dur = (datetime.fromisoformat(j["ended"]) - datetime.fromisoformat(j["started"])).total_seconds() / 3600
                if key in months:
                    months[key]["jobs"] += 1
                    months[key]["hours"] += dur
                if (now - datetime.fromisoformat(j["started"])).days < 30:
                    jobs30 += 1
        except Exception:  # noqa: BLE001
            pass
        energy30 = sum(m["hours"] for k, m in months.items() if k >= (now - timedelta(days=30)).strftime("%Y-%m")) \
            * setting(c, "printer_power_w") / 1000 * setting(c, "kwh_price")
        forecast = []
        for fid, g90 in recent.items():
            f = fils.get(fid)
            if f is None or g90 <= 0:
                continue
            per_day = g90 / 90
            rem = stats_u.get(fid, {}).get("remaining_g", 0)
            forecast.append({"id": fid, "name": f["color_name"], "sub": f"{f['brand']} · {f['material']}",
                             "colors": json.loads(f["colors"] or "[]"), "per_week": round(per_day * 7),
                             "remaining_g": round(rem), "days_left": round(rem / per_day) if per_day else None})
        forecast.sort(key=lambda x: (x["days_left"] is None, x["days_left"] or 0))
        top = sorted(by_fil.items(), key=lambda x: -x[1])[:10]
        value = sum(s["value"] for s in stats_u.values())
        unpriced = sum(1 for s in stats_u.values() if not s["value_known"])
        return jsonify({
            "currency": cur,
            "kpi": {
                "spools_sealed": sum(s["sealed"] for s in stats_u.values()),
                "spools_open": sum(s["open"] for s in stats_u.values()),
                "remaining_kg": round(sum(s["remaining_g"] for s in stats_u.values()) / 1000, 1),
                "stock_value": round(value, 2), "stock_value_unpriced": unpriced,
                "used_30d_g": round(last30["grams"]), "cost_30d": round(last30["cost"] + energy30, 2), "jobs_30d": jobs30,
            },
            "months": [{**m, "grams": round(m["grams"]), "cost": round(m["cost"], 2), "hours": round(m["hours"], 1)} for m in months.values()],
            "by_material": [{"material": k, "grams": round(v)} for k, v in sorted(by_mat.items(), key=lambda x: -x[1])[:8]],
            "top": [{"id": fid, "name": fils[fid]["color_name"], "sub": f"{fils[fid]['brand']} · {fils[fid]['material']}",
                     "colors": json.loads(fils[fid]["colors"] or "[]"), "grams": round(g)} for fid, g in top if fid in fils],
            "forecast": forecast[:15],
        })
