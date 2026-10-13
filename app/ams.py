"""AMS-Live: liest die AMS-Slots direkt vom Drucker (MQTT) und verknüpft RFID-Tags mit dem Inventar.

Verbindung je Drucker:
  LAN   : mqtts://<IP>:8883, User "bblp", Passwort = LAN-Zugangscode (Drucker-Display)
  Cloud : mqtts://us.mqtt.bambulab.com:8883, User "u_<uid>", Passwort = Bambu-Token (aus dem Bambu-Login)
Topic: device/<Seriennummer>/report  (Anfrage "pushall" an device/<Seriennummer>/request)

Tags werden über tray_uuid (Bambu-Spulen-ID, auf beiden Tags einer Spule gleich) identifiziert,
ersatzweise über tag_uid (Chip-UID). Ein Tag auf einer Fremdhersteller-Spule meldet weiter das
Original-Filament – die App ordnet ihn deshalb NICHT automatisch über den Tag-Inhalt zu,
sondern über die Verlinkung, die du einmal setzt.
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import threading
import time
import uuid
from datetime import datetime, timedelta

from flask import Blueprint, abort, jsonify, request

from bambu import bambu_catalog, conn, get_setting
from printer_status import JOBS_SCHEMA, STATUS_KEYS, derive, track_job
from bambu_cloud import BambuCloud, CloudError

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    mqtt = None

bp = Blueprint("ams", __name__, url_prefix="/api/ams")

SCHEMA = """
CREATE TABLE IF NOT EXISTS printers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, serial TEXT NOT NULL, mode TEXT DEFAULT 'lan',
  host TEXT, access_code TEXT, port INTEGER DEFAULT 8883, enabled INTEGER DEFAULT 1, model TEXT
);
CREATE TABLE IF NOT EXISTS ams_trays (
  printer_id INTEGER, ams_id INTEGER, tray_id INTEGER, tag_key TEXT, tray_uuid TEXT, tag_uid TEXT,
  tray_type TEXT, tray_info_idx TEXT, sub_brands TEXT, color TEXT, cols TEXT, remain INTEGER, tray_weight INTEGER,
  total_len INTEGER, state INTEGER, empty INTEGER DEFAULT 1, ams_humidity TEXT, ams_temp TEXT, updated TEXT,
  PRIMARY KEY (printer_id, ams_id, tray_id)
);
CREATE TABLE IF NOT EXISTS tags (
  tag_key TEXT PRIMARY KEY, tray_uuid TEXT, tag_uid TEXT, filament_ref INTEGER, link_mode TEXT,
  full_weight_g INTEGER, remain_pct INTEGER, start_pct INTEGER, consumed_g REAL DEFAULT 0,
  tag_type TEXT, tag_idx TEXT, tag_color TEXT, tag_weight INTEGER,
  first_seen TEXT, last_seen TEXT, printer_id INTEGER, ams_id INTEGER, tray_id INTEGER, in_ams INTEGER DEFAULT 0, note TEXT
);
CREATE TABLE IF NOT EXISTS tag_log (tag_key TEXT, ts TEXT, remain INTEGER, grams REAL);
CREATE INDEX IF NOT EXISTS tag_log_key ON tag_log(tag_key, ts);
"""

ZERO_UUID = "0" * 32
ZERO_UID = "0" * 16
CLOUD_HOSTS = {"global": "us.mqtt.bambulab.com", "china": "cn.mqtt.bambulab.com"}


def init_schema():
    with conn() as c:
        c.executescript(SCHEMA)
        c.executescript(JOBS_SCHEMA)
        have = {r[1] for r in c.execute("PRAGMA table_info(print_jobs)")}
        for col, typ in (("slots", "TEXT"), ("end_percent", "INTEGER"), ("last_percent", "INTEGER"), ("task_id", "INTEGER"), ("usage_src", "TEXT")):
            if col not in have:
                c.execute(f"ALTER TABLE print_jobs ADD COLUMN {col} {typ}")
        c.execute("UPDATE printers SET enabled=enabled")  # no-op, Tabelle sicher vorhanden
        c.execute("UPDATE tags SET in_ams=0")  # nach Neustart erst nach erstem Report wieder „im AMS“
        cols = {r[1] for r in c.execute("PRAGMA table_info(spools)")}
        if "full_weight_g" not in cols:
            c.execute("ALTER TABLE spools ADD COLUMN full_weight_g INTEGER")
        # Einmalig: automatische Zuordnungen nach Bambu-Angabe entfernen (Tags sind nur IDs)
        if not c.execute("SELECT 1 FROM settings WHERE key='links_reset_v1'").fetchone():
            auto_rfids = [r[0] for r in c.execute("SELECT rfid FROM spools WHERE link_mode='auto' AND rfid IS NOT NULL")]
            c.execute("UPDATE spools SET filament_ref=NULL, link_mode=NULL WHERE link_mode='auto'")
            for rf in auto_rfids:
                c.execute("UPDATE tags SET filament_ref=NULL, link_mode=NULL WHERE link_mode='cloud' AND upper(tray_uuid)=upper(?)", (rf,))
            c.execute("INSERT INTO settings(key,value) VALUES('links_reset_v1','1')")


def now():
    return datetime.now().isoformat(timespec="seconds")


def tag_key(tray: dict) -> str | None:
    tu = (tray.get("tray_uuid") or "").upper()
    if tu and tu != ZERO_UUID:
        return tu
    uid = (tray.get("tag_uid") or "").upper()
    if uid and uid != ZERO_UID:
        return "UID:" + uid
    return None


def catalog_name(idx: str | None, color: str | None) -> str:
    """Was der Tag behauptet – z. B. 'PLA Basic · Schwarz' (nur Info, keine Zuordnung)."""
    if not idx:
        return ""
    col = (color or "").upper()[:8]
    hits = [e for e in bambu_catalog() if e.get("fila_id") == idx]
    for e in hits:
        if e["colors"][0].upper().lstrip("#")[:8] == col:
            return f'{e["type"]} · {e["name_de"] or e["name_en"]}'
    return hits[0]["type"] if hits else ""


def _int(v, default=None):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- MQTT-Worker
class PrinterConn:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.id = cfg["id"]
        self.serial = cfg["serial"].strip()
        self.client = None
        self.connected = False
        self.error = None
        self.last_msg = None
        self.state: dict[tuple, dict] = {}   # (ams_id, tray_id) -> tray dict
        self.ams_info: dict[int, dict] = {}  # ams_id -> humidity/temp
        self.model = None
        self.raw: dict = {}       # zusammengeführter Druckerstatus (print.*)
        self.tray_now = None
        self._last_pushall = 0.0
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # -- Verbindung
    def _credentials(self):
        if self.cfg["mode"] == "cloud":
            with conn() as c:
                token = get_setting(c, "bambu_token")
                region = get_setting(c, "bambu_region", "global")
                uname = get_setting(c, "bambu_mqtt_user")
            if not token:
                raise RuntimeError("Cloud-Modus: zuerst im Bambu-Tab anmelden")
            if not uname:
                uname = mqtt_username(token, region)
            return CLOUD_HOSTS.get(region, CLOUD_HOSTS["global"]), 8883, uname, token, True
        if not self.cfg.get("host") or not self.cfg.get("access_code"):
            raise RuntimeError("LAN-Modus: IP und Zugangscode fehlen")
        return self.cfg["host"].strip(), int(self.cfg.get("port") or 8883), "bblp", self.cfg["access_code"].strip(), False

    def start(self):
        threading.Thread(target=self._run, daemon=True, name=f"mqtt-{self.serial}").start()

    def stop(self):
        self._stop.set()
        try:
            if self.client:
                self.client.disconnect()
                self.client.loop_stop()
        except Exception:  # noqa: BLE001
            pass

    def _run(self):
        if mqtt is None:
            self.error = "paho-mqtt nicht installiert"
            return
        backoff = 5
        while not self._stop.is_set():
            try:
                host, port, user, pw, verify = self._credentials()
                cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"filinv-{uuid.uuid4().hex[:10]}",
                                 protocol=mqtt.MQTTv311, clean_session=True)
                ctx = ssl.create_default_context()
                if not verify:  # Drucker nutzt ein selbstsigniertes Zertifikat (CN = Seriennummer)
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                cl.tls_set_context(ctx)
                cl.username_pw_set(user, pw)
                cl.on_connect = self._on_connect
                cl.on_disconnect = self._on_disconnect
                cl.on_message = self._on_message
                cl.reconnect_delay_set(5, 60)
                self.client = cl
                cl.connect(host, port, keepalive=60)
                cl.loop_start()
                backoff = 5
                last_push = time.time()
                while not self._stop.is_set():
                    if self.connected and time.time() - last_push > 300:
                        self.pushall()
                        last_push = time.time()
                    if not cl.is_connected() and self.error and "abgelehnt" in self.error:
                        break
                    time.sleep(2)
                cl.loop_stop()
                cl.disconnect()
            except Exception as e:  # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"
                self.connected = False
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 300)

    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0 or getattr(rc, "value", 1) == 0:
            self.connected, self.error = True, None
            client.subscribe(f"device/{self.serial}/report")
            self.pushall()
        else:
            self.connected = False
            self.error = f"Verbindung abgelehnt ({rc}) – Zugangscode/Token und Seriennummer prüfen"

    def _on_disconnect(self, client, userdata, flags, rc, props=None):
        self.connected = False
        if rc and str(rc) not in ("Success", "0") and not self.error:
            self.error = f"Getrennt ({rc})"

    def pushall(self):
        self._last_pushall = time.time()
        if self.client:
            self.client.publish(f"device/{self.serial}/request",
                                json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except ValueError:
            return
        self.last_msg = now()
        p = data.get("print") or {}
        changed = False
        status_changed = False
        with self._lock:
            for k in STATUS_KEYS:
                if k in p:
                    status_changed |= self.raw.get(k) != p[k]
                    self.raw[k] = p[k]
            if isinstance(p.get("device"), dict):
                dev = self.raw.setdefault("device", {})
                for k, v in p["device"].items():
                    dev[k] = v
            ams = (p.get("ams") or {})
            if "tray_now" in ams:
                self.tray_now = ams["tray_now"]
            for unit in ams.get("ams") or []:
                aid = _int(unit.get("id"), 0)
                info = self.ams_info.setdefault(aid, {})
                for k in ("humidity", "humidity_raw", "temp"):
                    if k in unit:
                        info[k] = unit[k]
                for tr in unit.get("tray") or []:
                    changed |= self._merge(aid, _int(tr.get("id"), 0), tr)
            # externe Spule(n): vt_tray (ältere Modelle) bzw. vir_slot (H2-Serie)
            ext = list(p.get("vir_slot") or [])
            if p.get("vt_tray"):
                ext.append(p["vt_tray"])
            for tr in ext:
                tid = _int(tr.get("id"), 255)
                changed |= self._merge(255, tid, tr)
        if changed:
            self.persist()
        # Nur Delta-Meldungen bekommen (z. B. Drucker war beim Verbinden kurz weg)? → Vollstatus anfordern
        if ("gcode_state" not in self.raw or not self.state) and time.time() - self._last_pushall > 30:
            self.pushall()
        if status_changed and "gcode_state" in self.raw:
            try:
                with conn() as c:
                    track_job(c, self.id, dict(self.raw), now())
            except Exception as e:  # noqa: BLE001
                print("Job-Tracking:", e, flush=True)

    def status(self) -> dict | None:
        with self._lock:
            if not self.raw:
                return None
            return derive(dict(self.raw), self.cfg.get("model"), self.tray_now)

    def _merge(self, aid, tid, tr) -> bool:
        cur = self.state.setdefault((aid, tid), {})
        before = json.dumps(cur, sort_keys=True)
        cur.update(tr)
        return before != json.dumps(cur, sort_keys=True)

    # -- in die DB
    def persist(self):
        ts = now()
        with self._lock:
            snapshot = {k: dict(v) for k, v in self.state.items()}
            info = {k: dict(v) for k, v in self.ams_info.items()}
        with conn() as c:
            seen_keys = set()
            for (aid, tid), tr in snapshot.items():
                key = tag_key(tr)
                state = _int(tr.get("state"))
                has_meta = bool(tr.get("tray_type")) or bool(tr.get("tray_info_idx"))
                empty = not spool_loaded(state, has_meta) or (aid == 255 and not has_meta)
                if empty:
                    key = None
                remain = _int(tr.get("remain"), -1)
                color = (tr.get("tray_color") or "").upper()
                cols = tr.get("cols") or ([color] if color else [])
                c.execute("""INSERT INTO ams_trays (printer_id, ams_id, tray_id, tag_key, tray_uuid, tag_uid, tray_type, tray_info_idx,
                               sub_brands, color, cols, remain, tray_weight, total_len, state, empty, ams_humidity, ams_temp, updated)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                             ON CONFLICT(printer_id, ams_id, tray_id) DO UPDATE SET tag_key=excluded.tag_key, tray_uuid=excluded.tray_uuid,
                               tag_uid=excluded.tag_uid, tray_type=excluded.tray_type, tray_info_idx=excluded.tray_info_idx,
                               sub_brands=excluded.sub_brands, color=excluded.color, cols=excluded.cols, remain=excluded.remain,
                               tray_weight=excluded.tray_weight, total_len=excluded.total_len, state=excluded.state, empty=excluded.empty,
                               ams_humidity=excluded.ams_humidity, ams_temp=excluded.ams_temp, updated=excluded.updated""",
                          (self.id, aid, tid, key, tr.get("tray_uuid"), tr.get("tag_uid"), tr.get("tray_type"), tr.get("tray_info_idx"),
                           tr.get("tray_sub_brands"), color, json.dumps([x.upper() for x in cols]), remain, _int(tr.get("tray_weight"), 0),
                           _int(tr.get("total_len")), state, 1 if empty else 0,
                           info.get(aid, {}).get("humidity_raw") or info.get(aid, {}).get("humidity"), info.get(aid, {}).get("temp"), ts))
                if key:
                    seen_keys.add(key)
                    update_tag(c, key, tr, self.id, aid, tid, remain, ts)
            # Tags, die dieser Drucker vorher hatte und jetzt nicht mehr: aus dem AMS genommen
            q = "UPDATE tags SET in_ams=0 WHERE printer_id=? AND in_ams=1"
            if seen_keys:
                q += f" AND tag_key NOT IN ({','.join('?' * len(seen_keys))})"
            c.execute(q, (self.id, *seen_keys))


def update_tag(c, key, tr, pid, aid, tid, remain, ts):
    old = c.execute("SELECT * FROM tags WHERE tag_key=?", (key,)).fetchone()
    tag_w = _int(tr.get("tray_weight"), 0) or None
    vals = dict(tray_uuid=tr.get("tray_uuid"), tag_uid=tr.get("tag_uid"), tag_type=tr.get("tray_type"),
                tag_idx=tr.get("tray_info_idx"), tag_color=(tr.get("tray_color") or "").upper(), tag_weight=tag_w,
                last_seen=ts, printer_id=pid, ams_id=aid, tray_id=tid, in_ams=1)
    if remain is not None and remain >= 0:
        vals["remain_pct"] = remain
    if not old and tr.get("tag_uid"):
        # per Handy-NFC vorab erfasster Tag? → übernehmen statt doppelt anlegen
        import nfc
        if nfc.merge_nfc(c, key, tr.get("tag_uid"), tr.get("tray_uuid")):
            old = c.execute("SELECT * FROM tags WHERE tag_key=?", (key,)).fetchone()
    if not old:
        ref, mode = None, None
        # Gleiche RFID in der Cloud-Bibliothek schon verlinkt? → übernehmen
        if tr.get("tray_uuid") and tr["tray_uuid"] != ZERO_UUID:
            # nur bewusst gesetzte Verlinkungen übernehmen – nie Bambus Material-/Farbangabe
            r = c.execute("SELECT filament_ref FROM spools WHERE upper(rfid)=upper(?) AND filament_ref IS NOT NULL AND link_mode IN ('manual','rfid')",
                          (tr["tray_uuid"],)).fetchone()
            if r:
                ref, mode = r["filament_ref"], "cloud"
        vals.update(tag_key=key, first_seen=ts, filament_ref=ref, link_mode=mode,
                    full_weight_g=tag_w or 1000, start_pct=vals.get("remain_pct"))
        c.execute(f"INSERT INTO tags ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})", list(vals.values()))
    else:
        # Verbrauch protokollieren (nur Abnahme zählt; Zunahme = neue Spule / Korrektur)
        if "remain_pct" in vals and old["remain_pct"] is not None and vals["remain_pct"] != old["remain_pct"]:
            full = old["full_weight_g"] or 1000
            delta = old["remain_pct"] - vals["remain_pct"]
            if delta > 0:
                vals["consumed_g"] = (old["consumed_g"] or 0) + delta / 100 * full
        c.execute(f"UPDATE tags SET {','.join(k + '=?' for k in vals)} WHERE tag_key=?", [*vals.values(), key])
        if "remain_pct" in vals and old["remain_pct"] is not None and old["remain_pct"] > vals["remain_pct"] >= 0:
            import units
            units.ams_delta(c, key, old["remain_pct"] - vals["remain_pct"], old["full_weight_g"])
    if "remain_pct" in vals and (not old or old["remain_pct"] != vals["remain_pct"]):
        full = (old["full_weight_g"] if old else None) or tag_w or 1000
        c.execute("INSERT INTO tag_log (tag_key, ts, remain, grams) VALUES (?,?,?,?)",
                  (key, ts, vals["remain_pct"], round(vals["remain_pct"] / 100 * full, 1)))


def spool_loaded(state, has_meta) -> bool:
    """Tray-Status-Bitfeld: 0x01 Spule, 0x02 Metadaten, 0x04 Bewegung, 0x08 stabil, 0x10 RFID."""
    if state is None:
        return has_meta
    if not state & 0x01:
        return False
    if state <= 3:
        return state == 3
    return bool(state & 0x08) or has_meta


def mqtt_username(token: str, region: str) -> str:
    """u_<uid> – aus dem JWT, sonst über die Preference-API."""
    parts = token.split(".")
    if len(parts) == 3:
        try:
            b = parts[1] + "=" * (-len(parts[1]) % 4)
            u = json.loads(base64.urlsafe_b64decode(b)).get("username")
            if u:
                return u
        except Exception:  # noqa: BLE001
            pass
    cl = BambuCloud(region, token)
    r = cl._req("get", f"{cl.api}/v1/design-user-service/my/preference")
    uid = cl._json(r).get("uid")
    if not uid:
        raise RuntimeError("Bambu-User-ID nicht ermittelbar")
    return f"u_{uid}"


# ---------------------------------------------------------------- Manager
class Manager:
    def __init__(self):
        self.conns: dict[int, PrinterConn] = {}
        self.lock = threading.Lock()

    def reload(self):
        if os.environ.get("AMS_MQTT", "1") != "1":
            return
        with conn() as c:
            cfgs = [dict(r) for r in c.execute("SELECT * FROM printers WHERE enabled=1")]
        with self.lock:
            wanted = {p["id"]: p for p in cfgs}
            for pid in list(self.conns):
                if pid not in wanted or self.conns[pid].cfg != wanted[pid]:
                    self.conns.pop(pid).stop()
            for pid, cfg in wanted.items():
                if pid not in self.conns:
                    pc = PrinterConn(cfg)
                    self.conns[pid] = pc
                    pc.start()

    def status(self, pid):
        pc = self.conns.get(pid)
        if not pc:
            return {"connected": False, "error": None, "last_msg": None, "running": False}
        return {"connected": pc.connected, "error": pc.error, "last_msg": pc.last_msg, "running": True}

    def printer_status(self, pid):
        pc = self.conns.get(pid)
        return pc.status() if pc and pc.connected else None


manager = Manager()


def start(app):
    if getattr(app, "_ams_started", False):
        return
    app._ams_started = True
    init_schema()
    manager.reload()


# ---------------------------------------------------------------- Hilfen für andere Module
def tag_out(r) -> dict:
    d = dict(r)
    full = d.get("full_weight_g") or 1000
    d["remain_g"] = round(d["remain_pct"] / 100 * full) if d.get("remain_pct") is not None and d["remain_pct"] >= 0 else None
    d["claims"] = catalog_name(d.get("tag_idx"), d.get("tag_color"))
    d["in_ams"] = bool(d.get("in_ams"))
    d["consumed_g"] = round(d.get("consumed_g") or 0)
    return d


def linked_tags() -> dict[int, list]:
    out: dict[int, list] = {}
    try:
        with conn() as c:
            rows = c.execute("""SELECT t.*, p.name AS printer_name FROM tags t LEFT JOIN printers p ON p.id=t.printer_id
                                WHERE t.filament_ref IS NOT NULL ORDER BY t.in_ams DESC, t.last_seen DESC""").fetchall()
            for r in rows:
                out.setdefault(r["filament_ref"], []).append(tag_out(r))
    except Exception:  # noqa: BLE001 – Tabelle evtl. noch nicht da
        pass
    return out


def propagate_link(c, rfid: str | None, filament_id: int | None, source: str):
    """Cloud-Spule ↔ AMS-Tag: gleiche RFID (tray_uuid) → gleiche Verlinkung."""
    if not rfid or rfid.upper() == ZERO_UUID:
        return
    try:
        if source == "cloud":
            c.execute("UPDATE tags SET filament_ref=?, link_mode=? WHERE upper(tray_uuid)=upper(?)",
                      (filament_id, "cloud" if filament_id else None, rfid))
        else:
            c.execute("UPDATE spools SET filament_ref=?, link_mode=? WHERE upper(rfid)=upper(?)",
                      (filament_id, "manual" if filament_id else None, rfid))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- API
PRINTER_FIELDS = ("name", "serial", "mode", "host", "access_code", "port", "enabled", "model")


def _printer_out(r):
    d = dict(r)
    d["access_code"] = "••••••" if d.get("access_code") else ""
    d.update(manager.status(d["id"]))
    return d


@bp.get("/printers")
def printers():
    with conn() as c:
        return jsonify([_printer_out(r) for r in c.execute("SELECT * FROM printers ORDER BY id")])


@bp.post("/printers")
def add_printer():
    b = request.get_json(force=True)
    if not (b.get("serial") or "").strip():
        return jsonify({"ok": False, "error": "Seriennummer fehlt"}), 400
    vals = {k: b[k] for k in PRINTER_FIELDS if k in b}
    vals["serial"] = vals["serial"].strip().upper()
    with conn() as c:
        cur = c.execute(f"INSERT INTO printers ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})", list(vals.values()))
        pid = cur.lastrowid
    manager.reload()
    return jsonify({"ok": True, "id": pid})


@bp.put("/printers/<int:pid>")
def edit_printer(pid):
    b = request.get_json(force=True)
    vals = {k: b[k] for k in PRINTER_FIELDS if k in b and not (k == "access_code" and b[k] in ("", "••••••"))}
    if not vals:
        return jsonify({"ok": True})
    with conn() as c:
        c.execute(f"UPDATE printers SET {','.join(k + '=?' for k in vals)} WHERE id=?", [*vals.values(), pid])
    manager.reload()
    return jsonify({"ok": True})


@bp.delete("/printers/<int:pid>")
def del_printer(pid):
    with conn() as c:
        c.execute("DELETE FROM printers WHERE id=?", (pid,))
        c.execute("DELETE FROM ams_trays WHERE printer_id=?", (pid,))
        c.execute("UPDATE tags SET in_ams=0 WHERE printer_id=?", (pid,))
    manager.reload()
    return "", 204


@bp.post("/printers/import-cloud")
def import_cloud():
    """Drucker aus dem Bambu-Account übernehmen (Cloud-Modus)."""
    with conn() as c:
        token, region = get_setting(c, "bambu_token"), get_setting(c, "bambu_region", "global")
    if not token:
        return jsonify({"ok": False, "error": "Zuerst mit dem Bambu-Account anmelden"}), 400
    cl = BambuCloud(region, token)
    try:
        r = cl._req("get", f"{cl.api}/v1/iot-service/api/user/bind")
        devs = cl._json(r).get("devices") or []
    except CloudError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    added = 0
    with conn() as c:
        for d in devs:
            sn = (d.get("dev_id") or "").upper()
            if not sn or c.execute("SELECT 1 FROM printers WHERE serial=?", (sn,)).fetchone():
                continue
            c.execute("INSERT INTO printers (name, serial, mode, model, enabled) VALUES (?,?,?,?,1)",
                      (d.get("name") or sn, sn, "cloud", d.get("dev_product_name") or d.get("dev_model_name")))
            added += 1
    manager.reload()
    return jsonify({"ok": True, "found": len(devs), "added": added})


@bp.post("/refresh")
def refresh():
    for pc in list(manager.conns.values()):
        pc.pushall()
    return jsonify({"ok": True})


@bp.get("/live")
def live():
    with conn() as c:
        prs = [_printer_out(r) for r in c.execute("SELECT * FROM printers ORDER BY id")]
        fil = {r["id"]: dict(r) for r in c.execute("SELECT id, brand, material, color_name, sku, colors, qty FROM filaments")}
        for p in prs:
            trays = []
            for t in c.execute("SELECT * FROM ams_trays WHERE printer_id=? ORDER BY ams_id, tray_id", (p["id"],)):
                d = dict(t)
                d["cols"] = json.loads(d["cols"] or "[]")
                d["claims"] = catalog_name(d["tray_info_idx"], d["color"])
                d["tag"] = None
                if d["tag_key"]:
                    tg = c.execute("SELECT * FROM tags WHERE tag_key=?", (d["tag_key"],)).fetchone()
                    if tg:
                        d["tag"] = tag_out(tg)
                        f = fil.get(tg["filament_ref"])
                        if f:
                            f = dict(f)
                            f["colors"] = json.loads(f["colors"] or "[]")
                        d["tag"]["filament"] = f
                trays.append(d)
            p["trays"] = trays
        return jsonify(prs)


@bp.get("/tags")
def tags():
    with conn() as c:
        fil = {r["id"]: dict(r) for r in c.execute("SELECT id, brand, material, color_name, sku FROM filaments")}
        out = []
        for r in c.execute("""SELECT t.*, p.name AS printer_name FROM tags t LEFT JOIN printers p ON p.id=t.printer_id
                              ORDER BY t.in_ams DESC, t.filament_ref IS NOT NULL, t.last_seen DESC"""):
            d = tag_out(r)
            d["filament"] = fil.get(d["filament_ref"])
            out.append(d)
        return jsonify(out)


@bp.post("/tags/<path:key>/link")
def link_tag(key):
    fid = request.get_json(force=True).get("filament_id")
    with conn() as c:
        t = c.execute("SELECT * FROM tags WHERE tag_key=?", (key,)).fetchone()
        if not t:
            abort(404)
        f = c.execute("SELECT * FROM filaments WHERE id=?", (fid,)).fetchone() if fid else None
        if fid and not f:
            abort(400)
        c.execute("UPDATE tags SET filament_ref=?, link_mode=? WHERE tag_key=?", (fid, "manual" if fid else None, key))
        # Vollgewicht der Spule aus dem Inventar übernehmen, wenn dort gepflegt
        if f and f["weight_g"] and not t["full_weight_g"]:
            c.execute("UPDATE tags SET full_weight_g=? WHERE tag_key=?", (f["weight_g"], key))
        propagate_link(c, t["tray_uuid"], fid, "tag")
    return jsonify({"ok": True})


@bp.put("/tags/<path:key>")
def edit_tag(key):
    b = request.get_json(force=True)
    vals = {}
    if "full_weight_g" in b:
        vals["full_weight_g"] = _int(b["full_weight_g"]) or None
    if "note" in b:
        vals["note"] = (b["note"] or "").strip()
    if b.get("reset_consumption"):
        vals["consumed_g"] = 0
    with conn() as c:
        if vals:
            c.execute(f"UPDATE tags SET {','.join(k + '=?' for k in vals)} WHERE tag_key=?", [*vals.values(), key])
        r = c.execute("SELECT * FROM tags WHERE tag_key=?", (key,)).fetchone()
        if not r:
            abort(404)
        return jsonify(tag_out(r))


@bp.delete("/tags/<path:key>")
def del_tag(key):
    with conn() as c:
        c.execute("DELETE FROM tags WHERE tag_key=?", (key,))
        c.execute("DELETE FROM tag_log WHERE tag_key=?", (key,))
    return "", 204


@bp.get("/tags/<path:key>/log")
def tag_log(key):
    with conn() as c:
        since = (datetime.now() - timedelta(days=int(request.args.get("days", 90)))).isoformat()
        rows = c.execute("SELECT ts, remain, grams FROM tag_log WHERE tag_key=? AND ts>=? ORDER BY ts", (key, since)).fetchall()
        return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------- Druckerstatus
def _job_out(c, r, fil):
    d = dict(r)
    d.pop("snapshot", None)
    d.pop("slots", None)
    usage = json.loads(d.pop("usage") or "[]")
    for u in usage:
        f = fil.get(u.get("filament_ref"))
        u["filament"] = f
        if u.get("unit_id"):
            u["unit_code"] = f"S{u['unit_id']:04d}"
    d["usage"] = usage
    d["grams"] = round(sum(u["grams"] for u in usage if not u.get("skipped")), 1)
    d["unassigned"] = sum(1 for u in usage if u.get("unassigned"))
    if d["started"] and d["ended"]:
        d["minutes"] = round((datetime.fromisoformat(d["ended"]) - datetime.fromisoformat(d["started"])).total_seconds() / 60)
    import insights
    d["cost"] = insights.job_costs(c, d) if d.get("ended") else None
    return d


@bp.get("/status")
def printer_status():
    """Kompakter Status aller Drucker (für Kopfzeile und Drucker-Tab)."""
    with conn() as c:
        fil = {r["id"]: {**dict(r), "colors": json.loads(r["colors"] or "[]")}
               for r in c.execute("SELECT id, brand, material, color_name, colors FROM filaments")}
        out = []
        for r in c.execute("SELECT * FROM printers WHERE enabled=1 ORDER BY id"):
            p = _printer_out(r)
            st = manager.printer_status(r["id"])
            if st and st.get("tray_now"):
                tn = st["tray_now"]
                q = "SELECT * FROM ams_trays WHERE printer_id=? AND ams_id=?" + (" AND tray_id=?" if tn["tray_id"] is not None else "")
                t = c.execute(q, (r["id"], tn["ams_id"], *([tn["tray_id"]] if tn["tray_id"] is not None else []))).fetchone()
                if t:
                    tag = c.execute("SELECT * FROM tags WHERE tag_key=?", (t["tag_key"],)).fetchone() if t["tag_key"] else None
                    tn.update(color=t["color"], tray_type=t["tray_type"], claims=catalog_name(t["tray_info_idx"], t["color"]),
                              remain=t["remain"], tag=tag_out(tag) if tag else None,
                              filament=fil.get(tag["filament_ref"]) if tag and tag["filament_ref"] else None)
            p["status"] = st
            job = c.execute("SELECT * FROM print_jobs WHERE printer_id=? AND ended IS NULL ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
            p["job"] = _job_out(c, job, fil) if job else None
            out.append(p)
        return jsonify(out)


@bp.get("/jobs")
def jobs():
    lim = min(int(request.args.get("limit", 25)), 200)
    with conn() as c:
        fil = {r["id"]: {**dict(r), "colors": json.loads(r["colors"] or "[]")}
               for r in c.execute("SELECT id, brand, material, color_name, colors FROM filaments")}
        names = {r["id"]: r["name"] or r["serial"] for r in c.execute("SELECT id, name, serial FROM printers")}
        rows = c.execute("SELECT * FROM print_jobs ORDER BY id DESC LIMIT ?", (lim,)).fetchall()
        out = []
        for r in rows:
            d = _job_out(c, r, fil)
            d["printer"] = names.get(r["printer_id"])
            out.append(d)
        return jsonify(out)


# ---------------------------------------------------------------- RFID: AMS-Tags + Cloud-Spulen vereint
def _claim_cloud(r) -> str:
    from bambu import catalog_match
    cat = catalog_match({"filament_id": r["filament_id"], "color": r["color"], "tray_id_name": r["tray_id_name"]})
    if len(cat) == 1:
        return f'{cat[0]["type"]} · {cat[0]["name_de"] or cat[0]["name_en"]}'
    return " ".join(x for x in (r["vendor"], r["name"]) if x)


@bp.get("/rfid")
def rfid_list():
    """Alle bekannten RFID-Tags – egal ob aus dem AMS (MQTT) oder aus der Bambu-Cloud-Bibliothek."""
    with conn() as c:
        fil = {r["id"]: {**dict(r), "colors": json.loads(r["colors"] or "[]")}
               for r in c.execute("SELECT id, brand, material, color_name, sku, colors FROM filaments")}
        names = {r["id"]: r["name"] or r["serial"] for r in c.execute("SELECT id, name, serial FROM printers")}
        cloud = {}
        for r in c.execute("SELECT * FROM spools WHERE rfid IS NOT NULL AND rfid != ''"):
            cloud[r["rfid"].upper()] = r
        out, seen = [], set()
        for t in c.execute("SELECT * FROM tags ORDER BY in_ams DESC, last_seen DESC"):
            d = tag_out(t)
            cs = cloud.get((t["tray_uuid"] or "").upper())
            if cs is not None:
                seen.add(cs["rfid"].upper())
            pct = d["remain_pct"] if d.get("remain_pct") is not None and d["remain_pct"] >= 0 else None
            if pct is None and cs is not None and cs["total_net_weight"] and cs["net_weight"] is not None:
                pct = round(cs["net_weight"] / cs["total_net_weight"] * 100)
            full = d["full_weight_g"] or 1000
            out.append({
                "key": t["tag_key"], "rfid": t["tray_uuid"] if t["tray_uuid"] and t["tray_uuid"] != ZERO_UUID else None,
                "tag_uid": t["tag_uid"], "sources": ["ams"] + (["cloud"] if cs is not None else []),
                "claims": d["claims"] or (_claim_cloud(cs) if cs is not None else "") or (t["tag_type"] or ""),
                "claim_color": (t["tag_color"] or "").upper(),
                "percent": pct, "full_weight_g": full, "grams": round(pct / 100 * full) if pct is not None else None,
                "consumed_g": d["consumed_g"], "filament_ref": t["filament_ref"], "filament": fil.get(t["filament_ref"]),
                "unit_id": t["unit_id"] if "unit_id" in t.keys() else None,
                "in_ams": d["in_ams"], "slot": [t["ams_id"], t["tray_id"]] if d["in_ams"] else None,
                "printer": names.get(t["printer_id"]), "last_seen": t["last_seen"],
            })
        for key, r in cloud.items():
            if key in seen:
                continue
            pct = round(r["net_weight"] / r["total_net_weight"] * 100) if r["total_net_weight"] and r["net_weight"] is not None else None
            full = r["full_weight_g"] or r["total_net_weight"] or 1000
            out.append({
                "key": r["rfid"], "rfid": r["rfid"], "tag_uid": None, "sources": ["cloud"],
                "claims": _claim_cloud(r), "claim_color": (r["color"] or "").upper().lstrip("#"),
                "percent": pct, "full_weight_g": full, "grams": round(pct / 100 * full) if pct is not None else None,
                "consumed_g": None, "filament_ref": r["filament_ref"], "filament": fil.get(r["filament_ref"]),
                "in_ams": bool(r["in_printer"]), "slot": [r["ams_id"], int(r["slot_id"])] if r["in_printer"] and str(r["slot_id"] or "").isdigit() else None,
                "printer": r["device_name"], "last_seen": r["last_seen"], "gone": not r["present"],
            })
        # Restgewicht der zugeordneten Spule hat Vorrang (berücksichtigt Wiegen/Druckaufträge)
        um = {r["id"]: r for r in c.execute("SELECT id, remaining_g, initial_g FROM spool_units")}
        for o in out:
            u = um.get(o.get("unit_id"))
            if u and u["remaining_g"] is not None:
                o["grams"] = round(u["remaining_g"])
                o["full_weight_g"] = int(u["initial_g"] or o["full_weight_g"] or 1000)
                o["percent"] = round(u["remaining_g"] / (u["initial_g"] or 1000) * 100)
        return jsonify(out)


@bp.post("/rfid/<path:key>/link")
def rfid_link(key):
    """Tag ↔ beliebige Inventar-Spule. Gilt für AMS-Tag und Cloud-Spule mit derselben RFID."""
    fid = request.get_json(force=True).get("filament_id")
    with conn() as c:
        if fid and not c.execute("SELECT 1 FROM filaments WHERE id=?", (fid,)).fetchone():
            abort(400)
        mode = "manual" if fid else None
        n = c.execute("UPDATE tags SET filament_ref=?, link_mode=? WHERE tag_key=? OR upper(tray_uuid)=upper(?)", (fid, mode, key, key)).rowcount
        uu = [r[0] for r in c.execute("SELECT tray_uuid FROM tags WHERE tag_key=?", (key,)) if r[0]] + [key]
        for u in uu:
            n += c.execute("UPDATE spools SET filament_ref=?, link_mode=? WHERE upper(rfid)=upper(?)", (fid, mode, u)).rowcount
        if not n:
            abort(404)
        import units
        if c.execute("SELECT 1 FROM tags WHERE tag_key=? OR upper(tray_uuid)=upper(?)", (key, key)).fetchone():
            units.link_tag(c, key, fid, request.get_json(force=True).get("unit_id"))
    return jsonify({"ok": True})


@bp.post("/rfid/<path:key>/empty")
def rfid_empty(key):
    """„Spule leer“: zugehörige Spule als leer markieren und den Tag für die nächste Spule freigeben."""
    import units
    with conn() as c:
        t = c.execute("SELECT * FROM tags WHERE tag_key=? OR upper(tray_uuid)=upper(?)", (key, key)).fetchone()
        if not t:
            abort(404)
        if t["unit_id"]:
            units.mark_empty(c, t["unit_id"])
        elif t["filament_ref"]:
            units.adjust_filament(c, t["filament_ref"], -1)
        c.execute("UPDATE tags SET filament_ref=NULL, link_mode=NULL, unit_id=NULL WHERE tag_key=?", (t["tag_key"],))
        if t["tray_uuid"]:
            c.execute("UPDATE spools SET filament_ref=NULL, link_mode=NULL WHERE upper(rfid)=upper(?)", (t["tray_uuid"],))
    return jsonify({"ok": True})


@bp.put("/rfid/<path:key>")
def rfid_edit(key):
    b = request.get_json(force=True)
    w = _int(b.get("full_weight_g")) or None
    with conn() as c:
        c.execute("UPDATE tags SET full_weight_g=? WHERE tag_key=? OR upper(tray_uuid)=upper(?)", (w, key, key))
        uu = [r[0] for r in c.execute("SELECT tray_uuid FROM tags WHERE tag_key=?", (key,)) if r[0]] + [key]
        for u in uu:
            c.execute("UPDATE spools SET full_weight_g=? WHERE upper(rfid)=upper(?)", (w, u))
    return jsonify({"ok": True})
