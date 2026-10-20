# Third-party software, data and models

NOMAD's own code is GPL-3.0-or-later ([LICENSE](LICENSE)). These keep their own licenses.

## Bundled in this repo (`headunit/static/vendor/`)

| What | Version | License | File |
|---|---|---|---|
| MapLibre GL JS | 5.24.0 | BSD-3-Clause | `vendor/maplibre/LICENSE.txt` |
| PMTiles (JavaScript) | 4.5.0 | BSD-3-Clause | `vendor/pmtiles/LICENSE` |
| Protomaps basemaps (style) | 5.7.2 | BSD-3-Clause | `vendor/protomaps/LICENSE` |
| maplibre-contour | 0.1.1 | BSD-3-Clause | `vendor/maplibre-contour/LICENSE` |
| Noto Sans glyphs (protomaps/basemaps-assets) | 028c18f713 | SIL Open Font License 1.1 | `vendor/map-assets/fonts/OFL.txt` |
| Map sprites (protomaps/basemaps-assets, from tangrams/icons) | 028c18f713 | MIT (Mapzen) | `vendor/map-assets/sprites/LICENSE.md` |

## Installed or built on the Pi (not in this repo)

| What | License |
|---|---|
| llama.cpp, whisper.cpp (ggml-org) | MIT |
| Gemma 4 E2B model weights (Google) | Google's Gemma terms: see the model card on Hugging Face |
| Kokoro-82M voice model (hexgrad), kokoro-onnx | Apache-2.0, MIT |
| Valhalla routing engine | MIT |
| SatDump | GPL-3.0 |
| rtl-sdr, gpsd, chrony, BlueZ, PipeWire | GPL / LGPL (Debian packages) |
| sgp4 (Python), sstv (Python), onnxruntime | MIT and others: see each package |
| DashLink build dependencies: ncnn (Tencent), whisper.cpp | BSD-3-Clause, MIT |
| YOLO11 detector (Ultralytics), optional for DashLink | AGPL-3.0: not distributed here; export it yourself |

## Data

| Source | Used for | Terms |
|---|---|---|
| OpenStreetMap contributors, via Protomaps builds and Geofabrik extracts | street map, place search, routing | ODbL 1.0: show "© OpenStreetMap contributors" (the map does) |
| USGS The National Map, orthoimagery | aerial imagery | public domain (US Government) |
| AWS Terrain Tiles (Mapzen; from USGS 3DEP, SRTM, GMTED and others) | elevation layers | open data, attribution: see registry.opendata.aws/terrain-tiles |
| NOAA / National Weather Service (forecast, alerts, NEXRAD radar, GOES-18 via NESDIS STAR) | weather | public domain (US Government) |
| Washington DNR (Geology: Tsunami_Hazard), Oregon DOGAMI (tsunami evacuation zones) | tsunami layers | public agency data; check each agency's terms before redistributing the downloaded files |
| FEMA National Shelter System (open shelters), USGS earthquake feed | shelters, earthquakes | public domain (US Government) |
| CelesTrak | satellite orbits | free; please don't poll more than needed (NOMAD refreshes every two days) |
| Photon (Komoot), Nominatim, OSRM demo server | online fallbacks only | each service's usage policy; not for bulk use |

## News feeds (default list)

Headlines come from the publishers' public RSS feeds (NPR, BBC, The Guardian, Ars Technica) and stay their property; NOMAD shows titles and summaries and links back.
