import gzip
import json
import socket
import struct
import subprocess
import time
import unittest
import urllib.request
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from pipeline import (  # noqa: F401
    osm_pbf_utils,
)
import importlib.util

ROOT = Path(__file__).resolve().parent.parent

spec90 = importlib.util.spec_from_file_location(
    "tiles_90", ROOT / "pipeline" / "90_tiles.py"
)
tiles_90 = importlib.util.module_from_spec(spec90)
assert spec90.loader is not None
spec90.loader.exec_module(tiles_90)


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7


def _parse_pmtiles_dir(raw_dir: bytes) -> list[tuple[int, int, int, int]]:
    pos = 0
    n, pos = _read_varint(raw_dir, pos)
    tids, rls, lens, offs = [], [], [], []
    last = 0
    for _ in range(n):
        d, pos = _read_varint(raw_dir, pos)
        last += d
        tids.append(last)
    for _ in range(n):
        rl, pos = _read_varint(raw_dir, pos)
        rls.append(rl)
    for _ in range(n):
        ln, pos = _read_varint(raw_dir, pos)
        lens.append(ln)
    for i in range(n):
        o, pos = _read_varint(raw_dir, pos)
        offs.append(offs[i - 1] + lens[i - 1] if (o == 0 and i > 0) else o - 1)
    return list(zip(tids, rls, lens, offs))


class TestPhase8VectorTiles(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tiles_path = ROOT / "mod" / "data" / "BLR" / "tiles.pmtiles"
        cls.foundations_path = ROOT / "mod" / "data" / "BLR" / "foundations.pmtiles"
        cls.report_path = ROOT / "reports" / "90_tiles.json"
        cls.preview_png = ROOT / "reports" / "90_tiles_preview.png"
        cls.flat_npz = ROOT / "data" / "processed" / "blr_buildings_flat.npz"

    def test_01_files_and_report_exist(self) -> None:
        self.assertTrue(self.tiles_path.exists(), "tiles.pmtiles missing")
        self.assertTrue(self.foundations_path.exists(), "foundations.pmtiles missing")
        self.assertTrue(self.report_path.exists(), "reports/90_tiles.json missing")
        self.assertTrue(self.preview_png.exists(), "reports/90_tiles_preview.png missing")
        self.assertTrue(self.flat_npz.exists(), "blr_buildings_flat.npz missing")

        self.assertGreater(self.tiles_path.stat().st_size, 10_000_000)
        self.assertGreater(self.foundations_path.stat().st_size, 5_000_000)
        self.assertGreater(self.preview_png.stat().st_size, 10_000)

        with open(self.report_path, "r", encoding="utf-8") as f:
            rep = json.load(f)
        self.assertEqual(rep["phase"], 8)
        self.assertEqual(rep["status"], "PASS")
        self.assertEqual(rep["total_unique_buildings_indexed"], 831116)
        for z in range(5, 16):
            self.assertGreater(rep["zoom_tile_counts"][str(z)], 0)

    def test_02_pmtiles_v3_headers_and_metadata(self) -> None:
        for path, exp_min_z, exp_layers in [
            (
                self.tiles_path,
                5,
                {
                    "water",
                    "parks",
                    "commercial",
                    "airports",
                    "buildings",
                    "city_labels",
                    "suburb_labels",
                    "neighborhood_labels",
                    "ocean_foundations",
                },
            ),
            (self.foundations_path, 13, {"foundations"}),
        ]:
            with open(path, "rb") as f:
                hdr = f.read(127)
                self.assertEqual(hdr[:8], b"PMTiles\x03")
                root_off, root_len, meta_off, meta_len, leaf_off, leaf_len, data_off, data_len = (
                    struct.unpack_from("<QQQQQQQQ", hdr, 8)
                )
                n_addr, n_entries, n_contents = struct.unpack_from("<QQQ", hdr, 72)
                clustered, int_comp, tile_comp, tile_type, min_z, max_z = (
                    struct.unpack_from("<BBBBBB", hdr, 96)
                )
                self.assertEqual(clustered, 1)
                self.assertEqual(int_comp, 2)
                self.assertEqual(tile_comp, 2)
                self.assertEqual(tile_type, 1)
                self.assertEqual(min_z, exp_min_z)
                self.assertEqual(max_z, 15)
                self.assertGreater(n_entries, 100)
                self.assertGreater(data_len, 1_000_000)

                f.seek(meta_off)
                meta = json.loads(gzip.decompress(f.read(meta_len)).decode("utf-8"))
                layer_ids = {l["id"] for l in meta["vector_layers"]}
                self.assertEqual(layer_ids, exp_layers)

    def test_03_mvt_building_id_parity_and_winding(self) -> None:
        # Load OSM IDs from blr_buildings.parquet
        tbl = pq.read_table(
            ROOT / "data" / "processed" / "blr_buildings.parquet", columns=["osm_id"]
        )
        valid_osm_ids = set(tbl["osm_id"].to_pylist())

        # Inspect Central Bengaluru zoom-14 tile (14, 11723, 7597)
        cbd_tx, cbd_ty = tiles_90.lonlat_to_tile(77.595, 12.972, 14)
        target_tid = tiles_90.zxy_to_tile_id(14, cbd_tx, cbd_ty)

        with open(self.tiles_path, "rb") as f:
            hdr = f.read(127)
            root_off, root_len, _, _, leaf_off, _, data_off, _ = struct.unpack_from(
                "<QQQQQQQQ", hdr, 8
            )
            f.seek(root_off)
            entries = _parse_pmtiles_dir(gzip.decompress(f.read(root_len)))
            if entries[0][1] == 0:
                chosen = None
                for tid, _, ln, off in entries:
                    if tid <= target_tid:
                        chosen = (ln, off)
                assert chosen is not None
                f.seek(leaf_off + chosen[1])
                entries = _parse_pmtiles_dir(gzip.decompress(f.read(chosen[0])))

            mvt_bytes = b""
            for tid, rl, ln, off in entries:
                if tid <= target_tid < tid + rl:
                    f.seek(data_off + off)
                    mvt_bytes = gzip.decompress(f.read(ln))
                    break

        self.assertGreater(len(mvt_bytes), 1000, "CBD zoom-14 tile should be non-empty")

        # Parse MVT layers and verify `buildings` features have valid OSM IDs and clockwise exterior rings
        p = 0
        found_buildings = 0
        while p < len(mvt_bytes):
            _, p = _read_varint(mvt_bytes, p)
            ln, p = _read_varint(mvt_bytes, p)
            lbuf = mvt_bytes[p : p + ln]
            p += ln

            lp = 0
            lname = ""
            keys = []
            vals = []
            feats = []
            while lp < len(lbuf):
                ltag, lp = _read_varint(lbuf, lp)
                lfn, lwt = ltag >> 3, ltag & 7
                if lwt == 0:
                    _, lp = _read_varint(lbuf, lp)
                elif lwt == 2:
                    lln, lp = _read_varint(lbuf, lp)
                    sub = lbuf[lp : lp + lln]
                    lp += lln
                    if lfn == 1:
                        lname = sub.decode("utf-8")
                    elif lfn == 2:
                        feats.append(sub)
                    elif lfn == 3:
                        keys.append(sub.decode("utf-8"))
                    elif lfn == 4:
                        vtag, vp = _read_varint(sub, 0)
                        if (vtag >> 3) == 4:
                            vval, _ = _read_varint(sub, vp)
                            vals.append(vval)
                        else:
                            vals.append(None)

            if lname == "buildings":
                self.assertEqual(keys, ["id", "height"])
                for fb in feats[:200]:
                    fp = 0
                    fid = None
                    ftags = []
                    gints = []
                    while fp < len(fb):
                        ftag, fp = _read_varint(fb, fp)
                        ffn, fwt = ftag >> 3, ftag & 7
                        if fwt == 0:
                            v, fp = _read_varint(fb, fp)
                            if ffn == 1:
                                fid = v
                        elif fwt == 2:
                            fln, fp = _read_varint(fb, fp)
                            fsub = fb[fp : fp + fln]
                            fp += fln
                            sp = 0
                            arr = []
                            while sp < len(fsub):
                                sv, sp = _read_varint(fsub, sp)
                                arr.append(sv)
                            if ffn == 2:
                                ftags = arr
                            elif ffn == 4:
                                gints = arr

                    self.assertIsNotNone(fid)
                    self.assertIn(fid, valid_osm_ids)
                    tag_id = vals[ftags[1]]
                    tag_h = vals[ftags[3]]
                    self.assertEqual(tag_id, fid)
                    self.assertGreater(tag_h, 0)

                    # Verify clockwise polygon winding in MVT coordinates
                    idx = 0
                    cx, cy = 0, 0
                    pts = []
                    while idx < len(gints):
                        cmd = gints[idx]
                        idx += 1
                        cid, cnt = cmd & 7, cmd >> 3
                        if cid in (1, 2):
                            for _ in range(cnt):
                                cx += tiles_90.unzigzag(gints[idx])
                                cy += tiles_90.unzigzag(gints[idx + 1])
                                idx += 2
                                pts.append((cx, cy))
                    area2 = sum(
                        pts[i][0] * pts[(i + 1) % len(pts)][1]
                        - pts[(i + 1) % len(pts)][0] * pts[i][1]
                        for i in range(len(pts))
                    )
                    self.assertGreater(area2, 0, "MVT exterior polygon ring must have positive clockwise area")
                    found_buildings += 1

        self.assertGreater(found_buildings, 50)

    def test_04_serve_data_mvt_and_range_endpoints(self) -> None:
        cbd_tx, cbd_ty = tiles_90.lonlat_to_tile(77.595, 12.972, 14)
        serve_js = str(ROOT / "scripts" / "serve-data.js").replace("\\", "/")
        node_script = f"""
const {{ handleRequest }} = require('{serve_js}');
const {{ Writable }} = require('stream');

function runReq(url, headers = {{}}) {{
  return new Promise((resolve, reject) => {{
    const resHeaders = {{}};
    let statusCode = 200;
    const chunks = [];
    const res = new Writable({{
      write(chunk, enc, cb) {{
        chunks.push(Buffer.from(chunk));
        cb();
      }}
    }});
    res.setHeader = (k, v) => {{ resHeaders[k.toLowerCase()] = v; }};
    res.writeHead = (code, h = {{}}) => {{
      statusCode = code;
      for (const [k, v] of Object.entries(h)) resHeaders[k.toLowerCase()] = v;
    }};
    const origEnd = res.end.bind(res);
    res.end = (data) => {{
      if (data) chunks.push(Buffer.from(data));
      origEnd();
      const body = Buffer.concat(chunks);
      resolve({{ statusCode, headers: resHeaders, len: body.length, firstByte: body[0] || 0, magic: body.subarray(0, 8).toString('binary') }});
    }};
    res.on('finish', () => {{
      const body = Buffer.concat(chunks);
      resolve({{ statusCode, headers: resHeaders, len: body.length, firstByte: body[0] || 0, magic: body.subarray(0, 8).toString('binary') }});
    }});
    handleRequest({{ method: 'GET', url, headers }}, res);
  }});
}}

(async () => {{
  const r1 = await runReq('/BLR/tiles/14/{cbd_tx}/{cbd_ty}.mvt');
  const r2 = await runReq('/BLR/foundations/14/{cbd_tx}/{cbd_ty}.mvt');
  const r3 = await runReq('/data/BLR/tiles.pmtiles', {{ range: 'bytes=0-126' }});
  console.log(JSON.stringify({{ r1, r2, r3 }}));
}})();
"""
        proc = subprocess.run(
            ["node", "-e", node_script],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        out = json.loads(proc.stdout.strip())

        # 1. Verify /BLR/tiles/{z}/{x}/{y}.mvt
        self.assertEqual(out["r1"]["statusCode"], 200)
        self.assertEqual(out["r1"]["headers"]["access-control-allow-origin"], "*")
        self.assertEqual(out["r1"]["headers"]["x-pmtiles-hit"], "1")
        self.assertGreater(out["r1"]["len"], 1000)
        self.assertEqual(out["r1"]["firstByte"], 0x1A)

        # 2. Verify /BLR/foundations/{z}/{x}/{y}.mvt
        self.assertEqual(out["r2"]["statusCode"], 200)
        self.assertEqual(out["r2"]["headers"]["access-control-allow-origin"], "*")
        self.assertEqual(out["r2"]["headers"]["x-pmtiles-hit"], "1")
        self.assertGreater(out["r2"]["len"], 500)
        self.assertEqual(out["r2"]["firstByte"], 0x1A)

        # 3. Verify HTTP Range request against /data/BLR/tiles.pmtiles
        self.assertEqual(out["r3"]["statusCode"], 206)
        self.assertEqual(out["r3"]["headers"]["access-control-allow-origin"], "*")
        self.assertEqual(out["r3"]["len"], 127)
        self.assertTrue(out["r3"]["magic"].startswith("PMTiles\x03"))


if __name__ == "__main__":
    unittest.main()
