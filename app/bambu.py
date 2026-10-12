"""Bambu Filament Manager: Sync der Cloud-Spulen + RFID-Verlinkung mit dem Inventar."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from flask import Blueprint, abort, jsonify, request

from bambu_cloud import AuthExpired, BambuCloud, CloudError, CodeRequired, TfaRequired, diagnose

BASE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE / "data"))
DB_PATH = DATA_DIR / "inventar.db"

bp = Blueprint("bambu", __name__, url_prefix="/api/bambu")
_sync_lock = threading.Lock()
_pending: dict = {}  # laufender Login (account, region, tfa_key) – nur im Speicher


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def get_setting(c, key, default=None):
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(c, key, value):
    if value is None:
        c.execute("DELETE FROM settings WHERE key=?", (key,))
    else:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


# ---------------- Katalog-Abgleich ----------------
_cat = None


CATALOG_PATHS = (DATA_DIR / "catalog" / "bambu.json", BASE / "catalog" / "bambu.json")


def bambu_catalog():
    global _cat
    if _cat is None:
        for p in CATALOG_PATHS:
            if p.exists():
                _cat = json.loads(p.read_text(encoding="utf-8"))
                break
        else:
            return []  # (noch) nicht geladen – siehe ensure_catalog()
    return _cat


def ensure_catalog(app=None):
    """Fehlt der Bambu-Katalog (nicht im Image), beim Start im Hintergrund nach DATA_DIR laden."""
    if any(p.exists() for p in CATALOG_PATHS) or os.environ.get("CATALOG_FETCH", "1") != "1":
        return

    def run():
        global _cat
        import catalog_fetch
        for attempt in range(5):
            try:
                n = catalog_fetch.fetch(CATALOG_PATHS[0])
                _cat = None
                if app is not None:
                    import app as appmod
                    appmod._CATALOG = None
                print(f"Bambu-Katalog geladen: {n} Farben", flush=True)
                return
            except Exception as e:  # noqa: BLE001
                print(f"Bambu-Katalog: Download fehlgeschlagen ({e}), neuer Versuch …", flush=True)
                time.sleep(30 * (attempt + 1))

    threading.Thread(target=run, daemon=True, name="catalog").start()


def _code(s):
    """'W01' / 'W1' → 'W1'"""
    m = re.match(r"^([A-Z])0*(\d+)$", (s or "").upper())
    return f"{m.group(1)}{m.group(2)}" if m else None


def catalog_match(spool) -> list[dict]:
    """Bambu-Katalogeinträge (→ 5-stellige ID) zu einer Cloud-Spule."""
    fid = (spool.get("filament_id") or "").upper()
    color = (spool.get("color") or "").upper()[:9]
    hits = [e for e in bambu_catalog() if e.get("fila_id") == fid and e["colors"][0].upper() == color]
    if len(hits) != 1 and spool.get("tray_id_name"):
        # trayIdName = "<filamentId ohne GF>-<Farbcode>", z. B. "A00-W01"
        part = spool["tray_id_name"].split("-")
        if len(part) == 2:
            f2, code = "GF" + part[0].upper(), _code(part[1])
            by_tray = [e for e in bambu_catalog() if e.get("fila_id") == f2 and _code(e.get("tray_code")) == code]
            if by_tray:
                hits = by_tray
    return hits


def _hex_dist(a, b):
    try:
        a, b = a.lstrip("#"), b.lstrip("#")
        return sum((int(a[i:i + 2], 16) - int(b[i:i + 2], 16)) ** 2 for i in (0, 2, 4)) ** 0.5
    except (ValueError, IndexError):
        return 999


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def suggest(c, spool) -> tuple[list[dict], str]:
    """Kandidaten aus dem Inventar. Rückgabe (Liste, Methode)."""
    fils = [dict(r) for r in c.execute("SELECT id, brand, material, color_name, sku, colors, qty, sealed, refill, location FROM filaments")]
    cat = catalog_match(spool)
    skus = {e["sku"] for e in cat}
    if skus:
        exact = [f for f in fils if f["sku"] in skus]
        if exact:
            return exact, "sku"
    # Fallback: gleiche Marke + Material, ähnlichste Farbe
    vendor, typ = _norm(spool.get("vendor")), _norm(spool.get("name") or spool.get("type"))
    cand = []
    for f in fils:
        if vendor and vendor[:5] not in _norm(f["brand"]):
            continue
        if typ and _norm(f["material"]) != typ:
            continue
        d = _hex_dist(json.loads(f["colors"] or "[]")[0] if f["colors"] else "#000000", spool.get("color") or "#000000")
        if d < 60:
            cand.append((d, f))
    cand.sort(key=lambda x: x[0])
    return [f for _, f in cand[:5]], "color"


# ---------------- Sync ----------------
def _client(c) -> BambuCloud:
    return BambuCloud(get_setting(c, "bambu_region", "global"), get_setting(c, "bambu_token"))


def _map(h: dict) -> dict:
    colors = h.get("colors") or ([h["color"]] if h.get("color") else [])
    return {
        "cloud_id": h.get("id"), "rfid": h.get("RFID") or None, "tray_id_name": h.get("trayIdName") or None,
        "filament_id": h.get("filamentId"), "vendor": h.get("filamentVendor"), "type": h.get("filamentType"),
        "name": h.get("filamentName"), "color": (h.get("color") or "").upper() or None,
        "colors": json.dumps([x.upper() for x in colors]), "net_weight": h.get("netWeight"),
        "total_net_weight": h.get("totalNetWeight"), "status": h.get("status"), "create_type": h.get("createType"),
        "note": h.get("note"), "in_printer": 1 if h.get("inPrinter") else 0, "device_name": h.get("deviceName"),
        "ams_id": h.get("amsId"), "slot_id": h.get("slotId"), "cloud_updated": h.get("updatedAt"),
        "raw": json.dumps(h, ensure_ascii=False),
    }


def run_sync() -> dict:
    if not _sync_lock.acquire(blocking=False):
        return {"ok": False, "error": "Sync läuft bereits"}
    try:
        with conn() as c:
            client = _client(c)
            if not client.token and not client.mock:
                return {"ok": False, "error": "Nicht mit Bambu verbunden"}
            try:
                hits = client.get_spools()
            except AuthExpired as e:
                set_setting(c, "bambu_error", str(e))
                set_setting(c, "bambu_token_valid", "0")
                return {"ok": False, "error": str(e), "auth": True}
            except CloudError as e:
                set_setting(c, "bambu_error", str(e))
                return {"ok": False, "error": str(e)}
            now = datetime.now().isoformat(timespec="seconds")
            auto = get_setting(c, "bambu_auto_link", "0") == "1"
            seen, new, linked = set(), 0, 0
            for h in hits:
                s = _map(h)
                if s["cloud_id"] is None:
                    continue
                seen.add(s["cloud_id"])
                old = c.execute("SELECT filament_ref, link_mode FROM spools WHERE cloud_id=?", (s["cloud_id"],)).fetchone()
                if old:
                    c.execute(f"UPDATE spools SET {','.join(k + '=?' for k in s)}, present=1, last_seen=? WHERE cloud_id=?",
                              [*s.values(), now, s["cloud_id"]])
                    continue
                new += 1
                ref, mode = None, None
                # Gleiche RFID schon mal verlinkt (Spule in der Cloud neu angelegt)? → Link übernehmen
                if s["rfid"]:
                    prev = c.execute("SELECT filament_ref FROM spools WHERE rfid=? AND filament_ref IS NOT NULL", (s["rfid"],)).fetchone()
                    if prev:
                        ref, mode = prev["filament_ref"], "rfid"
                if ref is None and auto:
                    cands, how = suggest(c, s)
                    if how == "sku" and len(cands) == 1:
                        ref, mode = cands[0]["id"], "auto"
                if ref:
                    linked += 1
                c.execute(f"INSERT INTO spools ({','.join(s)}, filament_ref, link_mode, present, last_seen) VALUES ({','.join('?' * len(s))},?,?,1,?)",
                          [*s.values(), ref, mode, now])
            gone = 0
            if seen:
                gone = c.execute(f"UPDATE spools SET present=0 WHERE present=1 AND cloud_id NOT IN ({','.join('?' * len(seen))})", list(seen)).rowcount
            else:
                gone = c.execute("UPDATE spools SET present=0 WHERE present=1").rowcount
            set_setting(c, "bambu_last_sync", now)
            set_setting(c, "bambu_error", None)
            set_setting(c, "bambu_token_valid", "1")
            task_res = {}
            if client.token or os.environ.get("BAMBU_MOCK_TASKS"):
                try:
                    import tasks
                    task_res = tasks.sync(c, client)
                except Exception as e:  # noqa: BLE001
                    print("Druckaufträge:", e, flush=True)
                    task_res = {"error": str(e)}
            return {"ok": True, "total": len(seen), "new": new, "auto_linked": linked, "gone": gone, "at": now, "tasks": task_res}
    finally:
        _sync_lock.release()


def start_scheduler(app):
    if os.environ.get("BAMBU_SCHEDULER", "1") != "1" or getattr(app, "_bambu_sched", False):
        return
    app._bambu_sched = True

    def loop():
        time.sleep(15)
        while True:
            try:
                with conn() as c:
                    minutes = int(get_setting(c, "bambu_interval", "15") or 0)
                    last = get_setting(c, "bambu_last_sync")
                    active = bool(get_setting(c, "bambu_token")) or bool(os.environ.get("BAMBU_MOCK"))
                    valid = get_setting(c, "bambu_token_valid", "1") == "1"
                due = minutes > 0 and active and valid and (
                    not last or (datetime.now() - datetime.fromisoformat(last)).total_seconds() >= minutes * 60)
                if due:
                    run_sync()
            except Exception as e:  # noqa: BLE001
                print("Bambu-Sync Fehler:", e, flush=True)
            time.sleep(60)

    threading.Thread(target=loop, daemon=True, name="bambu-sync").start()


# ---------------- Abfragen ----------------
def _spool_out(r) -> dict:
    d = dict(r)
    d.pop("raw", None)
    d["colors"] = json.loads(d["colors"] or "[]")
    d["in_printer"] = bool(d["in_printer"])
    d["present"] = bool(d["present"])
    tn, n = d.get("total_net_weight"), d.get("net_weight")
    d["percent"] = round(n / tn * 100) if tn and n is not None else None
    # eigenes Vollgewicht (z. B. Sunlu 1 kg unter einem Bambu-Tag) überstimmt Bambus Angabe
    full = d.get("full_weight_g") or tn
    d["eff_total"] = full
    d["eff_grams"] = round(d["percent"] / 100 * full) if d["percent"] is not None and full else n
    return d


def linked_spools(filament_id: int | None = None) -> dict[int, list]:
    out: dict[int, list] = {}
    try:
        with conn() as c:
            q = "SELECT * FROM spools WHERE filament_ref IS NOT NULL" + (" AND filament_ref=?" if filament_id else "")
            for r in c.execute(q + " ORDER BY present DESC, net_weight", (filament_id,) if filament_id else ()):
                out.setdefault(r["filament_ref"], []).append(_spool_out(r))
    except sqlite3.OperationalError:
        pass
    return out


# ---------------- API ----------------
@bp.get("/status")
def status():
    with conn() as c:
        cnt = c.execute("SELECT COUNT(*) n, SUM(filament_ref IS NOT NULL) l, SUM(in_printer) p FROM spools WHERE present=1").fetchone()
        return jsonify({
            "connected": bool(get_setting(c, "bambu_token")) or bool(os.environ.get("BAMBU_MOCK")),
            "mock": bool(os.environ.get("BAMBU_MOCK")),
            "token_valid": get_setting(c, "bambu_token_valid", "1") == "1",
            "account": get_setting(c, "bambu_account"), "region": get_setting(c, "bambu_region", "global"),
            "last_sync": get_setting(c, "bambu_last_sync"), "error": get_setting(c, "bambu_error"),
            "interval": int(get_setting(c, "bambu_interval", "15")), "auto_link": get_setting(c, "bambu_auto_link", "0") == "1",
            "spools": cnt["n"] or 0, "linked": cnt["l"] or 0, "in_printer": cnt["p"] or 0,
            "pending": _pending.get("step"),
        })


def _store_token(token, account, region):
    with conn() as c:
        set_setting(c, "bambu_token", token)
        set_setting(c, "bambu_account", account)
        set_setting(c, "bambu_region", region)
        set_setting(c, "bambu_token_valid", "1")
        set_setting(c, "bambu_error", None)
        set_setting(c, "bambu_token_at", datetime.now().isoformat(timespec="seconds"))
    _pending.clear()


def _err(e, code=400):
    return jsonify({"ok": False, "error": str(e)}), code


@bp.post("/login")
def login():
    b = request.get_json(force=True)
    account, region = (b.get("account") or "").strip(), b.get("region") or "global"
    cl = BambuCloud(region)
    try:
        tok = cl.login(account, b.get("password") or "")
    except CodeRequired as e:
        _pending.update(step="code", account=account, region=region)
        return jsonify({"ok": True, "step": "code", "message": str(e)})
    except TfaRequired as e:
        _pending.update(step="tfa", account=account, region=region, tfa_key=e.tfa_key)
        return jsonify({"ok": True, "step": "tfa", "message": "Bitte 2FA-Code aus der Authenticator-App eingeben"})
    except CloudError as e:
        return _err(e)
    _store_token(tok, account, region)
    return jsonify({"ok": True, "step": "done", "sync": run_sync()})


@bp.post("/code")
def code():
    b = request.get_json(force=True)
    if not _pending.get("step"):
        return _err("Kein Login aktiv – bitte erneut anmelden")
    cl = BambuCloud(_pending["region"])
    try:
        if _pending["step"] == "tfa":
            tok = cl.login_tfa(_pending["tfa_key"], (b.get("code") or "").strip())
        else:
            tok = cl.login_code(_pending["account"], (b.get("code") or "").strip())
    except CloudError as e:
        return _err(e)
    _store_token(tok, _pending["account"], _pending["region"])
    return jsonify({"ok": True, "step": "done", "sync": run_sync()})


@bp.post("/request-code")
def request_code():
    b = request.get_json(force=True)
    account, region = (b.get("account") or "").strip(), b.get("region") or "global"
    try:
        BambuCloud(region).request_code(account)
    except CloudError as e:
        return _err(e)
    _pending.update(step="code", account=account, region=region)
    return jsonify({"ok": True, "step": "code"})


@bp.post("/token")
def token():
    b = request.get_json(force=True)
    tok = (b.get("token") or "").strip()
    if not tok:
        return _err("Token fehlt")
    _store_token(tok, b.get("account") or "Token", b.get("region") or "global")
    res = run_sync()
    return jsonify({"ok": res.get("ok", False), "sync": res, "error": res.get("error")})


@bp.post("/logout")
def logout():
    with conn() as c:
        for k in ("bambu_token", "bambu_account", "bambu_error", "bambu_token_valid"):
            set_setting(c, k, None)
    _pending.clear()
    return jsonify({"ok": True})


@bp.put("/settings")
def settings():
    b = request.get_json(force=True)
    with conn() as c:
        if "interval" in b:
            set_setting(c, "bambu_interval", max(0, int(b["interval"])))
        if "auto_link" in b:
            set_setting(c, "bambu_auto_link", "1" if b["auto_link"] else "0")
    return status()


@bp.post("/sync")
def sync():
    res = run_sync()
    return jsonify(res), (200 if res.get("ok") else 400)


@bp.get("/spools")
def spools():
    with conn() as c:
        rows = c.execute("SELECT * FROM spools ORDER BY present DESC, filament_ref IS NOT NULL, vendor, name, color").fetchall()
        out = []
        for r in rows:
            d = _spool_out(r)
            cat = catalog_match(d)
            d["catalog"] = [{"sku": e["sku"], "name": e["name_de"] or e["name_en"], "type": e["type"]} for e in cat]
            if d["filament_ref"] is None:
                cands, how = suggest(c, d)
                d["suggestions"] = [{"id": f["id"], "label": f"{f['color_name']} · {f['material']} · {f['brand']}" + (f" · {f['sku']}" if f["sku"] else "") + f" · {f['qty']}×" + (" OVP" if f["sealed"] else "") + (" Refill" if f["refill"] else "") + (f" · {f['location']}" if f["location"] else ""), "how": how} for f in cands]
            else:
                f = c.execute("SELECT id, color_name, material, brand, sku FROM filaments WHERE id=?", (d["filament_ref"],)).fetchone()
                d["linked"] = dict(f) if f else None
            out.append(d)
        return jsonify(out)


@bp.post("/spools/<int:cloud_id>/link")
def link(cloud_id):
    fid = request.get_json(force=True).get("filament_id")
    with conn() as c:
        if not c.execute("SELECT 1 FROM spools WHERE cloud_id=?", (cloud_id,)).fetchone():
            abort(404)
        if fid is not None and not c.execute("SELECT 1 FROM filaments WHERE id=?", (fid,)).fetchone():
            abort(400)
        c.execute("UPDATE spools SET filament_ref=?, link_mode=? WHERE cloud_id=?", (fid, "manual" if fid else None, cloud_id))
        r = c.execute("SELECT rfid FROM spools WHERE cloud_id=?", (cloud_id,)).fetchone()
        from ams import propagate_link
        propagate_link(c, r["rfid"], fid, "cloud")
    return jsonify({"ok": True})


@bp.post("/autolink")
def autolink():
    n = 0
    with conn() as c:
        for r in c.execute("SELECT * FROM spools WHERE filament_ref IS NULL AND present=1").fetchall():
            cands, how = suggest(c, dict(r))
            if how == "sku" and len(cands) == 1:
                c.execute("UPDATE spools SET filament_ref=?, link_mode='auto' WHERE cloud_id=?", (cands[0]["id"], r["cloud_id"]))
                n += 1
    return jsonify({"ok": True, "linked": n})


@bp.post("/spools/<int:cloud_id>/create")
def create_from_spool(cloud_id):
    """Neuen Inventar-Eintrag aus einer Cloud-Spule anlegen und verlinken."""
    with conn() as c:
        r = c.execute("SELECT * FROM spools WHERE cloud_id=?", (cloud_id,)).fetchone()
        if not r:
            abort(404)
        s = _spool_out(r)
        cat = catalog_match(s)
        e = cat[0] if len(cat) == 1 else None
        vendor = s["vendor"] or "Bambu Lab"
        row = {
            "brand": vendor, "material": (e["type"] if e else s["name"] or s["type"] or ""),
            "color_name": (e["name_de"] or e["name_en"]) if e else (s["note"] or s["color"] or "Neu"),
            "official_name": (e["name_de"] or e["name_en"]) if e else "", "sku": e["sku"] if e else "",
            "qty": 1, "sealed": 0, "spool": 1, "refill": 0, "sample": 0,
            "last_check": datetime.now().date().isoformat(), "location": "", "notes": "aus Bambu Filament Manager angelegt",
            "colors": json.dumps(e["colors"] if e else (s["colors"] or ["#888888FF"])),
            "pattern": e["pattern"] if e else ("gradient" if len(s["colors"]) > 1 else "solid"),
            "swatch_source": "bambu" if e or "bambu" in vendor.lower() else "manual",
        }
        cur = c.execute(f"INSERT INTO filaments ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
        c.execute("UPDATE spools SET filament_ref=?, link_mode='manual' WHERE cloud_id=?", (cur.lastrowid, cloud_id))
        return jsonify({"ok": True, "filament_id": cur.lastrowid})


@bp.get("/diag")
def diag():
    return jsonify(diagnose(request.args.get("region", "global")))
