<p align="center">
  <img src="docs/images/hyderabad_metro_logo.svg" alt="Hyderabad Metro Rail HMRL Logo" width="480" />
</p>

# Subway Builder City Mod: Greater Hyderabad Metropolitan Area (HYD)

[![Release](https://img.shields.io/github/v/release/Vadapao26/subway-builder-hyd?style=flat-square)](https://github.com/Vadapao26/subway-builder-hyd/releases/tag/v1.0.0)
[![Status](https://img.shields.io/badge/status-active-success?style=flat-square)](#)
[![Subway Builder](https://img.shields.io/badge/game-Subway%20Builder%20v1.7.2-blue?style=flat-square)](#)

A complete, production-grade automated pipeline that generates custom city mods for **Subway Builder** (`v1.7.2`, `metro-maker4`, Modding API `1.0.0`, Railyard mod manager). 

This project produces a full mod package for the **Greater Hyderabad Metropolitan Area (`HYD`)**, seamlessly covering the entire twin-city metropolis across Telangana from RGIA Shamshabad in the south to Patancheru and Medchal in the north:
- **IT & Tech Core (Cyberabad)**: HITEC City, Madhapur, Gachibowli Financial District, Kondapur, Nanakramguda, Knowledge City.
- **Historic Core & Commercial Spine**: Secunderabad Junction, Begumpet, Ameerpet, Punjagutta, Khairatabad, Nampally, Charminar, Old City, Koti.
- **Industrial & Radial Corridors**: Patancheru, BHEL, Sanathnagar, Balanagar, Medchal, Uppal, Cherlapally, LB Nagar.
- **Airport Corridor**: Rajiv Gandhi International Airport (RGIA Shamshabad) and the southern PVNR Expressway corridor.

---

## Mod Summary: Greater Hyderabad (`HYD`)

- **City Code**: `HYD`
- **Total Population / Residents**: `9,849,136` (WorldPop 2020 UN-adjusted)
- **Total Jobs**: `9,849,136` (normalized `1.0` ratio matching built-in cities `DET`, `PAR`, `TOK`)
- **Active Demand Points**: `2,732` continuous organic points anchored to authentic building footprints with ~380m collision relaxation (no rigid Cartesian grid)
- **Active Workplace Nodes**: `1,939` (71.0% active workplace ratio)
  - **70% Regional Economic Anchors**: HITEC City & Madhapur (745k jobs), Balanagar & Sanathnagar (774k jobs), Uppal & Cherlapally (785k jobs), Gachibowli Financial District (599k jobs), Central Commercial Spine Ameerpet/Begumpet (508k jobs), Historic Core Nampally/Charminar (445k jobs), Secunderabad Junction (403k jobs), LB Nagar (406k jobs), Medchal & Kompally (405k jobs), RGIA Shamshabad Airport (381k jobs), Patancheru & BHEL (316k jobs).
  - **30% Neighborhood Local-Serving Commerce**: Distributed proportionally to residential population and local building density across Kukatpally, Dilsukhnagar, Mehdipatnam, Jubilee/Banjara Hills, and Malkajgiri.
- **Demand Dot Scaling**: `0.20` (`"demandDotScaling": 0.20` in `config.json` and mod entry script to prevent gray blobbing and render discrete stars)
- **Commuter Groups (`pops`)**: `68,284` calibrated commuter OD pairs via doubly-constrained gravity model
  - Mean Commute Distance: `11.87 km` (within target `8.0 – 14.0 km`, median `9.49 km`)
  - Mean Commute Duration: `26.5 min` (within target `25.0 – 45.0 min`, median `23.2 min`)
- **Buildings Indexed**: `792,869` OSM building footprints (`118.98 MB` SBIB v1 binary spatial grid)
- **Road Network**: `192,303` classified road segments (`highway`, `major`, `medium`, `minor`)
- **Airport Features**: `433` buffered polygons for runways, taxiways, and aprons across RGIA Shamshabad, Begumpet Airport, Dundigal AFA, and Hakimpet
- **Vector Map Tiles**: `2,346` MVT tiles across zooms 5..15 (`tiles.pmtiles`, `35.61 MB`) and `1,867` foundation depth tiles (`foundations.pmtiles`, `19.52 MB`)
- **Building Spatial Index**: `SBIB` v1 binary spatial grid (`buildings_index.bin.gz`, `61.52 MB`) with tags index (`building_tags.json.gz`, 34,115 tagged structures)
- **Distributable Package**: `dist/SubwayBuilder-HYD-v1.0.0.zip` (`191.16 MB`)

---

## Mod Installation & Activation

The mod files have been packaged in `dist/` and `mod/`:

1. **Mod Package Directory**:
   `%APPDATA%\metro-maker4\mods\hyd-greater-hyderabad\`
   - `manifest.json`: Registers mod metadata, thumbnail, and main script.
   - `index.js`: Invokes `window.SubwayBuilderAPI.registerCity(...)` with the city configuration.
   - `thumbnail.png` & `thumbnail.svg`: City selection menu graphics featuring official HMRL transit lines.

2. **City Data Directory**:
   `%APPDATA%\metro-maker4\cities\data\HYD\`
   - `config.json`: City definition (Railyard & Go deserialization compliant).
   - `demand_data.json.gz`: Organic demand points & commuter gravity groups.
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
Navigate to **Mods**, ensure **Greater Hyderabad (HYD)** is enabled, and start a new game selecting **Hyderabad**.

---

## Pipeline Execution Summary

The pipeline is organized into modular phases (`pipeline/00_setup_check.py` through `pipeline/99_package_mod.py`):

| Phase | Script | Output Artifacts | Status |
|---|---|---|---|
| **Phase 0** | `pipeline/00_setup_check.py` | Environment & schema verification log | **PASS** |
| **Phase 1** | `pipeline/10_download.py` | Raw WorldPop GeoTIFF & clipped OSM PBF (`hyd_clipped.osm.pbf`, 389.9 MB) | **PASS** |
| **Phase 2** | `pipeline/20_extract_osm.py` | 8 Parquet feature tables (`792,869` buildings, `200k` roads) | **PASS** |
| **Phase 3** | `pipeline/30_population_grid.py` | `points_residents.parquet` (9.86M population) | **PASS** |
| **Phase 4** | `pipeline/40_employment_weights.py` | `employment_weights.parquet` (OSM base + 11 regional boosts) | **PASS** |
| **Phase 4.2**| `pipeline/50_demand_points.py` | `demand_points.parquet` (Organic coordinates & dual-tier jobs) | **PASS** |
| **Phase 5** | `pipeline/60_routing.py` | Directed road graph & `routing_matrix.npz` (11 calibrated OD corridors) | **PASS** |
| **Phase 6** | `pipeline/70_gravity_pops.py` | `demand_data.json.gz` (`68,284` pops, mean 11.87 km, 26.5 min) | **PASS** |
| **Phase 7** | `pipeline/80_roads_runways.py` | `roads.geojson.gz` & `runways_taxiways.geojson.gz` | **PASS** |
| **Phase 8** | `pipeline/90_tiles.py` | `tiles.pmtiles` & `foundations.pmtiles` (2,346 MVT tiles) | **PASS** |
| **Phase 9** | `pipeline/95_buildings.py` | `buildings_index.bin.gz` (SBIB v1) & `building_tags.json.gz` | **PASS** |
| **Phase 10**| `pipeline/99_package_mod.py` | Mod packaging & distributable release ZIP (`191.16 MB`) | **PASS** |
