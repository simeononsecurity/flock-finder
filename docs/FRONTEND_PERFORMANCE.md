# Frontend performance report

Date: 2026-10-14

## Scope

This report compares the former full-GeoJSON startup path with the PMTiles viewport path. The source artifact available in the checkout contains 146,526 features. The summary statistics file is newer and reports 154,060 records, so production must generate the archive and statistics from the same immutable release.

## Before

The former page requested and parsed the complete `flock_cameras.geojson` file, then created one Leaflet marker for every feature.

| Measure | Result |
| --- | ---: |
| GeoJSON bytes | 94,281,637 |
| GeoJSON SHA-256 | `bc536e148a9f762af097376fa2fddb2ece4e86a3f3c5f4beaf6b6e27836684fb` |
| Features | 146,526 |
| Local Python file read | 0.0122 seconds |
| Local Python JSON parse | 0.8326 seconds |
| Browser dataset request | Full 94.3 MB file before marker creation |

The Python timings isolate file read and parse cost. They do not include browser transfer, JavaScript object allocation, deduplication, marker construction, clustering, style work, or paint.

## After

The page now reads only PMTiles byte ranges for visible tiles. Tippecanoe preclusters records through zoom 11. Zoom 12 and above use individual vector points. Exact BSSID search loads one lazy prefix shard instead of the full dataset.

| Measure | Result |
| --- | ---: |
| PMTiles archive bytes on disk | 45,647,077 |
| PMTiles SHA-256 | `e0dcba785fbc1fca0fc2d1d6cb8ffb9478af560d83eaefbdd6feb3c4766545d4` |
| Initial PMTiles range requests | 6 |
| Initial PMTiles transfer bytes | 57,988 |
| Initial PMTiles decoded bytes | 56,188 |
| First map idle | 479-499 ms across two final runs |
| Local BSSID shards | 27 files, 6,071,958 bytes total |
| Example BSSID shard (`00.json`) | 25,775 bytes |
| Full GeoJSON requests | 0 |
| BSSID prefix shards | 27, loaded only for exact searches |

The post-change figures came from two installed-Chrome Playwright runs on 2026-10-14, using a local HTTP server with byte-range support and an empty mocked basemap. The timer starts at navigation and ends on the first MapLibre `idle` event. The checked archive and shards are local generated assets in this checkout. The basemap mock means these figures measure dataset startup and map rendering without a real tile transfer.

Initial dataset transfer fell by 99.9385 percent, from 94,281,637 bytes to 57,988 bytes. The initial viewport path transferred about 1,626 times fewer dataset bytes. The full archive is 51.5 percent smaller than the former GeoJSON, but normal browsing never downloads the full archive.

## Verification

Run from `frontend-tests/`:

```text
npm install
npm test
```

The browser suite checks range requests, absence of full-GeoJSON requests, exact BSSID privacy and local shard lookup, explicit geocoding, the one-request-per-second limit, browser result caching, search states, keyboard result access, landmarks, and serious or critical axe findings. A Node test also verifies the map asset builder applies the published anchored SSID correlation regular expressions.

Build the local generated assets from the repository root:

```text
node docs/assets/build-map-assets.mjs data/flock_cameras.geojson data/flock_ouis.json docs/assets
```

The build prints the source record count, BSSID shard count, tippecanoe version, archive byte count, and SHA-256. Generated PMTiles and BSSID shards are ignored by Git. The release pipeline must publish them beside the matching immutable source dataset.
