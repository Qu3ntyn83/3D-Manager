"""Bambu-Farbkatalog aus Bambu Studio laden und ins App-Format umwandeln.

Die Rohdaten (filaments_color_codes.json) gehören zu Bambu Studio (AGPL-3.0) und werden
deshalb nicht im Repository mitgeliefert, sondern beim Docker-Build bzw. beim ersten Start geladen.

    python catalog_fetch.py [Zieldatei]
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

URL = "https://raw.githubusercontent.com/bambulab/BambuStudio/master/resources/profiles/BBL/filament/filaments_color_codes.json"
PATTERN = {"单色": "solid", "渐变色": "gradient", "多拼色": "multi"}


def convert(raw: dict) -> list[dict]:
    out = []
    for e in raw.get("data", []):
        n = e.get("fila_color_name") or {}
        out.append({
            "brand": "Bambu Lab", "sku": e.get("fila_color_code", ""), "fila_id": e.get("fila_id", ""),
            "tray_code": e.get("color_code", ""), "type": (e.get("fila_type") or "").strip(),
            "name_de": (n.get("de") or n.get("en") or "").strip(), "name_en": (n.get("en") or "").strip(),
            "colors": [c[:9].upper() for c in e.get("fila_color") or []] or ["#888888FF"],
            "pattern": PATTERN.get(e.get("fila_color_type"), "solid"), "source": "bambu",
        })
    return out


def fetch(dest: Path, timeout: int = 30) -> int:
    req = urllib.request.Request(URL, headers={"User-Agent": "3d-manager"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 – feste HTTPS-URL
        data = convert(json.loads(r.read().decode("utf-8")))
    if len(data) < 50:
        raise RuntimeError(f"Katalog unvollständig ({len(data)} Einträge)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(dest)
    return len(data)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "catalog" / "bambu.json")
    try:
        print(f"Bambu-Katalog: {fetch(target)} Farben → {target}")
    except Exception as e:  # noqa: BLE001
        print(f"Bambu-Katalog konnte nicht geladen werden ({e}) – wird beim Start erneut versucht.")
        sys.exit(0)
