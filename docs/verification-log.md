# Phase 0 Verification Log

**Date Verified**: 2026-10-30  
**Target Game Version**: Subway Builder `1.7.2` (`metro-maker4`, Modding API `1.0.0`)  
**Target OS**: Windows (`C:\Program Files (x86)\Steam\steamapps\common\Subway Builder`)

This document records the verification results for every `[VERIFY]` item in the SRD against the official Subway Builder documentation, community repositories, and the actual installed game files (`resources/app.asar` and `%APPDATA%\metro-maker4`).

---

## Summary Table of `[VERIFY]` Items

| # | SRD Section | Item to Verify | Status | Resolution / Finding |
|---|-------------|----------------|--------|----------------------|
| 1 | Sec 2.4 | Community tool `bladistay/Subway-Builder-map-patcher` — capabilities & whether worth reusing | **VERIFIED** | Community Node.js patcher (also forked as `stepanskur/Subway-Builder-map-patcher` and used in `Valdotorium/Subwaybuilder-Maps`). Used for older map patching & local `pmtiles` serving; however, our Python + standalone SBIB binary encoder pipeline is cleaner, reproducible, and directly targets the v1.7.2 binary spec without patching `app.asar`. |
| 2 | Sec 2.5 | Built-in city data and tiles location & accessibility in installed game files | **VERIFIED** | Installed game is at `C:\Program Files (x86)\Steam\steamapps\common\Subway Builder`. Downloaded built-in cities (`DET`, `PAR`, `TOK`) live in `C:\Users\siddh\AppData\Roaming\metro-maker4\cities\data\<CODE>\` and are standard `.gz` and `.pmtiles` files, completely readable. |
| 3 | Sec 2 (Known inconsistency) | Buildings index format: binary (`buildings-index.bin` / `buildings_index.bin.gz`) vs JSON (`buildings_index.json.gz`) | **VERIFIED** | **Must be binary (`SBIB` v1 format).** Verified in `index-CuszHwSj.js`: `loadBuildingIndex` calls `loadBinaryData` -> `createBinaryDetector` -> `readHeader`, which strictly requires magic `0x49424253` (`1229079123`, ASCII `"SBBI"` / little-endian) and version `1`. JSON is rejected. |
| 4 | Sec 3 | Licences for every dataset used | **VERIFIED** | Recorded in `docs/data-sources.md`: OpenStreetMap (Geofabrik Southern Zone) is **ODbL 1.0**; WorldPop 2020 100m India Population Raster is **CC-BY-4.0**. |
| 5 | Sec 4.2 / R8 | City `code` (`BLR`) rules and conflicts with built-in cities | **VERIFIED** | Checked all 54 built-in city codes in `dist/renderer/cityPopulations.json` (`NYC`, `LON`, `DUB`, `PAR`, `TOK`, `DET`, etc.). `BLR` is **not** used by any built-in city. Furthermore, `registerCity` scopes modded cities with `getCityUid` (`<modId>:<cityCode>`) so conflicts with other mods are prevented. |
| 6 | Sec 4.2 | `initialViewState.zoom` value (`11.5` vs built-in cities) | **VERIFIED** | Built-in cities use `zoom: 13.5` for downtown focus (and `minZoom: 8.5–9.5`), while `NYC_EXP` (regional metro) uses `zoom: 10`. `11.5` (or `12.5`) is valid and supported. |
| 7 | Sec 4.2 | `osm_extract` regional Geofabrik extract covering Karnataka and Tamil Nadu | **VERIFIED** | Geofabrik's **Southern Zone** (`https://download.geofabrik.de/asia/india/southern-zone-latest.osm.pbf`, ~532 MB) covers both Karnataka and Tamil Nadu in a single file. |
| 8 | Phase 0 | Exact path of the game install and `mods/` folder on Windows | **VERIFIED** | Game install: `C:\Program Files (x86)\Steam\steamapps\common\Subway Builder\game.exe`. App data dir: `C:\Users\siddh\AppData\Roaming\metro-maker4\`. Mods folder: `C:\Users\siddh\AppData\Roaming\metro-maker4\mods\` (already exists). Note: Saves are in `C:\Users\siddh\AppData\Roaming\SubwayBuilder\saves\`. |
| 9 | Phase 0 | Mod package structure (manifest, entry file, data file locations) | **VERIFIED** | Each mod lives in `%APPDATA%\metro-maker4\mods\<mod-folder>\` with `manifest.json` (`id`, `name`, `version`, `description`, `author`, `main: "index.js"`) and `index.js` (IIFE using `window.SubwayBuilderAPI`). City data files live either in `%APPDATA%\metro-maker4\cities\data\BLR\` (auto-discovered via `scanCityDataFiles` or `/data/BLR/...` on the internal data server) or via absolute paths passed to `api.cities.setCityDataFiles`. |
| 10 | Phase 1 | Gridded population dataset (`WorldPop` India rasters): availability, year, resolution, licence | **VERIFIED** | WorldPop 2020 UN-adjusted Constrained 100m (3 arc-second) GeoTIFF (`ind_ppp_2020_UNadj_constrained.tif`) and 1km aggregated GeoTIFF are publicly available on `data.worldpop.org` under **CC-BY-4.0**. |
| 11 | Phase 1 | Census of India administrative boundaries / ward-level population table granularity and usability | **VERIFIED** | Published BBMP (Bruhat Bengaluru Mahanagara Palike) & BMRDA / Census 2011 + 2020–2026 urban agglomeration projections (~8.5M in 2011; ~13.6M across BMR + Hosur in 2020–2025) and OpenCity Bengaluru ward boundaries are usable for optional cross-checking. |
| 12 | Phase 3 | Built-in game city demand data readability, point count, and typical residents per point | **VERIFIED** | Built-in `demand_data.json.gz` files are directly readable: `DET` has 5,705 points (1,736,400 residents, ~304 res/pt); `PAR` has 6,722 points (5,903,400 residents, ~878 res/pt); `TOK` has 12,898 points (16,532,400 residents, ~1,282 res/pt). Remarkably, all three built-in cities set `total_jobs == total_residents == sum(pops.size)` (employment ratio = `1.0`). |
| 13 | Phase 6 | Gravity model in built-in cities & whether `drivingPath` is required in `DemandDataSchema` | **VERIFIED** | In `index-CuszHwSj.js`, built-in cities without empirical census OD (`DUB`, `TOK`) set `nonEmpiricalCommutes: true` and use a gravity model. In `PopSchema`, `drivingPath` is explicitly `.optional()` (`z.array(z.tuple([z.number(), z.number()])).optional()`). Built-in `DET` pops do **not** include `drivingPath` (only `id`, `size`, `residenceId`, `jobId`, `drivingSeconds`, `drivingDistance`). |
| 14 | Phase 7 | Required properties for `roads.geojson` and `runwaysTaxiways` GeoJSON | **VERIFIED** | Extracted `RoadsGeojsonSchema` and `RunwaysTaxiwaysGeojsonSchema` from `index-CuszHwSj.js`:<br>• `roads`: `FeatureCollection` of `LineString` features with `properties: { roadClass: "highway" \| "major" \| "medium" \| "minor", structure: "normal" \| "tunnel" \| "bridge", name: string }`.<br>• `runwaysTaxiways`: `FeatureCollection` of `Polygon` features with `properties: Record<string, any> (optional)` (e.g. `osm_id`, `aeroway`, `area`, `roadType: "runway"`). If missing/empty, the game catches and defaults to `{ type: "FeatureCollection", features: [] }`. |
| 15 | Phase 8 | Required vector tile layers and attributes from built-in city `pmtiles` and `GameMain` | **VERIFIED** | Inspected `DET/tiles.pmtiles` (zooms 5–15) and `DET/foundations.pmtiles` (zooms 13–15) plus `buildMapStyle()` in `GameMain-zem-cOEv.js`:<br>• `tiles`: layers `water` (polygon), `parks` (`area`: Number), `commercial` (`area`: Number, `type`: `"commercial"` or `"college"`), `airports` (polygon), `buildings` (minzoom 12, `id`: Number [OSM ID], `height`: Number), `ocean_foundations` (`depth_min`: Number), `city_labels` / `suburb_labels` / `neighborhood_labels` (`name`, `place`, `label_type`).<br>• `foundations`: layer `foundations` (minzoom 13, `foundationDepth`: Number). |
| 16 | Phase 9 | How to run the binary buildings encoder (`api.utils.buildings.encode`) or standalone encoder | **VERIFIED** | Extracted the exact JavaScript implementation of `encodeBuildingIndexBinary`, `computeSectionOffsets`, `writeHeader`, and `readHeader` directly from `index-CuszHwSj.js`! Because we have the exact byte-for-byte specification (88-byte header, magic `1229079123`, version `1`, 8-byte aligned typed array sections), we can encode `.bin` / `.bin.gz` **directly in Python** in `95_buildings.py` and verify it both in Python and via `api.utils.buildings.validate(buffer)` in-game! |

---

## Detailed Technical Notes from Game Inspection

### 1. Exact Binary Buildings Index (`buildings_index.bin`) Specification
Extracted from `dist/renderer/public/index-CuszHwSj.js`:
- **Magic (`uint32` LE at offset 0)**: `1229079123` (`0x49424253`)
- **Version (`uint8` at offset 4)**: `1`
- **Flags (`uint8` at offset 5)**: Bitmask: `HEIGHTS_FLAG = 1`, `OSM_IDS_FLAG = 2`
- **Reserved (`uint16` LE at offset 6)**: `0`
- **Counts (`uint32` LE at offsets 8..36)**:
  - `8`: `buildingCount`
  - `12`: `cols`
  - `16`: `rows`
  - `20`: `totalRings`
  - `24`: `totalCoords`
  - `28`: `nonEmptyCells`
  - `32`: `totalCellRefs`
  - `36`: `totalOsmIds`
- **Grid & BBox (`float64` LE at offsets 40..80)**:
  - `40`: `cellSize` (`0.0009` degrees in all built-in cities; `cellSizeLon = cellSize / cos(midLat)`)
  - `48`: `maxFoundationDepth`
  - `56`: `minLon`
  - `64`: `minLat`
  - `72`: `maxLon`
  - `80`: `maxLat`
- **Data Sections (starting at offset `88`, with `align8(o) = (o + 7) & ~7` where noted)**:
  1. `bounds`: `Float64Array` of length `buildingCount * 4` (`[minX, minY, maxX, maxY]` per building)
  2. `foundationDepths`: `Float32Array` of length `buildingCount`
  3. `align8` -> `buildingRingOffsets`: `Uint32Array` of length `buildingCount + 1`
  4. `ringCoordOffsets`: `Uint32Array` of length `totalRings + 1`
  5. `align8` -> `coords`: `Float64Array` of length `totalCoords * 2` (`[lon, lat]` pairs)
  6. `cellRowStarts`: `Uint32Array` of length `rows + 1`
  7. `cellCols`: `Uint32Array` of length `nonEmptyCells` (cells sorted by `row` asc, then `col` asc)
  8. `cellBuildingOffsets`: `Uint32Array` of length `nonEmptyCells + 1`
  9. `cellBuildingIds`: `Uint32Array` of length `totalCellRefs`
  10. If `flags & 1` (`HEIGHTS_FLAG`): `align8` -> `heights`: `Float32Array` of length `buildingCount`
  11. If `flags & 2` (`OSM_IDS_FLAG`): `align8` -> `osmIdOffsets`: `Uint32Array` of length `buildingCount + 1`, followed by `align8` -> `osmIds`: `Float64Array` of length `totalOsmIds`

### 2. How the Game Loads City Data Files
- `loadBuildingIndex(cityCode)` calls `loadBinaryData(path)`. **Important**: `loadBinaryData` rejects OS absolute paths (`isAbsolutePath(path)` throws an error!) and always fetches from the game's internal local HTTP data server (`http://127.0.0.1:<dataServerPort><path>?useDownloaded=true`).
- When files are placed in `%APPDATA%\metro-maker4\cities\data\BLR\` (`buildings_index.bin.gz`, `demand_data.json.gz`, `roads.geojson.gz`, `runways_taxiways.geojson.gz`, `building_tags.json.gz`), the game's internal data server serves them at `/data/BLR/<filename>`, and `window.electron.scanCityDataFiles('BLR')` automatically discovers them!
- For map tiles (`tilesUrl` and `foundationTilesUrl`), `api.map.setTileURLOverride({ cityCode: 'BLR', tilesUrl: 'http://127.0.0.1:8080/BLR/tiles/{z}/{x}/{y}.mvt', foundationTilesUrl: 'http://127.0.0.1:8080/BLR/foundations/{z}/{x}/{y}.mvt', maxZoom: 15 })` bypasses the built-in `map://` check because `isModdedCity = !!tileOverride` is `true`.

### 3. Environment & Tooling Status on User Machine
- **OS**: Windows 10/11 (`C:\` drive has ~400+ GB total,checked via `00_setup_check.py`).
- **Python**: `3.11.9` installed at `C:\Users\siddh\AppData\Local\Programs\Python\Python311\python.exe`.
- **Node.js / npm**: `v24.19.0` / `11.17.0` installed (can run `scripts/serve-data.js` and any JS helpers).
- **Git**: `2.55.0.windows.3` installed.
- **WSL2 & Docker**: Currently **not active / not installed** (`DockerDesktop` only has an empty `tmp-delete` folder, and `wsl` returned `E_ACCESSDENIED` / not configured).
  - **Resolution Note**: We have documented both (a) how to enable WSL2 + Docker Desktop if the user wants containerized OSRM/Tilemaker, and (b) a **100% native Windows fallback** (using Python/Node/Java/portable binaries for OSM extraction, routing, vector tiles, and binary buildings encoding) so the pipeline can run on Windows even without WSL2 or Docker!
