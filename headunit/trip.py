"""The trip computer: distance from the truck's own speed (OBD) or else the GPS's, fuel from the MAF (obd.py), an
odometer you set once from Settings, two resettable trips, and the oil-change reminder. Kept in DATA/trip.json,
saved every half minute and on every reset.

Engine warnings come from here too (overheating, charging trouble, low fuel, a new check-engine code), each said
once until it clears, as {"id", "level", "text"} for the screen to show and speak.
"""
import json
import os
import pathlib
import threading
import time
import durable

import drives
import obd

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
FILE = DATA / "trip.json"
KM_PER_MI = 1.609344
_lock = threading.Lock()


def _blank_trip():
    return {"km": 0.0, "gal": 0.0, "moving_s": 0.0, "since": time.time()}


def _load():
    d = {"odo_km": None, "total_km": 0.0, "trips": {"a": _blank_trip(), "b": _blank_trip()}, "oil_at_km": None,
         "oil_every_km": 5000 * KM_PER_MI}
    try:
        d.update(json.loads(FILE.read_text()))
    except (OSError, ValueError):
        pass
    return d


_d = _load()
_alerts = {}  # id -> alert, while its condition lasts
_dirty = [0.0]


def _save():
    DATA.mkdir(parents=True, exist_ok=True)
    durable.write_text(FILE, json.dumps(_d))


def distance_km():
    """(km counted since NOMAD started counting, the odometer in km or None). Fill-ups compare the first: it never
    jumps, even when the odometer is set in between."""
    with _lock:
        return _d.get("total_km", 0.0), _d["odo_km"]


def status():
    with _lock:
        d = json.loads(json.dumps(_d))
    for t in d["trips"].values():
        t["mpg"] = round(t["km"] / KM_PER_MI / t["gal"], 1) if t["gal"] > 0.05 else None
        t["avg_kmh"] = round(t["km"] / (t["moving_s"] / 3600), 1) if t["moving_s"] > 60 else None
    if d["odo_km"] is not None and d["oil_at_km"] is not None:
        d["oil_left_km"] = round(d["oil_at_km"] + d["oil_every_km"] - d["odo_km"], 1)
    else:
        d["oil_left_km"] = None
    d["alerts"] = list(_alerts.values())
    return d


def reset(which):
    with _lock:
        _d["trips"][which] = _blank_trip()
        _save()
    return status()


def set_odometer(km):
    with _lock:
        _d["odo_km"] = float(km)
        if _d["oil_at_km"] is None:
            _d["oil_at_km"] = float(km)  # no record of the last change: count from now
        _save()
    return status()


def oil_changed(every_km=None):
    with _lock:
        if _d["odo_km"] is None:
            raise ValueError("Set the odometer first")
        _d["oil_at_km"] = _d["odo_km"]
        if every_km:
            _d["oil_every_km"] = float(every_km)
        _save()
    return status()


def _alert(aid, on, level, text):
    if on and aid not in _alerts:
        _alerts[aid] = {"id": f"{aid}:{int(time.time())}", "level": level, "text": text}
    elif not on and aid in _alerts:
        del _alerts[aid]


def _check_engine(s):
    v = {k: x["v"] for k, x in s["values"].items() if time.time() - x["t"] < 20}
    running = v.get("rpm", 0) > 300
    _alert("hot", v.get("coolant", 0) >= 112, "bad", f"Engine is overheating: coolant {v.get('coolant', 0):.0f} °C. Pull over when it's safe and let it idle or shut it off.")
    _alert("charge", running and 0 < v.get("voltage", 13) < 12.4, "bad",
           f"Battery voltage is low with the engine running ({v.get('voltage', 0):.1f} volts): the alternator may not be charging.")
    _alert("overvolt", v.get("voltage", 0) > 15.3, "bad", f"Charging voltage is high ({v.get('voltage', 0):.1f} volts).")
    _alert("fuel", 0 < v.get("fuel", 100) < 12, "warn", f"Fuel is low, about {v.get('fuel', 0):.0f} percent. Say “find gas” for the nearest station.")
    codes = [d["code"] for d in s["dtcs"] or []]
    said = ", ".join(d["code"] + ", " + d["text"] for d in s["dtcs"] or [])
    _alert("cel:" + ",".join(codes), bool(codes), "warn", f"Check engine: {said}.")
    for k in [k for k in _alerts if k.startswith("cel:") and k != "cel:" + ",".join(codes)]:
        del _alerts[k]


def trip_loop(position):
    """position() -> the current position dict (or None); ticks once a second."""
    drives.recover()
    last = time.time()
    while True:
        time.sleep(1)
        now = time.time()
        dt, last = now - last, now
        if not 0 < dt < 5:
            continue
        s = obd.status()
        sp = s["values"].get("speed")
        p = position()
        if s["status"] == "connected" and sp and now - sp["t"] < 5:
            kmh = sp["v"]
        else:
            kmh = (p.get("speed_mps") or 0) * 3.6 if p and p.get("source") == "gps" and p.get("mode", 0) >= 2 else 0
            if kmh < 3:  # GPS wander at a stop
                kmh = 0
        gph = s["gph"] if s["status"] == "connected" and s["gph"] is not None else 0
        with _lock:
            km = kmh * dt / 3600
            for t in _d["trips"].values():
                t["km"] += km
                t["gal"] += gph * dt / 3600
                if kmh > 0:
                    t["moving_s"] += dt
            if _d["odo_km"] is not None:
                _d["odo_km"] += km
            _d["total_km"] = _d.get("total_km", 0.0) + km  # always counts (fill-ups use it, odometer set or not)
            if now - _dirty[0] > 30:
                _dirty[0] = now
                _save()
            oil_due = (_d["odo_km"] is not None and _d["oil_at_km"] is not None
                       and _d["odo_km"] - _d["oil_at_km"] >= _d["oil_every_km"])
        try:
            drives.tick(p, kmh, now)
        except OSError:
            pass  # a full or read-only card mustn't stop the trip computer
        _alert("oil", oil_due, "warn", "An oil change is due. Tap Oil changed in Settings once it's done.")
        if s["status"] == "connected":
            _check_engine(s)
        else:
            for k in [k for k in _alerts if k != "oil"]:
                del _alerts[k]
