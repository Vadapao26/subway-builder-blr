# Subway Builder City Mod Pipeline: Bengaluru + Hosur (BLR)

A complete, production-grade automated pipeline that generates custom city mods for **Subway Builder** (`v1.7.2`, `metro-maker4`, Modding API `1.0.0`). 

This project produces a full mod package for **Greater Bengaluru and Hosur (`BLR`)**, covering the urban agglomeration across Karnataka and Tamil Nadu from Hosur & SIPCOT industrial areas in the south to Kempegowda International Airport in the north.

---

## Mod Summary: Bengaluru + Hosur (`BLR`)

- **City Code**: `BLR`
- **Total Population / Residents**: `13,978,303`
- **Total Jobs**: `13,978,303` (normalized `1.0` ratio matching built-in cities `DET`, `PAR`, `TOK`)
- **Active Demand Points**: `3,375` (1,000m cells snapped to the giant connected road network)
- **Commuter Groups (`pops`)**: `82,970` calibrated commuter OD pairs
  - Mean Commute Distance: `12.46 km` (within CMP target `8.0 – 14.0 km`)
  - Mean Commute Duration: `27.7 min` (within CMP target `25.0 – 45.0 min`)
- **Buildings Indexed**: `831,116` OSM building footprints
- **Road Network**: `227,733` classified road segments (`highway`, `major`, `medium`, `minor`)
- **Airport Features**: `329` buffered polygons for runways, taxiways, and aprons across KIA, HAL, Yelahanka, and Jakkur
- **Vector Map Tiles**: `3,076` MVT tiles across zooms 5..15 (`tiles.pmtiles`) and `2,188` foundation depth tiles (`foundations.pmtiles`)
- **Building Spatial Index**: `SBIB` v1 binary spatial grid (`buildings_index.bin.gz`, `64.40 MB`) with tags index (`building_tags.json.gz`)

---

## Mod Installation & Activation

The mod files have already been packaged and deployed to your local Subway Builder directories:

1. **Mod Package Directory**:
   `%APPDATA%\metro-maker4\mods\blr-bengaluru-hosur\`
   - `manifest.json`: Registers mod metadata, thumbnail, and main script.
   - `index.js`: Invokes `window.SubwayBuilderAPI.registerCity(...)` with the city configuration.
   - `thumbnail.png` & `thumbnail.svg`: City selection menu graphics.

2. **City Data Directory**:
   `%APPDATA%\metro-maker4\cities\data\BLR\`
   - `demand_data.json.gz`: Gridded demand points & commuter gravity groups.
   - `roads.geojson.gz`: Complete classified road network.
   - `runways_taxiways.geojson.gz`: Buffered airport polygons.
   - `tiles.pmtiles`: MVT vector map tiles (water, parks, commercial, airports, labels, buildings).
   - `foundations.pmtiles`: Building foundation depths.
   - `buildings_index.bin.gz`: Fast binary collision and clearance detector.
   - `building_tags.json.gz`: Semantic building tags (names, shops, amenities).
   - `coastline.geojson.gz` & `ocean_depth_index.json.gz`: Valid empty inland placeholders.

### Launching the Game
Simply open Subway Builder from Steam:
```powershell
& "C:\Program Files (x86)\Steam\steamapps\common\Subway Builder\game.exe"
```
Navigate to **Mods**, ensure **Bengaluru + Hosur (BLR)** is enabled, and start a new game selecting **Bengaluru**.

### Optional: Serving Tiles via Local Server
If using `api.map.setTileURLOverride` during development:
```powershell
node scripts/serve-data.js --port 8080
```

---

## Pipeline Execution & Verification

The pipeline is organized into modular phases (`pipeline/00_setup_check.py` through `pipeline/99_package_mod.py`):

| Phase | Script | Output Artifacts | Status |
|---|---|---|---|
| **Phase 0** | `pipeline/00_setup_check.py` | Environment & schema verification log | **PASS** |
| **Phase 1** | `pipeline/10_download.py` | Raw WorldPop GeoTIFF & clipped OSM PBF | **PASS** |
| **Phase 2** | `pipeline/20_extract_osm.py` | 8 Parquet feature tables (`831k` buildings) | **PASS** |
| **Phase 3** | `pipeline/30_population_grid.py` | `points_residents.parquet` (13.98M pop) | **PASS** |
| **Phase 4** | `pipeline/40_employment_weights.py` | `demand_points.parquet` (13.98M jobs) | **PASS** |
| **Phase 5** | `pipeline/60_routing.py` | Directed road graph & `routing_matrix.npz` | **PASS** |
| **Phase 6** | `pipeline/70_gravity_pops.py` | `demand_data.json.gz` (`82,970` pops) | **PASS** |
| **Phase 7** | `pipeline/80_roads_runways.py` | `roads.geojson.gz` & `runways_taxiways.geojson.gz` | **PASS** |
| **Phase 8** | `pipeline/90_tiles.py` | `tiles.pmtiles` & `foundations.pmtiles` | **PASS** |
| **Phase 9** | `pipeline/95_buildings.py` | `buildings_index.bin.gz` (SBIB v1) & `building_tags.json.gz` | **PASS** |
| **Phase 10** | `pipeline/99_package_mod.py` | Mod packaging & `%APPDATA%` deployment | **PASS** |

### Running the Test Suite
All 29 automated tests across all 11 phases pass:
```powershell
python -m unittest discover -s tests -v
```
