"""Druckerstatus aus den MQTT-Reports: Zustand, Fortschritt, Temperaturen, HMS-Meldungen, Druckhistorie."""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

BASE = Path(__file__).parent

# Felder aus "print", die wir als Status behalten (alles andere ist für die App uninteressant)
STATUS_KEYS = {
    "gcode_state", "mc_percent", "mc_remaining_time", "layer_num", "total_layer_num", "subtask_name", "gcode_file",
    "task_id", "subtask_id", "job_id", "project_id", "stg_cur", "mc_print_stage", "print_error", "hms",
    "nozzle_temper", "nozzle_target_temper", "bed_temper", "bed_target_temper", "chamber_temper",
    "cooling_fan_speed", "big_fan1_speed", "big_fan2_speed", "heatbreak_fan_speed", "spd_lvl", "spd_mag",
    "wifi_signal", "nozzle_diameter", "nozzle_type", "lights_report", "print_type", "plate_idx", "3D",
}

STATE_DE = {
    "IDLE": "Bereit", "PREPARE": "Vorbereitung", "RUNNING": "Druckt", "PAUSE": "Pausiert",
    "FINISH": "Fertig", "FAILED": "Fehlgeschlagen", "SLICING": "Slicen", "INIT": "Startet", "OFFLINE": "Offline",
}
SPEED_DE = {1: "Leise", 2: "Standard", 3: "Sport", 4: "Ludicrous"}
STAGE_DE = {
    0: "Druckt", 1: "Bett-Nivellierung", 2: "Heizbett aufheizen", 3: "Resonanzkompensation", 4: "Filamentwechsel",
    5: "Pause (M400)", 6: "Pause: Filament leer", 7: "Hotend aufheizen", 8: "Extrusion kalibrieren", 9: "Druckbett scannen",
    10: "Erste Schicht prüfen", 11: "Druckplatte erkennen", 12: "Micro-Lidar kalibrieren", 13: "Referenzfahrt",
    14: "Düse reinigen", 15: "Extrudertemperatur prüfen", 16: "Pause durch Nutzer", 17: "Pause: Frontabdeckung",
    18: "Micro-Lidar kalibrieren", 19: "Flussrate kalibrieren", 20: "Pause: Düsentemperatur-Fehler",
    21: "Pause: Heizbett-Fehler", 22: "Filament entladen", 23: "Pause: Schrittverlust", 24: "Filament laden",
    25: "Motorgeräusch kalibrieren", 26: "Pause: AMS verloren", 27: "Pause: Hotend-Lüfter langsam",
    28: "Pause: Kammertemperatur-Fehler", 29: "Kammer kühlen", 30: "Pause (G-Code)", 32: "Pause: Düse verklebt",
    33: "Pause: Cutter-Fehler", 34: "Pause: Fehler erste Schicht", 35: "Pause: Düse verstopft", 39: "Düsenversatz kalibrieren",
    40: "Hochtemperatur-Nivellierung", 42: "Tür/Abdeckung prüfen", 47: "Nivellierung Phase 1", 48: "Nivellierung Phase 2",
    49: "Kammer aufheizen", 50: "Heizbett abkühlen", 51: "Kalibrierlinien drucken", 52: "Material prüfen",
    54: "Warten auf Heizbett", 55: "Materialposition prüfen", 57: "Oberfläche vermessen", 58: "Thermische Vorkonditionierung",
    63: "Kammertemperatur angleichen", 64: "Hotend vorbereiten", 66: "Kammerluft reinigen", 69: "Düse abkühlen",
    72: "Hotend-Typ erkennen", 74: "Fremdkörper auf Druckbett prüfen", 76: "Vorextrusion", 77: "AMS vorbereiten",
}
HMS_MODULE = {0x03: "Mainboard", 0x05: "Mainboard", 0x07: "AMS", 0x08: "Werkzeugkopf", 0x0C: "Kamera", 0x10: "Kammer", 0x12: "AMS Lite", 0x18: "AMS HT"}
HMS_SEVERITY = {1: "fatal", 2: "serious", 3: "common", 4: "info"}


@lru_cache(maxsize=2)
def _hms(lang: str) -> dict:
    p = BASE / "hms" / f"hms_{lang}.json.gz"
    if not p.exists():
        return {}
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


def error_text(kind: str, code: str, model: str | None = None) -> str:
    code = code.replace("_", "").upper()
    for lang in ("de", "en"):
        entry = _hms(lang).get(kind, {}).get(code)
        if entry:
            for msg, models in entry.items():
                if not models or (model and model in models):
                    return msg
            return next(iter(entry))
    return ""


def _split16(v):
    """H2-Serie packt Temperaturen als (Soll << 16) | Ist."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return None, None
    return v & 0xFFFF, (v >> 16) & 0xFFFF


def _num(v):
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def _fan_pct(v):
    """Lüfter kommen als 0–15 (Stufe) → Prozent."""
    try:
        return round(int(v) / 15 * 100)
    except (TypeError, ValueError):
        return None


def _slot(ams_id: int, tray: int | None) -> dict | None:
    if ams_id in (254, 255):                       # externe Spule
        return {"ams_id": 255, "tray_id": None}
    if 0 <= ams_id < 4 or 128 <= ams_id < 136:     # AMS (2 Pro) 0–3, AMS HT 128–135
        return {"ams_id": ams_id, "tray_id": 0 if ams_id >= 128 else tray}
    return None


def active_tray(raw: dict, tray_now) -> dict | None:
    """Aktiver Slot.

    H2-Serie: ``device.extruder.info[n].snow`` = (AMS-ID << 8) | Slot je Düse, die aktive Düse steht in
    ``device.extruder.state`` (Bits 4–7). Das alte ``ams.tray_now`` bleibt dort beim automatischen
    Umschalten auf Nachbarslots oft auf dem zuerst geladenen Slot stehen – darum hat ``snow`` Vorrang.
    Ältere Modelle: ``ams.tray_now`` = AMS*4 + Slot, 254/255 = externe Spule, ≥128 = AMS HT.
    """
    ext = (raw.get("device") or {}).get("extruder") or {}
    info = [e for e in ext.get("info") or [] if isinstance(e, dict) and "snow" in e]
    if info:
        try:
            cur = (int(ext.get("state") or 0) >> 4) & 0xF
        except (TypeError, ValueError):
            cur = 0
        e = next((e for e in info if _int0(e.get("id")) == cur), info[0] if len(info) == 1 else None)
        if e is not None:
            sn = _int0(e.get("snow"), 0xFFFF)
            if sn != 0xFFFF:
                hit = _slot(sn >> 8, sn & 0xFF)
                if hit:
                    return hit
            else:
                return None                        # Düse hat gerade nichts geladen
    try:
        tn = int(tray_now) if tray_now not in (None, "") else None
    except (TypeError, ValueError):
        return None
    if tn is None or tn == 255:
        return None
    if tn == 254:
        return {"ams_id": 255, "tray_id": None}
    if tn >= 128:
        return _slot(tn, 0)
    return _slot(tn // 4, tn % 4)


def _int0(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def derive(raw: dict, model: str | None, tray_now) -> dict:
    """Rohstatus → aufbereiteter Status für die UI."""
    dev = raw.get("device") or {}
    state = raw.get("gcode_state") or "UNKNOWN"
    s = {
        "state": state, "state_de": STATE_DE.get(state, state.title()),
        "active": state in ("RUNNING", "PREPARE", "PAUSE"),
        "percent": raw.get("mc_percent"), "remaining_min": raw.get("mc_remaining_time"),
        "layer": raw.get("layer_num"), "layers": raw.get("total_layer_num"),
        "job": raw.get("subtask_name") or (raw.get("gcode_file") or "").rsplit("/", 1)[-1] or None,
        "speed": SPEED_DE.get(raw.get("spd_lvl"), None), "speed_pct": raw.get("spd_mag"),
        "wifi": raw.get("wifi_signal"), "nozzle": f'{raw.get("nozzle_diameter") or ""} mm {raw.get("nozzle_type") or ""}'.strip(),
        "print_type": raw.get("print_type"),
    }
    if s["layer"] is None and isinstance(raw.get("3D"), dict):
        s["layer"], s["layers"] = raw["3D"].get("layer_num"), raw["3D"].get("total_layer_num")
    if s["remaining_min"] is not None and s["active"]:
        s["eta"] = (datetime.now() + timedelta(minutes=int(s["remaining_min"]))).isoformat(timespec="minutes")
    stg = raw.get("stg_cur")
    if stg not in (None, -1, 255) and s["active"]:
        s["stage"] = STAGE_DE.get(int(stg), f"Schritt {stg}")

    # Temperaturen: klassische Felder, bei H2-Serie zusätzlich gepackt unter device.*
    noz, noz_t = _num(raw.get("nozzle_temper")), _num(raw.get("nozzle_target_temper"))
    ext = ((dev.get("extruder") or {}).get("info") or [])
    if ext and "temp" in ext[0]:
        noz, noz_t = _split16(ext[0]["temp"])
    bed, bed_t = _num(raw.get("bed_temper")), _num(raw.get("bed_target_temper"))
    if (dev.get("bed") or {}).get("info", {}).get("temp") is not None:
        bed, bed_t = _split16(dev["bed"]["info"]["temp"])
    ch, ch_t = _num(raw.get("chamber_temper")), None
    if (dev.get("ctc") or {}).get("info", {}).get("temp") is not None:
        ch, ch_t = _split16(dev["ctc"]["info"]["temp"])
    s["temps"] = [
        {"key": "nozzle", "label": "Düse", "cur": noz, "target": noz_t},
        {"key": "bed", "label": "Bett", "cur": bed, "target": bed_t},
    ] + ([{"key": "chamber", "label": "Kammer", "cur": ch, "target": ch_t}] if ch is not None else [])
    s["fans"] = [f for f in (
        {"label": "Bauteil", "pct": _fan_pct(raw.get("cooling_fan_speed"))},
        {"label": "Aux", "pct": _fan_pct(raw.get("big_fan1_speed"))},
        {"label": "Kammer", "pct": _fan_pct(raw.get("big_fan2_speed"))},
    ) if f["pct"] is not None]
    lights = {l.get("node"): l.get("mode") for l in raw.get("lights_report") or []}
    s["light"] = lights.get("chamber_light")

    s["tray_now"] = active_tray(raw, tray_now)

    # Fehler / HMS
    msgs = []
    pe = raw.get("print_error")
    if pe and int(pe) != 0:
        code = f"{int(pe):08X}"
        msgs.append({"code": f"{code[:4]}_{code[4:]}", "severity": "serious", "text": error_text("device_error", code, model) or "Druckfehler"})
    for h in raw.get("hms") or []:
        try:
            attr, code = int(h.get("attr", 0)), int(h.get("code", 0))
        except (TypeError, ValueError):
            continue
        full = f"{attr >> 16:04X}{attr & 0xFFFF:04X}{code >> 16:04X}{code & 0xFFFF:04X}"
        msgs.append({"code": "_".join(full[i:i + 4] for i in range(0, 16, 4)),
                     "severity": HMS_SEVERITY.get(code >> 16, "info"),
                     "text": error_text("device_hms", full, model) or f"{HMS_MODULE.get(attr >> 24, 'Drucker')}-Meldung (kein Text hinterlegt)"})
    s["messages"] = msgs
    return s


# ---------------------------------------------------------------- Druckhistorie
JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS print_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, printer_id INTEGER, job_key TEXT, name TEXT,
  started TEXT, ended TEXT, result TEXT, layers INTEGER, snapshot TEXT, usage TEXT,
  slots TEXT, end_percent INTEGER, last_percent INTEGER, task_id INTEGER, usage_src TEXT
);
CREATE INDEX IF NOT EXISTS print_jobs_printer ON print_jobs(printer_id, started);
"""


def job_key(raw: dict) -> str | None:
    for k in ("subtask_id", "task_id", "job_id"):
        v = str(raw.get(k) or "")
        if v and v != "0":
            return f"{k}:{v}"
    name = raw.get("subtask_name") or raw.get("gcode_file")
    return f"name:{name}" if name else None


def track_job(c, printer_id: int, raw: dict, ts: str):
    """Startet/beendet Einträge in print_jobs und rechnet den Filamentverbrauch je Tag ab."""
    state = raw.get("gcode_state")
    if not state:
        return
    open_job = c.execute("SELECT * FROM print_jobs WHERE printer_id=? AND ended IS NULL ORDER BY id DESC LIMIT 1",
                         (printer_id,)).fetchone()
    key = job_key(raw)
    running = state in ("RUNNING", "PREPARE", "PAUSE")
    if running and key:
        if open_job and open_job["job_key"] == key:
            if raw.get("mc_percent") is not None:
                c.execute("UPDATE print_jobs SET last_percent=? WHERE id=?", (raw.get("mc_percent"), open_job["id"]))
            return
        if open_job:
            _close(c, open_job, "abgebrochen", ts)
        snap = {r["tag_key"]: r["consumed_g"] or 0 for r in c.execute("SELECT tag_key, consumed_g FROM tags")}
        c.execute("INSERT INTO print_jobs (printer_id, job_key, name, started, layers, snapshot, slots) VALUES (?,?,?,?,?,?,?)",
                  (printer_id, key, raw.get("subtask_name") or key, ts, raw.get("total_layer_num"), json.dumps(snap),
                   json.dumps(slot_snapshot(c, printer_id))))
    elif open_job and state in ("FINISH", "FAILED", "IDLE"):
        _close(c, open_job, {"FINISH": "fertig", "FAILED": "fehlgeschlagen"}.get(state, "beendet"), ts)


def slot_snapshot(c, printer_id) -> dict:
    """Welche Spule steckt bei Druckstart in welchem Slot (für die spätere Zuordnung der Slicer-Gramm)."""
    out = {}
    for t in c.execute("""SELECT a.ams_id, a.tray_id, a.tag_key, a.empty, g.unit_id, g.filament_ref FROM ams_trays a
                          LEFT JOIN tags g ON g.tag_key=a.tag_key WHERE a.printer_id=?""", (printer_id,)):
        if t["empty"]:
            continue
        out[f"{t['ams_id']}:{t['tray_id']}"] = {"tag_key": t["tag_key"], "unit_id": t["unit_id"], "filament_ref": t["filament_ref"]}
    return out


def _close(c, job, result, ts):
    snap = json.loads(job["snapshot"] or "{}")
    usage = []
    for r in c.execute("SELECT tag_key, consumed_g, filament_ref, unit_id, ams_id, tray_id FROM tags"):
        d = (r["consumed_g"] or 0) - snap.get(r["tag_key"], 0)
        if d >= 0.5:
            usage.append({"tag_key": r["tag_key"], "grams": round(d, 1), "ams_g": round(d, 1), "filament_ref": r["filament_ref"],
                          "unit_id": r["unit_id"], "slot": [r["ams_id"], r["tray_id"]], "source": "ams"})
    end_pct = 100 if result == "fertig" else job["last_percent"]
    c.execute("UPDATE print_jobs SET ended=?, result=?, usage=?, usage_src='ams', end_percent=? WHERE id=?",
              (ts, result, json.dumps(usage), end_pct, job["id"]))
