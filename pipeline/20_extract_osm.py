#!/usr/bin/env python3
"""
Phase 2: OpenStreetMap Extraction & Coverage Report.
Tasks:
1. Extract from the clipped OSM PBF (`data/raw/blr_clipped.osm.pbf`):
   - Buildings (`building=*`, with `building:levels`, `height`, `name`, `amenity`, `shop`, `office` tags)
   - Highways (`highway=*`, with `name`, `bridge`, `tunnel`, `oneway`, `maxspeed`, `lanes` tags)
   - Land use (`landuse=*`, `amenity=university/college/hospital/school`)
   - Airports (`aeroway=runway`, `taxiway`, `apron`, `aerodrome`, `terminal`)
   - Water (`natural=water`, `waterway=*`, `water=*`)
   - Parks (`leisure=park/garden/nature_reserve/golf_course/pitch/stadium`, `landuse=recreation_ground/grass/meadow/forest/village_green`)
   - Railways (`railway=*`)
   - Named employment POIs (nodes and way centroids with `name` + `office`/`shop`/`amenity`/`industrial`/`commercial`/`aeroway`)
2. Save extracted datasets to `data/processed/<city_code>_*.parquet` for downstream phases (Phases 4, 5, 7, 8, 9).
3. Produce a coverage report:
   - Overall metrics and per-grid-cell metrics (`reports/20_coverage_grid.csv` and `reports/20_extract_osm.json`):
     building count, share of buildings with levels or height tags, total building footprint area per grid cell,
     road length per km², and sparse-coverage flags where populated areas have sparse OSM buildings.
   - Simple map image (`reports/20_coverage_map.svg` and `.bmp`/`.png`) showing building footprint density,
     road density, and flagged sparse cells.
"""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import struct
import sys
import time
import zlib

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from osm_pbf_utils import (
    iter_pbf_blobs,
    parse_dense_nodes,
    parse_primitive_block,
    parse_way,
    read_varint,
    skip_field,
)


def parse_float_prefix(val: str | None) -> float | None:
    if not val:
        return None
    s = val.strip().lower().replace("m", "").replace("meters", "").strip()
    num_chars = []
    for ch in s:
        if ch in "0123456789.-":
            num_chars.append(ch)
        elif num_chars:
            break
    if not num_chars:
        return None
    try:
        v = float("".join(num_chars))
        return v if math.isfinite(v) and v > 0 else None
    except ValueError:
        return None


def polygon_area_m2(coords: list[tuple[float, float]], cos_lat: float) -> float:
    """Shoelace formula in local meters for a closed ring of (lon, lat) points."""
    if len(coords) < 3:
        return 0.0
    m_per_deg_lat = 111132.0
    m_per_deg_lon = 111320.0 * cos_lat
    x0, y0 = coords[0]
    area2 = 0.0
    for i in range(len(coords) - 1):
        x1 = (coords[i][0] - x0) * m_per_deg_lon
        y1 = (coords[i][1] - y0) * m_per_deg_lat
        x2 = (coords[i + 1][0] - x0) * m_per_deg_lon
        y2 = (coords[i + 1][1] - y0) * m_per_deg_lat
        area2 += x1 * y2 - x2 * y1
    return abs(area2) * 0.5


def polyline_length_m(coords: list[tuple[float, float]], cos_lat: float) -> float:
    if len(coords) < 2:
        return 0.0
    m_per_deg_lat = 111132.0
    m_per_deg_lon = 111320.0 * cos_lat
    total = 0.0
    for i in range(len(coords) - 1):
        dx = (coords[i + 1][0] - coords[i][0]) * m_per_deg_lon
        dy = (coords[i + 1][1] - coords[i][1]) * m_per_deg_lat
        total += math.hypot(dx, dy)
    return total


def write_png_rgb(path: Path, rgb: np.ndarray) -> None:
    """Write a 3-channel uint8 numpy array (H, W, 3) to a standard PNG file using stdlib zlib."""
    height, width, _ = rgb.shape
    # Prepend filter byte 0 to each scanline
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


def render_coverage_images(
    grid_df: pd.DataFrame,
    rows: int,
    cols: int,
    bbox: dict,
    svg_path: Path,
    png_path: Path,
    city_name: str,
) -> None:
    """Render both a crisp vector SVG and a raster PNG coverage heatmap."""
    cell_px = 14
    pad_left = 70
    pad_top = 60
    pad_right = 240
    pad_bottom = 60
    map_w = cols * cell_px
    map_h = rows * cell_px

    # Build 2 grids: left = building footprint area, right = road density + sparse flags
    gap = 50
    total_w = pad_left + map_w * 2 + gap + pad_right
    total_h = pad_top + map_h + pad_bottom

    rgb = np.full((total_h, total_w, 3), 248, dtype=np.uint8)

    bldg_grid = np.zeros((rows, cols), dtype=np.float64)
    road_grid = np.zeros((rows, cols), dtype=np.float64)
    sparse_grid = np.zeros((rows, cols), dtype=bool)

    for _, row in grid_df.iterrows():
        r = int(row["row"])
        c = int(row["col"])
        if 0 <= r < rows and 0 <= c < cols:
            bldg_grid[r, c] = row["building_footprint_m2"]
            road_grid[r, c] = row["road_km_per_km2"]
            sparse_grid[r, c] = bool(row["sparse_flag"])

    max_bldg = max(1.0, float(np.percentile(bldg_grid[bldg_grid > 0], 98)) if np.any(bldg_grid > 0) else 1.0)
    max_road = max(1.0, float(np.percentile(road_grid[road_grid > 0], 98)) if np.any(road_grid > 0) else 1.0)

    svg_rects_bldg = []
    svg_rects_road = []

    for r in range(rows):
        # Row 0 is south; flip vertically so North is at top
        y0 = pad_top + (rows - 1 - r) * cell_px
        for c in range(cols):
            x0_left = pad_left + c * cell_px
            x0_right = pad_left + map_w + gap + c * cell_px

            # Building color ramp (dark navy -> teal -> yellow)
            b_val = min(1.0, math.log1p(bldg_grid[r, c]) / math.log1p(max_bldg)) if bldg_grid[r, c] > 0 else 0.0
            if bldg_grid[r, c] == 0:
                br, bg, bb = 232, 236, 240
            else:
                br = int(20 + 230 * (b_val**1.3))
                bg = int(45 + 195 * b_val)
                bb = int(110 + 60 * (1.0 - b_val))

            if sparse_grid[r, c]:
                # Highlight sparse urban cells with a red border/tint
                br, bg, bb = 225, 75, 75

            rgb[y0 : y0 + cell_px - 1, x0_left : x0_left + cell_px - 1] = (br, bg, bb)
            svg_rects_bldg.append(
                f'<rect x="{x0_left}" y="{y0}" width="{cell_px-1}" height="{cell_px-1}" fill="rgb({br},{bg},{bb})"/>'
            )

            # Road density color ramp (light grey -> blue -> orange)
            rd_val = min(1.0, road_grid[r, c] / max_road) if road_grid[r, c] > 0 else 0.0
            if road_grid[r, c] == 0:
                rr, rg, rb = 232, 236, 240
            else:
                rr = int(35 + 215 * rd_val)
                rg = int(80 + 110 * (1.0 - abs(rd_val - 0.5)))
                rb = int(160 * (1.0 - rd_val) + 30)

            rgb[y0 : y0 + cell_px - 1, x0_right : x0_right + cell_px - 1] = (rr, rg, rb)
            svg_rects_road.append(
                f'<rect x="{x0_right}" y="{y0}" width="{cell_px-1}" height="{cell_px-1}" fill="rgb({rr},{rg},{rb})"/>'
            )

    write_png_rgb(png_path, rgb)

    svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="{total_h}" viewBox="0 0 {total_w} {total_h}">
  <rect width="100%" height="100%" fill="#f8f9fa"/>
  <text x="{pad_left}" y="30" font-family="sans-serif" font-size="18" font-weight="bold" fill="#1e293b">
    {city_name} — Phase 2 OSM Coverage Report ({bbox['south']}°N–{bbox['north']}°N, {bbox['west']}°E–{bbox['east']}°E)
  </text>
  <text x="{pad_left}" y="50" font-family="sans-serif" font-size="13" fill="#475569">
    Left: Building Footprint Density (log scale; red = sparse flag) | Right: Road Network Density (km/km²)
  </text>
  <g id="buildings-grid">
    {''.join(svg_rects_bldg)}
  </g>
  <g id="roads-grid">
    {''.join(svg_rects_road)}
  </g>
  <g transform="translate({pad_left + map_w * 2 + gap + 20}, {pad_top})" font-family="sans-serif" font-size="12" fill="#334155">
    <text x="0" y="20" font-weight="bold">Legend</text>
    <rect x="0" y="32" width="16" height="16" fill="rgb(245,235,55)"/>
    <text x="24" y="45">High Building Density</text>
    <rect x="0" y="56" width="16" height="16" fill="rgb(60,120,145)"/>
    <text x="24" y="69">Moderate Building Density</text>
    <rect x="0" y="80" width="16" height="16" fill="rgb(232,236,240)"/>
    <text x="24" y="93">Zero Buildings / Rural</text>
    <rect x="0" y="104" width="16" height="16" fill="rgb(225,75,75)"/>
    <text x="24" y="117">Flagged Sparse Cell</text>
    <rect x="0" y="140" width="16" height="16" fill="rgb(250,135,30)"/>
    <text x="24" y="153">High Road Density</text>
    <rect x="0" y="164" width="16" height="16" fill="rgb(65,120,150)"/>
    <text x="24" y="177">Moderate Road Density</text>
  </g>
</svg>
"""
    svg_path.write_text(svg_content, encoding="utf-8")


def parse_dense_node_tags(keys_vals: list[int], string_table: list[str], count: int) -> list[dict[str, str]]:
    """Unpack the flat `keys_vals` array (0-delimited per node) into a list of tag dicts."""
    if not keys_vals:
        return [{} for _ in range(count)]
    res: list[dict[str, str]] = []
    pos = 0
    n = len(keys_vals)
    st_len = len(string_table)
    for _ in range(count):
        tags: dict[str, str] = {}
        while pos < n:
            k_idx = keys_vals[pos]
            pos += 1
            if k_idx == 0:
                break
            if pos < n:
                v_idx = keys_vals[pos]
                pos += 1
                if k_idx < st_len and v_idx < st_len:
                    tags[string_table[k_idx]] = string_table[v_idx]
        res.append(tags)
    while len(res) < count:
        res.append({})
    return res


def classify_road_class(highway: str) -> str | None:
    if highway in ("motorway", "motorway_link", "trunk", "trunk_link"):
        return "highway"
    if highway in ("primary", "primary_link"):
        return "major"
    if highway in ("secondary", "secondary_link", "tertiary", "tertiary_link"):
        return "medium"
    if highway in ("residential", "unclassified", "living_street", "service", "road"):
        return "minor"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2: Extract OSM features and produce coverage report.")
    parser.add_argument("--city", type=Path, default=Path("cities/blr.yaml"), help="Path to city YAML config")
    args = parser.parse_args()

    t_start = time.time()
    repo_root = Path(__file__).resolve().parent.parent
    city_path = args.city if args.city.is_absolute() else (repo_root / args.city)
    with open(city_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    city_code = cfg["code"].lower()
    city_name = cfg["name"]
    bbox = cfg["bbox"]
    south, north = float(bbox["south"]), float(bbox["north"])
    west, east = float(bbox["west"]), float(bbox["east"])
    margin = float(cfg.get("osm_extract", {}).get("bbox_margin_deg", 0.05))
    ext_south, ext_north = south - margin, north + margin
    ext_west, ext_east = west - margin, east + margin

    mid_lat = 0.5 * (south + north)
    cos_lat = math.cos(math.radians(mid_lat))
    m_per_deg_lat = 111132.0
    m_per_deg_lon = 111320.0 * cos_lat

    grid_cell_m = float(cfg["params"].get("grid_cell_m", 1000))
    dlat = grid_cell_m / m_per_deg_lat
    dlon = grid_cell_m / m_per_deg_lon
    rows = int(math.ceil((north - south) / dlat))
    cols = int(math.ceil((east - west) / dlon))
    cell_area_km2 = (grid_cell_m / 1000.0) ** 2

    clipped_pbf = repo_root / "data" / "raw" / f"{city_code}_clipped.osm.pbf"
    if not clipped_pbf.is_file():
        raise FileNotFoundError(f"Clipped OSM PBF not found: {clipped_pbf}. Run Phase 1 first.")

    processed_dir = repo_root / "data" / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    reports_dir = repo_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    print(f"[Phase 2] Pass 1: Reading nodes & POIs from {clipped_pbf.name} ...", flush=True)
    t0 = time.time()
    # Store coordinates inside expanded bbox (ext_south..ext_north, ext_west..ext_east)
    node_coords: dict[int, tuple[float, float]] = {}
    pois_records: list[dict] = []

    for offset, btype, _, payload in iter_pbf_blobs(clipped_pbf):
        if btype != "OSMData":
            continue
        st, gran, lat_off, lon_off, groups = parse_primitive_block(payload)
        for grp in groups:
            t, gpos = read_varint(grp, 0)
            if (t >> 3) != 2:  # Only DenseNodes in Pass 1
                continue
            length, gpos = read_varint(grp, gpos)
            ids, lats, lons, keys_vals = parse_dense_nodes(grp[gpos : gpos + length], gran, lat_off, lon_off)
            has_any_in_ext = False
            for nid, lat, lon in zip(ids, lats, lons):
                if ext_south <= lat <= ext_north and ext_west <= lon <= ext_east:
                    node_coords[nid] = (lon, lat)
                    has_any_in_ext = True

            if has_any_in_ext and keys_vals:
                node_tags_list = parse_dense_node_tags(keys_vals, st, len(ids))
                for nid, lat, lon, tags in zip(ids, lats, lons, node_tags_list):
                    if not tags or not (south <= lat <= north and west <= lon <= east):
                        continue
                    name = tags.get("name") or tags.get("name:en")
                    place = tags.get("place")
                    office = tags.get("office")
                    amenity = tags.get("amenity")
                    shop = tags.get("shop")
                    industrial = tags.get("industrial")
                    railway = tags.get("railway")
                    if name and (place or office or amenity or shop or industrial or railway):
                        pois_records.append(
                            {
                                "osm_id": int(nid),
                                "osm_type": "node",
                                "lon": round(lon, 7),
                                "lat": round(lat, 7),
                                "name": name,
                                "place": place or "",
                                "office": office or "",
                                "amenity": amenity or "",
                                "shop": shop or "",
                                "industrial": industrial or "",
                                "railway": railway or "",
                            }
                        )

    print(
        f"[Phase 2] Pass 1 complete in {time.time() - t0:.2f}s: {len(node_coords):,} nodes cached, {len(pois_records):,} POI nodes found.",
        flush=True,
    )

    print(f"[Phase 2] Pass 2: Extracting ways (buildings, roads, landuse, airports, water, parks, railways)...", flush=True)
    t1 = time.time()

    buildings_records: list[dict] = []
    roads_records: list[dict] = []
    landuse_records: list[dict] = []
    airports_records: list[dict] = []
    water_records: list[dict] = []
    parks_records: list[dict] = []
    railways_records: list[dict] = []

    # Per-cell accumulators
    cell_bldg_count = np.zeros((rows, cols), dtype=np.int64)
    cell_bldg_with_levels_or_height = np.zeros((rows, cols), dtype=np.int64)
    cell_bldg_area_m2 = np.zeros((rows, cols), dtype=np.float64)
    cell_road_length_m = np.zeros((rows, cols), dtype=np.float64)
    cell_emp_landuse_m2 = np.zeros((rows, cols), dtype=np.float64)

    for offset, btype, _, payload in iter_pbf_blobs(clipped_pbf):
        if btype != "OSMData":
            continue
        st, _, _, _, groups = parse_primitive_block(payload)
        for grp in groups:
            t_check, _ = read_varint(grp, 0)
            if (t_check >> 3) != 3:  # Only Ways in Pass 2
                continue
            gpos = 0
            gn = len(grp)
            while gpos < gn:
                tag, gpos = read_varint(grp, gpos)
                fnum = tag >> 3
                wtype = tag & 0x07
                if fnum == 3 and wtype == 2:
                    length, gpos = read_varint(grp, gpos)
                    wid, tags, refs = parse_way(grp[gpos : gpos + length], st)
                    gpos += length
                    if not tags or len(refs) < 2:
                        continue

                    # Resolve coordinates that exist in node_coords
                    coords = [node_coords[r] for r in refs if r in node_coords]
                    if len(coords) < 2:
                        continue

                    lons = [pt[0] for pt in coords]
                    lats = [pt[1] for pt in coords]
                    min_lon, max_lon = min(lons), max(lons)
                    min_lat, max_lat = min(lats), max(lats)
                    cx = 0.5 * (min_lon + max_lon)
                    cy = 0.5 * (min_lat + max_lat)

                    in_study_bbox = (south <= cy <= north) and (west <= cx <= east)
                    in_routing_bbox = (ext_south <= cy <= ext_north) and (ext_west <= cx <= ext_east)
                    if not in_routing_bbox:
                        continue

                    # 1. Buildings (inside study bbox)
                    bldg_tag = tags.get("building")
                    if bldg_tag and bldg_tag != "no" and in_study_bbox and len(coords) >= 3:
                        if coords[0] != coords[-1]:
                            coords = coords + [coords[0]]
                        area_m2 = polygon_area_m2(coords, cos_lat)
                        if area_m2 >= 4.0:
                            levels = parse_float_prefix(tags.get("building:levels"))
                            height = parse_float_prefix(tags.get("height"))
                            has_lh = (levels is not None) or (height is not None)
                            name = tags.get("name") or tags.get("name:en") or ""
                            amenity = tags.get("amenity") or ""
                            shop = tags.get("shop") or ""
                            office = tags.get("office") or ""

                            r_idx = min(rows - 1, max(0, int((cy - south) / dlat)))
                            c_idx = min(cols - 1, max(0, int((cx - west) / dlon)))
                            cell_bldg_count[r_idx, c_idx] += 1
                            if has_lh:
                                cell_bldg_with_levels_or_height[r_idx, c_idx] += 1
                            cell_bldg_area_m2[r_idx, c_idx] += area_m2

                            buildings_records.append(
                                {
                                    "osm_id": int(wid),
                                    "building": bldg_tag,
                                    "levels": float(levels) if levels is not None else np.nan,
                                    "height": float(height) if height is not None else np.nan,
                                    "name": name,
                                    "amenity": amenity,
                                    "shop": shop,
                                    "office": office,
                                    "area_m2": round(area_m2, 2),
                                    "min_lon": round(min_lon, 7),
                                    "min_lat": round(min_lat, 7),
                                    "max_lon": round(max_lon, 7),
                                    "max_lat": round(max_lat, 7),
                                    "centroid_lon": round(cx, 7),
                                    "centroid_lat": round(cy, 7),
                                    "row": r_idx,
                                    "col": c_idx,
                                    "coords_json": json.dumps([[round(p[0], 6), round(p[1], 6)] for p in coords]),
                                }
                            )
                            if name and (office or shop or amenity or bldg_tag in ("commercial", "office", "industrial", "retail", "hospital", "university", "college")):
                                pois_records.append(
                                    {
                                        "osm_id": int(wid),
                                        "osm_type": "way",
                                        "lon": round(cx, 7),
                                        "lat": round(cy, 7),
                                        "name": name,
                                        "place": "",
                                        "office": office,
                                        "amenity": amenity,
                                        "shop": shop,
                                        "industrial": bldg_tag if bldg_tag == "industrial" else "",
                                        "railway": "",
                                    }
                                )

                    # 2. Highways (keep across expanded routing bbox for Phase 5 OSRM/routing, flag `in_study_bbox` for Phase 7)
                    hw_tag = tags.get("highway")
                    if hw_tag:
                        road_class = classify_road_class(hw_tag)
                        if road_class is not None:
                            length_m = polyline_length_m(coords, cos_lat)
                            if length_m > 0:
                                bridge_tag = tags.get("bridge")
                                tunnel_tag = tags.get("tunnel")
                                if bridge_tag and bridge_tag != "no":
                                    structure = "bridge"
                                elif tunnel_tag and tunnel_tag != "no":
                                    structure = "tunnel"
                                else:
                                    structure = "normal"

                                if in_study_bbox:
                                    r_idx = min(rows - 1, max(0, int((cy - south) / dlat)))
                                    c_idx = min(cols - 1, max(0, int((cx - west) / dlon)))
                                    cell_road_length_m[r_idx, c_idx] += length_m
                                else:
                                    r_idx, c_idx = -1, -1

                                roads_records.append(
                                    {
                                        "osm_id": int(wid),
                                        "highway": hw_tag,
                                        "roadClass": road_class,
                                        "structure": structure,
                                        "name": tags.get("name") or tags.get("name:en") or tags.get("ref") or "",
                                        "oneway": tags.get("oneway") or "",
                                        "maxspeed": tags.get("maxspeed") or "",
                                        "length_m": round(length_m, 2),
                                        "in_study_bbox": in_study_bbox,
                                        "row": r_idx,
                                        "col": c_idx,
                                        "coords_json": json.dumps([[round(p[0], 6), round(p[1], 6)] for p in coords]),
                                    }
                                )

                    if not in_study_bbox:
                        continue

                    # 3. Airports (aeroway=runway, taxiway, apron, aerodrome, terminal)
                    aero_tag = tags.get("aeroway")
                    if aero_tag in ("runway", "taxiway", "apron", "aerodrome", "terminal", "helipad"):
                        is_poly = coords[0] == coords[-1] or aero_tag in ("apron", "aerodrome", "terminal")
                        poly_coords = coords if coords[0] == coords[-1] else coords + [coords[0]]
                        area_m2 = polygon_area_m2(poly_coords, cos_lat) if len(poly_coords) >= 4 else 0.0
                        airports_records.append(
                            {
                                "osm_id": int(wid),
                                "aeroway": aero_tag,
                                "name": tags.get("name") or tags.get("name:en") or "",
                                "ref": tags.get("ref") or "",
                                "is_polygon": is_poly,
                                "area_m2": round(area_m2, 2),
                                "centroid_lon": round(cx, 7),
                                "centroid_lat": round(cy, 7),
                                "coords_json": json.dumps([[round(p[0], 6), round(p[1], 6)] for p in coords]),
                            }
                        )

                    # 4. Parks & Green spaces & Forests / Reserves / Mangroves
                    leisure_tag = tags.get("leisure")
                    lu_tag = tags.get("landuse")
                    nat_tag = tags.get("natural")
                    bnd_tag = tags.get("boundary")
                    wet_tag = tags.get("wetland")
                    if (
                        leisure_tag in ("park", "garden", "nature_reserve", "golf_course", "pitch", "stadium", "playground")
                        or lu_tag in ("recreation_ground", "grass", "meadow", "forest", "village_green")
                        or nat_tag in ("wood", "scrub", "wetland")
                        or bnd_tag in ("national_park", "protected_area")
                        or wet_tag == "mangrove"
                    ) and len(coords) >= 3:
                        poly_coords = coords if coords[0] == coords[-1] else coords + [coords[0]]
                        area_m2 = polygon_area_m2(poly_coords, cos_lat)
                        if area_m2 >= 50.0:
                            parks_records.append(
                                {
                                    "osm_id": int(wid),
                                    "kind": leisure_tag or lu_tag or nat_tag or bnd_tag or "park",
                                    "name": tags.get("name") or tags.get("name:en") or "",
                                    "area_m2": round(area_m2, 2),
                                    "centroid_lon": round(cx, 7),
                                    "centroid_lat": round(cy, 7),
                                    "coords_json": json.dumps([[round(p[0], 6), round(p[1], 6)] for p in poly_coords]),
                                }
                            )

                    # 5. Land use & Institutional campuses (commercial, industrial, retail, residential, college/university/hospital)
                    amenity_tag = tags.get("amenity")
                    if (
                        lu_tag in ("commercial", "industrial", "retail", "residential", "institutional", "education", "railway", "military")
                        or amenity_tag in ("university", "college", "school", "hospital", "research_institute")
                    ) and len(coords) >= 3:
                        poly_coords = coords if coords[0] == coords[-1] else coords + [coords[0]]
                        area_m2 = polygon_area_m2(poly_coords, cos_lat)
                        if area_m2 >= 50.0:
                            category = amenity_tag if amenity_tag in ("university", "college", "school", "hospital", "research_institute") else (lu_tag or "")
                            r_idx = min(rows - 1, max(0, int((cy - south) / dlat)))
                            c_idx = min(cols - 1, max(0, int((cx - west) / dlon)))
                            if category in ("commercial", "industrial", "retail", "institutional", "education", "university", "college", "hospital", "research_institute"):
                                cell_emp_landuse_m2[r_idx, c_idx] += area_m2
                            landuse_records.append(
                                {
                                    "osm_id": int(wid),
                                    "category": category,
                                    "name": tags.get("name") or tags.get("name:en") or "",
                                    "area_m2": round(area_m2, 2),
                                    "centroid_lon": round(cx, 7),
                                    "centroid_lat": round(cy, 7),
                                    "row": r_idx,
                                    "col": c_idx,
                                    "coords_json": json.dumps([[round(p[0], 6), round(p[1], 6)] for p in poly_coords]),
                                }
                            )

                    # 6. Water bodies & waterways
                    nat_tag = tags.get("natural")
                    ww_tag = tags.get("waterway")
                    w_tag = tags.get("water")
                    if nat_tag == "water" or ww_tag in ("river", "canal", "stream", "drain", "riverbank") or w_tag:
                        is_poly = (nat_tag == "water" or ww_tag == "riverbank" or bool(w_tag)) and len(coords) >= 3
                        poly_coords = (coords if coords[0] == coords[-1] else coords + [coords[0]]) if is_poly else coords
                        area_m2 = polygon_area_m2(poly_coords, cos_lat) if is_poly else 0.0
                        water_records.append(
                            {
                                "osm_id": int(wid),
                                "water_type": w_tag or ww_tag or nat_tag or "water",
                                "name": tags.get("name") or tags.get("name:en") or "",
                                "is_polygon": is_poly,
                                "area_m2": round(area_m2, 2),
                                "centroid_lon": round(cx, 7),
                                "centroid_lat": round(cy, 7),
                                "coords_json": json.dumps([[round(p[0], 6), round(p[1], 6)] for p in poly_coords]),
                            }
                        )

                    # 7. Railways
                    rw_tag = tags.get("railway")
                    if rw_tag in ("rail", "subway", "light_rail", "monorail", "tram", "construction", "station"):
                        railways_records.append(
                            {
                                "osm_id": int(wid),
                                "railway": rw_tag,
                                "name": tags.get("name") or tags.get("name:en") or "",
                                "length_m": round(polyline_length_m(coords, cos_lat), 2),
                                "coords_json": json.dumps([[round(p[0], 6), round(p[1], 6)] for p in coords]),
                            }
                        )
                else:
                    gpos = skip_field(grp, gpos, wtype)

    print(
        f"[Phase 2] Pass 2 complete in {time.time() - t1:.2f}s: "
        f"buildings={len(buildings_records):,}, roads={len(roads_records):,}, "
        f"landuse={len(landuse_records):,}, airports={len(airports_records):,}, "
        f"water={len(water_records):,}, parks={len(parks_records):,}, "
        f"railways={len(railways_records):,}, pois={len(pois_records):,}",
        flush=True,
    )

    # Save Parquet tables
    datasets = {
        "buildings": buildings_records,
        "roads": roads_records,
        "landuse": landuse_records,
        "airports": airports_records,
        "water": water_records,
        "parks": parks_records,
        "railways": railways_records,
        "pois": pois_records,
    }
    output_files = {}
    for name, records in datasets.items():
        out_path = processed_dir / f"{city_code}_{name}.parquet"
        df = pd.DataFrame(records)
        df.to_parquet(out_path, index=False)
        output_files[name] = {
            "path": str(out_path.relative_to(repo_root)),
            "count": len(df),
            "size_bytes": out_path.stat().st_size,
        }

    # Build per-grid-cell coverage report
    grid_rows = []
    sparse_cells_count = 0
    urban_road_cells_count = 0
    for r in range(rows):
        cell_lat = south + (r + 0.5) * dlat
        for c in range(cols):
            cell_lon = west + (c + 0.5) * dlon
            b_cnt = int(cell_bldg_count[r, c])
            b_lh = int(cell_bldg_with_levels_or_height[r, c])
            b_area = float(cell_bldg_area_m2[r, c])
            rd_len_m = float(cell_road_length_m[r, c])
            rd_km_per_km2 = (rd_len_m / 1000.0) / cell_area_km2
            lh_share = (b_lh / b_cnt) if b_cnt > 0 else 0.0

            # Flag cells where road density indicates built-up urban activity (>= 4.0 km/km²)
            # but building footprint coverage is sparse (< 25 buildings or < 2,500 m² footprint)
            is_urban_road = rd_km_per_km2 >= 4.0
            if is_urban_road:
                urban_road_cells_count += 1
            is_sparse = is_urban_road and (b_cnt < 25 or b_area < 2500.0)
            if is_sparse:
                sparse_cells_count += 1

            grid_rows.append(
                {
                    "cell_id": f"R{r:03d}_C{c:03d}",
                    "row": r,
                    "col": c,
                    "center_lon": round(cell_lon, 6),
                    "center_lat": round(cell_lat, 6),
                    "building_count": b_cnt,
                    "buildings_with_levels_or_height": b_lh,
                    "share_with_levels_or_height": round(lh_share, 4),
                    "building_footprint_m2": round(b_area, 2),
                    "employment_landuse_m2": round(float(cell_emp_landuse_m2[r, c]), 2),
                    "road_length_m": round(rd_len_m, 2),
                    "road_km_per_km2": round(rd_km_per_km2, 3),
                    "sparse_flag": is_sparse,
                }
            )

    grid_df = pd.DataFrame(grid_rows)
    csv_path = reports_dir / "20_coverage_grid.csv"
    grid_df.to_csv(csv_path, index=False)

    svg_path = reports_dir / "20_coverage_map.svg"
    png_path = reports_dir / "20_coverage_map.png"
    render_coverage_images(grid_df, rows, cols, bbox, svg_path, png_path, city_name)

    total_bldg = int(cell_bldg_count.sum())
    total_lh = int(cell_bldg_with_levels_or_height.sum())
    total_bldg_area_km2 = float(cell_bldg_area_m2.sum()) / 1e6
    study_roads_len_km = float(cell_road_length_m.sum()) / 1000.0
    total_study_area_km2 = rows * cols * cell_area_km2

    summary = {
        "phase": "20_extract_osm",
        "city_code": cfg["code"],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "runtime_seconds": round(time.time() - t_start, 2),
        "grid_spec": {
            "grid_cell_m": grid_cell_m,
            "rows": rows,
            "cols": cols,
            "total_cells": rows * cols,
            "total_area_km2": round(total_study_area_km2, 2),
        },
        "counts": {
            "buildings_in_study_bbox": total_bldg,
            "buildings_with_levels_or_height": total_lh,
            "share_buildings_with_levels_or_height": round(total_lh / max(1, total_bldg), 4),
            "total_building_footprint_km2": round(total_bldg_area_km2, 3),
            "roads_total_extracted": len(roads_records),
            "roads_in_study_bbox": int(sum(1 for r in roads_records if r["in_study_bbox"])),
            "total_road_length_km_in_study_bbox": round(study_roads_len_km, 2),
            "mean_road_km_per_km2": round(study_roads_len_km / max(1.0, total_study_area_km2), 3),
            "landuse_polygons": len(landuse_records),
            "airport_features": len(airports_records),
            "water_features": len(water_records),
            "park_features": len(parks_records),
            "railway_features": len(railways_records),
            "named_pois": len(pois_records),
        },
        "coverage_evaluation": {
            "urban_road_cells_count": urban_road_cells_count,
            "sparse_flagged_cells_count": sparse_cells_count,
            "sparse_share_of_urban_cells": round(sparse_cells_count / max(1, urban_road_cells_count), 4),
        },
        "outputs": {
            "coverage_csv": str(csv_path.relative_to(repo_root)),
            "coverage_map_png": str(png_path.relative_to(repo_root)),
            "coverage_map_svg": str(svg_path.relative_to(repo_root)),
            "parquet_tables": output_files,
        },
    }

    report_json_path = reports_dir / "20_extract_osm.json"
    with open(report_json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("=== Phase 2 Coverage Summary ===")
    print(f"Buildings in study box:    {total_bldg:,} ({total_bldg_area_km2:.2f} km² footprint)")
    print(f"With levels/height tags:   {total_lh:,} ({summary['counts']['share_buildings_with_levels_or_height']*100:.2f}%)")
    print(f"Road network (study box):  {summary['counts']['roads_in_study_bbox']:,} ways ({study_roads_len_km:,.1f} km, mean {summary['counts']['mean_road_km_per_km2']:.2f} km/km²)")
    print(f"Airports / Runways:        {len(airports_records):,}")
    print(f"Parks:                     {len(parks_records):,} | Water: {len(water_records):,} | Railways: {len(railways_records):,}")
    print(f"Urban cells:               {urban_road_cells_count:,} | Flagged sparse cells: {sparse_cells_count:,} ({summary['coverage_evaluation']['sparse_share_of_urban_cells']*100:.1f}%)")
    print(f"Saved CSV report:          {csv_path}")
    print(f"Saved Map images:          {png_path} & {svg_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
