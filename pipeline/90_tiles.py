#!/usr/bin/env python3
"""
Phase 8: Vector Tiles Generation (pipeline/90_tiles.py)

Generates Mapbox Vector Tiles (MVT v2.1) packed into PMTiles v3 archives for
Subway Builder:
  - mod/data/BLR/tiles.pmtiles (zooms 5..15):
      water, parks, commercial, airports, city_labels, suburb_labels,
      neighborhood_labels, buildings (zooms 12..15 with integer OSM id & height)
  - mod/data/BLR/foundations.pmtiles (zooms 13..15):
      foundations (with foundationDepth matching Phase 9 buildings_index.bin.gz)

Also saves data/processed/blr_buildings_flat.npz so Phase 9 (95_buildings.py)
uses 100% identical building OSM IDs, heights, foundation depths, and polygon
rings as the vector tiles.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import struct
import time
import zlib
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pyarrow.parquet as pq
import yaml


ROOT = Path(__file__).resolve().parent.parent

# PMTiles v3 Hilbert curve cumulative tile counts per zoom level (z = 0..16)
TZ_VALUES = [
    0,
    1,
    5,
    21,
    85,
    341,
    1365,
    5461,
    21845,
    87381,
    349525,
    1398101,
    5592405,
    22369621,
    89478485,
    357913941,
    1431655765,
]


def _encode_varint_slow(val: int) -> bytes:
    out = bytearray()
    while val >= 0x80:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val)
    return bytes(out)


# Precomputed varint lookup table for fast MVT protobuf encoding
VI_TABLE: List[bytes] = [_encode_varint_slow(i) for i in range(65536)]


def encode_varint(val: int) -> bytes:
    if 0 <= val < 65536:
        return VI_TABLE[val]
    return _encode_varint_slow(val)


def zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 31)


def unzigzag(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def _rotate(n: int, x: int, y: int, rx: int, ry: int) -> Tuple[int, int]:
    if ry == 0:
        if rx == 1:
            x = n - 1 - x
            y = n - 1 - y
        return y, x
    return x, y


def zxy_to_tile_id(z: int, x: int, y: int) -> int:
    """Convert (z, x, y) slippy tile coordinates to PMTiles v3 Hilbert tile_id."""
    acc = TZ_VALUES[z]
    n = 1 << z
    tx, ty = x, y
    d = 0
    s = n >> 1
    while s > 0:
        rx = 1 if (tx & s) > 0 else 0
        ry = 1 if (ty & s) > 0 else 0
        d += s * s * ((3 * rx) ^ ry)
        tx, ty = _rotate(s, tx, ty, rx, ry)
        s >>= 1
    return acc + d


def tile_id_to_zxy(tile_id: int) -> Tuple[int, int, int]:
    """Convert PMTiles v3 Hilbert tile_id back to (z, x, y)."""
    for z in range(len(TZ_VALUES) - 1):
        if tile_id < TZ_VALUES[z + 1]:
            t = tile_id - TZ_VALUES[z]
            n = 1 << z
            x = 0
            y = 0
            s = 1
            while s < n:
                rx = 1 & (t // 2)
                ry = 1 & (t ^ rx)
                x, y = _rotate(s, x, y, rx, ry)
                x += s * rx
                y += s * ry
                t //= 4
                s <<= 1
            return z, x, y
    raise ValueError(f"tile_id out of range: {tile_id}")


def lonlat_to_mercator_norm(lon: float, lat: float) -> Tuple[float, float]:
    mx = (lon + 180.0) / 360.0
    lat_rad = math.radians(max(-85.05112878, min(85.05112878, lat)))
    my = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) * 0.5
    return mx, my


def lonlat_to_tile(lon: float, lat: float, z: int) -> Tuple[int, int]:
    mx, my = lonlat_to_mercator_norm(lon, lat)
    n = 1 << z
    tx = max(0, min(n - 1, int(mx * n)))
    ty = max(0, min(n - 1, int(my * n)))
    return tx, ty


def encode_mvt_value(val: Any) -> bytes:
    """Encode a Python scalar as an MVT protobuf Value message."""
    if isinstance(val, str):
        b = val.encode("utf-8")
        return b"\x0a" + encode_varint(len(b)) + b
    elif isinstance(val, bool):
        return b"\x38" + (b"\x01" if val else b"\x00")
    elif isinstance(val, (int, np.integer)):
        iv = int(val)
        if iv >= 0:
            # field 4: int_value (wire type 0 -> tag 0x20)
            return b"\x20" + encode_varint(iv)
        else:
            # field 6: sint_value (wire type 0 -> tag 0x30)
            return b"\x30" + encode_varint(zigzag(iv))
    elif isinstance(val, (float, np.floating)):
        # field 3: double_value (wire type 1 -> tag 0x19)
        return b"\x19" + struct.pack("<d", float(val))
    raise TypeError(f"Unsupported MVT value type: {type(val)}")


def clip_polygon_to_box(
    pts: List[Tuple[int, int]], xmin: int, ymin: int, xmax: int, ymax: int
) -> List[Tuple[int, int]]:
    """Sutherland-Hodgman polygon clipping against axis-aligned rectangle [xmin, xmax] x [ymin, ymax]."""
    if len(pts) < 3:
        return []

    def clip_edge(ring: List[Tuple[int, int]], edge: int) -> List[Tuple[int, int]]:
        if not ring:
            return []
        out: List[Tuple[int, int]] = []
        px, py = ring[-1]
        for cx, cy in ring:
            if edge == 0:  # x >= xmin
                p_in, c_in = px >= xmin, cx >= xmin
            elif edge == 1:  # x <= xmax
                p_in, c_in = px <= xmax, cx <= xmax
            elif edge == 2:  # y >= ymin
                p_in, c_in = py >= ymin, cy >= ymin
            else:  # y <= ymax
                p_in, c_in = py <= ymax, cy <= ymax

            if c_in:
                if not p_in:
                    out.append(_intersect(px, py, cx, cy, edge))
                out.append((cx, cy))
            elif p_in:
                out.append(_intersect(px, py, cx, cy, edge))
            px, py = cx, cy
        return out

    def _intersect(x1: int, y1: int, x2: int, y2: int, edge: int) -> Tuple[int, int]:
        dx = x2 - x1
        dy = y2 - y1
        if edge == 0:
            y = y1 + (dy * (xmin - x1)) // dx if dx != 0 else y1
            return (xmin, y)
        elif edge == 1:
            y = y1 + (dy * (xmax - x1)) // dx if dx != 0 else y1
            return (xmax, y)
        elif edge == 2:
            x = x1 + (dx * (ymin - y1)) // dy if dy != 0 else x1
            return (x, ymin)
        else:
            x = x1 + (dx * (ymax - y1)) // dy if dy != 0 else x1
            return (x, ymax)

    res = pts
    for edge_id in range(4):
        res = clip_edge(res, edge_id)
        if len(res) < 3:
            return []
    return res


def encode_polygon_ring_mvt(pts: List[Tuple[int, int]]) -> bytes:
    """
    Encode an open or closed polygon ring in tile coordinates (0..4096) into MVT
    geometry bytes (MoveTo + LineTo + ClosePath), ensuring clockwise exterior
    winding in y-down screen coordinates (shoelace sum > 0) and deduplicating
    consecutive identical vertices. Returns b"" if degenerate (< 3 vertices or 0 area).
    """
    if len(pts) < 3:
        return b""
    # Deduplicate consecutive identical integer coordinates
    dedup: List[Tuple[int, int]] = [pts[0]]
    for p in pts[1:]:
        if p != dedup[-1]:
            dedup.append(p)
    if len(dedup) > 1 and dedup[-1] == dedup[0]:
        dedup.pop()
    k = len(dedup)
    if k < 3:
        return b""

    # Compute signed shoelace area in y-down tile coordinates: sum(x_i * y_{i+1} - x_{i+1} * y_i)
    area2 = 0
    px, py = dedup[-1]
    for cx, cy in dedup:
        area2 += px * cy - cx * py
        px, py = cx, cy
    if area2 == 0:
        return b""
    if area2 < 0:
        # Reverse vertices [0, k-1, k-2, ..., 1] so exterior ring is clockwise (area2 > 0)
        dedup = [dedup[0]] + dedup[:0:-1]

    x0, y0 = dedup[0]
    parts: List[bytes] = [
        VI_TABLE[9],  # MoveTo(1)
        encode_varint(zigzag(x0)),
        encode_varint(zigzag(y0)),
        encode_varint(((k - 1) << 3) | 2),  # LineTo(k - 1)
    ]
    px, py = x0, y0
    for cx, cy in dedup[1:]:
        parts.append(encode_varint(zigzag(cx - px)))
        parts.append(encode_varint(zigzag(cy - py)))
        px, py = cx, cy
    parts.append(VI_TABLE[15])  # ClosePath(1)
    return b"".join(parts)


def encode_point_mvt(x: int, y: int) -> bytes:
    """Encode a single POINT geometry in tile coordinates (0..4096)."""
    return b"\x09" + encode_varint(zigzag(x)) + encode_varint(zigzag(y))


def build_mvt_layer(
    name: str,
    features_raw: List[Tuple[int | None, int, Dict[str, Any], bytes]],
    extent: int = 4096,
) -> bytes:
    """
    Build an MVT Layer protobuf message (wrapped with Tile field 3 tag 0x1a).
    features_raw: list of (feature_id_or_None, geom_type, props_dict, geom_bytes)
    """
    if not features_raw:
        return b""
    key_map: Dict[str, int] = {}
    keys_list: List[bytes] = []
    val_map: Dict[Tuple[type, Any], int] = {}
    vals_list: List[bytes] = []
    feat_msgs: List[bytes] = []

    for fid, gtype, props, geom_bytes in features_raw:
        if not geom_bytes:
            continue
        tag_bytes_list: List[bytes] = []
        for k, v in props.items():
            ki = key_map.get(k)
            if ki is None:
                ki = len(keys_list)
                key_map[k] = ki
                kb = k.encode("utf-8")
                keys_list.append(b"\x1a" + encode_varint(len(kb)) + kb)
            vkey = (type(v), v)
            vi = val_map.get(vkey)
            if vi is None:
                vi = len(vals_list)
                val_map[vkey] = vi
                vb = encode_mvt_value(v)
                vals_list.append(b"\x22" + encode_varint(len(vb)) + vb)
            tag_bytes_list.append(encode_varint(ki))
            tag_bytes_list.append(encode_varint(vi))

        f_parts: List[bytes] = []
        if fid is not None and fid >= 0:
            f_parts.append(b"\x08" + encode_varint(int(fid)))
        if tag_bytes_list:
            tb = b"".join(tag_bytes_list)
            f_parts.append(b"\x12" + encode_varint(len(tb)) + tb)
        f_parts.append(b"\x18" + VI_TABLE[gtype])
        f_parts.append(b"\x22" + encode_varint(len(geom_bytes)) + geom_bytes)
        fb = b"".join(f_parts)
        feat_msgs.append(b"\x12" + encode_varint(len(fb)) + fb)

    if not feat_msgs:
        return b""

    name_b = name.encode("utf-8")
    layer_body = (
        b"\x78\x02"  # version = 2 (field 15, varint)
        + b"\x0a"
        + encode_varint(len(name_b))
        + name_b
        + b"".join(feat_msgs)
        + b"".join(keys_list)
        + b"".join(vals_list)
        + b"\x28"
        + encode_varint(extent)
    )
    return b"\x1a" + encode_varint(len(layer_body)) + layer_body


def serialize_pmtiles_directory(entries: List[Tuple[int, int, int, int]]) -> bytes:
    """
    Serialize a list of PMTiles v3 directory entries (tile_id, run_length, length, offset)
    into uncompressed directory bytes (to be gzipped).
    """
    n = len(entries)
    parts: List[bytes] = [encode_varint(n)]
    last_id = 0
    for tid, _, _, _ in entries:
        parts.append(encode_varint(tid - last_id))
        last_id = tid
    for _, rl, _, _ in entries:
        parts.append(encode_varint(rl))
    for _, _, ln, _ in entries:
        parts.append(encode_varint(ln))
    for i, (_, _, ln, off) in enumerate(entries):
        if i > 0 and off == entries[i - 1][3] + entries[i - 1][2]:
            parts.append(VI_TABLE[0])
        else:
            parts.append(encode_varint(off + 1))
    return b"".join(parts)


def write_pmtiles_v3(
    out_path: Path,
    tiles_by_id: Dict[int, bytes],
    metadata: Dict[str, Any],
    min_zoom: int,
    max_zoom: int,
    bbox: Dict[str, float],
    center_lon: float,
    center_lat: float,
    center_zoom: int,
) -> Dict[str, int]:
    """
    Write a valid PMTiles v3 archive from {tile_id: uncompressed_mvt_bytes}.
    Supports automatic root + leaf directory splitting if root directory > 16 KB.
    """
    sorted_tids = sorted(tiles_by_id.keys())
    # Deduplicate identical consecutive or repeated tile payloads if any
    tile_payloads: List[bytes] = []
    entries: List[Tuple[int, int, int, int]] = []
    cur_offset = 0

    for tid in sorted_tids:
        raw_mvt = tiles_by_id[tid]
        if not raw_mvt:
            continue
        gz_mvt = gzip.compress(raw_mvt, compresslevel=6)
        ln = len(gz_mvt)
        entries.append((tid, 1, ln, cur_offset))
        tile_payloads.append(gz_mvt)
        cur_offset += ln

    meta_bytes = gzip.compress(
        json.dumps(metadata, separators=(",", ":")).encode("utf-8"), compresslevel=6
    )
    root_dir_gz = gzip.compress(serialize_pmtiles_directory(entries), compresslevel=6)
    leaf_dirs_bytes = b""

    # If 127 + len(root_dir_gz) > 16384, split into leaf directories of 2048 entries
    if 127 + len(root_dir_gz) > 16384:
        leaf_chunks: List[bytes] = []
        root_entries: List[Tuple[int, int, int, int]] = []
        leaf_off = 0
        chunk_size = 2048
        for i in range(0, len(entries), chunk_size):
            sub = entries[i : i + chunk_size]
            sub_gz = gzip.compress(serialize_pmtiles_directory(sub), compresslevel=6)
            root_entries.append((sub[0][0], 0, len(sub_gz), leaf_off))
            leaf_chunks.append(sub_gz)
            leaf_off += len(sub_gz)
        leaf_dirs_bytes = b"".join(leaf_chunks)
        root_dir_gz = gzip.compress(serialize_pmtiles_directory(root_entries), compresslevel=6)

    root_off = 127
    root_len = len(root_dir_gz)
    meta_off = root_off + root_len
    meta_len = len(meta_bytes)
    leaf_off = meta_off + meta_len
    leaf_len = len(leaf_dirs_bytes)
    data_off = leaf_off + leaf_len
    data_len = cur_offset

    n_addressed = len(entries)
    n_entries = len(entries)
    n_contents = len(entries)

    min_lon_e7 = int(round(bbox["west"] * 1e7))
    min_lat_e7 = int(round(bbox["south"] * 1e7))
    max_lon_e7 = int(round(bbox["east"] * 1e7))
    max_lat_e7 = int(round(bbox["north"] * 1e7))
    center_lon_e7 = int(round(center_lon * 1e7))
    center_lat_e7 = int(round(center_lat * 1e7))

    header = bytearray(127)
    header[0:8] = b"PMTiles\x03"
    struct.pack_into(
        "<QQQQQQQQ",
        header,
        8,
        root_off,
        root_len,
        meta_off,
        meta_len,
        leaf_off,
        leaf_len,
        data_off,
        data_len,
    )
    struct.pack_into("<QQQ", header, 72, n_addressed, n_entries, n_contents)
    struct.pack_into(
        "<BBBBBB",
        header,
        96,
        1,  # clustered = 1
        2,  # internal_compression = 2 (gzip)
        2,  # tile_compression = 2 (gzip)
        1,  # tile_type = 1 (MVT)
        min_zoom,
        max_zoom,
    )
    struct.pack_into(
        "<iiiiBii",
        header,
        102,
        min_lon_e7,
        min_lat_e7,
        max_lon_e7,
        max_lat_e7,
        center_zoom,
        center_lon_e7,
        center_lat_e7,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(header)
        f.write(root_dir_gz)
        f.write(meta_bytes)
        if leaf_dirs_bytes:
            f.write(leaf_dirs_bytes)
        for chunk in tile_payloads:
            f.write(chunk)

    return {
        "file_size_bytes": out_path.stat().st_size,
        "num_tiles": n_entries,
        "root_dir_bytes": root_len,
        "leaf_dir_bytes": leaf_len,
        "data_bytes": data_len,
    }


def buffer_linestring_to_polygon_merc(
    coords: List[List[float]], half_width_m: float
) -> List[Tuple[float, float]]:
    """Buffer a small WGS84 polyline into a closed polygon in normalized Web Mercator (mx, my)."""
    if len(coords) < 2:
        return []
    # 1 meter in normalized Web Mercator around lat 13 deg is ~ 1 / (40075016.686 * cos(13 deg))
    delta = half_width_m / 39040000.0
    mpts = [lonlat_to_mercator_norm(pt[0], pt[1]) for pt in coords]
    left: List[Tuple[float, float]] = []
    right: List[Tuple[float, float]] = []
    for i in range(len(mpts) - 1):
        x1, y1 = mpts[i]
        x2, y2 = mpts[i + 1]
        dx = x2 - x1
        dy = y2 - y1
        seg_len = math.hypot(dx, dy)
        if seg_len < 1e-12:
            continue
        nx = -dy / seg_len * delta
        ny = dx / seg_len * delta
        if not left:
            left.append((x1 + nx, y1 + ny))
            right.append((x1 - nx, y1 - ny))
        left.append((x2 + nx, y2 + ny))
        right.append((x2 - nx, y2 - ny))
    if len(left) < 2:
        return []
    return left + right[::-1]


def load_or_build_flat_buildings(
    proc_dir: Path, cfg: Dict[str, Any]
) -> Dict[str, np.ndarray]:
    """
    Load or build data/processed/<city_code>_buildings_flat.npz containing deterministic
    building heights, foundation depths, integer OSM IDs, and flattened polygon
    rings for both Phase 8 (90_tiles.py) and Phase 9 (95_buildings.py).
    """
    code = cfg["code"].lower()
    cache_path = proc_dir / f"{code}_buildings_flat.npz"
    if cache_path.exists():
        data = np.load(cache_path)
        return {k: data[k] for k in data.files}

    t0 = time.perf_counter()
    tbl = pq.read_table(
        proc_dir / f"{code}_buildings.parquet",
        columns=[
            "osm_id",
            "building",
            "levels",
            "height",
            "area_m2",
            "min_lon",
            "min_lat",
            "max_lon",
            "max_lat",
            "centroid_lon",
            "centroid_lat",
            "coords_json",
        ],
    )
    n = tbl.num_rows
    osm_ids = tbl["osm_id"].to_numpy().astype(np.int64)
    area_m2 = tbl["area_m2"].to_numpy().astype(np.float32)
    min_lon = tbl["min_lon"].to_numpy().astype(np.float64)
    min_lat = tbl["min_lat"].to_numpy().astype(np.float64)
    max_lon = tbl["max_lon"].to_numpy().astype(np.float64)
    max_lat = tbl["max_lat"].to_numpy().astype(np.float64)
    centroid_lon = tbl["centroid_lon"].to_numpy().astype(np.float64)
    centroid_lat = tbl["centroid_lat"].to_numpy().astype(np.float64)

    levels_raw = tbl["levels"].to_pylist()
    height_raw = tbl["height"].to_pylist()
    btype_raw = tbl["building"].to_pylist()

    params = cfg["params"]
    m_per_lvl = float(params.get("meters_per_building_level", 3.5))
    fd_per_lvl = float(params.get("foundation_depth_per_level_m", 2.5))
    default_fd = float(params.get("default_foundation_depth_m", 7.0))

    heights = np.empty(n, dtype=np.float32)
    fdepths = np.empty(n, dtype=np.float32)

    tall_types = {"apartments", "office", "commercial", "hotel", "hospital", "university", "civic"}
    med_types = {"industrial", "warehouse", "retail", "school", "college", "public"}

    for i in range(n):
        h = height_raw[i]
        lv = levels_raw[i]
        bt = btype_raw[i]
        if h is not None and h > 0:
            hm = float(h)
            l_eff = hm / m_per_lvl
        elif lv is not None and lv > 0:
            l_eff = float(lv)
            hm = l_eff * m_per_lvl
        else:
            if bt in tall_types:
                l_eff = 4.0
            elif bt in med_types:
                l_eff = 3.0
            else:
                l_eff = 2.0
            hm = l_eff * m_per_lvl
        heights[i] = max(3.5, min(250.0, hm))
        fdepths[i] = max(default_fd, min(40.0, round(l_eff * fd_per_lvl)))

    cjs = tbl["coords_json"].to_pylist()
    rings = [json.loads(s) for s in cjs]
    lens = np.fromiter((len(r) for r in rings), dtype=np.int32, count=n)
    ring_offsets = np.zeros(n + 1, dtype=np.uint32)
    np.cumsum(lens, out=ring_offsets[1:])
    total_verts = int(ring_offsets[-1])
    coords = np.fromiter(
        (c for r in rings for pt in r for c in pt),
        dtype=np.float64,
        count=total_verts * 2,
    ).reshape(-1, 2)

    np.savez(
        cache_path,
        osm_ids=osm_ids,
        heights=heights,
        foundation_depths=fdepths,
        area_m2=area_m2,
        min_lon=min_lon,
        min_lat=min_lat,
        max_lon=max_lon,
        max_lat=max_lat,
        centroid_lon=centroid_lon,
        centroid_lat=centroid_lat,
        ring_offsets=ring_offsets,
        coords=coords,
    )
    print(
        f"[90_tiles] Cached {n:,} buildings ({total_verts:,} vertices) to {cache_path.name} in {time.perf_counter() - t0:.2f}s"
    )
    return {
        "osm_ids": osm_ids,
        "heights": heights,
        "foundation_depths": fdepths,
        "area_m2": area_m2,
        "min_lon": min_lon,
        "min_lat": min_lat,
        "max_lon": max_lon,
        "max_lat": max_lat,
        "centroid_lon": centroid_lon,
        "centroid_lat": centroid_lat,
        "ring_offsets": ring_offsets,
        "coords": coords,
    }


def build_buildings_and_foundations_layers(
    bdata: Dict[str, np.ndarray],
    bbox: Dict[str, float],
) -> Tuple[Dict[Tuple[int, int, int], bytes], Dict[Tuple[int, int, int], bytes], Dict[str, int]]:
    """
    Vectorized + bucketed MVT encoder for `buildings` (zooms 12..15) and
    `foundations` (zooms 13..15).
    Returns:
      - building_layer_by_zxy: {(z, tx, ty): mvt_layer_bytes}
      - foundation_layer_by_zxy: {(z, tx, ty): mvt_layer_bytes}
      - stats dict
    """
    t0 = time.perf_counter()
    osm_ids = bdata["osm_ids"]
    heights_int = np.rint(bdata["heights"]).astype(np.int32)
    fdepths_int = np.rint(bdata["foundation_depths"]).astype(np.int32)
    area_m2 = bdata["area_m2"]
    clon = bdata["centroid_lon"]
    clat = bdata["centroid_lat"]
    ring_offsets = bdata["ring_offsets"]
    coords = bdata["coords"]
    n_bld = len(osm_ids)

    # Convert all building polygon vertices to global zoom-15 MVT integer coords
    # (scale = 32768 * 4096 = 134,217,728)
    scale15 = float((1 << 15) * 4096)
    vx = (coords[:, 0] + 180.0) / 360.0
    vy = (1.0 - np.arcsinh(np.tan(np.radians(coords[:, 1]))) / math.pi) * 0.5
    gx15 = np.rint(vx * scale15).astype(np.int32)
    gy15 = np.rint(vy * scale15).astype(np.int32)

    # Centroid normalized Mercator for tile assignment
    cmx = (clon + 180.0) / 360.0
    cmy = (1.0 - np.arcsinh(np.tan(np.radians(clat))) / math.pi) * 0.5

    ring_lens = np.diff(ring_offsets).astype(np.int32)

    building_layer_by_zxy: Dict[Tuple[int, int, int], bytes] = {}
    foundation_layer_by_zxy: Dict[Tuple[int, int, int], bytes] = {}
    stats: Dict[str, int] = {}

    # Area thresholds by zoom (matches tippecanoe --drop-smallest-as-needed behavior
    # while keeping 100% of buildings at zoom 15)
    area_thresholds = {12: 300.0, 13: 120.0, 14: 45.0, 15: 0.0}

    for z in (12, 13, 14, 15):
        tz0 = time.perf_counter()
        shift = 15 - z
        gxz = gx15 >> shift if shift > 0 else gx15
        gyz = gy15 >> shift if shift > 0 else gy15

        min_area = area_thresholds[z]
        mask = area_m2 >= min_area
        idx_all = np.nonzero(mask)[0].astype(np.int32)

        n_tiles = 1 << z
        tx_arr = np.clip((cmx[idx_all] * n_tiles).astype(np.int32), 0, n_tiles - 1)
        ty_arr = np.clip((cmy[idx_all] * n_tiles).astype(np.int32), 0, n_tiles - 1)

        # Separate 4-corner buildings (ring_len == 5) for vectorized geometry encoding
        rl_sel = ring_lens[idx_all]
        is_quad = rl_sel == 5

        # Precompute geometry bytes for all selected buildings at zoom z
        geom_bytes_list: List[bytes] = [b""] * len(idx_all)

        # 1. Vectorized path for 4-corner buildings (93.3% of all buildings)
        quad_pos = np.nonzero(is_quad)[0]
        if len(quad_pos) > 0:
            q_idx = idx_all[quad_pos]
            q_off = ring_offsets[q_idx]
            q_tx0 = tx_arr[quad_pos] << 12
            q_ty0 = ty_arr[quad_pos] << 12

            x0 = gxz[q_off] - q_tx0
            y0 = gyz[q_off] - q_ty0
            x1 = gxz[q_off + 1] - q_tx0
            y1 = gyz[q_off + 1] - q_ty0
            x2 = gxz[q_off + 2] - q_tx0
            y2 = gyz[q_off + 2] - q_ty0
            x3 = gxz[q_off + 3] - q_tx0
            y3 = gyz[q_off + 3] - q_ty0

            area2 = (
                (x0 * y1 - x1 * y0)
                + (x1 * y2 - x2 * y1)
                + (x2 * y3 - x3 * y2)
                + (x3 * y0 - x0 * y3)
            )
            neg = area2 < 0
            if np.any(neg):
                x1_new = np.where(neg, x3, x1)
                y1_new = np.where(neg, y3, y1)
                x3 = np.where(neg, x1, x3)
                y3 = np.where(neg, y1, y3)
                x1, y1 = x1_new, y1_new

            dx1 = x1 - x0
            dy1 = y1 - y0
            dx2 = x2 - x1
            dy2 = y2 - y1
            dx3 = x3 - x2
            dy3 = y3 - y2

            zx0 = ((x0 << 1) ^ (x0 >> 31)).tolist()
            zy0 = ((y0 << 1) ^ (y0 >> 31)).tolist()
            zdx1 = ((dx1 << 1) ^ (dx1 >> 31)).tolist()
            zdy1 = ((dy1 << 1) ^ (dy1 >> 31)).tolist()
            zdx2 = ((dx2 << 1) ^ (dx2 >> 31)).tolist()
            zdy2 = ((dy2 << 1) ^ (dy2 >> 31)).tolist()
            zdx3 = ((dx3 << 1) ^ (dx3 >> 31)).tolist()
            zdy3 = ((dy3 << 1) ^ (dy3 >> 31)).tolist()
            valid_q = (area2 != 0).tolist()
            q_pos_list = quad_pos.tolist()

            vi = VI_TABLE
            ev = encode_varint
            for j, pos_i in enumerate(q_pos_list):
                if not valid_q[j]:
                    continue
                a0, b0 = zx0[j], zy0[j]
                a1, b1 = zdx1[j], zdy1[j]
                a2, b2 = zdx2[j], zdy2[j]
                a3, b3 = zdx3[j], zdy3[j]
                if (a0 | b0 | a1 | b1 | a2 | b2 | a3 | b3) < 65536:
                    geom_bytes_list[pos_i] = (
                        b"\x09"
                        + vi[a0]
                        + vi[b0]
                        + b"\x1a"
                        + vi[a1]
                        + vi[b1]
                        + vi[a2]
                        + vi[b2]
                        + vi[a3]
                        + vi[b3]
                        + b"\x0f"
                    )
                else:
                    geom_bytes_list[pos_i] = (
                        b"\x09"
                        + ev(a0)
                        + ev(b0)
                        + b"\x1a"
                        + ev(a1)
                        + ev(b1)
                        + ev(a2)
                        + ev(b2)
                        + ev(a3)
                        + ev(b3)
                        + b"\x0f"
                    )

        # 2. General polygon path for non-4-corner buildings (6.7% of buildings)
        poly_pos = np.nonzero(~is_quad)[0]
        if len(poly_pos) > 0:
            gxz_list = gxz.tolist()
            gyz_list = gyz.tolist()
            for pos_i in poly_pos.tolist():
                b_i = int(idx_all[pos_i])
                s_off = int(ring_offsets[b_i])
                e_off = int(ring_offsets[b_i + 1]) - 1  # exclude closing duplicate vertex
                if e_off - s_off < 3:
                    continue
                tx0 = int(tx_arr[pos_i]) << 12
                ty0 = int(ty_arr[pos_i]) << 12
                pts = [
                    (gxz_list[k] - tx0, gyz_list[k] - ty0)
                    for k in range(s_off, e_off)
                ]
                geom_bytes_list[pos_i] = encode_polygon_ring_mvt(pts)

        # Group selected buildings by tile (tx, ty) using argsort on packed tile key
        tile_keys = (tx_arr.astype(np.int64) << 20) | ty_arr.astype(np.int64)
        order = np.argsort(tile_keys, kind="mergesort")
        sorted_keys = tile_keys[order]
        unique_keys, split_starts = np.unique(sorted_keys, return_index=True)
        split_ends = np.append(split_starts[1:], len(order))

        order_list = order.tolist()
        idx_all_list = idx_all.tolist()
        osm_ids_list = osm_ids.tolist()
        heights_list = heights_int.tolist()
        fdepths_list = fdepths_int.tolist()

        vi = VI_TABLE
        ev = encode_varint
        z_bld_count = 0
        z_fnd_count = 0

        for u_idx, tkey in enumerate(unique_keys.tolist()):
            tx = int(tkey >> 20)
            ty = int(tkey & 0xFFFFF)
            s_i = int(split_starts[u_idx])
            e_i = int(split_ends[u_idx])

            # Build `buildings` layer for (z, tx, ty)
            # keys = ["id", "height"] (key 0 = "id", key 1 = "height")
            h_map: Dict[int, int] = {}
            vals_pb: List[bytes] = []
            feat_pb: List[bytes] = []

            # Also build `foundations` layer for (z >= 13, tx, ty)
            # keys = ["foundationDepth"] (key 0 = "foundationDepth")
            fd_map: Dict[int, int] = {}
            fd_vals_pb: List[bytes] = []
            fd_feat_pb: List[bytes] = []
            do_fnd = z >= 13

            for k_ord in range(s_i, e_i):
                pos_i = order_list[k_ord]
                gb = geom_bytes_list[pos_i]
                if not gb:
                    continue
                b_i = idx_all_list[pos_i]
                oid = osm_ids_list[b_i]
                hm = heights_list[b_i]

                # height value index
                h_vi = h_map.get(hm)
                if h_vi is None:
                    h_vi = len(vals_pb)
                    h_map[hm] = h_vi
                    vb = b"\x20" + ev(hm)
                    vals_pb.append(b"\x22" + vi[len(vb)] + vb)

                # id value index (unique per building)
                id_vi = len(vals_pb)
                vb_id = b"\x20" + ev(oid)
                vals_pb.append(b"\x22" + vi[len(vb_id)] + vb_id)

                tb = b"\x00" + ev(id_vi) + b"\x01" + ev(h_vi)
                oid_vi = ev(oid)
                fb = (
                    b"\x08"
                    + oid_vi
                    + b"\x12"
                    + vi[len(tb)]
                    + tb
                    + b"\x18\x03\x22"
                    + ev(len(gb))
                    + gb
                )
                feat_pb.append(b"\x12" + ev(len(fb)) + fb)
                z_bld_count += 1

                if do_fnd:
                    fd = fdepths_list[b_i]
                    f_vi = fd_map.get(fd)
                    if f_vi is None:
                        f_vi = len(fd_vals_pb)
                        fd_map[fd] = f_vi
                        vbf = b"\x20" + ev(fd)
                        fd_vals_pb.append(b"\x22" + vi[len(vbf)] + vbf)
                    tbf = b"\x00" + vi[f_vi]
                    fbf = (
                        b"\x12"
                        + vi[len(tbf)]
                        + tbf
                        + b"\x18\x03\x22"
                        + ev(len(gb))
                        + gb
                    )
                    fd_feat_pb.append(b"\x12" + ev(len(fbf)) + fbf)
                    z_fnd_count += 1

            if feat_pb:
                # Layer name = "buildings", keys = ["id", "height"]
                lbody = (
                    b"\x78\x02\x0a\x09buildings"
                    + b"".join(feat_pb)
                    + b"\x1a\x02id\x1a\x06height"
                    + b"".join(vals_pb)
                    + b"\x28\x80\x20"
                )
                building_layer_by_zxy[(z, tx, ty)] = (
                    b"\x1a" + ev(len(lbody)) + lbody
                )

            if do_fnd and fd_feat_pb:
                # Layer name = "foundations", keys = ["foundationDepth"]
                flbody = (
                    b"\x78\x02\x0a\x0bfoundations"
                    + b"".join(fd_feat_pb)
                    + b"\x1a\x0ffoundationDepth"
                    + b"".join(fd_vals_pb)
                    + b"\x28\x80\x20"
                )
                foundation_layer_by_zxy[(z, tx, ty)] = (
                    b"\x1a" + ev(len(flbody)) + flbody
                )

        stats[f"buildings_z{z}"] = z_bld_count
        if z >= 13:
            stats[f"foundations_z{z}"] = z_fnd_count
        print(
            f"[90_tiles] z={z}: encoded {z_bld_count:,} buildings across {len(unique_keys):,} tiles in {time.perf_counter() - tz0:.2f}s"
        )

    stats["buildings_encode_seconds"] = round(time.perf_counter() - t0, 2)
    return building_layer_by_zxy, foundation_layer_by_zxy, stats


def load_context_polygons_and_labels(
    proc_dir: Path, mod_data_dir: Path, raw_dir: Path, cfg: Dict[str, Any]
) -> Tuple[
    Dict[str, List[Tuple[List[Tuple[float, float]], Dict[str, Any], float]]],
    Dict[str, List[Tuple[float, float, Dict[str, Any], int]]],
]:
    """
    Load water, parks, commercial/college, airports polygons (in normalized Web Mercator)
    and city/suburb/neighborhood point labels.
    """
    code = cfg["code"].lower()
    poly_layers: Dict[str, List[Tuple[List[Tuple[float, float]], Dict[str, Any], float]]] = {
        "water": [],
        "ocean_foundations": [],
        "parks": [],
        "landuse": [],
        "commercial": [],
        "airports": [],
    }

    # 1. Water polygons + buffered rivers/canals/streams/drains
    major_rivers = {
        "ulhas", "mithi", "kalu", "bhatsa", "vaitarna", "dahisar", "poisar",
        "oshiwara", "kalundre", "patalganga", "taloja", "taloje", "gadi", "kasadi",
        "waldhuni", "kamvadi", "gadhe", "surya", "pinjal", "pej", "barvi", "shil",
        "malad", "mahim", "irla", "vashi", "bhogeshwari",
        "mula", "mutha", "pavana", "indrayani", "ramnadi", "bhima", "nira"
    }
    wt = pq.read_table(proc_dir / f"{code}_water.parquet").to_pydict()
    for i in range(len(wt["osm_id"])):
        coords = json.loads(wt["coords_json"][i])
        is_poly = wt["is_polygon"][i]
        wtype = wt["water_type"][i]
        wname = (wt["name"][i] or "").lower()
        area = float(wt["area_m2"][i] or 0.0)
        if is_poly and len(coords) >= 3:
            mpts = [lonlat_to_mercator_norm(pt[0], pt[1]) for pt in coords]
            poly_layers["water"].append((mpts, {"name": wt["name"][i] or "", "area": area}, max(area, 50000.0)))
            poly_layers["ocean_foundations"].append(
                (mpts, {"depth_min": -5, "depth_max": -8, "area": max(area, 50000.0)}, max(area, 50000.0))
            )
        elif not is_poly and len(coords) >= 2:
            is_major = any(r in wname for r in major_rivers)
            if is_major or wtype in ("river", "creek"):
                hw = 45.0
            elif wtype in ("canal", "stream"):
                hw = 25.0
            else:
                hw = 15.0
            mpts = buffer_linestring_to_polygon_merc(coords, hw)
            if len(mpts) >= 3:
                poly_layers["water"].append((mpts, {"name": wt["name"][i] or ""}, 500000.0))
                poly_layers["ocean_foundations"].append(
                    (mpts, {"depth_min": -5, "depth_max": -8, "area": 500000.0}, 500000.0)
                )

    # 1b. Ocean, sea & large creek water polygons from coastline
    ocean_geojson = proc_dir / f"{code}_ocean_water.geojson"
    if ocean_geojson.exists():
        with open(ocean_geojson, "r", encoding="utf-8") as f:
            ocean_fc = json.load(f)
        for feat in ocean_fc.get("features", []):
            geom = feat.get("geometry", {})
            gtype = geom.get("type")
            coords_list = geom.get("coordinates", [])
            if gtype == "Polygon":
                for ring in coords_list[:1]:
                    if len(ring) >= 3:
                        mpts = [lonlat_to_mercator_norm(pt[0], pt[1]) for pt in ring]
                        poly_layers["water"].append((mpts, {"kind": "ocean"}, 10000000.0))
                        poly_layers["ocean_foundations"].append(
                            (mpts, {"depth_min": -10, "depth_max": -15, "area": 10000000.0}, 10000000.0)
                        )
            elif gtype == "MultiPolygon":
                for poly_rings in coords_list:
                    for ring in poly_rings[:1]:
                        if len(ring) >= 3:
                            mpts = [lonlat_to_mercator_norm(pt[0], pt[1]) for pt in ring]
                            poly_layers["water"].append((mpts, {"kind": "ocean"}, 10000000.0))
                            poly_layers["ocean_foundations"].append(
                                (mpts, {"depth_min": -10, "depth_max": -15, "area": 10000000.0}, 10000000.0)
                            )

    # 2. Parks polygons (written to both "parks" for vanilla and "landuse" for mapLoader)
    pk = pq.read_table(proc_dir / f"{code}_parks.parquet").to_pydict()
    for i in range(len(pk["osm_id"])):
        coords = json.loads(pk["coords_json"][i])
        area = float(pk["area_m2"][i] or 0.0)
        if len(coords) >= 3 and area > 0:
            mpts = [lonlat_to_mercator_norm(pt[0], pt[1]) for pt in coords]
            poly_layers["parks"].append((mpts, {"area": round(area, 2)}, area))
            poly_layers["landuse"].append((mpts, {"kind": "park", "area": round(area, 2)}, area))

    # 3. Landuse -> commercial ("commercial" or "college") & airport boundaries
    lu = pq.read_table(proc_dir / f"{code}_landuse.parquet").to_pydict()
    college_cats = {"college", "university", "school", "education", "research_institute"}
    comm_cats = {"commercial", "retail", "industrial", "hospital", "institutional"}
    for i in range(len(lu["osm_id"])):
        cat = lu["category"][i]
        name = (lu["name"][i] or "").lower()
        area = float(lu["area_m2"][i] or 0.0)
        if area <= 0:
            continue
        coords = json.loads(lu["coords_json"][i])
        if len(coords) < 3:
            continue
        mpts = [lonlat_to_mercator_norm(pt[0], pt[1]) for pt in coords]
        if any(k in name for k in ("airport", "aerodrome", "air force")):
            poly_layers["airports"].append((mpts, {}, area))
            poly_layers["landuse"].append((mpts, {"kind": "aerodrome"}, area))
        elif cat in college_cats:
            poly_layers["commercial"].append(
                (mpts, {"area": round(area, 2), "type": "college"}, area)
            )
        elif cat in comm_cats:
            poly_layers["commercial"].append(
                (mpts, {"area": round(area, 2), "type": "commercial"}, area)
            )

    # 4. Airports polygons + buffered runways/taxiways from Phase 7
    ap = pq.read_table(proc_dir / f"{code}_airports.parquet").to_pydict()
    for i in range(len(ap["osm_id"])):
        if not ap["is_polygon"][i]:
            continue
        coords = json.loads(ap["coords_json"][i])
        area = float(ap["area_m2"][i] or 1000.0)
        if len(coords) >= 3:
            mpts = [lonlat_to_mercator_norm(pt[0], pt[1]) for pt in coords]
            poly_layers["airports"].append((mpts, {}, max(area, 1000.0)))
            poly_layers["landuse"].append((mpts, {"kind": "aerodrome"}, max(area, 1000.0)))

    rw_gz = mod_data_dir / "runways_taxiways.geojson.gz"
    if rw_gz.exists():
        with gzip.open(rw_gz, "rt", encoding="utf-8") as f:
            rw_fc = json.load(f)
        for feat in rw_fc.get("features", []):
            ring = feat.get("geometry", {}).get("coordinates", [[]])[0]
            if len(ring) >= 3:
                mpts = [lonlat_to_mercator_norm(pt[0], pt[1]) for pt in ring]
                area = float(feat.get("properties", {}).get("area", 5000.0))
                poly_layers["airports"].append((mpts, {}, max(area, 5000.0)))
                poly_layers["landuse"].append((mpts, {"kind": "aerodrome"}, max(area, 5000.0)))

    # 5. Labels (city_labels, suburb_labels, neighborhood_labels)
    label_layers: Dict[str, List[Tuple[float, float, Dict[str, Any], int]]] = {
        "city_labels": [],
        "suburb_labels": [],
        "neighborhood_labels": [],
    }
    seen_names: Dict[str, set] = {
        "city_labels": set(),
        "suburb_labels": set(),
        "neighborhood_labels": set(),
    }

    # Ensure primary cities/towns are always present in city_labels
    if code == "bom":
        canonical_cities = [
            ("Mumbai", 72.8777, 19.0760),
            ("Thane", 72.9754, 19.1860),
            ("Navi Mumbai", 73.0000, 19.0300),
            ("Kalyan", 73.1352, 19.2444),
            ("Titwala", 73.2085, 19.2975),
            ("Ambernath", 73.2010, 19.2020),
            ("Badlapur", 73.2500, 19.1600),
            ("Panvel", 73.1175, 18.9910),
            ("Vasai", 72.8300, 19.3800),
            ("Virar", 72.8100, 19.4700),
        ]
    elif code == "hyd":
        canonical_cities = [
            ("Hyderabad", 78.4867, 17.3850),
            ("Secunderabad", 78.4983, 17.4399),
            ("Cyberabad", 78.3772, 17.4435),
            ("Gachibowli", 78.3489, 17.4401),
            ("Kukatpally", 78.4111, 17.4849),
            ("Patancheru", 78.2642, 17.5332),
            ("Medchal", 78.4814, 17.6297),
            ("Uppal", 78.5602, 17.4022),
            ("Shamshabad", 78.4294, 17.2403),
            ("Malkajgiri", 78.5300, 17.4500),
            ("LB Nagar", 78.5522, 17.3457),
            ("Charminar", 78.4747, 17.3616),
        ]
    elif code == "pnq":
        canonical_cities = [
            ("Pune", 73.8567, 18.5204),
            ("Pimpri-Chinchwad", 73.7997, 18.6279),
            ("Hinjawadi", 73.7389, 18.5913),
            ("Bhosari", 73.8436, 18.6272),
            ("Chakan", 73.8548, 18.7606),
            ("Talegaon", 73.6756, 18.7344),
            ("Hadapsar", 73.9259, 18.5089),
            ("Kharadi", 73.9515, 18.5516),
            ("Kothrud", 73.8077, 18.5074),
            ("Alandi", 73.8967, 18.6758),
            ("Katraj", 73.8652, 18.4529),
            ("Wakad", 73.7628, 18.5987),
            ("Nigdi", 73.7656, 18.6534),
        ]
    else:
        canonical_cities = [
            ("Bengaluru", 77.5946, 12.9716),
            ("Hosur", 77.8253, 12.7409),
            ("Yelahanka", 77.5963, 13.1007),
            ("Whitefield", 77.7499, 12.9698),
            ("Electronic City", 77.6602, 12.8452),
            ("Devanahalli", 77.7047, 13.2467),
            ("Anekal", 77.6994, 12.7086),
            ("Kengeri", 77.4827, 12.9077),
        ]
    next_id = 1
    for cname, lon, lat in canonical_cities:
        mx, my = lonlat_to_mercator_norm(lon, lat)
        label_layers["city_labels"].append(
            (mx, my, {"name": cname, "place": "city", "label_type": "city"}, next_id)
        )
        seen_names["city_labels"].add(cname.lower())
        next_id += 1

    if code == "bom":
        canonical_suburbs = [
            ("Titwala Station", 73.2031, 19.2964),
            ("Ambivali Station", 73.1705, 19.2662),
            ("Kalyan Junction", 73.1352, 19.2444),
            ("Dombivli Station", 73.0880, 19.2180),
            ("Thane Station", 72.9754, 19.1860),
            ("Dadar Station", 72.8430, 19.0178),
            ("CSMT Terminal", 72.8358, 18.9400),
            ("Churchgate Station", 72.8272, 18.9352),
            ("Andheri Station", 72.8467, 19.1197),
            ("Borivali Station", 72.8560, 19.2290),
            ("Goregaon Station", 72.8440, 19.1645),
            ("Manda (Titwala)", 73.2090, 19.2990),
            ("Mahaganapati Temple (Titwala)", 73.2120, 19.3015),
            ("Bandra Kurla Complex (BKC)", 72.8680, 19.0660),
            ("Vashi Station", 72.9986, 19.0655),
            ("Belapur CBD", 73.0410, 19.0210),
            ("Panvel Station", 73.1175, 18.9910),
        ]
        for sname, slon, slat in canonical_suburbs:
            mx, my = lonlat_to_mercator_norm(slon, slat)
            label_layers["suburb_labels"].append(
                (mx, my, {"name": sname, "place": "suburb", "label_type": "suburb"}, next_id)
            )
            seen_names["suburb_labels"].add(sname.lower())
            next_id += 1

    pois = pq.read_table(proc_dir / f"{code}_pois.parquet").to_pydict()
    for i in range(len(pois["osm_id"])):
        place = (pois["place"][i] or "").strip()
        name = (pois["name"][i] or "").strip()
        rw_tag = (pois["railway"][i] or "").strip() if "railway" in pois else ""
        if not name or (not place and not rw_tag):
            continue
        lon = float(pois["lon"][i])
        lat = float(pois["lat"][i])
        mx, my = lonlat_to_mercator_norm(lon, lat)
        nlow = name.lower()

        if rw_tag in ("station", "stop") and "station" not in nlow:
            station_name = f"{name} Station"
            st_low = station_name.lower()
            if st_low not in seen_names["suburb_labels"]:
                seen_names["suburb_labels"].add(st_low)
                label_layers["suburb_labels"].append(
                    (mx, my, {"name": station_name, "place": "station", "label_type": "suburb"}, next_id)
                )
                next_id += 1
            continue

        if place in ("city", "town"):
            if nlow not in seen_names["city_labels"]:
                seen_names["city_labels"].add(nlow)
                label_layers["city_labels"].append(
                    (mx, my, {"name": name, "place": "city", "label_type": "city"}, next_id)
                )
                next_id += 1
        elif place in ("suburb", "quarter"):
            if nlow not in seen_names["suburb_labels"]:
                seen_names["suburb_labels"].add(nlow)
                label_layers["suburb_labels"].append(
                    (
                        mx,
                        my,
                        {"name": name, "place": "suburb", "label_type": "suburb"},
                        next_id,
                    )
                )
                next_id += 1
        elif place in ("neighbourhood", "village", "hamlet", "locality"):
            if nlow not in seen_names["neighborhood_labels"]:
                seen_names["neighborhood_labels"].add(nlow)
                label_layers["neighborhood_labels"].append(
                    (
                        mx,
                        my,
                        {
                            "name": name,
                            "place": "neighbourhood",
                            "label_type": "neighborhood",
                        },
                        next_id,
                    )
                )
                next_id += 1

    return poly_layers, label_layers


def generate_diagnostic_plot(
    tiles_pmtiles_path: Path,
    foundations_pmtiles_path: Path,
    out_png: Path,
    bbox: Dict[str, float] | None = None,
    center_lon: float = 77.595,
    center_lat: float = 12.972,
) -> None:
    """
    Decode real MVT tiles directly from tiles.pmtiles and foundations.pmtiles
    and render a 3-panel verification plot.
    """
    def read_tile_from_pmtiles(pm_path: Path, z: int, x: int, y: int) -> bytes:
        target_tid = zxy_to_tile_id(z, x, y)
        with open(pm_path, "rb") as f:
            hdr = f.read(127)
            root_off, root_len, _, _, leaf_off, leaf_len, data_off, _ = struct.unpack_from(
                "<QQQQQQQQ", hdr, 8
            )
            f.seek(root_off)
            root_b = gzip.decompress(f.read(root_len))

            def parse_dir(b: bytes) -> List[Tuple[int, int, int, int]]:
                p = 0
                def rv(buf: bytes, pos: int) -> Tuple[int, int]:
                    val, shift = 0, 0
                    while True:
                        byte = buf[pos]
                        pos += 1
                        val |= (byte & 0x7F) << shift
                        if not (byte & 0x80):
                            return val, pos
                        shift += 7
                n, p = rv(b, p)
                tids, rls, lens, offs = [], [], [], []
                last = 0
                for _ in range(n):
                    d, p = rv(b, p)
                    last += d
                    tids.append(last)
                for _ in range(n):
                    rl, p = rv(b, p)
                    rls.append(rl)
                for _ in range(n):
                    ln, p = rv(b, p)
                    lens.append(ln)
                for i in range(n):
                    o, p = rv(b, p)
                    offs.append(offs[i - 1] + lens[i - 1] if (o == 0 and i > 0) else o - 1)
                return list(zip(tids, rls, lens, offs))

            entries = parse_dir(root_b)
            if entries and entries[0][1] == 0:
                # Find leaf containing target_tid
                chosen_leaf = None
                for tid, rl, ln, off in entries:
                    if tid <= target_tid:
                        chosen_leaf = (ln, off)
                    else:
                        break
                if chosen_leaf is None:
                    return b""
                f.seek(leaf_off + chosen_leaf[1])
                entries = parse_dir(gzip.decompress(f.read(chosen_leaf[0])))

            for tid, rl, ln, off in entries:
                if tid <= target_tid < tid + rl:
                    f.seek(data_off + off)
                    return gzip.decompress(f.read(ln))
        return b""

    def decode_mvt_polygons(mvt_bytes: bytes) -> Dict[str, List[List[Tuple[int, int]]]]:
        layers: Dict[str, List[List[Tuple[int, int]]]] = defaultdict(list)
        p = 0
        def rv(buf: bytes, pos: int) -> Tuple[int, int]:
            val, shift = 0, 0
            while True:
                byte = buf[pos]
                pos += 1
                val |= (byte & 0x7F) << shift
                if not (byte & 0x80):
                    return val, pos
                shift += 7

        while p < len(mvt_bytes):
            tag, p = rv(mvt_bytes, p)
            ln, p = rv(mvt_bytes, p)
            lbuf = mvt_bytes[p : p + ln]
            p += ln
            lp = 0
            lname = ""
            feats = []
            while lp < len(lbuf):
                ltag, lp = rv(lbuf, lp)
                lfn, lwt = ltag >> 3, ltag & 7
                if lwt == 0:
                    _, lp = rv(lbuf, lp)
                elif lwt == 2:
                    lln, lp = rv(lbuf, lp)
                    sub = lbuf[lp : lp + lln]
                    lp += lln
                    if lfn == 1:
                        lname = sub.decode("utf-8")
                    elif lfn == 2:
                        feats.append(sub)
            for fb in feats:
                fp = 0
                ftype = 0
                gints: List[int] = []
                while fp < len(fb):
                    ftag, fp = rv(fb, fp)
                    ffn, fwt = ftag >> 3, ftag & 7
                    if fwt == 0:
                        v, fp = rv(fb, fp)
                        if ffn == 3:
                            ftype = v
                    elif fwt == 2:
                        fln, fp = rv(fb, fp)
                        fsub = fb[fp : fp + fln]
                        fp += fln
                        if ffn == 4:
                            sp = 0
                            while sp < len(fsub):
                                gv, sp = rv(fsub, sp)
                                gints.append(gv)
                if ftype == 3 and gints:
                    idx = 0
                    cx, cy = 0, 0
                    ring: List[Tuple[int, int]] = []
                    while idx < len(gints):
                        cmd = gints[idx]
                        idx += 1
                        cid, cnt = cmd & 7, cmd >> 3
                        if cid == 1:
                            if len(ring) >= 3:
                                layers[lname].append(ring)
                            ring = []
                            for _ in range(cnt):
                                cx += unzigzag(gints[idx])
                                cy += unzigzag(gints[idx + 1])
                                idx += 2
                                ring.append((cx, cy))
                        elif cid == 2:
                            for _ in range(cnt):
                                cx += unzigzag(gints[idx])
                                cy += unzigzag(gints[idx + 1])
                                idx += 2
                                ring.append((cx, cy))
                        elif cid == 7:
                            if ring:
                                ring.append(ring[0])
                                layers[lname].append(ring)
                                ring = []
        return layers

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

    W_PANEL = 500
    H_PANEL = 500
    GAP = 20
    W_TOTAL = W_PANEL * 3 + GAP * 4
    H_TOTAL = H_PANEL + 60
    rgb = np.full((H_TOTAL, W_TOTAL, 3), (15, 23, 42), dtype=np.uint8)

    def draw_ring_on_panel(
        panel_idx: int,
        ring: List[Tuple[int, int]],
        scale_x: float,
        scale_y: float,
        color: Tuple[int, int, int],
        fill_box: bool = False,
    ) -> None:
        ox = GAP + panel_idx * (W_PANEL + GAP)
        oy = 40
        rgb[oy : oy + H_PANEL, ox : ox + W_PANEL] = np.where(
            rgb[oy : oy + H_PANEL, ox : ox + W_PANEL] == (15, 23, 42),
            np.array([30, 41, 59], dtype=np.uint8),
            rgb[oy : oy + H_PANEL, ox : ox + W_PANEL],
        )
        pxs = [max(0, min(W_PANEL - 1, int(pt[0] * scale_x))) for pt in ring]
        pys = [max(0, min(H_PANEL - 1, int(pt[1] * scale_y))) for pt in ring]
        if fill_box and pxs and pys:
            x0, x1 = min(pxs), max(pxs)
            y0, y1 = min(pys), max(pys)
            rgb[oy + y0 : oy + y1 + 1, ox + x0 : ox + x1 + 1] = color
            return
        for i in range(len(pxs) - 1):
            x0, y0 = pxs[i], pys[i]
            x1, y1 = pxs[i + 1], pys[i + 1]
            steps = max(abs(x1 - x0), abs(y1 - y0), 1)
            xs = np.linspace(x0, x1, steps + 1).astype(np.int32)
            ys = np.linspace(y0, y1, steps + 1).astype(np.int32)
            rgb[oy + ys, ox + xs] = color

    # Initialize panel backgrounds
    for p_i in range(3):
        ox = GAP + p_i * (W_PANEL + GAP)
        rgb[40 : 40 + H_PANEL, ox : ox + W_PANEL] = (30, 41, 59)

    # Panel 0: Zoom 10 overview across study area tiles
    west = bbox["west"] if bbox else 77.40
    south = bbox["south"] if bbox else 12.70
    east = bbox["east"] if bbox else 77.90
    north = bbox["north"] if bbox else 13.25
    z10_x0, z10_y1 = lonlat_to_tile(west, south, 10)
    z10_x1, z10_y0 = lonlat_to_tile(east, north, 10)
    nx10 = max(1, z10_x1 - z10_x0 + 1)
    ny10 = max(1, z10_y1 - z10_y0 + 1)
    colors_rgb = {
        "commercial": (245, 158, 11),
        "parks": (74, 222, 128),
        "water": (56, 189, 248),
        "airports": (192, 132, 252),
    }
    svg_p0: List[str] = []
    for tx in range(z10_x0, z10_x1 + 1):
        for ty in range(z10_y0, z10_y1 + 1):
            mvt = read_tile_from_pmtiles(tiles_pmtiles_path, 10, tx, ty)
            if not mvt:
                continue
            lyrs = decode_mvt_polygons(mvt)
            base_x = (tx - z10_x0) * 4096
            base_y = (ty - z10_y0) * 4096
            for lname, col in colors_rgb.items():
                hex_col = "#{:02x}{:02x}{:02x}".format(*col)
                for ring in lyrs.get(lname, []):
                    shifted = [(base_x + pt[0], base_y + pt[1]) for pt in ring]
                    draw_ring_on_panel(
                        0,
                        shifted,
                        W_PANEL / (nx10 * 4096.0),
                        H_PANEL / (ny10 * 4096.0),
                        col,
                        fill_box=False,
                    )
                    pts_s = " ".join(
                        f"{GAP + int(p[0] * W_PANEL / (nx10 * 4096.0))},{40 + int(p[1] * H_PANEL / (ny10 * 4096.0))}"
                        for p in shifted
                    )
                    svg_p0.append(
                        f'<polygon points="{pts_s}" fill="{hex_col}" fill-opacity="0.55" stroke="{hex_col}" stroke-width="0.6"/>'
                    )

    # Panel 1: Zoom 14 Central tile (Center / CBD)
    cbd_tx, cbd_ty = lonlat_to_tile(center_lon, center_lat, 14)
    mvt14 = read_tile_from_pmtiles(tiles_pmtiles_path, 14, cbd_tx, cbd_ty)
    lyrs14 = decode_mvt_polygons(mvt14)
    svg_p1: List[str] = []
    for lname, col in [
        ("parks", (34, 197, 94)),
        ("water", (56, 189, 248)),
        ("commercial", (245, 158, 11)),
        ("buildings", (226, 232, 240)),
    ]:
        hex_col = "#{:02x}{:02x}{:02x}".format(*col)
        for ring in lyrs14.get(lname, []):
            draw_ring_on_panel(
                1,
                ring,
                W_PANEL / 4096.0,
                H_PANEL / 4096.0,
                col,
                fill_box=(lname == "buildings"),
            )
            pts_s = " ".join(
                f"{GAP * 2 + W_PANEL + int(p[0] * W_PANEL / 4096.0)},{40 + int(p[1] * H_PANEL / 4096.0)}"
                for p in ring
            )
            svg_p1.append(
                f'<polygon points="{pts_s}" fill="{hex_col}" fill-opacity="0.75"/>'
            )

    # Panel 2: Zoom 14 Foundations tile at same CBD coordinate
    fnd14 = read_tile_from_pmtiles(foundations_pmtiles_path, 14, cbd_tx, cbd_ty)
    flyrs14 = decode_mvt_polygons(fnd14)
    svg_p2: List[str] = []
    for ring in flyrs14.get("foundations", []):
        draw_ring_on_panel(
            2, ring, W_PANEL / 4096.0, H_PANEL / 4096.0, (244, 63, 94), fill_box=True
        )
        pts_s = " ".join(
            f"{GAP * 3 + W_PANEL * 2 + int(p[0] * W_PANEL / 4096.0)},{40 + int(p[1] * H_PANEL / 4096.0)}"
            for p in ring
        )
        svg_p2.append('<polygon points="' + pts_s + '" fill="#f43f5e" fill-opacity="0.8"/>')

    out_png.parent.mkdir(parents=True, exist_ok=True)
    write_png_rgb(out_png, rgb)

    out_svg = out_png.with_suffix(".svg")
    svg_doc = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W_TOTAL}" height="{H_TOTAL}" viewBox="0 0 {W_TOTAL} {H_TOTAL}">',
        f'<rect width="{W_TOTAL}" height="{H_TOTAL}" fill="#0f172a"/>',
        f'<rect x="{GAP}" y="40" width="{W_PANEL}" height="{H_PANEL}" fill="#1e293b"/>',
        f'<rect x="{GAP * 2 + W_PANEL}" y="40" width="{W_PANEL}" height="{H_PANEL}" fill="#1e293b"/>',
        f'<rect x="{GAP * 3 + W_PANEL * 2}" y="40" width="{W_PANEL}" height="{H_PANEL}" fill="#1e293b"/>',
        f'<text x="{GAP}" y="26" fill="#f8fafc" font-family="sans-serif" font-size="14" font-weight="bold">tiles.pmtiles @ Zoom 10 Overview</text>',
        f'<text x="{GAP * 2 + W_PANEL}" y="26" fill="#f8fafc" font-family="sans-serif" font-size="14" font-weight="bold">tiles.pmtiles @ z=14/{cbd_tx}/{cbd_ty} ({len(lyrs14.get("buildings", []))} blds)</text>',
        f'<text x="{GAP * 3 + W_PANEL * 2}" y="26" fill="#f8fafc" font-family="sans-serif" font-size="14" font-weight="bold">foundations.pmtiles @ z=14/{cbd_tx}/{cbd_ty} ({len(flyrs14.get("foundations", []))} blds)</text>',
        *svg_p0,
        *svg_p1,
        *svg_p2,
        "</svg>",
    ]
    out_svg.write_text("\n".join(svg_doc), encoding="utf-8")


def main() -> None:
    t_start = time.perf_counter()
    parser = argparse.ArgumentParser(description="Phase 8: Vector tiles generation")
    parser.add_argument("--city", type=Path, default=Path("cities/blr.yaml"), help="Path to city YAML config")
    args = parser.parse_args()

    city_path = args.city if args.city.is_absolute() else (ROOT / args.city)
    with open(city_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    code = cfg["code"].upper()
    bbox = cfg["bbox"]
    init_view = cfg["initialViewState"]
    proc_dir = ROOT / "data" / "processed"
    raw_dir = ROOT / "data" / "raw"
    mod_data_dir = ROOT / "mod" / "data" / code
    reports_dir = ROOT / "reports"
    mod_data_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load or build cached flat building arrays (shared with Phase 9)
    bdata = load_or_build_flat_buildings(proc_dir, cfg)

    # 2. Encode buildings (zooms 12..15) and foundations (zooms 13..15)
    building_layer_by_zxy, foundation_layer_by_zxy, bld_stats = (
        build_buildings_and_foundations_layers(bdata, bbox)
    )

    # 3. Load non-building context polygons & point labels
    poly_layers, label_layers = load_context_polygons_and_labels(
        proc_dir, mod_data_dir, raw_dir, cfg
    )

    # Minimum polygon area (m^2) by zoom so low-zoom overview tiles stay crisp
    poly_min_area = {
        5: 250000.0,
        6: 150000.0,
        7: 80000.0,
        8: 40000.0,
        9: 15000.0,
        10: 5000.0,
        11: 2000.0,
        12: 500.0,
        13: 0.0,
        14: 0.0,
        15: 0.0,
    }

    tiles_mvt_by_id: Dict[int, bytes] = {}
    ocean_foundations_mvt_by_id: Dict[int, bytes] = {}
    layer_feature_counts: Dict[str, int] = defaultdict(int)
    zoom_tile_counts: Dict[str, int] = {}

    for z in range(5, 16):
        n_tiles = 1 << z
        scale = float(n_tiles * 4096)
        tx_min_study, ty_max_study = lonlat_to_tile(bbox["west"], bbox["south"], z)
        tx_max_study, ty_min_study = lonlat_to_tile(bbox["east"], bbox["north"], z)
        min_area = poly_min_area[z]

        # Collect non-building layers per (tx, ty) at zoom z
        tile_layers: Dict[Tuple[int, int], Dict[str, List[Tuple[int | None, int, Dict[str, Any], bytes]]]] = defaultdict(
            lambda: defaultdict(list)
        )

        # 3a. Polygon layers: water, ocean_foundations, parks, commercial, airports
        for lname, poly_list in poly_layers.items():
            for mpts, props, area in poly_list:
                if area < min_area and lname not in ("airports", "water", "ocean_foundations"):
                    continue
                xs_g = [int(round(pt[0] * scale)) for pt in mpts]
                ys_g = [int(round(pt[1] * scale)) for pt in mpts]
                tx0 = max(tx_min_study, min(xs_g) >> 12)
                tx1 = min(tx_max_study, max(xs_g) >> 12)
                ty0 = max(ty_min_study, min(ys_g) >> 12)
                ty1 = min(ty_max_study, max(ys_g) >> 12)
                if tx0 > tx1 or ty0 > ty1:
                    continue

                if tx0 == tx1 and ty0 == ty1:
                    ox = tx0 << 12
                    oy = ty0 << 12
                    lpts = [(x - ox, y - oy) for x, y in zip(xs_g, ys_g)]
                    gb = encode_polygon_ring_mvt(lpts)
                    if gb:
                        tile_layers[(tx0, ty0)][lname].append((None, 3, props, gb))
                        layer_feature_counts[lname] += 1
                else:
                    for tx in range(tx0, tx1 + 1):
                        ox = tx << 12
                        for ty in range(ty0, ty1 + 1):
                            oy = ty << 12
                            lpts = [(x - ox, y - oy) for x, y in zip(xs_g, ys_g)]
                            clipped = clip_polygon_to_box(lpts, -128, -128, 4224, 4224)
                            if len(clipped) >= 3:
                                gb = encode_polygon_ring_mvt(clipped)
                                if gb:
                                    tile_layers[(tx, ty)][lname].append((None, 3, props, gb))
                                    layer_feature_counts[lname] += 1

        # 3b. Label point layers: city_labels, suburb_labels, neighborhood_labels
        for lname, pt_list in label_layers.items():
            if lname == "neighborhood_labels" and z < 10:
                continue
            if lname == "suburb_labels" and z < 8:
                continue
            for mx, my, props, fid in pt_list:
                gx = int(round(mx * scale))
                gy = int(round(my * scale))
                tx = gx >> 12
                ty = gy >> 12
                if tx_min_study <= tx <= tx_max_study and ty_min_study <= ty <= ty_max_study:
                    lx = gx - (tx << 12)
                    ly = gy - (ty << 12)
                    gb = encode_point_mvt(lx, ly)
                    tile_layers[(tx, ty)][lname].append((fid, 1, props, gb))
                    layer_feature_counts[lname] += 1

        # Combine context layers + buildings layer for every active tile at zoom z
        active_xys = set(tile_layers.keys())
        if z >= 12:
            for bz, btx, bty in building_layer_by_zxy.keys():
                if bz == z:
                    active_xys.add((btx, bty))

        z_cnt = 0
        for tx, ty in sorted(active_xys):
            t_parts: List[bytes] = []
            if (tx, ty) in tile_layers:
                for lname in (
                    "water",
                    "ocean_foundations",
                    "parks",
                    "landuse",
                    "commercial",
                    "airports",
                    "city_labels",
                    "suburb_labels",
                    "neighborhood_labels",
                ):
                    f_list = tile_layers[(tx, ty)].get(lname)
                    if f_list:
                        lb = build_mvt_layer(lname, f_list)
                        if lb:
                            t_parts.append(lb)
                            if lname == "ocean_foundations":
                                tid = zxy_to_tile_id(z, tx, ty)
                                ocean_foundations_mvt_by_id[tid] = lb
            if z >= 12:
                bld_lb = building_layer_by_zxy.get((z, tx, ty))
                if bld_lb:
                    t_parts.append(bld_lb)
            if t_parts:
                tid = zxy_to_tile_id(z, tx, ty)
                tiles_mvt_by_id[tid] = b"".join(t_parts)
                z_cnt += 1

        zoom_tile_counts[str(z)] = z_cnt

    layer_feature_counts["buildings"] = sum(
        bld_stats[f"buildings_z{z}"] for z in (12, 13, 14, 15)
    )
    layer_feature_counts["foundations"] = sum(
        bld_stats[f"foundations_z{z}"] for z in (13, 14, 15)
    )

    # 4. Pack foundations.pmtiles (zooms 13..15)
    foundations_mvt_by_id: Dict[int, bytes] = {}
    for (z, tx, ty), flb in foundation_layer_by_zxy.items():
        tid = zxy_to_tile_id(z, tx, ty)
        foundations_mvt_by_id[tid] = flb

    tiles_meta = {
        "name": f"{code} Vector Tiles",
        "format": "pbf",
        "type": "overlay",
        "description": f"Subway Builder MVT tiles for {cfg['name']} ({code})",
        "version": "2",
        "vector_layers": [
            {"id": "water", "description": "", "minzoom": 5, "maxzoom": 15, "fields": {}},
            {"id": "parks", "description": "", "minzoom": 5, "maxzoom": 15, "fields": {"area": "Number"}},
            {
                "id": "landuse",
                "description": "",
                "minzoom": 5,
                "maxzoom": 15,
                "fields": {"kind": "String", "area": "Number"},
            },
            {
                "id": "commercial",
                "description": "",
                "minzoom": 5,
                "maxzoom": 15,
                "fields": {"area": "Number", "type": "String"},
            },
            {"id": "airports", "description": "", "minzoom": 5, "maxzoom": 15, "fields": {}},
            {
                "id": "buildings",
                "description": "",
                "minzoom": 12,
                "maxzoom": 15,
                "fields": {"height": "Number", "id": "Number"},
            },
            {
                "id": "city_labels",
                "description": "",
                "minzoom": 5,
                "maxzoom": 15,
                "fields": {"label_type": "String", "name": "String", "place": "String"},
            },
            {
                "id": "suburb_labels",
                "description": "",
                "minzoom": 5,
                "maxzoom": 15,
                "fields": {"label_type": "String", "name": "String", "place": "String"},
            },
            {
                "id": "neighborhood_labels",
                "description": "",
                "minzoom": 5,
                "maxzoom": 15,
                "fields": {"label_type": "String", "name": "String", "place": "String"},
            },
            {
                "id": "ocean_foundations",
                "description": "",
                "minzoom": 5,
                "maxzoom": 15,
                "fields": {"depth_min": "Number"},
            },
        ],
    }

    foundations_meta = {
        "name": f"{code} Building Foundations",
        "format": "pbf",
        "type": "overlay",
        "description": f"Subway Builder foundation depth tiles for {cfg['name']} ({code})",
        "version": "2",
        "vector_layers": [
            {
                "id": "foundations",
                "description": "",
                "minzoom": 13,
                "maxzoom": 15,
                "fields": {"foundationDepth": "Number"},
            }
        ],
    }

    tiles_pmtiles_path = mod_data_dir / "tiles.pmtiles"
    foundations_pmtiles_path = mod_data_dir / "foundations.pmtiles"
    ocean_pmtiles_path = mod_data_dir / "ocean_foundations.pmtiles"

    tiles_archive_stats = write_pmtiles_v3(
        out_path=tiles_pmtiles_path,
        tiles_by_id=tiles_mvt_by_id,
        metadata=tiles_meta,
        min_zoom=5,
        max_zoom=15,
        bbox=bbox,
        center_lon=float(init_view["longitude"]),
        center_lat=float(init_view["latitude"]),
        center_zoom=15,
    )

    foundations_archive_stats = write_pmtiles_v3(
        out_path=foundations_pmtiles_path,
        tiles_by_id=foundations_mvt_by_id,
        metadata=foundations_meta,
        min_zoom=13,
        max_zoom=15,
        bbox=bbox,
        center_lon=float(init_view["longitude"]),
        center_lat=float(init_view["latitude"]),
        center_zoom=15,
    )

    ocean_foundations_meta = {
        "name": f"{code} Ocean Foundations",
        "format": "pbf",
        "type": "overlay",
        "description": f"Subway Builder ocean depth tiles for {cfg['name']} ({code})",
        "version": "2",
        "vector_layers": [
            {
                "id": "ocean_foundations",
                "description": "",
                "minzoom": 5,
                "maxzoom": 15,
                "fields": {"depth_min": "Number", "depth_max": "Number", "area": "Number"},
            }
        ],
    }

    ocean_archive_stats = write_pmtiles_v3(
        out_path=ocean_pmtiles_path,
        tiles_by_id=ocean_foundations_mvt_by_id,
        metadata=ocean_foundations_meta,
        min_zoom=5,
        max_zoom=15,
        bbox=bbox,
        center_lon=float(init_view["longitude"]),
        center_lat=float(init_view["latitude"]),
        center_zoom=15,
    )

    # Also duplicate for Railyard direct naming (<CODE>.pmtiles, <CODE>_foundations.pmtiles, <CODE>_ocean_foundations.pmtiles)
    import shutil
    shutil.copy2(tiles_pmtiles_path, mod_data_dir / f"{code}.pmtiles")
    shutil.copy2(foundations_pmtiles_path, mod_data_dir / f"{code}_foundations.pmtiles")
    shutil.copy2(ocean_pmtiles_path, mod_data_dir / f"{code}_ocean_foundations.pmtiles")

    # 5. Generate diagnostic preview plot from the written PMTiles archives
    preview_png = reports_dir / "90_tiles_preview.png"
    generate_diagnostic_plot(
        tiles_pmtiles_path,
        foundations_pmtiles_path,
        preview_png,
        bbox=bbox,
        center_lon=float(init_view["longitude"]),
        center_lat=float(init_view["latitude"]),
    )

    elapsed = round(time.perf_counter() - t_start, 2)
    summary = {
        "phase": 8,
        "script": "pipeline/90_tiles.py",
        "city_code": code,
        "status": "PASS",
        "runtime_seconds": elapsed,
        "tiles_pmtiles": {
            "path": str(tiles_pmtiles_path.relative_to(ROOT)).replace("\\", "/"),
            **tiles_archive_stats,
            "min_zoom": 5,
            "max_zoom": 15,
        },
        "foundations_pmtiles": {
            "path": str(foundations_pmtiles_path.relative_to(ROOT)).replace("\\", "/"),
            **foundations_archive_stats,
            "min_zoom": 13,
            "max_zoom": 15,
        },
        "zoom_tile_counts": zoom_tile_counts,
        "layer_feature_counts": dict(layer_feature_counts),
        "building_zoom_breakdown": bld_stats,
        "total_unique_buildings_indexed": int(len(bdata["osm_ids"])),
        "building_id_source": "blr_buildings.parquet osm_id (cached in data/processed/blr_buildings_flat.npz for 100% parity with Phase 9 buildings_index.bin.gz)",
    }

    report_path = reports_dir / "90_tiles.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(
        f"[90_tiles] Completed in {elapsed:.2f}s -> "
        f"tiles.pmtiles ({tiles_archive_stats['file_size_bytes'] / 1e6:.2f} MB, {tiles_archive_stats['num_tiles']} tiles), "
        f"foundations.pmtiles ({foundations_archive_stats['file_size_bytes'] / 1e6:.2f} MB, {foundations_archive_stats['num_tiles']} tiles)"
    )


if __name__ == "__main__":
    main()
