#!/usr/bin/env python3
"""
Phase 1: Raw data acquisition.
Tasks:
1. Download the OpenStreetMap regional extract covering the bounding box and both states
   (Geofabrik Southern Zone), using parallel HTTP Range requests if supported, and clip
   to the study-area bounding box + routing margin (`bbox_margin_deg`).
2. Download the WorldPop India gridded population GeoTIFF raster.
3. Download optional administrative boundaries (BBMP wards GeoJSON from DataMeet) for cross-checking.
4. Record URL, download date, file size, SHA-256 hash, and licence for every file in:
   - `data/raw/manifest.json` (git-ignored raw folder)
   - `docs/data-sources.md`
   - `reports/10_download.json`
Re-running this script is strictly idempotent: any file present in `data/raw/` whose
SHA-256 hash (or expected size + non-empty check) matches `data/raw/manifest.json` is skipped.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import requests
import yaml

# Allow importing sibling modules in pipeline/
sys.path.insert(0, str(Path(__file__).resolve().parent))
from osm_pbf_utils import clip_osm_pbf_to_bbox


USER_AGENT = "sb-city-pipeline/0.1 (Subway Builder Custom City Mod Pipeline)"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(4 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def probe_url(url: str, timeout: int = 20) -> tuple[int, bool]:
    """Return (content_length, supports_range) for `url`."""
    headers = {"User-Agent": USER_AGENT, "Range": "bytes=0-1023", "Accept-Encoding": "identity"}
    try:
        r = requests.get(url, headers=headers, stream=True, timeout=timeout)
        status = r.status_code
        cr = r.headers.get("Content-Range", "")
        cl = r.headers.get("Content-Length", "0")
        r.close()
        if status == 206 and "/" in cr:
            total_size = int(cr.split("/")[-1])
            return total_size, True
        elif status == 200:
            return int(cl) if cl.isdigit() else 0, False
    except Exception:
        pass
    try:
        r = requests.head(url, headers={"User-Agent": USER_AGENT}, allow_redirects=True, timeout=timeout)
        cl = r.headers.get("Content-Length", "0")
        return (int(cl) if cl.isdigit() else 0), False
    except Exception:
        return 0, False


def download_single_stream(url: str, dest: Path, expected_size: int = 0) -> None:
    part_file = dest.with_suffix(dest.suffix + ".part")
    t0 = time.time()
    with requests.get(url, headers={"User-Agent": USER_AGENT}, stream=True, timeout=120) as r:
        r.raise_for_status()
        downloaded = 0
        last_log = t0
        with open(part_file, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                now = time.time()
                if now - last_log >= 5.0:
                    pct = f" ({downloaded * 100 / expected_size:.1f}%)" if expected_size else ""
                    rate = downloaded / max(0.1, now - t0) / 1e6
                    print(f"  [stream] {dest.name}: {downloaded / 1e6:.2f} MB{pct} @ {rate:.2f} MB/s", flush=True)
                    last_log = now
    if expected_size and part_file.stat().st_size != expected_size:
        raise IOError(f"Incomplete download for {dest.name}: got {part_file.stat().st_size}, expected {expected_size}")
    part_file.replace(dest)


def download_parallel_range(url: str, dest: Path, total_size: int, num_workers: int = 16, chunk_mb: int = 4) -> None:
    """Download a large file using parallel HTTP Range requests with automatic retries."""
    part_file = dest.with_suffix(dest.suffix + ".part")
    chunk_size = chunk_mb * 1024 * 1024
    ranges: list[tuple[int, int, int]] = []
    idx = 0
    for start in range(0, total_size, chunk_size):
        end = min(start + chunk_size - 1, total_size - 1)
        ranges.append((idx, start, end))
        idx += 1

    # Pre-allocate file on disk
    with open(part_file, "wb") as f:
        f.truncate(total_size)

    t0 = time.time()
    completed_bytes = 0

    def fetch_segment(seg: tuple[int, int, int]) -> int:
        _, start, end = seg
        expected_len = end - start + 1
        for attempt in range(5):
            try:
                headers = {"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end}"}
                r = requests.get(url, headers=headers, timeout=60)
                if r.status_code in (200, 206) and len(r.content) == expected_len:
                    with open(part_file, "r+b") as wf:
                        wf.seek(start)
                        wf.write(r.content)
                    return expected_len
            except Exception as exc:
                if attempt == 4:
                    raise RuntimeError(f"Range {start}-{end} failed after 5 attempts: {exc}") from exc
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Range {start}-{end} failed")

    print(f"  [parallel] Downloading {dest.name} ({total_size / 1e6:.2f} MB) across {len(ranges)} chunks with {num_workers} workers...", flush=True)
    last_log = t0
    with ThreadPoolExecutor(max_workers=num_workers) as ex:
        for seg_bytes in ex.map(fetch_segment, ranges):
            completed_bytes += seg_bytes
            now = time.time()
            if now - last_log >= 5.0 or completed_bytes == total_size:
                pct = completed_bytes * 100.0 / total_size
                rate = completed_bytes / max(0.1, now - t0) / 1e6
                print(f"  [parallel] {dest.name}: {completed_bytes / 1e6:.2f} / {total_size / 1e6:.2f} MB ({pct:.1f}%) @ {rate:.2f} MB/s", flush=True)
                last_log = now

    if part_file.stat().st_size != total_size:
        raise IOError(f"Size mismatch for {dest.name}: {part_file.stat().st_size} != {total_size}")
    part_file.replace(dest)


def ensure_downloaded(
    key: str,
    url: str,
    dest: Path,
    licence: str,
    manifest: dict,
    manifest_path: Path,
) -> dict:
    """Download `url` to `dest` if not already verified in `manifest`."""
    existing = manifest.get("files", {}).get(key)
    if existing and dest.is_file():
        actual_size = dest.stat().st_size
        if actual_size == existing.get("size_bytes") and actual_size > 0:
            actual_hash = sha256_file(dest)
            if actual_hash == existing.get("sha256"):
                print(f"[SKIP] {dest.name} already verified (SHA-256: {actual_hash[:16]}...)")
                return {**existing, "skipped": True}

    print(f"[FETCH] Probing {url} ...", flush=True)
    total_size, supports_range = probe_url(url)
    t0 = time.time()
    if supports_range and total_size >= 8 * 1024 * 1024:
        download_parallel_range(url, dest, total_size)
    else:
        download_single_stream(url, dest, total_size)

    elapsed = round(time.time() - t0, 2)
    file_hash = sha256_file(dest)
    size_bytes = dest.stat().st_size
    entry = {
        "key": key,
        "filename": dest.name,
        "path": str(dest.relative_to(dest.parent.parent.parent)),
        "url": url,
        "licence": licence,
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
        "size_bytes": size_bytes,
        "sha256": file_hash,
        "download_seconds": elapsed,
        "skipped": False,
    }
    manifest.setdefault("files", {})[key] = entry
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"[DONE] {dest.name} ({size_bytes / 1e6:.2f} MB, SHA-256: {file_hash[:16]}..., {elapsed}s)", flush=True)
    return entry


def update_data_sources_doc(doc_path: Path, manifest: dict) -> None:
    """Append or update the verified manifest table in `docs/data-sources.md`."""
    if not doc_path.is_file():
        return
    content = doc_path.read_text(encoding="utf-8")
    marker = "## 4. Verified Raw Data Manifest (`data/raw/manifest.json`)"
    if marker in content:
        content = content.split(marker)[0].rstrip() + "\n\n"
    else:
        content = content.rstrip() + "\n\n---\n\n"

    lines = [
        marker,
        "",
        "| Key | File | URL | Download Date (UTC) | Size (MB) | SHA-256 | Licence |",
        "|-----|------|-----|---------------------|-----------|---------|---------|",
    ]
    for k, info in manifest.get("files", {}).items():
        mb = f"{info['size_bytes'] / (1024 * 1024):.2f}"
        dt = info["downloaded_at_utc"][:19] + "Z"
        lines.append(
            f"| `{k}` | `{info['filename']}` | {info['url']} | `{dt}` | {mb} | `{info['sha256']}` | {info['licence']} |"
        )
    lines.append("")
    doc_path.write_text(content + "\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1: Raw data acquisition & clipping with SHA-256 manifest.")
    parser.add_argument("--city", type=Path, default=Path("cities/blr.yaml"), help="Path to city YAML config")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    city_path = args.city if args.city.is_absolute() else (repo_root / args.city)
    with open(city_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    city_code = cfg["code"].lower()
    raw_dir = repo_root / "data" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = raw_dir / "manifest.json"

    if manifest_path.is_file():
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    else:
        manifest = {"city_code": cfg["code"], "files": {}}

    # 1. Population raster (WorldPop India)
    pop_cfg = cfg["population_raster"]
    pop_dest = raw_dir / Path(pop_cfg["url"]).name
    pop_entry = ensure_downloaded(
        key="population_raster",
        url=pop_cfg["url"],
        dest=pop_dest,
        licence=pop_cfg.get("licence", "CC-BY-4.0"),
        manifest=manifest,
        manifest_path=manifest_path,
    )

    # 2. Optional Admin / Ward Boundaries (DataMeet BBMP Wards GeoJSON)
    admin_cfg = cfg.get("admin_boundaries")
    admin_entry = None
    if admin_cfg and admin_cfg.get("url"):
        admin_dest = raw_dir / f"{city_code}_admin_wards.geojson"
        admin_entry = ensure_downloaded(
            key="admin_boundaries",
            url=admin_cfg["url"],
            dest=admin_dest,
            licence=admin_cfg.get("licence", "CC-BY-4.0"),
            manifest=manifest,
            manifest_path=manifest_path,
        )

    # 3. OpenStreetMap regional PBF extract
    osm_cfg = cfg["osm_extract"]
    osm_dest = raw_dir / Path(osm_cfg["url"]).name
    osm_entry = ensure_downloaded(
        key="osm_extract_raw",
        url=osm_cfg["url"],
        dest=osm_dest,
        licence=osm_cfg.get("licence", "ODbL-1.0"),
        manifest=manifest,
        manifest_path=manifest_path,
    )

    # 4. Clip OSM PBF to bounding box + routing margin
    bbox = cfg["bbox"]
    margin = float(osm_cfg.get("bbox_margin_deg", 0.05))
    clip_south = round(bbox["south"] - margin, 6)
    clip_north = round(bbox["north"] + margin, 6)
    clip_west = round(bbox["west"] - margin, 6)
    clip_east = round(bbox["east"] + margin, 6)

    clipped_dest = raw_dir / f"{city_code}_clipped.osm.pbf"
    existing_clip = manifest.get("files", {}).get("osm_extract_clipped")
    clip_stats = {}
    if (
        existing_clip
        and clipped_dest.is_file()
        and clipped_dest.stat().st_size == existing_clip.get("size_bytes")
        and existing_clip.get("source_sha256") == osm_entry["sha256"]
        and existing_clip.get("clip_bbox") == [clip_south, clip_west, clip_north, clip_east]
    ):
        actual_clip_hash = sha256_file(clipped_dest)
        if actual_clip_hash == existing_clip.get("sha256"):
            print(f"[SKIP] {clipped_dest.name} already clipped and verified (SHA-256: {actual_clip_hash[:16]}...)")
            clip_entry = {**existing_clip, "skipped": True}
        else:
            existing_clip = None

    if not existing_clip or not clipped_dest.is_file():
        print(
            f"[CLIP] Clipping {osm_dest.name} to bbox [{clip_south}, {clip_west}, {clip_north}, {clip_east}] (margin={margin} deg)...",
            flush=True,
        )
        t_clip = time.time()
        clip_stats = clip_osm_pbf_to_bbox(
            src_pbf=osm_dest,
            dst_pbf=clipped_dest,
            south=clip_south,
            north=clip_north,
            west=clip_west,
            east=clip_east,
        )
        clip_elapsed = round(time.time() - t_clip, 2)
        clip_hash = sha256_file(clipped_dest)
        clip_entry = {
            "key": "osm_extract_clipped",
            "filename": clipped_dest.name,
            "path": str(clipped_dest.relative_to(repo_root)),
            "url": f"clipped from {osm_dest.name}",
            "licence": osm_cfg.get("licence", "ODbL-1.0"),
            "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
            "size_bytes": clipped_dest.stat().st_size,
            "sha256": clip_hash,
            "source_sha256": osm_entry["sha256"],
            "clip_bbox": [clip_south, clip_west, clip_north, clip_east],
            "clip_stats": clip_stats,
            "clip_seconds": clip_elapsed,
            "skipped": False,
        }
        manifest["files"]["osm_extract_clipped"] = clip_entry
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        print(
            f"[DONE] Clipped {clipped_dest.name} ({clip_entry['size_bytes'] / 1e6:.2f} MB, {clip_stats['kept_blobs']}/{clip_stats['total_source_blobs']} blobs kept in {clip_elapsed}s)",
            flush=True,
        )

    # Update docs/data-sources.md and write reports/10_download.json
    update_data_sources_doc(repo_root / "docs" / "data-sources.md", manifest)

    reports_dir = repo_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / "10_download.json"
    report = {
        "phase": "10_download",
        "city_code": cfg["code"],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "manifest_path": str(manifest_path.relative_to(repo_root)),
        "files_count": len(manifest.get("files", {})),
        "files": manifest.get("files", {}),
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"=== Phase 1 Complete: {len(manifest['files'])} files verified in {manifest_path} ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
