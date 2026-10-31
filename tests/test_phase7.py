import gzip
import json
from pathlib import Path
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestPhase7(unittest.TestCase):
    def test_roads_and_runways_geojson_schemas(self):
        roads_gz = REPO_ROOT / "mod" / "data" / "BLR" / "roads.geojson.gz"
        runways_gz = REPO_ROOT / "mod" / "data" / "BLR" / "runways_taxiways.geojson.gz"
        self.assertTrue(roads_gz.is_file(), "mod/data/BLR/roads.geojson.gz must exist")
        self.assertTrue(runways_gz.is_file(), "mod/data/BLR/runways_taxiways.geojson.gz must exist")

        with gzip.open(roads_gz, "rt", encoding="utf-8") as f:
            rj = json.load(f)
        self.assertEqual(rj["type"], "FeatureCollection")
        self.assertGreater(len(rj["features"]), 100000)

        valid_rc = {"highway", "major", "medium", "minor"}
        valid_st = {"normal", "tunnel", "bridge"}
        for feat in rj["features"][:2000]:
            self.assertEqual(feat["type"], "Feature")
            self.assertEqual(feat["geometry"]["type"], "LineString")
            self.assertGreaterEqual(len(feat["geometry"]["coordinates"]), 2)
            props = feat["properties"]
            self.assertIn(props["roadClass"], valid_rc)
            self.assertIn(props["structure"], valid_st)
            self.assertIsInstance(props["name"], str)

        with gzip.open(runways_gz, "rt", encoding="utf-8") as f:
            aj = json.load(f)
        self.assertEqual(aj["type"], "FeatureCollection")
        self.assertGreater(len(aj["features"]), 200)

        for feat in aj["features"]:
            self.assertEqual(feat["type"], "Feature")
            self.assertEqual(feat["geometry"]["type"], "Polygon")
            ring = feat["geometry"]["coordinates"][0]
            self.assertGreaterEqual(len(ring), 4)
            self.assertEqual(ring[0], ring[-1], "Polygon ring must be closed")
            props = feat["properties"]
            self.assertEqual(props["roadType"], "runway")
            self.assertGreater(props["area"], 0)

    def test_phase7_report_and_overlay_images(self):
        rep_path = REPO_ROOT / "reports" / "80_roads_runways.json"
        png_path = REPO_ROOT / "reports" / "80_roads_runways_overlay.png"
        svg_path = REPO_ROOT / "reports" / "80_roads_runways_overlay.svg"

        self.assertTrue(rep_path.is_file())
        self.assertTrue(png_path.is_file())
        self.assertTrue(svg_path.is_file())
        self.assertEqual(png_path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

        rep = json.loads(rep_path.read_text(encoding="utf-8"))
        self.assertTrue(rep["roads"]["schema_validation"]["valid"])
        self.assertTrue(rep["runways_taxiways"]["schema_validation"]["valid"])


if __name__ == "__main__":
    unittest.main()
