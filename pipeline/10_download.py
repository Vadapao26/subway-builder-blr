#!/usr/bin/env python3
"""Phase 1: Raw data acquisition (OSM regional extract + WorldPop raster) with SHA-256 manifest."""
import argparse

def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1: Download raw OSM and WorldPop datasets.")
    parser.add_argument("--city", default="cities/blr.yaml", help="Path to city YAML config")
    parser.parse_args()
    print("Phase 1 stub: ready for implementation after Phase 0 acceptance.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
