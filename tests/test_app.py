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
