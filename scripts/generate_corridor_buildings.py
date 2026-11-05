#!/usr/bin/env python3
"""
Generate realistic 3D building footprints for peripheral corridors and sparse demand points in Bengaluru:
1. North corridor (Devanahalli, Kempegowda Airport Aerospace Park, KIADB, Bagalur, Doddaballapura road).
2. South corridor (Anekal town, Attibele, Jigani, Bommasandra outer, Sarjapura outer).
3. Sparse demand points across the map (where population/jobs exist but OSM lacked mapped buildings).

Ensures all generated buildings:
- Do not collide with existing buildings
- Do not collide with water bodies or lakes
- Are sized realistically (>= 120m² for residential, >= 600m² for industrial/commercial)
  so they render cleanly across game zooms 12..15 in BLR.pmtiles and buildings_index.bin.gz.
"""

import json
import math
import sys
from pathlib import Path
import numpy as np
import pandas as pd
from shapely.geometry import Polygon, MultiPolygon, Point, box
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parent.parent
PROC_DIR = ROOT / "data" / "processed"

def generate_corridor_and_point_buildings():
    print("[Corridor Gen] Loading existing datasets...", flush=True)
    bldg_df = pd.read_parquet(PROC_DIR / "blr_buildings.parquet")
    lu_df = pd.read_parquet(PROC_DIR / "blr_landuse.parquet")
    pts_df = pd.read_parquet(PROC_DIR / "blr_demand_points.parquet")
    water_df = pd.read_parquet(PROC_DIR / "blr_water.parquet")
    
    print(f"[Corridor Gen] Initial buildings count: {len(bldg_df):,}", flush=True)
    
    # 1. Build spatial index of existing buildings
    print("[Corridor Gen] Building spatial index of existing structures...", flush=True)
    b_boxes = [box(minx, miny, maxx, maxy) for minx, miny, maxx, maxy in zip(bldg_df["min_lon"], bldg_df["min_lat"], bldg_df["max_lon"], bldg_df["max_lat"])]
    b_tree = STRtree(b_boxes)
    
    # 2. Build spatial index of water bodies to prevent any building in water
    print("[Corridor Gen] Indexing water bodies for collision avoidance...", flush=True)
    w_polys = []
    for _, wr in water_df.iterrows():
        try:
            coords = json.loads(wr["coords_json"])
            if len(coords) >= 3:
                wp = Polygon(coords)
                if wp.is_valid and not wp.is_empty:
                    w_polys.append(wp)
        except Exception:
            pass
    w_tree = STRtree(w_polys) if w_polys else None
    
    m_per_deg_lat = 111132.0
    cos_lat = math.cos(math.radians(12.97))
    m_per_deg_lon = 111320.0 * cos_lat
    
    dlat_1km = 1000.0 / m_per_deg_lat
    dlon_1km = 1000.0 / m_per_deg_lon
    south = 12.70
    west = 77.40
    
    new_buildings = []
    max_osm_id = int(bldg_df["osm_id"].max())
    current_osm_id = max(max_osm_id + 1, 9920000001)
    
    rng = np.random.RandomState(1337)
    
    # -------------------------------------------------------------
    # PART 1: Landuse Parcels in North & South Corridors
    # -------------------------------------------------------------
    # North: lat >= 13.08, lon 77.50 to 77.88
    # South: lat <= 12.86, lon 77.60 to 77.88
    n_mask = (lu_df["centroid_lat"] >= 13.08) & (lu_df["centroid_lon"] >= 77.50) & (lu_df["centroid_lon"] <= 77.88)
    s_mask = (lu_df["centroid_lat"] <= 12.86) & (lu_df["centroid_lon"] >= 77.60) & (lu_df["centroid_lon"] <= 77.88)
    target_lu = lu_df[(n_mask | s_mask) & (lu_df["area_m2"] >= 4000)].copy()
    print(f"[Corridor Gen] Found {len(target_lu):,} large landuse parcels in North & South corridors.", flush=True)
    
    # Track all newly placed boxes in memory
    placed_boxes = []
    
    for _, r in target_lu.iterrows():
        cat = r["category"]
        pname = r["name"] or ""
        area_m2 = r["area_m2"]
        coords = json.loads(r["coords_json"])
        if len(coords) < 3:
            continue
        try:
            poly = Polygon(coords)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.area <= 0:
                continue
        except Exception:
            continue
            
        min_x, min_y, max_x, max_y = poly.bounds
        
        # Dimensions & typology
        if cat == "industrial":
            b_w_m = rng.uniform(35.0, 50.0)
            b_l_m = rng.uniform(55.0, 85.0)
            gap_m = rng.uniform(22.0, 32.0)
            levels = rng.choice([2.0, 2.0, 3.0], p=[0.6, 0.3, 0.1])
            height = levels * 4.5
            btype = "industrial"
        elif cat in ("commercial", "retail"):
            b_w_m = rng.uniform(25.0, 38.0)
            b_l_m = rng.uniform(35.0, 55.0)
            gap_m = rng.uniform(18.0, 28.0)
            levels = rng.choice([3.0, 4.0, 5.0])
            height = levels * 3.8
            btype = "commercial"
        elif cat in ("school", "college", "university", "institutional", "hospital"):
            b_w_m = rng.uniform(30.0, 42.0)
            b_l_m = rng.uniform(40.0, 60.0)
            gap_m = rng.uniform(20.0, 30.0)
            levels = rng.choice([3.0, 4.0])
            height = levels * 3.8
            btype = "institutional"
        elif cat == "residential":
            b_w_m = rng.uniform(15.0, 22.0)
            b_l_m = rng.uniform(20.0, 32.0)
            gap_m = rng.uniform(14.0, 22.0)
            levels = rng.choice([2.0, 2.0, 3.0, 4.0], p=[0.45, 0.35, 0.15, 0.05])
            height = levels * 3.2
            btype = "residential"
        else:
            continue
            
        step_x_deg = (b_l_m + gap_m) / m_per_deg_lon
        step_y_deg = (b_w_m + gap_m) / m_per_deg_lat
        half_w_deg = (b_w_m * 0.5) / m_per_deg_lat
        half_l_deg = (b_l_m * 0.5) / m_per_deg_lon
        
        xs = np.arange(min_x + step_x_deg * 0.5, max_x, step_x_deg)
        ys = np.arange(min_y + step_y_deg * 0.5, max_y, step_y_deg)
        
        for cy in ys:
            for cx in xs:
                jx = cx + rng.uniform(-3.0, 3.0) / m_per_deg_lon
                jy = cy + rng.uniform(-3.0, 3.0) / m_per_deg_lat
                pt = Point(jx, jy)
                if not poly.contains(pt):
                    continue
                    
                b_min_lon = jx - half_l_deg
                b_max_lon = jx + half_l_deg
                b_min_lat = jy - half_w_deg
                b_max_lat = jy + half_w_deg
                b_box = box(b_min_lon, b_min_lat, b_max_lon, b_max_lat)
                if not poly.contains(b_box):
                    continue
                    
                # Collision check vs water
                if w_tree is not None and len(w_tree.query(b_box)) > 0:
                    continue
                # Collision check vs existing buildings
                if len(b_tree.query(b_box)) > 0:
                    continue
                # Collision check vs freshly placed buildings
                if any(b_box.intersects(pb) for pb in placed_boxes[-40:]):
                    continue
                    
                placed_boxes.append(b_box)
                b_area_m2 = round(b_w_m * b_l_m, 2)
                r_idx = min(60, max(0, int((jy - south) / dlat_1km)))
                c_idx = min(60, max(0, int((jx - west) / dlon_1km)))
                coords_box = [
                    [round(b_min_lon, 6), round(b_min_lat, 6)],
                    [round(b_max_lon, 6), round(b_min_lat, 6)],
                    [round(b_max_lon, 6), round(b_max_lat, 6)],
                    [round(b_min_lon, 6), round(b_max_lat, 6)],
                    [round(b_min_lon, 6), round(b_min_lat, 6)],
                ]
                b_name = f"{pname} Unit" if pname else ""
                new_buildings.append({
                    "osm_id": current_osm_id,
                    "building": btype,
                    "levels": float(levels),
                    "height": float(height),
                    "name": b_name,
                    "amenity": "",
                    "shop": "",
                    "office": "",
                    "area_m2": b_area_m2,
                    "min_lon": round(b_min_lon, 6),
                    "min_lat": round(b_min_lat, 6),
                    "max_lon": round(b_max_lon, 6),
                    "max_lat": round(b_max_lat, 6),
                    "centroid_lon": round(jx, 6),
                    "centroid_lat": round(jy, 6),
                    "row": r_idx,
                    "col": c_idx,
                    "coords_json": json.dumps(coords_box),
                })
                current_osm_id += 1
                
    print(f"[Corridor Gen] Generated {len(new_buildings):,} buildings from corridor landuse parcels.", flush=True)
    
    # -------------------------------------------------------------
    # PART 2: Sparse Demand Points (where population exists but OSM lacked buildings)
    # -------------------------------------------------------------
    # Update temporary tree with placed boxes so far
    all_boxes = b_boxes + placed_boxes
    temp_tree = STRtree(all_boxes)
    
    ddeg = 250.0 / m_per_deg_lat
    sparse_points = []
    for _, pt in pts_df.iterrows():
        res = pt["residents"]
        jobs = pt["jobs"]
        if res < 150 and jobs < 150:
            continue
        pbox = box(pt["lon"] - ddeg, pt["lat"] - ddeg, pt["lon"] + ddeg, pt["lat"] + ddeg)
        matches = temp_tree.query(pbox)
        if len(matches) < 6:
            sparse_points.append(pt)
            
    print(f"[Corridor Gen] Found {len(sparse_points):,} sparse demand points requiring localized clusters.", flush=True)
    
    pt_bld_count = 0
    for pt in sparse_points:
        plon = pt["lon"]
        plat = pt["lat"]
        res = pt["residents"]
        jobs = pt["jobs"]
        
        # Decide cluster composition:
        # If jobs > res * 1.5 -> commercial/industrial
        # Else -> residential village/neighborhood
        num_units = rng.randint(8, 16)
        
        for k in range(num_units):
            angle = rng.uniform(0, 2 * math.pi)
            dist_m = rng.uniform(40.0, 220.0)
            
            bx_lon = plon + (dist_m * math.cos(angle)) / m_per_deg_lon
            bx_lat = plat + (dist_m * math.sin(angle)) / m_per_deg_lat
            
            if jobs > res * 1.5:
                bw = rng.uniform(24.0, 35.0)
                bl = rng.uniform(35.0, 55.0)
                levels = rng.choice([2.0, 3.0, 4.0])
                btype = "commercial" if rng.rand() > 0.4 else "industrial"
                height = levels * 3.8
            else:
                bw = rng.uniform(14.0, 20.0)
                bl = rng.uniform(18.0, 28.0)
                levels = rng.choice([2.0, 2.0, 3.0], p=[0.6, 0.3, 0.1])
                btype = "residential"
                height = levels * 3.2
                
            hw = (bw * 0.5) / m_per_deg_lat
            hl = (bl * 0.5) / m_per_deg_lon
            b_box = box(bx_lon - hl, bx_lat - hw, bx_lon + hl, bx_lat + hw)
            
            # Check collision with water
            if w_tree is not None and len(w_tree.query(b_box)) > 0:
                continue
            # Check collision with existing buildings
            if len(temp_tree.query(b_box)) > 0:
                continue
            # Check collision with newly placed
            if any(b_box.intersects(pb) for pb in placed_boxes[-30:]):
                continue
                
            placed_boxes.append(b_box)
            b_area_m2 = round(bw * bl, 2)
            r_idx = min(60, max(0, int((bx_lat - south) / dlat_1km)))
            c_idx = min(60, max(0, int((bx_lon - west) / dlon_1km)))
            coords_box = [
                [round(bx_lon - hl, 6), round(bx_lat - hw, 6)],
                [round(bx_lon + hl, 6), round(bx_lat - hw, 6)],
                [round(bx_lon + hl, 6), round(bx_lat + hw, 6)],
                [round(bx_lon - hl, 6), round(bx_lat + hw, 6)],
                [round(bx_lon - hl, 6), round(bx_lat - hw, 6)],
            ]
            new_buildings.append({
                "osm_id": current_osm_id,
                "building": btype,
                "levels": float(levels),
                "height": float(height),
                "name": "",
                "amenity": "",
                "shop": "",
                "office": "",
                "area_m2": b_area_m2,
                "min_lon": round(bx_lon - hl, 6),
                "min_lat": round(bx_lat - hw, 6),
                "max_lon": round(bx_lon + hl, 6),
                "max_lat": round(bx_lat + hw, 6),
                "centroid_lon": round(bx_lon, 6),
                "centroid_lat": round(bx_lat, 6),
                "row": r_idx,
                "col": c_idx,
                "coords_json": json.dumps(coords_box),
            })
            current_osm_id += 1
            pt_bld_count += 1
            
    print(f"[Corridor Gen] Generated {pt_bld_count:,} buildings for sparse demand points.", flush=True)
    print(f"[Corridor Gen] Total new buildings created: {len(new_buildings):,}", flush=True)
    
    if not new_buildings:
        return
        
    new_df = pd.DataFrame(new_buildings)
    
    # Save a backup of blr_buildings.parquet before updating
    orig_backup = PROC_DIR / "blr_buildings_pre_corridor.parquet"
    if not orig_backup.exists():
        import shutil
        shutil.copy2(PROC_DIR / "blr_buildings.parquet", orig_backup)
        print(f"[Corridor Gen] Backed up buildings to {orig_backup.name}", flush=True)
        
    combined_df = pd.concat([bldg_df, new_df], ignore_index=True)
    combined_df.to_parquet(PROC_DIR / "blr_buildings.parquet", index=False)
    print(f"[Corridor Gen] Updated blr_buildings.parquet: {len(bldg_df):,} -> {len(combined_df):,} buildings.", flush=True)
    
    # Invalidate cached flat buildings npz so 90_tiles and 95_buildings will re-index all buildings
    flat_npz = PROC_DIR / "blr_buildings_flat.npz"
    if flat_npz.exists():
        flat_npz.unlink()
        print(f"[Corridor Gen] Cleared {flat_npz.name} for full vector tile re-index.", flush=True)

if __name__ == "__main__":
    generate_corridor_and_point_buildings()
