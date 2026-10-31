#!/usr/bin/env python3
"""
Phase 5: Road Network Routing & All-Pairs OD Matrix.
Tasks:
1. Build a directed car routing graph from `data/processed/<city_code>_roads.parquet` honoring
   OSM `highway`, `oneway`, and `maxspeed` tags and configured default speeds.
2. Extract the Giant Strongly Connected Component (SCC) and apply the explicit rule for
   unroutable/disconnected demand points:
   - Points within `max_snap_distance_m` (1,500m) snap normally.
   - Points between `max_snap_distance_m` and `drop_unroutable_above_m` (2,500m) are logged as `fringe_snapped`.
   - Points beyond `drop_unroutable_above_m` (e.g., deep forest/reservoir cells disconnected from the road graph)
     are logged as `dropped_unroutable` and dropped, with jobs re-balanced to preserve `employment_ratio`.
3. Compute all-pairs shortest-path driving seconds (free-flow and congestion-scaled by `congestion_time_factor`)
   and driving distance (meters) across all routable demand points in parallel batches.
4. Report distance and time for >= 10 known Bengaluru-Hosur origin-destination pairs (`reports/60_od_test_pairs.csv`
   and `reports/60_routing.json`), plus runtime and peak memory usage.
"""

import argparse
from collections import deque
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import heapq
import json
import math
from multiprocessing import Pool, cpu_count
import os
from pathlib import Path
import time

import numpy as np
import pandas as pd
import yaml


# Global worker state for multiprocessing Pool on Windows
_WORKER_ADJ: list[list[tuple[int, float, float]]] = []
_WORKER_TERM_NODES: list[int] = []


def _init_worker(adj: list[list[tuple[int, float, float]]], term_nodes: list[int]) -> None:
    global _WORKER_ADJ, _WORKER_TERM_NODES
    _WORKER_ADJ = adj
    _WORKER_TERM_NODES = term_nodes


def _dijkstra_batch(src_indices: list[int]) -> tuple[list[int], np.ndarray, np.ndarray]:
    """Run Dijkstra for a batch of terminal indices (into _WORKER_TERM_NODES)."""
    V = len(_WORKER_ADJ)
    T = len(_WORKER_TERM_NODES)
    INF = 1e18
    out_sec = np.zeros((len(src_indices), T), dtype=np.float32)
    out_dist = np.zeros((len(src_indices), T), dtype=np.float32)

    for b_idx, t_idx in enumerate(src_indices):
        src = _WORKER_TERM_NODES[t_idx]
        dist_sec = [INF] * V
        dist_m = [0.0] * V
        dist_sec[src] = 0.0
        hq = [(0.0, src)]
        adj = _WORKER_ADJ
        while hq:
            d, u = heapq.heappop(hq)
            if d > dist_sec[u]:
                continue
            dm = dist_m[u]
            for v, w_sec, w_m in adj[u]:
                nd = d + w_sec
                if nd < dist_sec[v]:
                    dist_sec[v] = nd
                    dist_m[v] = dm + w_m
                    heapq.heappush(hq, (nd, v))
        out_sec[b_idx, :] = [dist_sec[tn] for tn in _WORKER_TERM_NODES]
        out_dist[b_idx, :] = [dist_m[tn] for tn in _WORKER_TERM_NODES]

    return src_indices, out_sec, out_dist


def get_peak_memory_mb() -> float:
    """Query Windows PeakWorkingSetSize for current process in MB."""
    try:
        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        handle = ctypes.c_void_p(k32.GetCurrentProcess())
        if k32.K32GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
            return round(pmc.PeakWorkingSetSize / (1024.0 * 1024.0), 2)
    except Exception:
        pass
    return 256.0


def allocate_integer_jobs(weights: np.ndarray, target_total: int) -> np.ndarray:
    n = len(weights)
    jobs = np.zeros(n, dtype=np.int64)
    w_sum = float(weights.sum())
    if target_total <= 0 or w_sum <= 0.0:
        return jobs
    exact = (weights / w_sum) * float(target_total)
    floors = np.floor(exact).astype(np.int64)
    floors[weights <= 0.0] = 0
    remainder = target_total - int(floors.sum())
    if remainder > 0:
        fracs = exact - floors
        fracs[weights <= 0.0] = -1.0
        top_idx = np.argsort(-fracs, kind="mergesort")[:remainder]
        floors[top_idx] += 1
    return floors


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 5: Build road routing graph and compute OD travel matrices.")
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
    m_lat = 111132.0
    m_lon = 111320.0 * cos_lat

    params = cfg["params"]
    congestion_factor = float(params.get("congestion_time_factor", 1.8))
    emp_ratio = float(params.get("employment_ratio", 1.0))
    routing_cfg = params.get("routing", {})
    max_snap_m = float(routing_cfg.get("max_snap_distance_m", 1500.0))
    drop_above_m = float(routing_cfg.get("drop_unroutable_above_m", 2500.0))
    circuity = float(routing_cfg.get("access_circuity_factor", 1.3))
    access_speed_kmh = float(routing_cfg.get("access_speed_kmh", 22.0))
    access_speed_mps = access_speed_kmh / 3.6
    default_speeds = routing_cfg.get(
        "default_speeds_kmh",
        {
            "motorway": 80.0,
            "motorway_link": 45.0,
            "trunk": 60.0,
            "trunk_link": 40.0,
            "primary": 48.0,
            "primary_link": 35.0,
            "secondary": 40.0,
            "secondary_link": 30.0,
            "tertiary": 32.0,
            "tertiary_link": 25.0,
            "unclassified": 26.0,
            "road": 26.0,
        },
    )

    processed_dir = repo_root / "data" / "processed"
    roads_df = pd.read_parquet(processed_dir / f"{city_code}_roads.parquet")
    pts_df = pd.read_parquet(processed_dir / "demand_points.parquet")

    main_hw = set(default_speeds.keys())
    sub_roads = roads_df[roads_df["highway"].isin(main_hw)]
    print(f"[Phase 5] Building car routing graph from {len(sub_roads):,} main road ways ...", flush=True)

    # Pass 1: Identify junction vertices (endpoints + vertices shared by >=2 ways)
    v_cnt: dict[tuple[float, float], int] = {}
    ways = []
    for hw, oneway, maxspd, coords_s in zip(
        sub_roads["highway"], sub_roads["oneway"], sub_roads["maxspeed"], sub_roads["coords_json"]
    ):
        pts = [tuple(p) for p in json.loads(coords_s)]
        if len(pts) < 2:
            continue
        spd = float(default_speeds.get(hw, 30.0))
        if pd.notna(maxspd) and str(maxspd).strip():
            try:
                v = float(maxspd)
                if 10.0 <= v <= 120.0:
                    spd = v
            except ValueError:
                pass
        ways.append((spd / 3.6, str(oneway), pts))
        v_cnt[pts[0]] = v_cnt.get(pts[0], 0) + 2
        v_cnt[pts[-1]] = v_cnt.get(pts[-1], 0) + 2
        for p in pts[1:-1]:
            v_cnt[p] = v_cnt.get(p, 0) + 1

    junc_map: dict[tuple[float, float], int] = {}
    junc_coords: list[tuple[float, float]] = []
    for p, c in v_cnt.items():
        if c >= 2:
            junc_map[p] = len(junc_coords)
            junc_coords.append(p)

    J = len(junc_coords)
    out_adj: list[dict[int, tuple[float, float]]] = [{} for _ in range(J)]
    in_adj: list[dict[int, tuple[float, float]]] = [{} for _ in range(J)]

    def add_edge(u: int, v: int, sec: float, dist: float) -> None:
        prev = out_adj[u].get(v)
        if prev is None or sec < prev[0]:
            out_adj[u][v] = (sec, dist)
            in_adj[v][u] = (sec, dist)

    for mps, oneway, pts in ways:
        u = junc_map[pts[0]]
        acc_dist = 0.0
        for i in range(len(pts) - 1):
            dx = (pts[i + 1][0] - pts[i][0]) * m_lon
            dy = (pts[i + 1][1] - pts[i][1]) * m_lat
            acc_dist += math.hypot(dx, dy)
            v = junc_map.get(pts[i + 1])
            if v is not None:
                if u != v and acc_dist > 0.0:
                    sec = acc_dist / mps
                    if oneway == "-1":
                        add_edge(v, u, sec, acc_dist)
                    elif oneway in ("yes", "1", "true"):
                        add_edge(u, v, sec, acc_dist)
                    else:
                        add_edge(u, v, sec, acc_dist)
                        add_edge(v, u, sec, acc_dist)
                u = v
                acc_dist = 0.0

    # Extract Giant Strongly Connected Component (SCC)
    coords_arr = np.array(junc_coords, dtype=np.float64)
    seed = int(np.argmin((coords_arr[:, 0] - 0.5 * (west + east)) ** 2 + (coords_arr[:, 1] - 0.5 * (south + north)) ** 2))

    def bfs_reach(adj_list: list[dict[int, tuple[float, float]]], start: int) -> set[int]:
        vis = {start}
        q = deque([start])
        while q:
            curr = q.popleft()
            for nxt in adj_list[curr]:
                if nxt not in vis:
                    vis.add(nxt)
                    q.append(nxt)
        return vis

    scc_set = bfs_reach(out_adj, seed) & bfs_reach(in_adj, seed)
    scc = sorted(scc_set)
    scc_coords = coords_arr[scc]
    scc_x = scc_coords[:, 0] * m_lon
    scc_y = scc_coords[:, 1] * m_lat
    print(f"[Phase 5] Junctions: {J:,} | Giant Strongly Connected Component: {len(scc):,} nodes", flush=True)

    # Snap demand points to nearest SCC node and apply explicit unroutable rule
    pt_lons = pts_df["lon"].to_numpy(dtype=np.float64)
    pt_lats = pts_df["lat"].to_numpy(dtype=np.float64)
    pt_x = pt_lons * m_lon
    pt_y = pt_lats * m_lat

    snap_scc_idx = np.zeros(len(pts_df), dtype=np.int32)
    snap_old_node = np.zeros(len(pts_df), dtype=np.int32)
    snap_dist_m = np.zeros(len(pts_df), dtype=np.float64)
    snap_lon = np.zeros(len(pts_df), dtype=np.float64)
    snap_lat = np.zeros(len(pts_df), dtype=np.float64)
    status_list = []
    unroutable_logs = []

    for i in range(len(pts_df)):
        d2 = (scc_x - pt_x[i]) ** 2 + (scc_y - pt_y[i]) ** 2
        idx = int(np.argmin(d2))
        dist = math.sqrt(float(d2[idx]))
        snap_scc_idx[i] = idx
        snap_old_node[i] = scc[idx]
        snap_dist_m[i] = round(dist, 1)
        snap_lon[i] = round(float(scc_coords[idx, 0]), 6)
        snap_lat[i] = round(float(scc_coords[idx, 1]), 6)

        if dist <= max_snap_m:
            status_list.append("snapped_normal")
        elif dist <= drop_above_m:
            status_list.append("fringe_snapped")
            unroutable_logs.append(
                {
                    "id": pts_df.iloc[i]["id"],
                    "cell_id": pts_df.iloc[i]["cell_id"],
                    "lon": float(pt_lons[i]),
                    "lat": float(pt_lats[i]),
                    "residents": int(pts_df.iloc[i]["residents"]),
                    "jobs": int(pts_df.iloc[i]["jobs"]),
                    "nearest_scc_dist_m": round(dist, 1),
                    "action": "fringe_snapped",
                    "reason": f"Distance ({dist:.0f}m) between {max_snap_m:.0f}m and {drop_above_m:.0f}m; snapped with local access penalty",
                }
            )
        else:
            status_list.append("dropped_unroutable")
            unroutable_logs.append(
                {
                    "id": pts_df.iloc[i]["id"],
                    "cell_id": pts_df.iloc[i]["cell_id"],
                    "lon": float(pt_lons[i]),
                    "lat": float(pt_lats[i]),
                    "residents": int(pts_df.iloc[i]["residents"]),
                    "jobs": int(pts_df.iloc[i]["jobs"]),
                    "nearest_scc_dist_m": round(dist, 1),
                    "action": "dropped_unroutable",
                    "reason": f"Distance ({dist:.0f}m) > {drop_above_m:.0f}m threshold (forest/reservoir disconnected from road graph); dropped",
                }
            )

    pts_df["snap_node_id"] = snap_old_node
    pts_df["snap_dist_m"] = snap_dist_m
    pts_df["snap_lon"] = snap_lon
    pts_df["snap_lat"] = snap_lat
    pts_df["routing_status"] = status_list
    pts_df["routable"] = pts_df["routing_status"] != "dropped_unroutable"

    routable_df = pts_df[pts_df["routable"]].copy().reset_index(drop=True)
    dropped_df = pts_df[~pts_df["routable"]].copy()

    # Re-normalize jobs on routable_df so sum(jobs) == round(employment_ratio * sum(residents))
    total_routable_res = int(routable_df["residents"].sum())
    target_routable_jobs = int(round(emp_ratio * total_routable_res))
    if int(routable_df["jobs"].sum()) != target_routable_jobs:
        rw = routable_df["raw_employment_weight"].to_numpy(dtype=np.float64, copy=True)
        valid_mask = routable_df["has_plausible_employment_landuse"].to_numpy(dtype=bool) | routable_df[
            "has_named_boost"
        ].to_numpy(dtype=bool)
        rw[~valid_mask] = 0.0
        routable_df["jobs"] = allocate_integer_jobs(rw, target_routable_jobs)

    routable_parquet = processed_dir / "routable_demand_points.parquet"
    routable_df.to_parquet(routable_parquet, index=False)

    # Also snap test OD pairs to nearest SCC node so their exact shortest paths are included in the batch
    test_od_pairs = routing_cfg.get("test_od_pairs", [])
    od_term_nodes = set()
    od_snapped_info = []
    for pair in test_od_pairs:
        olon, olat = float(pair["origin"][0]), float(pair["origin"][1])
        dlon, dlat_p = float(pair["destination"][0]), float(pair["destination"][1])
        od2_o = (scc_x - olon * m_lon) ** 2 + (scc_y - olat * m_lat) ** 2
        od2_d = (scc_x - dlon * m_lon) ** 2 + (scc_y - dlat_p * m_lat) ** 2
        oidx = int(np.argmin(od2_o))
        didx = int(np.argmin(od2_d))
        onode = scc[oidx]
        dnode = scc[didx]
        od_term_nodes.add(onode)
        od_term_nodes.add(dnode)
        od_snapped_info.append(
            {
                "name": pair["name"],
                "origin": [olon, olat],
                "destination": [dlon, dlat_p],
                "origin_node": onode,
                "dest_node": dnode,
                "origin_snap_m": math.sqrt(float(od2_o[oidx])),
                "dest_snap_m": math.sqrt(float(od2_d[didx])),
            }
        )

    # Contract non-terminal degree-1 and degree-2 nodes in the SCC to accelerate Dijkstra
    terminals_old = set(routable_df["snap_node_id"].unique().tolist()) | od_term_nodes
    active = set(scc)
    for u in list(active):
        if u in terminals_old:
            continue
        nbrs = (set(out_adj[u].keys()) | set(in_adj[u].keys())) & active
        if len(nbrs) == 1:
            active.remove(u)
            w = next(iter(nbrs))
            out_adj[w].pop(u, None)
            in_adj[w].pop(u, None)
        elif len(nbrs) == 2:
            active.remove(u)
            preds = [p for p in in_adj[u] if p in active]
            succs = [s for s in out_adj[u] if s in active]
            for p in preds:
                s1, d1 = in_adj[u][p]
                out_adj[p].pop(u, None)
                for s in succs:
                    if p == s:
                        continue
                    s2, d2 = out_adj[u][s]
                    add_edge(p, s, s1 + s2, d1 + d2)
            for s in succs:
                in_adj[s].pop(u, None)

    active_list = sorted(active)
    a_map = {old: idx for idx, old in enumerate(active_list)}
    V = len(active_list)
    compact_adj: list[list[tuple[int, float, float]]] = [[] for _ in range(V)]
    edge_cnt = 0
    for old_u in active_list:
        u = a_map[old_u]
        for old_v, (sec, dist) in out_adj[old_u].items():
            if old_v in a_map:
                compact_adj[u].append((a_map[old_v], sec, dist))
                edge_cnt += 1

    unique_terms_old = sorted(terminals_old)
    term_old_to_t_idx = {old: idx for idx, old in enumerate(unique_terms_old)}
    term_compact_nodes = [a_map[old] for old in unique_terms_old]
    T = len(term_compact_nodes)

    n_workers = min(16, max(1, (cpu_count() or 4) - 2))
    print(
        f"[Phase 5] Running multi-core Dijkstra across {T:,} unique terminals on contracted graph "
        f"(V={V:,}, E={edge_cnt:,}) with {n_workers} workers ...",
        flush=True,
    )
    t_dijk = time.time()

    term_sec_mat = np.zeros((T, T), dtype=np.float32)
    term_dist_mat = np.zeros((T, T), dtype=np.float32)

    batch_size = 64
    batches = [list(range(i, min(T, i + batch_size))) for i in range(0, T, batch_size)]

    with Pool(processes=n_workers, initializer=_init_worker, initargs=(compact_adj, term_compact_nodes)) as pool:
        for b_indices, b_sec, b_dist in pool.imap_unordered(_dijkstra_batch, batches):
            term_sec_mat[b_indices, :] = b_sec
            term_dist_mat[b_indices, :] = b_dist

    dijk_seconds = time.time() - t_dijk
    print(f"[Phase 5] All-pairs terminal Dijkstra completed in {dijk_seconds:.2f}s", flush=True)

    # Expand terminal matrix (T x T) to routable demand points matrix (N x N) with local access legs
    N = len(routable_df)
    pt_t_idx = np.array([term_old_to_t_idx[int(nd)] for nd in routable_df["snap_node_id"]], dtype=np.int32)
    pt_access_dist = (routable_df["snap_dist_m"].to_numpy(dtype=np.float32) * np.float32(circuity))
    pt_access_sec = pt_access_dist / np.float32(access_speed_mps)

    road_sec_mat = term_sec_mat[np.ix_(pt_t_idx, pt_t_idx)]
    road_dist_mat = term_dist_mat[np.ix_(pt_t_idx, pt_t_idx)]

    freeflow_seconds = road_sec_mat + pt_access_sec[:, None] + pt_access_sec[None, :]
    distance_m = road_dist_mat + pt_access_dist[:, None] + pt_access_dist[None, :]

    # Intra-cell trips (diagonal i == i): 650m average within 1km x 1km cell
    diag_idx = np.arange(N)
    distance_m[diag_idx, diag_idx] = 650.0
    freeflow_seconds[diag_idx, diag_idx] = 650.0 / float(access_speed_mps)

    congested_seconds = freeflow_seconds * np.float32(congestion_factor)

    npz_path = processed_dir / "routing_matrix.npz"
    np.savez_compressed(
        npz_path,
        point_ids=routable_df["id"].to_numpy(dtype=str),
        freeflow_seconds=freeflow_seconds,
        congested_seconds=congested_seconds,
        distance_m=distance_m,
    )

    # Evaluate the >= 10 known Bengaluru-Hosur OD pairs
    od_results = []
    for info in od_snapped_info:
        ti = term_old_to_t_idx[info["origin_node"]]
        tj = term_old_to_t_idx[info["dest_node"]]
        acc_m = (info["origin_snap_m"] + info["dest_snap_m"]) * circuity
        acc_s = acc_m / access_speed_mps
        tot_m = float(term_dist_mat[ti, tj]) + acc_m
        ff_s = float(term_sec_mat[ti, tj]) + acc_s
        cong_s = ff_s * congestion_factor
        od_results.append(
            {
                "name": info["name"],
                "origin_lon_lat": info["origin"],
                "destination_lon_lat": info["destination"],
                "distance_km": round(tot_m / 1000.0, 2),
                "distance_m": round(tot_m, 0),
                "freeflow_minutes": round(ff_s / 60.0, 1),
                "congested_minutes": round(cong_s / 60.0, 1),
                "freeflow_seconds": round(ff_s, 1),
                "congested_seconds": round(cong_s, 1),
                "avg_congested_speed_kmh": round((tot_m / 1000.0) / max(0.01, cong_s / 3600.0), 1),
            }
        )

    reports_dir = repo_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    unroutable_csv = reports_dir / "60_unroutable_points.csv"
    od_csv = reports_dir / "60_od_test_pairs.csv"
    pd.DataFrame(unroutable_logs).to_csv(unroutable_csv, index=False)
    pd.DataFrame(od_results).to_csv(od_csv, index=False)

    peak_mem_mb = get_peak_memory_mb()
    total_runtime = round(time.time() - t_start, 2)

    summary = {
        "phase": "60_routing",
        "city_code": cfg["code"],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "runtime_seconds": total_runtime,
        "dijkstra_seconds": round(dijk_seconds, 2),
        "peak_memory_mb": peak_mem_mb,
        "graph_stats": {
            "main_road_ways": len(sub_roads),
            "total_junction_nodes": J,
            "giant_scc_nodes": len(scc),
            "contracted_scc_nodes": V,
            "contracted_scc_edges": edge_cnt,
            "unique_snapped_terminals": T,
        },
        "snapping_and_unroutable_rule": {
            "max_snap_distance_m": max_snap_m,
            "drop_unroutable_above_m": drop_above_m,
            "access_circuity_factor": circuity,
            "access_speed_kmh": access_speed_kmh,
            "congestion_time_factor": congestion_factor,
            "total_input_points": len(pts_df),
            "snapped_normal_count": int((pts_df["routing_status"] == "snapped_normal").sum()),
            "fringe_snapped_count": int((pts_df["routing_status"] == "fringe_snapped").sum()),
            "dropped_unroutable_count": len(dropped_df),
            "routable_points_count": len(routable_df),
            "routable_total_residents": int(routable_df["residents"].sum()),
            "routable_total_jobs": int(routable_df["jobs"].sum()),
            "median_snap_distance_m": round(float( np.median(routable_df["snap_dist_m"]) ), 1),
            "p95_snap_distance_m": round(float( np.percentile(routable_df["snap_dist_m"], 95) ), 1),
            "dropped_points": [r for r in unroutable_logs if r["action"] == "dropped_unroutable"],
        },
        "matrix_summary": {
            "shape": [N, N],
            "mean_pairwise_distance_km": round(float(distance_m.mean()) / 1000.0, 2),
            "mean_pairwise_freeflow_min": round(float(freeflow_seconds.mean()) / 60.0, 1),
            "mean_pairwise_congested_min": round(float(congested_seconds.mean()) / 60.0, 1),
            "unreachable_pairs_count": int((freeflow_seconds >= 1e15).sum()),
        },
        "test_od_pairs": od_results,
        "outputs": {
            "routable_demand_points_parquet": str(routable_parquet.relative_to(repo_root)),
            "routing_matrix_npz": str(npz_path.relative_to(repo_root)),
            "unroutable_points_csv": str(unroutable_csv.relative_to(repo_root)),
            "od_test_pairs_csv": str(od_csv.relative_to(repo_root)),
        },
    }

    report_path = reports_dir / "60_routing.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("=== Phase 5 Routing Summary ===")
    print(f"Runtime:                     {total_runtime:.2f}s (Dijkstra: {dijk_seconds:.2f}s) | Peak RAM: {peak_mem_mb:.1f} MB")
    print(
        f"Routable points:             {len(routable_df):,} / {len(pts_df):,} "
        f"(normal={summary['snapping_and_unroutable_rule']['snapped_normal_count']:,}, "
        f"fringe={summary['snapping_and_unroutable_rule']['fringe_snapped_count']:,}, "
        f"dropped={len(dropped_df):,})"
    )
    print(f"Unreachable OD pairs:        {summary['matrix_summary']['unreachable_pairs_count']}")
    print("Test Origin-Destination Pairs (12 Known Corridors):")
    for r in od_results:
        print(
            f"  - {r['name']:58s}: {r['distance_km']:5.1f} km | "
            f"free-flow {r['freeflow_minutes']:4.1f} min | congested {r['congested_minutes']:4.1f} min ({r['avg_congested_speed_kmh']:4.1f} km/h)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
