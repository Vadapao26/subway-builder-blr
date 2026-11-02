# Bengaluru + Hosur (BLR) — Subway Builder Community Submission Package

This document contains everything needed to publish **Bengaluru + Hosur (`BLR`)** to the official **Subway Builder Modded** ecosystem ([subwaybuildermodded.com](https://subwaybuildermodded.com/)) and its GitHub Registry ([Subway-Builder-Modded/registry](https://github.com/Subway-Builder-Modded/registry)).

---

## 1. Release Bundle Details

- **Local Package Path**: `dist/SubwayBuilder-BLR-v1.0.0.zip` (205.25 MB)
- **Mod Directory Structure inside ZIP**:
  ```text
  mods/blr-bengaluru-hosur/
  ├── manifest.json
  ├── index.js
  ├── thumbnail.png
  └── thumbnail.svg
  cities/data/BLR/
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

When creating the submission issue on the [Subway-Builder-Modded Registry](https://github.com/Subway-Builder-Modded/registry/issues/new?template=publish-map.yml), paste these exact fields:

| Registry Field | Value to Enter |
|---|---|
| **Map ID** | `blr-bengaluru-hosur` |
| **City Name** | `Bengaluru` |
| **City Code** | `BLR` |
| **Country Code** | `IN` |
| **Description** | `High-density South Asian megacity transit mod spanning Bengaluru urban core to Hosur & SIPCOT industrial corridors (TN) and Kempegowda International Airport (BLR). Features 13.98M population, 831k buildings, complete road & runway vectors, and 83k calibrated commuter OD pairs.` |
| **Initial Latitude** | `12.97` |
| **Initial Longitude** | `77.59` |
| **Initial Zoom** | `11.5` |
| **Total Population** | `13978303` |
| **Demand Points Count** | `3375` |
| **Total Buildings** | `831116` |
| **Data Sources** | `OpenStreetMap (Geofabrik Southern Zone, ODbL-1.0), WorldPop 2020 1km UNadj Population Raster (CC-BY-4.0), BBMP Ward Boundaries (CC-BY-4.0)` |
| **Download Link** | `https://github.com/Vadapao26/subway-builder-blr/releases/download/v1.0.0/SubwayBuilder-BLR-v1.0.0.zip` |
| **Gallery Images (Logo & Screenshots)** | Drag and drop `docs/gallery/screenshot1_logo.png` directly into the issue text box along with in-game screenshots |

---

## 3. How the Metro Logo Appears on the Download Page

On **subwaybuildermodded.com** and in the **Railyard app gallery**, each city's mod download page displays a gallery carousel (`gallery/screenshot1.webp`, `screenshot2.webp`, etc.):

- **Screenshot 1 (Primary Banner)**: The official **Namma Metro** logo (`docs/gallery/screenshot1_logo.png`).
- **Screenshot 2**: The in-game Residents demand distribution map.
- **Screenshot 3**: The in-game Workers demand distribution map.

When submitting the issue on the Subway-Builder-Modded Registry, attaching `docs/gallery/screenshot1_logo.png` as the first image will make the registry maintainers set it as `gallery/screenshot1.webp` on your map's public download page (exactly how Kolkata, Delhi, and Mumbai did it).

---

## 3. How the Submission Automation Works

1. **Step 1**: Upload `dist/SubwayBuilder-BLR-v1.0.0.zip` as a GitHub Release on your GitHub repository (or upload to Google Drive / OneDrive).
2. **Step 2**: Open the [Subway-Builder-Modded Registry Issue Tracker](https://github.com/Subway-Builder-Modded/registry/issues/new?template=publish-map.yml).
3. **Step 3**: Paste the values from Section 2 above and link your release ZIP URL.
4. **Step 4**: The automated GitHub Action validates the manifest and opens a Pull Request automatically. Once approved by maintainers, the city will appear in **Railyard** and [subwaybuildermodded.com](https://subwaybuildermodded.com/) for one-click installation by all players worldwide.
