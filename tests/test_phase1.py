"""Unit and acceptance tests for Phase 1: Raw data acquisition."""

import json
import subprocess
import sys
import unittest
from pathlib import Path


class TestPhase1Download(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parent.parent
        self.raw_dir = self.repo_root / "data" / "raw"
        self.manifest_path = self.raw_dir / "manifest.json"

    def test_raw_manifest_and_files_exist(self) -> None:
        self.assertTrue(self.manifest_path.is_file(), "data/raw/manifest.json must exist")
        with open(self.manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        files = manifest.get("files", {})
        for required_key in [
            "population_raster",
            "admin_boundaries",
            "osm_extract_raw",
            "osm_extract_clipped",
        ]:
            self.assertIn(required_key, files, f"Missing {required_key} in manifest.json")
            entry = files[required_key]
            file_path = self.raw_dir / entry["filename"]
            self.assertTrue(file_path.is_file(), f"Missing raw file on disk: {file_path}")
            self.assertGreater(file_path.stat().st_size, 0, f"Raw file is empty: {file_path}")
            self.assertEqual(file_path.stat().st_size, entry["size_bytes"])
            self.assertEqual(len(entry.get("sha256", "")), 64, "Invalid SHA-256 hash length")

    def test_idempotent_rerun_skips_verified_files(self) -> None:
        script = self.repo_root / "pipeline" / "10_download.py"
        res = subprocess.run(
            [sys.executable, str(script), "--city", "cities/blr.yaml"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"10_download.py failed on rerun: {res.stderr}")
        self.assertIn("[SKIP]", res.stdout)
        self.assertNotIn("[FETCH]", res.stdout)
        self.assertNotIn("[CLIP]", res.stdout)


if __name__ == "__main__":
    unittest.main()
