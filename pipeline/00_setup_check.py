#!/usr/bin/env python3
"""
Phase 0: Environment check and verification script.
Verifies:
- Python version (>= 3.10) and key Python libraries
- WSL2 availability and status
- Docker availability and whether it can run containers
- Free disk space on workspace drive
- Subway Builder game installation and metro-maker4 app data / mods directory
- City configuration file validity (cities/blr.yaml)
Writes a JSON report to reports/00_setup_check.json.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


def run_cmd(args: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out.strip()
    except Exception as exc:
        return -1, str(exc)


def check_python() -> dict:
    ver = sys.version_info
    packages = {}
    for pkg in [
        "yaml",
        "requests",
        "numpy",
        "pandas",
        "pyarrow",
        "shapely",
        "pyproj",
        "rasterio",
        "osmium",
        "scipy",
        "matplotlib",
        "pytest",
    ]:
        try:
            __import__(pkg)
            packages[pkg] = True
        except ImportError:
            packages[pkg] = False

    return {
        "executable": sys.executable,
        "version": f"{ver.major}.{ver.minor}.{ver.micro}",
        "ok": (ver.major, ver.minor) >= (3, 10),
        "packages": packages,
    }


def check_wsl2() -> dict:
    wsl_path = shutil.which("wsl")
    if not wsl_path:
        return {"available": False, "path": None, "detail": "wsl.exe not found in PATH"}
    code, out = run_cmd([wsl_path, "--status"])
    # Clean UTF-16 null bytes if wsl outputs UTF-16LE
    clean_out = out.replace("\x00", "").strip()
    return {
        "available": code == 0,
        "path": wsl_path,
        "return_code": code,
        "detail": clean_out[:300],
    }


def check_docker() -> dict:
    docker_path = shutil.which("docker")
    if not docker_path:
        return {
            "available": False,
            "can_run_containers": False,
            "path": None,
            "detail": "docker CLI not found in PATH",
        }
    code, out = run_cmd([docker_path, "--version"])
    info_code, info_out = run_cmd([docker_path, "info"], timeout=8)
    return {
        "available": code == 0,
        "can_run_containers": info_code == 0,
        "path": docker_path,
        "version": out[:200],
        "daemon_running": info_code == 0,
    }


def check_disk_space(target_dir: Path) -> dict:
    usage = shutil.disk_usage(target_dir)
    free_gb = round(usage.free / (1024**3), 2)
    total_gb = round(usage.total / (1024**3), 2)
    return {
        "path": str(target_dir.resolve()),
        "total_gb": total_gb,
        "free_gb": free_gb,
        "ok": free_gb >= 10.0,  # Recommend at least 10 GB free for OSM + WorldPop + tiles
    }


def check_game_paths() -> dict:
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    metro_dir = appdata / "metro-maker4"
    mods_dir = metro_dir / "mods"
    cities_data_dir = metro_dir / "cities" / "data"
    saves_dir = appdata / "SubwayBuilder" / "saves"

    steam_game_dir = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Subway Builder")
    app_asar = steam_game_dir / "resources" / "app.asar"

    builtin_downloaded = []
    if cities_data_dir.is_dir():
        builtin_downloaded = sorted(
            [d.name for d in cities_data_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
        )

    return {
        "game_install_dir": str(steam_game_dir),
        "game_install_exists": steam_game_dir.is_dir(),
        "app_asar_exists": app_asar.is_file(),
        "appdata_metro_dir": str(metro_dir),
        "appdata_metro_exists": metro_dir.is_dir(),
        "mods_dir": str(mods_dir),
        "mods_dir_exists": mods_dir.is_dir(),
        "cities_data_dir": str(cities_data_dir),
        "downloaded_builtin_cities": builtin_downloaded,
        "saves_dir": str(saves_dir),
        "saves_dir_exists": saves_dir.is_dir(),
    }


def check_cli_tools() -> dict:
    tools = {}
    for cmd in ["node", "npm", "git", "osmium", "tippecanoe", "pmtiles"]:
        tools[cmd] = shutil.which(cmd)
    return tools


def validate_city_config(city_yaml: Path) -> dict:
    if not city_yaml.is_file():
        return {"ok": False, "error": f"Config file not found: {city_yaml}"}
    with open(city_yaml, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    required_keys = ["name", "code", "description", "population", "bbox", "initialViewState", "osm_extract", "params"]
    missing = [k for k in required_keys if k not in cfg]
    bbox = cfg.get("bbox", {})
    bbox_ok = all(k in bbox for k in ("south", "north", "west", "east")) and (
        bbox.get("south", 0) < bbox.get("north", 0) and bbox.get("west", 0) < bbox.get("east", 0)
    )
    return {
        "ok": len(missing) == 0 and bbox_ok,
        "path": str(city_yaml),
        "city_code": cfg.get("code"),
        "city_name": cfg.get("name"),
        "bbox": bbox,
        "missing_keys": missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 0: Environment & setup verification for Subway Builder City Mod Pipeline.")
    parser.add_argument(
        "--city",
        type=Path,
        default=Path("cities/blr.yaml"),
        help="Path to the city YAML configuration file (default: cities/blr.yaml)",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    city_path = args.city if args.city.is_absolute() else (repo_root / args.city)

    report = {
        "phase": "00_setup_check",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "python": check_python(),
        "wsl2": check_wsl2(),
        "docker": check_docker(),
        "disk": check_disk_space(repo_root),
        "game": check_game_paths(),
        "cli_tools": check_cli_tools(),
        "city_config": validate_city_config(city_path),
        "verification_log_exists": (repo_root / "docs" / "verification-log.md").is_file(),
    }

    reports_dir = repo_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_file = reports_dir / "00_setup_check.json"
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print("=== Phase 0: Environment & Setup Check ===")
    print(f"Python:      {report['python']['version']} (ok={report['python']['ok']})")
    print(f"Disk Free:   {report['disk']['free_gb']} GB / {report['disk']['total_gb']} GB (ok={report['disk']['ok']})")
    print(f"Game Found:  {report['game']['game_install_exists']} ({report['game']['game_install_dir']})")
    print(f"Mods Folder: {report['game']['mods_dir_exists']} ({report['game']['mods_dir']})")
    print(f"Built-in Cities Downloaded: {report['game']['downloaded_builtin_cities']}")
    print(f"WSL2 Active: {report['wsl2']['available']} ({report['wsl2']['detail']})")
    print(f"Docker Ready:{report['docker']['can_run_containers']} ({report['docker']['detail'] if not report['docker']['available'] else report['docker']['version']})")
    print(f"City Config: {report['city_config']['city_name']} ({report['city_config']['city_code']}) ok={report['city_config']['ok']}")
    print(f"Verification Log Exists: {report['verification_log_exists']}")
    print(f"Saved report to: {report_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
