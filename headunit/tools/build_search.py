#!/usr/bin/env python3
"""Build the offline place search index (SQLite FTS5) from an OpenStreetMap extract.

    osmium tags-filter -O -o search-src.osm.pbf region.osm.pbf nwr/amenity nwr/shop nwr/tourism nwr/leisure \
        nwr/office nwr/craft nwr/healthcare nwr/historic n/place nwr/aeroway=aerodrome nwr/railway=station \
        nwr/public_transport=station nwr/natural=peak,volcano nwr/addr:housenumber nwr/boundary=national_park \
        nwr/landuse=retail
    build_search.py search-src.osm.pbf search.sqlite

Needs osmium-tool on the build machine; the result is used by server.py with only the stdlib.
"""
import json
import sqlite3
import subprocess
import sys
import time

# Words people say for a category, so "gas station" or "coffee" find places that are tagged that way.
WORDS = {
    "fuel": "gas station fuel", "charging_station": "ev charger charging station", "fast_food": "fast food",
    "cafe": "cafe coffee", "restaurant": "restaurant food", "bar": "bar", "pub": "pub bar",
    "supermarket": "supermarket grocery store", "convenience": "convenience store", "pharmacy": "pharmacy drugstore",
    "hospital": "hospital", "clinic": "clinic", "doctors": "doctor", "dentist": "dentist", "parking": "parking",
    "toilets": "restroom toilets", "bank": "bank", "atm": "atm cash", "post_office": "post office",
    "police": "police", "fire_station": "fire station", "school": "school", "university": "university college",
    "library": "library", "hotel": "hotel lodging", "motel": "motel lodging", "camp_site": "campground camping",
    "car_repair": "auto repair mechanic", "car_wash": "car wash", "hardware": "hardware store",
    "doityourself": "hardware home improvement", "department_store": "department store", "mall": "mall shopping",
    "aerodrome": "airport", "station": "station", "peak": "peak mountain", "volcano": "volcano mountain",
    "park": "park",
    "rest_area": "rest area", "ice_cream": "ice cream", "bakery": "bakery", "car": "car dealer",
    "tyres": "tires tire shop", "veterinary": "vet veterinary", "place_of_worship": "church place of worship",
}
CATEGORY_KEYS = ("amenity", "shop", "tourism", "leisure", "office", "craft", "healthcare", "historic",
                 "aeroway", "railway", "public_transport", "natural", "place", "boundary", "landuse")
PLACE_RANK = {"city": 9, "town": 8, "village": 6, "hamlet": 4, "suburb": 5, "neighbourhood": 4, "locality": 3}


def centroid(geom):
    """Middle of the bounding box: good enough to drive to."""
    t, c = geom["type"], geom["coordinates"]
    pts = [c] if t == "Point" else c if t == "LineString" else c[0] if t == "Polygon" else c[0][0]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(ys) + max(ys)) / 2, (min(xs) + max(xs)) / 2


def record(tags):
    name = tags.get("name") or tags.get("brand")
    number, street = tags.get("addr:housenumber"), tags.get("addr:street")
    street_addr = f"{number} {street}" if number and street else None
    city = tags.get("addr:city")
    kind_key, kind_val = next(((k, tags[k]) for k in CATEGORY_KEYS if k in tags), (None, None))
    if not name and not street_addr:
        return None
    words = []
    if kind_val and kind_val not in ("yes",):
        words.append(WORDS.get(kind_val, kind_val.replace("_", " ")))
    if tags.get("cuisine"):
        words.append(tags["cuisine"].replace(";", " ").replace("_", " "))
    if tags.get("brand") and tags.get("brand") != name:
        words.append(tags["brand"])
    rank = PLACE_RANK.get(tags.get("place"), 0) if kind_key == "place" else (
        7 if kind_val in ("aerodrome", "volcano") else 5 if kind_val == "peak" else 3 if name else 1)
    title = name or street_addr
    address = ", ".join(x for x in (street_addr if street_addr != title else None, city) if x)
    # "; " keeps the phrases apart for the result label; the full-text tokenizer ignores it.
    return title, "; ".join(words), address, rank


def main(src, out):
    db = sqlite3.connect(out)
    db.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        DROP TABLE IF EXISTS places; DROP TABLE IF EXISTS search;
        CREATE TABLE places (id INTEGER PRIMARY KEY, name TEXT, kind TEXT, address TEXT, lat REAL, lon REAL, rank INTEGER);
    """)
    proc = subprocess.Popen(["osmium", "export", src, "-f", "geojsonseq", "-o", "-", "-x", "print_record_separator=false",
                             "--geometry-types=point,linestring,polygon"], stdout=subprocess.PIPE, text=True,
                            bufsize=1 << 20)
    batch, n, t0 = [], 0, time.time()
    for line in proc.stdout:
        try:
            f = json.loads(line)
            rec = record(f["properties"])
            if not rec:
                continue
            lat, lon = centroid(f["geometry"])
        except (ValueError, KeyError, IndexError, TypeError):
            continue
        batch.append((*rec[:3], round(lat, 6), round(lon, 6), rec[3]))
        if len(batch) >= 50000:
            db.executemany("INSERT INTO places (name, kind, address, lat, lon, rank) VALUES (?,?,?,?,?,?)", batch)
            n += len(batch)
            batch = []
            print(f"  {n:,} places  {time.time() - t0:.0f}s", flush=True)
    db.executemany("INSERT INTO places (name, kind, address, lat, lon, rank) VALUES (?,?,?,?,?,?)", batch)
    n += len(batch)
    proc.wait()
    print(f"{n:,} places; building full-text index", flush=True)
    db.executescript("""
        CREATE VIRTUAL TABLE search USING fts5(name, kind, address, content='places', content_rowid='id',
                                               tokenize='unicode61 remove_diacritics 2', prefix='2 3');
        INSERT INTO search(rowid, name, kind, address) SELECT id, name, kind, address FROM places;
        INSERT INTO search(search) VALUES('optimize');
        CREATE INDEX places_rank ON places(rank) WHERE rank >= 6;
        VACUUM;
    """)
    db.close()
    print(f"done in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:3])
