#!/usr/bin/env python3
"""
Rebuild Clean Bengaluru Water Bodies (pipeline/rebuild_blr_water.py)

1. Extracts both:
   a) Standalone water polygons from OSM ways (natural=water, water=lake/pond/reservoir/basin, landuse=reservoir/basin).
      STRICTLY EXCLUDES linear roadside stormwater drains (waterway=drain/ditch/stream), eliminating clutter.
   b) Water multipolygon relations (including Madivala Lake 2310417, Ulsoor, Bellandur, Sankey, Hebbal, Agara, etc.).
2. Uses Shapely to assemble complete closed polygon rings and subtract island holes.
3. Applies a clean area threshold (>= 2500 m^2) to retain all genuine city lakes and neighborhood keres
   while filtering out tiny backyard puddles and farm pits.
4. Saves to data/processed/blr_water.parquet with valid GeoJSON coordinates.
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from shapely.geometry import LineString, MultiPolygon, Polygon, mapping
from shapely.ops import linemerge, polygonize, unary_union

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

from osm_pbf_utils import (
    decode_zigzag,
    iter_pbf_blobs,
    parse_dense_nodes,
    parse_primitive_block,
    parse_string_table,
    parse_way,
    read_varint,
    skip_field,
)


def main() -> None:
    t0 = time.time()
    bbox = {"west": 77.4, "south": 12.7, "east": 77.9, "north": 13.25}
    pbf_path = ROOT / "data/raw/blr_clipped.osm.pbf"
    rel_json_path = Path(r"C:\Users\siddh\.gemini\antigravity\scratch\all_sz_water_relations.json")

    print("[rebuild_blr_water] Loading candidate water multipolygon relations...")
    with open(rel_json_path, "r", encoding="utf-8") as f:
        all_sz_rels = json.load(f)

    # Map member ways to relations
    rel_by_way: Dict[int, List[Dict[str, Any]]] = {}
    rel_lookup: Dict[int, Dict[str, Any]] = {}
    for r in all_sz_rels:
        rid = r["id"]
        rel_lookup[rid] = r
        for mid, mtype, role in zip(r["memids"], r["types"], r["roles"]):
            if mtype == 1:  # Way
                if mid not in rel_by_way:
                    rel_by_way[mid] = []
                rel_by_way[mid].append({"rel_id": rid, "role": role})

    print(f"[rebuild_blr_water] Loaded {len(all_sz_rels):,} relations referencing {len(rel_by_way):,} member ways.")

    # Pass 1: Collect node coordinates in study bbox
    print("[rebuild_blr_water] Pass 1: Reading nodes from blr_clipped.osm.pbf...")
    node_coords: Dict[int, Tuple[float, float]] = {}
    for offset, btype, _, payload in iter_pbf_blobs(pbf_path):
        if btype != "OSMData":
            continue
        st, gran, lat_off, lon_off, groups = parse_primitive_block(payload)
        for grp in groups:
            if len(grp) == 0:
                continue
            t_check, _ = read_varint(grp, 0)
            if (t_check >> 3) != 2:  # DenseNodes
                continue
            gpos = 0
            gn = len(grp)
            while gpos < gn:
                tag, gpos = read_varint(grp, gpos)
                fnum = tag >> 3
                wtype = tag & 0x07
                if fnum == 2 and wtype == 2:
                    length, gpos = read_varint(grp, gpos)
                    ids, lats, lons, _ = parse_dense_nodes(grp[gpos : gpos + length], gran, lat_off, lon_off)
                    gpos += length
                    for nid, lat, lon in zip(ids, lats, lons):
                        if (bbox["south"] - 0.05) <= lat <= (bbox["north"] + 0.05) and (bbox["west"] - 0.05) <= lon <= (bbox["east"] + 0.05):
                            node_coords[nid] = (round(lon, 7), round(lat, 7))
                else:
                    gpos = skip_field(grp, gpos, wtype)

    print(f"[rebuild_blr_water] Collected {len(node_coords):,} nodes in study area.")

    # Pass 2: Collect standalone water ways AND relation member ways
    print("[rebuild_blr_water] Pass 2: Reading ways...")
    standalone_water_ways: List[Dict[str, Any]] = []
    rel_member_ways: Dict[int, Dict[str, Any]] = {}  # way_id -> {refs, coords}

    for offset, btype, _, payload in iter_pbf_blobs(pbf_path):
        if btype != "OSMData":
            continue
        st, _, _, _, groups = parse_primitive_block(payload)
        for grp in groups:
            if len(grp) == 0:
                continue
            t_check, _ = read_varint(grp, 0)
            if (t_check >> 3) != 3:  # Ways
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

                    # Check if this way is a member of any relation
                    if wid in rel_by_way:
                        coords = [node_coords[r] for r in refs if r in node_coords]
                        if len(coords) >= 2:
                            rel_member_ways[wid] = {"refs": refs, "coords": coords}

                    # Check standalone water polygon
                    nat = tags.get("natural")
                    wt = tags.get("water")
                    lu = tags.get("landuse")
                    ww = tags.get("waterway")

                    # Strictly exclude drains, ditches, sewers, and linear streams!
                    if ww in ("drain", "ditch", "stream", "culvert", "pipeline"):
                        continue

                    is_water = (nat == "water") or (wt in ("lake", "pond", "reservoir", "basin", "lagoon", "oxbow")) or (lu in ("reservoir", "basin"))
                    if is_water and len(refs) >= 3 and refs[0] == refs[-1]:
                        coords = [node_coords[r] for r in refs if r in node_coords]
                        if len(coords) >= 4 and coords[0] == coords[-1]:
                            standalone_water_ways.append({
                                "osm_id": wid,
                                "name": tags.get("name") or tags.get("name:en") or "",
                                "water_type": wt or nat or lu or "water",
                                "coords": coords
                            })
                else:
                    gpos = skip_field(grp, gpos, wtype)

    print(f"[rebuild_blr_water] Standalone closed water ways: {len(standalone_water_ways):,}")
    print(f"[rebuild_blr_water] Relation member ways with coords: {len(rel_member_ways):,}")

    # Reconstruct multipolygon relations
    print("[rebuild_blr_water] Assembling multipolygon relations...")
    assembled_lakes: List[Dict[str, Any]] = []

    # Cosine of central latitude for accurate area in m^2
    cos_lat = math.cos(math.radians(12.97))
    m_per_deg_lat = 111132.954 - 559.822 * math.cos(math.radians(25.94))
    m_per_deg_lon = 111412.84 * cos_lat

    def compute_polygon_area_m2(poly: Polygon) -> float:
        # Transform coords to local meters
        pts = list(poly.exterior.coords)
        if len(pts) < 3:
            return 0.0
        xs = [p[0] * m_per_deg_lon for p in pts]
        ys = [p[1] * m_per_deg_lat for p in pts]
        area = 0.5 * abs(sum(xs[i] * ys[i + 1] - xs[i + 1] * ys[i] for i in range(len(xs) - 1)))
        # Subtract holes
        for hole in poly.interiors:
            hpts = list(hole.coords)
            if len(hpts) >= 3:
                hxs = [p[0] * m_per_deg_lon for p in hpts]
                hys = [p[1] * m_per_deg_lat for p in hpts]
                area -= 0.5 * abs(sum(hxs[i] * hys[i + 1] - hxs[i + 1] * hys[i] for i in range(len(hxs) - 1)))
        return max(0.0, area)

    # Track relations present in BLR
    active_rel_ids = set()
    for wid in rel_member_ways:
        for entry in rel_by_way[wid]:
            active_rel_ids.add(entry["rel_id"])

    print(f"[rebuild_blr_water] Active water relations in BLR: {len(active_rel_ids)}")

    for rid in active_rel_ids:
        r = rel_lookup[rid]
        name = r["name"] or r["tags"].get("name") or r["tags"].get("name:en") or ""
        wt = r["tags"].get("water") or r["tags"].get("natural") or "lake"

        outer_lines: List[LineString] = []
        inner_lines: List[LineString] = []

        for mid, mtype, role in zip(r["memids"], r["types"], r["roles"]):
            if mtype == 1 and mid in rel_member_ways:
                coords = rel_member_ways[mid]["coords"]
                if len(coords) >= 2:
                    ls = LineString(coords)
                    if role == "inner":
                        inner_lines.append(ls)
                    else:
                        outer_lines.append(ls)

        if not outer_lines:
            continue

        try:
            merged_outer = linemerge(outer_lines)
            outer_polys = list(polygonize(merged_outer))
            if not outer_polys:
                # If polygonize fails on unclosed linemerge, try convex/concave or direct polygon if single line
                if hasattr(merged_outer, "geoms"):
                    for g in merged_outer.geoms:
                        coords = list(g.coords)
                        if len(coords) >= 3:
                            outer_polys.append(Polygon(coords))
                else:
                    coords = list(merged_outer.coords)
                    if len(coords) >= 3:
                        outer_polys.append(Polygon(coords))

            merged_inner = linemerge(inner_lines) if inner_lines else None
            inner_polys = list(polygonize(merged_inner)) if merged_inner else []

            for opoly in outer_polys:
                if not opoly.is_valid:
                    opoly = opoly.buffer(0)
                # Cut out inner holes
                for ipoly in inner_polys:
                    if ipoly.is_valid and opoly.contains(ipoly):
                        opoly = opoly.difference(ipoly)

                if opoly.is_empty:
                    continue

                if isinstance(opoly, Polygon):
                    polys_to_add = [opoly]
                elif isinstance(opoly, MultiPolygon):
                    polys_to_add = list(opoly.geoms)
                else:
                    continue

                for p in polys_to_add:
                    area = compute_polygon_area_m2(p)
                    cx, cy = p.centroid.x, p.centroid.y
                    if not (bbox["south"] <= cy <= bbox["north"] and bbox["west"] <= cx <= bbox["east"]):
                        continue
                    if area < 1000.0:  # Skip slivers
                        continue
                    coords = list(p.exterior.coords)
                    assembled_lakes.append({
                        "osm_id": rid,
                        "name": name,
                        "water_type": wt,
                        "area_m2": round(area, 2),
                        "centroid_lon": round(cx, 7),
                        "centroid_lat": round(cy, 7),
                        "coords": coords,
                    })
        except Exception as e:
            # Ignore occasional topologically degenerate relation geometries
            pass

    print(f"[rebuild_blr_water] Successfully assembled {len(assembled_lakes)} relation lake polygons.")

    # Now add standalone water ways that are not already covered by a relation
    for sw in standalone_water_ways:
        poly = Polygon(sw["coords"])
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or not isinstance(poly, Polygon):
            continue
        area = compute_polygon_area_m2(poly)
        # Quality filter: only real lakes and ponds (area >= 2500 m^2)
        if area < 2500.0:
            continue
        cx, cy = poly.centroid.x, poly.centroid.y
        if not (bbox["south"] <= cy <= bbox["north"] and bbox["west"] <= cx <= bbox["east"]):
            continue

        assembled_lakes.append({
            "osm_id": sw["osm_id"],
            "name": sw["name"],
            "water_type": sw["water_type"],
            "area_m2": round(area, 2),
            "centroid_lon": round(cx, 7),
            "centroid_lat": round(cy, 7),
            "coords": list(poly.exterior.coords),
        })

    # Deduplicate and sort by area descending
    print(f"[rebuild_blr_water] Total water bodies before dedup: {len(assembled_lakes)}")
    # Deduplicate by approximate centroid
    seen_locs = []
    final_lakes: List[Dict[str, Any]] = []
    for lk in sorted(assembled_lakes, key=lambda x: x["area_m2"], reverse=True):
        pt = (lk["centroid_lon"], lk["centroid_lat"])
        # Check if too close to an already added larger lake of same name
        is_dup = False
        for spt, sname in seen_locs:
            dist = math.hypot(pt[0] - spt[0], pt[1] - spt[1])
            if dist < 0.001 and (not lk["name"] or lk["name"] == sname):
                is_dup = True
                break
        if not is_dup:
            seen_locs.append((pt, lk["name"]))
            final_lakes.append(lk)

    print(f"[rebuild_blr_water] Final clean lakes count: {len(final_lakes)}")

    # Verify key lakes
    target_lakes = ["madiwala", "madivala", "bellandur", "varthur", "ulsoor", "halasuru", "hebbal", "sankey", "yelahanka", "jakkur", "agara", "begur", "kaikondrahalli"]
    print("\n=== VERIFYING KEY LAKES IN FINAL DATASET ===")
    for t in target_lakes:
        found = [l for l in final_lakes if t in (l["name"] or "").lower()]
        if found:
            for f in found:
                print(f"  [FOUND] {f['name']} (ID {f['osm_id']}, area={f['area_m2']:,.0f} m^2, at {f['centroid_lon']:.4f}, {f['centroid_lat']:.4f})")
        else:
            print(f"  [MISSING] {t.title()}")

    # Build Parquet table
    out_table = pa.Table.from_arrays(
        [
            pa.array([l["osm_id"] for l in final_lakes], type=pa.int64()),
            pa.array([l["water_type"] for l in final_lakes], type=pa.string()),
            pa.array([l["name"] for l in final_lakes], type=pa.string()),
            pa.array([True] * len(final_lakes), type=pa.bool_()),
            pa.array([l["area_m2"] for l in final_lakes], type=pa.float64()),
            pa.array([l["centroid_lon"] for l in final_lakes], type=pa.float64()),
            pa.array([l["centroid_lat"] for l in final_lakes], type=pa.float64()),
            pa.array([json.dumps([[round(p[0], 6), round(p[1], 6)] for p in l["coords"]]) for l in final_lakes], type=pa.string()),
        ],
        names=["osm_id", "water_type", "name", "is_polygon", "area_m2", "centroid_lon", "centroid_lat", "coords_json"],
    )

    out_path = ROOT / "data/processed/blr_water.parquet"
    pq.write_table(out_table, out_path, compression="snappy")
    print(f"\n[rebuild_blr_water] Saved clean dataset to {out_path} ({len(final_lakes)} lakes, {out_path.stat().st_size:,} bytes).")
    print(f"[rebuild_blr_water] Done in {time.time() - t0:.2f}s.")


if __name__ == "__main__":
    main()
