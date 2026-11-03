# Mumbai Metropolitan Region (BOM) — Subway Builder Community Submission Package

This document contains everything needed to publish the **Mumbai Metropolitan Region (`BOM`)** custom city mod to the official **Subway Builder Modded** ecosystem ([subwaybuildermodded.com](https://subwaybuildermodded.com/)) and its GitHub Registry ([Subway-Builder-Modded/registry](https://github.com/Subway-Builder-Modded/registry)).

---

## 1. Release Bundle Details

- **Local Package Path**: `dist/SubwayBuilder-BOM-v1.0.0.zip` (184.81 MB)
- **Mod Directory Structure inside ZIP**:
  ```text
  mods/bom-mumbai-mmr/
  ├── manifest.json
  ├── index.js
  ├── thumbnail.png
  └── thumbnail.svg
  cities/data/BOM/
  ├── config.json
  ├── demand_data.json.gz
  ├── roads.geojson.gz
  ├── runways_taxiways.geojson.gz
  ├── tiles.pmtiles
  ├── foundations.pmtiles
  ├── buildings_index.bin.gz
  ├── building_tags.json.gz
  ├── coastline.geojson.gz
  └── ocean_depth_index.json.gz
  ```

---

## 2. Community Registry Form Data (`publish-map.yml`)

When creating the submission issue on the [Subway-Builder-Modded Registry](https://github.com/Subway-Builder-Modded/registry/issues/new?template=publish-map.yml), copy and paste these exact fields:

| Registry Field | Value to Enter |
|---|---|
| **Map ID** | `bom-mumbai-mmr` |
| **City Name** | `Mumbai` |
| **City Code** | `BOM` |
| **Country Code** | `IN` |
| **Description** | `Next-generation transit mod for the Mumbai Metropolitan Region (MMR). Spans South Mumbai, Western Suburbs, Thane, Kalyan, Titwala, Navi Mumbai, and the airport corridors. Features 24.5M population, 254k buildings, continuous organic building-centroid coordinates, dual-tier employment anchors (BKC, Nariman Point, Lower Parel, SEEPZ, Belapur, NMIAL, JNPT), calibrated commuter gravity model (82k OD pairs), and anti-blobbing dot scaling (demandDotScaling: 0.20).` |
| **Initial Latitude** | `19.0760` |
| **Initial Longitude** | `72.8777` |
| **Initial Zoom** | `11.5` |
| **Total Population** | `24483977` |
| **Demand Points Count** | `3304` |
| **Total Buildings** | `254240` |
| **Data Sources** | `OpenStreetMap (Geofabrik Western Zone, ODbL-1.0), WorldPop 2020 1km UNadj Population Raster (CC-BY-4.0)` |
| **Download Link** | `https://github.com/Vadapao26/subway-builder-bom/releases/download/v1.0.0/SubwayBuilder-BOM-v1.0.0.zip` |
| **Gallery Images (Logo & Screenshots)** | Drag and drop `docs/gallery/screenshot1_bom_logo.png` directly into the issue text box along with in-game screenshots |

---

## 3. High-Scoring Railyard Bot & Schema Compliance Highlights

1. **Go Float Deserialization Compatibility**:
   - `config.json` omits `minZoom`/`maxZoom` (or uses strict integers), preventing Railyard's Go backend parser from crashing.
   - Bounding box is formatted as standard `[72.75, 18.88, 73.28, 19.48]`.
2. **Anti-Blobbing Discrete Starfields**:
   - `"demandDotScaling": 0.20` is explicitly configured in `config.json` and `index.js`, preventing high-density peninsular corridors from turning into a gray blob.
3. **Continuous Organic Coordinates**:
   - Replaces the 1 km grid lattice with authentic building footprint centroids and pairwise collision relaxation (~380m separation), yielding organic street-network fluid distributions matching the Paris benchmark.
4. **Dual-Tier Employment Distribution (77.8% Active Job Nodes)**:
   - 70% Regional Economic Anchors (BKC, Nariman Point, Lower Parel, SEEPZ, Belapur, Thane, JNPT, CSMIA, NMIAL, Titwala).
   - 30% Neighborhood Local-Serving Commerce, eliminating 0-job voids across residential suburbs.
5. **Exact SBIB v1 Binary Collision Format**:
   - 88-byte header with little-endian magic `1229079123` (`0x49424253`), version `1`, 8-byte aligned typed arrays, 100% verified via Subway Builder's native Node validator.
