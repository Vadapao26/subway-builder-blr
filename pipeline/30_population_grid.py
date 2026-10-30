#!/usr/bin/env python3
"""Phase 3: Aggregate WorldPop population raster into demand points and cross-check total population."""
import argparse

def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 3: Residents per demand point.")
    parser.add_argument("--city", default="cities/blr.yaml", help="Path to city YAML config")
    parser.parse_args()
    print("Phase 3 stub: ready for implementation.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
