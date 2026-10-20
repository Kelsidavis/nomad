#!/bin/sh
# Builds Valhalla routing tiles for the Western US (runs inside ghcr.io/valhalla/valhalla:3.9.1).
set -e
cd /data
mkdir -p valhalla_tiles
valhalla_build_config --mjolnir-tile-dir /data/valhalla_tiles --mjolnir-tile-extract /data/valhalla_tiles.tar \
  --mjolnir-timezone /data/valhalla_tiles/timezones.sqlite --mjolnir-admin /data/valhalla_tiles/admins.sqlite \
  --mjolnir-concurrency 10 > valhalla.json
valhalla_build_timezones > valhalla_tiles/timezones.sqlite
valhalla_build_admins -c valhalla.json us-west-latest.osm.pbf
valhalla_build_tiles -c valhalla.json us-west-latest.osm.pbf
valhalla_build_extract -c valhalla.json -v
ls -la valhalla_tiles.tar valhalla_tiles/*.sqlite
echo VALHALLA_DONE
