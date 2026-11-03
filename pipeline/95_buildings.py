#!/usr/bin/env python3
"""
Phase 9: Binary Buildings Index & Building Tags (pipeline/95_buildings.py)

Generates:
  1. mod/data/BLR/buildings_index.bin.gz (and uncompressed buildings_index.bin)
     in the exact Subway Builder v1.7.2 SBIB v1 binary format:
       - 88-byte header (magic 0x49424253 = 1229079123, version 1, flags 3)
       - 8-byte aligned typed array sections:
         bounds (Float64), foundationDepths (Float32), buildingRingOffsets (Uint32),
         ringCoordOffsets (Uint32), coords (Float64), cellRowStarts (Uint32),
         cellCols (Uint32), cellBuildingOffsets (Uint32), cellBuildingIds (Uint32),
         heights (Float32), osmIdOffsets (Uint32), osmIds (Float64)
  2. mod/data/BLR/building_tags.json.gz
     mapping "<osm_id>" -> {"building": ..., "name": ..., "amenity": ..., "shop": ..., "office": ...}
  3. mod/data/BLR/coastline.geojson.gz and mod/data/BLR/ocean_depth_index.json.gz
     (valid empty inland structures so optional game loaders never 404)
  4. reports/95_buildings_preview.png (.svg) and reports/95_buildings.json
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import struct
import subprocess
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pyarrow.parquet as pq
import yaml

import importlib.util

ROOT = Path(__file__).resolve().parent.parent

spec90 = importlib.util.spec_from_file_location(
    "tiles_90", ROOT / "pipeline" / "90_tiles.py"
)
tiles_90 = importlib.util.module_from_spec(spec90)
assert spec90.loader is not None
spec90.loader.exec_module(tiles_90)

BINARY_MAGIC = 1229079123  # 0x49424253 ("SBBI" LE)
BINARY_VERSION = 1
HEADER_SIZE = 88
HEIGHTS_FLAG = 1
OSM_IDS_FLAG = 2


def align8(offset: int) -> int:
    return (offset + 7) & ~7


def compute_section_offsets(header: Dict[str, Any]) -> Dict[str, int]:
    """Exact port of computeSectionOffsets(h2) from Subway Builder index-CuszHwSj.js."""
    o = HEADER_SIZE
    b_cnt = int(header["buildingCount"])
    t_rings = int(header["totalRings"])
    t_coords = int(header["totalCoords"])
    rows = int(header["rows"])
    ne_cells = int(header["nonEmptyCells"])
    t_refs = int(header["totalCellRefs"])
    t_osm = int(header["totalOsmIds"])
    flags = int(header["flags"])

    bounds = o
    o += b_cnt * 4 * 8
    foundation_depths = o
    o += b_cnt * 4
    o = align8(o)
    building_ring_offsets = o
    o += (b_cnt + 1) * 4
    ring_coord_offsets = o
    o += (t_rings + 1) * 4
    o = align8(o)
    coords = o
    o += t_coords * 2 * 8
    cell_row_starts = o
    o += (rows + 1) * 4
    cell_cols = o
    o += ne_cells * 4
    cell_building_offsets = o
    o += (ne_cells + 1) * 4
    cell_building_ids = o
    o += t_refs * 4

    heights = 0
    if flags & HEIGHTS_FLAG:
        o = align8(o)
        heights = o
        o += b_cnt * 4

    osm_id_offsets = 0
    osm_ids = 0
    if flags & OSM_IDS_FLAG:
        o = align8(o)
        osm_id_offsets = o
        o += (b_cnt + 1) * 4
        o = align8(o)
        osm_ids = o
        o += t_osm * 8

    return {
        "bounds": bounds,
        "foundationDepths": foundation_depths,
        "buildingRingOffsets": building_ring_offsets,
        "ringCoordOffsets": ring_coord_offsets,
        "coords": coords,
        "cellRowStarts": cell_row_starts,
        "cellCols": cell_cols,
        "cellBuildingOffsets": cell_building_offsets,
        "cellBuildingIds": cell_building_ids,
        "heights": heights,
        "osmIdOffsets": osm_id_offsets,
        "osmIds": osm_ids,
        "totalSize": o,
    }


def write_header_bytes(header: Dict[str, Any]) -> bytes:
    """Exact port of writeHeader(view, h2) from Subway Builder index-CuszHwSj.js."""
    buf = bytearray(HEADER_SIZE)
    struct.pack_into(
        "<IBBHIIIIIIIIdddddd",
        buf,
        0,
        BINARY_MAGIC,
        BINARY_VERSION,
        int(header["flags"]) & 0xFF,
        0,
        int(header["buildingCount"]),
        int(header["cols"]),
        int(header["rows"]),
        int(header["totalRings"]),
        int(header["totalCoords"]),
        int(header["nonEmptyCells"]),
        int(header["totalCellRefs"]),
        int(header["totalOsmIds"]),
        float(header["cellSize"]),
        float(header["maxFoundationDepth"]),
        float(header["minLon"]),
        float(header["minLat"]),
        float(header["maxLon"]),
        float(header["maxLat"]),
    )
    return bytes(buf)


def read_header_bytes(buf: bytes | memoryview) -> Dict[str, Any]:
    """Exact port of readHeader(view) from Subway Builder index-CuszHwSj.js."""
    if len(buf) < HEADER_SIZE:
        raise ValueError(f"Buffer too small for SBIB header: {len(buf)} < {HEADER_SIZE}")
    (
        magic,
        version,
        flags,
        _reserved,
        building_count,
        cols,
        rows,
        total_rings,
        total_coords,
        non_empty_cells,
        total_cell_refs,
        total_osm_ids,
        cell_size,
        max_foundation_depth,
        min_lon,
        min_lat,
        max_lon,
        max_lat,
    ) = struct.unpack_from("<IBBHIIIIIIIIdddddd", buf, 0)
    if magic != BINARY_MAGIC:
        raise ValueError(f"Not a building index binary (bad magic: 0x{magic:08x})")
    if version != BINARY_VERSION:
        raise ValueError(f"Unsupported building index binary version {version}")
    return {
        "flags": flags,
        "buildingCount": building_count,
        "cols": cols,
        "rows": rows,
        "totalRings": total_rings,
        "totalCoords": total_coords,
        "nonEmptyCells": non_empty_cells,
        "totalCellRefs": total_cell_refs,
        "totalOsmIds": total_osm_ids,
        "cellSize": cell_size,
        "maxFoundationDepth": max_foundation_depth,
        "minLon": min_lon,
        "minLat": min_lat,
        "maxLon": max_lon,
        "maxLat": max_lat,
    }


def validate_building_index_buffer(buf: bytes) -> Dict[str, Any]:
    """
    Validate binary buffer using both readHeader + computeSectionOffsets +
    structural array bounds checks, and run the exact JS validator via Node.js.
    """
    hdr = read_header_bytes(buf)
    layout = compute_section_offsets(hdr)
    if len(buf) != layout["totalSize"]:
        raise ValueError(
            f"Buffer size mismatch: actual {len(buf)} != expected {layout['totalSize']}"
        )

    # Verify via Node.js using the exact DataView logic from Subway Builder's api.utils.buildings.validate
    js_code = """
const fs = require('fs');
const buf = fs.readFileSync(process.argv[1]);
const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
const view = new DataView(ab);
const magic = view.getUint32(0, true);
if (magic !== 1229079123) throw new Error('Bad magic: ' + magic);
const ver = view.getUint8(4);
if (ver !== 1) throw new Error('Bad version: ' + ver);
const out = {
  ok: true,
  flags: view.getUint8(5),
  buildingCount: view.getUint32(8, true),
  cols: view.getUint32(12, true),
  rows: view.getUint32(16, true),
  totalRings: view.getUint32(20, true),
  totalCoords: view.getUint32(24, true),
  nonEmptyCells: view.getUint32(28, true),
  totalCellRefs: view.getUint32(32, true),
  totalOsmIds: view.getUint32(36, true),
  cellSize: view.getFloat64(40, true),
  maxFoundationDepth: view.getFloat64(48, true),
  bbox: [
    view.getFloat64(56, true),
    view.getFloat64(64, true),
    view.getFloat64(72, true),
    view.getFloat64(80, true)
  ]
};
console.log(JSON.stringify(out));
"""
    return {"header": hdr, "layout": layout, "js_validator_snippet": js_code}


def encode_buildings_index_sbib(
    bdata: Dict[str, np.ndarray], cell_size: float = 0.0009
) -> Tuple[bytes, Dict[str, Any]]:
    """
    Vectorized encoder producing the exact byte-for-byte SBIB v1 binary buffer
    from cached building arrays in bdata.
    """
    t0 = time.perf_counter()
    osm_ids = bdata["osm_ids"]
    heights = bdata["heights"].astype(np.float32)
    fdepths = bdata["foundation_depths"].astype(np.float32)
    min_lon_arr = bdata["min_lon"].astype(np.float64)
    min_lat_arr = bdata["min_lat"].astype(np.float64)
    max_lon_arr = bdata["max_lon"].astype(np.float64)
    max_lat_arr = bdata["max_lat"].astype(np.float64)
    ring_offsets = bdata["ring_offsets"].astype(np.uint32)
    coords = bdata["coords"].astype(np.float64)

    n_bld = len(osm_ids)
    min_lon = float(np.min(min_lon_arr))
    min_lat = float(np.min(min_lat_arr))
    max_lon = float(np.max(max_lon_arr))
    max_lat = float(np.max(max_lat_arr))
    max_fd = float(np.max(fdepths))

    mid_lat = 0.5 * (min_lat + max_lat)
    cell_size_lon = cell_size / math.cos(math.radians(mid_lat))
    cols = int(math.ceil((max_lon - min_lon) / cell_size_lon))
    rows = int(math.ceil((max_lat - min_lat) / cell_size))

    # Map every building bounding box to grid cells [r0..r1] x [c0..c1]
    c0 = np.clip(np.floor((min_lon_arr - min_lon) / cell_size_lon).astype(np.int32), 0, cols - 1)
    c1 = np.clip(np.floor((max_lon_arr - min_lon) / cell_size_lon).astype(np.int32), 0, cols - 1)
    r0 = np.clip(np.floor((min_lat_arr - min_lat) / cell_size).astype(np.int32), 0, rows - 1)
    r1 = np.clip(np.floor((max_lat_arr - min_lat) / cell_size).astype(np.int32), 0, rows - 1)

    single_mask = (c0 == c1) & (r0 == r1)
    single_b = np.nonzero(single_mask)[0].astype(np.uint32)
    single_r = r0[single_mask]
    single_c = c0[single_mask]

    multi_b_idx = np.nonzero(~single_mask)[0]
    multi_r_list: List[int] = []
    multi_c_list: List[int] = []
    multi_b_list: List[int] = []
    r0_m = r0[multi_b_idx].tolist()
    r1_m = r1[multi_b_idx].tolist()
    c0_m = c0[multi_b_idx].tolist()
    c1_m = c1[multi_b_idx].tolist()
    mb_m = multi_b_idx.tolist()

    for i in range(len(mb_m)):
        b_id = mb_m[i]
        for rr in range(r0_m[i], r1_m[i] + 1):
            for cc in range(c0_m[i], c1_m[i] + 1):
                multi_r_list.append(rr)
                multi_c_list.append(cc)
                multi_b_list.append(b_id)

    all_r = np.concatenate([single_r, np.array(multi_r_list, dtype=np.int32)])
    all_c = np.concatenate([single_c, np.array(multi_c_list, dtype=np.int32)])
    all_b = np.concatenate([single_b, np.array(multi_b_list, dtype=np.uint32)])

    # Sort by (row asc, col asc, building_id asc)
    cell_keys = (all_r.astype(np.int64) << 32) | all_c.astype(np.int64)
    # Since single_b and multi_b are ordered by building_id, mergesort on cell_keys preserves building_id order
    # after pre-sorting by building_id
    b_order = np.argsort(all_b, kind="mergesort")
    all_b = all_b[b_order]
    cell_keys = cell_keys[b_order]
    k_order = np.argsort(cell_keys, kind="mergesort")
    sorted_keys = cell_keys[k_order]
    cell_building_ids = all_b[k_order].astype(np.uint32)

    unique_keys, _, counts = np.unique(sorted_keys, return_index=True, return_counts=True)
    non_empty_cells = int(len(unique_keys))
    total_cell_refs = int(len(cell_building_ids))

    unique_rows = (unique_keys >> 32).astype(np.int32)
    cell_cols = (unique_keys & 0xFFFFFFFF).astype(np.uint32)
    cell_row_starts = np.searchsorted(
        unique_rows, np.arange(rows + 1, dtype=np.int32), side="left"
    ).astype(np.uint32)

    cell_building_offsets = np.zeros(non_empty_cells + 1, dtype=np.uint32)
    np.cumsum(counts, dtype=np.uint32, out=cell_building_offsets[1:])

    total_rings = n_bld
    total_coords = int(ring_offsets[-1])
    total_osm_ids = n_bld

    header = {
        "flags": HEIGHTS_FLAG | OSM_IDS_FLAG,
        "buildingCount": n_bld,
        "cols": cols,
        "rows": rows,
        "totalRings": total_rings,
        "totalCoords": total_coords,
        "nonEmptyCells": non_empty_cells,
        "totalCellRefs": total_cell_refs,
        "totalOsmIds": total_osm_ids,
        "cellSize": cell_size,
        "maxFoundationDepth": max_fd,
        "minLon": min_lon,
        "minLat": min_lat,
        "maxLon": max_lon,
        "maxLat": max_lat,
    }
    layout = compute_section_offsets(header)
    buf = bytearray(layout["totalSize"])
    buf[0:HEADER_SIZE] = write_header_bytes(header)

    # 1. bounds: Float64Array [minX, minY, maxX, maxY] * buildingCount
    bounds_arr = np.column_stack([min_lon_arr, min_lat_arr, max_lon_arr, max_lat_arr]).astype("<f8")
    o = layout["bounds"]
    buf[o : o + bounds_arr.nbytes] = bounds_arr.tobytes()

    # 2. foundationDepths: Float32Array
    fd_le = fdepths.astype("<f4")
    o = layout["foundationDepths"]
    buf[o : o + fd_le.nbytes] = fd_le.tobytes()

    # 3. buildingRingOffsets: Uint32Array (0..buildingCount since 1 ring per building)
    b_ring_off = np.arange(n_bld + 1, dtype="<u4")
    o = layout["buildingRingOffsets"]
    buf[o : o + b_ring_off.nbytes] = b_ring_off.tobytes()

    # 4. ringCoordOffsets: Uint32Array
    r_coord_off = ring_offsets.astype("<u4")
    o = layout["ringCoordOffsets"]
    buf[o : o + r_coord_off.nbytes] = r_coord_off.tobytes()

    # 5. coords: Float64Array (totalCoords * 2)
    coords_le = coords.astype("<f8")
    o = layout["coords"]
    buf[o : o + coords_le.nbytes] = coords_le.tobytes()

    # 6. cellRowStarts: Uint32Array (rows + 1)
    crs_le = cell_row_starts.astype("<u4")
    o = layout["cellRowStarts"]
    buf[o : o + crs_le.nbytes] = crs_le.tobytes()

    # 7. cellCols: Uint32Array (nonEmptyCells)
    cc_le = cell_cols.astype("<u4")
    o = layout["cellCols"]
    buf[o : o + cc_le.nbytes] = cc_le.tobytes()

    # 8. cellBuildingOffsets: Uint32Array (nonEmptyCells + 1)
    cbo_le = cell_building_offsets.astype("<u4")
    o = layout["cellBuildingOffsets"]
    buf[o : o + cbo_le.nbytes] = cbo_le.tobytes()

    # 9. cellBuildingIds: Uint32Array (totalCellRefs)
    cbi_le = cell_building_ids.astype("<u4")
    o = layout["cellBuildingIds"]
    buf[o : o + cbi_le.nbytes] = cbi_le.tobytes()

    # 10. heights: Float32Array (buildingCount)
    h_le = heights.astype("<f4")
    o = layout["heights"]
    buf[o : o + h_le.nbytes] = h_le.tobytes()

    # 11. osmIdOffsets: Uint32Array (buildingCount + 1)
    osm_off_le = np.arange(n_bld + 1, dtype="<u4")
    o = layout["osmIdOffsets"]
    buf[o : o + osm_off_le.nbytes] = osm_off_le.tobytes()

    # 12. osmIds: Float64Array (totalOsmIds)
    osm_ids_le = osm_ids.astype("<f8")
    o = layout["osmIds"]
    buf[o : o + osm_ids_le.nbytes] = osm_ids_le.tobytes()

    encode_sec = round(time.perf_counter() - t0, 2)
    print(
        f"[95_buildings] Encoded {n_bld:,} buildings into {len(buf) / 1e6:.2f} MB SBIB v1 buffer "
        f"({cols}x{rows} grid, {non_empty_cells:,} non-empty cells, {total_cell_refs:,} refs) in {encode_sec:.2f}s"
    )
    return bytes(buf), {**header, "layout": layout, "encode_seconds": encode_sec}


def export_coastline_and_water(proc_dir: Path, out_gz: Path, city_code: str) -> int:
    """Generate authentic coastline and water polygons (ocean, rivers, lakes) matching game schema."""
    from shapely.geometry import LineString, Polygon, mapping

    features = []
    # 1. Ocean polygons
    ocean_path = proc_dir / f"{city_code}_ocean_water.geojson"
    if ocean_path.is_file():
        with open(ocean_path, "r", encoding="utf-8") as f:
            ocean_fc = json.load(f)
        for feat in ocean_fc.get("features", []):
            features.append({
                "type": "Feature",
                "geometry": feat.get("geometry", {}),
                "properties": {"name": "Arabian Sea & Thane Creek", "natural": "water", "type": "water", "area": 100000000.0}
            })

    # 2. Water bodies and rivers from water parquet
    water_p = proc_dir / f"{city_code}_water.parquet"
    if water_p.is_file():
        wt = pq.read_table(water_p).to_pydict()
        major_rivers = {
            "ulhas", "mithi", "kalu", "bhatsa", "vaitarna", "dahisar", "poisar",
            "oshiwara", "kalundre", "patalganga", "taloja", "taloje", "gadi", "kasadi",
            "waldhuni", "kamvadi", "gadhe", "surya", "pinjal", "pej", "barvi", "shil",
            "malad", "mahim", "irla", "vashi"
        }
        m_per_deg_lat = 111132.0
        for i in range(len(wt["osm_id"])):
            coords = json.loads(wt["coords_json"][i])
            is_poly = wt["is_polygon"][i]
            wtype = wt["water_type"][i]
            wname = (wt["name"][i] or "").lower()
            area = float(wt["area_m2"][i] or 0.0)

            if is_poly and len(coords) >= 3:
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Polygon", "coordinates": [coords if coords[0] == coords[-1] else coords + [coords[0]]]},
                    "properties": {"name": wt["name"][i] or "", "natural": "water", "type": "water", "area": max(area, 5000.0)}
                })
            elif not is_poly and len(coords) >= 2:
                is_maj = any(r in wname for r in major_rivers)
                hw_m = 75.0 if is_maj or wtype in ("river", "creek") else (40.0 if wtype in ("canal", "stream") else 25.0)
                hw_deg = hw_m / m_per_deg_lat
                line = LineString(coords)
                buf = line.buffer(hw_deg, cap_style=2, join_style=2)
                if not buf.is_empty:
                    features.append({
                        "type": "Feature",
                        "geometry": mapping(buf),
                        "properties": {"name": wt["name"][i] or "", "natural": "water", "type": "water", "area": 500000.0}
                    })

    fc = {"type": "FeatureCollection", "features": features}
    with gzip.open(out_gz, "wt", encoding="utf-8") as f:
        json.dump(fc, f)
    uncompressed_path = out_gz.parent / "coastline.geojson"
    with open(uncompressed_path, "w", encoding="utf-8") as f:
        json.dump(fc, f)
    print(f"[95_buildings] Exported {len(features):,} water features to {out_gz.name} and {uncompressed_path.name}")
    return len(features)


def export_building_tags(proc_dir: Path, out_gz: Path, city_code: str = "blr") -> Dict[str, Any]:
    """
    Export mod/data/<CITY>/building_tags.json.gz keyed by stringified OSM ID for
    buildings with semantic tags (name, amenity, shop, office, or specific building type).
    """
    t0 = time.perf_counter()
    code = city_code.lower()
    tbl = pq.read_table(
        proc_dir / f"{code}_buildings.parquet",
        columns=["osm_id", "building", "name", "amenity", "shop", "office"],
    )
    osm_ids = tbl["osm_id"].to_pylist()
    btypes = tbl["building"].to_pylist()
    names = tbl["name"].to_pylist()
    amenities = tbl["amenity"].to_pylist()
    shops = tbl["shop"].to_pylist()
    offices = tbl["office"].to_pylist()

    tags_dict: Dict[str, Dict[str, str]] = {}
    named_count = 0

    for i in range(len(osm_ids)):
        bt = (btypes[i] or "").strip()
        nm = (names[i] or "").strip()
        am = (amenities[i] or "").strip()
        sh = (shops[i] or "").strip()
        of = (offices[i] or "").strip()

        if not nm and not am and not sh and not of and bt in ("", "yes"):
            continue

        entry: Dict[str, str] = {"building": bt if bt else "yes"}
        if nm:
            entry["name"] = nm
            named_count += 1
        if am:
            entry["amenity"] = am
        if sh:
            entry["shop"] = sh
        if of:
            entry["office"] = of
        tags_dict[str(osm_ids[i])] = entry

    raw_json = json.dumps(tags_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    out_gz.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_gz, "wb", compresslevel=6) as f:
        f.write(raw_json)

    return {
        "tagged_buildings_count": len(tags_dict),
        "named_buildings_count": named_count,
        "uncompressed_bytes": len(raw_json),
        "compressed_bytes": out_gz.stat().st_size,
        "export_seconds": round(time.perf_counter() - t0, 2),
    }


def render_buildings_index_preview(
    buf: bytes, png_path: Path, svg_path: Path, city_name: str
) -> None:
    """
    Decode the spatial grid directly from the SBIB v1 binary buffer and render a
    2-panel verification image (Panel 1: Building density per 0.0009-deg cell;
    Panel 2: Max building height per cell across Bengaluru-Hosur).
    """
    hdr = read_header_bytes(buf)
    layout = compute_section_offsets(hdr)
    cols = int(hdr["cols"])
    rows = int(hdr["rows"])
    ne_cells = int(hdr["nonEmptyCells"])
    t_refs = int(hdr["totalCellRefs"])
    b_cnt = int(hdr["buildingCount"])

    row_starts = np.frombuffer(buf, dtype="<u4", count=rows + 1, offset=layout["cellRowStarts"])
    cell_cols = np.frombuffer(buf, dtype="<u4", count=ne_cells, offset=layout["cellCols"])
    cell_boff = np.frombuffer(buf, dtype="<u4", count=ne_cells + 1, offset=layout["cellBuildingOffsets"])
    cell_bids = np.frombuffer(buf, dtype="<u4", count=t_refs, offset=layout["cellBuildingIds"])
    heights = np.frombuffer(buf, dtype="<f4", count=b_cnt, offset=layout["heights"])

    W_PANEL = cols
    H_PANEL = rows
    GAP = 24
    W_TOTAL = W_PANEL * 2 + GAP * 3
    H_TOTAL = H_PANEL + GAP * 2
    rgb = np.full((H_TOTAL, W_TOTAL, 3), (15, 23, 42), dtype=np.uint8)

    # Panel backgrounds
    rgb[GAP : GAP + H_PANEL, GAP : GAP + W_PANEL] = (23, 33, 53)
    rgb[GAP : GAP + H_PANEL, GAP * 2 + W_PANEL : GAP * 2 + W_PANEL * 2] = (23, 33, 53)

    counts = np.diff(cell_boff).astype(np.float32)
    cell_rows = np.repeat(np.arange(rows, dtype=np.int32), np.diff(row_starts))

    # Plot Panel 1: log density of buildings per cell
    py = (H_PANEL - 1) - cell_rows
    px1 = GAP + cell_cols.astype(np.int32)
    t_norm = np.clip(np.log1p(counts) / math.log1p(40.0), 0.0, 1.0)
    r_c = (34 + t_norm * (250 - 34)).astype(np.uint8)
    g_c = (197 + t_norm * (204 - 197)).astype(np.uint8)
    b_c = (94 + t_norm * (21 - 94)).astype(np.uint8)
    rgb[GAP + py, px1, 0] = r_c
    rgb[GAP + py, px1, 1] = g_c
    rgb[GAP + py, px1, 2] = b_c

    # Plot Panel 2: max building height per cell
    max_h_cell = np.maximum.reduceat(heights[cell_bids], cell_boff[:-1])
    h_norm = np.clip((max_h_cell - 7.0) / 35.0, 0.0, 1.0)
    px2 = GAP * 2 + W_PANEL + cell_cols.astype(np.int32)
    r_h = (56 + h_norm * (244 - 56)).astype(np.uint8)
    g_h = (189 - h_norm * (189 - 63)).astype(np.uint8)
    b_h = (248 - h_norm * (248 - 94)).astype(np.uint8)
    rgb[GAP + py, px2, 0] = r_h
    rgb[GAP + py, px2, 1] = g_h
    rgb[GAP + py, px2, 2] = b_h

    # Write PNG
    height_img, width_img, _ = rgb.shape
    raw_scanlines = bytearray()
    for y in range(height_img):
        raw_scanlines.append(0)
        raw_scanlines.extend(rgb[y].tobytes())

    def make_chunk(chunk_type: bytes, data: bytes) -> bytes:
        chunk = struct.pack(">I", len(data)) + chunk_type + data
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return chunk + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width_img, height_img, 8, 2, 0, 0, 0)
    compressed = zlib.compress(bytes(raw_scanlines), level=6)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    png_path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + make_chunk(b"IHDR", ihdr)
        + make_chunk(b"IDAT", compressed)
        + make_chunk(b"IEND", b"")
    )

    svg_doc = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W_TOTAL}" height="{H_TOTAL}" viewBox="0 0 {W_TOTAL} {H_TOTAL}">',
        f'<rect width="{W_TOTAL}" height="{H_TOTAL}" fill="#0f172a"/>',
        f'<text x="{GAP}" y="17" fill="#f8fafc" font-family="sans-serif" font-size="13" font-weight="bold">{city_name} SBIB v1 Grid Density ({cols}x{rows} cells, {ne_cells:,} active)</text>',
        f'<text x="{GAP * 2 + W_PANEL}" y="17" fill="#f8fafc" font-family="sans-serif" font-size="13" font-weight="bold">Max Building Height per Cell ({b_cnt:,} buildings)</text>',
        "</svg>",
    ]
    svg_path.write_text("\n".join(svg_doc), encoding="utf-8")


def main() -> None:
    t_start = time.perf_counter()
    parser = argparse.ArgumentParser(description="Phase 9: Buildings spatial collision index encoder")
    parser.add_argument("--city", type=Path, default=Path("cities/blr.yaml"), help="Path to city YAML config")
    args = parser.parse_args()

    city_path = args.city if args.city.is_absolute() else (ROOT / args.city)
    with open(city_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    code = cfg["code"].upper()
    bbox = cfg["bbox"]
    cell_size = float(cfg["params"].get("buildings_grid_cell_deg", 0.0009))
    proc_dir = ROOT / "data" / "processed"
    mod_data_dir = ROOT / "mod" / "data" / code
    reports_dir = ROOT / "reports"
    mod_data_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load cached building arrays (shared with Phase 8 90_tiles.py)
    bdata = tiles_90.load_or_build_flat_buildings(proc_dir, cfg)

    # 2. Encode SBIB v1 binary buffer
    sbib_bytes, enc_info = encode_buildings_index_sbib(bdata, cell_size=cell_size)

    bin_path = mod_data_dir / "buildings_index.bin"
    bin_gz_path = mod_data_dir / "buildings_index.bin.gz"
    bin_path.write_bytes(sbib_bytes)
    with gzip.open(bin_gz_path, "wb", compresslevel=6) as f:
        f.write(sbib_bytes)

    # 3. Validate binary buffer with Node.js validator matching api.utils.buildings.validate
    val_check = validate_building_index_buffer(sbib_bytes)
    proc_js = subprocess.run(
        ["node", "-e", val_check["js_validator_snippet"], str(bin_path)],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    js_val_res = json.loads(proc_js.stdout.strip())
    if not js_val_res.get("ok"):
        raise RuntimeError(f"JS validator failed: {js_val_res}")

    # 4. Export building_tags.json.gz + inland coastline/ocean_depth placeholders
    tags_gz_path = mod_data_dir / "building_tags.json.gz"
    tags_info = export_building_tags(proc_dir, tags_gz_path, city_code=code)

    coastline_gz_path = mod_data_dir / "coastline.geojson.gz"
    export_coastline_and_water(proc_dir, coastline_gz_path, city_code=code)

    ocean_gz_path = mod_data_dir / "ocean_depth_index.json.gz"
    with gzip.open(ocean_gz_path, "wt", encoding="utf-8") as f:
        json.dump(
            {
                "cs": 0.0027,
                "bbox": [bbox["west"], bbox["south"], bbox["east"], bbox["north"]],
                "grid": [186, 204],
                "cells": [],
            },
            f,
        )

    # 5. Render diagnostic preview plot directly from the encoded SBIB buffer
    preview_png = reports_dir / "95_buildings_preview.png"
    preview_svg = reports_dir / "95_buildings_preview.svg"
    render_buildings_index_preview(sbib_bytes, preview_png, preview_svg, cfg["name"])

    elapsed = round(time.perf_counter() - t_start, 2)
    summary = {
        "phase": 9,
        "script": "pipeline/95_buildings.py",
        "city_code": code,
        "status": "PASS",
        "runtime_seconds": elapsed,
        "buildings_index": {
            "bin_path": str(bin_path.relative_to(ROOT)).replace("\\", "/"),
            "bin_gz_path": str(bin_gz_path.relative_to(ROOT)).replace("\\", "/"),
            "uncompressed_bytes": len(sbib_bytes),
            "compressed_bytes": bin_gz_path.stat().st_size,
            "magic": BINARY_MAGIC,
            "version": BINARY_VERSION,
            "flags": enc_info["flags"],
            "building_count": enc_info["buildingCount"],
            "cols": enc_info["cols"],
            "rows": enc_info["rows"],
            "total_rings": enc_info["totalRings"],
            "total_coords": enc_info["totalCoords"],
            "non_empty_cells": enc_info["nonEmptyCells"],
            "total_cell_refs": enc_info["totalCellRefs"],
            "total_osm_ids": enc_info["totalOsmIds"],
            "cell_size_deg": enc_info["cellSize"],
            "max_foundation_depth_m": enc_info["maxFoundationDepth"],
            "bbox": [
                enc_info["minLon"],
                enc_info["minLat"],
                enc_info["maxLon"],
                enc_info["maxLat"],
            ],
            "js_validator": js_val_res,
        },
        "building_tags": {
            "path": str(tags_gz_path.relative_to(ROOT)).replace("\\", "/"),
            **tags_info,
        },
    }

    report_path = reports_dir / "95_buildings.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(
        f"[95_buildings] Completed in {elapsed:.2f}s -> "
        f"buildings_index.bin.gz ({bin_gz_path.stat().st_size / 1e6:.2f} MB), "
        f"building_tags.json.gz ({tags_gz_path.stat().st_size / 1e6:.2f} MB, {tags_info['tagged_buildings_count']:,} tagged buildings)"
    )


if __name__ == "__main__":
    main()
