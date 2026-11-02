import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestPhase10ModPackaging(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod_dir = ROOT / "mod"
        cls.data_dir = ROOT / "mod" / "data" / "BLR"
        cls.manifest_path = cls.mod_dir / "manifest.json"
        cls.index_path = cls.mod_dir / "index.js"
        cls.thumb_png = cls.mod_dir / "thumbnail.png"
        cls.thumb_svg = cls.mod_dir / "thumbnail.svg"
        cls.report_path = ROOT / "reports" / "99_package_mod.json"

    def test_01_mod_metadata_files(self) -> None:
        for p in [self.manifest_path, self.index_path, self.thumb_png, self.thumb_svg, self.report_path]:
            self.assertTrue(p.exists(), f"Missing {p}")
            self.assertGreater(p.stat().st_size, 0)

        with open(self.manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        self.assertEqual(manifest["id"], "blr-bengaluru-hosur")
        self.assertEqual(manifest["name"], "Bengaluru + Hosur (BLR)")
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["main"], "index.js")

        index_js = self.index_path.read_text(encoding="utf-8")
        self.assertIn("registerCity", index_js)
        self.assertIn("BLR", index_js)

    def test_02_all_mod_data_files_present(self) -> None:
        required_data_files = [
            "demand_data.json.gz",
            "roads.geojson.gz",
            "runways_taxiways.geojson.gz",
            "tiles.pmtiles",
            "foundations.pmtiles",
            "buildings_index.bin.gz",
            "building_tags.json.gz",
            "coastline.geojson.gz",
            "ocean_depth_index.json.gz",
        ]
        for fn in required_data_files:
            fp = self.data_dir / fn
            self.assertTrue(fp.exists(), f"Missing {fp}")
            self.assertGreater(fp.stat().st_size, 10, f"File {fn} is too small")

    def test_03_report_metrics(self) -> None:
        with open(self.report_path, "r", encoding="utf-8") as f:
            rep = json.load(f)

        self.assertEqual(rep["phase"], 10)
        self.assertEqual(rep["status"], "PASS")
        self.assertEqual(rep["city_code"], "BLR")
        self.assertGreater(rep["total_data_bytes"], 100_000_000)

    def test_04_config_json_schema(self) -> None:
        cfg_path = self.data_dir / "config.json"
        self.assertTrue(cfg_path.exists())
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual(cfg["code"], "BLR")
        self.assertEqual(cfg["name"], "Bengaluru")
        self.assertEqual(len(cfg["bbox"]), 4)
        self.assertEqual(cfg["creator"], "Vadapao26")
        self.assertEqual(cfg["country"], "IN")
        self.assertNotIn("minZoom", cfg)
        self.assertNotIn("maxZoom", cfg)


if __name__ == "__main__":
    unittest.main()
