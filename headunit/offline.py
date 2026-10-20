"""Offline map data for the head unit: vector map (PMTiles), aerial imagery (MBTiles), place search
(SQLite FTS5) and routing (a local Valhalla). All optional: whatever is missing falls back to online.

Data lives in ~/.local/share/headunit (HEADUNIT_DATA):
    maps/*.pmtiles               street map (Protomaps basemap schema)
    imagery/region.mbtiles       aerial imagery, whole region, low zoom
    imagery/home.mbtiles         aerial imagery, home area, street level
    search.sqlite                built by tools/build_search.py
Valhalla answers on 127.0.0.1:8002 (HEADUNIT_VALHALLA).
"""
import json
import math
import os
import pathlib
import re
import sqlite3
import threading
import urllib.error
import urllib.request

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
VALHALLA = os.environ.get("HEADUNIT_VALHALLA", "http://127.0.0.1:8002")

_local = threading.local()  # one SQLite connection per server thread


def _db(path):
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    if path not in conns:
        conns[path] = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return conns[path]


# --- what we have -------------------------------------------------------------------------------

def maps():
    return sorted((DATA / "maps").glob("*.pmtiles"))


def imagery_sets():
    """{"region": {"bounds": [w, s, e, n], "minzoom", "maxzoom"}, ...} for each imagery file present."""
    out = {}
    for p in sorted((DATA / "imagery").glob("*.mbtiles")):
        try:
            meta = dict(_db(p).execute("SELECT name, value FROM metadata"))
            out[p.stem] = {"bounds": [float(v) for v in meta["bounds"].split(",")],
                           "minzoom": int(meta["minzoom"]), "maxzoom": int(meta["maxzoom"])}
        except (sqlite3.Error, KeyError, ValueError):
            continue
    return out


TERRAIN = DATA / "terrain" / "terrain.mbtiles"  # elevation, terrarium PNG (tools/fetch_imagery.py --source terrain)
TERRAIN_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"  # AWS Terrain Tiles


def terrain_info():
    try:
        meta = dict(_db(TERRAIN).execute("SELECT name, value FROM metadata"))
        return {"bounds": [float(v) for v in meta["bounds"].split(",")], "maxzoom": int(meta["maxzoom"])}
    except (sqlite3.Error, KeyError, ValueError):
        return None


def terrain_tile(z, x, y):
    """An elevation tile from the downloaded file, or None (not there, or not downloaded yet)."""
    if not TERRAIN.exists():
        return None
    row = _db(TERRAIN).execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                               (z, x, 2 ** z - 1 - y)).fetchone()
    return row[0] if row else None


def status():
    return {"maps": [p.name for p in maps()], "imagery": imagery_sets(), "terrain": terrain_info(),
            "search": (DATA / "search.sqlite").exists(), "routing": valhalla_up()}


def imagery_tile(name, z, x, y):
    p = DATA / "imagery" / f"{name}.mbtiles"
    if not re.fullmatch(r"[a-z0-9_-]+", name) or not p.exists():
        return None
    row = _db(p).execute("SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                         (z, x, 2 ** z - 1 - y)).fetchone()
    return row[0] if row else None


# --- search -------------------------------------------------------------------------------------

# Spoken and written short forms, matched both ways ("mt st helens" finds "Mount Saint Helens").
ABBREV = [("st", "saint", "street"), ("mt", "mount"), ("ft", "fort"), ("ave", "avenue"), ("rd", "road"),
          ("dr", "drive"), ("blvd", "boulevard"), ("hwy", "highway"), ("ln", "lane"), ("ct", "court"),
          ("pl", "place"), ("pkwy", "parkway"), ("n", "north"), ("s", "south"), ("e", "east"), ("w", "west"),
          ("ne", "northeast"), ("nw", "northwest"), ("se", "southeast"), ("sw", "southwest"), ("&", "and")]
ALTS = {w: group for group in ABBREV for w in group}
# Category phrases from tools/build_search.py, shown as a short label under unnamed results.
LABELS = {"gas station fuel": "Gas station", "restaurant food": "Restaurant", "cafe coffee": "Cafe",
          "supermarket grocery store": "Supermarket", "pharmacy drugstore": "Pharmacy", "hotel lodging": "Hotel",
          "motel lodging": "Motel", "ev charger charging station": "EV charger", "restroom toilets": "Restroom",
          "atm cash": "ATM", "university college": "University", "campground camping": "Campground",
          "auto repair mechanic": "Auto repair", "hardware home improvement": "Home improvement",
          "mall shopping": "Mall", "peak mountain": "Peak", "volcano mountain": "Volcano", "pub bar": "Pub",
          "tires tire shop": "Tire shop", "vet veterinary": "Vet", "church place of worship": "Place of worship"}

def search(q, lat=None, lon=None, limit=10):
    """Local place search: nearby matches first, but big places (cities, airports) from anywhere."""
    p = DATA / "search.sqlite"
    if not p.exists():
        return None
    words = re.findall(r"\w+", q.lower())
    if not words:
        return []
    # Only the last word is a prefix (it may still be being typed); short forms match exactly.
    match = " AND ".join("(" + " OR ".join(f'"{a}"' + ("*" if i == len(words) - 1 and a == w else "")
                                           for a in ALTS.get(w, (w,))) + ")" for i, w in enumerate(words))
    db = _db(p)
    sql = ("SELECT p.name, p.kind, p.address, p.lat, p.lon, p.rank FROM search JOIN places p ON p.id = search.rowid "
           "WHERE search MATCH ? {where} ORDER BY {order} LIMIT ?")
    rows = []
    if lat is not None:
        rows += db.execute(sql.format(where="AND p.lat BETWEEN ? AND ? AND p.lon BETWEEN ? AND ?",
                                      order="(p.lat - ?) * (p.lat - ?) + (p.lon - ?) * (p.lon - ?) * 0.5"),
                           (match, lat - 1, lat + 1, lon - 1.4, lon + 1.4, lat, lat, lon, lon, 200)).fetchall()
    # From anywhere, only places worth driving far for: towns, landmarks, airports.
    rows += db.execute(sql.format(where="AND p.rank >= 5", order="p.rank DESC"), (match, 40)).fetchall()
    wanted = canonical(q)
    seen, scored = set(), []
    for name, kind, address, plat, plon, rank in rows:
        key = (name, round(plat, 4), round(plon, 4))
        if key in seen:
            continue
        seen.add(key)
        km = distance_km(lat, lon, plat, plon) if lat is not None else 0
        score = (km + 0.5) / (1 + rank)
        if name and canonical(name) == wanted:
            score /= 20 if rank >= 5 else 3  # exactly what was asked for; a landmark or town much more so
        scored.append((score, {"name": name, "address": address or label(kind),
                               "lat": plat, "lon": plon}))
    scored.sort(key=lambda s: s[0])
    return [r for _, r in scored[:limit]]


def canonical(text):
    """Lowercase words with short forms unified: "Mt. St. Helens" == "mount saint helens"."""
    return " ".join(ALTS.get(w, (w,))[0] for w in re.findall(r"\w+", text.lower()))


def label(kind):
    first = (kind or "").split(";")[0].strip()
    return LABELS.get(first, first.capitalize())


def distance_km(lat1, lon1, lat2, lon2):
    kx = 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon2 - lon1) * kx, (lat2 - lat1) * 110.54)


# --- routing (Valhalla) ---------------------------------------------------------------------------

def valhalla_up():
    try:
        with urllib.request.urlopen(VALHALLA + "/status", timeout=1) as r:
            return r.status == 200
    except (OSError, urllib.error.URLError):
        return False


# Valhalla maneuver type -> (OSRM-style type, modifier), which is what the UI's icons understand.
MANEUVERS = {
    1: ("depart", ""), 2: ("depart", "right"), 3: ("depart", "left"),
    4: ("arrive", ""), 5: ("arrive", "right"), 6: ("arrive", "left"),
    7: ("new name", "straight"), 8: ("continue", "straight"),
    9: ("turn", "slight right"), 10: ("turn", "right"), 11: ("turn", "sharp right"),
    12: ("turn", "uturn"), 13: ("turn", "uturn"),
    14: ("turn", "sharp left"), 15: ("turn", "left"), 16: ("turn", "slight left"),
    17: ("on ramp", "straight"), 18: ("on ramp", "slight right"), 19: ("on ramp", "slight left"),
    20: ("off ramp", "slight right"), 21: ("off ramp", "slight left"),
    22: ("fork", "straight"), 23: ("fork", "slight right"), 24: ("fork", "slight left"),
    25: ("merge", "straight"), 26: ("roundabout", ""), 27: ("continue", "straight"),
    28: ("continue", "straight"), 29: ("continue", "straight"),
    37: ("merge", "slight right"), 38: ("merge", "slight left"),
}


def route(frm, to, units="km"):
    """Driving route from the local Valhalla, in the same shape as the online OSRM route. Each step also carries
    Valhalla's spoken wording (verbal_*), with distances in miles when units is "mi"."""
    body = {"locations": [{"lat": frm[0], "lon": frm[1]}, {"lat": to[0], "lon": to[1]}],
            "costing": "auto", "units": "miles" if units == "mi" else "kilometers",
            "directions_options": {"language": "en-US", "units": "miles" if units == "mi" else "kilometers"}}
    per_unit = 1609.344 if units == "mi" else 1000  # Valhalla reports lengths in the requested unit
    req = urllib.request.Request(VALHALLA + "/route", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        trip = json.load(r)["trip"]
    coords, steps = [], []
    for leg in trip["legs"]:
        shape = decode_polyline6(leg["shape"])
        for m in leg["maneuvers"]:
            if m["type"] == 27:  # "exit the roundabout": the entry step already said which exit
                continue
            kind, mod = MANEUVERS.get(m["type"], ("continue", "straight"))
            at = shape[min(m["begin_shape_index"], len(shape) - 1)]
            steps.append({"lat": at[0], "lon": at[1], "type": kind, "modifier": mod,
                          "name": ", ".join(m.get("street_names") or []),
                          "distance": m.get("length", 0) * per_unit, "duration": m.get("time", 0),
                          "instruction": m.get("instruction", "").rstrip("."),
                          # spoken: before the turn ("Turn right onto Main Street"), well before it
                          # ("In 1 mile, turn right"), and right after ("Continue for 2 miles")
                          "verbal_pre": m.get("verbal_pre_transition_instruction", ""),
                          "verbal_alert": m.get("verbal_transition_alert_instruction", ""),
                          "verbal_post": m.get("verbal_post_transition_instruction", "")})
        coords += shape if not coords else shape[1:]
    return {"distance": trip["summary"]["length"] * per_unit, "duration": trip["summary"]["time"],
            "coords": coords, "steps": steps, "source": "offline"}


def decode_polyline6(s):
    out, lat, lon, i = [], 0, 0, 0
    while i < len(s):
        vals = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(result >> 1) if result & 1 else result >> 1)
        lat += vals[0]
        lon += vals[1]
        out.append([lat / 1e6, lon / 1e6])
    return out
