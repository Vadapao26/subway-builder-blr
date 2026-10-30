#!/usr/bin/env node
/**
 * Local static & MVT tile server with CORS and HTTP Range support for Subway Builder custom cities.
 * Usage: node scripts/serve-data.js [--port 8080] [--dir mod]
 */

const http = require('http');
const fs = require('fs');
const path = require('path');

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
  '.mvt': 'application/vnd.mapbox-vector-tile',
  '.pbf': 'application/x-protobuf',
  '.pmtiles': 'application/octet-stream',
  '.gz': 'application/octet-stream',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
};

const server = http.createServer((req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, HEAD, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Range, Content-Type, Accept');
  res.setHeader('Access-Control-Expose-Headers', 'Content-Length, Content-Range, Accept-Ranges');

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  const urlPath = decodeURIComponent(req.url.split('?')[0]);
  const safePath = path.normalize(urlPath).replace(/^(\.\.(\/|\\|$))+/, '');
  const filePath = path.join(rootDir, safePath);

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
});

server.listen(port, '127.0.0.1', () => {
  console.log(`[serve-data] Serving ${rootDir} at http://127.0.0.1:${port} (CORS + Range enabled)`);
});
