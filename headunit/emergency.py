"""Emergencies: tsunami evacuation zones, routes and assembly areas (offline, from tools/fetch_emergency.py), help
nearby (community centres, Red Cross, hospitals, fire stations from the offline search data), FEMA's open shelters
and USGS earthquakes (online, cached), and alerts for a strong earthquake nearby.
"""
import json
import math
import os
import pathlib
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
EMERGENCY = DATA / "emergency"
CACHE = pathlib.Path(os.environ.get("HEADUNIT_CACHE", pathlib.Path.home() / ".cache/headunit"))
USER_AGENT = "nomad-headunit/0.1 (car head unit)"
QUAKES_WEEK = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_week.geojson"  # ~250 KB
QUAKES_HOUR = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_hour.geojson"  # a few KB
SHELTERS_URL = "https://gis.fema.gov/arcgis/rest/services/NSS/OpenShelters/MapServer/0/query"
# Full-text queries on the offline search index (column filters: a plain bounding box over 18 M rows is slow)
HELP_QUERIES = {"community": 'kind : "community centre"', "hospital": 'kind : "hospital"', "fire": 'kind : "fire station"',
                "redcross": 'name : "red" "cross"'}
NOT_HELP = {"park", "pharmacy drugstore", "blood donation"}  # "Red Cross Park" and the like

_layers = {}  # name -> (mtime, features, bboxes)
_quakes = {"t": 0, "list": []}
_shelters = {"t": 0, "key": None, "list": []}
_alerts = []  # recent earthquake alerts, newest first (the page speaks the ones it hasn't seen)
_lock = threading.Lock()


def km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = 0.5 - math.cos((lat2 - lat1) * p) / 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2
    return 12742 * math.asin(math.sqrt(a))


def layer_path(name):
    if name not in ("zones", "routes", "assembly", "sirens"):
        raise ValueError("no such layer")
    return EMERGENCY / f"{name}.geojson"


def _features(name):
    """The layer's features and their bounding boxes, reloaded when the file changes."""
    p = layer_path(name)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return [], []
    with _lock:
        if name in _layers and _layers[name][0] == mtime:
            return _layers[name][1:]
        feats = json.loads(p.read_text())["features"]
        boxes = [_bbox(f["geometry"]) for f in feats]
        _layers[name] = (mtime, feats, boxes)
        return feats, boxes


def _rings(g):
    if g["type"] == "Polygon":
        yield g["coordinates"]
    elif g["type"] == "MultiPolygon":
        yield from g["coordinates"]


def _bbox(g):
    xs, ys = [], []
    def walk(c):
        if c and isinstance(c[0], (int, float)):
            xs.append(c[0]); ys.append(c[1])
        else:
            for x in c:
                walk(x)
    walk(g["coordinates"])
    return (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 0, 0)


def _inside(lon, lat, ring):
    hit = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


# What each zone means for the driver. Oregon's zones are nested bands: "distant" is nearest the water and
# floods in any tsunami; "local" is the band beyond it that floods only after a Cascadia earthquake offshore.
ZONE_ADVICE = {
    "distant": "In a tsunami zone for any tsunami: leave on a tsunami warning, or right after a strong quake",
    "local": "In the local tsunami zone: after strong shaking here, go to high ground now (waves can come in 15 minutes)",
    "hazard": "In a tsunami hazard area: after strong shaking or a tsunami warning, go to high ground",
}


def zone_at(lat, lon):
    """The tsunami evacuation zone at a point: {"state", "zone": "distant" | "local" | "hazard", "advice"}, or None."""
    feats, boxes = _features("zones")
    found = None
    for f, (x0, y0, x1, y1) in zip(feats, boxes):
        if x0 <= lon <= x1 and y0 <= lat <= y1:
            for poly in _rings(f["geometry"]):
                if _inside(lon, lat, poly[0]) and not any(_inside(lon, lat, hole) for hole in poly[1:]):
                    found = f["properties"]
                    if found.get("zone") != "local":  # the inner band (or WA's single area): the most at risk
                        return {**found, "advice": ZONE_ADVICE[found["zone"]]}
    return {**found, "advice": ZONE_ADVICE[found["zone"]]} if found else None


def assembly_near(lat, lon, n=5):
    feats, _ = _features("assembly")
    out = []
    for f in feats:
        x, y = f["geometry"]["coordinates"][:2]
        p = f["properties"]
        if p.get("kind") in ("Post-tsunami assembly area", "Post-tsunami shelter", "Vertical evacuation structure", "Stair"):
            out.append({**p, "lat": y, "lon": x, "km": round(km(lat, lon, y, x), 2)})
    return sorted(out, key=lambda a: a["km"])[:n]


def help_near(lat, lon, n=4):
    """Community centres, Red Cross, hospitals and fire stations nearby, from the offline search data."""
    db_path = DATA / "search.sqlite"
    out = {k: [] for k in HELP_QUERIES}
    if not db_path.exists():
        return out
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    box = (lat - 0.9, lat + 0.9, lon - 1.3, lon + 1.3)  # about 100 km; Red Cross offices are few and far between
    try:
        for key, q in HELP_QUERIES.items():
            rows = db.execute("SELECT p.name, p.kind, p.address, p.lat, p.lon FROM search JOIN places p ON p.id = search.rowid "
                              "WHERE search MATCH ? AND p.lat BETWEEN ? AND ? AND p.lon BETWEEN ? AND ? AND p.name != ''",
                              (q, *box)).fetchall()
            out[key] = _closest([r for r in rows if r[1] not in NOT_HELP], lat, lon, n)
    finally:
        db.close()
    return out


def _closest(rows, lat, lon, n):
    """Nearest first, one per place (OpenStreetMap often has a building and a point for the same one)."""
    out, seen = [], set()
    for r in sorted(rows, key=lambda r: km(lat, lon, r[3], r[4])):
        key = (r[0].lower(), round(r[3], 2), round(r[4], 2))
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": r[0], "kind": r[1], "address": r[2], "lat": r[3], "lon": r[4], "km": round(km(lat, lon, r[3], r[4]), 2)})
        if len(out) == n:
            break
    return out


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def shelters_near(lat, lon, radius_km=150):
    """FEMA National Shelter System shelters that are open now, within radius_km (online; 15-minute cache)."""
    import network
    key = (round(lat, 1), round(lon, 1))
    if (time.time() - _shelters["t"] < 900 or not network.allowed("ask")) and _shelters["key"] == key:
        return _shelters["list"]
    if not network.allowed("ask"):
        return []
    d = radius_km / 111
    params = {"where": "shelter_status='OPEN'", "outFields": "shelter_name,address,city,state,org_name,evacuation_capacity,latitude,longitude",
              "geometry": f"{lon - d * 1.5},{lat - d},{lon + d * 1.5},{lat + d}", "geometryType": "esriGeometryEnvelope",
              "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "false", "f": "json"}
    try:
        feats = _get(f"{SHELTERS_URL}?{urllib.parse.urlencode(params)}").get("features", [])
    except (OSError, ValueError):
        return _shelters["list"] if _shelters["key"] == key else []
    found = sorted(({"name": a["shelter_name"], "address": f"{a['address']}, {a['city']}", "org": a.get("org_name"),
                     "capacity": a.get("evacuation_capacity"), "lat": a["latitude"], "lon": a["longitude"],
                     "km": round(km(lat, lon, a["latitude"], a["longitude"]), 1)}
                    for a in (f["attributes"] for f in feats) if a.get("latitude")), key=lambda s: s["km"])
    _shelters.update(t=time.time(), key=key, list=found[:10])
    return _shelters["list"]


def _quake(f):
    p, (lon, lat, depth) = f["properties"], f["geometry"]["coordinates"]
    return {"id": f["id"], "mag": p["mag"], "place": p["place"], "time": p["time"] / 1000, "lat": lat, "lon": lon,
            "depth_km": depth, "tsunami": bool(p.get("tsunami")), "alert": p.get("alert"), "url": p.get("url")}


def quakes():
    """USGS earthquakes of magnitude 2.5 and up in the past week (refreshed by quake_loop; cached for offline)."""
    if not _quakes["list"]:
        try:
            _quakes["list"] = json.loads((CACHE / "quakes.json").read_text())
        except (OSError, ValueError):
            pass
    return _quakes["list"]


def quakes_near(lat, lon, radius_km=500, n=8):
    return sorted(({**q, "km": round(km(lat, lon, q["lat"], q["lon"]))} for q in quakes()
                   if km(lat, lon, q["lat"], q["lon"]) <= radius_km), key=lambda q: -q["time"])[:n]


def worth_alerting(q, d):
    """A quake the driver should hear about: strong and near, very strong farther away, or a tsunami flag."""
    return (q["mag"] >= 4.5 and d <= 300) or (q["mag"] >= 6.0 and d <= 1200) or (q["tsunami"] and q["mag"] >= 6.5 and d <= 4000)


def quake_loop(where):
    """The USGS feeds: the last hour every 2 minutes (10 on Data saver) for alerts, the past week every hour
    (6 on saver) for the map; new quakes worth alerting about (for where() = (lat, lon)) go to alerts()."""
    import network
    seen = set()
    first = True
    week_at = 0
    while True:
        saver = network.data_use()["effective"] == "saver"
        if not network.allowed("ask"):
            time.sleep(120)
            continue
        try:
            fresh = [_quake(f) for f in _get(QUAKES_HOUR, 30)["features"]]
            if time.time() - week_at > (6 * 3600 if saver else 3600):
                week = [_quake(f) for f in _get(QUAKES_WEEK, 30)["features"]]
                week_at = time.time()
            else:
                week = quakes()
            ids = {q["id"] for q in fresh}
            feed = sorted(fresh + [q for q in week if q["id"] not in ids and time.time() - q["time"] < 7 * 86400],
                          key=lambda q: -q["time"])
            _quakes.update(t=time.time(), list=feed)
            CACHE.mkdir(parents=True, exist_ok=True)
            (CACHE / "quakes.json").write_text(json.dumps(feed))
            loc = where()
            for q in feed:
                if q["id"] in seen:
                    continue
                seen.add(q["id"])
                if first or not loc or time.time() - q["time"] > 3600:
                    continue  # only quakes that happen while we're running, and recent ones
                d = km(loc[0], loc[1], q["lat"], q["lon"])
                if worth_alerting(q, d):
                    _alerts.insert(0, {**q, "km": round(d)})
                    del _alerts[10:]
            first = False
        except (OSError, ValueError, KeyError) as e:
            print(f"quakes: {e}")
        time.sleep(600 if saver else 120)


def alerts():
    return _alerts


def summary(lat, lon):
    """Everything the Emergency screen shows, for a position."""
    near_coast = [a for a in assembly_near(lat, lon) if a["km"] <= 40]  # assembly areas only matter near the coast
    return {"zone": zone_at(lat, lon), "assembly": near_coast, "help": help_near(lat, lon),
            "shelters": shelters_near(lat, lon), "quakes": quakes_near(lat, lon), "have_tsunami_data": layer_path("zones").exists()}
