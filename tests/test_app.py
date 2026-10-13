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
