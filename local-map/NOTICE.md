# Notices for Local Map

Local Map is by Cypher Sisters, under the Apache-2.0 License. It ships the files below from others, unchanged, and uses data that each owner fetches for their own use.

## Shipped in this package

| File | What it is | License | Fetched from | sha256 |
|---|---|---|---|---|
| `web/vendor/leaflet.js` | Leaflet 1.9.4, the map viewer | BSD-2-Clause (`web/vendor/licenses/LICENSE-leaflet.txt`) | https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js | `db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a` |
| `web/vendor/leaflet.css` | Leaflet 1.9.4 styles | BSD-2-Clause | https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css | `a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6` |
| `web/vendor/protomaps-leaflet.js` | protomaps-leaflet 5.1.0, draws vector tiles on a canvas | BSD-3-Clause (`web/vendor/licenses/LICENSE-protomaps-leaflet.txt`) | https://cdn.jsdelivr.net/npm/protomaps-leaflet@5.1.0/dist/protomaps-leaflet.js | `26af014f7b1af308ec120b791cff76657bb9c3383633b52033e6edf9e5e4cdb5` |
| `web/vendor/licenses/LICENSE-leaflet.txt` | Leaflet's license text | | https://raw.githubusercontent.com/Leaflet/Leaflet/v1.9.4/LICENSE | `53e8dc25862014e4324741ca18fbe3611e11d42ef69f59f86ea8c5389647d4cb` |
| `web/vendor/licenses/LICENSE-protomaps-leaflet.txt` | protomaps-leaflet's license text | | https://raw.githubusercontent.com/protomaps/protomaps-leaflet/main/LICENSE | `a2ada548aa25012e833b71364a402d6924f2afcf5f8f146e2989a377290ab3ad` |
| `data/places.tsv.gz` | Towns of 1,000 people or more, trimmed, gzip-compressed (see below) | CC BY 4.0, GeoNames | https://download.geonames.org/export/dump/cities1000.zip | `bf77c27ea709dcc707f240ec6990f81a165e656d7a16be889ae110593389dd14` |
| `data/regions.tsv.gz` | Names of states and regions (first-level divisions), gzip-compressed | CC BY 4.0, GeoNames | https://download.geonames.org/export/dump/admin1CodesASCII.txt | `fac3d839b7cf539e05101c1e90f386632bee440f2bcc34ae2a749e195340f0ba` |

**Place names from GeoNames** (geonames.org), licensed under Creative Commons Attribution 4.0 (CC BY 4.0). `data/places.tsv.gz` is made from `cities1000.txt` in `cities1000.zip` (zip sha256 `94b9d1b363ed0d3dd32f1a0a34622bfd3bdb5cfd454d5c9463d1f6c3e3ef31f3`), fetched 2026-11-15: one line per town (171,939 towns) with its name, its ASCII name, admin1 code, country code, latitude and longitude rounded to 4 decimals, and population, sorted by country, admin1 code and name. Every other column was left out. It is 3.2 MB compressed. `data/regions.tsv.gz` is made from `admin1CodesASCII.txt` (sha256 `2e4867f5397acb056ea26377601b57af37cb4c81399770225018e9e439e91ad3`), fetched 2026-11-15: each code (country.admin1) and its name; the ASCII name and GeoNames id columns were left out. It is used to show the state or region next to each town.

## Fetched by each owner, not shipped

- **Map data (c) OpenStreetMap contributors**, under the Open Database License (openstreetmap.org/copyright). The emergency points are fetched by each owner, for their own use, from the Overpass API at overpass-api.de, and the street map tiles are made from OpenStreetMap data. None of it is in this package.
- **Basemap tiles** come from the Protomaps project's public daily planet builds (build.protomaps.com, listed at build-metadata.protomaps.dev), cut to the owner's region on their own computer.

## Specifications and ideas

- **PMTiles v3 specification** (github.com/protomaps/PMTiles, `spec/v3/spec.md`), public domain. The reader and extractor in `fetcher.py` are our own code, written from the specification.
- The idea of a standard-library extractor that reads only the tiles for one area follows GLEAPP-MapDownloader (MIT) and wjmallard/offline-maps (MIT). No code was copied from either.
