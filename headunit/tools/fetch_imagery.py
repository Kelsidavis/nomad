#!/usr/bin/env python3
"""Download map tiles into an MBTiles file for offline use: USGS aerial imagery (public domain, NAIP-based, US
only), or elevation (--source terrain: Mapzen/AWS Terrain Tiles, terrarium PNG, from USGS 3DEP, SRTM and others).

    fetch_imagery.py OUT.mbtiles WEST,SOUTH,EAST,NORTH MINZOOM MAXZOOM [--source usgs|terrain] [--threads 6]

Examples:
    fetch_imagery.py imagery-region.mbtiles -125,31,-102,49.1 0 12      # whole region, ~10 m/pixel
    fetch_imagery.py imagery-home.mbtiles -118.7,33.6,-117.6,34.4 13 16  # one metro at street level
    fetch_imagery.py terrain.mbtiles -125,31,-102,49.1 0 11 --source terrain   # elevation, ~50 m/pixel

Resumable: tiles already in the file are skipped. Stdlib only.
"""
import argparse
import math
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SOURCES = {
    "usgs": {"url": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
             "name": "USGS Imagery", "format": "jpg", "type": "baselayer",
             "attribution": "USGS The National Map: Orthoimagery (public domain)"},
    "terrain": {"url": "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png",
                "name": "Terrain", "format": "png", "type": "overlay", "encoding": "terrarium",
                "attribution": "Terrain: Mapzen, USGS 3DEP, SRTM (AWS Terrain Tiles)"},
}
USER_AGENT = "pi-headunit/0.1 (personal car head unit; offline imagery)"


def tile_range(bbox, z):
    w, s, e, n = bbox
    count = 2 ** z

    def xy(lon, lat):
        lat = max(-85.0511, min(85.0511, lat))
        x = int((lon + 180) / 360 * count)
        y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * count)
        return min(count - 1, x), min(count - 1, y)

    x0, y0 = xy(w, n)
    x1, y1 = xy(e, s)
    return x0, x1, y0, y1


def open_db(path, bbox, minz, maxz, source):
    db = sqlite3.connect(path, check_same_thread=False)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB,
                                          PRIMARY KEY (zoom_level, tile_column, tile_row));
        CREATE TABLE IF NOT EXISTS missing (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER,
                                            PRIMARY KEY (zoom_level, tile_column, tile_row));
    """)
    meta = {**{k: v for k, v in source.items() if k != "url"}, "version": "1",
            "bounds": ",".join(map(str, bbox)), "minzoom": str(minz), "maxzoom": str(maxz)}
    db.executemany("INSERT OR REPLACE INTO metadata VALUES (?, ?)", meta.items())
    db.commit()
    return db


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument("bbox", help="west,south,east,north in degrees")
    ap.add_argument("minzoom", type=int)
    ap.add_argument("maxzoom", type=int)
    ap.add_argument("--source", choices=SOURCES, default="usgs")
    ap.add_argument("--threads", type=int, default=6)
    args = ap.parse_args()
    bbox = [float(v) for v in args.bbox.split(",")]
    source = SOURCES[args.source]
    db = open_db(args.out, bbox, args.minzoom, args.maxzoom, source)
    lock = threading.Lock()
    stats = {"got": 0, "skip": 0, "empty": 0, "fail": 0, "bytes": 0}

    def fetch(z, x, y):
        for attempt in range(4):
            try:
                req = urllib.request.Request(source["url"].format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None  # no imagery here (ocean, outside the US)
                time.sleep(2 ** attempt)
            except (OSError, urllib.error.URLError):
                time.sleep(2 ** attempt)
        raise OSError("gave up")

    def work(job):
        z, x, y = job
        try:
            data = fetch(z, x, y)
        except OSError:
            with lock:
                stats["fail"] += 1
            return
        tms_y = 2 ** z - 1 - y  # MBTiles rows count from the bottom
        with lock:
            if data:
                db.execute("INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)", (z, x, tms_y, data))
                stats["got"] += 1
                stats["bytes"] += len(data)
            else:
                db.execute("INSERT OR REPLACE INTO missing VALUES (?, ?, ?)", (z, x, tms_y))
                stats["empty"] += 1

    t0 = time.time()
    for z in range(args.minzoom, args.maxzoom + 1):
        x0, x1, y0, y1 = tile_range(bbox, z)
        have = {(r[0], r[1]) for r in db.execute(
            "SELECT tile_column, tile_row FROM tiles WHERE zoom_level=? UNION "
            "SELECT tile_column, tile_row FROM missing WHERE zoom_level=?", (z, z))}
        jobs = [(z, x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1) if (x, 2 ** z - 1 - y) not in have]
        stats["skip"] += (x1 - x0 + 1) * (y1 - y0 + 1) - len(jobs)
        print(f"z{z}: {len(jobs)} tiles to fetch", flush=True)
        with ThreadPoolExecutor(args.threads) as pool:
            for i, _ in enumerate(pool.map(work, jobs), 1):
                if i % 2000 == 0:
                    with lock:
                        db.commit()
                    rate = stats["got"] / max(1, time.time() - t0)
                    print(f"  z{z} {i}/{len(jobs)}  {stats['bytes'] / 1e9:.2f} GB  {rate:.0f} tiles/s", flush=True)
        db.commit()
    print(f"done: {stats}  {time.time() - t0:.0f}s", flush=True)
    return 1 if stats["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
