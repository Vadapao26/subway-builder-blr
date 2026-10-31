# Modelling Assumptions and Configurable Parameters

Per Section 0 and Section 6 of the SRD, every modelling choice marked `[ASSUMPTION]` is stored as a named parameter in `cities/blr.yaml` (never hard-coded in scripts) and justified here.

---

## 1. Study Area Bounding Box (`bbox`)
- **Parameter in `cities/blr.yaml`**:
  ```yaml
  bbox: {south: 12.70, north: 13.25, west: 77.40, east: 77.90}
  ```
- **Dimensions**: `0.55°` latitude (~61.0 km N–S) × `0.50°` longitude (~54.3 km E–W) ≈ **3,312 km²**.
- **Why chosen**: Covers the entire Bruhat Bengaluru Mahanagara Palike (BBMP) core, Kempegowda International Airport (`13.1986°N, 77.7066°E`) in the north, Peenya (`13.03°N, 77.51°E`) in the north-west, Whitefield/ITPL (`12.98°N, 77.73°E`) in the east, Electronic City (`12.84°N, 77.66°E`) in the south, and the Hosur urban & SIPCOT industrial area (`12.73°N, 77.82°E`) across the Tamil Nadu border in the south-east.
- **How to change**: Edit `bbox` in `cities/blr.yaml` and re-run from Phase 1.

---

## 2. Initial Camera View (`initialViewState`)
- **Parameter in `cities/blr.yaml`**:
  ```yaml
  initialViewState: {zoom: 11.5, latitude: 12.97, longitude: 77.59, bearing: 0}
  ```
- **Why chosen**: Centered near Majestic / MG Road / Cubbon Park (`12.97°N, 77.59°E`). `zoom: 11.5` gives a metropolitan overview of Bengaluru; note that built-in cities (`NYC`, `PAR`, `TOK`, `DET`) use `zoom: 13.5` for a closer downtown start.
- **How to change**: Edit `initialViewState` in `cities/blr.yaml` and re-run Phase 10 (`99_package_mod.py`).

---

## 3. Demand Point Grid Resolution (`params.grid_cell_m`, `params.min_residents_per_point`)
- **Parameters in `cities/blr.yaml`**:
  - `grid_cell_m: 1000` (1,000 meters = 1 km)
  - `min_residents_per_point: 50`
- **Why chosen**:
  - Inspected built-in cities in Phase 0:
    - Detroit (`DET`): `5,705` demand points (`1,736,400` residents)
    - Paris (`PAR`): `6,722` demand points (`5,903,400` residents)
    - Tokyo (`TOK`): `12,898` demand points (`16,532,400` residents)
  - Our study box is `~61 km × 54 km = 3,312 km²`. At `grid_cell_m: 1000`, the full grid has `3,410` cells (`62 rows × 55 cols`), and after dropping sparse rural cells with `< 50` residents and `0` buildings/employment landuse, yields `3,388` active demand points. This keeps routing table queries and in-game Raptor pathfinding fast while preserving 1-km neighborhood granularity.
- **How to change**: Change `params.grid_cell_m` or `params.min_residents_per_point` in `cities/blr.yaml` and re-run from Phase 3.

---

## 4. Employment Ratio (`params.employment_ratio`)
- **Parameter in `cities/blr.yaml`**: `employment_ratio: 1.0`
- **Why chosen**:
  - **Discovery from built-in game files**: In all three inspected built-in cities (`DET`, `PAR`, `TOK`), `sum(points.jobs) == sum(points.residents) == sum(pops.size)` (an exact `1.000` ratio!). The game treats `residents` at each point as the number of *active daily commuters* living there, and pairs every resident into a commuter group (`pop`) heading to a job (`jobId`).
  - **Real-world comparison**: In Indian Census / PLFS data for Bengaluru Urban, the Worker Population Ratio (WPR) is ~`0.42–0.45` of total population. If `residents` represents total census population (~13.6M) and we only want active commuters (~5.7M), either `employment_ratio: 0.42` can be used (scaling jobs to 42% of total population) OR `employment_ratio: 1.0` can be used following the game's convention. We expose this choice explicitly to the user.
- **How to change**: Edit `params.employment_ratio` in `cities/blr.yaml` and re-run from Phase 4.

---

## 5. Synthetic Employment Category Weights & Named Cluster Boosts (`params.employment_weights`, `params.named_employment_boosts`)
- **Parameters in `cities/blr.yaml`**:
  - Footprint density weights (relative jobs per m² of floor/footprint area before global normalization): `office: 0.08`, `commercial: 0.05`, `retail: 0.04`, `industrial: 0.03`, `institutional: 0.03`, `landuse_commercial: 0.015`, `landuse_industrial: 0.010`, `landuse_retail: 0.015`, `landuse_institutional: 0.010`, `landuse_airport: 0.008`.
  - Named POI weights (relative jobs per named OSM node/way POI before global normalization): `poi_office: 20.0`, `poi_commercial: 8.0`, `poi_retail: 5.0`, `poi_industrial: 15.0`, `poi_institutional: 8.0`, plus `osm_named_polygon_multiplier: 1.5` for named commercial/industrial/campus polygons in OSM.
  - Named cluster boosts for: **Kempegowda International Airport (BLR)**, **Electronic City**, **Whitefield / ITPL**, **Outer Ring Road (Bellandur–Marathahalli–Manyata)**, **Central Business District (MG Road / Majestic)**, **Peenya Industrial Area**, and **Hosur SIPCOT Industrial Phases I & II**.
- **Why chosen**: OpenStreetMap in India often maps large IT campuses, tech parks, and industrial estates as a single `landuse=industrial` or `landuse=commercial` polygon with incomplete `building=office` tags inside. Combining OSM building/landuse footprints, multi-cell polygon rasterization, named employment POIs, and explicit cluster weights ensures major employment hubs attract realistic commuter volumes.
- **How to change**: Adjust `params.employment_weights` or `params.named_employment_boosts` in `cities/blr.yaml` and re-run Phase 4.

---

## 6. Congestion Time-Scaling Factor & Road Routing Rules (`params.congestion_time_factor`, `params.routing`)
- **Parameters in `cities/blr.yaml`**:
  - `congestion_time_factor: 1.8`
  - `routing.max_snap_distance_m: 1500.0`
  - `routing.drop_unroutable_above_m: 2500.0`
  - `routing.access_circuity_factor: 1.3`
  - `routing.access_speed_kmh: 22.0`
  - `routing.default_speeds_kmh`: `{motorway: 80, trunk: 60, primary: 48, secondary: 40, tertiary: 32, unclassified: 26}`
- **Why chosen**:
  - Free-flow car speeds ignore urban traffic congestion. According to TomTom Traffic Index and CMP studies for Bengaluru, peak-hour travel times are **1.7× to 2.1×** free-flow durations (average peak speed ~18–22 km/h). Multiplying free-flow `drivingSeconds` by `1.8` prevents driving from being unrealistically fast compared to metro transit in the game's mode-choice model.
  - Explicit unroutable/snapping rule: Demand points are snapped to the Giant Strongly Connected Component (SCC) of the car road graph. Points within `<= 1,500m` snap normally; points between `1,500m` and `2,500m` are logged as fringe-snapped; points `> 2,500m` from the road graph (e.g., deep inside Bannerghatta National Park forest reserve) are logged and dropped, with total jobs re-normalized to maintain `employment_ratio * total_residents`.
- **How to change**: Edit `params.congestion_time_factor` or `params.routing` in `cities/blr.yaml` and re-run Phase 5/6.

---

## 7. Gravity Model Decay & Commute Targets (`params.gravity_beta`, `params.doubly_constrained_blend`, `params.top_k_destinations`)
- **Parameters in `cities/blr.yaml`**:
  - `gravity_beta: 0.140` (calibrated decay parameter in $\exp(-\beta \cdot t_{\text{min}})$)
  - `doubly_constrained_blend: 0.72` (convex blend of doubly-constrained Sinkhorn/Furness IPF and origin-constrained local gravity on the sparse top-$K$ graph)
  - `target_mean_commute_km: [8.0, 14.0]`
  - `target_mean_commute_min: [25.0, 45.0]`
  - `top_k_destinations: 25`
- **Why chosen**: BMRCL / DULT Comprehensive Mobility Plan (CMP 2020) for Bengaluru reports an average motorized trip length of **~9.5–11.5 km** (target range `8.0–14.0 km`) and average commute duration of **~25–45 minutes**. Keeping at most the top `K = 25` destinations per origin limits total `pops` to `~83,000` groups (comparable to Tokyo `82,662`), ensuring smooth simulation performance while preserving 100% point-level conservation (`sum(pops.size) == sum(residents) == sum(jobs)`).
- **How to change**: Edit `gravity_beta`, `doubly_constrained_blend`, `target_mean_commute_km`, or `top_k_destinations` in `cities/blr.yaml` and re-run Phase 6.

---

## 8. Building Foundation Depth & Spatial Grid (`params.default_foundation_depth_m`, `params.buildings_grid_cell_deg`)
- **Parameters in `cities/blr.yaml`**:
  - `default_foundation_depth_m: 7.0`
  - `meters_per_building_level: 3.5`
  - `foundation_depth_per_level_m: 2.5`
  - `buildings_grid_cell_deg: 0.0009`
- **Why chosen**:
  - When a building has `building:levels` or `height` in OSM, we estimate its height (`levels * 3.5m`) and foundation depth (`clamp(levels * 2.5m, 5.0m, 60.0m)`). When neither tag is present (~85%+ of residential buildings in OSM India), `default_foundation_depth_m: 7.0` (typical 2–3 storey reinforced concrete footing/basement depth) allows deep-bored metro tunnels (>12m depth) to pass underneath low-rise residential blocks while requiring demolition for shallow cut-and-cover or high-rise foundations.
  - `buildings_grid_cell_deg: 0.0009` matches the exact `cellSize: 0.0009` found in `DET`, `PAR`, and `TOK` `buildings_index.bin.gz` headers.
- **How to change**: Edit in `cities/blr.yaml` and re-run Phase 9 (`95_buildings.py`).
