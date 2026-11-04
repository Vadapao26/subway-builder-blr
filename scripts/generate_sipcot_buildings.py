import json
import math
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from shapely.geometry import Polygon, MultiPolygon, Point, box
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parent.parent
PROC_DIR = ROOT / "data" / "processed"

def generate_buildings_for_parcels():
    print("[SIPCOT Gen] Loading existing datasets...")
    bldg_df = pd.read_parquet(PROC_DIR / "blr_buildings.parquet")
    lu_df = pd.read_parquet(PROC_DIR / "blr_landuse.parquet")
    
    # We focus on the south-east peripheral zone (Hosur SIPCOT, Attibele, Anekal, Sarjapura outer)
    # lat <= 12.86, lon >= 77.65
    se_mask = (lu_df["centroid_lat"] <= 12.86) & (lu_df["centroid_lon"] >= 77.65)
    se_lu = lu_df[se_mask & (lu_df["area_m2"] >= 5000)].copy()
    print(f"[SIPCOT Gen] Found {len(se_lu)} large parcels in South-East sector.")
    
    # Index existing buildings in this bounding box for fast collision check
    bldg_se = bldg_df[(bldg_df["centroid_lat"] <= 12.88) & (bldg_df["centroid_lon"] >= 77.63)]
    print(f"[SIPCOT Gen] Indexing {len(bldg_se)} existing buildings in SE sector...")
    existing_boxes = [box(minx, miny, maxx, maxy) for minx, miny, maxx, maxy in zip(bldg_se["min_lon"], bldg_se["min_lat"], bldg_se["max_lon"], bldg_se["max_lat"])]
    existing_tree = STRtree(existing_boxes) if existing_boxes else None
    
    m_per_deg_lat = 111132.0
    cos_lat = math.cos(math.radians(12.78))
    m_per_deg_lon = 111320.0 * cos_lat
    
    dlat_1km = 1000.0 / m_per_deg_lat
    dlon_1km = 1000.0 / m_per_deg_lon
    south = 12.70
    west = 77.40
    
    new_buildings = []
    current_osm_id = 9900000001
    
    rng = np.random.RandomState(42)
    
    for idx, r in se_lu.iterrows():
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
        
        # Determine typology based on category
        if cat == "industrial":
            # Industrial factory sheds: ~50m-80m by ~30m-45m
            b_w_m = rng.uniform(35.0, 50.0)
            b_l_m = rng.uniform(55.0, 85.0)
            gap_m = rng.uniform(25.0, 35.0)
            levels = rng.choice([2.0, 2.0, 3.0], p=[0.6, 0.3, 0.1])
            height = levels * 4.5
            btype = "industrial"
        elif cat in ("commercial", "retail"):
            b_w_m = rng.uniform(30.0, 42.0)
            b_l_m = rng.uniform(40.0, 60.0)
            gap_m = rng.uniform(20.0, 30.0)
            levels = rng.choice([3.0, 4.0, 5.0])
            height = levels * 3.8
            btype = "commercial"
        elif cat in ("school", "college", "university", "institutional", "hospital"):
            b_w_m = rng.uniform(35.0, 48.0)
            b_l_m = rng.uniform(45.0, 65.0)
            gap_m = rng.uniform(25.0, 35.0)
            levels = rng.choice([3.0, 4.0])
            height = levels * 3.8
            btype = "institutional"
        elif cat == "residential":
            b_w_m = rng.uniform(14.0, 20.0)
            b_l_m = rng.uniform(18.0, 28.0)
            gap_m = rng.uniform(14.0, 20.0)
            levels = rng.choice([2.0, 2.0, 3.0, 4.0], p=[0.5, 0.3, 0.15, 0.05])
            height = levels * 3.2
            btype = "residential"
        else:
            continue
            
        step_x_deg = (b_l_m + gap_m) / m_per_deg_lon
        step_y_deg = (b_w_m + gap_m) / m_per_deg_lat
        half_w_deg = (b_w_m * 0.5) / m_per_deg_lat
        half_l_deg = (b_l_m * 0.5) / m_per_deg_lon
        
        # Grid sample points inside polygon
        xs = np.arange(min_x + step_x_deg * 0.5, max_x, step_x_deg)
        ys = np.arange(min_y + step_y_deg * 0.5, max_y, step_y_deg)
        
        # Jitter slightly for organic layout
        for cy in ys:
            for cx in xs:
                # Add small jitter (up to 4 meters)
                jx = cx + rng.uniform(-4.0, 4.0) / m_per_deg_lon
                jy = cy + rng.uniform(-4.0, 4.0) / m_per_deg_lat
                
                # Check center point is strictly inside parcel polygon
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
                    
                # Collision check against existing buildings
                if existing_tree is not None:
                    matches = existing_tree.query(b_box)
                    if len(matches) > 0:
                        continue
                        
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
                
    print(f"[SIPCOT Gen] Generated {len(new_buildings):,} realistic building footprints!")
    if not new_buildings:
        return
        
    new_df = pd.DataFrame(new_buildings)
    
    # Save a backup of original blr_buildings.parquet if not already backed up
    orig_backup = PROC_DIR / "blr_buildings_osm_orig.parquet"
    if not orig_backup.exists():
        import shutil
        shutil.copy2(PROC_DIR / "blr_buildings.parquet", orig_backup)
        print(f"[SIPCOT Gen] Backed up original buildings to {orig_backup.name}")
        
    combined_df = pd.concat([bldg_df, new_df], ignore_index=True)
    combined_df.to_parquet(PROC_DIR / "blr_buildings.parquet", index=False)
    print(f"[SIPCOT Gen] Successfully updated blr_buildings.parquet: {len(bldg_df):,} -> {len(combined_df):,} buildings.")

if __name__ == "__main__":
    generate_buildings_for_parcels()
