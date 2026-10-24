"""Vorschaubilder und Kennzahlen für STL- und 3MF-Dateien (ohne Slicer, nur numpy + Pillow)."""
from __future__ import annotations

import io
import re
import struct
import zipfile
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw

MAX_TRIS = 400_000


# ---------------------------------------------------------------- Meshes lesen
def stl_triangles(data: bytes) -> np.ndarray:
    """(n, 3, 3) float32 – binär oder ASCII."""
    if len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if 84 + n * 50 == len(data):
            rec = np.frombuffer(data, dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]), count=n, offset=84)
            return rec["v"].astype(np.float32)
    txt = data.decode("latin-1", "ignore")
    nums = re.findall(r"vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)", txt)
    if not nums:
        raise ValueError("keine STL-Dreiecke gefunden")
    v = np.array(nums, dtype=np.float32)
    return v[: len(v) // 3 * 3].reshape(-1, 3, 3)


def _model_tris(xml: bytes) -> list[np.ndarray]:
    """Alle <mesh> einer 3MF-Modelldatei (Namespace-unabhängig)."""
    out = []
    root = ET.fromstring(xml)
    for mesh in root.iter():
        if not mesh.tag.endswith("mesh"):
            continue
        verts, tris = [], []
        for el in mesh.iter():
            t = el.tag.rsplit("}", 1)[-1]
            if t == "vertex":
                verts.append((float(el.get("x", 0)), float(el.get("y", 0)), float(el.get("z", 0))))
            elif t == "triangle":
                tris.append((int(el.get("v1", 0)), int(el.get("v2", 0)), int(el.get("v3", 0))))
        if verts and tris:
            v = np.asarray(verts, dtype=np.float32)
            idx = np.asarray(tris, dtype=np.int64)
            idx = idx[(idx < len(v)).all(axis=1)]
            out.append(v[idx])
    return out


def threemf_triangles(z: zipfile.ZipFile) -> np.ndarray:
    parts = []
    for name in z.namelist():
        if name.lower().endswith(".model") and name.lower().startswith("3d/"):
            if z.getinfo(name).file_size > 400 * 1024 * 1024:
                continue
            parts += _model_tris(z.read(name))
    if not parts:
        raise ValueError("keine Geometrie im 3MF")
    return np.concatenate(parts)


# ---------------------------------------------------------------- Rendern
def render(tris: np.ndarray, size: int = 512, color=(214, 108, 52)) -> bytes:
    """Isometrische Ansicht mit flacher Schattierung, transparenter Hintergrund → PNG."""
    if len(tris) > MAX_TRIS:  # sehr große Netze: jedes k-te Dreieck (Vorschau, keine Messung)
        tris = tris[:: int(np.ceil(len(tris) / MAX_TRIS))]
    v = tris.reshape(-1, 3).astype(np.float64)
    v -= (v.min(0) + v.max(0)) / 2
    a, b = np.radians(-45), np.radians(-60)
    rz = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    rx = np.array([[1, 0, 0], [0, np.cos(b), -np.sin(b)], [0, np.sin(b), np.cos(b)]])
    p = (v @ rz.T @ rx.T).reshape(-1, 3, 3)
    n = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
    ln = np.linalg.norm(n, axis=1)
    keep = ln > 1e-12
    p, n, ln = p[keep], n[keep], ln[keep]
    n /= ln[:, None]
    front = n[:, 2] > 0                       # Rückseiten weglassen
    p, n = p[front], n[front]
    if not len(p):
        raise ValueError("leeres Netz")
    light = np.array([-0.35, 0.45, 0.82]); light /= np.linalg.norm(light)
    shade = 0.35 + 0.65 * np.clip(n @ light, 0, 1)
    xy = p[:, :, :2]
    lo, hi = xy.reshape(-1, 2).min(0), xy.reshape(-1, 2).max(0)
    scale = (size * 0.86) / max(hi - lo)
    off = (size - (hi - lo) * scale) / 2
    pts = (xy - lo) * scale + off
    pts[:, :, 1] = size - pts[:, :, 1]
    order = np.argsort(p[:, :, 2].mean(1))    # hinten → vorne
    ss = 2
    img = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cols = (np.array(color)[None, :] * shade[:, None]).astype(int)
    big = pts * ss
    for i in order:
        c = tuple(cols[i]) + (255,)
        d.polygon([tuple(big[i, 0]), tuple(big[i, 1]), tuple(big[i, 2])], fill=c, outline=c)
    img = img.resize((size, size), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def mesh_stats(tris: np.ndarray) -> dict:
    v = tris.reshape(-1, 3)
    size = v.max(0) - v.min(0)
    # Volumen über vorzeichenbehaftete Tetraeder (nur bei geschlossenem Netz sinnvoll)
    vol = float(np.abs(np.einsum("ij,ij->i", tris[:, 0], np.cross(tris[:, 1], tris[:, 2])).sum()) / 6.0)
    return {"triangles": int(len(tris)), "size_mm": [round(float(x), 1) for x in size], "volume_cm3": round(vol / 1000, 1)}


# ---------------------------------------------------------------- 3MF-Metadaten
THUMB_NAMES = ("Metadata/plate_1.png", "Auxiliaries/.thumbnails/thumbnail_3mf.png", "Metadata/thumbnail.png",
               "Auxiliaries/.thumbnails/thumbnail_middle.png", "Metadata/top_1.png")


def _kv(el):
    return {m.get("key"): m.get("value") for m in el if m.tag == "metadata" and m.get("key")}


def threemf_info(data: bytes) -> dict:
    """Titel, Designer, Platten, Druckzeit/Gewicht (falls gesliced), Filamente, eingebettete Vorschau."""
    info: dict = {"plates": [], "filaments": []}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        # Modell-Metadaten
        main = next((n for n in ("3D/3dmodel.model", "3D/3DModel.model") if n in names), None)
        if main:
            head = z.read(main)[:200_000].decode("utf-8", "ignore")
            for key in ("Title", "Designer", "Description", "License", "DesignerUserId", "Origin", "Application"):
                m = re.search(rf'<metadata name="{key}"[^>]*>(.*?)</metadata>', head, re.S)
                if m and m.group(1).strip():
                    info[key.lower()] = re.sub(r"<[^>]+>", "", m.group(1)).strip()[:4000]
        # gesliced: Druckzeit, Gewicht, Filamente je Platte
        if "Metadata/slice_info.config" in names:
            root = ET.fromstring(z.read("Metadata/slice_info.config"))
            for pl in root.iter("plate"):
                kv = _kv(pl)
                fil = [{"type": f.get("type"), "color": f.get("color"), "g": float(f.get("used_g") or 0)} for f in pl.iter("filament")]
                info["plates"].append({"index": int(kv.get("index") or len(info["plates"]) + 1),
                                       "seconds": int(float(kv.get("prediction") or 0)) or None,
                                       "grams": round(float(kv.get("weight") or 0), 1) or None, "filaments": fil})
            info["sliced"] = any(p["seconds"] for p in info["plates"])
        # Plattennamen (Projektdatei)
        if "Metadata/model_settings.config" in names and not info["plates"]:
            root = ET.fromstring(z.read("Metadata/model_settings.config"))
            for i, pl in enumerate(root.iter("plate"), 1):
                kv = _kv(pl)
                info["plates"].append({"index": int(kv.get("plater_id") or i), "name": kv.get("plater_name") or None})
        # Filamente des Projekts
        if "Metadata/project_settings.config" in names:
            try:
                import json
                ps = json.loads(z.read("Metadata/project_settings.config"))
                types, colors = ps.get("filament_type") or [], ps.get("filament_colour") or []
                info["filaments"] = [{"type": t, "color": colors[i] if i < len(colors) else None} for i, t in enumerate(types)]
                info["printer"] = ps.get("printer_model") or None
            except ValueError:
                pass
        for t in THUMB_NAMES:
            if t in names:
                info["_thumb"] = z.read(t)
                break
        info["has_gcode"] = any(n.endswith(".gcode") for n in names)
    return info


def analyze(data: bytes, kind: str) -> tuple[dict, bytes | None]:
    """→ (Metadaten, PNG-Vorschau oder None)."""
    meta: dict = {}
    thumb = None
    if kind == "3mf":
        meta = threemf_info(data)
        thumb = meta.pop("_thumb", None)
        if thumb is None or "triangles" not in meta:
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    tris = threemf_triangles(z)
                meta.update(mesh_stats(tris))
                if thumb is None:
                    thumb = render(tris)
            except Exception:  # noqa: BLE001 – Vorschau ist optional
                pass
    elif kind == "stl":
        tris = stl_triangles(data)
        meta.update(mesh_stats(tris))
        thumb = render(tris)
    return meta, thumb
