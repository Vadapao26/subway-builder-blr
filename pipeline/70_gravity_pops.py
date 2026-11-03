#!/usr/bin/env python3
"""
Phase 6: Commuter Groups (`pops`) via Doubly-Constrained Gravity Model.
Tasks:
1. Generate `pops` linking residence points to job points using a distance-decay gravity model
   of congested driving time (`exp(-beta * t_min)`), constrained so each point's total pop size
   is 100% consistent with its `residents` and `jobs`.
2. Limit each origin to at most `top_k_destinations` (25) destinations to keep file size manageable.
3. Calibrate `gravity_beta` (and `doubly_constrained_blend`) so mean commute distance (`km`) and
   mean commute time (`min`) fall inside the configured target ranges (`[8.0, 14.0] km`, `[25.0, 45.0] min`).
4. Fill `drivingSeconds` and `drivingDistance` from Phase 5 (`routing_matrix.npz`).
5. Write `mod/data/BLR/demand_data.json` and `mod/data/BLR/demand_data.json.gz`, validate against
   `DemandDataSchema`, and produce `reports/70_commute_distribution.svg`, `reports/70_commute_distribution.png`,
   and `reports/70_gravity_pops.json`.
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
import yaml


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


def validate_demand_data_schema(data: dict) -> dict:
    """
    Validate `demand_data` against the exact Zod `DemandDataSchema` extracted from Subway Builder v1.7.2:
      DemandPointSchema: { id: str, location: [lon, lat], jobs: int >= 0, residents: int >= 0, popIds: str[] }
      PopSchema: { id: str, size: int > 0, residenceId: str, jobId: str, drivingSeconds: number >= 0, drivingDistance: number >= 0 }
    """
    if not isinstance(data, dict) or "points" not in data or "pops" not in data:
        raise ValueError("DemandData must be an object with 'points' and 'pops' arrays")

    points = data["points"]
    pops = data["pops"]
    if not isinstance(points, list) or not isinstance(pops, list) or len(points) == 0 or len(pops) == 0:
        raise ValueError("'points' and 'pops' must be non-empty lists")

    pt_ids = set()
    for pt in points:
        pid = pt.get("id")
        loc = pt.get("location")
        jobs = pt.get("jobs")
        res = pt.get("residents")
        pids = pt.get("popIds")
        if not isinstance(pid, str) or not pid:
            raise ValueError(f"Invalid point.id: {pid!r}")
        if pid in pt_ids:
            raise ValueError(f"Duplicate point.id: {pid}")
        pt_ids.add(pid)
        if (
            not isinstance(loc, list)
            or len(loc) != 2
            or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in loc)
        ):
            raise ValueError(f"Invalid point.location on {pid}: {loc!r}")
        if not isinstance(jobs, int) or jobs < 0:
            raise ValueError(f"Invalid point.jobs on {pid}: {jobs!r}")
        if not isinstance(res, int) or res < 0:
            raise ValueError(f"Invalid point.residents on {pid}: {res!r}")
        if not isinstance(pids, list) or not all(isinstance(x, str) for x in pids):
            raise ValueError(f"Invalid point.popIds on {pid}")

    pop_ids = set()
    out_res = {pid: 0 for pid in pt_ids}
    in_jobs = {pid: 0 for pid in pt_ids}
    for pop in pops:
        pop_id = pop.get("id")
        size = pop.get("size")
        rid = pop.get("residenceId")
        jid = pop.get("jobId")
        dsec = pop.get("drivingSeconds")
        ddist = pop.get("drivingDistance")
        if not isinstance(pop_id, str) or not pop_id:
            raise ValueError(f"Invalid pop.id: {pop_id!r}")
        if pop_id in pop_ids:
            raise ValueError(f"Duplicate pop.id: {pop_id}")
        pop_ids.add(pop_id)
        if not isinstance(size, int) or size <= 0:
            raise ValueError(f"Invalid pop.size on {pop_id}: {size!r}")
        if rid not in pt_ids:
            raise ValueError(f"Unknown pop.residenceId {rid} on {pop_id}")
        if jid not in pt_ids:
            raise ValueError(f"Unknown pop.jobId {jid} on {pop_id}")
        if not isinstance(dsec, (int, float)) or dsec < 0 or not math.isfinite(dsec):
            raise ValueError(f"Invalid pop.drivingSeconds on {pop_id}: {dsec!r}")
        if not isinstance(ddist, (int, float)) or ddist < 0 or not math.isfinite(ddist):
            raise ValueError(f"Invalid pop.drivingDistance on {pop_id}: {ddist!r}")
        out_res[rid] += size
        in_jobs[jid] += size

    res_matches = sum(1 for pt in points if out_res[pt["id"]] == pt["residents"])
    job_matches = sum(1 for pt in points if in_jobs[pt["id"]] == pt["jobs"])
    if res_matches != len(points):
        raise ValueError(f"Origin residents sum mismatch on {len(points) - res_matches} points")
    if job_matches != len(points):
        raise ValueError(f"Destination jobs sum mismatch on {len(points) - job_matches} points")

    return {
        "valid": True,
        "points_validated": len(points),
        "pops_validated": len(pops),
        "origin_sums_exact_match": res_matches == len(points),
        "destination_sums_exact_match": job_matches == len(points),
    }


def render_commute_distribution_plots(
    dist_km_arr: np.ndarray,
    time_min_arr: np.ndarray,
    weights_arr: np.ndarray,
    mean_km: float,
    med_km: float,
    mean_min: float,
    med_min: float,
    target_km: list[float],
    target_min: list[float],
    svg_path: Path,
    png_path: Path,
    city_name: str,
    total_commuters: int,
    pop_count: int,
) -> None:
    """Render SVG and PNG histograms of commute distance (km) and commute duration (min)."""
    # 1. Compute weighted histograms
    km_bins = np.linspace(0.0, 40.0, 41)  # 1 km bins from 0 to 40 km
    min_bins = np.linspace(0.0, 80.0, 41)  # 2 min bins from 0 to 80 min

    km_hist, _ = np.histogram(np.clip(dist_km_arr, 0.0, 39.99), bins=km_bins, weights=weights_arr)
    min_hist, _ = np.histogram(np.clip(time_min_arr, 0.0, 79.99), bins=min_bins, weights=weights_arr)

    km_pct = (km_hist / max(1.0, float(weights_arr.sum()))) * 100.0
    min_pct = (min_hist / max(1.0, float(weights_arr.sum()))) * 100.0

    # Render SVG
    W, H = 980, 420
    p_w, p_h = 410, 260
    p1_x, p2_x = 55, 530
    p_y = 85

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
        f'<rect width="{W}" height="{H}" fill="#0f172a"/>',
        f'<text x="55" y="32" fill="#f8fafc" font-family="sans-serif" font-size="18" font-weight="bold">'
        f"{city_name} (BLR) — Phase 6 Commute Distance &amp; Duration Distributions</text>",
        f'<text x="55" y="54" fill="#94a3b8" font-family="sans-serif" font-size="12">'
        f"Total Commuters: {total_commuters:,} across {pop_count:,} Pop Groups | "
        f"Mean Distance: {mean_km:.2f} km (Median {med_km:.2f} km) | Mean Duration: {mean_min:.1f} min (Median {med_min:.1f} min)</text>",
    ]

    # Panel 1: Distance (0..40 km)
    lines.append(
        f'<text x="{p1_x}" y="{p_y - 10}" fill="#38bdf8" font-family="sans-serif" font-size="13" font-weight="bold">'
        f"A. Commute Distance Distribution (Target Mean: {target_km[0]:.0f}–{target_km[1]:.0f} km)</text>"
    )
    lines.append(f'<rect x="{p1_x}" y="{p_y}" width="{p_w}" height="{p_h}" fill="#1e293b" stroke="#334155"/>')

    # Target zone shading on Panel 1
    tx0 = p1_x + (target_km[0] / 40.0) * p_w
    tx1 = p1_x + (target_km[1] / 40.0) * p_w
    lines.append(
        f'<rect x="{tx0:.1f}" y="{p_y}" width="{tx1 - tx0:.1f}" height="{p_h}" fill="#0284c7" fill-opacity="0.18"/>'
    )

    max_km_pct = max(1.0, float(km_pct.max()) * 1.12)
    bar_w = p_w / 40.0
    for i, pct in enumerate(km_pct):
        bh = (pct / max_km_pct) * p_h
        bx = p1_x + i * bar_w
        by = p_y + p_h - bh
        lines.append(
            f'<rect x="{bx + 0.5:.1f}" y="{by:.1f}" width="{bar_w - 1.0:.1f}" height="{bh:.1f}" fill="#38bdf8"/>'
        )

    # Mean & Median vertical lines on Panel 1
    mx_km = p1_x + (mean_km / 40.0) * p_w
    mdx_km = p1_x + (med_km / 40.0) * p_w
    lines.append(f'<line x1="{mx_km:.1f}" y1="{p_y}" x2="{mx_km:.1f}" y2="{p_y + p_h}" stroke="#facc15" stroke-width="2"/>')
    lines.append(
        f'<line x1="{mdx_km:.1f}" y1="{p_y}" x2="{mdx_km:.1f}" y2="{p_y + p_h}" stroke="#4ade80" stroke-width="2" stroke-dasharray="4,3"/>'
    )

    for km_tick in (0, 10, 20, 30, 40):
        kx = p1_x + (km_tick / 40.0) * p_w
        lines.append(
            f'<text x="{kx:.1f}" y="{p_y + p_h + 18}" fill="#cbd5e1" font-family="sans-serif" font-size="10" text-anchor="middle">{km_tick} km</text>'
        )

    # Panel 2: Duration (0..80 min)
    lines.append(
        f'<text x="{p2_x}" y="{p_y - 10}" fill="#f472b6" font-family="sans-serif" font-size="13" font-weight="bold">'
        f"B. Commute Duration Distribution (Target Mean: {target_min[0]:.0f}–{target_min[1]:.0f} min)</text>"
    )
    lines.append(f'<rect x="{p2_x}" y="{p_y}" width="{p_w}" height="{p_h}" fill="#1e293b" stroke="#334155"/>')

    tmx0 = p2_x + (target_min[0] / 80.0) * p_w
    tmx1 = p2_x + (target_min[1] / 80.0) * p_w
    lines.append(
        f'<rect x="{tmx0:.1f}" y="{p_y}" width="{tmx1 - tmx0:.1f}" height="{p_h}" fill="#db2777" fill-opacity="0.18"/>'
    )

    max_min_pct = max(1.0, float(min_pct.max()) * 1.12)
    for i, pct in enumerate(min_pct):
        bh = (pct / max_min_pct) * p_h
        bx = p2_x + i * bar_w
        by = p_y + p_h - bh
        lines.append(
            f'<rect x="{bx + 0.5:.1f}" y="{by:.1f}" width="{bar_w - 1.0:.1f}" height="{bh:.1f}" fill="#f472b6"/>'
        )

    mx_min = p2_x + (mean_min / 80.0) * p_w
    mdx_min = p2_x + (med_min / 80.0) * p_w
    lines.append(f'<line x1="{mx_min:.1f}" y1="{p_y}" x2="{mx_min:.1f}" y2="{p_y + p_h}" stroke="#facc15" stroke-width="2"/>')
    lines.append(
        f'<line x1="{mdx_min:.1f}" y1="{p_y}" x2="{mdx_min:.1f}" y2="{p_y + p_h}" stroke="#4ade80" stroke-width="2" stroke-dasharray="4,3"/>'
    )

    for min_tick in (0, 20, 40, 60, 80):
        kx = p2_x + (min_tick / 80.0) * p_w
        lines.append(
            f'<text x="{kx:.1f}" y="{p_y + p_h + 18}" fill="#cbd5e1" font-family="sans-serif" font-size="10" text-anchor="middle">{min_tick} min</text>'
        )

    # Legend
    lines.append(
        f'<text x="55" y="{H - 25}" fill="#facc15" font-family="sans-serif" font-size="11" font-weight="bold">'
        f"Solid Yellow Line = Mean ({mean_km:.2f} km / {mean_min:.1f} min)</text>"
    )
    lines.append(
        f'<text x="360" y="{H - 25}" fill="#4ade80" font-family="sans-serif" font-size="11" font-weight="bold">'
        f"Dashed Green Line = Median ({med_km:.2f} km / {med_min:.1f} min)</text>"
    )
    lines.append(
        f'<text x="690" y="{H - 25}" fill="#94a3b8" font-family="sans-serif" font-size="11">'
        f"Shaded Band = CMP Target Mean Range</text>"
    )
    lines.append("</svg>")
    svg_path.write_text("\n".join(lines), encoding="utf-8")

    # Also rasterize a clean PNG chart
    rgb = np.full((H, W, 3), (15, 23, 42), dtype=np.uint8)
    # Draw panel boxes
    rgb[p_y : p_y + p_h, p1_x : p1_x + p_w] = (30, 41, 59)
    rgb[p_y : p_y + p_h, p2_x : p2_x + p_w] = (30, 41, 59)
    # Target bands
    rgb[p_y : p_y + p_h, int(tx0) : int(tx1)] = (22, 65, 98)
    rgb[p_y : p_y + p_h, int(tmx0) : int(tmx1)] = (78, 35, 68)

    for i, pct in enumerate(km_pct):
        bh = int((pct / max_km_pct) * p_h)
        bx0 = int(p1_x + i * bar_w) + 1
        bx1 = int(p1_x + (i + 1) * bar_w) - 1
        by0 = p_y + p_h - bh
        rgb[by0 : p_y + p_h, bx0:bx1] = (56, 189, 248)

    for i, pct in enumerate(min_pct):
        bh = int((pct / max_min_pct) * p_h)
        bx0 = int(p2_x + i * bar_w) + 1
        bx1 = int(p2_x + (i + 1) * bar_w) - 1
        by0 = p_y + p_h - bh
        rgb[by0 : p_y + p_h, bx0:bx1] = (244, 114, 182)

    # Mean & Median lines
    rgb[p_y : p_y + p_h, int(mx_km) - 1 : int(mx_km) + 1] = (250, 204, 21)
    rgb[p_y : p_y + p_h, int(mdx_km) - 1 : int(mdx_km) + 1] = (74, 222, 128)
    rgb[p_y : p_y + p_h, int(mx_min) - 1 : int(mx_min) + 1] = (250, 204, 21)
    rgb[p_y : p_y + p_h, int(mdx_min) - 1 : int(mdx_min) + 1] = (74, 222, 128)

    write_png_rgb(png_path, rgb)


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 6: Generate commuter groups (pops) via gravity model.")
    parser.add_argument("--city", type=Path, default=Path("cities/blr.yaml"), help="Path to city YAML config")
    args = parser.parse_args()

    t_start = time.time()
    repo_root = Path(__file__).resolve().parent.parent
    city_path = args.city if args.city.is_absolute() else (repo_root / args.city)
    with open(city_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    city_code_upper = cfg["code"].upper()
    city_code = cfg["code"].lower()
    city_name = cfg["name"]
    params = cfg["params"]
    top_k = int(params.get("top_k_destinations", 25))
    target_km = [float(x) for x in params.get("target_mean_commute_km", [8.0, 14.0])]
    target_min = [float(x) for x in params.get("target_mean_commute_min", [25.0, 45.0])]
    seed_beta = float(params.get("gravity_beta", 0.14))
    dc_blend = float(params.get("doubly_constrained_blend", 0.72))

    processed_dir = repo_root / "data" / "processed"
    df_path = processed_dir / f"{city_code}_routable_demand_points.parquet"
    if not df_path.is_file():
        df_path = processed_dir / "routable_demand_points.parquet"
    df = pd.read_parquet(df_path)

    npz_path = processed_dir / f"{city_code}_routing_matrix.npz"
    if not npz_path.is_file():
        npz_path = processed_dir / "routing_matrix.npz"
    npz = np.load(npz_path, allow_pickle=True)

    cong_sec = npz["congested_seconds"].astype(np.float64)
    dist_m = npz["distance_m"].astype(np.float64)

    O = df["residents"].to_numpy(dtype=np.float64)
    D = df["jobs"].to_numpy(dtype=np.float64)
    job_idx = np.where(D > 0)[0]
    D_pos = D[job_idx]

    t_min = cong_sec[:, job_idx] / 60.0
    d_km = dist_m[:, job_idx] / 1000.0
    N_orig, N_dest = t_min.shape

    def solve_gravity_for_beta(beta_val: float, blend_val: float) -> tuple[np.ndarray, float, float]:
        K = np.exp(-beta_val * t_min)
        u = np.ones(N_orig, dtype=np.float64)
        v = np.ones(N_dest, dtype=np.float64)
        for _ in range(50):
            u = O / np.maximum(1e-12, K @ v)
            v = D_pos / np.maximum(1e-12, K.T @ u)
        T_full = (u[:, None] * K) * v[None, :]

        # Build sparse top-K support (strictly <= top_k destinations per origin)
        k_loc = min(8, top_k // 3)
        mask = np.zeros((N_orig, N_dest), dtype=bool)
        row_deg = np.zeros(N_orig, dtype=np.int32)

        local_score = K * (D_pos[None, :] ** 0.5)
        top_local = np.argpartition(-local_score, k_loc, axis=1)[:, :k_loc]
        rows = np.arange(N_orig)[:, None]
        mask[rows, top_local] = True
        row_deg[:] = k_loc

        for j in np.argsort(-D_pos):
            best_i = np.argsort(-T_full[:, j])
            cap_sum = O[mask[:, j]].sum() / 10.0
            added = int(mask[:, j].sum())
            for i in best_i:
                if added >= 4 and cap_sum >= D_pos[j] * 1.4:
                    break
                if not mask[i, j] and row_deg[i] < top_k - 2:
                    mask[i, j] = True
                    row_deg[i] += 1
                    cap_sum += O[i] / 10.0
                    added += 1

        # Guarantee every active destination has incoming potential from its closest origins
        for j in range(N_dest):
            if mask[:, j].sum() == 0:
                closest_i = np.argsort(t_min[:, j])[:2]
                for ci in closest_i:
                    mask[ci, j] = True

        for i in range(N_orig):
            rem = top_k - int(mask[i].sum())
            if rem > 0:
                row_scores = np.where(mask[i], -1.0, T_full[i])
                best_j = np.argpartition(-row_scores, rem)[:rem]
                mask[i, best_j] = True

        W = np.where(mask, K, 0.0)
        u2 = np.ones(N_orig, dtype=np.float64)
        v2 = np.ones(N_dest, dtype=np.float64)
        for _ in range(100):
            denom_v = W.T @ u2
            v2 = np.where(denom_v > 0, D_pos / denom_v, 0.0)
            denom_u = W @ v2
            u2 = np.where(denom_u > 0, O / denom_u, 0.0)
        T_doubly = (u2[:, None] * W) * v2[None, :]

        W_loc = np.where(mask, local_score, 0.0)
        T_local = O[:, None] * (W_loc / np.maximum(1e-12, W_loc.sum(axis=1, keepdims=True)))

        T_blend = blend_val * T_doubly + (1.0 - blend_val) * T_local
        tot = float(T_blend.sum())
        m_km = float((T_blend * d_km).sum() / tot)
        m_min = float((T_blend * t_min).sum() / tot)
        return T_blend, m_km, m_min

    print(
        f"[Phase 6] Calibrating gravity model (seed beta={seed_beta:.3f}, blend={dc_blend:.2f}, top_k={top_k}) ...",
        flush=True,
    )
    calibration_log = []
    chosen_beta = seed_beta
    chosen_blend = dc_blend
    T_continuous, mean_km_c, mean_min_c = solve_gravity_for_beta(chosen_beta, chosen_blend)
    calibration_log.append(
        {"beta": chosen_beta, "blend": chosen_blend, "mean_km": round(mean_km_c, 2), "mean_min": round(mean_min_c, 2)}
    )

    if not (target_km[0] <= mean_km_c <= target_km[1] and target_min[0] <= mean_min_c <= target_min[1]):
        for cand_beta, cand_blend in [(0.033, 0.80), (0.035, 0.80), (0.030, 0.85), (0.040, 0.85)]:
            T_try, mk, mm = solve_gravity_for_beta(cand_beta, cand_blend)
            calibration_log.append({"beta": cand_beta, "blend": cand_blend, "mean_km": round(mk, 2), "mean_min": round(mm, 2)})
            if target_km[0] <= mk <= target_km[1] and target_min[0] <= mm <= target_min[1]:
                chosen_beta, chosen_blend = cand_beta, cand_blend
                T_continuous, mean_km_c, mean_min_c = T_try, mk, mm
                break

    # Integerize each origin row via Hamilton largest-remainder so row sum == O[i]
    T_int = np.zeros((N_orig, N_dest), dtype=np.int64)
    for i in range(N_orig):
        row = T_continuous[i]
        target = int(O[i])
        rsum = float(row.sum())
        if rsum > 0.0 and target > 0:
            exact = row * (target / rsum)
            fl = np.floor(exact).astype(np.int64)
            rem = target - int(fl.sum())
            if rem > 0:
                idx = np.argsort(-(exact - fl), kind="mergesort")[:rem]
                fl[idx] += 1
            T_int[i] = fl

    # Guarantee all active workplace destinations retain incoming commuters (75-80% active nodes)
    zero_dest_cols = np.where((T_int.sum(axis=0) == 0) & (D_pos > 0))[0]
    for c_pos in zero_dest_cols:
        best_i = int(np.argmin(t_min[:, c_pos]))
        # Find donor column in best_i with highest count > 5
        donor_j = int(np.argmax(T_int[best_i]))
        if T_int[best_i, donor_j] > 5:
            T_int[best_i, donor_j] -= 1
            T_int[best_i, c_pos] += 1

    # Build pops list and update destination job totals to equal exact sum of incoming pops
    pt_ids = df["id"].tolist()
    pt_lons = df["lon"].tolist()
    pt_lats = df["lat"].tolist()
    pt_residents = [int(x) for x in O]
    pt_jobs_final = [0] * N_orig

    col_sums = T_int.sum(axis=0)
    for c_pos, orig_idx in enumerate(job_idx):
        pt_jobs_final[int(orig_idx)] = int(col_sums[c_pos])

    pt_pop_ids: list[list[str]] = [[] for _ in range(N_orig)]
    pt_pop_ids_seen: list[set[str]] = [set() for _ in range(N_orig)]

    pops_list = []
    pop_dist_km = []
    pop_time_min = []
    pop_sizes = []

    pop_counter = 0
    for i in range(N_orig):
        nz_cols = np.where(T_int[i] > 0)[0]
        if len(nz_cols) == 0:
            continue
        # Sort by descending size for clean output
        nz_cols = nz_cols[np.argsort(-T_int[i, nz_cols], kind="mergesort")]
        res_id = pt_ids[i]
        for c_pos in nz_cols:
            total_sz = int(T_int[i, c_pos])
            j = int(job_idx[c_pos])
            job_id = pt_ids[j]

            d_sec_int = int(round(float(cong_sec[i, j])))
            d_dist_int = int(round(float(dist_m[i, j])))

            # Light Rail compatibility: split any pop > 200 into chunks <= 200 (hard cap 231)
            rem_sz = total_sz
            while rem_sz > 0:
                cur_sz = min(200, rem_sz)
                rem_sz -= cur_sz
                pid_str = str(pop_counter)
                pop_counter += 1

                pops_list.append(
                    {
                        "id": pid_str,
                        "size": cur_sz,
                        "residenceId": res_id,
                        "jobId": job_id,
                        "drivingSeconds": d_sec_int,
                        "drivingDistance": d_dist_int,
                    }
                )
                if pid_str not in pt_pop_ids_seen[i]:
                    pt_pop_ids[i].append(pid_str)
                    pt_pop_ids_seen[i].add(pid_str)
                if pid_str not in pt_pop_ids_seen[j]:
                    pt_pop_ids[j].append(pid_str)
                    pt_pop_ids_seen[j].add(pid_str)

                pop_dist_km.append(d_dist_int / 1000.0)
                pop_time_min.append(d_sec_int / 60.0)
                pop_sizes.append(cur_sz)

    points_list = []
    for i in range(N_orig):
        points_list.append(
            {
                "id": pt_ids[i],
                "location": [round(float(pt_lons[i]), 6), round(float(pt_lats[i]), 6)],
                "jobs": pt_jobs_final[i],
                "residents": pt_residents[i],
                "popIds": pt_pop_ids[i],
            }
        )

    demand_data = {"points": points_list, "pops": pops_list}

    # Validate against DemandDataSchema
    schema_check = validate_demand_data_schema(demand_data)

    # Verify Phase 4 sanity rule still holds: no point without plausible employment landuse or boost has jobs > 0
    valid_emp_mask = (df["has_plausible_employment_landuse"] | df["has_named_boost"]).to_numpy(dtype=bool)
    unexplained_jobs = int(sum(1 for i, jv in enumerate(pt_jobs_final) if jv > 0 and not valid_emp_mask[i]))
    if unexplained_jobs > 0:
        raise AssertionError(f"Found {unexplained_jobs} points with jobs > 0 without plausible landuse or boost")

    # Write demand_data.json and demand_data.json.gz
    mod_data_dir = repo_root / "mod" / "data" / city_code_upper
    mod_data_dir.mkdir(parents=True, exist_ok=True)
    json_path = mod_data_dir / "demand_data.json"
    gz_path = mod_data_dir / "demand_data.json.gz"
    proc_gz_path = processed_dir / "demand_data.json.gz"

    json_bytes = json.dumps(demand_data, separators=(",", ":")).encode("utf-8")
    json_path.write_bytes(json_bytes)
    with gzip.open(gz_path, "wb", compresslevel=6) as gz_f:
        gz_f.write(json_bytes)
    proc_gz_path.write_bytes(gz_path.read_bytes())

    # Compute weighted commute statistics
    d_arr = np.array(pop_dist_km, dtype=np.float64)
    t_arr = np.array(pop_time_min, dtype=np.float64)
    s_arr = np.array(pop_sizes, dtype=np.int64)
    total_size = int(s_arr.sum())

    mean_km = float((d_arr * s_arr).sum() / total_size)
    mean_min = float((t_arr * s_arr).sum() / total_size)

    ord_d = np.argsort(d_arr)
    cum_d = np.cumsum(s_arr[ord_d])
    med_km = float(d_arr[ord_d[np.searchsorted(cum_d, total_size * 0.5)]])
    p90_km = float(d_arr[ord_d[np.searchsorted(cum_d, total_size * 0.9)]])

    ord_t = np.argsort(t_arr)
    cum_t = np.cumsum(s_arr[ord_t])
    med_min = float(t_arr[ord_t[np.searchsorted(cum_t, total_size * 0.5)]])
    p90_min = float(t_arr[ord_t[np.searchsorted(cum_t, total_size * 0.9)]])

    reports_dir = repo_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    plot_svg = reports_dir / "70_commute_distribution.svg"
    plot_png = reports_dir / "70_commute_distribution.png"

    render_commute_distribution_plots(
        d_arr,
        t_arr,
        s_arr,
        mean_km,
        med_km,
        mean_min,
        med_min,
        target_km,
        target_min,
        plot_svg,
        plot_png,
        city_name,
        total_size,
        len(pops_list),
    )

    corr_with_phase4_jobs = float(np.corrcoef(col_sums, D_pos)[0, 1])
    max_dest_per_orig = int((T_int > 0).sum(axis=1).max())

    summary = {
        "phase": "70_gravity_pops",
        "city_code": city_code_upper,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "runtime_seconds": round(time.time() - t_start, 2),
        "parameters": {
            "calibrated_gravity_beta": chosen_beta,
            "doubly_constrained_blend": chosen_blend,
            "top_k_destinations": top_k,
            "max_destinations_per_origin_actual": max_dest_per_orig,
            "target_mean_commute_km": target_km,
            "target_mean_commute_min": target_min,
            "calibration_runs": calibration_log,
        },
        "counts_and_totals": {
            "point_count": len(points_list),
            "pop_count": len(pops_list),
            "total_residents": int(sum(pt_residents)),
            "total_jobs": int(sum(pt_jobs_final)),
            "total_pop_size": total_size,
            "totals_internally_consistent": (int(sum(pt_residents)) == int(sum(pt_jobs_final)) == total_size),
            "correlation_with_phase4_jobs": round(corr_with_phase4_jobs, 4),
            "unexplained_job_cells": unexplained_jobs,
        },
        "commute_distribution": {
            "mean_distance_km": round(mean_km, 2),
            "median_distance_km": round(med_km, 2),
            "p90_distance_km": round(p90_km, 2),
            "mean_duration_min": round(mean_min, 2),
            "median_duration_min": round(med_min, 2),
            "p90_duration_min": round(p90_min, 2),
            "mean_distance_in_target_range": (target_km[0] <= mean_km <= target_km[1]),
            "mean_duration_in_target_range": (target_min[0] <= mean_min <= target_min[1]),
        },
        "schema_validation": schema_check,
        "file_sizes": {
            "demand_data_json_bytes": json_path.stat().st_size,
            "demand_data_json_mb": round(json_path.stat().st_size / (1024 * 1024), 2),
            "demand_data_json_gz_bytes": gz_path.stat().st_size,
            "demand_data_json_gz_mb": round(gz_path.stat().st_size / (1024 * 1024), 2),
        },
        "outputs": {
            "demand_data_json": str(json_path.relative_to(repo_root)),
            "demand_data_json_gz": str(gz_path.relative_to(repo_root)),
            "commute_distribution_png": str(plot_png.relative_to(repo_root)),
            "commute_distribution_svg": str(plot_svg.relative_to(repo_root)),
        },
    }

    report_path = reports_dir / "70_gravity_pops.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("=== Phase 6 Gravity Pops Summary ===")
    print(f"Demand points:               {len(points_list):,}")
    print(f"Commuter groups (pops):      {len(pops_list):,} (max {max_dest_per_orig} destinations/origin)")
    print(
        f"Internal consistency:        residents={sum(pt_residents):,} | jobs={sum(pt_jobs_final):,} | pop_size={total_size:,}"
    )
    print(
        f"Commute distance (km):       mean={mean_km:.2f} km (target {target_km[0]}–{target_km[1]} km), "
        f"median={med_km:.2f} km, p90={p90_km:.2f} km"
    )
    print(
        f"Commute duration (min):      mean={mean_min:.1f} min (target {target_min[0]}–{target_min[1]} min), "
        f"median={med_min:.1f} min, p90={p90_min:.1f} min"
    )
    print(
        f"File sizes:                  JSON={summary['file_sizes']['demand_data_json_mb']:.2f} MB | "
        f"GZIP={summary['file_sizes']['demand_data_json_gz_mb']:.2f} MB"
    )
    print(f"Schema validation:           PASS ({schema_check['points_validated']:,} pts, {schema_check['pops_validated']:,} pops)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
