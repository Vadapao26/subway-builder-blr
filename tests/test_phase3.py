import json
from pathlib import Path
import unittest
import pandas as pd
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestPhase3(unittest.TestCase):
    def test_points_residents_parquet_and_csv(self):
        p_parquet = REPO_ROOT / "data" / "processed" / "points_residents.parquet"
        p_csv = REPO_ROOT / "data" / "processed" / "points_residents.csv"
        self.assertTrue(p_parquet.is_file(), "points_residents.parquet must exist")
        self.assertTrue(p_csv.is_file(), "points_residents.csv must exist")

        df = pd.read_parquet(p_parquet)
        self.assertGreater(len(df), 2000, "Expected >2,000 demand points in BLR study box")

        for col in ("id", "cell_id", "lon", "lat", "location", "residents", "residents_raw"):
            self.assertIn(col, df.columns, f"Missing column {col}")

        # Acceptance: no negative or NaN values
        self.assertFalse(df["residents"].isna().any(), "residents must not contain NaN")
        self.assertFalse(df["residents_raw"].isna().any(), "residents_raw must not contain NaN")
        self.assertFalse(df["lon"].isna().any(), "lon must not contain NaN")
        self.assertFalse(df["lat"].isna().any(), "lat must not contain NaN")
        self.assertTrue((df["residents"] >= 0).all(), "residents must be non-negative")
        self.assertTrue((df["residents_raw"] >= 0).all(), "residents_raw must be non-negative")

        # Verify coordinates lie inside configured bbox
        with open(REPO_ROOT / "cities" / "blr.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        bbox = cfg["bbox"]
        self.assertTrue(((df["lat"] >= bbox["south"]) & (df["lat"] <= bbox["north"])).all())
        self.assertTrue(((df["lon"] >= bbox["west"]) & (df["lon"] <= bbox["east"])).all())

        # Verify unique IDs
        self.assertEqual(df["id"].nunique(), len(df), "Demand point IDs must be unique")

    def test_phase3_report_and_crosscheck(self):
        report_path = REPO_ROOT / "reports" / "30_population_grid.json"
        self.assertTrue(report_path.is_file(), "reports/30_population_grid.json must exist")
        with open(report_path, "r", encoding="utf-8") as f:
            rep = json.load(f)

        self.assertEqual(rep["phase"], "30_population_grid")
        self.assertEqual(rep["city_code"], "BLR")

        cc = rep["population_crosscheck"]
        self.assertEqual(cc["reference_population_config"], 13600000)
        self.assertGreater(cc["active_points_total_residents"], 12000000)
        self.assertLess(abs(cc["percent_difference_vs_reference"]), 10.0)
        self.assertEqual(cc["bbmp_wards_crosscheck"]["wards_loaded"], 243)
        self.assertGreater(cc["bbmp_wards_crosscheck"]["bbmp_worldpop_2020_residents"], 8000000)


if __name__ == "__main__":
    unittest.main()
