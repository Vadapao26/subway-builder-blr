#!/usr/bin/env python3
"""
Shared Grid Utility: Variable-Step & Staggered Demand Grid for Subway Builder.
Supports both uniform grids and organic variable grids (e.g. 400m-600m alternating steps with row staggering).
"""

from __future__ import annotations
import math
from typing import Any, Dict, Tuple
import numpy as np


class GridSpec:
    def __init__(
        self,
        south: float,
        north: float,
        west: float,
        east: float,
        row_bounds: np.ndarray,
        col_bounds: np.ndarray,
        stagger_m: float = 0.0,
    ):
        self.south = float(south)
        self.north = float(north)
        self.west = float(west)
        self.east = float(east)
        self.row_bounds = np.asarray(row_bounds, dtype=np.float64)
        self.col_bounds = np.asarray(col_bounds, dtype=np.float64)
        self.rows = len(self.row_bounds) - 1
        self.cols = len(self.col_bounds) - 1

        self.mid_lat = 0.5 * (self.south + self.north)
        self.cos_lat = math.cos(math.radians(self.mid_lat))
        self.m_per_deg_lat = 111132.0
        self.m_per_deg_lon = 111320.0 * self.cos_lat

        self.stagger_m = float(stagger_m)
        self.stagger_deg_lon = self.stagger_m / self.m_per_deg_lon
        # Precompute stagger shift per row
        self.row_stagger_lons = np.zeros(self.rows, dtype=np.float64)
        for r in range(self.rows):
            if r % 2 == 1:
                self.row_stagger_lons[r] = self.stagger_deg_lon

    def map_lat_to_row(self, lats: np.ndarray | float) -> np.ndarray:
        lats_arr = np.asarray(lats, dtype=np.float64)
        r = np.searchsorted(self.row_bounds, lats_arr, side="right") - 1
        return np.clip(r, 0, self.rows - 1)

    def map_lon_to_col(self, lons: np.ndarray | float, rows: np.ndarray | int | None = None) -> np.ndarray:
        lons_arr = np.asarray(lons, dtype=np.float64).copy()
        if rows is not None and self.stagger_m > 0:
            r_arr = np.asarray(rows, dtype=np.int32)
            shift = np.where(r_arr % 2 == 1, self.stagger_deg_lon, 0.0)
            lons_arr -= shift
        c = np.searchsorted(self.col_bounds, lons_arr, side="right") - 1
        return np.clip(c, 0, self.cols - 1)

    def map_coords_to_row_col(self, lons: np.ndarray, lats: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        r = self.map_lat_to_row(lats)
        c = self.map_lon_to_col(lons, rows=r)
        return r, c

    def get_cell_bounds(self, r: int, c: int) -> Tuple[float, float, float, float]:
        """Returns (south, north, west, east)."""
        s = self.row_bounds[r]
        n = self.row_bounds[r + 1]
        shift = self.row_stagger_lons[r]
        w = self.col_bounds[c] + shift
        e = self.col_bounds[c + 1] + shift
        return s, n, w, e

    def get_cell_center(self, r: int, c: int) -> Tuple[float, float]:
        """Returns (lon, lat) of cell center."""
        s, n, w, e = self.get_cell_bounds(r, c)
        return 0.5 * (w + e), 0.5 * (s + n)


def create_grid_spec(cfg: Dict[str, Any]) -> GridSpec:
    bbox = cfg["bbox"]
    south, north = float(bbox["south"]), float(bbox["north"])
    west, east = float(bbox["west"]), float(bbox["east"])

    mid_lat = 0.5 * (south + north)
    cos_lat = math.cos(math.radians(mid_lat))
    m_per_deg_lat = 111132.0
    m_per_deg_lon = 111320.0 * cos_lat

    params = cfg.get("params", {})
    var_cfg = params.get("variable_grid", {})
    if var_cfg.get("enabled", False):
        col_steps = [float(x) for x in var_cfg.get("col_steps_m", [400, 480, 600, 420, 550, 450, 520, 580])]
        row_steps = [float(x) for x in var_cfg.get("row_steps_m", [420, 560, 400, 520, 480, 600, 450, 500])]
        stagger_m = float(var_cfg.get("stagger_offset_m", 240.0))

        row_bounds = [south]
        cur_lat = south
        idx = 0
        while cur_lat < north:
            cur_lat += row_steps[idx % len(row_steps)] / m_per_deg_lat
            row_bounds.append(min(north, cur_lat))
            idx += 1

        col_bounds = [west]
        cur_lon = west
        idx = 0
        while cur_lon < east:
            cur_lon += col_steps[idx % len(col_steps)] / m_per_deg_lon
            col_bounds.append(min(east, cur_lon))
            idx += 1

        return GridSpec(
            south,
            north,
            west,
            east,
            np.array(row_bounds, dtype=np.float64),
            np.array(col_bounds, dtype=np.float64),
            stagger_m=stagger_m,
        )

    # Standard uniform grid
    grid_cell_m = float(params.get("grid_cell_m", 1000.0))
    dlat = grid_cell_m / m_per_deg_lat
    dlon = grid_cell_m / m_per_deg_lon
    rows = int(math.ceil((north - south) / dlat))
    cols = int(math.ceil((east - west) / dlon))

    row_bounds = np.linspace(south, north, rows + 1, dtype=np.float64)
    col_bounds = np.linspace(west, east, cols + 1, dtype=np.float64)
    return GridSpec(south, north, west, east, row_bounds, col_bounds, stagger_m=0.0)
