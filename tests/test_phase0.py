"""Unit and smoke tests for Phase 0 setup and repository layout."""

import subprocess
import sys
from pathlib import Path
import unittest


class TestPhase0Setup(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parent.parent

    def test_required_files_exist(self) -> None:
        required = [
            "cities/blr.yaml",
            "pipeline/00_setup_check.py",
            "pipeline/10_download.py",
            "pipeline/20_extract_osm.py",
            "pipeline/30_population_grid.py",
            "pipeline/40_employment_weights.py",
            "pipeline/50_demand_points.py",
            "pipeline/60_routing.py",
            "pipeline/70_gravity_pops.py",
            "pipeline/80_roads_runways.py",
            "pipeline/90_tiles.py",
            "pipeline/95_buildings.py",
            "pipeline/99_package_mod.py",
            "docs/data-sources.md",
            "docs/assumptions.md",
            "docs/verification-log.md",
            "scripts/serve-data.js",
            "README.md",
        ]
        for rel in required:
            p = self.repo_root / rel
            self.assertTrue(p.is_file(), f"Missing required file: {rel}")
            self.assertGreater(p.stat().st_size, 0, f"Empty file: {rel}")

    def test_00_setup_check_runs(self) -> None:
        script = self.repo_root / "pipeline" / "00_setup_check.py"
        res = subprocess.run(
            [sys.executable, str(script), "--city", "cities/blr.yaml"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"00_setup_check.py failed: {res.stderr}")
        self.assertTrue((self.repo_root / "reports" / "00_setup_check.json").is_file())


if __name__ == "__main__":
    unittest.main()
