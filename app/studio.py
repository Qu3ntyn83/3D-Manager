"""Bambu Studio: Fremdfilament mit den richtigen Werten im AMS-Slot anzeigen.

Bambu Studio übernimmt für jeden AMS-Slot, was der Drucker meldet (tray_info_idx = Filamentprofil,
Farbe, Typ, Düsentemperatur). Bei einem umgesetzten Bambu-Tag meldet der Drucker das, was im Tag steht
(z. B. „Bambu PLA Basic Schwarz“) – nicht die Sunlu-Spule, die tatsächlich drin ist.

Dieses Modul schickt dem Drucker für Slots mit zugeordnetem Tag die Werte der Inventar-Spule
(MQTT „ams_filament_setting“, wie Bambu Studio selbst) und prüft danach, ob der Drucker sie meldet:
  ok        Drucker meldet unsere Werte → Studio zeigt das richtige Profil
  sent      gesendet, Rückmeldung steht noch aus
  rejected  Drucker meldet weiter die Tag-Werte (Firmware sperrt Slots mit Bambu-Tag)

Profilwahl je Filament: eigenes Profil aus der Bambu-Cloud (Bambu Studio → „Eigene Filamente“),
ein Systemprofil (z. B. „SUNLU PLA+“) oder automatisch passend zu Marke/Material.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Blueprint, abort, jsonify, request

from bambu import BASE, DATA_DIR, conn, get_setting, set_setting

bp = Blueprint("studio", __name__, url_prefix="/api/studio")
PRESET_PATHS = (DATA_DIR / "catalog" / "studio_presets.json", BASE / "catalog" / "studio_presets.json")
SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_slots (
  printer_id INTEGER, ams_id INTEGER, tray_id INTEGER, tag_key TEXT, desired TEXT,
  status TEXT, sent_at REAL, attempts INTEGER DEFAULT 0, reported TEXT, updated TEXT,
  PRIMARY KEY (printer_id, ams_id, tray_id)
);
"""
CONFIRM_SECONDS = 20
GENERIC = {"PLA": "GFL99", "PETG": "GFG99", "ABS": "GFB99", "ASA": "GFB98", "TPU": "GFU99", "PA": "GFN99",
           "PC": "GFC99", "PVA": "GFS99", "SUPPORT": "GFS99", "SONSTIGE": "GFL99"}
_static = None
_lock = threading.Lock()


def now():
    return datetime.now().isoformat(timespec="seconds")


def init_schema():
    with conn() as c:
        c.executescript(SCHEMA)


# ---------------------------------------------------------------- Profile
def static_presets() -> list[dict]:
    global _static
    if _static is None:
        for p in PRESET_PATHS:
            if p.exists():
                try:
                    _static = json.loads(p.read_text(encoding="utf-8"))
                    break
                except ValueError:
                    pass
        else:
            return []
    return _static


def ensure_presets():
    """Fehlende Systemprofile (nicht im Repo, AGPL) beim Start im Hintergrund laden."""
    if any(p.exists() for p in PRESET_PATHS) or os.environ.get("CATALOG_FETCH", "1") != "1":
        return

    def run():
        global _static
        import catalog_fetch
        for attempt in range(5):
            try:
                n = catalog_fetch.fetch_studio(PRESET_PATHS[0])
                _static = None
                print(f"Bambu-Studio-Profile geladen: {n}", flush=True)
                return
            except Exception as e:  # noqa: BLE001
                print(f"Bambu-Studio-Profile: Download fehlgeschlagen ({e}) …", flush=True)
                time.sleep(30 * (attempt + 1))

    threading.Thread(target=run, daemon=True, name="studio-presets").start()


def fetch_cloud(c, client) -> int:
    """Eigene und öffentliche Filamentprofile aus dem Bambu-Konto (wie Bambu Studio sie synchronisiert)."""
    mock = os.environ.get("BAMBU_MOCK_SLICER")
    if mock:
        data = json.loads(Path(mock).read_text(encoding="utf-8"))
    else:
        r = client._req("get", f"{client.api}/v1/iot-service/api/slicer/setting?version=02.00.00.00&public=true")
        if r.status_code != 200:
            return 0
        data = client._json(r)
    fil = data.get("filament") or {}
    out = []
    for scope in ("private", "public"):
        for e in fil.get(scope) or []:
            if not isinstance(e, dict) or not e.get("filament_id"):
                continue
            temps = e.get("nozzle_temperature") or []
            out.append({"filament_id": e["filament_id"], "setting_id": e.get("setting_id") or "", "name": e.get("name") or "",
                        "vendor": e.get("filament_vendor") or "", "type": e.get("filament_type") or "",
                        "nozzle_min": temps[0] if len(temps) > 1 else None, "nozzle_max": temps[1] if len(temps) > 1 else None,
                        "private": scope == "private"})
    set_setting(c, "studio_cloud_presets", json.dumps(out))
    set_setting(c, "studio_cloud_at", now())
    return len(out)


def _base_name(name: str) -> str:
    return name.split(" @")[0].strip()


def presets(c) -> list[dict]:
    """Zusammengeführt je filament_id: eigene Cloud-Profile zuerst, dann System."""
    try:
        cloud = json.loads(get_setting(c, "studio_cloud_presets") or "[]")
    except ValueError:
        cloud = []
    by_id: dict[str, dict] = {}
    for e in cloud:
        p = by_id.setdefault(e["filament_id"], {"filament_id": e["filament_id"], "name": _base_name(e["name"]), "vendor": e["vendor"],
                                                "type": e["type"], "nozzle_min": e["nozzle_min"], "nozzle_max": e["nozzle_max"],
                                                "source": "eigenes" if e["private"] else "system", "settings": []})
        if e.get("setting_id"):
            p["settings"].append({"setting_id": e["setting_id"], "name": e["name"]})
    for s in static_presets():
        p = by_id.get(s["filament_id"])
        if p:
            for k in ("type", "nozzle_min", "nozzle_max", "vendor"):
                p[k] = p[k] or s.get(k)
            continue
        by_id[s["filament_id"]] = {**s, "source": "system", "settings": []}
    order = {"eigenes": 0, "system": 1}
    return sorted(by_id.values(), key=lambda p: (order.get(p["source"], 2), p["vendor"] != "Generic", p["vendor"].lower(), p["name"].lower()))


def _toks(s: str) -> set[str]:
    s = re.sub(r"\s+\+", "+", (s or "").lower())          # „PLA +“ → „pla+“
    return {t for t in re.split(r"[^a-z0-9+]+", s) if t and t != "+"}


def auto_preset(f, plist: list[dict]) -> dict | None:
    """Bestes Profil zu Marke + Material, sonst Generic der Materialfamilie."""
    from insights import family
    brand = (f["brand"] or "").lower().replace("bambu lab", "bambu")
    mat = _toks(f["material"])
    best, score = None, 0
    for p in plist:
        name = p["name"].lower()
        vend = (p["vendor"] or "").lower().replace("bambu lab", "bambu")
        if not brand or brand not in (vend + " " + name):
            continue
        rest = _toks(name.replace(brand, " ")) - {"bambu", "lab"}
        if not rest or not rest <= mat | {"basic"} and not mat <= rest:
            continue
        s = 10 + len(rest & mat) * 3 - len(rest - mat) * 2 + (2 if p["source"] == "eigenes" and rest <= mat else 0)
        if s > score:
            best, score = p, s
    if best:
        return best
    gid = GENERIC.get(family(f["material"]), "GFL99")
    return next((p for p in plist if p["filament_id"] == gid), None)


def chosen(f, plist) -> tuple[dict | None, bool]:
    """(Profil, automatisch?)"""
    try:
        st = json.loads(f["studio"] or "null") if "studio" in f.keys() else None
    except (TypeError, ValueError):
        st = None
    if st and st.get("filament_id"):
        p = next((x for x in plist if x["filament_id"] == st["filament_id"]), None)
        return (p or {"filament_id": st["filament_id"], "name": st.get("name") or st["filament_id"], "type": "", "settings": [],
                      "nozzle_min": None, "nozzle_max": None, "source": "?"}), False
    return auto_preset(f, plist), True


# Seriennummer-Präfix → Bezeichnungen, wie sie in Profilnamen vorkommen („@BBL H2S“, „@Bambu Lab H2S 0.4 nozzle“)
MODELS = {"093": ("H2S",), "094": ("H2D",), "239": ("H2D Pro",), "00M": ("X1C", "X1 Carbon"), "00W": ("X1", "X1C", "X1 Carbon"),
          "03W": ("X1E",), "01S": ("P1P",), "01P": ("P1S",), "22E": ("P2S",), "030": ("A1M", "A1 mini"), "039": ("A1",)}


def model_names(model: str | None, serial: str | None) -> tuple:
    names = MODELS.get((serial or "")[:3].upper(), ())
    return tuple(n for n in ((model,) if model else ()) + names if n)


def pick_setting(settings: list[dict], names: tuple) -> str:
    for n in names:
        pat = re.compile(rf"(@BBL |@Bambu Lab ){re.escape(n)}( |$)", re.I)
        hit = [x for x in settings if pat.search(x["name"])]
        if hit:
            plain = [x for x in hit if "nozzle" not in x["name"].lower() or "0.4 nozzle" in x["name"].lower()]
            return (plain or hit)[0]["setting_id"]
    return settings[0]["setting_id"] if len(settings) == 1 else ""


def desired_for(c, fid, model: str | None = None, plist=None, serial: str | None = None) -> dict | None:
    f = c.execute("SELECT * FROM filaments WHERE id=?", (fid,)).fetchone()
    if not f:
        return None
    plist = plist if plist is not None else presets(c)
    p, auto = chosen(f, plist)
    if not p:
        return None
    from insights import effective_profile, family
    fam = effective_profile(c, f)
    try:
        own = json.loads(f["profile"] or "{}") or {}
    except (TypeError, ValueError):
        own = {}
    colors = json.loads(f["colors"] or "[]") or ["#FFFFFFFF"]
    col = colors[0].lstrip("#").upper()
    col = (col + "FF")[:8] if len(col) == 6 else col[:8]
    setting = pick_setting(p["settings"], model_names(model, serial)) if p.get("settings") else ""
    ttype = (p.get("type") or family(f["material"])).upper()
    return {"tray_info_idx": p["filament_id"], "setting_id": setting, "tray_type": ttype, "tray_color": col,
            "nozzle_temp_min": int(own.get("nozzle_min") or p.get("nozzle_min") or fam.get("nozzle_min") or 190),
            "nozzle_temp_max": int(own.get("nozzle_max") or p.get("nozzle_max") or fam.get("nozzle_max") or 240),
            "preset": p["name"], "auto": auto, "filament_id": fid}


def matches(desired: dict, tr: dict) -> bool:
    return (tr.get("tray_info_idx") or "") == desired["tray_info_idx"] and \
        (tr.get("tray_color") or "").upper()[:6] == desired["tray_color"][:6]


def command(aid: int, tid: int, d: dict) -> dict:
    """Wie Bambu Studio (DeviceManager::command_ams_filament_settings)."""
    return {"print": {"command": "ams_filament_setting", "sequence_id": str(int(time.time()) % 100000),
                      "ams_id": aid, "slot_id": tid, "tray_id": tid, "tray_info_idx": d["tray_info_idx"],
                      "setting_id": d["setting_id"], "tray_color": d["tray_color"], "tray_type": d["tray_type"],
                      "nozzle_temp_min": d["nozzle_temp_min"], "nozzle_temp_max": d["nozzle_temp_max"]}}


def is_ours(c, pid, aid, tid, tr) -> bool:
    """Meldet der Drucker gerade unsere Werte? Dann nicht als „Tag meldet …“ übernehmen."""
    r = c.execute("SELECT desired FROM studio_slots WHERE printer_id=? AND ams_id=? AND tray_id=?", (pid, aid, tid)).fetchone()
    if not r or not r["desired"]:
        return False
    try:
        return matches(json.loads(r["desired"]), tr)
    except (ValueError, KeyError):
        return False


# ---------------------------------------------------------------- Abgleich mit dem Drucker
def auto_enabled(c) -> bool:
    return (get_setting(c, "studio_auto") or "1") == "1"


def check(pc, force_slot: tuple | None = None):
    """Nach jedem AMS-Report: Slots mit zugeordnetem Tag abgleichen, ggf. senden, Ergebnis vermerken."""
    from ams import tag_key
    with pc._lock:
        snapshot = {k: dict(v) for k, v in pc.state.items()}
    to_send = []
    with _lock, conn() as c:
        c.executescript(SCHEMA)
        auto = auto_enabled(c)
        plist = None
        for (aid, tid), tr in snapshot.items():
            if aid == 255:
                continue
            key = tag_key(tr)
            row = c.execute("SELECT * FROM studio_slots WHERE printer_id=? AND ams_id=? AND tray_id=?", (pc.id, aid, tid)).fetchone()
            t = c.execute("SELECT filament_ref FROM tags WHERE tag_key=?", (key,)).fetchone() if key else None
            if not key or not t or not t["filament_ref"]:
                if row:
                    c.execute("DELETE FROM studio_slots WHERE printer_id=? AND ams_id=? AND tray_id=?", (pc.id, aid, tid))
                continue
            plist = plist if plist is not None else presets(c)
            d = desired_for(c, t["filament_ref"], pc.cfg.get("model"), plist, pc.serial)
            if not d:
                continue
            reported = {k: tr.get(k) for k in ("tray_info_idx", "tray_color", "tray_type", "nozzle_temp_min", "nozzle_temp_max")}
            same_insert = row and row["tag_key"] == key
            changed_target = not row or not row["desired"] or json.loads(row["desired"]).get("tray_info_idx") != d["tray_info_idx"] \
                or json.loads(row["desired"]).get("tray_color") != d["tray_color"]
            prev = row["status"] if same_insert else None
            tries = row["attempts"] if same_insert else 0
            if matches(d, tr):
                status, sent_at, attempts = "ok", row["sent_at"] if row else None, tries
            elif force_slot == (aid, tid) or (auto and tries < 3 and (not same_insert or changed_target or prev == "ok")):
                status, sent_at, attempts = "sent", time.time(), tries + 1
                to_send.append((aid, tid, d))
            elif prev == "sent" and time.time() - (row["sent_at"] or 0) > CONFIRM_SECONDS:
                status, sent_at, attempts = "rejected", row["sent_at"], tries
            elif prev in ("sent", "rejected"):
                status, sent_at, attempts = prev, row["sent_at"], tries
            else:
                status, sent_at, attempts = ("pending" if auto else "off"), None, tries
            c.execute("""INSERT INTO studio_slots (printer_id, ams_id, tray_id, tag_key, desired, status, sent_at, attempts, reported, updated)
                         VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(printer_id, ams_id, tray_id) DO UPDATE SET tag_key=excluded.tag_key,
                         desired=excluded.desired, status=excluded.status, sent_at=excluded.sent_at, attempts=excluded.attempts,
                         reported=excluded.reported, updated=excluded.updated""",
                      (pc.id, aid, tid, key, json.dumps(d), status, sent_at, attempts, json.dumps(reported), now()))
    for aid, tid, d in to_send:
        send(pc, aid, tid, d)


def recheck_all():
    """Nach Änderungen am Inventar (Profil, Farbe, Temperatur) alle verbundenen Drucker neu abgleichen."""
    def run():
        from ams import manager
        for pc in list(manager.conns.values()):
            if pc.connected:
                _safe_check(pc)
    threading.Thread(target=run, daemon=True).start()


def send(pc, aid, tid, d):
    if not pc.client:
        return False
    pc.client.publish(f"device/{pc.serial}/request", json.dumps(command(aid, tid, d)))
    # Rückmeldung abholen und nach Ablauf der Frist bewerten
    threading.Timer(3, pc.pushall).start()
    threading.Timer(CONFIRM_SECONDS + 2, lambda: _safe_check(pc)).start()
    return True


def _safe_check(pc):
    try:
        check(pc)
    except Exception as e:  # noqa: BLE001
        print("Studio-Abgleich:", e, flush=True)


def slot_info(c, pid, aid, tid) -> dict | None:
    r = c.execute("SELECT * FROM studio_slots WHERE printer_id=? AND ams_id=? AND tray_id=?", (pid, aid, tid)).fetchone()
    if not r:
        return None
    d = json.loads(r["desired"] or "{}")
    return {"status": r["status"], "preset": d.get("preset"), "auto": d.get("auto"), "filament_id": d.get("tray_info_idx"),
            "attempts": r["attempts"], "updated": r["updated"]}


# ---------------------------------------------------------------- API
@bp.get("/presets")
def api_presets():
    with conn() as c:
        return jsonify({"presets": presets(c), "cloud_at": get_setting(c, "studio_cloud_at"), "auto": auto_enabled(c)})


@bp.post("/presets/refresh")
def api_refresh():
    from bambu import _client
    with conn() as c:
        client = _client(c)
        if not client.token and not os.environ.get("BAMBU_MOCK_SLICER"):
            return jsonify({"ok": False, "error": "Nicht mit Bambu verbunden"}), 400
        try:
            n = fetch_cloud(c, client)
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify({"ok": True, "count": n})


@bp.get("/filament/<int:fid>")
def api_for_filament(fid):
    with conn() as c:
        d = desired_for(c, fid)
        if not d:
            abort(404)
        return jsonify(d)


@bp.post("/push")
def api_push():
    """Werte für einen Slot (oder alle Slots eines Druckers) jetzt senden."""
    b = request.get_json(force=True) or {}
    from ams import manager
    pc = manager.conns.get(int(b.get("printer_id") or 0))
    if not pc or not pc.connected:
        return jsonify({"ok": False, "error": "Drucker nicht verbunden"}), 409
    if b.get("ams_id") is not None:
        check(pc, force_slot=(int(b["ams_id"]), int(b["tray_id"])))
    else:
        with pc._lock:
            slots = list(pc.state)
        for s in slots:
            check(pc, force_slot=s)
    with conn() as c:
        return jsonify({"ok": True, "slots": [dict(r) for r in c.execute(
            "SELECT ams_id, tray_id, status FROM studio_slots WHERE printer_id=?", (pc.id,))]})


@bp.put("/auto")
def api_auto():
    on = bool((request.get_json(force=True) or {}).get("on"))
    with conn() as c:
        set_setting(c, "studio_auto", "1" if on else "0")
    return jsonify({"ok": True, "auto": on})
