#!/usr/bin/env python3
"""
Phase 10: Mod Packaging and Integration (pipeline/99_package_mod.py)

Tasks:
  1. Generate mod/manifest.json, mod/index.js, and config.json.
  2. Generate modern SVG & PNG mod thumbnail (mod/thumbnail.png and thumbnail.svg).
  3. Validate all required mod data files in mod/data/<CITY>/:
       - demand_data.json.gz (and demand_data.json)
       - roads.geojson.gz (and roads.geojson)
       - runways_taxiways.geojson.gz (and runways_taxiways.geojson)
       - tiles.pmtiles
       - foundations.pmtiles
       - buildings_index.bin.gz (and buildings_index.bin)
       - building_tags.json.gz
       - coastline.geojson.gz
       - ocean_depth_index.json.gz
  4. Automatically deploy into Subway Builder local directories:
       - %APPDATA%/metro-maker4/mods/<mod_id>/
       - %APPDATA%/metro-maker4/cities/data/<CITY>/
  5. Generate comprehensive summary report reports/99_package_mod.json.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import struct
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent


def create_thumbnail_png(png_path: Path, svg_path: Path, code: str = "BLR", name: str = "Bengaluru") -> None:
    """Generate a high-contrast thumbnail for Subway Builder UI."""
    W = 400
    H = 260
    rgb = np.full((H, W, 3), (15, 23, 42), dtype=np.uint8)

    # Gradient background
    for y in range(H):
        ratio = y / float(H)
        r = int(15 + ratio * (20 - 15))
        g = int(23 + ratio * (35 - 23))
        b = int(42 + ratio * (60 - 42))
        rgb[y, :] = (r, g, b)

    if code == "PNQ":
        # Pune Metro Transit Lines: Line 1 (Purple), Line 2 (Aqua), Line 3 (Red Puneri Metro) + Bhosari/Chakan & Airport Spurs
        lines = [
            # Line 1 (Purple Line: PCMC/Nigdi to Swargate)
            ([(170, 40), (175, 75), (180, 105), (190, 135), (200, 160), (205, 175), (210, 220)], (168, 85, 247), 4),
            # Line 2 (Aqua Line: Vanaz/Kothrud to Ramwadi/Viman Nagar)
            ([(90, 205), (150, 185), (205, 175), (235, 165), (270, 145), (330, 130)], (6, 182, 212), 4),
            # Line 3 (Red Line: Hinjawadi Phase 3 to Civil Court)
            ([(45, 105), (85, 120), (130, 135), (165, 150), (205, 175)], (239, 68, 68), 3),
            # Bhosari MIDC to Chakan Auto Cluster Spur
            ([(180, 105), (225, 80), (255, 35)], (249, 115, 22), 3),
            # Yerawada to Lohegaon Pune Airport Spur
            ([(270, 145), (310, 105), (345, 85)], (56, 189, 248), 3),
        ]
        centers = [(205, 175), (200, 160), (235, 165), (210, 220), (85, 120), (225, 80)]
        title_text = "PUNE + PCMC TWIN-CITY REGION"
        sub_text = "PNQ • 7.46M POP • 300K BUILDINGS • HINJAWADI & CHAKAN"
    elif code == "HYD":
        # Hyderabad Metro Transit Lines: Red Line (Miyapur-LB Nagar), Blue Line (Raidurg-Nagole), Green Line (JBS-MGBS), Airport Express
        lines = [
            # Red Line (Corridor 1: Miyapur / Kukatpally -> Ameerpet -> MGBS -> LB Nagar)
            ([(70, 65), (120, 95), (170, 120), (220, 160), (265, 190), (320, 220)], (239, 68, 68), 4),
            # Blue Line (Corridor 3: Raidurg / HITEC City -> Ameerpet -> Parade Ground -> Uppal / Nagole)
            ([(55, 150), (110, 135), (170, 120), (240, 105), (290, 115), (345, 135)], (37, 99, 235), 4),
            # Green Line (Corridor 2: JBS / Parade Ground -> Musheerabad -> MGBS -> Falaknuma)
            ([(240, 65), (240, 105), (235, 135), (220, 160), (210, 215)], (34, 197, 94), 4),
            # Airport Express (Raidurg / Gachibowli -> Shamshabad RGIA)
            ([(55, 150), (85, 180), (130, 215), (175, 245)], (245, 158, 11), 3),
            # Industrial Radial Spur (Patancheru / BHEL -> Miyapur)
            ([(30, 45), (70, 65)], (168, 85, 247), 3),
        ]
        # Major interchanges: Ameerpet (Red/Blue), MGBS (Red/Green), Parade Ground (Blue/Green), Raidurg (Blue/Airport)
        centers = [(170, 120), (220, 160), (240, 105), (55, 150)]
        title_text = "GREATER HYDERABAD METROPOLITAN AREA"
        sub_text = "HYD • 10.0M POP • CYBERABAD • CHARMINAR • RGIA AIRPORT"
    elif code == "BOM":
        # Mumbai Transit Lines: Western Line, Central Line, Aqua Line 3, Line 1 Versova-Ghatkopar
        lines = [
            # Western Suburban Line (Churchgate to Virar)
            ([(90, 240), (95, 180), (100, 130), (105, 80), (110, 30)], (239, 68, 68), 4),
            # Central Main Line (CSMT to Thane, Kalyan, Titwala)
            ([(110, 230), (120, 170), (145, 120), (185, 95), (250, 75), (320, 55)], (249, 115, 22), 4),
            # Aqua Line 3 (Colaba - BKC - SEEPZ)
            ([(95, 235), (105, 175), (125, 130), (140, 105)], (6, 182, 212), 3),
            # Metro Line 1 & Line 2 (East-West & Western suburbs)
            ([(70, 115), (105, 115), (155, 115)], (234, 179, 8), 3),
            # Harbour / Atal Setu Line (to Panvel & NMIAL)
            ([(110, 230), (160, 200), (220, 180), (280, 195), (340, 210)], (56, 189, 248), 3),
        ]
        centers = [(105, 175), (125, 130), (185, 95), (250, 75)]
        title_text = "MUMBAI METROPOLITAN REGION"
        sub_text = "BOM • 24.5M POP • 254K BUILDINGS • TITWALA INCL."
    else:
        # Abstract Bengaluru transit metro network lines
        lines = [
            ([(40, 140), (120, 135), (200, 130), (280, 125), (360, 120)], (168, 85, 247), 4),
            ([(200, 30), (200, 80), (200, 130), (210, 180), (260, 230)], (34, 197, 94), 4),
            ([(210, 180), (280, 200), (350, 220)], (234, 179, 8), 3),
            ([(200, 30), (270, 70), (310, 110), (280, 160)], (56, 189, 248), 3),
        ]
        centers = [(200, 130), (210, 180), (120, 135), (280, 125)]
        title_text = "BENGALURU + HOSUR"
        sub_text = "BLR • 13.98M POP • 831K BUILDINGS"

    for pts, col, th in lines:
        for i in range(len(pts) - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            steps = max(abs(x1 - x0), abs(y1 - y0), 1)
            xs = np.linspace(x0, x1, steps + 1).astype(np.int32)
            ys = np.linspace(y0, y1, steps + 1).astype(np.int32)
            for dy in range(-th // 2, th // 2 + 1):
                for dx in range(-th // 2, th // 2 + 1):
                    yc = np.clip(ys + dy, 0, H - 1)
                    xc = np.clip(xs + dx, 0, W - 1)
                    rgb[yc, xc] = col

    for cx, cy in centers:
        for r in range(4, 8):
            for deg in range(0, 360, 5):
                rad = np.radians(deg)
                px = int(round(cx + r * np.cos(rad)))
                py = int(round(cy + r * np.sin(rad)))
                if 0 <= px < W and 0 <= py < H:
                    rgb[py, px] = (255, 255, 255)

    # Encode PNG
    raw_scanlines = bytearray()
    for y in range(H):
        raw_scanlines.append(0)
        raw_scanlines.extend(rgb[y].tobytes())

    def make_chunk(chunk_type: bytes, data: bytes) -> bytes:
        chunk = struct.pack(">I", len(data)) + chunk_type + data
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return chunk + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
    compressed = zlib.compress(bytes(raw_scanlines), level=6)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + make_chunk(b"IHDR", ihdr)
        + make_chunk(b"IDAT", compressed)
        + make_chunk(b"IEND", b"")
    )
    png_path.write_bytes(png_bytes)

    # Encode companion SVG
    svg_doc = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#0f172a" />
      <stop offset="100%" stop-color="#1e293b" />
    </linearGradient>
  </defs>
  <rect width="{W}" height="{H}" fill="url(#bg)" />
  <text x="20" y="32" fill="#f8fafc" font-family="sans-serif" font-size="15" font-weight="bold">{title_text}</text>
  <text x="20" y="48" fill="#94a3b8" font-family="sans-serif" font-size="10">{sub_text}</text>
</svg>"""
    svg_path.write_text(svg_doc, encoding="utf-8")


def package_and_deploy(city_config_path: Path | None = None) -> Dict[str, Any]:
    t0 = time.perf_counter()
    if city_config_path is None:
        city_config_path = ROOT / "cities" / "bom.yaml"
        if not city_config_path.exists():
            city_config_path = ROOT / "cities" / "blr.yaml"

    with open(city_config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    code = cfg["code"].upper()
    name = cfg["name"]
    if code == "PNQ":
        mod_id = "pnq-pune-pcmc"
        mod_name = f"Pune + PCMC ({code})"
    elif code == "BOM":
        mod_id = f"{code.lower()}-mumbai-mmr"
        mod_name = f"Mumbai Metropolitan Region ({code})"
    elif code == "HYD":
        mod_id = f"{code.lower()}-greater-hyderabad"
        mod_name = f"Greater Hyderabad ({code})"
    else:
        mod_id = "blr-bengaluru-hosur"
        mod_name = f"Bengaluru + Hosur ({code})"

    mod_dir = ROOT / "mod"
    data_dir = mod_dir / "data" / code
    reports_dir = ROOT / "reports"
    mod_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    # 1. Generate thumbnails
    thumb_png = mod_dir / "thumbnail.png"
    thumb_svg = mod_dir / "thumbnail.svg"
    create_thumbnail_png(thumb_png, thumb_svg, code=code, name=name)

    # 2. Write mod manifest.json
    manifest = {
        "id": mod_id,
        "name": mod_name,
        "version": "1.0.0",
        "description": cfg.get("description", f"High-density transit network mod for {name} ({code})"),
        "author": cfg.get("creator", "Vadapao26"),
        "main": "index.js",
        "thumbnail": "thumbnail.png",
        "dependencies": {
            "subway-builder": ">=1.0.0"
        }
    }
    manifest_path = mod_dir / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # 3. Write mod index.js
    index_js = f"""// Subway Builder Mod Entry - {name} ({code})
(function () {{
  if (typeof window === "undefined" || !window.SubwayBuilderAPI) return;
  const api = window.SubwayBuilderAPI;
  api.cities.registerCity({{
    code: "{code}",
    name: "{name}",
    country: "{cfg.get('country', 'IN')}",
    creator: "{cfg.get('creator', 'Vadapao26')}",
    bbox: [{cfg['bbox']['west']}, {cfg['bbox']['south']}, {cfg['bbox']['east']}, {cfg['bbox']['north']}],
    initialViewState: {{
      latitude: {cfg['initialViewState']['latitude']},
      longitude: {cfg['initialViewState']['longitude']},
      zoom: {cfg['initialViewState']['zoom']},
      bearing: {cfg['initialViewState'].get('bearing', 0)}
    }},
    demandDotScaling: {cfg.get('demandDotScaling', 0.20)}
  }});
}})();
"""
    index_path = mod_dir / "index.js"
    index_path.write_text(index_js, encoding="utf-8")

    # 4. Write data config.json (Go deserialization compatible: integer zoom, valid bbox, creator)
    config_json = {
        "code": code,
        "name": name,
        "bbox": [cfg["bbox"]["west"], cfg["bbox"]["south"], cfg["bbox"]["east"], cfg["bbox"]["north"]],
        "description": cfg.get("description", f"High-density transit network mod for {name} ({code})"),
        "population": cfg.get("population", 7500000),
        "minZoom": int(cfg.get("minZoom", cfg["initialViewState"].get("minZoom", 10))),
        "maxZoom": int(cfg.get("maxZoom", cfg["initialViewState"].get("maxZoom", 16))),
        "initialViewState": {
            "latitude": cfg["initialViewState"]["latitude"],
            "longitude": cfg["initialViewState"]["longitude"],
            "zoom": int(round(cfg["initialViewState"]["zoom"])),
            "bearing": int(round(cfg["initialViewState"].get("bearing", 0))),
            "minZoom": int(cfg.get("minZoom", cfg["initialViewState"].get("minZoom", 10))),
            "maxZoom": int(cfg.get("maxZoom", cfg["initialViewState"].get("maxZoom", 16))),
        },
        "creator": cfg.get("creator", "Vadapao26"),
        "version": "1.0.0",
        "country": cfg.get("country", "IN"),
        "demandDotScaling": cfg.get("demandDotScaling", 0.20),
    }
    cfg_json_path = data_dir / "config.json"
    with open(cfg_json_path, "w", encoding="utf-8") as f:
        json.dump(config_json, f, indent=2)

    is_coastal = bool(cfg.get("is_coastal", code in ("BOM", "NYC", "TOK")))

    if is_coastal:
        # Ensure uncompressed ocean_depth_index.json & coastline.geojson exist alongside .gz
        ocean_gz = data_dir / "ocean_depth_index.json.gz"
        ocean_json = data_dir / "ocean_depth_index.json"
        if ocean_gz.exists() and not ocean_json.exists():
            with gzip.open(ocean_gz, "rt", encoding="utf-8") as f_in, open(ocean_json, "w", encoding="utf-8") as f_out:
                f_out.write(f_in.read())

        coast_gz = data_dir / "coastline.geojson.gz"
        coast_json = data_dir / "coastline.geojson"
        if coast_gz.exists():
            with gzip.open(coast_gz, "rt", encoding="utf-8") as f_in, open(coast_json, "w", encoding="utf-8") as f_out:
                f_out.write(f_in.read())
    else:
        # Landlocked city: purge any lingering fake ocean artifacts
        for ofn in [
            "coastline.geojson", "coastline.geojson.gz",
            "ocean_depth_index.json", "ocean_depth_index.json.gz",
            "ocean_foundations.pmtiles", f"{code}_ocean_foundations.pmtiles"
        ]:
            op = data_dir / ofn
            if op.exists():
                op.unlink()
    required_data_files = [
        "config.json",
        "demand_data.json.gz",
        "roads.geojson.gz",
        "runways_taxiways.geojson.gz",
        "buildings_index.bin.gz",
        "building_tags.json.gz",
    ]
    # Check pmtiles (either <code.pmtiles> or tiles.pmtiles)
    if (data_dir / f"{code}.pmtiles").exists():
        required_data_files.append(f"{code}.pmtiles")
    elif (data_dir / "tiles.pmtiles").exists():
        required_data_files.append("tiles.pmtiles")

    if (data_dir / f"{code}_foundations.pmtiles").exists():
        required_data_files.append(f"{code}_foundations.pmtiles")
    elif (data_dir / "foundations.pmtiles").exists():
        required_data_files.append("foundations.pmtiles")

    if is_coastal:
        required_data_files.extend(["coastline.geojson.gz", "ocean_depth_index.json.gz"])

    file_manifest: Dict[str, int] = {}
    for fn in required_data_files:
        fp = data_dir / fn
        if not fp.exists():
            raise FileNotFoundError(f"Missing required city data file: {fp}")
        file_manifest[fn] = fp.stat().st_size

    # Ensure <CODE>.pmtiles and <CODE>_foundations.pmtiles exist
    if (data_dir / "tiles.pmtiles").exists() and not (data_dir / f"{code}.pmtiles").exists():
        shutil.copy2(data_dir / "tiles.pmtiles", data_dir / f"{code}.pmtiles")
    if (data_dir / "foundations.pmtiles").exists() and not (data_dir / f"{code}_foundations.pmtiles").exists():
        shutil.copy2(data_dir / "foundations.pmtiles", data_dir / f"{code}_foundations.pmtiles")

    # 6. Deploy to Subway Builder AppData directories
    appdata = os.environ.get("APPDATA")
    deployed_mod_path = None
    deployed_city_data_path = None

    if appdata:
        mm_dir = Path(appdata) / "metro-maker4"
        dest_mod = mm_dir / "mods" / mod_id
        dest_city_data = mm_dir / "cities" / "data" / code

        try:
            dest_mod.mkdir(parents=True, exist_ok=True)
            dest_city_data.mkdir(parents=True, exist_ok=True)

            if not is_coastal:
                for ofn in [
                    "coastline.geojson", "coastline.geojson.gz",
                    "ocean_depth_index.json", "ocean_depth_index.json.gz",
                    "ocean_foundations.pmtiles", f"{code}_ocean_foundations.pmtiles"
                ]:
                    stale_target = dest_city_data / ofn
                    if stale_target.exists():
                        stale_target.unlink()

            for mf in ["manifest.json", "index.js", "thumbnail.png", "thumbnail.svg"]:
                src_f = mod_dir / mf
                if src_f.exists():
                    shutil.copy2(src_f, dest_mod / mf)

            for fn in os.listdir(data_dir):
                src_file = data_dir / fn
                if src_file.is_file():
                    shutil.copy2(src_file, dest_city_data / fn)

            deployed_mod_path = str(dest_mod).replace("\\", "/")
            deployed_city_data_path = str(dest_city_data).replace("\\", "/")
            print(f"[99_package_mod] Deployed mod package to: {dest_mod}")
            print(f"[99_package_mod] Deployed city data files to: {dest_city_data}")
        except PermissionError as pe:
            print(
                f"[99_package_mod] Notice: Sandboxed run could not write directly to {pe.filename}. "
                f"Mod package and data files are prepared in mod/ and ready for manual or bypass copy."
            )

    # 7. Create clean distributable city map zip archive matching official registry requirements
    dist_dir = ROOT / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dist_dir / f"SubwayBuilder-{code}-v1.0.0.zip"
    import zipfile

    # Include both canonical (tiles.pmtiles, foundations.pmtiles) for Railyard & Subway Builder
    # and city-coded (BLR.pmtiles, BLR_foundations.pmtiles) for registry standards.
    map_zip_files = [
        "config.json",
        "demand_data.json.gz",
        "roads.geojson.gz",
        "runways_taxiways.geojson.gz",
        "tiles.pmtiles",
        "foundations.pmtiles",
        f"{code}.pmtiles",
        f"{code}_foundations.pmtiles",
        "buildings_index.bin.gz",
        "building_tags.json.gz",
    ]
    if is_coastal:
        if (data_dir / "coastline.geojson.gz").exists():
            map_zip_files.append("coastline.geojson.gz")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for fn in map_zip_files:
            fpath = data_dir / fn
            if fpath.is_file():
                zf.write(fpath, arcname=fn)
            else:
                print(f"[99_package_mod] Warning: optional map file {fn} not found in {data_dir}")

    print(f"[99_package_mod] Clean city map zip created: {zip_path} ({zip_path.stat().st_size / 1e6:.2f} MB)")

    # Also package standalone code mod zip if user submits mod separately
    code_mod_zip = dist_dir / f"registry-codemod-{code.lower()}.zip"
    with zipfile.ZipFile(code_mod_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf_mod:
        for mf in ["manifest.json", "index.js", "thumbnail.png", "thumbnail.svg"]:
            fpath = mod_dir / mf
            if fpath.exists():
                zf_mod.write(fpath, arcname=mf)
    print(f"[99_package_mod] Standalone code mod zip created: {code_mod_zip} ({code_mod_zip.stat().st_size / 1e3:.1f} KB)")

    elapsed = round(time.perf_counter() - t0, 2)
    summary = {
        "phase": 10,
        "script": "pipeline/99_package_mod.py",
        "city_code": code,
        "status": "PASS",
        "runtime_seconds": elapsed,
        "mod_package": {
            "id": mod_id,
            "name": mod_name,
            "version": "1.0.0",
            "manifest": str(manifest_path.relative_to(ROOT)).replace("\\", "/"),
            "entry": str(index_path.relative_to(ROOT)).replace("\\", "/"),
            "thumbnail_png": str(thumb_png.relative_to(ROOT)).replace("\\", "/"),
            "thumbnail_svg": str(thumb_svg.relative_to(ROOT)).replace("\\", "/"),
        },
        "city_data_files": file_manifest,
        "total_data_bytes": sum(file_manifest.values()),
        "deployment": {
            "appdata_detected": bool(appdata),
            "mod_path": deployed_mod_path,
            "city_data_path": deployed_city_data_path,
        },
    }

    report_path = reports_dir / "99_package_mod.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 10: Mod packaging and integration")
    parser.add_argument("--city", type=Path, default=Path("cities/bom.yaml"), help="Path to city YAML config")
    args = parser.parse_args()
    city_path = args.city if args.city.is_absolute() else (ROOT / args.city)

    res = package_and_deploy(city_path)
    print(
        f"[99_package_mod] Phase 10 Complete ({res['runtime_seconds']}s): "
        f"Mod packaged with {len(res['city_data_files'])} data files "
        f"({res['total_data_bytes'] / 1e6:.2f} MB total)."
    )


if __name__ == "__main__":
    main()

