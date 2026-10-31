#!/usr/bin/env python3
"""
Phase 3: Residents per Demand Point.
Tasks:
1. Read the WorldPop population raster (`data/raw/ind_ppp_2020_1km_Aggregated_UNadj.tif`)
   and aggregate it onto the configurable study grid (`params.grid_cell_m`).
2. Assign each active demand point an `id`, `location` (`[lon, lat]`), and integer `residents` count.
3. Cross-check:
   - Compare total residents inside the study bounding box against the published reference
     population figure (`population` in `cities/blr.yaml`) and report the difference.
   - Cross-check population inside the BBMP municipal ward boundaries (`blr_admin_wards.geojson`).
   - Compare point count and residents-per-point distribution against built-in game cities (`DET`, `PAR`, `TOK`).
4. Output `data/processed/points_residents.parquet` (and `.csv`) with no negative or NaN values,
   and write `reports/30_population_grid.json`.
"""

import argparse
from datetime import datetime, timezone
import gzip
import json
import math
from pathlib import Path
import struct
import time

import numpy as np
import pandas as pd
from shapely.geometry import Point, shape
from shapely.prepared import prep
import yaml


def parse_tiff_header(f) -> dict:
    """Parse Classic TIFF (v42) or BigTIFF (v43) IFD0 tags needed for GeoTIFF reading."""
    f.seek(0)
    head = f.read(16)
    byte_order, version = struct.unpack("<2sH", head[:4])
    if byte_order != b"II":
        raise ValueError(f"Only little-endian TIFF supported, got {byte_order!r}")

    tags = {}
    if version == 42:
        ifd_offset = struct.unpack("<I", head[4:8])[0]
        f.seek(ifd_offset)
        num_entries = struct.unpack("<H", f.read(2))[0]
        raw_entries = [f.read(12) for _ in range(num_entries)]
        for ent in raw_entries:
            tag, ttype, cnt, val_off = struct.unpack("<HHII", ent)
            tsize = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 12: 8}.get(ttype, 1)
            tchar = {1: "B", 2: "s", 3: "H", 4: "I", 12: "d"}.get(ttype, "B")
            total_bytes = cnt * tsize
            if total_bytes <= 4:
                raw_bytes = ent[8 : 8 + total_bytes]
            else:
                f.seek(val_off)
                raw_bytes = f.read(total_bytes)
            if ttype == 2:
                tags[tag] = raw_bytes.decode("ascii", errors="ignore").rstrip("\x00")
            else:
                vals = struct.unpack(f"<{cnt}{tchar}", raw_bytes)
                tags[tag] = vals[0] if cnt == 1 else vals
    elif version == 43:
        _, _, off_size, zero, ifd_offset = struct.unpack("<2sHHHQ", head)
        f.seek(ifd_offset)
        num_entries = struct.unpack("<Q", f.read(8))[0]
        raw_entries = [f.read(20) for _ in range(num_entries)]
        for ent in raw_entries:
            tag, ttype, cnt, val_off = struct.unpack("<HHQQ", ent)
            tsize = {1: 1, 2: 1, 3: 2, 4: 4, 8: 2, 12: 8, 16: 8, 17: 8, 18: 8}.get(ttype, 1)
            tchar = {1: "B", 2: "s", 3: "H", 4: "I", 8: "h", 12: "d", 16: "Q", 17: "q", 18: "Q"}.get(ttype, "B")
            total_bytes = cnt * tsize
            if total_bytes <= 8:
                raw_bytes = ent[12 : 12 + total_bytes]
            else:
                f.seek(val_off)
                raw_bytes = f.read(total_bytes)
            if ttype == 2:
                tags[tag] = raw_bytes.decode("ascii", errors="ignore").rstrip("\x00")
            else:
                vals = struct.unpack(f"<{cnt}{tchar}", raw_bytes)
                tags[tag] = vals[0] if cnt == 1 else vals
    else:
        raise ValueError(f"Unsupported TIFF version: {version}")

    scale = tags[33550]
    tiepoint = tags[33922]
    strip_offsets = tags[273] if isinstance(tags[273], tuple) else (tags[273],)
    strip_byte_counts = tags[279] if isinstance(tags[279], tuple) else (tags[279],)
    nodata_str = tags.get(42113, "-99999")
    return {
        "width": int(tags[256]),
        "height": int(tags[257]),
        "bits_per_sample": int(tags.get(258, 32)),
        "compression": int(tags.get(259, 1)),
        "predictor": int(tags.get(317, 1)),
        "rows_per_strip": int(tags[278]),
        "strip_offsets": strip_offsets,
        "strip_byte_counts": strip_byte_counts,
        "dx": float(scale[0]),
        "dy": float(scale[1]),
        "x0": float(tiepoint[3]),
        "y0": float(tiepoint[4]),
        "nodata": float(nodata_str),
    }


def lzw_decompress_msb(raw: bytes) -> bytearray:
    """Fast TIFF MSB-first LZW decompressor (early code-size change)."""
    CLEAR, EOI = 256, 257
    base_table = [bytes([i]) for i in range(256)] + [b"", b""]
    table = base_table.copy()
    code_size = 9
    bit_buf = 0
    bit_cnt = 0
    pos = 0
    n = len(raw)
    old_code = -1
    out = bytearray()

    while pos < n:
        bit_buf = (bit_buf << 8) | raw[pos]
        bit_cnt += 8
        pos += 1
        while bit_cnt >= code_size:
            bit_cnt -= code_size
            code = (bit_buf >> bit_cnt) & ((1 << code_size) - 1)
            bit_buf &= (1 << bit_cnt) - 1
            if code == EOI:
                pos = n
                break
            elif code == CLEAR:
                table = base_table.copy()
                code_size = 9
                old_code = -1
            else:
                if old_code == -1:
                    out.extend(table[code])
                    old_code = code
                else:
                    if code < len(table):
                        entry = table[code]
                        out.extend(entry)
                        table.append(table[old_code] + entry[:1])
                        old_code = code
                    elif code == len(table):
                        entry = table[old_code] + table[old_code][:1]
                        out.extend(entry)
                        table.append(entry)
                        old_code = code
                    else:
                        raise ValueError(f"Invalid LZW code {code} at byte {pos}")
                    tlen = len(table)
                    if tlen == 511:
                        code_size = 10
                    elif tlen == 1023:
                        code_size = 11
                    elif tlen == 2047:
                        code_size = 12
    return out


def read_worldpop_window(tif_path: Path, bbox: dict) -> tuple[np.ndarray, dict]:
    """Read and decode the strips of the WorldPop GeoTIFF overlapping `bbox`."""
    south, north = float(bbox["south"]), float(bbox["north"])
    west, east = float(bbox["west"]), float(bbox["east"])

    with open(tif_path, "rb") as f:
        meta = parse_tiff_header(f)
        width = meta["width"]
        height = meta["height"]
        rps = meta["rows_per_strip"]
        x0, y0 = meta["x0"], meta["y0"]
        dx, dy = meta["dx"], meta["dy"]

        r_min = max(0, int((y0 - north) / dy))
        r_max = min(height - 1, int((y0 - south) / dy) + 1)
        c_min = max(0, int((west - x0) / dx))
        c_max = min(width - 1, int((east - x0) / dx) + 1)

        s_min = r_min // rps
        s_max = r_max // rps

        strips_decoded = {}
        for s_idx in range(s_min, s_max + 1):
            offset = meta["strip_offsets"][s_idx]
            bcount = meta["strip_byte_counts"][s_idx]
            strip_rows = min(rps, height - s_idx * rps)
            f.seek(offset)
            raw = f.read(bcount)
            if meta["compression"] == 5:
                dec = lzw_decompress_msb(raw)
            elif meta["compression"] == 1:
                dec = bytearray(raw)
            else:
                raise ValueError(f"Unsupported TIFF compression: {meta['compression']}")

            if meta["predictor"] == 2:
                # libtiff horAcc32: 32-bit horizontal differencing along each scanline
                u32 = np.frombuffer(dec, dtype="<u4").reshape(strip_rows, width)
                floats = np.cumsum(u32, axis=1, dtype=np.uint32).view("<f4")
            else:
                floats = np.frombuffer(dec, dtype="<f4").reshape(strip_rows, width)
            strips_decoded[s_idx] = floats

    win_h = r_max - r_min + 1
    win_w = c_max - c_min + 1
    window = np.zeros((win_h, win_w), dtype=np.float32)
    for r in range(r_min, r_max + 1):
        s_idx = r // rps
        sr = r % rps
        window[r - r_min, :] = strips_decoded[s_idx][sr, c_min : c_max + 1]

    meta["win_r_min"] = r_min
    meta["win_r_max"] = r_max
    meta["win_c_min"] = c_min
    meta["win_c_max"] = c_max
    return window, meta


def load_builtin_city_stats() -> dict:
    """Read built-in game city demand stats (DET, PAR, TOK) if present on disk for comparison."""
    base = Path(r"C:\Users\siddh\AppData\Roaming\metro-maker4\cities\data")
    stats = {}
    if not base.exists():
        return stats
    for code in ("DET", "PAR", "TOK"):
        p = base / code / "demand_data.json.gz"
        if p.is_file():
            with gzip.open(p, "rt", encoding="utf-8") as f:
                data = json.load(f)
            pts = data.get("points", [])
            res = np.array([pt.get("residents", 0) for pt in pts], dtype=np.int64)
            stats[code] = {
                "point_count": len(pts),
                "total_residents": int(res.sum()),
                "min_residents": int(res.min()) if len(res) else 0,
                "median_residents": float(np.median(res)) if len(res) else 0.0,
                "mean_residents": round(float(res.mean()), 1) if len(res) else 0.0,
                "p90_residents": float(np.percentile(res, 90)) if len(res) else 0.0,
                "max_residents": int(res.max()) if len(res) else 0,
            }
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 3: Aggregate population raster into demand points.")
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
    ref_population = int(cfg["population"])

    mid_lat = 0.5 * (south + north)
    cos_lat = math.cos(math.radians(mid_lat))
    m_per_deg_lat = 111132.0
    m_per_deg_lon = 111320.0 * cos_lat

    grid_cell_m = float(cfg["params"].get("grid_cell_m", 1000))
    min_res_threshold = int(cfg["params"].get("min_residents_per_point", 50))
    dlat = grid_cell_m / m_per_deg_lat
    dlon = grid_cell_m / m_per_deg_lon
    rows = int(math.ceil((north - south) / dlat))
    cols = int(math.ceil((east - west) / dlon))

    tif_url = cfg["population_raster"]["url"]
    tif_filename = tif_url.split("/")[-1]
    tif_path = repo_root / "data" / "raw" / tif_filename
    if not tif_path.is_file():
        raise FileNotFoundError(f"Population raster not found: {tif_path}. Run Phase 1 first.")

    print(f"[Phase 3] Decoding WorldPop window from {tif_path.name} ...", flush=True)
    window, meta = read_worldpop_window(tif_path, bbox)

    x0, y0 = meta["x0"], meta["y0"]
    dx, dy = meta["dx"], meta["dy"]
    r_min, r_max = meta["win_r_min"], meta["win_r_max"]
    c_min, c_max = meta["win_c_min"], meta["win_c_max"]
    px_area = dx * dy

    # Area-weighted exact aggregation from WorldPop pixels onto the study grid
    grid_pop_raw = np.zeros((rows, cols), dtype=np.float64)
    for pr in range(r_min, r_max + 1):
        px_n = y0 - pr * dy
        px_s = px_n - dy
        if px_n <= south or px_s >= north:
            continue
        for pc in range(c_min, c_max + 1):
            px_w = x0 + pc * dx
            px_e = px_w + dx
            if px_e <= west or px_w >= east:
                continue
            val = float(window[pr - r_min, pc - c_min])
            if not math.isfinite(val) or val <= 0.0:
                continue
            gr0 = max(0, int((max(px_s, south) - south) / dlat))
            gr1 = min(rows - 1, int((min(px_n, north) - south) / dlat))
            gc0 = max(0, int((max(px_w, west) - west) / dlon))
            gc1 = min(cols - 1, int((min(px_e, east) - west) / dlon))
            for r in range(gr0, gr1 + 1):
                cs = south + r * dlat
                cn = min(north, cs + dlat)
                ov_y = max(0.0, min(px_n, cn) - max(px_s, cs))
                if ov_y <= 0.0:
                    continue
                for c in range(gc0, gc1 + 1):
                    cw = west + c * dlon
                    ce = min(east, cw + dlon)
                    ov_x = max(0.0, min(px_e, ce) - max(px_w, cw))
                    if ov_x > 0.0:
                        grid_pop_raw[r, c] += val * (ov_x * ov_y / px_area)

    # Load Phase 2 coverage grid if present to preserve cells with building/employment activity
    cov_csv = repo_root / "reports" / "20_coverage_grid.csv"
    has_activity = np.zeros((rows, cols), dtype=bool)
    if cov_csv.is_file():
        cov_df = pd.read_csv(cov_csv)
        for _, row_data in cov_df.iterrows():
            r_idx = int(row_data["row"])
            c_idx = int(row_data["col"])
            if 0 <= r_idx < rows and 0 <= c_idx < cols:
                if row_data["building_count"] > 0 or row_data["employment_landuse_m2"] > 0:
                    has_activity[r_idx, c_idx] = True

    # Also mark any cell overlapping a configured named employment boost
    for boost in cfg["params"].get("named_employment_boosts", []):
        bb = boost["bbox"]
        for r in range(rows):
            clat = south + (r + 0.5) * dlat
            if bb["south"] <= clat <= bb["north"]:
                for c in range(cols):
                    clon = west + (c + 0.5) * dlon
                    if bb["west"] <= clon <= bb["east"]:
                        has_activity[r, c] = True

    # Optional BBMP ward boundary cross-check
    wards_path = repo_root / "data" / "raw" / f"{city_code}_admin_wards.geojson"
    ward_prepared = []
    if wards_path.is_file():
        with open(wards_path, "r", encoding="utf-8") as f:
            gj = json.load(f)
        for feat in gj.get("features", []):
            geom = shape(feat["geometry"]).buffer(0)
            props = feat.get("properties", {})
            wname = str(props.get("KGISWardName") or props.get("WARD_NAME") or props.get("name") or "")
            wno = str(props.get("KGISWardNo") or props.get("WARD_NO") or "")
            ward_prepared.append((prep(geom), geom.bounds, wno, wname))

    points_records = []
    dropped_sparse_cells = 0
    dropped_sparse_residents = 0.0
    ward_pop_totals: dict[str, int] = {}
    bbmp_total_residents = 0
    bbmp_cell_count = 0

    for r in range(rows):
        cell_lat = min(north - 1e-6, south + (r + 0.5) * dlat)
        for c in range(cols):
            cell_lon = min(east - 1e-6, west + (c + 0.5) * dlon)
            raw_pop = float(grid_pop_raw[r, c])
            if not math.isfinite(raw_pop) or raw_pop < 0.0:
                raw_pop = 0.0
            res_int = int(round(raw_pop))

            if res_int < min_res_threshold and not has_activity[r, c]:
                dropped_sparse_cells += 1
                dropped_sparse_residents += raw_pop
                continue

            in_bbmp = False
            ward_name = ""
            if ward_prepared:
                pt = Point(cell_lon, cell_lat)
                for pgeom, bounds, wno, wname in ward_prepared:
                    if bounds[0] <= cell_lon <= bounds[2] and bounds[1] <= cell_lat <= bounds[3]:
                        if pgeom.contains(pt):
                            in_bbmp = True
                            ward_name = f"Ward {wno}: {wname}" if wno else wname
                            bbmp_total_residents += res_int
                            bbmp_cell_count += 1
                            ward_pop_totals[ward_name] = ward_pop_totals.get(ward_name, 0) + res_int
                            break

            points_records.append(
                {
                    "id": f"pt_{r:03d}_{c:03d}",
                    "cell_id": f"R{r:03d}_C{c:03d}",
                    "row": r,
                    "col": c,
                    "lon": round(cell_lon, 6),
                    "lat": round(cell_lat, 6),
                    "location": [round(cell_lon, 6), round(cell_lat, 6)],
                    "residents": res_int,
                    "residents_raw": round(raw_pop, 2),
                    "in_bbmp": in_bbmp,
                    "ward_name": ward_name,
                }
            )

    df = pd.DataFrame(points_records)
    assert not df["residents"].isna().any(), "NaN values found in residents"
    assert (df["residents"] >= 0).all(), "Negative values found in residents"

    processed_dir = repo_root / "data" / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = processed_dir / "points_residents.parquet"
    city_parquet_path = processed_dir / f"{city_code}_points_residents.parquet"
    csv_path = processed_dir / "points_residents.csv"

    df.to_parquet(parquet_path, index=False)
    df.to_parquet(city_parquet_path, index=False)
    df.to_csv(csv_path, index=False)

    total_raw_bbox_pop = float(grid_pop_raw.sum())
    total_active_residents = int(df["residents"].sum())
    diff_vs_ref = total_active_residents - ref_population
    pct_diff_vs_ref = (diff_vs_ref / ref_population) * 100.0 if ref_population > 0 else 0.0

    res_arr = df["residents"].to_numpy(dtype=np.int64)
    top_wards = sorted(ward_pop_totals.items(), key=lambda kv: kv[1], reverse=True)[:10]

    builtin_comparison = load_builtin_city_stats()

    report = {
        "phase": "30_population_grid",
        "city_code": cfg["code"],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "runtime_seconds": round(time.time() - t_start, 2),
        "grid_spec": {
            "grid_cell_m": grid_cell_m,
            "min_residents_per_point": min_res_threshold,
            "rows": rows,
            "cols": cols,
            "total_grid_cells": rows * cols,
            "active_demand_points": len(df),
            "dropped_sparse_cells": dropped_sparse_cells,
            "dropped_sparse_residents": round(dropped_sparse_residents, 1),
        },
        "population_crosscheck": {
            "reference_population_config": ref_population,
            "worldpop_2020_raw_bbox_sum": round(total_raw_bbox_pop, 1),
            "active_points_total_residents": total_active_residents,
            "difference_vs_reference": diff_vs_ref,
            "percent_difference_vs_reference": round(pct_diff_vs_ref, 2),
            "bbmp_wards_crosscheck": {
                "wards_loaded": len(ward_prepared),
                "bbmp_cells_count": bbmp_cell_count,
                "bbmp_worldpop_2020_residents": bbmp_total_residents,
                "non_bbmp_bmrda_and_hosur_residents": total_active_residents - bbmp_total_residents,
                "top_10_wards_by_residents": [{"ward": k, "residents": v} for k, v in top_wards],
            },
        },
        "point_distribution": {
            "point_count": len(df),
            "min_residents": int(res_arr.min()),
            "p25_residents": float(np.percentile(res_arr, 25)),
            "median_residents": float(np.median(res_arr)),
            "mean_residents": round(float(res_arr.mean()), 1),
            "p90_residents": float(np.percentile(res_arr, 90)),
            "max_residents": int(res_arr.max()),
        },
        "builtin_cities_comparison": builtin_comparison,
        "outputs": {
            "points_residents_parquet": str(parquet_path.relative_to(repo_root)),
            "city_points_residents_parquet": str(city_parquet_path.relative_to(repo_root)),
            "points_residents_csv": str(csv_path.relative_to(repo_root)),
        },
    }

    reports_dir = repo_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / "30_population_grid.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print("=== Phase 3 Population Grid Summary ===")
    print(f"Active demand points:        {len(df):,} / {rows * cols:,} grid cells ({grid_cell_m:.0f}m resolution)")
    print(f"Total residents (WorldPop):  {total_active_residents:,} (raw bbox sum: {total_raw_bbox_pop:,.0f})")
    print(f"Reference figure (config):   {ref_population:,}")
    print(f"Difference vs reference:     {diff_vs_ref:+,} ({pct_diff_vs_ref:+.2f}%)")
    print(f"Inside BBMP (243 wards):     {bbmp_total_residents:,} across {bbmp_cell_count:,} cells")
    print(f"Outside BBMP (BMRDA+Hosur):  {total_active_residents - bbmp_total_residents:,}")
    print(
        f"Residents/point stats:       min={res_arr.min():,}, median={np.median(res_arr):,.0f}, "
        f"mean={res_arr.mean():,.1f}, p90={np.percentile(res_arr, 90):,.0f}, max={res_arr.max():,}"
    )
    print(f"Saved outputs:               {parquet_path} & {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
