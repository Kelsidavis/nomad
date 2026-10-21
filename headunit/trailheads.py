"""Trailheads, campgrounds and viewpoints from OpenStreetMap, kept for use with no signal.

The offline map (Protomaps tiles) draws trails and forest service roads but carries no trailheads, campgrounds or viewpoints,
so these come from OpenStreetMap's Overpass service: whenever the truck is online and in a place it hasn't fetched yet, a 60 km box around
it is downloaded and merged into DATA/trailheads.json. Everything fetched stays, so a region visited once is there on the next trip
with no internet. The map layer (app.js "Trailheads") and the card you get by tapping one read /api/trailheads.
"""
import json
import math
import os
import pathlib
import threading
import time
import urllib.parse
import urllib.request

import config
import durable
import network

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
FILE = DATA / "trailheads.json"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://lz4.overpass-api.de/api/interpreter", "https://z.overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter", "https://overpass.private.coffee/api/interpreter"]  # (the public servers are often at capacity)
RADIUS_KM = 60
RECHECK_S = 14 * 86400   # an area is fetched again after two weeks
USER_AGENT = "pi-headunit/0.1 (personal car head unit)"
_lock = threading.Lock()
_db = {"items": {}, "areas": []}


def _load():
    try:
        _db.update(json.loads(FILE.read_text()))
    except (OSError, ValueError):
        pass


def _km(a, b, c, d):
    p = math.pi / 180
    x = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(x))


def _query(lat, lon, radius_km):
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(0.2, math.cos(math.radians(lat))))
    box = f"{lat - dlat:.4f},{lon - dlon:.4f},{lat + dlat:.4f},{lon + dlon:.4f}"
    return (f'[out:json][timeout:60];('
            f'node["highway"="trailhead"]({box});node["tourism"="camp_site"]({box});node["tourism"="viewpoint"]({box});'
            f'node["amenity"="ranger_station"]({box}););out body;')


def _kind(tags):
    if tags.get("highway") == "trailhead":
        return "trailhead"
    if tags.get("tourism") == "camp_site":
        return "camp"
    if tags.get("tourism") == "viewpoint":
        return "viewpoint"
    return "ranger"


def parse(data):
    """Overpass JSON -> {id: item}."""
    out = {}
    for el in data.get("elements", []):
        if el.get("type") != "node" or "lat" not in el:
            continue
        t = el.get("tags", {})
        out[f"n{el['id']}"] = {k: v for k, v in {
            "id": f"n{el['id']}", "kind": _kind(t), "lat": round(el["lat"], 5), "lon": round(el["lon"], 5),
            "name": t.get("name"), "ref": t.get("ref"), "fee": t.get("fee"), "access": t.get("access"),
            "operator": t.get("operator"), "capacity": t.get("capacity"), "website": t.get("website") or t.get("contact:website"),
            "description": (t.get("description") or "")[:200] or None}.items() if v}
    return out


def fetch(lat, lon, radius_km=RADIUS_KM):
    """Download the area around (lat, lon) and merge it in. Returns how many places were found."""
    body = urllib.parse.urlencode({"data": _query(lat, lon, radius_km)}).encode()
    last = None
    for url in OVERPASS:
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as r:
                found = parse(json.load(r))
            break
        except (OSError, ValueError) as e:
            last = e
    else:
        raise OSError(f"Overpass: {last}")
    with _lock:
        _db["items"].update(found)
        _db["areas"] = [a for a in _db["areas"] if _km(a["lat"], a["lon"], lat, lon) > 5] + [{"lat": lat, "lon": lon, "r": radius_km, "t": time.time(), "n": len(found)}]
        durable.write_text(FILE, json.dumps(_db))
    return len(found)


def covered(lat, lon):
    """The truck is well inside an area fetched recently."""
    now = time.time()
    with _lock:
        return any(_km(a["lat"], a["lon"], lat, lon) < a["r"] * 0.5 and now - a["t"] < RECHECK_S for a in _db["areas"])


def geojson():
    with _lock:
        items = list(_db["items"].values())
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [i["lon"], i["lat"]]}, "properties": i} for i in items]}


def status():
    with _lock:
        return {"places": len(_db["items"]), "areas": [{**a, "age_h": round((time.time() - a["t"]) / 3600)} for a in _db["areas"]]}


def loop(where):
    """Fetch the area around the truck when online and it hasn't been fetched lately (checked every ten minutes)."""
    time.sleep(60)
    fails = 0
    while True:
        try:
            p = where()
            if p and network.allowed("ask") and not covered(*p):
                n = fetch(*p)
                fails = 0
                print(f"trailheads: {n} places around {p[0]:.2f},{p[1]:.2f}")
        except Exception as e:  # noqa: BLE001 - offline or the service busy: try again later, waiting longer each time
            fails += 1
            print(f"trailheads: {e}")
        time.sleep(min(3600, 600 * 2 ** min(fails, 3)))


_load()
