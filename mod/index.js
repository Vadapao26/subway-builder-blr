// Subway Builder Mod Entry - Bengaluru (BLR)
(function () {
  if (typeof window === "undefined" || !window.SubwayBuilderAPI) return;
  const api = window.SubwayBuilderAPI;
  api.cities.registerCity({
    code: "BLR",
    name: "Bengaluru",
    country: "IN",
    creator: "Vadapao26",
    bbox: [77.4, 12.7, 77.9, 13.25],
    initialViewState: {
      latitude: 12.97,
      longitude: 77.59,
      zoom: 11.5,
      bearing: 0
    },
    demandDotScaling: 0.2
  });
})();
