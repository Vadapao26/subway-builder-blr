"""Unit and acceptance tests for Phase 2: OSM extraction and coverage report."""

import json
import math
from pathlib import Path
import sys
import unittest

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
from importlib import import_module

phase2_mod = import_module("20_extract_osm")


class TestPhase2OsmExtraction(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parent.parent
        self.reports_dir = self.repo_root / "reports"
        self.processed_dir = self.repo_root / "data" / "processed"

    def test_geometry_helpers(self) -> None:
        # 100m x 100m square near equator (cos_lat = 1.0)
        deg = 100.0 / 111132.0
        square = [(0.0, 0.0), (deg, 0.0), (deg, deg), (0.0, deg), (0.0, 0.0)]
        area = phase2_mod.polygon_area_m2(square, cos_lat=1.0)
        self.assertAlmostEqual(area, 10000.0, delta=50.0)

        line = [(0.0, 0.0), (0.0, deg)]
        length = phase2_mod.polyline_length_m(line, cos_lat=1.0)
        self.assertAlmostEqual(length, 100.0, delta=0.5)

    def test_coverage_report_and_map_exist(self) -> None:
        csv_path = self.reports_dir / "20_coverage_grid.csv"
        png_path = self.reports_dir / "20_coverage_map.png"
        svg_path = self.reports_dir / "20_coverage_map.svg"
        json_path = self.reports_dir / "20_extract_osm.json"

        for p in (csv_path, png_path, svg_path, json_path):
            self.assertTrue(p.is_file(), f"Missing Phase 2 report file: {p}")
            self.assertGreater(p.stat().st_size, 0, f"Empty Phase 2 report file: {p}")

        df = pd.read_csv(csv_path)
        for col in [
            "cell_id",
            "row",
            "col",
            "building_count",
            "buildings_with_levels_or_height",
            "share_with_levels_or_height",
            "building_footprint_m2",
            "road_km_per_km2",
            "sparse_flag",
        ]:
            self.assertIn(col, df.columns)
        self.assertFalse(df["building_count"].isna().any())
        self.assertTrue((df["building_footprint_m2"] >= 0).all())
        self.assertTrue((df["road_km_per_km2"] >= 0).all())

        with open(json_path, "r", encoding="utf-8") as f:
            summary = json.load(f)
        self.assertGreater(summary["counts"]["buildings_in_study_bbox"], 500000)
        self.assertGreater(summary["counts"]["roads_in_study_bbox"], 100000)
        self.assertGreater(summary["counts"]["airport_features"], 50)

    def test_extracted_parquet_tables_exist(self) -> None:
        for table in ["buildings", "roads", "landuse", "airports", "water", "parks", "railways", "pois"]:
            p = self.processed_dir / f"blr_{table}.parquet"
            self.assertTrue(p.is_file(), f"Missing extracted Parquet table: {p}")
            self.assertGreater(p.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
