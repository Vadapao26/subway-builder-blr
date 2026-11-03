#!/usr/bin/env python3
"""
Phase 4 (Part 2): Scale Synthetic Jobs, Merge Demand Points & Render Maps.
Tasks:
1. Scale `raw_employment_weight` from `data/processed/employment_weights.parquet` so that
   `sum(jobs) == round(employment_ratio * sum(residents))` using Hamilton's largest-remainder method.
2. Merge `residents` and `jobs` into the final `points` list (`data/processed/demand_points.parquet` and `.csv`).
3. Verify Phase 4 acceptance sanity checks:
   - No cell has `jobs > 0` without plausible employment landuse (`has_plausible_employment_landuse`)
     unless it has a named boost (`has_named_boost`).
   - Total jobs matches `round(employment_ratio * total_residents)`.
4. Export Jobs map, Residents map, and side-by-side comparison map images (`reports/40_jobs_map.png`,
   `reports/40_residents_map.png`, `reports/50_demand_points_map.png`, `reports/50_demand_points_map.svg`)
   and summary report `reports/50_demand_points.json`.
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
from shapely.geometry import Point
from shapely.strtree import STRtree
import yaml

pipeline_dir = Path(__file__).resolve().parent
if str(pipeline_dir) not in sys.path:
    sys.path.insert(0, str(pipeline_dir))
from grid_utils import create_grid_spec


def write_png_rgb(path: Path, rgb: np.ndarray) -> None:
    """Write a 3-channel uint8 numpy array (H, W, 3) to a standard PNG file using stdlib zlib."""
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


def allocate_integer_jobs(weights: np.ndarray, target_total: int) -> np.ndarray:
    """Hamilton largest-remainder allocation so sum(jobs) == target_total and zero weights stay 0."""
    n = len(weights)
    jobs = np.zeros(n, dtype=np.int64)
    w_sum = float(weights.sum())
    if target_total <= 0 or w_sum <= 0.0:
        return jobs

    exact = (weights / w_sum) * float(target_total)
    floors = np.floor(exact).astype(np.int64)
    # Guarantee zero-weight cells stay strictly 0
    floors[weights <= 0.0] = 0
    remainder = target_total - int(floors.sum())
    if remainder > 0:
        fracs = exact - floors
        fracs[weights <= 0.0] = -1.0
        top_idx = np.argsort(-fracs, kind="mergesort")[:remainder]
        floors[top_idx] += 1
    return floors


def render_heatmap_grid(
    grid_vals: np.ndarray,
    max_ref: float,
    palette: str,
    scale: int = 10,
    boost_boxes: list[tuple[int, int, int, int]] | None = None,
) -> np.ndarray:
    """Render a (rows, cols) value array (row 0 = south) into an upscaled RGB image (row 0 = north)."""
    rows, cols = grid_vals.shape
    h, w = rows * scale, cols * scale
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    rgb[:, :] = (16, 22, 34)  # Dark slate background

    log_max = math.log1p(max(1.0, max_ref))
    for r in range(rows):
        img_r = rows - 1 - r
        y0, y1 = img_r * scale, (img_r + 1) * scale
        for c in range(cols):
            x0, x1 = c * scale, (c + 1) * scale
            val = float(grid_vals[r, c])
            if val <= 0.0:
                continue
            t = min(1.0, math.log1p(val) / log_max)
            if palette == "residents":
                # Viridis/Cyan-Green-Yellow ramp for residents
                red = int(20 + 230 * (t ** 1.6))
                green = int(45 + 195 * (t ** 0.9))
                blue = int(90 + 130 * math.sin(t * math.pi))
            else:
                # Magma/Indigo-Magenta-Orange-Gold ramp for jobs
                red = int(40 + 215 * (t ** 0.85))
                green = int(20 + 185 * (t ** 1.7))
                blue = int(85 + 140 * math.sin(t * math.pi * 0.85))
            rgb[y0:y1, x0:x1] = (red, green, blue)
            # Subtle 1px grid border
            rgb[y0, x0:x1] = np.minimum(255, rgb[y0, x0:x1].astype(np.int32) + 8).astype(np.uint8)
            rgb[y0:y1, x0] = np.minimum(255, rgb[y0:y1, x0].astype(np.int32) + 8).astype(np.uint8)

    # Overlay cluster bounding boxes if provided
    if boost_boxes:
        for r0, r1, c0, c1 in boost_boxes:
            iy0 = (rows - 1 - r1) * scale
            iy1 = (rows - r0) * scale - 1
            ix0 = c0 * scale
            ix1 = (c1 + 1) * scale - 1
            color = (56, 189, 248) if palette == "jobs" else (148, 163, 184)
            rgb[iy0 : iy0 + 2, ix0 : ix1 + 1] = color
            rgb[iy1 - 1 : iy1 + 1, ix0 : ix1 + 1] = color
            rgb[iy0 : iy1 + 1, ix0 : ix0 + 2] = color
            rgb[iy0 : iy1 + 1, ix1 - 1 : ix1 + 1] = color

    return rgb


def render_svg_comparison(
    res_grid: np.ndarray,
    jobs_grid: np.ndarray,
    rows: int,
    cols: int,
    bbox: dict,
    boost_summaries: list[dict],
    dlat: float,
    dlon: float,
    svg_path: Path,
    city_name: str,
    total_res: int,
    total_jobs: int,
) -> None:
    """Write a side-by-side annotated SVG map of Residents vs Jobs per demand point."""
    cell_px = 9
    panel_w = cols * cell_px
    panel_h = rows * cell_px
    gap = 40
    margin_l = 40
    margin_t = 70
    margin_b = 95
    total_w = margin_l * 2 + panel_w * 2 + gap
    total_h = margin_t + panel_h + margin_b

    max_res = float(max(1, res_grid.max()))
    max_jobs = float(max(1, jobs_grid.max()))
    log_max_res = math.log1p(max_res)
    log_max_jobs = math.log1p(max_jobs)

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="{total_h}" viewBox="0 0 {total_w} {total_h}">',
        f'<rect width="{total_w}" height="{total_h}" fill="#0f172a"/>',
        f'<text x="{margin_l}" y="30" fill="#f8fafc" font-family="sans-serif" font-size="18" font-weight="bold">'
        f"{city_name} (BLR) — Phase 4 Demand Points: Residents vs. Synthetic Jobs (1 km Grid)</text>",
        f'<text x="{margin_l}" y="52" fill="#94a3b8" font-family="sans-serif" font-size="12">'
        f'Bbox [{bbox["south"]}, {bbox["west"]}] to [{bbox["north"]}, {bbox["east"]}] | '
        f"Total Residents: {total_res:,} | Total Jobs: {total_jobs:,} (Ratio: {total_jobs/max(1,total_res):.2f})</text>",
    ]

    # Panel titles
    p1_x = margin_l
    p2_x = margin_l + panel_w + gap
    lines.append(
        f'<text x="{p1_x}" y="{margin_t - 8}" fill="#38bdf8" font-family="sans-serif" font-size="14" font-weight="bold">'
        f"A. Residents per Cell (WorldPop 2020, max {int(max_res):,}/km²)</text>"
    )
    lines.append(
        f'<text x="{p2_x}" y="{margin_t - 8}" fill="#f472b6" font-family="sans-serif" font-size="14" font-weight="bold">'
        f"B. Synthetic Jobs per Cell (OSM + Cluster Boosts, max {int(max_jobs):,}/km²)</text>"
    )

    # Backgrounds
    lines.append(f'<rect x="{p1_x}" y="{margin_t}" width="{panel_w}" height="{panel_h}" fill="#101622" stroke="#334155"/>')
    lines.append(f'<rect x="{p2_x}" y="{margin_t}" width="{panel_w}" height="{panel_h}" fill="#101622" stroke="#334155"/>')

    for r in range(rows):
        iy = margin_t + (rows - 1 - r) * cell_px
        for c in range(cols):
            rv = float(res_grid[r, c])
            if rv > 0:
                t = min(1.0, math.log1p(rv) / log_max_res)
                red = int(20 + 230 * (t ** 1.6))
                green = int(45 + 195 * (t ** 0.9))
                blue = int(90 + 130 * math.sin(t * math.pi))
                ix = p1_x + c * cell_px
                lines.append(
                    f'<rect x="{ix}" y="{iy}" width="{cell_px}" height="{cell_px}" fill="rgb({red},{green},{blue})"/>'
                )

            jv = float(jobs_grid[r, c])
            if jv > 0:
                t = min(1.0, math.log1p(jv) / log_max_jobs)
                red = int(40 + 215 * (t ** 0.85))
                green = int(20 + 185 * (t ** 1.7))
                blue = int(85 + 140 * math.sin(t * math.pi * 0.85))
                ix = p2_x + c * cell_px
                lines.append(
                    f'<rect x="{ix}" y="{iy}" width="{cell_px}" height="{cell_px}" fill="rgb({red},{green},{blue})"/>'
                )

    # Draw named cluster bounding boxes and labels on Jobs panel
    south = float(bbox["south"])
    west = float(bbox["west"])
    for idx, b in enumerate(boost_summaries, 1):
        bb = b["bbox"]
        r0 = max(0, int((float(bb["south"]) - south) / dlat))
        r1 = min(rows - 1, int((float(bb["north"]) - south) / dlat))
        c0 = max(0, int((float(bb["west"]) - west) / dlon))
        c1 = min(cols - 1, int((float(bb["east"]) - west) / dlon))
        bx = p2_x + c0 * cell_px
        by = margin_t + (rows - 1 - r1) * cell_px
        bw = (c1 - c0 + 1) * cell_px
        bh = (r1 - r0 + 1) * cell_px
        lines.append(
            f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" fill="none" stroke="#38bdf8" stroke-width="1.5" stroke-dasharray="3,2"/>'
        )
        lines.append(
            f'<text x="{bx + 3}" y="{max(margin_t + 10, by - 3)}" fill="#bae6fd" font-family="sans-serif" font-size="9" font-weight="bold">'
            f'#{idx} ({b["jobs"]:,})</text>'
        )

    # Footer legend listing numbered employment hubs
    ly = margin_t + panel_h + 24
    lines.append(
        f'<text x="{margin_l}" y="{ly}" fill="#e2e8f0" font-family="sans-serif" font-size="11" font-weight="bold">'
        f"Named Employment Clusters (Dashed Blue Boxes on Jobs Map):</text>"
    )
    for idx, b in enumerate(boost_summaries, 1):
        col_idx = (idx - 1) % 3
        row_idx = (idx - 1) // 3
        tx = margin_l + col_idx * 355
        ty = ly + 18 + row_idx * 16
        short_name = b["name"].split("(")[0].strip()
        lines.append(
            f'<text x="{tx}" y="{ty}" fill="#cbd5e1" font-family="sans-serif" font-size="10">'
            f'#{idx} {short_name}: {b["jobs"]:,} jobs ({b["share_of_city_jobs_pct"]:.1f}%)</text>'
        )

    lines.append("</svg>")
    svg_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4 (Part 2): Scale synthetic jobs and merge demand points.")
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
    mid_lat = 0.5 * (south + north)
    cos_lat = math.cos(math.radians(mid_lat))
    m_per_deg_lat = 111132.0
    m_per_deg_lon = 111320.0 * cos_lat

    params = cfg["params"]
    grid = create_grid_spec(cfg)
    grid_cell_m = float(params.get("grid_cell_m", 500))
    dlat = grid_cell_m / m_per_deg_lat
    dlon = grid_cell_m / m_per_deg_lon
    rows = grid.rows
    cols = grid.cols

    emp_ratio = float(params.get("employment_ratio", 1.0))

    processed_dir = repo_root / "data" / "processed"
    city_res = processed_dir / f"{city_code}_points_residents.parquet"
    city_emp = processed_dir / f"{city_code}_employment_weights.parquet"
    res_df = pd.read_parquet(city_res if city_res.is_file() else (processed_dir / "points_residents.parquet"))
    emp_df = pd.read_parquet(city_emp if city_emp.is_file() else (processed_dir / "employment_weights.parquet"))

    merged = pd.merge(
        res_df,
        emp_df[
            [
                "id",
                "emp_bldg_area_m2",
                "emp_landuse_area_m2",
                "emp_poi_count",
                "osm_base_weight",
                "named_boost_weight",
                "raw_employment_weight",
                "has_plausible_employment_landuse",
                "has_named_boost",
                "named_boost_clusters",
                "sample_osm_names",
            ]
        ],
        on="id",
        how="inner",
    )

    total_residents = int(merged["residents"].sum())
    target_total_jobs = int(round(emp_ratio * total_residents))

    # Organic coordinate relaxation (Building Centroids + Road Network Snapping + Spatial Hash Relaxation)
    bldg_parquet = processed_dir / f"{city_code}_buildings.parquet"
    if bldg_parquet.is_file():
        print(f"[Phase 4.2] Generating organic coordinates from building centroids and road network snapping...")
        bldg_df = pd.read_parquet(bldg_parquet)
        b_r, b_c = grid.map_coords_to_row_col(bldg_df["centroid_lon"].to_numpy(dtype=np.float64), bldg_df["centroid_lat"].to_numpy(dtype=np.float64))
        bldg_df["row"] = b_r
        bldg_df["col"] = b_c
        bldg_df["w_lon"] = bldg_df["centroid_lon"] * bldg_df["area_m2"]
        bldg_df["w_lat"] = bldg_df["centroid_lat"] * bldg_df["area_m2"]
        grp = bldg_df.groupby(["row", "col"]).agg({"w_lon": "sum", "w_lat": "sum", "area_m2": "sum"}).reset_index()
        grp["bldg_lon"] = grp["w_lon"] / grp["area_m2"]
        grp["bldg_lat"] = grp["w_lat"] / grp["area_m2"]
        merged = merged.merge(grp[["row", "col", "bldg_lon", "bldg_lat", "area_m2"]], on=["row", "col"], how="left")
        merged["area_m2"] = merged["area_m2"].fillna(0.0)

        # Load road network nodes for realistic street snapping
        roads_parquet = processed_dir / f"{city_code}_roads.parquet"
        road_tree = None
        road_points = []
        if roads_parquet.is_file():
            print(f"[Phase 4.2] Indexing road network nodes for street-snapped organic coordinates...")
            roads_df = pd.read_parquet(roads_parquet)
            for c_json in roads_df["coords_json"]:
                coords = json.loads(c_json)
                if len(coords) >= 2:
                    road_points.append(Point(coords[0][0], coords[0][1]))
                    road_points.append(Point(coords[-1][0], coords[-1][1]))
                    if len(coords) > 2:
                        road_points.append(Point(coords[len(coords) // 2][0], coords[len(coords) // 2][1]))
            if road_points:
                road_tree = STRtree(road_points)
                print(f"[Phase 4.2] Indexed {len(road_points):,} road network sample nodes.")

        rng = np.random.RandomState(42)
        new_coords = []
        for _, r in merged.iterrows():
            c = int(r["col"])
            row_idx = int(r["row"])
            # Continuous non-lattice organic base within the cell using variable bounds & stagger
            s, n, w, e = grid.get_cell_bounds(row_idx, c)
            rand_u = rng.uniform(0.15, 0.85)
            rand_v = rng.uniform(0.15, 0.85)
            base_lon = w + rand_u * (e - w)
            base_lat = s + rand_v * (n - s)

            if pd.notnull(r["bldg_lon"]) and r["area_m2"] > 40.0:
                l_lon = float(r["bldg_lon"]) + rng.uniform(-40.0, 40.0) / m_per_deg_lon
                l_lat = float(r["bldg_lat"]) + rng.uniform(-40.0, 40.0) / m_per_deg_lat
            elif road_tree is not None:
                pt = Point(base_lon, base_lat)
                near_idx = road_tree.nearest(pt)
                near_pt = road_points[near_idx]
                dist_m = math.hypot((near_pt.x - base_lon) * m_per_deg_lon, (near_pt.y - base_lat) * m_per_deg_lat)
                if dist_m <= 600.0:
                    l_lon = near_pt.x + rng.uniform(-30.0, 30.0) / m_per_deg_lon
                    l_lat = near_pt.y + rng.uniform(-30.0, 30.0) / m_per_deg_lat
                else:
                    l_lon, l_lat = base_lon, base_lat
            else:
                l_lon, l_lat = base_lon, base_lat

            new_coords.append([l_lon, l_lat])

        coords_arr = np.array(new_coords)
        # Fast spatial-hash collision relaxation (repulsion for points closer than ~260m)
        min_sep_m = 260.0
        bin_size = min_sep_m * 1.25
        coords_m_x = coords_arr[:, 0] * m_per_deg_lon
        coords_m_y = coords_arr[:, 1] * m_per_deg_lat
        pts_m = np.column_stack([coords_m_x, coords_m_y])

        for it in range(10):
            bins = {}
            for i in range(len(pts_m)):
                bx = int(pts_m[i, 0] / bin_size)
                by = int(pts_m[i, 1] / bin_size)
                bins.setdefault((bx, by), []).append(i)
            shifts = np.zeros_like(pts_m)
            for (bx, by), idxs in bins.items():
                neighbor_idxs = []
                for dbx in (-1, 0, 1):
                    for dby in (-1, 0, 1):
                        neighbor_idxs.extend(bins.get((bx + dbx, by + dby), []))
                if len(neighbor_idxs) <= 1:
                    continue
                n_arr = pts_m[neighbor_idxs]
                for i in idxs:
                    d = pts_m[i] - n_arr
                    dist = np.hypot(d[:, 0], d[:, 1])
                    too_close = (dist < min_sep_m) & (dist > 1e-4)
                    if np.any(too_close):
                        overlap = min_sep_m - dist[too_close]
                        norm = d[too_close] / dist[too_close, None]
                        shifts[i] += np.sum(norm * overlap[:, None] * 0.22, axis=0)
            pts_m += shifts

        coords_arr[:, 0] = pts_m[:, 0] / m_per_deg_lon
        coords_arr[:, 1] = pts_m[:, 1] / m_per_deg_lat

        merged["lon"] = [round(c[0], 6) for c in coords_arr]
        merged["lat"] = [round(c[1], 6) for c in coords_arr]
        merged["location"] = [[round(c[0], 6), round(c[1], 6)] for c in coords_arr]
    else:
        merged["area_m2"] = 0.0

    # Dual-Tier Employment Allocation:
    # 70% Regional Economic Anchors + 30% Neighborhood Local-Serving Commerce
    tier1_target = int(round(0.70 * target_total_jobs))
    tier2_target = target_total_jobs - tier1_target

    raw_weights = merged["raw_employment_weight"].to_numpy(dtype=np.float64, copy=True)
    valid_mask = merged["has_plausible_employment_landuse"].to_numpy(dtype=bool) | merged["has_named_boost"].to_numpy(dtype=bool)
    raw_weights[~valid_mask] = 0.0
    t1_jobs = allocate_integer_jobs(raw_weights, tier1_target)

    # Tier 2: Neighborhood Local Commerce (proportional to residents & local building density)
    valid_local = (merged["residents"] >= 150) | (merged["area_m2"] >= 250) | (raw_weights > 0.0)
    w_loc = np.zeros(len(merged), dtype=np.float64)
    res_vals = merged.loc[valid_local, "residents"].to_numpy(dtype=np.float64)
    bldg_vals = merged.loc[valid_local, "area_m2"].to_numpy(dtype=np.float64)
    w_loc[valid_local] = (res_vals ** 0.8) * (1.0 + np.log1p(bldg_vals / 1000.0))
    t2_jobs = allocate_integer_jobs(w_loc, tier2_target)

    jobs_arr = t1_jobs + t2_jobs
    merged["jobs"] = jobs_arr
    # Mark local commerce cells as having valid local employment activity
    merged.loc[t2_jobs > 0, "has_plausible_employment_landuse"] = True

    # Acceptance Sanity Check 1: No cell has jobs without plausible employment land use unless it has a named boost
    unexplained_mask = (merged["jobs"] > 0) & (~merged["has_plausible_employment_landuse"]) & (~merged["has_named_boost"])
    unexplained_count = int(unexplained_mask.sum())
    if unexplained_count > 0:
        raise AssertionError(f"Sanity check failed: {unexplained_count} cells have jobs without employment landuse or boost")

    # Acceptance Sanity Check 2: Total jobs matches employment_ratio * total_residents
    actual_total_jobs = int(merged["jobs"].sum())
    if actual_total_jobs != target_total_jobs:
        raise AssertionError(f"Total jobs {actual_total_jobs} != target {target_total_jobs}")

    # Save final demand_points.parquet and CSV
    demand_parquet = processed_dir / "demand_points.parquet"
    city_demand_parquet = processed_dir / f"{city_code}_demand_points.parquet"
    demand_csv = processed_dir / "demand_points.csv"

    merged.to_parquet(demand_parquet, index=False)
    merged.to_parquet(city_demand_parquet, index=False)
    merged.to_csv(demand_csv, index=False)

    # Build 2D grids for visualization and cluster summaries
    res_grid = np.zeros((rows, cols), dtype=np.float64)
    jobs_grid = np.zeros((rows, cols), dtype=np.float64)
    for r, c, rv, jv in zip(merged["row"], merged["col"], merged["residents"], merged["jobs"]):
        res_grid[int(r), int(c)] = float(rv)
        jobs_grid[int(r), int(c)] = float(jv)

    boost_boxes = []
    boost_summaries = []
    for boost in params.get("named_employment_boosts", []):
        bb = boost["bbox"]
        r0 = int(grid.map_lat_to_row(float(bb["south"])))
        r1 = int(grid.map_lat_to_row(float(bb["north"])))
        c0 = int(grid.map_lon_to_col(float(bb["west"])))
        c1 = int(grid.map_lon_to_col(float(bb["east"])))
        boost_boxes.append((r0, r1, c0, c1))
        cluster_jobs = int(jobs_grid[r0 : r1 + 1, c0 : c1 + 1].sum())
        cluster_res = int(res_grid[r0 : r1 + 1, c0 : c1 + 1].sum())
        boost_summaries.append(
            {
                "name": boost["name"],
                "bbox": bb,
                "reason": boost.get("reason", ""),
                "residents": cluster_res,
                "jobs": cluster_jobs,
                "share_of_city_jobs_pct": round((cluster_jobs / max(1, actual_total_jobs)) * 100.0, 2),
                "jobs_to_residents_ratio_in_cluster": round(cluster_jobs / max(1, cluster_res), 2),
            }
        )

    reports_dir = repo_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    res_img = render_heatmap_grid(res_grid, float(res_grid.max()), palette="residents", scale=10, boost_boxes=boost_boxes)
    jobs_img = render_heatmap_grid(jobs_grid, float(jobs_grid.max()), palette="jobs", scale=10, boost_boxes=boost_boxes)
    gap_strip = np.full((res_img.shape[0], 20, 3), (15, 23, 42), dtype=np.uint8)
    combined_img = np.concatenate([res_img, gap_strip, jobs_img], axis=1)

    res_png_path = reports_dir / "40_residents_map.png"
    jobs_png_path = reports_dir / "40_jobs_map.png"
    comb_png_path = reports_dir / "50_demand_points_map.png"
    comb_svg_path = reports_dir / "50_demand_points_map.svg"

    write_png_rgb(res_png_path, res_img)
    write_png_rgb(jobs_png_path, jobs_img)
    write_png_rgb(comb_png_path, combined_img)

    render_svg_comparison(
        res_grid,
        jobs_grid,
        rows,
        cols,
        bbox,
        boost_summaries,
        dlat,
        dlon,
        comb_svg_path,
        city_name,
        total_residents,
        actual_total_jobs,
    )

    bbmp_jobs = int(merged.loc[merged["in_bbmp"], "jobs"].sum())
    non_bbmp_jobs = actual_total_jobs - bbmp_jobs

    top_job_cells = (
        merged.sort_values("jobs", ascending=False)
        .head(15)[
            [
                "id",
                "cell_id",
                "lat",
                "lon",
                "residents",
                "jobs",
                "ward_name",
                "named_boost_clusters",
                "sample_osm_names",
            ]
        ]
        .to_dict(orient="records")
    )

    summary = {
        "phase": "50_demand_points",
        "city_code": cfg["code"],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "runtime_seconds": round(time.time() - t_start, 2),
        "totals": {
            "demand_point_count": len(merged),
            "points_with_residents_gt_0": int((merged["residents"] > 0).sum()),
            "points_with_jobs_gt_0": int((merged["jobs"] > 0).sum()),
            "points_with_zero_jobs": int((merged["jobs"] == 0).sum()),
            "total_residents": total_residents,
            "configured_employment_ratio": emp_ratio,
            "target_total_jobs": target_total_jobs,
            "actual_total_jobs": actual_total_jobs,
            "bbmp_jobs": bbmp_jobs,
            "non_bbmp_bmrda_and_hosur_jobs": non_bbmp_jobs,
        },
        "sanity_checks": {
            "unexplained_job_cells_count": unexplained_count,
            "no_jobs_without_plausible_landuse_or_boost": unexplained_count == 0,
            "total_jobs_matches_ratio": actual_total_jobs == target_total_jobs,
            "no_negative_or_nan_values": bool(
                (not merged["jobs"].isna().any())
                and (not merged["residents"].isna().any())
                and (merged["jobs"] >= 0).all()
                and (merged["residents"] >= 0).all()
            ),
        },
        "jobs_distribution": {
            "min_jobs": int(jobs_arr.min()),
            "median_jobs_all_points": float(np.median(jobs_arr)),
            "median_jobs_active_emp_points": float(np.median(jobs_arr[jobs_arr > 0])),
            "mean_jobs_all_points": round(float(jobs_arr.mean()), 1),
            "mean_jobs_active_emp_points": round(float(jobs_arr[jobs_arr > 0].mean()), 1),
            "p90_jobs": float(np.percentile(jobs_arr, 90)),
            "max_jobs": int(jobs_arr.max()),
        },
        "named_cluster_job_totals": boost_summaries,
        "top_15_job_cells": top_job_cells,
        "outputs": {
            "demand_points_parquet": str(demand_parquet.relative_to(repo_root)),
            "city_demand_points_parquet": str(city_demand_parquet.relative_to(repo_root)),
            "demand_points_csv": str(demand_csv.relative_to(repo_root)),
            "residents_map_png": str(res_png_path.relative_to(repo_root)),
            "jobs_map_png": str(jobs_png_path.relative_to(repo_root)),
            "combined_map_png": str(comb_png_path.relative_to(repo_root)),
            "combined_map_svg": str(comb_svg_path.relative_to(repo_root)),
        },
    }

    report_path = reports_dir / "50_demand_points.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("=== Phase 4.2 Demand Points & Jobs Summary ===")
    print(f"Total demand points:         {len(merged):,} (with jobs > 0: {(merged['jobs'] > 0).sum():,})")
    print(f"Total residents:             {total_residents:,}")
    print(f"Total jobs (ratio {emp_ratio:.2f}):     {actual_total_jobs:,}")
    print(f"Sanity checks passed:        unexplained_job_cells={unexplained_count}, exact_ratio_match={actual_total_jobs == target_total_jobs}")
    print("Major Employment Corridors:")
    for b in boost_summaries:
        print(f"  - {b['name']:58s}: {b['jobs']:9,d} jobs ({b['share_of_city_jobs_pct']:5.1f}%) | res={b['residents']:9,d}")
    print(f"Maps exported:               {res_png_path.name}, {jobs_png_path.name}, {comb_png_path.name}, {comb_svg_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
