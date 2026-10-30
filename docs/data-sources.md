# Data Sources and Licences

Every dataset used by the Subway Builder City Mod Pipeline (`BLR` and future cities) is documented below with its source URL, licence, attribution requirement, and verification status.

---

## 1. OpenStreetMap (Geofabrik Regional Extract)

- **Dataset Name**: Geofabrik India — Southern Zone (`southern-zone-latest.osm.pbf`)
- **Coverage**: Andhra Pradesh, Karnataka (including Bengaluru), Kerala, Lakshadweep, Puducherry, Tamil Nadu (including Hosur), Telangana.
- **Source URL**: `https://download.geofabrik.de/asia/india/southern-zone-latest.osm.pbf`
- **Features Extracted**: Buildings (`building=*`, `building:levels`, `height`, `name`, `amenity`, `shop`, `office`), road network (`highway=*`, `bridge`, `tunnel`, `name`), land use (`landuse=*`, `leisure=*`, `natural=*`), airport infrastructure (`aeroway=runway`, `taxiway`, `apron`, `aerodrome`), water bodies, parks, railways, and named employment POIs.
- **Licence**: Open Database Licence (**ODbL 1.0**) — https://opendatacommons.org/licenses/odbl/1-0/
- **Attribution Requirement**: Must credit *"© OpenStreetMap contributors"* in the mod README and city description/documentation.
- **Download Date & SHA-256 Hash**: *Populated automatically in `data/raw/manifest.json` and updated here by `pipeline/10_download.py` in Phase 1.*

---

## 2. Gridded Population Raster (WorldPop India)

- **Dataset Name**: WorldPop Global Project Population Data — India 2020 UN-adjusted Constrained 100m (`ind_ppp_2020_UNadj_constrained.tif`)
  - *Fallback 1km dataset*: `ind_ppp_2020_1km_Aggregated_UNadj.tif`
- **Coverage**: All of India at 3 arc-second (~100m at the equator) resolution, WGS84 (`EPSG:4326`).
- **Year**: 2020 (scaled to match the configured target metropolitan population if specified, or used as raw UN-adjusted counts).
- **Source URL**: `https://data.worldpop.org/GIS/Population/Global_2000_2020_Constrained/2020/BSGM/IND/ind_ppp_2020_UNadj_constrained.tif`
- **Licence**: Creative Commons Attribution 4.0 International (**CC-BY-4.0**) — https://creativecommons.org/licenses/by/4.0/
- **Attribution Requirement**:
  > WorldPop (www.worldpop.org - School of Geography and Environmental Science, University of Southampton; Department of Geography and Geosciences, University of Louisville; Departement de Geographie, Universite de Namur) and Center for International Earth Science Information Network (CIESIN), Columbia University (2018). Global High Resolution Population Denominators Project - Funded by The Bill and Melinda Gates Foundation (OPP1134076).
- **Download Date & SHA-256 Hash**: *Populated automatically in `data/raw/manifest.json` by `pipeline/10_download.py` in Phase 1.*

---

## 3. Reference Population & Administrative Cross-Check Data

- **Bengaluru + Hosur Study Area Reference Population**: `13,600,000`
  - **Sources**:
    1. **Census of India 2011**: Bengaluru Urban District (`9,621,551`), Bengaluru Rural District part (`~450,000`), and Hosur Taluk / Municipal Corporation (`~325,000` urban/suburban).
    2. **UN World Urbanization Prospects / Macrotrends / BMRDA Revised Structure Plan (2020–2025)**: Bengaluru Urban Agglomeration is estimated at `12.3M` (2020) to `14.0M` (2024–2025). Combined with the Hosur industrial corridor inside the `12.70–13.25°N, 77.40–77.90°E` bounding box, `13.6M` represents the circa-2023/2025 study-area population.
    3. **Optional Administrative Ward Boundaries**: BBMP 198/243 Ward Boundaries via DataMeet / OpenCity Bengaluru (`https://github.com/datameet/Municipal_Spatial_Data`), licensed under **CC-BY-4.0 / ODbL**.

---

## 4. Verified Raw Data Manifest (`data/raw/manifest.json`)

| Key | File | URL | Download Date (UTC) | Size (MB) | SHA-256 | Licence |
|-----|------|-----|---------------------|-----------|---------|---------|
| `population_raster` | `ind_ppp_2020_1km_Aggregated_UNadj.tif` | https://data.worldpop.org/GIS/Population/Global_2000_2020_1km_UNadj/2020/IND/ind_ppp_2020_1km_Aggregated_UNadj.tif | `2026-10-30T10:14:15Z` | 17.46 | `27d08e3eabf59f63a2147901c25c046d584c3295f9ceb4c4cf08a902a5bec911` | CC-BY-4.0 |
| `admin_boundaries` | `blr_admin_wards.geojson` | https://raw.githubusercontent.com/datameet/Municipal_Spatial_Data/master/Bangalore/BBMP.geojson | `2026-10-30T10:14:16Z` | 2.01 | `4a1186d15da9396c259a2c01b135ee5e903948d45de6e91c49e0ddd72f359a7b` | CC-BY-4.0 |
| `osm_extract_raw` | `southern-zone-latest.osm.pbf` | https://download.geofabrik.de/asia/india/southern-zone-latest.osm.pbf | `2026-10-30T10:40:40Z` | 533.04 | `5b4a9451d14406b96a1ed5fc541dd6b3ea4bef6d5cf1911349a4809da6f642eb` | ODbL-1.0 |
| `osm_extract_clipped` | `blr_clipped.osm.pbf` | clipped from southern-zone-latest.osm.pbf | `2026-10-30T10:46:27Z` | 395.31 | `086a452eabf7a9f07072010683cd49e03471bbda3bdfea142caf4cd5b80b4a62` | ODbL-1.0 |
