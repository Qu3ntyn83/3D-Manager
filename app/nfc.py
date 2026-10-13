"""NFC-Scan vom Handy (Android-App „3D Manager“): Bambu-Tag erkennen, anlegen und zuordnen.

Die App liest per Android-NFC immer die UID der Spule. Telefone mit NXP-NFC-Chip können zusätzlich
die verschlüsselten MIFARE-Classic-Blöcke lesen (Schlüssel werden aus der UID abgeleitet) und
liefern dann auch Spulen-ID (tray_uuid), Material, Farbe, Gewicht usw.

Zuordnung zu vorhandenen Tags:
  1. tray_uuid  = Schlüssel der AMS-/Cloud-Tags
  2. UID        = Anfang von tag_uid aus dem AMS-Report (auch in umgekehrter Byte-Reihenfolge)
Ist der Tag unbekannt, wird er angelegt (Schlüssel = tray_uuid oder „NFC:<UID>“). Sieht das AMS den
Tag später, werden beide Einträge zusammengeführt (ams.update_tag → merge_nfc).
"""
from __future__ import annotations

import re
from datetime import datetime

from flask import Blueprint, abort, jsonify, request

from bambu import conn

bp = Blueprint("nfc", __name__, url_prefix="/api/nfc")
_HEX = re.compile(r"^[0-9A-F]+$")


def now():
    return datetime.now().isoformat(timespec="seconds")


def hexnorm(v) -> str:
    h = re.sub(r"[\s:\-]", "", str(v or "")).upper()
    return h if _HEX.match(h) and len(h) % 2 == 0 else ""


def rev(h: str) -> str:
    return "".join(h[i:i + 2] for i in range(len(h) - 2, -1, -2))


def uid_matches(tag_uid: str | None, uid: str) -> bool:
    """AMS meldet tag_uid als 8 Byte; die Karten-UID (4 oder 7 Byte) steht am Anfang – je nach Firmware gedreht."""
    tu = hexnorm(tag_uid)
    return bool(uid) and bool(tu) and (tu.startswith(uid) or tu.startswith(rev(uid)) or tu == uid)


def find_tag(c, uid: str, uuid: str):
    if uuid:
        t = c.execute("SELECT * FROM tags WHERE upper(tray_uuid)=? OR tag_key=?", (uuid, uuid)).fetchone()
        if t:
            return t
    if uid:
        t = c.execute("SELECT * FROM tags WHERE tag_key=?", ("NFC:" + uid,)).fetchone()
        if t:
            return t
        for t in c.execute("SELECT * FROM tags WHERE tag_uid IS NOT NULL AND tag_uid != ''"):
            if uid_matches(t["tag_uid"], uid):
                return t
    return None


def rename_key(c, old: str, new: str):
    if old == new or c.execute("SELECT 1 FROM tags WHERE tag_key=?", (new,)).fetchone():
        return False
    c.execute("UPDATE tags SET tag_key=? WHERE tag_key=?", (new, old))
    c.execute("UPDATE tag_log SET tag_key=? WHERE tag_key=?", (new, old))
    return True


def merge_nfc(c, key: str, tag_uid: str | None, tray_uuid: str | None) -> bool:
    """Vom AMS gesehener, noch unbekannter Tag: per Handy angelegten „NFC:“-Eintrag übernehmen."""
    for r in c.execute("SELECT tag_key FROM tags WHERE tag_key LIKE 'NFC:%'").fetchall():
        if uid_matches(tag_uid, r["tag_key"][4:]):
            if rename_key(c, r["tag_key"], key):
                if tray_uuid:
                    c.execute("UPDATE tags SET tray_uuid=? WHERE tag_key=?", (tray_uuid, key))
                return True
    return False


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


@bp.post("/scan")
def scan():
    d = request.get_json(force=True) or {}
    uid, uuid = hexnorm(d.get("uid")), hexnorm(d.get("tray_uuid"))
    if uuid and set(uuid) == {"0"}:
        uuid = ""
    if not uid and not uuid:
        abort(400, "UID fehlt")
    weight = _int(d.get("weight")) or None
    color = hexnorm(d.get("color")) or None
    claims = {"tag_type": (d.get("detail") or d.get("type") or "").strip() or None,
              "tag_idx": (d.get("material_id") or "").strip() or None, "tag_color": color, "tag_weight": weight}
    ts = now()
    with conn() as c:
        t = find_tag(c, uid, uuid)
        created = False
        if not t:
            key = uuid or "NFC:" + uid
            ref = mode = None
            if uuid:  # dieselbe Spule in der Cloud-Bibliothek schon bewusst verlinkt?
                r = c.execute("SELECT filament_ref FROM spools WHERE upper(rfid)=? AND filament_ref IS NOT NULL AND link_mode IN ('manual','rfid')",
                              (uuid,)).fetchone()
                if r:
                    ref, mode = r["filament_ref"], "cloud"
            c.execute("""INSERT INTO tags (tag_key, tray_uuid, tag_uid, filament_ref, link_mode, full_weight_g, tag_type, tag_idx, tag_color,
                         tag_weight, first_seen, last_seen, in_ams, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                      (key, uuid or None, uid or None, ref, mode, weight or 1000, claims["tag_type"], claims["tag_idx"], color, weight,
                       ts, ts, "per Handy-NFC erfasst"))
            if ref:
                import units
                units.link_tag(c, key, ref, None)
            created = True
        else:
            key = t["tag_key"]
            if uuid and key.startswith("NFC:") and rename_key(c, key, uuid):
                key = uuid
            sets = {k: v for k, v in claims.items() if v and not t[k]}
            if uuid and not t["tray_uuid"]:
                sets["tray_uuid"] = uuid
            if uid and not t["tag_uid"]:
                sets["tag_uid"] = uid
            if not t["in_ams"]:
                sets["last_seen"] = ts
            if sets:
                c.execute(f"UPDATE tags SET {','.join(k + '=?' for k in sets)} WHERE tag_key=?", [*sets.values(), key])
    return jsonify({"ok": True, "key": key, "created": created, "uid": uid, "tray_uuid": uuid or None})
