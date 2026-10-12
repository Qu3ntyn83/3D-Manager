"""Automatische Backups: Datenbank (konsistente SQLite-Kopie) + Fotos als .tar.gz.

Ziel: BACKUP_DIR (Standard: <DATA_DIR>/backups) – z. B. ein NAS-Share per Volume einbinden.
Zeitpunkt und Aufbewahrung unter Einstellungen; zusätzlich „Jetzt sichern“.
Wiederherstellen: Container stoppen, Archiv in ./data entpacken, starten.
"""
from __future__ import annotations

import os
import re
import sqlite3
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, abort, jsonify, send_from_directory

from bambu import DATA_DIR, DB_PATH, conn, get_setting, set_setting

bp = Blueprint("backup", __name__, url_prefix="/api/backups")
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", DATA_DIR / "backups"))
NAME_RE = re.compile(r"^3d-manager-\d{8}-\d{6}\.tar\.gz$")
_lock = threading.Lock()


def run_backup(reason="manuell") -> dict:
    with _lock:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"3d-manager-{stamp}.tar.gz"
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "inventar.db"
            src = sqlite3.connect(DB_PATH)
            dst = sqlite3.connect(snap)
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
            with tarfile.open(BACKUP_DIR / name, "w:gz") as tar:
                tar.add(snap, arcname="inventar.db")
                up = DATA_DIR / "uploads"
                if up.exists():
                    tar.add(up, arcname="uploads")
        with conn() as c:
            set_setting(c, "backup_last", datetime.now().isoformat(timespec="seconds"))
            set_setting(c, "backup_last_reason", reason)
        prune()
        return {"ok": True, "name": name, "size": (BACKUP_DIR / name).stat().st_size}


def prune():
    from insights import setting
    with conn() as c:
        keep = setting(c, "backup_keep_days")
    files = sorted(BACKUP_DIR.glob("3d-manager-*.tar.gz"))
    cutoff = datetime.now() - timedelta(days=max(1, keep))
    for f in files[:-3]:  # die letzten 3 bleiben immer
        if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
            f.unlink(missing_ok=True)


def scheduler():
    if os.environ.get("BACKUP_SCHEDULER", "1") != "1":
        return

    def loop():
        time.sleep(30)
        while True:
            try:
                from insights import setting
                with conn() as c:
                    hour = setting(c, "backup_hour")
                    last = get_setting(c, "backup_last") or ""
                now = datetime.now()
                if hour >= 0 and now.hour == hour and not last.startswith(now.date().isoformat()):
                    run_backup("automatisch")
            except Exception as e:  # noqa: BLE001
                print("Backup-Fehler:", e, flush=True)
            time.sleep(600)

    threading.Thread(target=loop, daemon=True, name="backup").start()


@bp.get("")
def list_backups():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(BACKUP_DIR.glob("3d-manager-*.tar.gz"), reverse=True)
    with conn() as c:
        last = get_setting(c, "backup_last")
    return jsonify({"dir": str(BACKUP_DIR), "last": last,
                    "files": [{"name": f.name, "size": f.stat().st_size,
                               "date": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")} for f in files]})


@bp.post("")
def backup_now():
    try:
        return jsonify(run_backup())
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.get("/<name>")
def download(name):
    if not NAME_RE.match(name):
        abort(404)
    return send_from_directory(BACKUP_DIR, name, as_attachment=True)
