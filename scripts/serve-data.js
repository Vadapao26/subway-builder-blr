#!/usr/bin/env node
/**
 * Local static & PMTiles/MVT tile server with CORS and HTTP Range support for Subway Builder custom cities.
 *
 * Supports:
 *   1. Direct static & HTTP Range requests (e.g. /data/BLR/tiles.pmtiles, /data/BLR/demand_data.json.gz)
 *   2. Dynamic MVT tile extraction from PMTiles v3 archives for api.map.setTileURLOverride:
 *      - GET /BLR/tiles/{z}/{x}/{y}.mvt       (or /data/BLR/tiles/{z}/{x}/{y}.mvt)
 *      - GET /BLR/foundations/{z}/{x}/{y}.mvt (or /data/BLR/foundations/{z}/{x}/{y}.mvt)
 *
 * Usage: node scripts/serve-data.js [--port 8080] [--dir mod]
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const args = process.argv.slice(2);
let port = 8080;
let rootDir = path.resolve(__dirname, '..', 'mod');

for (let i = 0; i < args.length; i++) {
  if (args[i] === '--port' && args[i + 1]) port = parseInt(args[++i], 10);
  if (args[i] === '--dir' && args[i + 1]) rootDir = path.resolve(args[++i]);
}

const MIME_TYPES = {
  '.json': 'application/json',
  '.geojson': 'application/geo+json',
  '.bin': 'application/octet-stream',
  '.mvt': 'application/x-protobuf',
  '.pbf': 'application/x-protobuf',
  '.pmtiles': 'application/octet-stream',
  '.gz': 'application/octet-stream',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
};

// PMTiles v3 Hilbert curve cumulative tile offsets for z = 0..16
const TZ_VALUES = [
  0, 1, 5, 21, 85, 341, 1365, 5461, 21845, 87381, 349525,
  1398101, 5592405, 22369621, 89478485, 357913941, 1431655765,
];

function rotateHilbert(n, x, y, rx, ry) {
  if (ry === 0) {
    if (rx === 1) {
      x = n - 1 - x;
      y = n - 1 - y;
    }
    return [y, x];
  }
  return [x, y];
}

function zxyToTileId(z, x, y) {
  if (z < 0 || z >= TZ_VALUES.length) return -1;
  const acc = TZ_VALUES[z];
  const n = 1 << z;
  let tx = x;
  let ty = y;
  let d = 0;
  for (let s = n >> 1; s > 0; s >>= 1) {
    const rx = (tx & s) > 0 ? 1 : 0;
    const ry = (ty & s) > 0 ? 1 : 0;
    d += s * s * ((3 * rx) ^ ry);
    const rot = rotateHilbert(s, tx, ty, rx, ry);
    tx = rot[0];
    ty = rot[1];
  }
  return acc + d;
}

function readVarint(buf, pos) {
  let val = 0;
  let shift = 0;
  let p = pos;
  while (p < buf.length) {
    const b = buf[p++];
    val += (b & 0x7f) * Math.pow(2, shift);
    if ((b & 0x80) === 0) return [val, p];
    shift += 7;
  }
  return [val, p];
}

function parsePmtilesDirectory(dirBuf) {
  let pos = 0;
  const [numEntries, p1] = readVarint(dirBuf, pos);
  pos = p1;
  const tileIds = new Array(numEntries);
  const runLengths = new Array(numEntries);
  const lengths = new Array(numEntries);
  const offsets = new Array(numEntries);

  let lastId = 0;
  for (let i = 0; i < numEntries; i++) {
    const [d, np] = readVarint(dirBuf, pos);
    pos = np;
    lastId += d;
    tileIds[i] = lastId;
  }
  for (let i = 0; i < numEntries; i++) {
    const [rl, np] = readVarint(dirBuf, pos);
    pos = np;
    runLengths[i] = rl;
  }
  for (let i = 0; i < numEntries; i++) {
    const [ln, np] = readVarint(dirBuf, pos);
    pos = np;
    lengths[i] = ln;
  }
  for (let i = 0; i < numEntries; i++) {
    const [o, np] = readVarint(dirBuf, pos);
    pos = np;
    if (o === 0 && i > 0) {
      offsets[i] = offsets[i - 1] + lengths[i - 1];
    } else {
      offsets[i] = o - 1;
    }
  }
  return { numEntries, tileIds, runLengths, lengths, offsets };
}

// Cache of opened PMTiles indices keyed by file path
const pmtilesCache = new Map();

function getPmtilesIndex(pmPath) {
  const stat = fs.statSync(pmPath);
  const cached = pmtilesCache.get(pmPath);
  if (cached && cached.mtimeMs === stat.mtimeMs) {
    return cached;
  }

  const fd = fs.openSync(pmPath, 'r');
  try {
    const hdr = Buffer.alloc(127);
    fs.readSync(fd, hdr, 0, 127, 0);
    const magic = hdr.subarray(0, 7).toString('ascii');
    if (magic !== 'PMTiles') {
      throw new Error(`Invalid PMTiles magic in ${pmPath}`);
    }
    const rootOff = Number(hdr.readBigUInt64LE(8));
    const rootLen = Number(hdr.readBigUInt64LE(16));
    const leafOff = Number(hdr.readBigUInt64LE(40));
    const dataOff = Number(hdr.readBigUInt64LE(56));
    const tileComp = hdr.readUInt8(98);

    const rootGz = Buffer.alloc(rootLen);
    fs.readSync(fd, rootGz, 0, rootLen, rootOff);
    const rootDir = parsePmtilesDirectory(zlib.gunzipSync(rootGz));

    // Build flat Map<tileId, {offset, length}> (expanding any leaf directories once)
    const tileMap = new Map();
    if (rootDir.numEntries > 0 && rootDir.runLengths[0] === 0) {
      for (let i = 0; i < rootDir.numEntries; i++) {
        const lLen = rootDir.lengths[i];
        const lOff = rootDir.offsets[i];
        const leafGz = Buffer.alloc(lLen);
        fs.readSync(fd, leafGz, 0, lLen, leafOff + lOff);
        const leafDir = parsePmtilesDirectory(zlib.gunzipSync(leafGz));
        for (let j = 0; j < leafDir.numEntries; j++) {
          for (let r = 0; r < leafDir.runLengths[j]; r++) {
            tileMap.set(leafDir.tileIds[j] + r, {
              offset: dataOff + leafDir.offsets[j],
              length: leafDir.lengths[j],
            });
          }
        }
      }
    } else {
      for (let i = 0; i < rootDir.numEntries; i++) {
        for (let r = 0; r < rootDir.runLengths[i]; r++) {
          tileMap.set(rootDir.tileIds[i] + r, {
            offset: dataOff + rootDir.offsets[i],
            length: rootDir.lengths[i],
          });
        }
      }
    }

    const indexObj = { mtimeMs: stat.mtimeMs, tileComp, tileMap };
    pmtilesCache.set(pmPath, indexObj);
    return indexObj;
  } finally {
    fs.closeSync(fd);
  }
}

// Match URLs like /BLR/tiles/14/11725/7596.mvt or /data/BLR/foundations/14/11725/7596.mvt
const MVT_ROUTE_RE = /^\/(?:data\/)?([A-Za-z0-9_]+)\/(tiles|foundations)\/(\d+)\/(\d+)\/(\d+)\.(?:mvt|pbf)$/;

function handleRequest(req, res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, HEAD, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Range, Content-Type, Accept');
  res.setHeader('Access-Control-Expose-Headers', 'Content-Length, Content-Range, Accept-Ranges, X-PMTiles-Hit');

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  const urlPath = decodeURIComponent(req.url.split('?')[0]);

  // 1. Check if request is an unpacked MVT tile query against tiles.pmtiles or foundations.pmtiles
  const mvtMatch = urlPath.match(MVT_ROUTE_RE);
  if (mvtMatch) {
    const [, cityCode, kind, zStr, xStr, yStr] = mvtMatch;
    const z = parseInt(zStr, 10);
    const x = parseInt(xStr, 10);
    const y = parseInt(yStr, 10);

    // Locate <rootDir>/data/<cityCode>/<kind>.pmtiles or <rootDir>/<cityCode>/<kind>.pmtiles
    const candidates = [
      path.join(rootDir, 'data', cityCode, `${kind}.pmtiles`),
      path.join(rootDir, cityCode, `${kind}.pmtiles`),
      path.join(rootDir, `${kind}.pmtiles`),
    ];
    const pmPath = candidates.find((p) => fs.existsSync(p));
    if (pmPath) {
      try {
        const idx = getPmtilesIndex(pmPath);
        const tid = zxyToTileId(z, x, y);
        const entry = idx.tileMap.get(tid);
        if (!entry) {
          // Empty tile: return 200 with empty protobuf body so MapLibre logs no errors
          res.setHeader('Content-Type', 'application/x-protobuf');
          res.setHeader('X-PMTiles-Hit', '0');
          res.writeHead(200, { 'Content-Length': 0 });
          res.end();
          return;
        }
        const fd = fs.openSync(pmPath, 'r');
        const rawBuf = Buffer.alloc(entry.length);
        try {
          fs.readSync(fd, rawBuf, 0, entry.length, entry.offset);
        } finally {
          fs.closeSync(fd);
        }
        const payload = idx.tileComp === 2 ? zlib.gunzipSync(rawBuf) : rawBuf;
        res.setHeader('Content-Type', 'application/x-protobuf');
        res.setHeader('X-PMTiles-Hit', '1');
        res.writeHead(200, { 'Content-Length': payload.length });
        res.end(payload);
        return;
      } catch (err) {
        res.writeHead(500);
        res.end(`PMTiles error: ${err.message}`);
        return;
      }
    }
  }

  // 2. Fallback to static file serving (with HTTP Range support)
  const safePath = path.normalize(urlPath).replace(/^(\.\.(\/|\\|$))+/, '');
  let filePath = path.join(rootDir, safePath);
  if (!fs.existsSync(filePath) && !safePath.startsWith('data')) {
    const altPath = path.join(rootDir, 'data', safePath);
    if (fs.existsSync(altPath)) filePath = altPath;
  }

  if (!filePath.startsWith(rootDir)) {
    res.writeHead(403);
    res.end('Forbidden');
    return;
  }

  fs.stat(filePath, (err, stats) => {
    if (err || !stats.isFile()) {
      res.writeHead(404);
      res.end('Not Found');
      return;
    }

    const ext = path.extname(filePath).toLowerCase();
    const contentType = MIME_TYPES[ext] || 'application/octet-stream';
    res.setHeader('Accept-Ranges', 'bytes');
    res.setHeader('Content-Type', contentType);

    const range = req.headers.range;
    if (range) {
      const parts = range.replace(/bytes=/, '').split('-');
      const start = parseInt(parts[0], 10);
      const end = parts[1] ? parseInt(parts[1], 10) : stats.size - 1;
      if (start >= stats.size || end >= stats.size) {
        res.writeHead(416, { 'Content-Range': `bytes */${stats.size}` });
        res.end();
        return;
      }
      res.writeHead(206, {
        'Content-Range': `bytes ${start}-${end}/${stats.size}`,
        'Content-Length': end - start + 1,
      });
      fs.createReadStream(filePath, { start, end }).pipe(res);
    } else {
      res.writeHead(200, { 'Content-Length': stats.size });
      fs.createReadStream(filePath).pipe(res);
    }
  });
}

if (require.main === module) {
  const server = http.createServer(handleRequest);
  server.listen(port, '127.0.0.1', () => {
    console.log(`[serve-data] Serving ${rootDir} at http://127.0.0.1:${port} (CORS + Range + PMTiles MVT enabled)`);
  });
}

module.exports = { handleRequest, zxyToTileId, getPmtilesIndex };
