"""Smoke- und Logiktests für 3D Manager (laufen ohne Netzwerk, Drucker und Bambu-Account)."""
import json
import sqlite3


def test_index_and_static(client):
    assert client.get("/").status_code == 200
    assert client.get("/healthz").data == b"ok"
    assert client.get("/sw.js").status_code == 200
    assert client.get("/manifest.webmanifest").status_code == 200


def test_seed_creates_units(client):
    fil = client.get("/api/filaments").json
    assert len(fil) > 50
    for f in fil:
        assert len([u for u in f["units"] if u["status"] != "empty"]) == f["qty"]


def test_adjust_and_qty_sync(client):
    f = client.post("/api/filaments", json={"brand": "Test", "material": "PETG-CF", "color_name": "Grau", "qty": 2,
                                            "colors": ["#777777FF"], "net_weight_g": 800}).json
    assert f["qty"] == 2 and len(f["units"]) == 2 and f["units"][0]["initial_g"] == 800
    assert client.post(f"/api/filaments/{f['id']}/adjust", json={"delta": 1}).json["qty"] == 3
    r = client.post(f"/api/filaments/{f['id']}/adjust", json={"delta": -1}).json
    assert r["qty"] == 2 and sum(u["status"] == "empty" for u in r["units"]) == 1
    assert client.put(f"/api/filaments/{f['id']}", json={"qty": 4}).json["qty"] == 4


def test_weigh_with_tare(client):
    f = client.post("/api/filaments", json={"brand": "Test", "material": "PLA", "color_name": "Weiß", "qty": 1, "colors": ["#FFFFFFFF"]}).json
    uid = f["units"][0]["id"]
    assert client.post(f"/api/units/{uid}/weigh", json={"gross_g": 700, "tare_g": 200}).json["ok"]
    u = client.get(f"/api/units/{uid}").json
    assert u["remaining_g"] == 500 and u["status"] == "open" and u["remaining_src"] == "weighed"
    assert client.get(f"/api/filaments/{f['id']}").json["tare_g"] == 200


def test_family_detection(client):
    fam = lambda m: client.get(f"/api/profiles/family?material={m}").json["family"]  # noqa: E731
    assert fam("PETG-CF") == "PETG"
    assert fam("PA6-GF") == "PA"
    assert fam("Support for PLA/PETG") == "SUPPORT"
    assert fam("PLA Basic") == "PLA"
    assert fam("TPU 90A") == "TPU"


def test_shopping_and_ordered(client):
    f = client.post("/api/filaments", json={"brand": "Test", "material": "ASA", "color_name": "Rot", "qty": 1, "min_qty": 1,
                                            "colors": ["#FF0000FF"], "buy_url": "https://shop.example/asa"}).json
    items = client.get("/api/shopping").json
    it = [i for i in items if i["type"] == "filament" and i["id"] == f["id"]][0]
    assert it["shop"] == "shop.example"
    client.post("/api/shopping/ordered", json={"type": "filament", "id": f["id"], "ordered": True})
    assert client.get(f"/api/filaments/{f['id']}").json["ordered_at"]


def test_costs_in_stats(client):
    f = client.post("/api/filaments", json={"brand": "Test", "material": "PLA", "color_name": "Blau", "qty": 1, "price": 20,
                                            "net_weight_g": 1000, "colors": ["#0000FFFF"]}).json
    st = client.get("/api/stats").json
    assert st["kpi"]["stock_value"] >= 20


def test_cloud_task_booking(client, appmod):
    """Slicer-Gramm aus einem Bambu-Druckauftrag korrigieren die AMS-Schätzung und markieren Slots ohne Tag."""
    import tasks
    from bambu import conn
    f = client.post("/api/filaments", json={"brand": "Sunlu", "material": "PLA+", "color_name": "Pink", "qty": 1, "colors": ["#FF66AAFF"]}).json
    uid = f["units"][0]["id"]
    with conn() as c:
        c.execute("INSERT INTO printers (name, serial, mode) VALUES ('H2S', 'TESTSERIAL1', 'lan')")
        pid = c.execute("SELECT id FROM printers WHERE serial='TESTSERIAL1'").fetchone()[0]
        c.execute("INSERT INTO tags (tag_key, tray_uuid, filament_ref, unit_id, full_weight_g, remain_pct) VALUES ('K1','K1',?,?,1000,90)", (f["id"], uid))
        slots = {"0:0": {"tag_key": "K1", "unit_id": uid, "filament_ref": f["id"]}, "0:1": {"tag_key": None, "unit_id": None, "filament_ref": None}}
        usage = [{"tag_key": "K1", "grams": 30.0, "ams_g": 30.0, "unit_id": uid, "filament_ref": f["id"], "slot": [0, 0], "source": "ams"}]
        c.execute("""INSERT INTO print_jobs (printer_id, job_key, name, started, ended, result, usage, slots, end_percent)
                     VALUES (?,?,?,?,?,?,?,?,100)""", (pid, "t", "Testdruck", "2030-01-01T10:00:00", "2030-01-01T11:00:00", "fertig",
                                                         json.dumps(usage), json.dumps(slots)))
        c.execute("UPDATE spool_units SET remaining_g=970 WHERE id=?", (uid,))
        from datetime import datetime, timezone
        utc = lambda s: datetime.fromisoformat(s).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
        c.execute("INSERT INTO settings(key,value) VALUES('tasks_since','2029-01-01T00:00:00') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        c.execute("""INSERT INTO cloud_tasks (id, device_id, title, status, start, end, mapping) VALUES (1, 'TESTSERIAL1', 'Testdruck', 2, ?, ?, ?)""",
                  (utc("2030-01-01T10:01:00"), utc("2030-01-01T11:00:00"),
                   json.dumps([{"ams": 0, "weight": 34.5}, {"ams": 1, "weight": 12}])))
        t = c.execute("SELECT * FROM cloud_tasks WHERE id=1").fetchone()
        assert tasks.book(c, t)
    u = client.get(f"/api/units/{uid}").json
    assert round(u["remaining_g"], 1) == 965.5          # 970 − (34,5 − 30)
    job = [j for j in client.get("/api/ams/jobs").json if j["name"] == "Testdruck"][0]
    assert job["usage_src"] == "slicer" and job["unassigned"] == 1
    idx = [i for i, e in enumerate(job["usage"]) if e.get("unassigned")][0]
    assert client.post(f"/api/jobs/{job['id']}/assign", json={"index": idx, "filament_id": f["id"]}).json["ok"]
    assert round(client.get(f"/api/units/{uid}").json["remaining_g"], 1) == 953.5


def test_backup(client):
    r = client.post("/api/backups").json
    assert r["ok"] and r["name"].endswith(".tar.gz")
    assert any(f["name"] == r["name"] for f in client.get("/api/backups").json["files"])


def test_export_import_roundtrip(client):
    data = json.loads(client.get("/api/export").get_data(as_text=True))
    assert "units" in data["filaments"][0]
    before = len(client.get("/api/filaments").json)
    assert client.post("/api/import?replace=1", json=data).json["ok"]
    after = client.get("/api/filaments").json
    assert len(after) == before
    assert sum(len(f["units"]) for f in after) == sum(len(f["units"]) for f in data["filaments"])


def test_short_links(client):
    assert client.get("/s/12").location.endswith("/?unit=12")
    assert client.get("/f/3").location.endswith("/?fil=3")


def test_nfc_scan_create_link_and_merge(client):
    """Handy liest Tag ohne AMS → wird angelegt, zugeordnet und später mit dem AMS-Eintrag zusammengeführt."""
    import ams
    from bambu import conn
    r = client.post("/api/nfc/scan", json={"uid": "75886b1d", "mifare": False}).json
    assert r["created"] and r["key"] == "NFC:75886B1D"
    f = client.post("/api/filaments", json={"brand": "Sunlu", "material": "PLA", "color_name": "Pink", "qty": 1, "colors": ["#FF66AAFF"]}).json
    assert client.post("/api/ams/rfid/NFC:75886B1D/link", json={"filament_id": f["id"]}).json["ok"]
    # zweiter Scan findet denselben Tag
    r2 = client.post("/api/nfc/scan", json={"uid": "75886B1D"}).json
    assert not r2["created"] and r2["key"] == "NFC:75886B1D"
    # AMS meldet den Tag (tag_uid = UID + Nullen) → Eintrag übernimmt tray_uuid als Schlüssel, Zuordnung bleibt
    uuid = "A0A1A2A3A4A5A6A7A8A9AAABACADAEAF"
    with conn() as c:
        ams.update_tag(c, uuid, {"tray_uuid": uuid, "tag_uid": "75886B1D00000000", "tray_type": "PLA", "tray_weight": "1000"},
                       None, 0, 0, 80, "2030-01-01T00:00:00")
        rows = c.execute("SELECT tag_key, filament_ref FROM tags WHERE tag_key IN (?, 'NFC:75886B1D')", (uuid,)).fetchall()
    assert [(x["tag_key"], x["filament_ref"]) for x in rows] == [(uuid, f["id"])]
    # Handy mit vollem Auslesen (tray_uuid) findet ihn ebenfalls
    r3 = client.post("/api/nfc/scan", json={"uid": "75886B1D", "tray_uuid": uuid, "detail": "PLA Basic", "weight": 1000}).json
    assert r3["key"] == uuid and not r3["created"]
    t = [x for x in client.get("/api/ams/rfid").json if x["key"] == uuid][0]
    assert t["filament_ref"] == f["id"] and t["unit_id"]


def test_nfc_scan_full_read_creates_uuid_key(client):
    r = client.post("/api/nfc/scan", json={"uid": "11223344", "tray_uuid": "00112233445566778899AABBCCDDEEFF",
                                           "detail": "PETG Basic", "color": "00AE42FF", "weight": 1000}).json
    assert r["created"] and r["key"] == "00112233445566778899AABBCCDDEEFF"
    t = [x for x in client.get("/api/ams/rfid").json if x["key"] == r["key"]][0]
    assert t["claim_color"] == "00AE42FF" and t["full_weight_g"] == 1000
    assert client.post("/api/nfc/scan", json={"uid": "zz"}).status_code == 400


def test_bulk_edit_filaments(client):
    mk = lambda n: client.post("/api/filaments", json={"brand": "Bulk", "material": "PETG", "color_name": n, "qty": 1, "colors": ["#123456FF"],
                                                       "profile": {"nozzle_min": 230, "notes": "alt"}}).json  # noqa: E731
    a, b = mk("A"), mk("B")
    r = client.post("/api/filaments/bulk", json={"ids": [a["id"], b["id"]], "set": {"price": "21.99", "location": "Regal 3", "net_weight_g": 1000},
                                                  "profile": {"nozzle_max": 250, "notes": None}, "notes_append": "Charge 2026",
                                                  "unit_price": True, "sealed_weight": True}).json
    assert r["ok"] and r["count"] == 2
    for f in (a, b):
        g = client.get(f"/api/filaments/{f['id']}").json
        assert g["price"] == 21.99 and g["location"] == "Regal 3" and g["color_name"] in ("A", "B")
        assert g["profile"] == {"nozzle_min": 230, "nozzle_max": 250.0}
        assert g["notes"].endswith("Charge 2026")
        assert all(u["price"] == 21.99 for u in g["units"])
    # nicht erlaubte Felder werden ignoriert, null leert
    client.post("/api/filaments/bulk", json={"ids": [a["id"]], "set": {"color_name": "X", "qty": 9, "location": None}})
    g = client.get(f"/api/filaments/{a['id']}").json
    assert g["color_name"] == "A" and g["qty"] == 1 and g["location"] is None
    assert client.post("/api/filaments/bulk", json={"ids": []}).status_code == 400


def test_studio_sync_to_ams(client, monkeypatch):
    """Tag eines Bambu-Filaments auf Sunlu-Spule: Drucker bekommt SUNLU-Profil + Inventarfarbe; Ablehnung wird erkannt."""
    import json as _j
    import threading
    import studio
    from bambu import conn
    monkeypatch.setattr(studio, "static_presets", lambda: [
        {"filament_id": "GFSNL03", "name": "SUNLU PLA+", "vendor": "SUNLU", "type": "PLA", "nozzle_min": 190, "nozzle_max": 240},
        {"filament_id": "GFL99", "name": "Generic PLA", "vendor": "Generic", "type": "PLA", "nozzle_min": 190, "nozzle_max": 240},
        {"filament_id": "GFA00", "name": "Bambu PLA Basic", "vendor": "Bambu Lab", "type": "PLA", "nozzle_min": 190, "nozzle_max": 240}])
    monkeypatch.setattr(studio.threading, "Timer", lambda *a, **k: type("T", (), {"start": lambda self: None})())
    f = client.post("/api/filaments", json={"brand": "Sunlu", "material": "PLA +", "color_name": "Pink", "qty": 1, "colors": ["#FF66AAFF"]}).json
    with conn() as c:
        c.execute("INSERT INTO tags (tag_key, tray_uuid, tag_uid, filament_ref) VALUES ('ST1','ST1','11223344AABBCCDD',?)", (f["id"],))
    sent = []

    class Pc:
        id, serial, cfg, connected = 77, "0938XX", {"model": None}, True
        _lock = threading.Lock()
        state = {(0, 2): {"tray_uuid": "ST1", "tag_uid": "11223344AABBCCDD", "tray_info_idx": "GFA00", "tray_color": "000000FF", "state": 11}}
        client = type("C", (), {"publish": lambda self, t, p: sent.append(_j.loads(p))})()
        def pushall(self): pass
    pc = Pc()
    studio.check(pc)
    assert len(sent) == 1
    cmd = sent[0]["print"]
    assert cmd["command"] == "ams_filament_setting" and cmd["tray_info_idx"] == "GFSNL03" and cmd["tray_color"] == "FF66AAFF"
    assert cmd["ams_id"] == 0 and cmd["slot_id"] == 2 and cmd["tray_type"] == "PLA"
    studio.check(pc)            # Drucker hat noch nicht geantwortet → nicht erneut senden
    assert len(sent) == 1
    pc.state[(0, 2)].update(tray_info_idx="GFSNL03", tray_color="FF66AAFF")   # Drucker übernimmt
    studio.check(pc)
    with conn() as c:
        assert studio.slot_info(c, 77, 0, 2)["status"] == "ok"
        assert studio.is_ours(c, 77, 0, 2, pc.state[(0, 2)])
    # Profil wechseln → neue Werte; Drucker ignoriert → nach Frist „rejected“, keine Endlosschleife
    client.put(f"/api/filaments/{f['id']}", json={"studio": {"filament_id": "GFL99", "name": "Generic PLA"}})
    studio.check(pc)
    assert sent[-1]["print"]["tray_info_idx"] == "GFL99" and len(sent) == 2
    with conn() as c:
        c.execute("UPDATE studio_slots SET sent_at = sent_at - 60 WHERE printer_id=77")
    studio.check(pc)
    with conn() as c:
        assert studio.slot_info(c, 77, 0, 2)["status"] == "rejected"
    studio.check(pc)
    assert len(sent) == 2


def test_stats_full(client):
    for q in ("range=7d", "range=30d", "range=90d", "range=365d", "range=year", "range=all", "from=2026-01-01&to=2026-03-31"):
        d = client.get(f"/api/stats/full?{q}").json
        assert d["bucket"] in ("day", "week", "month") and d["series"]
        assert {"kpi", "prints", "printers", "heatmap", "slots", "stock", "idle", "by_material", "by_brand"} <= set(d)
        assert len(d["heatmap"]["hours"]) == 7 and all(len(r) == 24 for r in d["heatmap"]["hours"])
    d = client.get("/api/stats/full?range=30d&idle_days=0").json
    assert d["idle_total"] >= 1 and d["stock"]


def test_js_globals_unique():
    """Die Skripte teilen sich einen globalen Namensraum – doppelte Namen brechen ein ganzes Skript."""
    import re
    from pathlib import Path
    seen = {}
    for name in ("app.js", "extras.js", "stats.js", "library.js"):
        src = (Path(__file__).resolve().parents[1] / "app" / "static" / name).read_text(encoding="utf-8")
        for m in re.finditer(r"^(?:const|let|function|async function|class) ([A-Za-z_$][\w$]*)", src, re.M):
            assert m.group(1) not in seen, f"{m.group(1)} doppelt in {seen.get(m.group(1))} und {name}"
            seen[m.group(1)] = name


def _stl_bytes():
    import struct
    tris = [((0, 0, 0), (10, 0, 0), (0, 10, 0)), ((0, 0, 0), (0, 10, 0), (0, 0, 10)), ((0, 0, 0), (0, 0, 10), (10, 0, 0)), ((10, 0, 0), (0, 0, 10), (0, 10, 0))]
    out = b"x" * 80 + struct.pack("<I", len(tris))
    for t in tris:
        out += struct.pack("<3f", 0, 0, 0) + b"".join(struct.pack("<3f", *v) for v in t) + b"\0\0"
    return out


def test_library_upload_tags_collections(client):
    import io
    import time
    import library
    r = client.post("/api/library/models", data={"files": [(io.BytesIO(_stl_bytes()), "Tetraeder.stl")], "tags": "Test, #Deko"},
                    content_type="multipart/form-data")
    assert r.status_code == 201
    mid = r.json["ids"][0]
    fid = client.get(f"/api/library/models/{mid}").json["file_list"][0]["id"]
    library.analyze_now(fid)
    m = client.get(f"/api/library/models/{mid}").json
    f = m["file_list"][0]
    assert m["title"] == "Tetraeder" and m["tags"] == ["deko", "test"] and f["kind"] == "stl"
    assert f["meta"]["triangles"] == 4 and f["meta"]["size_mm"] == [10.0, 10.0, 10.0] and f["thumb"]
    assert client.get(f["thumb"]).status_code == 200
    d = client.get(f["url"])
    assert d.status_code == 200 and d.data[:5] == b"xxxxx" and "Tetraeder.stl" in d.headers["Content-Disposition"]
    # falsches Format, Pfad im Dateinamen
    bad = client.post("/api/library/models", data={"files": [(io.BytesIO(b"x"), "evil.exe")]}, content_type="multipart/form-data")
    assert bad.status_code == 400
    r2 = client.post(f"/api/library/models/{mid}/files", data={"files": [(io.BytesIO(_stl_bytes() + b""), "../../x.stl")]}, content_type="multipart/form-data")
    assert r2.status_code == 201  # gleiche Datei → kein Duplikat
    assert len(client.get(f"/api/library/models/{mid}").json["file_list"]) == 1
    # Tags, Sammlung, Suche
    k = client.post("/api/library/collections", json={"name": "Werkstatt"}).json["id"]
    client.put(f"/api/library/models/{mid}", json={"tags": ["halter", "Deko"], "collections": [k], "notes": "PETG"})
    assert client.get("/api/library/models?tag=halter").json[0]["id"] == mid
    assert client.get(f"/api/library/models?collection={k}").json[0]["collections"][0]["name"] == "Werkstatt"
    assert client.get("/api/library/models?q=tetra").json and not client.get("/api/library/models?q=gibtsnicht").json
    client.post("/api/library/tags/rename", json={"old": "halter", "new": "wandhalter"})
    assert "wandhalter" in client.get(f"/api/library/models/{mid}").json["tags"]
    assert client.delete(f"/api/library/models/{mid}").status_code == 204
    assert not library.file_path(fid, "stl").exists()


def test_library_cleanup(client):
    import io
    import library
    up = lambda name, data: client.post("/api/library/models", data={"files": [(io.BytesIO(data), name)]},  # noqa: E731
                                        content_type="multipart/form-data").json["ids"][0]
    a, b, gone = up("A.stl", _stl_bytes()), up("B.stl", _stl_bytes()), up("C.stl", _stl_bytes() + b"\0")
    fa, fb, fc = (client.get(f"/api/library/models/{m}").json["file_list"][0]["id"] for m in (a, b, gone))
    for f in (fa, fb, fc):
        library.analyze_now(f)
    (library.FILE_DIR / "999999.stl").write_bytes(b"alt")          # Datei ohne Eintrag
    (library.THUMB_DIR / "m999999.png").write_bytes(b"png")        # Cover ohne Modell
    library.file_path(fc, "stl").unlink()                          # Eintrag ohne Datei
    library.thumb_path(fa).unlink()                                # Vorschau fehlt
    r = client.get("/api/library/cleanup").json
    assert r["counts"]["orphan_files"] == 1 and r["counts"]["orphan_thumbs"] == 1
    assert [x["id"] for x in r["missing_files"]] == [fc] and [x["id"] for x in r["missing_thumbs"]] == [fa]
    assert r["duplicates"][0]["keep"]["id"] == fa and r["duplicates"][0]["remove"][0]["id"] == fb
    assert gone in [m["id"] for m in r["empty_models"]]
    assert (library.FILE_DIR / "999999.stl").exists()              # Bericht löscht nichts
    r = client.post("/api/library/cleanup", json={}).json
    assert not (library.FILE_DIR / "999999.stl").exists() and r["freed_bytes"] >= 6
    assert library.file_path(fb, "stl").exists()                   # Duplikate nur auf Wunsch
    client.post("/api/library/cleanup", json={"duplicates": True})
    assert not library.file_path(fb, "stl").exists() and library.file_path(fa, "stl").exists()
    library.analyze_now(fa)
    r = client.get("/api/library/cleanup").json
    assert not any(r["counts"][k] for k in ("orphan_files", "orphan_thumbs", "missing_files", "missing_thumbs", "duplicates"))


def test_makerworld_sync_mocked(client, monkeypatch):
    """Sammlungen → Modelle mit Tags → 3MF-Download (API-Antworten simuliert)."""
    import io
    import zipfile
    import makerworld
    from bambu import conn, set_setting
    with conn() as c:
        set_setting(c, "bambu_token", "x.eyJ1c2VybmFtZSI6InVfMTIzNDU2Nzg5In0.y")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("3D/3dmodel.model", '<model><metadata name="Title">Halter</metadata></model>')
    calls = []

    def fake_get(cl, path, auth=True):
        calls.append(path)
        if path.startswith("/v1/design-service/favoriteslist/123456789"):
            return {"total": 1, "hits": [{"id": 900, "title": "Werkstatt", "status": 2}]}
        if path == "/v1/design-service/favorites/900":
            return {"id": 900, "title": "Werkstatt", "description": "Halter", "designs": [{"id": 4242, "title": "Wandhalter", "modelId": "USabc",
                                                                                         "designCreator": {"name": "maker"}}]}
        if path == "/v1/design-service/design/4242":
            return {"id": 4242, "title": "Wandhalter", "summary": "<p>Ein <b>Halter</b></p>", "tags": ["Halter", "Wand"], "license": "BY",
                    "modelId": "USabc", "defaultInstanceId": 1, "instances": [{"id": 1, "profileId": 777, "title": "0.2mm", "isDefault": True}]}
        if path.startswith("/v1/iot-service/api/user/profile/777?model_id=USabc"):
            return {"url": "https://makerworld.bblmw.com/x/Wandhalter.3mf?exp=1&key=k", "name": "Wandhalter.3mf"}
        raise makerworld.MWError("unerwartet " + path)

    monkeypatch.setattr(makerworld, "api_get", fake_get)
    monkeypatch.setattr(makerworld, "fetch_bytes", lambda url, limit=0: buf.getvalue())
    monkeypatch.setattr(makerworld, "save_cover", lambda mid, url: None)
    monkeypatch.setattr(makerworld, "DELAY_API", 0)
    monkeypatch.setattr(makerworld, "DELAY_DL", 0)
    r = makerworld.run_sync()
    assert r["ok"], r
    assert r["collections"] == 1 and r["models"] == 1 and r["downloaded"] == 1
    m = [x for x in client.get("/api/library/models?source=makerworld").json if x["title"] == "Wandhalter"][0]
    assert {"halter", "wand"} <= set(m["tags"]) and m["files"] == 1 and m["mw_download"] == "done"
    assert m["collections"][0]["name"] == "Werkstatt"
    full = client.get(f"/api/library/models/{m['id']}").json
    assert full["description"] == "Ein Halter" and full["file_list"][0]["name"] == "Wandhalter.3mf"
    # zweiter Lauf: nichts doppelt, kein erneuter Download
    r = makerworld.run_sync()
    assert r["ok"] and r["downloaded"] == 0
    assert len([x for x in client.get("/api/library/models?source=makerworld").json if x["title"] == "Wandhalter"]) == 1
    assert makerworld.parse_ref("https://makerworld.com/de/collections/5565530-desk") == ("collection", 5565530)
    assert makerworld.parse_ref("https://makerworld.com/en/models/1769930-x#profileId-1") == ("design", 1769930)


def test_active_tray_h2_snow():
    """H2-Serie: aktiver Slot aus extruder.info[].snow, nicht aus dem hängenden ams.tray_now."""
    from printer_status import active_tray, derive
    ext = lambda state, *snows: {"device": {"extruder": {"state": state, "info": [{"id": i, "snow": s} for i, s in enumerate(snows)]}}}  # noqa: E731
    assert active_tray({}, "7") == {"ams_id": 1, "tray_id": 3}                 # klassisch: AMS B Slot 4
    assert active_tray({}, "128") == {"ams_id": 128, "tray_id": 0}             # AMS HT
    assert active_tray({}, 255) is None and active_tray({}, 254)["ams_id"] == 255
    assert active_tray(ext(0, (1 << 8) | 3), "3") == {"ams_id": 1, "tray_id": 3}   # B3 → B4 umgeschaltet, tray_now hängt auf A4
    assert active_tray(ext(0x10, 2, 128 << 8), "3") == {"ams_id": 128, "tray_id": 0}  # H2D: rechte Düse aktiv
    assert active_tray(ext(0, 0xFFFF), "3") is None                            # nichts geladen
    assert derive({**ext(0, (1 << 8) | 2), "gcode_state": "RUNNING"}, "H2S", "3")["tray_now"] == {"ams_id": 1, "tray_id": 2}


def test_pack_per_unit(client):
    """OVP/Spule/Refill gelten je Box; die Sorte fasst nur zusammen."""
    f = client.post("/api/filaments", json={"brand": "Test", "material": "PLA", "color_name": "Box", "qty": 2,
                                            "colors": ["#123456FF"], "sealed": True, "spool": False, "refill": True}).json
    u1, u2 = (u["id"] for u in f["units"])
    assert all(u["status"] == "sealed" and u["refill"] == 1 and u["spool"] == 0 for u in f["units"])
    # eine Box ist doch eine Spule, die andere wird geöffnet
    client.patch(f"/api/units/{u1}", json={"spool": True, "refill": False})
    client.post(f"/api/units/{u2}/open")
    g = client.get(f"/api/filaments/{f['id']}").json
    by = {u["id"]: u for u in g["units"]}
    assert by[u1]["spool"] == 1 and by[u1]["refill"] == 0 and by[u1]["status"] == "sealed"
    assert by[u2]["refill"] == 1 and by[u2]["status"] == "open"
    assert g["spool"] and g["refill"] and not g["sealed"]            # Sorte = Zusammenfassung
    # OVP an der Sorte setzen ändert keine Box
    client.put(f"/api/filaments/{f['id']}", json={"sealed": True})
    assert client.get(f"/api/units/{u2}").json["status"] == "open"
    # neue Box übernimmt die Verpackung der zuletzt angelegten
    client.post(f"/api/filaments/{f['id']}/adjust", json={"delta": 1})
    new = max(client.get(f"/api/filaments/{f['id']}").json["units"], key=lambda u: u["id"])
    assert new["refill"] == 1 and new["spool"] == 0
    # versehentlich geöffnet → wieder OVP
    client.patch(f"/api/units/{u2}", json={"status": "sealed"})
    assert client.get(f"/api/units/{u2}").json["opened_at"] is None
    # Massenbearbeitung: Refill für alle Boxen
    client.post("/api/filaments/bulk", json={"ids": [f["id"]], "set": {"refill": False}})
    assert not any(u["refill"] for u in client.get(f"/api/filaments/{f['id']}").json["units"] if u["status"] != "empty")
