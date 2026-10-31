from collections import defaultdict
import gzip
import json
from pathlib import Path
import unittest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestPhase6(unittest.TestCase):
    def test_demand_data_files_and_schema(self):
        json_p = REPO_ROOT / "mod" / "data" / "BLR" / "demand_data.json"
        gz_p = REPO_ROOT / "mod" / "data" / "BLR" / "demand_data.json.gz"
        self.assertTrue(json_p.is_file(), "mod/data/BLR/demand_data.json must exist")
        self.assertTrue(gz_p.is_file(), "mod/data/BLR/demand_data.json.gz must exist")

        with gzip.open(gz_p, "rt", encoding="utf-8") as f:
            data = json.load(f)

        points = data["points"]
        pops = data["pops"]
        self.assertGreater(len(points), 3000)
        self.assertGreater(len(pops), 20000)

        # Verify internal consistency
        total_res = sum(pt["residents"] for pt in points)
        total_jobs = sum(pt["jobs"] for pt in points)
        total_pop_size = sum(pop["size"] for pop in pops)
        self.assertEqual(total_res, total_jobs, "Total residents must equal total jobs")
        self.assertEqual(total_res, total_pop_size, "Total residents must equal sum of pop sizes")

        out_res = defaultdict(int)
        in_jobs = defaultdict(int)
        out_deg = defaultdict(int)
        expected_pop_ids = defaultdict(set)

        for pop in pops:
            self.assertGreater(pop["size"], 0)
            self.assertGreaterEqual(pop["drivingSeconds"], 0)
            self.assertGreaterEqual(pop["drivingDistance"], 0)
            rid = pop["residenceId"]
            jid = pop["jobId"]
            pid = pop["id"]
            out_res[rid] += pop["size"]
            in_jobs[jid] += pop["size"]
            out_deg[rid] += 1
            expected_pop_ids[rid].add(pid)
            expected_pop_ids[jid].add(pid)

        with open(REPO_ROOT / "cities" / "blr.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        top_k = int(cfg["params"]["top_k_destinations"])

        for pt in points:
            pid = pt["id"]
            self.assertEqual(out_res[pid], pt["residents"], f"Origin sum mismatch on {pid}")
            self.assertEqual(in_jobs[pid], pt["jobs"], f"Destination sum mismatch on {pid}")
            self.assertLessEqual(out_deg[pid], top_k, f"Origin {pid} exceeds top_k={top_k}")
            self.assertEqual(set(pt["popIds"]), expected_pop_ids[pid], f"popIds mismatch on {pid}")

    def test_commute_targets_and_report(self):
        rep_path = REPO_ROOT / "reports" / "70_gravity_pops.json"
        png_path = REPO_ROOT / "reports" / "70_commute_distribution.png"
        svg_path = REPO_ROOT / "reports" / "70_commute_distribution.svg"

        self.assertTrue(rep_path.is_file())
        self.assertTrue(png_path.is_file())
        self.assertTrue(svg_path.is_file())
        self.assertEqual(png_path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

        rep = json.loads(rep_path.read_text(encoding="utf-8"))
        self.assertTrue(rep["counts_and_totals"]["totals_internally_consistent"])
        self.assertTrue(rep["commute_distribution"]["mean_distance_in_target_range"])
        self.assertTrue(rep["commute_distribution"]["mean_duration_in_target_range"])
        self.assertTrue(rep["schema_validation"]["valid"])


if __name__ == "__main__":
    unittest.main()
