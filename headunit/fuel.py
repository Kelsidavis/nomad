"""Fuel prices and fill-ups.

Prices: the US Energy Information Administration's weekly retail averages (regular and diesel) for the state, the
region and the US, with a free API key (EIA_API_KEY in nomad.conf, from eia.gov/opendata). There's no free source of
each station's price, so the stations you've filled up at remember what you paid.

Fill-ups (from the Vehicle screen or "Truck, I put in 18 gallons at 3.89"): gallons, price, the station nearby, and
the distance counter (trip.py) at the time; between two full tanks that gives the real mpg and cost per mile.
Kept in DATA/fuel.json.
"""
import json
import os
import pathlib
import threading
import time
import urllib.parse
import urllib.request

import config
import network
import obd
import trip

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
LOG = DATA / "fuel.json"
PRICES = DATA / "fuel-prices.json"
EIA = "https://api.eia.gov/v2/petroleum/pri/gnd/data/"
# EIA's weekly series: product codes and areas (state codes exist for CA, CO, FL, MA, MN, NY, OH, TX and WA)
PRODUCTS = {"EPMR": "Regular", "EPMP": "Premium", "EPD2D": "Diesel"}
STATE_AREAS = {"CA": "SCA", "CO": "SCO", "FL": "SFL", "MA": "SMA", "MN": "SMN", "NY": "SNY", "OH": "SOH", "TX": "STX", "WA": "SWA"}
REGION_AREAS = {"WA": "R50", "OR": "R50", "CA": "R50", "NV": "R50", "AZ": "R50", "AK": "R50", "HI": "R50",
                "ID": "R40", "MT": "R40", "WY": "R40", "UT": "R40", "CO": "R40"}
AREA_NAMES = {"R50": "West Coast", "R40": "Rocky Mountain", "NUS": "US", **{v: k for k, v in STATE_AREAS.items()}}
KM_PER_MI = 1.609344
_lock = threading.Lock()


# ---- weekly averages ----
def prices(state=None, force=False):
    """{"areas": [{"area", "name", "products": {"Regular": {"price", "week", "change"}}}], "t", "error"?}, cached a
    day (EIA updates on Mondays)."""
    try:
        cached = json.loads(PRICES.read_text())
    except (OSError, ValueError):
        cached = None
    if cached and not force and time.time() - cached["t"] < 86400 and cached.get("state") == state:
        return cached
    key = config.get("EIA_API_KEY")
    if not key:
        return cached or {"areas": [], "error": "Add EIA_API_KEY to nomad.conf (free, from eia.gov/opendata) for average prices"}
    if not network.allowed("ask"):
        return {**cached, "stale": True} if cached else {"areas": [], "error": "Offline"}
    areas = [a for a in (STATE_AREAS.get(state or ""), REGION_AREAS.get(state or ""), "NUS") if a]
    q = [("api_key", key), ("frequency", "weekly"), ("data[0]", "value"), ("sort[0][column]", "period"),
         ("sort[0][direction]", "desc"), ("length", str(len(areas) * len(PRODUCTS) * 2))]
    q += [("facets[duoarea][]", a) for a in areas] + [("facets[product][]", p) for p in PRODUCTS]
    try:
        with urllib.request.urlopen(f"{EIA}?{urllib.parse.urlencode(q)}", timeout=15) as r:
            rows = json.load(r)["response"]["data"]
    except (OSError, ValueError, KeyError) as e:
        return {**cached, "stale": True} if cached else {"areas": [], "error": f"EIA: {e}"}
    out = []
    for a in areas:
        prods = {}
        for code, name in PRODUCTS.items():
            series = [x for x in rows if x["duoarea"] == a and x["product"] == code and x.get("value") is not None]
            if series:
                now = float(series[0]["value"])
                prev = float(series[1]["value"]) if len(series) > 1 else None
                prods[name] = {"price": round(now, 3), "week": series[0]["period"], "change": round(now - prev, 3) if prev else None}
        if prods:
            out.append({"area": a, "name": AREA_NAMES.get(a, a), "products": prods})
    res = {"t": time.time(), "state": state, "areas": out}
    DATA.mkdir(parents=True, exist_ok=True)
    PRICES.write_text(json.dumps(res))
    return res


# ---- fill-ups ----
def fillups():
    try:
        return json.loads(LOG.read_text())
    except (OSError, ValueError):
        return []


def _save(lst):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = LOG.with_suffix(".tmp")
    tmp.write_text(json.dumps(lst, ensure_ascii=False))
    tmp.replace(LOG)
    return lst


def add(gallons, price=None, total=None, full=True, station=None, grade="Regular"):
    """Log a fill-up. Give the price per gallon or the total (the other is worked out)."""
    gallons = float(gallons)
    if not 0 < gallons < 100:
        raise ValueError("gallons should be between 0 and 100")
    if price is None and total is None:
        raise ValueError("give the price per gallon or the total")
    price = round(float(price), 3) if price is not None else round(float(total) / gallons, 3)
    total = round(float(total), 2) if total is not None else round(gallons * price, 2)
    km, odo = trip.distance_km()
    entry = {"id": f"f{int(time.time() * 1000)}", "t": time.time(), "gallons": round(gallons, 3), "price": price, "total": total,
             "full": bool(full), "grade": grade, "km": round(km, 2), "odo_km": round(odo, 1) if odo is not None else None,
             "station": station}
    with _lock:
        return _save(fillups() + [entry]), entry


def delete(fid):
    with _lock:
        return _save([f for f in fillups() if f["id"] != fid])


def summary():
    """The log with mpg filled in (full tank to full tank), plus totals and per-station last prices."""
    lst = sorted(fillups(), key=lambda f: f["t"])
    out, last_full, gal_since = [], None, 0.0
    for f in lst:
        f = dict(f)
        gal_since += f["gallons"]
        if f["full"]:
            if last_full is not None and f["km"] > last_full["km"]:
                mi = (f["km"] - last_full["km"]) / KM_PER_MI
                f["mpg"] = round(mi / gal_since, 1) if gal_since else None
                f["miles"] = round(mi, 1)
                f["cost_per_mi"] = round(sum(x["total"] for x in lst if last_full["t"] < x["t"] <= f["t"]) / mi, 3) if mi > 1 else None
            last_full, gal_since = f, 0.0
        out.append(f)
    with_mpg = [f for f in out if f.get("mpg")]
    month = time.time() - 30 * 86400
    stations = {}
    for f in out:
        s = f.get("station")
        if s and s.get("lat") is not None:
            stations[f"{s.get('name')}|{s['lat']:.4f}|{s['lon']:.4f}"] = {**s, "price": f["price"], "t": f["t"], "grade": f["grade"]}
    return {"fillups": list(reversed(out)),
            "avg_mpg": round(sum(f["miles"] for f in with_mpg) / sum(f["miles"] / f["mpg"] for f in with_mpg), 1) if with_mpg else None,
            "spent_30d": round(sum(f["total"] for f in out if f["t"] > month), 2),
            "gallons_30d": round(sum(f["gallons"] for f in out if f["t"] > month), 1),
            "stations": list(stations.values())}


# ---- the drive ahead ----
def _number(key):
    try:
        return float(config.get(key))
    except (TypeError, ValueError):
        return None


def range_info(state=None):
    """What the tank and the mileage say about a drive, for route planning: mpg (the fill-ups' real average, else
    what the OBD adapter measured this trip), the tank's size (FUEL_TANK_GAL) and level (the OBD fuel gauge), the
    range to the reserve (FUEL_RESERVE_PCT, 10 by default) and a price per gallon (the last fill-up, else the
    state's weekly average). Whatever isn't known is None; the screen skips what it can't work out."""
    log = summary()
    o = obd.status()
    mpg, mpg_from = log["avg_mpg"], "fill-ups"
    tr = o["trip"]
    if not mpg and tr["gal"] > 0.3 and tr["km"] > 8:  # enough driving for the trip figure to mean something
        mpg, mpg_from = round(tr["km"] / KM_PER_MI / tr["gal"], 1), "OBD"
    if not mpg:
        mpg_from = None
    tank, reserve = _number("FUEL_TANK_GAL"), _number("FUEL_RESERVE_PCT")
    reserve = 10.0 if reserve is None else reserve
    gauge = o["values"].get("fuel")
    level = round(gauge["v"], 1) if gauge and time.time() - gauge["t"] < 600 else None
    gal_left = round(tank * level / 100, 2) if tank and level is not None else None
    range_mi = round(max(0.0, tank * (level - reserve) / 100) * mpg, 1) if gal_left is not None and mpg else None
    price, price_from = (log["fillups"][0]["price"], "last fill-up") if log["fillups"] else (None, None)
    if price is None:
        for a in prices(state).get("areas", []):
            reg = a["products"].get("Regular")
            if reg:
                price, price_from = reg["price"], f"{a['name']} average"
                break
    return {"mpg": mpg, "mpg_from": mpg_from, "tank_gal": tank, "level_pct": level, "gal_left": gal_left,
            "reserve_pct": reserve, "range_mi": range_mi, "price": price, "price_from": price_from}
