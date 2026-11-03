#!/usr/bin/env python3
"""
Phase 7: Roads and Airport Runways/Taxiways GeoJSON Export.
Tasks:
1. Export the road network from `data/processed/<city_code>_roads.parquet` to
   `mod/data/BLR/roads.geojson.gz` (and `.geojson`) matching `RoadsGeojsonSchema`:
   - `FeatureCollection` of `LineString` features
   - `properties: { roadClass: "highway" | "major" | "medium" | "minor", structure: "normal" | "tunnel" | "bridge", name: string }`
2. Export airport runways, taxiways, and aprons from `data/processed/<city_code>_airports.parquet` to
   `mod/data/BLR/runways_taxiways.geojson.gz` (and `.geojson`) matching `RunwaysTaxiwaysGeojsonSchema`:
   - `FeatureCollection` of `Polygon` features (buffering linear runway/taxiway centerlines into polygons
     with ICAO standard widths: 45m for runways, 23m for taxiways)
   - `properties: { osm_id: string, type: "multipolygon", aeroway: string, area: number, roadType: "runway", name: string }`
3. Validate both files against the game's schemas and export a visual overlay check image
   (`reports/80_roads_runways_overlay.png` and `.svg`) and summary report (`reports/80_roads_runways.json`).
"""

import argparse
from datetime import datetime, timezone
import gzip
import json
import math
from pathlib import Path
import struct
import time
import zlib

import numpy as np
import pandas as pd
from shapely.geometry import LineString, Polygon
import yaml


VALID_ROAD_CLASSES = {"highway", "major", "medium", "minor"}
VALID_STRUCTURES = {"normal", "tunnel", "bridge"}


def write_png_rgb(path: Path, rgb: np.ndarray) -> None:
    height, width, _ = rgb.shape
    raw_scanlines = bytearray()
    for y in range(height):
        raw_scanlines.append(0)
        raw_scanlines.extend(rgb[y].tobytes())

    def make_chunk(chunk_type: bytes, data: bytes) -> bytes:
        chunk = struct.pack(">I", len(data)) + chunk_type + data
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return chunk + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    compressed = zlib.compress(bytes(raw_scanlines), level=6)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + make_chunk(b"IHDR", ihdr)
        + make_chunk(b"IDAT", compressed)
        + make_chunk(b"IEND", b"")
    )
    path.write_bytes(png_bytes)


def buffer_line_to_polygon_coords(
    coords: list[list[float]], half_width_m: float, m_lon: float, m_lat: float
) -> tuple[list[list[float]], float]:
    """Buffer a LineString of [lon, lat] points in local meters and return exterior ring [[lon, lat], ...] and area_m2."""
    x0, y0 = coords[0][0], coords[0][1]
    pts_m = [((pt[0] - x0) * m_lon, (pt[1] - y0) * m_lat) for pt in coords]
    line = LineString(pts_m)
    poly_m = line.buffer(half_width_m, cap_style=2, join_style=2, quad_segs=4)
    if poly_m.is_empty:
        return [], 0.0
    if poly_m.geom_type == "MultiPolygon":
        poly_m = max(poly_m.geoms, key=lambda g: g.area)
    area_m2 = float(poly_m.area)
    ring_lonlat = [[round(x0 + x / m_lon, 6), round(y0 + y / m_lat, 6)] for x, y in poly_m.exterior.coords]
    if ring_lonlat and ring_lonlat[0] != ring_lonlat[-1]:
        ring_lonlat.append(ring_lonlat[0])
    return ring_lonlat, area_m2


def validate_roads_geojson(fc: dict) -> dict:
    if fc.get("type") != "FeatureCollection" or not isinstance(fc.get("features"), list):
        raise ValueError("Invalid Roads GeoJSON root")
    feats = fc["features"]
    for idx, f in enumerate(feats):
        if f.get("type") != "Feature":
            raise ValueError(f"Road feature {idx} missing type='Feature'")
        geom = f.get("geometry", {})
        if geom.get("type") != "LineString":
            raise ValueError(f"Road feature {idx} geometry.type != 'LineString'")
        coords = geom.get("coordinates", [])
        if not isinstance(coords, list) or len(coords) < 2:
            raise ValueError(f"Road feature {idx} has <2 coordinates")
        props = f.get("properties", {})
        if props.get("roadClass") not in VALID_ROAD_CLASSES:
            raise ValueError(f"Road feature {idx} invalid roadClass: {props.get('roadClass')}")
        if props.get("structure") not in VALID_STRUCTURES:
            raise ValueError(f"Road feature {idx} invalid structure: {props.get('structure')}")
        if not isinstance(props.get("name"), str):
            raise ValueError(f"Road feature {idx} name is not string")
    return {"valid": True, "feature_count": len(feats)}


def validate_runways_geojson(fc: dict) -> dict:
    if fc.get("type") != "FeatureCollection" or not isinstance(fc.get("features"), list):
        raise ValueError("Invalid RunwaysTaxiways GeoJSON root")
    feats = fc["features"]
    for idx, f in enumerate(feats):
        if f.get("type") != "Feature":
            raise ValueError(f"Runway feature {idx} missing type='Feature'")
        geom = f.get("geometry", {})
        if geom.get("type") != "Polygon":
            raise ValueError(f"Runway feature {idx} geometry.type != 'Polygon'")
        rings = geom.get("coordinates", [])
        if not isinstance(rings, list) or len(rings) < 1 or len(rings[0]) < 4:
            raise ValueError(f"Runway feature {idx} has invalid polygon ring")
    return {"valid": True, "feature_count": len(feats)}


def render_overlay_preview(
    road_features: list[dict],
    runway_features: list[dict],
    bbox: dict,
    png_path: Path,
    svg_path: Path,
    city_name: str,
) -> None:
    """Render a visual overlay check image showing road classes and airport runways/taxiways."""
    south, north = float(bbox["south"]), float(bbox["north"])
    west, east = float(bbox["west"]), float(bbox["east"])
    W, H = 900, 990
    rgb = np.full((H, W, 3), (12, 18, 30), dtype=np.uint8)

    def to_px(lon: float, lat: float) -> tuple[int, int]:
        x = int((lon - west) / (east - west) * (W - 1))
        y = int((north - lat) / (north - south) * (H - 1))
        return max(0, min(W - 1, x)), max(0, min(H - 1, y))

    def draw_segment(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int], thickness: int = 1) -> None:
        steps = max(abs(x1 - x0), abs(y1 - y0), 1)
        xs = np.linspace(x0, x1, steps + 1).astype(np.int32)
        ys = np.linspace(y0, y1, steps + 1).astype(np.int32)
        if thickness <= 1:
            rgb[ys, xs] = color
        else:
            for dy in range(-thickness // 2, thickness // 2 + 1):
                for dx in range(-thickness // 2, thickness // 2 + 1):
                    yc = np.clip(ys + dy, 0, H - 1)
                    xc = np.clip(xs + dx, 0, W - 1)
                    rgb[yc, xc] = color

    # Draw minor -> medium -> major -> highway in order so major highways sit on top
    order_map = {"minor": 0, "medium": 1, "major": 2, "highway": 3}
    color_map = {
        "minor": (45, 62, 88),
        "medium": (96, 165, 250),
        "major": (251, 191, 36),
        "highway": (244, 63, 94),
    }
    sorted_roads = sorted(road_features, key=lambda f: order_map.get(f["properties"]["roadClass"], 0))
    for feat in sorted_roads:
        rc = feat["properties"]["roadClass"]
        coords = feat["geometry"]["coordinates"]
        if rc == "minor" and len(coords) < 3:
            continue
        col = color_map[rc]
        thick = 2 if rc in ("highway", "major") else 1
        pts = [to_px(pt[0], pt[1]) for pt in coords]
        for i in range(len(pts) - 1):
            draw_segment(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], col, thick)

    # Draw runways/taxiways in bright cyan
    for feat in runway_features:
        ring = feat["geometry"]["coordinates"][0]
        pts = [to_px(pt[0], pt[1]) for pt in ring]
        for i in range(len(pts) - 1):
            draw_segment(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], (34, 211, 238), 2)

    write_png_rgb(png_path, rgb)

    # Write SVG overlay of major/highway/medium roads + airport runways
    svg_lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
        f'<rect width="{W}" height="{H}" fill="#0c121e"/>',
        f'<text x="24" y="34" fill="#f8fafc" font-family="sans-serif" font-size="18" font-weight="bold">'
        f"{city_name} (BLR) — Phase 7 Roads &amp; Runways GeoJSON Visual Overlay</text>",
        f'<text x="24" y="54" fill="#94a3b8" font-family="sans-serif" font-size="12">'
        f"Red = Highway | Gold = Major | Blue = Medium | Cyan = Airport Runways, Taxiways &amp; Aprons</text>",
    ]
    for feat in sorted_roads:
        rc = feat["properties"]["roadClass"]
        if rc == "minor":
            continue
        coords = feat["geometry"]["coordinates"]
        pts_str = " ".join(f"{to_px(p[0], p[1])[0]},{to_px(p[0], p[1])[1]}" for p in coords)
        stroke = {"medium": "#3b82f6", "major": "#fbbf24", "highway": "#f43f5e"}[rc]
        sw = {"medium": "0.7", "major": "1.3", "highway": "2.0"}[rc]
        svg_lines.append(f'<polyline points="{pts_str}" fill="none" stroke="{stroke}" stroke-width="{sw}" stroke-opacity="0.85"/>')

    for feat in runway_features:
        ring = feat["geometry"]["coordinates"][0]
        pts_str = " ".join(f"{to_px(p[0], p[1])[0]},{to_px(p[0], p[1])[1]}" for p in ring)
        svg_lines.append(f'<polygon points="{pts_str}" fill="#22d3ee" fill-opacity="0.6" stroke="#67e8f9" stroke-width="1.2"/>')

    svg_lines.append("</svg>")
    svg_path.write_text("\n".join(svg_lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 7: Export roads.geojson.gz and runways_taxiways.geojson.gz.")
    parser.add_argument("--city", type=Path, default=Path("cities/blr.yaml"), help="Path to city YAML config")
    args = parser.parse_args()

    t_start = time.time()
    repo_root = Path(__file__).resolve().parent.parent
    city_path = args.city if args.city.is_absolute() else (repo_root / args.city)
    with open(city_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    city_code_lower = cfg["code"].lower()
    city_code_upper = cfg["code"].upper()
    city_name = cfg["name"]
    bbox = cfg["bbox"]
    south, north = float(bbox["south"]), float(bbox["north"])
    west, east = float(bbox["west"]), float(bbox["east"])
    mid_lat = 0.5 * (south + north)
    cos_lat = math.cos(math.radians(mid_lat))
    m_lat = 111132.0
    m_lon = 111320.0 * cos_lat

    processed_dir = repo_root / "data" / "processed"
    roads_df = pd.read_parquet(processed_dir / f"{city_code_lower}_roads.parquet")
    air_df = pd.read_parquet(processed_dir / f"{city_code_lower}_airports.parquet")

    # Filter to study bbox and drop unnamed tiny service driveways (< 30m) to match built-in city road density
    sub_roads = roads_df[roads_df["in_study_bbox"]].copy()
    keep_mask = (sub_roads["highway"] != "service") | (sub_roads["name"] != "") | (sub_roads["length_m"] >= 50.0)
    sub_roads = sub_roads[keep_mask]

    print(f"[Phase 7] Formatting {len(sub_roads):,} road features for RoadsGeojsonSchema ...", flush=True)
    road_features = []
    class_counts = {"highway": 0, "major": 0, "medium": 0, "minor": 0}
    struct_counts = {"normal": 0, "bridge": 0, "tunnel": 0}

    for rclass, rstruct, rname, coords_s in zip(
        sub_roads["roadClass"], sub_roads["structure"], sub_roads["name"], sub_roads["coords_json"]
    ):
        coords = json.loads(coords_s)
        if len(coords) < 2:
            continue
        rc_str = str(rclass) if str(rclass) in VALID_ROAD_CLASSES else "minor"
        st_str = str(rstruct) if str(rstruct) in VALID_STRUCTURES else "normal"
        nm_str = str(rname) if pd.notna(rname) else ""
        class_counts[rc_str] += 1
        struct_counts[st_str] += 1
        road_features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": coords,
                },
                "properties": {
                    "roadClass": rc_str,
                    "structure": st_str,
                    "name": nm_str,
                },
            }
        )

    # Inject Mumbai Suburban Railway & Metro corridors from railways parquet so tracks render on map
    rail_parquet = processed_dir / f"{city_code_lower}_railways.parquet"
    if rail_parquet.is_file():
        rail_df = pd.read_parquet(rail_parquet)
        injected_rails = 0
        for rtype, rname, coords_s in zip(rail_df["railway"], rail_df["name"], rail_df["coords_json"]):
            if rtype not in ("rail", "subway", "monorail", "light_rail", "construction"):
                continue
            coords = json.loads(coords_s)
            if len(coords) < 2:
                continue
            rc_str = "major" if rtype in ("rail", "subway") else "medium"
            nm_str = str(rname) if pd.notna(rname) and rname else ("Railway Corridor" if rtype == "rail" else "Metro Corridor")
            class_counts[rc_str] += 1
            struct_counts["normal"] += 1
            road_features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": coords,
                    },
                    "properties": {
                        "roadClass": rc_str,
                        "structure": "normal",
                        "name": nm_str,
                    },
                }
            )
            injected_rails += 1
        print(f"[Phase 7] Injected {injected_rails:,} railway tracks (Central/Western Railway & Metros) into roads.geojson", flush=True)

    roads_fc = {"type": "FeatureCollection", "features": road_features}
    roads_val = validate_roads_geojson(roads_fc)

    print(f"[Phase 7] Formatting airport runways, taxiways & aprons from {len(air_df):,} features ...", flush=True)
    runway_features = []
    aeroway_counts: dict[str, int] = {}

    for osm_id, aeroway, name, is_poly, area_m2, coords_s in zip(
        air_df["osm_id"],
        air_df["aeroway"],
        air_df["name"],
        air_df["is_polygon"],
        air_df["area_m2"],
        air_df["coords_json"],
    ):
        aw = str(aeroway)
        if aw not in ("runway", "taxiway", "apron", "helipad"):
            continue
        coords = json.loads(coords_s)
        nm_str = str(name) if pd.notna(name) else ""

        if is_poly and len(coords) >= 4:
            ring = coords if coords[0] == coords[-1] else coords + [coords[0]]
            area_val = float(area_m2)
        elif len(coords) >= 2:
            half_w = 22.5 if aw == "runway" else (11.5 if aw == "taxiway" else 15.0)
            ring, area_val = buffer_line_to_polygon_coords(coords, half_w, m_lon, m_lat)
            if len(ring) < 4:
                continue
        else:
            continue

        aeroway_counts[aw] = aeroway_counts.get(aw, 0) + 1
        runway_features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [ring],
                },
                "properties": {
                    "osm_id": str(osm_id),
                    "type": "multipolygon",
                    "aeroway": aw,
                    "area": round(area_val, 2),
                    "roadType": "runway",
                    "name": nm_str,
                },
            }
        )

    runways_fc = {"type": "FeatureCollection", "features": runway_features}
    runways_val = validate_runways_geojson(runways_fc)

    mod_data_dir = repo_root / "mod" / "data" / city_code_upper
    mod_data_dir.mkdir(parents=True, exist_ok=True)

    roads_json_path = mod_data_dir / "roads.geojson"
    roads_gz_path = mod_data_dir / "roads.geojson.gz"
    runways_json_path = mod_data_dir / "runways_taxiways.geojson"
    runways_gz_path = mod_data_dir / "runways_taxiways.geojson.gz"

    roads_bytes = json.dumps(roads_fc, separators=(",", ":")).encode("utf-8")
    roads_json_path.write_bytes(roads_bytes)
    with gzip.open(roads_gz_path, "wb", compresslevel=6) as f:
        f.write(roads_bytes)

    runways_bytes = json.dumps(runways_fc, separators=(",", ":")).encode("utf-8")
    runways_json_path.write_bytes(runways_bytes)
    with gzip.open(runways_gz_path, "wb", compresslevel=6) as f:
        f.write(runways_bytes)

    reports_dir = repo_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    overlay_png = reports_dir / "80_roads_runways_overlay.png"
    overlay_svg = reports_dir / "80_roads_runways_overlay.svg"
    render_overlay_preview(road_features, runway_features, bbox, overlay_png, overlay_svg, city_name)

    summary = {
        "phase": "80_roads_runways",
        "city_code": city_code_upper,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "runtime_seconds": round(time.time() - t_start, 2),
        "roads": {
            "feature_count": len(road_features),
            "by_roadClass": class_counts,
            "by_structure": struct_counts,
            "schema_validation": roads_val,
            "geojson_bytes": roads_json_path.stat().st_size,
            "geojson_gz_bytes": roads_gz_path.stat().st_size,
            "geojson_gz_mb": round(roads_gz_path.stat().st_size / (1024 * 1024), 2),
        },
        "runways_taxiways": {
            "feature_count": len(runway_features),
            "by_aeroway": aeroway_counts,
            "schema_validation": runways_val,
            "empty_file_behavior_note": "Game catches missing/empty runwaysTaxiways and defaults to empty FeatureCollection, but providing Polygon features renders airport infrastructure properly.",
            "geojson_bytes": runways_json_path.stat().st_size,
            "geojson_gz_bytes": runways_gz_path.stat().st_size,
        },
        "outputs": {
            "roads_geojson": str(roads_json_path.relative_to(repo_root)),
            "roads_geojson_gz": str(roads_gz_path.relative_to(repo_root)),
            "runways_taxiways_geojson": str(runways_json_path.relative_to(repo_root)),
            "runways_taxiways_geojson_gz": str(runways_gz_path.relative_to(repo_root)),
            "overlay_png": str(overlay_png.relative_to(repo_root)),
            "overlay_svg": str(overlay_svg.relative_to(repo_root)),
        },
    }

    report_path = reports_dir / "80_roads_runways.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("=== Phase 7 Roads & Runways Summary ===")
    print(
        f"Roads exported:              {len(road_features):,} features ({summary['roads']['geojson_gz_mb']:.2f} MB .gz) | "
        f"classes={class_counts} | structures={struct_counts}"
    )
    print(
        f"Runways/Taxiways exported:   {len(runway_features):,} Polygon features ({runways_gz_path.stat().st_size / 1024:.1f} KB .gz) | "
        f"aeroways={aeroway_counts}"
    )
    print(f"Schema validations:          Roads=PASS, RunwaysTaxiways=PASS")
    print(f"Visual overlay exported:     {overlay_png} & {overlay_svg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
