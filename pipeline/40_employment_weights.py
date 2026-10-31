#!/usr/bin/env python3
"""
Phase 4 (Part 1): Synthetic Employment Weights per Demand Point.
Tasks:
1. Compute an employment weight per grid cell from OSM features:
   - Building floor/footprint area of office, commercial, retail, industrial, and institutional
     (education, health, government, transport) buildings (`blr_buildings.parquet`).
   - Landuse & airport footprint area (`blr_landuse.parquet`, `blr_airports.parquet`), with exact
     multi-cell polygon intersection for large campuses/estates (>= 2.5 ha) and a configurable
     multiplier for OSM-named employment polygons.
   - Named employment POIs (`blr_pois.parquet`).
2. Add configured named boosts (`params.named_employment_boosts` in `cities/blr.yaml`) for known
   employment corridors (Airport, Electronic City, Whitefield/ITPL, ORR, CBD, Peenya, Hosur SIPCOT),
   while also recording all OSM-derived named employment clusters.
3. Enforce sanity check invariant: no cell receives positive employment weight unless it has
   plausible employment landuse/buildings/POIs (`has_plausible_employment_landuse`) OR a named boost (`has_named_boost`).
4. Save `data/processed/employment_weights.parquet` (and `.csv`) and `reports/40_employment_weights.json`.
"""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time

import numpy as np
import pandas as pd
from shapely.geometry import Polygon, box
import yaml


OFFICE_BLDG_TYPES = {"office"}
COMMERCIAL_BLDG_TYPES = {"commercial", "hotel", "warehouse", "service", "kiosk", "bank"}
RETAIL_BLDG_TYPES = {"retail", "supermarket", "mall", "shop"}
INDUSTRIAL_BLDG_TYPES = {"industrial", "manufacture", "factory", "hangar", "garages", "storage_tank"}
INSTITUTIONAL_BLDG_TYPES = {
    "school",
    "college",
    "university",
    "hospital",
    "public",
    "civic",
    "government",
    "train_station",
    "transportation",
    "kindergarten",
    "fire_station",
}

COMMERCIAL_AMENITIES = {
    "bank",
    "restaurant",
    "cafe",
    "fast_food",
    "fuel",
    "cinema",
    "theatre",
    "marketplace",
    "pharmacy",
    "bar",
    "pub",
    "food_court",
    "car_rental",
    "car_wash",
    "veterinary",
}
INSTITUTIONAL_AMENITIES = {
    "school",
    "college",
    "university",
    "hospital",
    "clinic",
    "doctors",
    "townhall",
    "courthouse",
    "police",
    "fire_station",
    "post_office",
    "library",
    "research_institute",
    "public_building",
    "community_centre",
    "bus_station",
    "Social_facility",
    "nursing_home",
    "dentist",
    "kindergarten",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4 (Part 1): Compute synthetic employment weights per cell.")
    parser.add_argument("--city", type=Path, default=Path("cities/blr.yaml"), help="Path to city YAML config")
    args = parser.parse_args()

    t_start = time.time()
    repo_root = Path(__file__).resolve().parent.parent
    city_path = args.city if args.city.is_absolute() else (repo_root / args.city)
    with open(city_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    city_code = cfg["code"].lower()
    bbox = cfg["bbox"]
    south, north = float(bbox["south"]), float(bbox["north"])
    west, east = float(bbox["west"]), float(bbox["east"])

    mid_lat = 0.5 * (south + north)
    cos_lat = math.cos(math.radians(mid_lat))
    m_per_deg_lat = 111132.0
    m_per_deg_lon = 111320.0 * cos_lat
    deg2_to_m2 = m_per_deg_lat * m_per_deg_lon

    params = cfg["params"]
    grid_cell_m = float(params.get("grid_cell_m", 1000))
    dlat = grid_cell_m / m_per_deg_lat
    dlon = grid_cell_m / m_per_deg_lon
    rows = int(math.ceil((north - south) / dlat))
    cols = int(math.ceil((east - west) / dlon))

    weights_cfg = params["employment_weights"]
    w_office = float(weights_cfg.get("office", 0.08))
    w_comm = float(weights_cfg.get("commercial", 0.05))
    w_retail = float(weights_cfg.get("retail", 0.04))
    w_ind = float(weights_cfg.get("industrial", 0.03))
    w_inst = float(weights_cfg.get("institutional", 0.03))
    w_lu_comm = float(weights_cfg.get("landuse_commercial", 0.015))
    w_lu_ind = float(weights_cfg.get("landuse_industrial", 0.010))
    w_lu_retail = float(weights_cfg.get("landuse_retail", 0.015))
    w_lu_inst = float(weights_cfg.get("landuse_institutional", 0.010))
    w_lu_air = float(weights_cfg.get("landuse_airport", 0.008))
    w_poi_office = float(weights_cfg.get("poi_office", 20.0))
    w_poi_comm = float(weights_cfg.get("poi_commercial", 8.0))
    w_poi_retail = float(weights_cfg.get("poi_retail", 5.0))
    w_poi_ind = float(weights_cfg.get("poi_industrial", 15.0))
    w_poi_inst = float(weights_cfg.get("poi_institutional", 8.0))
    named_poly_mult = float(weights_cfg.get("osm_named_polygon_multiplier", 1.5))
    m_per_level = float(params.get("meters_per_building_level", 3.5))

    processed_dir = repo_root / "data" / "processed"
    bldg_df = pd.read_parquet(processed_dir / f"{city_code}_buildings.parquet")
    lu_df = pd.read_parquet(processed_dir / f"{city_code}_landuse.parquet")
    air_df = pd.read_parquet(processed_dir / f"{city_code}_airports.parquet")
    poi_df = pd.read_parquet(processed_dir / f"{city_code}_pois.parquet")

    # Per-cell accumulators
    bldg_office_m2 = np.zeros((rows, cols), dtype=np.float64)
    bldg_comm_m2 = np.zeros((rows, cols), dtype=np.float64)
    bldg_retail_m2 = np.zeros((rows, cols), dtype=np.float64)
    bldg_ind_m2 = np.zeros((rows, cols), dtype=np.float64)
    bldg_inst_m2 = np.zeros((rows, cols), dtype=np.float64)

    lu_comm_m2 = np.zeros((rows, cols), dtype=np.float64)
    lu_ind_m2 = np.zeros((rows, cols), dtype=np.float64)
    lu_retail_m2 = np.zeros((rows, cols), dtype=np.float64)
    lu_inst_m2 = np.zeros((rows, cols), dtype=np.float64)
    lu_air_m2 = np.zeros((rows, cols), dtype=np.float64)
    lu_named_bonus_weight = np.zeros((rows, cols), dtype=np.float64)

    poi_office_cnt = np.zeros((rows, cols), dtype=np.int32)
    poi_comm_cnt = np.zeros((rows, cols), dtype=np.int32)
    poi_retail_cnt = np.zeros((rows, cols), dtype=np.int32)
    poi_ind_cnt = np.zeros((rows, cols), dtype=np.int32)
    poi_inst_cnt = np.zeros((rows, cols), dtype=np.int32)

    cell_osm_names: list[list[set[str]]] = [[set() for _ in range(cols)] for _ in range(rows)]

    print(f"[Phase 4.1] Classifying {len(bldg_df):,} buildings ...", flush=True)
    # Vectorized or fast tuple iteration over buildings
    for btype, lvls, hgt, amen, shp, off, area, r, c, bname in zip(
        bldg_df["building"],
        bldg_df["levels"],
        bldg_df["height"],
        bldg_df["amenity"],
        bldg_df["shop"],
        bldg_df["office"],
        bldg_df["area_m2"],
        bldg_df["row"],
        bldg_df["col"],
        bldg_df["name"],
    ):
        r_idx = int(r)
        c_idx = int(c)
        if not (0 <= r_idx < rows and 0 <= c_idx < cols):
            continue

        # Effective floor multiplier from levels or height
        eff_levels = 1.0
        if pd.notna(lvls) and lvls > 0:
            eff_levels = min(25.0, max(1.0, float(lvls)))
        elif pd.notna(hgt) and hgt > 0:
            eff_levels = min(25.0, max(1.0, float(hgt) / m_per_level))
        floor_area = float(area) * eff_levels

        cat = None
        if off or btype in OFFICE_BLDG_TYPES:
            cat = "office"
        elif shp or btype in RETAIL_BLDG_TYPES:
            cat = "retail"
        elif btype in INDUSTRIAL_BLDG_TYPES:
            cat = "industrial"
        elif btype in COMMERCIAL_BLDG_TYPES or amen in COMMERCIAL_AMENITIES:
            cat = "commercial"
        elif btype in INSTITUTIONAL_BLDG_TYPES or amen in INSTITUTIONAL_AMENITIES:
            cat = "institutional"

        if cat == "office":
            bldg_office_m2[r_idx, c_idx] += floor_area
        elif cat == "commercial":
            bldg_comm_m2[r_idx, c_idx] += floor_area
        elif cat == "retail":
            bldg_retail_m2[r_idx, c_idx] += floor_area
        elif cat == "industrial":
            bldg_ind_m2[r_idx, c_idx] += floor_area
        elif cat == "institutional":
            bldg_inst_m2[r_idx, c_idx] += floor_area

        if cat and bname:
            cell_osm_names[r_idx][c_idx].add(str(bname))

    print(f"[Phase 4.1] Processing {len(lu_df):,} landuse polygons & {len(air_df):,} airport features ...", flush=True)
    osm_named_clusters_summary = []

    def add_landuse_area(r_idx: int, c_idx: int, cat: str, area_val: float, name_str: str) -> None:
        base_w = 0.0
        if cat == "commercial":
            lu_comm_m2[r_idx, c_idx] += area_val
            base_w = area_val * w_lu_comm
        elif cat == "industrial":
            lu_ind_m2[r_idx, c_idx] += area_val
            base_w = area_val * w_lu_ind
        elif cat == "retail":
            lu_retail_m2[r_idx, c_idx] += area_val
            base_w = area_val * w_lu_retail
        elif cat in ("school", "college", "university", "hospital", "education", "research_institute", "institutional", "railway", "military"):
            lu_inst_m2[r_idx, c_idx] += area_val
            base_w = area_val * w_lu_inst
        elif cat == "airport":
            lu_air_m2[r_idx, c_idx] += area_val
            base_w = area_val * w_lu_air

        if name_str and base_w > 0:
            lu_named_bonus_weight[r_idx, c_idx] += base_w * max(0.0, named_poly_mult - 1.0)
            cell_osm_names[r_idx][c_idx].add(name_str)

    for cat, name, area, r, c, coords_json in zip(
        lu_df["category"],
        lu_df["name"],
        lu_df["area_m2"],
        lu_df["row"],
        lu_df["col"],
        lu_df["coords_json"],
    ):
        if cat == "residential":
            continue
        name_str = str(name).strip() if pd.notna(name) and str(name).strip() else ""
        cat_str = str(cat)
        if any(k in name_str.lower() for k in ("airport", "aerodrome", "air force")):
            cat_str = "airport"
        area_f = float(area)
        if name_str and area_f >= 100000.0:
            osm_named_clusters_summary.append(
                {"name": name_str, "category": cat_str, "area_ha": round(area_f / 1e4, 2)}
            )

        if area_f < 25000.0:
            r_idx, c_idx = int(r), int(c)
            if 0 <= r_idx < rows and 0 <= c_idx < cols:
                add_landuse_area(r_idx, c_idx, cat_str, area_f, name_str)
        else:
            coords = json.loads(coords_json)
            if len(coords) < 3:
                continue
            poly = Polygon(coords).buffer(0)
            if poly.is_empty:
                continue
            minx, miny, maxx, maxy = poly.bounds
            gr0 = max(0, int((miny - south) / dlat))
            gr1 = min(rows - 1, int((maxy - south) / dlat))
            gc0 = max(0, int((minx - west) / dlon))
            gc1 = min(cols - 1, int((maxx - west) / dlon))
            for gr in range(gr0, gr1 + 1):
                cs = south + gr * dlat
                cn = cs + dlat
                for gc in range(gc0, gc1 + 1):
                    cw = west + gc * dlon
                    ce = cw + dlon
                    inter = poly.intersection(box(cw, cs, ce, cn))
                    if not inter.is_empty:
                        inter_m2 = inter.area * deg2_to_m2
                        if inter_m2 > 1.0:
                            add_landuse_area(gr, gc, cat_str, inter_m2, name_str)

    # Airport polygons (terminals, aprons, aerodromes)
    for aeroway, name, is_poly, area, coords_json in zip(
        air_df["aeroway"],
        air_df["airports"] if "airports" in air_df.columns else air_df["name"],
        air_df["is_polygon"],
        air_df["area_m2"],
        air_df["coords_json"],
    ):
        if not is_poly or aeroway not in ("aerodrome", "terminal", "apron"):
            continue
        name_str = str(name).strip() if pd.notna(name) and str(name).strip() else "Airport Facility"
        coords = json.loads(coords_json)
        if len(coords) < 3:
            continue
        poly = Polygon(coords).buffer(0)
        if poly.is_empty:
            continue
        minx, miny, maxx, maxy = poly.bounds
        gr0 = max(0, int((miny - south) / dlat))
        gr1 = min(rows - 1, int((maxy - south) / dlat))
        gc0 = max(0, int((minx - west) / dlon))
        gc1 = min(cols - 1, int((maxx - west) / dlon))
        for gr in range(gr0, gr1 + 1):
            cs = south + gr * dlat
            cn = cs + dlat
            for gc in range(gc0, gc1 + 1):
                cw = west + gc * dlon
                ce = cw + dlon
                inter = poly.intersection(box(cw, cs, ce, cn))
                if not inter.is_empty:
                    inter_m2 = inter.area * deg2_to_m2
                    if inter_m2 > 1.0:
                        if aeroway == "terminal":
                            bldg_comm_m2[gr, gc] += inter_m2
                        elif lu_air_m2[gr, gc] < inter_m2:
                            add_landuse_area(gr, gc, "airport", inter_m2 - lu_air_m2[gr, gc], name_str)

    print(f"[Phase 4.1] Aggregating {len(poi_df):,} named POIs ...", flush=True)
    for lon, lat, name, off, amen, shp, ind, rwy in zip(
        poi_df["lon"],
        poi_df["lat"],
        poi_df["name"],
        poi_df["office"],
        poi_df["amenity"],
        poi_df["shop"],
        poi_df["industrial"],
        poi_df["railway"],
    ):
        if not (south <= lat <= north and west <= lon <= east):
            continue
        r_idx = min(rows - 1, max(0, int((lat - south) / dlat)))
        c_idx = min(cols - 1, max(0, int((lon - west) / dlon)))
        if off:
            poi_office_cnt[r_idx, c_idx] += 1
        elif shp:
            poi_retail_cnt[r_idx, c_idx] += 1
        elif ind:
            poi_ind_cnt[r_idx, c_idx] += 1
        elif amen in COMMERCIAL_AMENITIES:
            poi_comm_cnt[r_idx, c_idx] += 1
        elif amen in INSTITUTIONAL_AMENITIES or rwy in ("station", "halt"):
            poi_inst_cnt[r_idx, c_idx] += 1

    # Base OSM employment weight per cell
    bldg_weight = (
        bldg_office_m2 * w_office
        + bldg_comm_m2 * w_comm
        + bldg_retail_m2 * w_retail
        + bldg_ind_m2 * w_ind
        + bldg_inst_m2 * w_inst
    )
    lu_weight = (
        lu_comm_m2 * w_lu_comm
        + lu_ind_m2 * w_lu_ind
        + lu_retail_m2 * w_lu_retail
        + lu_inst_m2 * w_lu_inst
        + lu_air_m2 * w_lu_air
        + lu_named_bonus_weight
    )
    poi_weight = (
        poi_office_cnt * w_poi_office
        + poi_comm_cnt * w_poi_comm
        + poi_retail_cnt * w_poi_retail
        + poi_ind_cnt * w_poi_ind
        + poi_inst_cnt * w_poi_inst
    )
    osm_base_weight = bldg_weight + lu_weight + poi_weight

    # Apply configured named employment cluster boosts from cities/blr.yaml
    named_boost_weight = np.zeros((rows, cols), dtype=np.float64)
    cell_boost_names: list[list[list[str]]] = [[[] for _ in range(cols)] for _ in range(rows)]
    boost_reports = []

    for boost in params.get("named_employment_boosts", []):
        bname = boost["name"]
        bb = boost["bbox"]
        bweight = float(boost["boost_weight"])
        breason = boost.get("reason", "")

        r0 = max(0, int((float(bb["south"]) - south) / dlat))
        r1 = min(rows - 1, int((float(bb["north"]) - south) / dlat))
        c0 = max(0, int((float(bb["west"]) - west) / dlon))
        c1 = min(cols - 1, int((float(bb["east"]) - west) / dlon))

        # Distribute boost_weight across cells in the cluster bbox proportionally to
        # existing OSM employment weight + baseline floor so cells with real activity get most weight
        sub_osm = osm_base_weight[r0 : r1 + 1, c0 : c1 + 1]
        alloc_basis = sub_osm + 25.0
        alloc_sum = float(alloc_basis.sum())
        if alloc_sum > 0:
            share = (alloc_basis / alloc_sum) * bweight
            named_boost_weight[r0 : r1 + 1, c0 : c1 + 1] += share

        matched_osm_names = set()
        cells_in_cluster = 0
        for gr in range(r0, r1 + 1):
            for gc in range(c0, c1 + 1):
                cells_in_cluster += 1
                cell_boost_names[gr][gc].append(bname)
                matched_osm_names.update(cell_osm_names[gr][gc])

        boost_reports.append(
            {
                "name": bname,
                "bbox": bb,
                "boost_weight": bweight,
                "reason": breason,
                "grid_cells_covered": cells_in_cluster,
                "osm_base_weight_in_bbox": round(float(sub_osm.sum()), 1),
                "total_weight_in_bbox": round(float(sub_osm.sum() + bweight), 1),
                "sample_osm_employers_in_bbox": sorted(matched_osm_names)[:12],
            }
        )

    # Build output table aligned with active points in points_residents.parquet
    pts_df = pd.read_parquet(processed_dir / "points_residents.parquet")
    records = []
    for _, pt in pts_df.iterrows():
        r_idx = int(pt["row"])
        c_idx = int(pt["col"])

        tot_bldg_emp_m2 = float(
            bldg_office_m2[r_idx, c_idx]
            + bldg_comm_m2[r_idx, c_idx]
            + bldg_retail_m2[r_idx, c_idx]
            + bldg_ind_m2[r_idx, c_idx]
            + bldg_inst_m2[r_idx, c_idx]
        )
        tot_lu_emp_m2 = float(
            lu_comm_m2[r_idx, c_idx]
            + lu_ind_m2[r_idx, c_idx]
            + lu_retail_m2[r_idx, c_idx]
            + lu_inst_m2[r_idx, c_idx]
            + lu_air_m2[r_idx, c_idx]
        )
        tot_poi_emp = int(
            poi_office_cnt[r_idx, c_idx]
            + poi_comm_cnt[r_idx, c_idx]
            + poi_retail_cnt[r_idx, c_idx]
            + poi_ind_cnt[r_idx, c_idx]
            + poi_inst_cnt[r_idx, c_idx]
        )

        has_plausible = (tot_bldg_emp_m2 > 0.0) or (tot_lu_emp_m2 > 0.0) or (tot_poi_emp > 0)
        b_wt = float(named_boost_weight[r_idx, c_idx])
        has_boost = b_wt > 0.0

        raw_wt = float(osm_base_weight[r_idx, c_idx] + b_wt)
        if not has_plausible and not has_boost:
            raw_wt = 0.0

        records.append(
            {
                "id": pt["id"],
                "cell_id": pt["cell_id"],
                "row": r_idx,
                "col": c_idx,
                "lon": float(pt["lon"]),
                "lat": float(pt["lat"]),
                "office_bldg_m2": round(float(bldg_office_m2[r_idx, c_idx]), 1),
                "commercial_bldg_m2": round(float(bldg_comm_m2[r_idx, c_idx]), 1),
                "retail_bldg_m2": round(float(bldg_retail_m2[r_idx, c_idx]), 1),
                "industrial_bldg_m2": round(float(bldg_ind_m2[r_idx, c_idx]), 1),
                "institutional_bldg_m2": round(float(bldg_inst_m2[r_idx, c_idx]), 1),
                "emp_bldg_area_m2": round(tot_bldg_emp_m2, 1),
                "commercial_lu_m2": round(float(lu_comm_m2[r_idx, c_idx]), 1),
                "industrial_lu_m2": round(float(lu_ind_m2[r_idx, c_idx]), 1),
                "retail_lu_m2": round(float(lu_retail_m2[r_idx, c_idx]), 1),
                "institutional_lu_m2": round(float(lu_inst_m2[r_idx, c_idx]), 1),
                "airport_lu_m2": round(float(lu_air_m2[r_idx, c_idx]), 1),
                "emp_landuse_area_m2": round(tot_lu_emp_m2, 1),
                "emp_poi_count": tot_poi_emp,
                "osm_base_weight": round(float(osm_base_weight[r_idx, c_idx]), 2),
                "named_boost_weight": round(b_wt, 2),
                "raw_employment_weight": round(raw_wt, 2),
                "has_plausible_employment_landuse": has_plausible,
                "has_named_boost": has_boost,
                "named_boost_clusters": "; ".join(cell_boost_names[r_idx][c_idx]),
                "sample_osm_names": "; ".join(sorted(cell_osm_names[r_idx][c_idx])[:5]),
            }
        )

    out_df = pd.DataFrame(records)
    parquet_path = processed_dir / "employment_weights.parquet"
    csv_path = processed_dir / "employment_weights.csv"
    out_df.to_parquet(parquet_path, index=False)
    out_df.to_csv(csv_path, index=False)

    osm_named_clusters_summary.sort(key=lambda x: x["area_ha"], reverse=True)

    summary = {
        "phase": "40_employment_weights",
        "city_code": cfg["code"],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "runtime_seconds": round(time.time() - t_start, 2),
        "counts": {
            "total_demand_points": len(out_df),
            "points_with_positive_weight": int((out_df["raw_employment_weight"] > 0).sum()),
            "points_with_plausible_osm_employment": int(out_df["has_plausible_employment_landuse"].sum()),
            "points_with_named_boost": int(out_df["has_named_boost"].sum()),
            "points_with_zero_weight": int((out_df["raw_employment_weight"] == 0).sum()),
        },
        "weight_totals": {
            "total_osm_base_weight": round(float(out_df["osm_base_weight"].sum()), 1),
            "total_named_boost_weight": round(float(out_df["named_boost_weight"].sum()), 1),
            "total_raw_employment_weight": round(float(out_df["raw_employment_weight"].sum()), 1),
            "osm_share_of_total_weight": round(
                float(out_df["osm_base_weight"].sum()) / max(1.0, float(out_df["raw_employment_weight"].sum())), 4
            ),
        },
        "configured_named_boosts": boost_reports,
        "top_osm_derived_named_polygons": osm_named_clusters_summary[:25],
        "outputs": {
            "employment_weights_parquet": str(parquet_path.relative_to(repo_root)),
            "employment_weights_csv": str(csv_path.relative_to(repo_root)),
        },
    }

    report_path = repo_root / "reports" / "40_employment_weights.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("=== Phase 4.1 Employment Weights Summary ===")
    print(f"Demand points evaluated:     {len(out_df):,}")
    print(f"Points with weight > 0:      {summary['counts']['points_with_positive_weight']:,}")
    print(f"  With OSM emp landuse/POI:  {summary['counts']['points_with_plausible_osm_employment']:,}")
    print(f"  With named cluster boost:  {summary['counts']['points_with_named_boost']:,}")
    print(
        f"Total raw weight:            {summary['weight_totals']['total_raw_employment_weight']:,.1f} "
        f"(OSM base: {summary['weight_totals']['osm_share_of_total_weight']*100:.1f}%, "
        f"Boosts: {(1-summary['weight_totals']['osm_share_of_total_weight'])*100:.1f}%)"
    )
    print(f"Saved:                       {parquet_path} & {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
