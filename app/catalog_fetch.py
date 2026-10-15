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


# ---------------------------------------------------------------- Bambu-Studio-Filamentprofile
RAW = "https://raw.githubusercontent.com/bambulab/BambuStudio/master/resources/profiles/BBL"


def _get(url: str, timeout: int = 30):
    from urllib.parse import quote
    req = urllib.request.Request(quote(url, safe=":/"), headers={"User-Agent": "3d-manager"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 – feste HTTPS-URL
        return json.loads(r.read().decode("utf-8"))


def fetch_studio(dest: Path) -> int:
    """System-Filamentprofile (Bambu, Generic, SUNLU, eSUN, PolyLite …) mit filament_id, Typ und Düsentemperatur.

    Pro Profil-Familie wird die „@base“-Datei gelesen und die Vererbung (fdm_filament_*) aufgelöst.
    """
    index = _get(f"{RAW}.json")
    paths = {e["name"]: e["sub_path"] for e in index.get("filament_list", [])}
    cache: dict[str, dict] = {}

    def load(name):
        if name not in cache:
            sub = paths.get(name) or f"filament/{name}.json"
            cache[name] = _get(f"{RAW}/{sub}")
        return cache[name]

    def resolved(name, depth=0):
        d = load(name)
        if d.get("inherits") and depth < 6:
            base = resolved(d["inherits"], depth + 1)
            return {**base, **d}
        return d

    first = lambda v: (v[0] if isinstance(v, list) and v else v)  # noqa: E731
    out = []
    for name in sorted(n for n in paths if n.endswith(" @base")):
        try:
            d = resolved(name)
        except Exception:  # noqa: BLE001 – einzelne Datei fehlt
            continue
        if not d.get("filament_id"):
            continue
        out.append({
            "filament_id": d["filament_id"], "name": name[:-6], "vendor": first(d.get("filament_vendor")) or "",
            "type": first(d.get("filament_type")) or "", "support": first(d.get("filament_is_support")) == "1",
            "nozzle_min": int(float(first(d.get("nozzle_temperature_range_low")) or 0)) or None,
            "nozzle_max": int(float(first(d.get("nozzle_temperature_range_high")) or 0)) or None,
        })
    if len(out) < 30:
        raise RuntimeError(f"Profilliste unvollständig ({len(out)})")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
    tmp.replace(dest)
    return len(out)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / "catalog" / "bambu.json")
    try:
        print(f"Bambu-Katalog: {fetch(target)} Farben → {target}")
    except Exception as e:  # noqa: BLE001
        print(f"Bambu-Katalog konnte nicht geladen werden ({e}) – wird beim Start erneut versucht.")
    try:
        sp = target.parent / "studio_presets.json"
        print(f"Bambu-Studio-Profile: {fetch_studio(sp)} → {sp}")
    except Exception as e:  # noqa: BLE001
        print(f"Bambu-Studio-Profile konnten nicht geladen werden ({e}) – wird beim Start erneut versucht.")
