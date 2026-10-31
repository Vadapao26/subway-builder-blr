import gzip
import json
import struct
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestPhase9Buildings(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bin_path = ROOT / "mod" / "data" / "BLR" / "buildings_index.bin"
        cls.bin_gz_path = ROOT / "mod" / "data" / "BLR" / "buildings_index.bin.gz"
        cls.tags_gz_path = ROOT / "mod" / "data" / "BLR" / "building_tags.json.gz"
        cls.coastline_gz_path = ROOT / "mod" / "data" / "BLR" / "coastline.geojson.gz"
        cls.ocean_gz_path = ROOT / "mod" / "data" / "BLR" / "ocean_depth_index.json.gz"
        cls.report_path = ROOT / "reports" / "95_buildings.json"
        cls.preview_png = ROOT / "reports" / "95_buildings_preview.png"
        cls.preview_svg = ROOT / "reports" / "95_buildings_preview.svg"

    def test_01_files_exist_and_non_empty(self) -> None:
        for p in [
            self.bin_path,
            self.bin_gz_path,
            self.tags_gz_path,
            self.coastline_gz_path,
            self.ocean_gz_path,
            self.report_path,
            self.preview_png,
            self.preview_svg,
        ]:
            self.assertTrue(p.exists(), f"Missing {p}")
            self.assertGreater(p.stat().st_size, 0, f"Empty {p}")

        self.assertGreater(self.bin_path.stat().st_size, 100_000_000)
        self.assertGreater(self.bin_gz_path.stat().st_size, 40_000_000)
        self.assertGreater(self.tags_gz_path.stat().st_size, 100_000)

    def test_02_sbib_v1_header_and_structure(self) -> None:
        buf = self.bin_path.read_bytes()
        self.assertGreaterEqual(len(buf), 88)

        magic, ver, flags, res = struct.unpack_from("<IBBH", buf, 0)
        self.assertEqual(magic, 1229079123, "Magic must be 0x49424253 (SBBI)")
        self.assertEqual(ver, 1, "Version must be 1")
        self.assertEqual(flags, 3, "Flags must have HEIGHTS_FLAG (1) and OSM_IDS_FLAG (2)")
        self.assertEqual(res, 0, "Reserved bytes must be 0")

        b_cnt, cols, rows, t_rings, t_coords, ne_cells, t_refs, t_osm = struct.unpack_from(
            "<IIIIIIII", buf, 8
        )
        self.assertEqual(b_cnt, 831116)
        self.assertEqual(t_rings, 831116)
        self.assertEqual(t_osm, 831116)
        self.assertEqual(t_coords, 4456911)
        self.assertGreater(ne_cells, 50000)
        self.assertGreater(t_refs, 831116)

        cell_size, max_fd, min_lon, min_lat, max_lon, max_lat = struct.unpack_from(
            "<dddddd", buf, 40
        )
        self.assertAlmostEqual(cell_size, 0.0009, places=6)
        self.assertGreaterEqual(max_fd, 7.0)
        self.assertLessEqual(max_fd, 50.0)
        self.assertGreaterEqual(min_lon, 77.3)
        self.assertLessEqual(max_lon, 77.95)
        self.assertGreaterEqual(min_lat, 12.65)
        self.assertLessEqual(max_lat, 13.3)

    def test_03_js_binary_detector_roundtrip_validation(self) -> None:
        # Run Node.js against the binary using Subway Builder's actual createBinaryDetector logic
        bin_file_js = str(self.bin_path).replace("\\", "/")
        node_script = f"""
const fs = require('fs');
const buf = fs.readFileSync('{bin_file_js}');
const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);

const BINARY_MAGIC = 1229079123;
const BINARY_VERSION = 1;
const HEADER_SIZE = 88;
const HEIGHTS_FLAG = 1;
const OSM_IDS_FLAG = 2;
const align8 = (n) => (n + 7) & ~7;

function computeSectionOffsets(h) {{
  let o = HEADER_SIZE;
  const bounds = o; o += h.buildingCount * 4 * 8;
  const foundationDepths = o; o += h.buildingCount * 4;
  o = align8(o);
  const buildingRingOffsets = o; o += (h.buildingCount + 1) * 4;
  const ringCoordOffsets = o; o += (h.totalRings + 1) * 4;
  o = align8(o);
  const coords = o; o += h.totalCoords * 2 * 8;
  const cellRowStarts = o; o += (h.rows + 1) * 4;
  const cellCols = o; o += h.nonEmptyCells * 4;
  const cellBuildingOffsets = o; o += (h.nonEmptyCells + 1) * 4;
  const cellBuildingIds = o; o += h.totalCellRefs * 4;
  let heights = 0;
  if (h.flags & HEIGHTS_FLAG) {{
    o = align8(o);
    heights = o;
    o += h.buildingCount * 4;
  }}
  let osmIdOffsets = 0;
  let osmIds = 0;
  if (h.flags & OSM_IDS_FLAG) {{
    o = align8(o);
    osmIdOffsets = o;
    o += (h.buildingCount + 1) * 4;
    o = align8(o);
    osmIds = o;
    o += h.totalOsmIds * 8;
  }}
  return {{ bounds, foundationDepths, buildingRingOffsets, ringCoordOffsets, coords, cellRowStarts, cellCols, cellBuildingOffsets, cellBuildingIds, heights, osmIdOffsets, osmIds, totalSize: o }};
}}

const view = new DataView(ab);
const magic = view.getUint32(0, true);
if (magic !== BINARY_MAGIC) throw new Error('Bad magic: ' + magic);
const ver = view.getUint8(4);
if (ver !== BINARY_VERSION) throw new Error('Bad version: ' + ver);

const header = {{
  flags: view.getUint8(5),
  buildingCount: view.getUint32(8, true),
  cols: view.getUint32(12, true),
  rows: view.getUint32(16, true),
  totalRings: view.getUint32(20, true),
  totalCoords: view.getUint32(24, true),
  nonEmptyCells: view.getUint32(28, true),
  totalCellRefs: view.getUint32(32, true),
  totalOsmIds: view.getUint32(36, true),
  cellSize: view.getFloat64(40, true),
  maxFoundationDepth: view.getFloat64(48, true),
  minLon: view.getFloat64(56, true),
  minLat: view.getFloat64(64, true),
  maxLon: view.getFloat64(72, true),
  maxLat: view.getFloat64(80, true)
}};

const layout = computeSectionOffsets(header);
if (ab.byteLength !== layout.totalSize) {{
  throw new Error(`Size mismatch: ${{ab.byteLength}} !== ${{layout.totalSize}}`);
}}

// Validate array views
const bounds = new Float64Array(ab, layout.bounds, header.buildingCount * 4);
const fdepths = new Float32Array(ab, layout.foundationDepths, header.buildingCount);
const heights = new Float32Array(ab, layout.heights, header.buildingCount);
const osmIds = new Float64Array(ab, layout.osmIds, header.totalOsmIds);
const cellRowStarts = new Uint32Array(ab, layout.cellRowStarts, header.rows + 1);
const cellCols = new Uint32Array(ab, layout.cellCols, header.nonEmptyCells);
const cellBuildingOffsets = new Uint32Array(ab, layout.cellBuildingOffsets, header.nonEmptyCells + 1);
const cellBuildingIds = new Uint32Array(ab, layout.cellBuildingIds, header.totalCellRefs);

// Spot-check first and last buildings
const b0_bounds = [bounds[0], bounds[1], bounds[2], bounds[3]];
const b0_fd = fdepths[0];
const b0_h = heights[0];
const b0_osm = osmIds[0];

console.log(JSON.stringify({{
  ok: true,
  b0_bounds,
  b0_fd,
  b0_h,
  b0_osm,
  totalSize: layout.totalSize
}}));
"""
        proc = subprocess.run(
            ["node", "-e", node_script],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        res = json.loads(proc.stdout.strip())
        self.assertTrue(res.get("ok"))
        self.assertGreater(res["b0_h"], 0)
        self.assertGreater(res["b0_fd"], 0)
        self.assertGreater(res["b0_osm"], 0)

    def test_04_building_tags_json(self) -> None:
        with gzip.open(self.tags_gz_path, "rt", encoding="utf-8") as f:
            tags = json.load(f)

        self.assertGreater(len(tags), 40000)
        # Verify sample entries have valid structure
        sample_keys = list(tags.keys())[:20]
        for k in sample_keys:
            self.assertTrue(k.isdigit())
            v = tags[k]
            self.assertIsInstance(v, dict)
            self.assertIn("building", v)


if __name__ == "__main__":
    unittest.main()
