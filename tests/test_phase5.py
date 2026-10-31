import json
from pathlib import Path
import unittest
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestPhase5(unittest.TestCase):
    def test_routing_outputs_and_matrix_integrity(self):
        routable_p = REPO_ROOT / "data" / "processed" / "routable_demand_points.parquet"
        npz_p = REPO_ROOT / "data" / "processed" / "routing_matrix.npz"
        self.assertTrue(routable_p.is_file(), "routable_demand_points.parquet must exist")
        self.assertTrue(npz_p.is_file(), "routing_matrix.npz must exist")

        df = pd.read_parquet(routable_p)
        n = len(df)
        self.assertGreater(n, 3000, "Expected >3,000 routable demand points")
        self.assertEqual(int(df["residents"].sum()), int(df["jobs"].sum()), "Routable jobs must equal routable residents")

        data = np.load(npz_p, allow_pickle=True)
        for key in ("point_ids", "freeflow_seconds", "congested_seconds", "distance_m"):
            self.assertIn(key, data.files, f"Missing {key} in routing_matrix.npz")

        ff = data["freeflow_seconds"]
        cong = data["congested_seconds"]
        dist = data["distance_m"]

        self.assertEqual(ff.shape, (n, n))
        self.assertEqual(cong.shape, (n, n))
        self.assertEqual(dist.shape, (n, n))

        self.assertFalse(np.isnan(ff).any(), "freeflow_seconds contains NaN")
        self.assertFalse(np.isnan(cong).any(), "congested_seconds contains NaN")
        self.assertFalse(np.isnan(dist).any(), "distance_m contains NaN")
        self.assertTrue((ff > 0).all(), "freeflow_seconds must be strictly positive")
        self.assertTrue((dist > 0).all(), "distance_m must be strictly positive")
        self.assertTrue((ff < 1e6).all(), "All routable pairs must be reachable (< 1e6 sec)")

    def test_od_pairs_and_unroutable_logging(self):
        rep_path = REPO_ROOT / "reports" / "60_routing.json"
        od_csv = REPO_ROOT / "reports" / "60_od_test_pairs.csv"
        unr_csv = REPO_ROOT / "reports" / "60_unroutable_points.csv"

        self.assertTrue(rep_path.is_file(), "reports/60_routing.json must exist")
        self.assertTrue(od_csv.is_file(), "reports/60_od_test_pairs.csv must exist")
        self.assertTrue(unr_csv.is_file(), "reports/60_unroutable_points.csv must exist")

        rep = json.loads(rep_path.read_text(encoding="utf-8"))
        self.assertEqual(rep["phase"], "60_routing")
        self.assertGreater(rep["runtime_seconds"], 0.0)
        self.assertGreater(rep["peak_memory_mb"], 0.0)
        self.assertEqual(rep["matrix_summary"]["unreachable_pairs_count"], 0)

        od_pairs = rep["test_od_pairs"]
        self.assertGreaterEqual(len(od_pairs), 10, "Must report at least 10 known OD pairs")
        for pair in od_pairs:
            self.assertGreater(pair["distance_km"], 1.5, f"OD pair {pair['name']} distance too small")
            self.assertLess(pair["distance_km"], 70.0, f"OD pair {pair['name']} distance too large")
            self.assertGreater(pair["congested_minutes"], pair["freeflow_minutes"])

        unr_df = pd.read_csv(unr_csv)
        self.assertGreater(len(unr_df), 0, "Expected unroutable/fringe points log to be non-empty")
        self.assertIn("action", unr_df.columns)


if __name__ == "__main__":
    unittest.main()
