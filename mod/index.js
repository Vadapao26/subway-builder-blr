/**
 * Subway Builder Custom City Mod Entry Point: Bengaluru + Hosur (BLR)
 *
 * Automatically registers the city BLR with its initial view state,
 * difficulty, camera limits, and vector tile URL overrides (when local server is used).
 */
(function () {
  'use strict';

  function initMod(api) {
    if (!api || !api.registerCity) {
      console.warn('[BLR Mod] SubwayBuilderAPI not available or missing registerCity.');
      return;
    }

    const cityConfig = {
      name: 'Bengaluru',
      code: 'BLR',
      description: 'Build transit for the Garden City, from Hosur to Kempegowda International Airport.',
      population: 13978303,
      difficulty: 'medium',
      initialViewState: {
        zoom: 11.5,
        latitude: 12.97,
        longitude: 77.59,
        bearing: 0,
      },
      minZoom: 8,
      maxZoom: 15,
      buildingZoomOffset: 0.0,
      hasCoastline: false,
    };

    try {
      api.registerCity(cityConfig);
      console.log('[BLR Mod] Registered city: Bengaluru (BLR)');
    } catch (err) {
      console.error('[BLR Mod] Error registering city:', err);
    }

    // If served via serve-data.js HTTP tile server, configure tile override
    if (api.map && typeof api.map.setTileURLOverride === 'function') {
      try {
        api.map.setTileURLOverride({
          cityCode: 'BLR',
          tilesUrl: 'http://127.0.0.1:8080/BLR/tiles/{z}/{x}/{y}.mvt',
          foundationTilesUrl: 'http://127.0.0.1:8080/BLR/foundations/{z}/{x}/{y}.mvt',
          maxZoom: 15,
        });
        console.log('[BLR Mod] Tile URL override configured for BLR');
      } catch (err) {
        console.warn('[BLR Mod] Could not set tile URL override (native map:// will be used):', err);
      }
    }
  }

  // Register when SubwayBuilderAPI is present or hook onto window load
  if (typeof window !== 'undefined') {
    if (window.SubwayBuilderAPI) {
      initMod(window.SubwayBuilderAPI);
    } else {
      window.addEventListener('load', () => {
        if (window.SubwayBuilderAPI) {
          initMod(window.SubwayBuilderAPI);
        }
      });
    }
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { initMod };
  }
})();
