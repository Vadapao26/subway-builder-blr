import json
from pathlib import Path
import unittest
import pandas as pd
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestPhase4(unittest.TestCase):
    def test_employment_weights_and_demand_points_exist(self):
        ew_path = REPO_ROOT / "data" / "processed" / "employment_weights.parquet"
        dp_path = REPO_ROOT / "data" / "processed" / "demand_points.parquet"
        dp_csv = REPO_ROOT / "data" / "processed" / "demand_points.csv"

        self.assertTrue(ew_path.is_file(), "employment_weights.parquet must exist")
        self.assertTrue(dp_path.is_file(), "demand_points.parquet must exist")
        self.assertTrue(dp_csv.is_file(), "demand_points.csv must exist")

        ew_df = pd.read_parquet(ew_path)
        dp_df = pd.read_parquet(dp_path)
        self.assertEqual(len(ew_df), len(dp_df), "Row counts must match")
        self.assertGreater(len(dp_df), 2000)

        for col in ("id", "lon", "lat", "residents", "jobs", "has_plausible_employment_landuse", "has_named_boost"):
            self.assertIn(col, dp_df.columns, f"Missing column {col}")

        self.assertFalse(dp_df["jobs"].isna().any(), "jobs must not contain NaN")
        self.assertFalse(dp_df["residents"].isna().any(), "residents must not contain NaN")
        self.assertTrue((dp_df["jobs"] >= 0).all(), "jobs must be non-negative")
        self.assertTrue((dp_df["residents"] >= 0).all(), "residents must be non-negative")

    def test_sanity_checks_no_unexplained_jobs_and_exact_ratio(self):
        with open(REPO_ROOT / "cities" / "blr.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        emp_ratio = float(cfg["params"]["employment_ratio"])

        dp_df = pd.read_parquet(REPO_ROOT / "data" / "processed" / "demand_points.parquet")

        # Sanity Check 1: No cell has jobs without plausible employment land use unless it has a named boost
        unexplained = dp_df[
            (dp_df["jobs"] > 0) & (~dp_df["has_plausible_employment_landuse"]) & (~dp_df["has_named_boost"])
        ]
        self.assertEqual(
            len(unexplained),
            0,
            f"Found {len(unexplained)} cells with jobs > 0 lacking both employment landuse and named boost",
        )

        # Sanity Check 2: Total jobs matches employment_ratio * total_residents
        total_res = int(dp_df["residents"].sum())
        total_jobs = int(dp_df["jobs"].sum())
        expected_jobs = int(round(emp_ratio * total_res))
        self.assertEqual(total_jobs, expected_jobs, f"Total jobs {total_jobs} != expected {expected_jobs}")

    def test_maps_and_reports_exported(self):
        reports_dir = REPO_ROOT / "reports"
        for img_name in ("40_residents_map.png", "40_jobs_map.png", "50_demand_points_map.png"):
            p = reports_dir / img_name
            self.assertTrue(p.is_file(), f"Missing map image {img_name}")
            self.assertGreater(p.stat().st_size, 5000, f"Map image {img_name} is unexpectedly small")
            self.assertEqual(p.read_bytes()[:8], b"\x89PNG\r\n\x1a\n", f"{img_name} is not a valid PNG")

        svg_path = reports_dir / "50_demand_points_map.svg"
        self.assertTrue(svg_path.is_file(), "50_demand_points_map.svg must exist")
        self.assertIn("<svg", svg_path.read_text(encoding="utf-8"))

        r40 = json.loads((reports_dir / "40_employment_weights.json").read_text(encoding="utf-8"))
        r50 = json.loads((reports_dir / "50_demand_points.json").read_text(encoding="utf-8"))
        self.assertTrue(r50["sanity_checks"]["no_jobs_without_plausible_landuse_or_boost"])
        self.assertTrue(r50["sanity_checks"]["total_jobs_matches_ratio"])
        self.assertEqual(len(r50["named_cluster_job_totals"]), 7)
        for cluster in r50["named_cluster_job_totals"]:
            self.assertGreater(cluster["jobs"], 250000, f"Cluster {cluster['name']} has unexpectedly low jobs")


if __name__ == "__main__":
    unittest.main()
