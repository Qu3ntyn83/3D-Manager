"""MakerWorld: eigene Sammlungen in die Bibliothek spiegeln, Modelle mit Tags anlegen, 3MF laden.

Alle Aufrufe gehen an api.bambulab.com (nicht an das Cloudflare-geschützte makerworld.com) und nutzen
den Bambu-Token, mit dem der 3D Manager ohnehin angemeldet ist.

  Sammlungen eines Nutzers   GET /v1/design-service/favoriteslist/{uid}?offset=&limit=
  Inhalt einer Sammlung      GET /v1/design-service/favorites/{collectionId}
  Modell-Details             GET /v1/design-service/design/{designId}
  3MF-Download (signiert)    GET /v1/iot-service/api/user/profile/{profileId}?model_id={modelId}
                             → {"url": …}  (kurzlebig, ohne Token und unverändert abrufen)

Ablauf beim Sync: Sammlungen → Modelle anlegen/zuordnen → Details (Beschreibung, Tags, Lizenz,
Standard-Druckprofil, Titelbild) → Dateien laden (gedrosselt, Limit je Lauf, abschaltbar).
"""
from __future__ import annotations

import base64
import html
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

from flask import Blueprint, abort, jsonify, request

from bambu import conn, get_setting, set_setting

bp = Blueprint("makerworld", __name__, url_prefix="/api/makerworld")
API = "https://api.bambulab.com"
UA = "Mozilla/5.0 (3D-Manager; +https://github.com/Qu3ntyn83/3D-Manager)"
DEFAULTS = {"mw_auto_download": "1", "mw_import_tags": "1", "mw_download_limit": "25", "mw_sync_hours": "6"}
DELAY_API, DELAY_DL = 0.6, 4.0
_lock = threading.Lock()
STATUS = {"running": False, "phase": "", "done": 0, "total": 0, "error": None, "last_run": None, "summary": None}


class MWError(Exception):
    pass


class MWStop(MWError):
    """Weitere Anfragen sinnlos (Anmeldung abgelaufen, CAPTCHA, Sperre)."""


def now():
    return datetime.now().isoformat(timespec="seconds")


def opt(c, k) -> str:
    return get_setting(c, k) or DEFAULTS[k]


# ---------------------------------------------------------------- Zugriff
def client():
    from bambu import _client
    with conn() as c:
        cl = _client(c)
    return cl


def api_get(cl, path: str, auth=True) -> dict:
    from bambu_cloud import AuthExpired, CloudError
    url = f"{getattr(cl, 'api', API)}{path}"
    try:
        r = cl._req("get", url, auth=auth and bool(cl.token), headers={"Referer": "https://makerworld.com/", "User-Agent": UA})
    except AuthExpired as e:
        raise MWStop("Bambu-Anmeldung abgelaufen – unter Bambu & AMS → Drucker & Konto neu anmelden") from e
    except CloudError as e:
        raise MWStop(str(e)) from e
    if r.status_code == 401:
        raise MWStop("Bambu-Anmeldung abgelaufen – unter Bambu & AMS → Drucker & Konto neu anmelden")
    if r.status_code in (403, 418, 429):
        txt = (r.text or "")[:200]
        if "captcha" in txt.lower() or r.status_code in (418, 429):
            raise MWStop(f"MakerWorld blockiert vorübergehend (HTTP {r.status_code}) – später erneut versuchen")
        raise MWError(f"kein Zugriff (HTTP 403) {txt}")
    if r.status_code == 404:
        raise MWError("nicht gefunden")
    if r.status_code != 200:
        raise MWError(f"HTTP {r.status_code}")
    return cl._json(r)


def fetch_bytes(url: str, limit=500 * 1024 * 1024) -> bytes:
    """Signierte CDN-/S3-URL unverändert abrufen (keine Umkodierung der Query, kein Token, keine Weiterleitung)."""
    host = (urlparse(url).hostname or "").lower()
    if not (host.endswith(".bblmw.com") or host.endswith(".bambulab.com") or host.endswith(".amazonaws.com") or host.endswith(".aliyuncs.com")):
        raise MWError(f"unerwarteter Download-Host {host}")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=120) as r:
            data = r.read(limit + 1)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
            return fetch_bytes(e.headers["Location"], limit)
        raise MWError(f"Download HTTP {e.code}")
    if len(data) > limit:
        raise MWError("Datei zu groß")
    return data


def my_uid(cl) -> int:
    parts = (cl.token or "").split(".")
    if len(parts) == 3:
        try:
            p = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            m = re.search(r"(\d{5,})", str(p.get("username") or p.get("uid") or ""))
            if m:
                return int(m.group(1))
        except Exception:  # noqa: BLE001
            pass
    d = api_get(cl, "/v1/design-user-service/my/preference")
    if not d.get("uid"):
        raise MWStop("Bambu-User-ID nicht ermittelbar")
    return int(d["uid"])


def to_text(h: str | None) -> str | None:
    if not h:
        return None
    t = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", h)
    t = re.sub(r"(?i)<li[^>]*>", "• ", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n{3,}", "\n\n", t).strip()[:20000] or None


def model_url(d: dict) -> str:
    slug = d.get("slug") or ""
    return f"https://makerworld.com/de/models/{d['id']}" + (f"-{slug}" if slug else "")


def parse_ref(s: str) -> tuple[str, int]:
    """URL oder Nummer → ('design'|'collection', id)."""
    s = (s or "").strip()
    m = re.search(r"collections?/(\d+)", s)
    if m:
        return "collection", int(m.group(1))
    m = re.search(r"models/(\d+)", s)
    if m:
        return "design", int(m.group(1))
    if s.isdigit():
        return "design", int(s)
    raise MWError("Bitte einen MakerWorld-Link auf ein Modell oder eine Sammlung angeben")


# ---------------------------------------------------------------- Speichern
def upsert_design(c, d: dict, auto_dl: bool) -> int:
    """Modell aus einem Sammlungs-/Design-Eintrag anlegen oder aktualisieren → lib_models.id"""
    import library
    row = c.execute("SELECT id, mw_download FROM lib_models WHERE mw_design_id=?", (d["id"],)).fetchone()
    creator = (d.get("designCreator") or {}).get("name")
    cover = d.get("cover") or d.get("coverUrl")
    if row:
        c.execute("UPDATE lib_models SET mw_cover_url=COALESCE(?, mw_cover_url), creator=COALESCE(creator, ?), mw_model_id=COALESCE(?, mw_model_id) WHERE id=?",
                  (cover, creator, d.get("modelId"), row["id"]))
        if auto_dl and (row["mw_download"] or "none") == "none":
            c.execute("UPDATE lib_models SET mw_download='pending' WHERE id=?", (row["id"],))
        return row["id"]
    cur = c.execute("""INSERT INTO lib_models (title, source, creator, mw_design_id, mw_model_id, mw_url, mw_cover_url, mw_download, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    ((d.get("titleTranslated") or d.get("title") or f"MakerWorld {d['id']}")[:200], "makerworld", creator, d["id"],
                     d.get("modelId"), model_url(d), cover, "pending" if auto_dl else "none", library.now(), library.now()))
    return cur.lastrowid


def inst_printers(i: dict) -> list[str]:
    ext = (i.get("extention") or {}).get("modelInfo") or {}
    out = [(ext.get("compatibility") or {}).get("devProductName")]
    out += [x.get("devProductName") for x in ext.get("otherCompatibility") or [] if isinstance(x, dict)]
    return [x for x in out if x]


def my_printer_models(c) -> list[str]:
    import studio
    names = []
    for r in c.execute("SELECT model, serial FROM printers"):
        names += list(studio.model_names(r["model"], r["serial"]))
    return [n.lower() for n in names]


def pick_instance(c, d: dict) -> dict | None:
    """Druckprofil für den eigenen Drucker bevorzugen, sonst das Standardprofil des Modells."""
    insts = [i for i in d.get("instances") or [] if i.get("profileId")]
    if not insts:
        return None
    mine = my_printer_models(c)
    default = next((i for i in insts if i.get("id") == d.get("defaultInstanceId")), None) or next((i for i in insts if i.get("isDefault")), None)
    for cand in ([default] if default else []) + insts:
        if mine and any(p.lower() in mine for p in inst_printers(cand)):
            return cand
    return default or insts[0]


def apply_details(c, mid: int, d: dict, import_tags: bool):
    import library
    inst = pick_instance(c, d)
    c.execute("""UPDATE lib_models SET title=?, description=COALESCE(description, ?), license=?, creator=COALESCE(?, creator),
                 mw_model_id=?, mw_url=?, mw_cover_url=COALESCE(?, mw_cover_url), mw_profile_id=?, mw_instance_title=?, mw_synced_at=?
                 WHERE id=?""",
              ((d.get("titleTranslated") or d.get("title") or "")[:200] or f"MakerWorld {d['id']}", to_text(d.get("summaryTranslated") or d.get("summary")),
               d.get("license"), (d.get("designCreator") or {}).get("name"), d.get("modelId"), model_url(d), d.get("coverUrl"),
               (inst or {}).get("profileId"), ((inst or {}).get("title") or "") + (f" · {', '.join(inst_printers(inst))}" if inst and inst_printers(inst) else ""),
               library.now(), mid))
    if import_tags:
        library.set_tags(c, mid, (d.get("tagsTranslated") or d.get("tags") or [])[:15], replace=False)


def save_cover(mid: int, url: str | None):
    """Titelbild lokal ablegen, damit die Bibliothek ohne MakerWorld vollständig ist."""
    if not url:
        return
    import io
    import library
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(fetch_bytes(url, 15 * 1024 * 1024)))
        img.thumbnail((800, 800))
        img.convert("RGBA").save(library.cover_path(mid), "PNG", optimize=True)
        with conn() as c:
            c.execute("UPDATE lib_models SET cover='own' WHERE id=? AND (cover IS NULL OR cover='own')", (mid,))
    except Exception:  # noqa: BLE001 – Titelbild ist optional
        pass


def download_model(cl, mid: int) -> str:
    """3MF des gewählten Druckprofils laden → 'done' oder Fehlertext."""
    import library
    with conn() as c:
        m = c.execute("SELECT * FROM lib_models WHERE id=?", (mid,)).fetchone()
    if not m or not m["mw_design_id"]:
        return "kein MakerWorld-Modell"
    if not m["mw_profile_id"] or not m["mw_model_id"]:
        d = api_get(cl, f"/v1/design-service/design/{m['mw_design_id']}")
        with conn() as c:
            apply_details(c, mid, d, False)
            m = c.execute("SELECT * FROM lib_models WHERE id=?", (mid,)).fetchone()
    if not m["mw_profile_id"]:
        return "Modell hat kein Druckprofil (nur Rohdateien)"
    if not cl.token:
        raise MWStop("Für Downloads mit dem Bambu-Konto anmelden")
    info = api_get(cl, f"/v1/iot-service/api/user/profile/{int(m['mw_profile_id'])}?model_id={m['mw_model_id']}")
    url = info.get("url") or (info.get("data") or {}).get("url")
    if not url:
        raise MWError("keine Download-Adresse erhalten")
    data = fetch_bytes(url)
    if data[:2] != b"PK":
        raise MWError("Antwort ist keine 3MF-Datei")
    name = info.get("name") or urlparse(url).path.rsplit("/", 1)[-1] or ""
    if not name.lower().endswith(".3mf"):
        name = re.sub(r"[^\w .()+-]", "_", m["title"])[:120] + ".3mf"
    with conn() as c:
        library.add_file(c, mid, name, data, source="makerworld")
        c.execute("UPDATE lib_models SET mw_download='done' WHERE id=?", (mid,))
    return "done"


# ---------------------------------------------------------------- Sync
def _set(**k):
    STATUS.update(k)


def sync_collection(c, cl, mw_id: int, auto_dl: bool, private=None) -> dict:
    import library
    d = api_get(cl, f"/v1/design-service/favorites/{mw_id}")
    row = c.execute("SELECT id, sync FROM lib_collections WHERE mw_collection_id=?", (mw_id,)).fetchone()
    name = d.get("titleTranslated") or d.get("title") or f"Sammlung {mw_id}"
    if row:
        cid = row["id"]
        c.execute("UPDATE lib_collections SET name=?, description=?, mw_cover_url=?, synced_at=?, mw_private=COALESCE(?, mw_private) WHERE id=?",
                  (name, d.get("description") or None, d.get("cover") or None, library.now(), private, cid))
    else:
        cid = c.execute("""INSERT INTO lib_collections (name, description, source, mw_collection_id, mw_cover_url, mw_private, created_at, synced_at)
                           VALUES (?,?,?,?,?,?,?,?)""", (name, d.get("description") or None, "makerworld", mw_id, d.get("cover") or None,
                                                         1 if private else 0, library.now(), library.now())).lastrowid
    ids = []
    for pos, des in enumerate(d.get("designs") or []):
        if not isinstance(des, dict) or not des.get("id"):
            continue
        mid = upsert_design(c, des, auto_dl)
        ids.append(mid)
        c.execute("INSERT INTO lib_collection_models (collection_id, model_id, position) VALUES (?,?,?) ON CONFLICT(collection_id, model_id) DO UPDATE SET position=excluded.position",
                  (cid, mid, pos))
    # auf MakerWorld entfernte Modelle aus der Sammlung nehmen (Modelle selbst bleiben)
    if ids:
        c.execute(f"DELETE FROM lib_collection_models WHERE collection_id=? AND model_id NOT IN ({','.join('?' * len(ids))})", (cid, *ids))
    else:
        c.execute("DELETE FROM lib_collection_models WHERE collection_id=?", (cid,))
    return {"id": cid, "name": name, "models": len(ids)}


def run_sync(extra: list[tuple[str, int]] | None = None, only_downloads=False) -> dict:
    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "Sync läuft bereits"}
    summary = {"collections": 0, "models": 0, "details": 0, "downloaded": 0, "errors": []}
    try:
        _set(running=True, phase="Anmelden …", done=0, total=0, error=None)
        cl = client()
        if not cl.token and not extra:
            raise MWStop("Nicht mit dem Bambu-Konto verbunden")
        with conn() as c:
            auto_dl = opt(c, "mw_auto_download") == "1"
            tags = opt(c, "mw_import_tags") == "1"
            limit = int(opt(c, "mw_download_limit") or 0)
            ignored = set(json.loads(get_setting(c, "mw_ignored_collections") or "[]"))
            extra_ids = json.loads(get_setting(c, "mw_extra_collections") or "[]")
        # 1. Sammlungen
        if not only_downloads:
            todo: list[tuple[int, bool | None]] = []
            if cl.token and not extra:
                _set(phase="Sammlungen abrufen …")
                uid = my_uid(cl)
                off = 0
                while True:
                    page = api_get(cl, f"/v1/design-service/favoriteslist/{uid}?offset={off}&limit=50")
                    hits = page.get("hits") or []
                    for h in hits:
                        if h.get("id") and h["id"] not in ignored:
                            todo.append((int(h["id"]), h.get("status") not in (None, 2)))
                    off += len(hits)
                    if not hits or off >= int(page.get("total") or 0):
                        break
                    time.sleep(DELAY_API)
                todo += [(int(i), None) for i in extra_ids if int(i) not in ignored and int(i) not in {t[0] for t in todo}]
                with conn() as c:  # Sammlungen, die nicht synchronisiert werden sollen
                    off_ids = {r["mw_collection_id"] for r in c.execute("SELECT mw_collection_id FROM lib_collections WHERE source='makerworld' AND sync=0")}
                todo = [t for t in todo if t[0] not in off_ids]
            for kind, rid in extra or []:
                if kind == "collection":
                    todo.append((rid, None))
            _set(phase="Sammlungen laden …", total=len(todo), done=0)
            for i, (mw_id, private) in enumerate(todo):
                try:
                    with conn() as c:
                        r = sync_collection(c, cl, mw_id, auto_dl, private)
                    summary["collections"] += 1
                    summary["models"] += r["models"]
                except MWStop:
                    raise
                except MWError as e:
                    summary["errors"].append(f"Sammlung {mw_id}: {e}")
                _set(done=i + 1)
                time.sleep(DELAY_API)
            # einzelne Modelle per Link
            for kind, rid in extra or []:
                if kind == "design":
                    with conn() as c:
                        upsert_design(c, {"id": rid, "title": f"MakerWorld {rid}"}, True)
            # 2. Details für neue Modelle
            with conn() as c:
                new = [r["id"] for r in c.execute("SELECT id FROM lib_models WHERE source='makerworld' AND mw_synced_at IS NULL")]
            _set(phase="Modell-Details …", total=len(new), done=0)
            for i, mid in enumerate(new):
                try:
                    with conn() as c:
                        did = c.execute("SELECT mw_design_id FROM lib_models WHERE id=?", (mid,)).fetchone()[0]
                    d = api_get(cl, f"/v1/design-service/design/{did}")
                    with conn() as c:
                        apply_details(c, mid, d, tags)
                    save_cover(mid, d.get("coverUrl"))
                    summary["details"] += 1
                except MWStop:
                    raise
                except MWError as e:
                    summary["errors"].append(f"Modell {mid}: {e}")
                _set(done=i + 1)
                time.sleep(DELAY_API)
        # 3. Dateien
        with conn() as c:
            pend = [r["id"] for r in c.execute("SELECT id FROM lib_models WHERE mw_download='pending' ORDER BY created_at")]
        if limit and not extra:
            pend = pend[:limit]
        if pend and not cl.token:
            summary["errors"].append("Dateien laden erfordert die Anmeldung mit dem Bambu-Konto")
            pend = []
        _set(phase="3MF-Dateien laden …", total=len(pend), done=0)
        for i, mid in enumerate(pend):
            try:
                res = download_model(cl, mid)
                if res == "done":
                    summary["downloaded"] += 1
                else:
                    with conn() as c:
                        c.execute("UPDATE lib_models SET mw_download=? WHERE id=?", (f"Fehler: {res}", mid))
            except MWStop:
                raise
            except MWError as e:
                with conn() as c:
                    c.execute("UPDATE lib_models SET mw_download=? WHERE id=?", (f"Fehler: {e}"[:200], mid))
                summary["errors"].append(f"Download {mid}: {e}")
            _set(done=i + 1)
            time.sleep(DELAY_DL)
        with conn() as c:
            set_setting(c, "mw_last_sync", now())
        return {"ok": True, **summary}
    except MWStop as e:
        STATUS["error"] = str(e)
        return {"ok": False, "error": str(e), **summary}
    except Exception as e:  # noqa: BLE001
        STATUS["error"] = str(e)
        return {"ok": False, "error": str(e), **summary}
    finally:
        STATUS.update(running=False, phase="", last_run=now(), summary=summary)
        _lock.release()


def start_bg(**kw):
    if STATUS["running"]:
        return False
    STATUS["running"] = True
    threading.Thread(target=lambda: run_sync(**kw), daemon=True, name="makerworld").start()
    return True


def scheduler():
    import os
    if os.environ.get("BAMBU_SCHEDULER", "1") != "1":
        return

    def loop():
        time.sleep(120)
        while True:
            try:
                with conn() as c:
                    hours = float(opt(c, "mw_sync_hours") or 0)
                    last = get_setting(c, "mw_last_sync")
                    token = get_setting(c, "bambu_token")
                due = hours > 0 and token and (not last or (datetime.now() - datetime.fromisoformat(last)).total_seconds() > hours * 3600)
                if due and not STATUS["running"]:
                    run_sync()
            except Exception as e:  # noqa: BLE001
                print("MakerWorld-Sync:", e, flush=True)
            time.sleep(900)

    threading.Thread(target=loop, daemon=True, name="makerworld-sched").start()


# ---------------------------------------------------------------- API
@bp.get("/status")
def status():
    with conn() as c:
        s = {k: opt(c, k) for k in DEFAULTS}
        connected = bool(get_setting(c, "bambu_token"))
        pending = c.execute("SELECT COUNT(*) FROM lib_models WHERE mw_download='pending'").fetchone()[0]
        failed = c.execute("SELECT COUNT(*) FROM lib_models WHERE mw_download LIKE 'Fehler%'").fetchone()[0]
        return jsonify({**STATUS, "connected": connected, "settings": {k: (v if k in ("mw_download_limit", "mw_sync_hours") else v == "1") for k, v in s.items()},
                        "last_sync": get_setting(c, "mw_last_sync"), "pending": pending, "failed": failed,
                        "extra_collections": json.loads(get_setting(c, "mw_extra_collections") or "[]")})


@bp.post("/sync")
def sync():
    ok = start_bg()
    return jsonify({"ok": ok, "error": None if ok else "Sync läuft bereits"}), (202 if ok else 409)


@bp.post("/import")
def import_ref():
    """Modell oder (öffentliche) Sammlung per Link übernehmen; Sammlungen werden künftig mit synchronisiert."""
    try:
        kind, rid = parse_ref((request.get_json(force=True) or {}).get("url"))
    except MWError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if kind == "collection":
        with conn() as c:
            ex = set(json.loads(get_setting(c, "mw_extra_collections") or "[]")) | {rid}
            set_setting(c, "mw_extra_collections", json.dumps(sorted(ex)))
            ign = set(json.loads(get_setting(c, "mw_ignored_collections") or "[]")) - {rid}
            set_setting(c, "mw_ignored_collections", json.dumps(sorted(ign)))
    ok = start_bg(extra=[(kind, rid)])
    return jsonify({"ok": ok, "kind": kind, "id": rid, "error": None if ok else "Sync läuft bereits – gleich erneut versuchen"}), (202 if ok else 409)


@bp.get("/profiles/<int:mid>")
def profiles(mid):
    """Alle Druckprofile eines MakerWorld-Modells (zur Auswahl im Dialog)."""
    with conn() as c:
        m = c.execute("SELECT * FROM lib_models WHERE id=?", (mid,)).fetchone()
        if not m or not m["mw_design_id"]:
            abort(404)
        mine = my_printer_models(c)
    try:
        d = api_get(client(), f"/v1/design-service/design/{m['mw_design_id']}")
    except MWError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    out = []
    for i in d.get("instances") or []:
        prs = inst_printers(i)
        out.append({"profile_id": i.get("profileId"), "title": i.get("titleTranslated") or i.get("title"), "printers": prs,
                    "mine": any(p.lower() in mine for p in prs), "default": i.get("id") == d.get("defaultInstanceId") or bool(i.get("isDefault")),
                    "ams": bool(i.get("needAms")), "grams": i.get("weight"), "seconds": i.get("prediction"), "plates": len((i.get("extention") or {}).get("modelInfo", {}).get("plates") or []) or None,
                    "selected": i.get("profileId") == m["mw_profile_id"], "cover": i.get("cover"), "downloads": i.get("downloadCount")})
    return jsonify({"ok": True, "profiles": out})


@bp.post("/download/<int:mid>")
def download_one(mid):
    """Datei eines Modells (erneut) laden – sofort im Hintergrund. Optional {"profile_id": …}."""
    pid = (request.get_json(silent=True) or {}).get("profile_id")
    with conn() as c:
        if not c.execute("SELECT 1 FROM lib_models WHERE id=? AND mw_design_id IS NOT NULL", (mid,)).fetchone():
            abort(404)
        if pid:
            c.execute("UPDATE lib_models SET mw_profile_id=? WHERE id=?", (int(pid), mid))
        c.execute("UPDATE lib_models SET mw_download='pending' WHERE id=?", (mid,))
    ok = start_bg(only_downloads=True)
    return jsonify({"ok": ok, "queued": True}), 202


@bp.put("/settings")
def settings():
    b = request.get_json(force=True) or {}
    with conn() as c:
        for k in DEFAULTS:
            if k in b:
                v = b[k]
                set_setting(c, k, ("1" if v else "0") if isinstance(v, bool) else str(int(float(v or 0))))
    return jsonify({"ok": True})
