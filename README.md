# Subway Builder City Mod Pipeline

A config-driven, reproducible data pipeline that generates playable custom city mods for **Subway Builder** (tested against Game Version `1.7.2` / Modding API `1.0.0`).

The initial target city is **Bengaluru + Hosur (`BLR`)**, spanning from the Hosur industrial area in Tamil Nadu (south-east) through the Bengaluru urban core (Electronic City, Whitefield, CBD, Peenya) up to Kempegowda International Airport (north). The pipeline architecture is portable to any other Indian metro by adding a city YAML configuration under `cities/`.

## Repository Layout

```text
sb-city-pipeline/
  cities/
    blr.yaml                # City configuration for Bengaluru + Hosur
  pipeline/
    00_setup_check.py       # Phase 0: Environment & dependency verification
    10_download.py          # Phase 1: Raw data acquisition & manifest hashes
    20_extract_osm.py       # Phase 2: OSM extraction & coverage report
    30_population_grid.py   # Phase 3: Residents per demand point from WorldPop
    40_employment_weights.py# Phase 4: Synthetic jobs per demand point
    50_demand_points.py     # Phase 4: Merge residents & jobs into demand points
    60_routing.py           # Phase 5: OSRM driving travel time & distance matrix
    70_gravity_pops.py      # Phase 6: Doubly-constrained gravity model commuter pops
    80_roads_runways.py     # Phase 7: Roads & airport runways/taxiways GeoJSON
    90_tiles.py             # Phase 8: Vector map tiles (MVT / PMTiles)
    95_buildings.py         # Phase 9: Binary buildings index (SBIB v1) & building tags
    99_package_mod.py       # Phase 10: Mod packaging & installation into metro-maker4/mods
  mod/                      # Generated mod output (do not hand edit)
  tests/                    # Automated unit and smoke tests
  docs/
    data-sources.md         # Data sources, URLs, hashes, and licences
    assumptions.md          # Documented modelling assumptions and parameters
    verification-log.md     # Phase 0 verification log against game files & docs
  scripts/
    serve-data.js           # Local HTTP server with CORS & Range support for tiles/data
  README.md
```

## Quick Start (Phase 0)

Run the environment and setup check:

```powershell
python pipeline/00_setup_check.py --city cities/blr.yaml
```

## Attribution & Licences

See [`docs/data-sources.md`](docs/data-sources.md) for full licence and attribution details:
- **Map & Feature Data**: © OpenStreetMap contributors, available under the Open Database Licence (ODbL).
- **Population Raster Data**: WorldPop (www.worldpop.org), licensed under Creative Commons Attribution 4.0 International (CC-BY-4.0).
