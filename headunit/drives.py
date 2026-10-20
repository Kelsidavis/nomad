"""Drive history: each drive's track, distance, time and where it started and ended, for the Vehicle screen and the
map (and as GPX from /api/drives/<id>?gpx=1). Kept on the Pi only, in DATA/drives/; Settings turns it off or
deletes it.

A drive starts when the truck moves and ends after five minutes stopped (or without a position). The drive in
progress is written every 30 s to current.json, so when the ignition cuts the power the next start finishes it.
"""
import json
import os
import pathlib
import threading
import time

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
DIR = DATA / "drives"
CURRENT = DIR / "current.json"
PREFS = DATA / "drives.json"
KEEP = 300
END_AFTER_S = 300
_lock = threading.Lock()
_cur = {"d": None, "saved": 0.0, "still_since": None}


def enabled():
    try:
        return json.loads(PREFS.read_text()).get("on", True)
    except (OSError, ValueError):
        return True


def set_enabled(on):
    DATA.mkdir(parents=True, exist_ok=True)
    PREFS.write_text(json.dumps({"on": bool(on)}))
    if not on:
        with _lock:
            _cur["d"] = None
            CURRENT.unlink(missing_ok=True)
    return {"on": bool(on)}


def _km(a, b, c, d):
    import math
    la1, lo1, la2, lo2 = map(math.radians, (a, b, c, d))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def _place(lat, lon):
    try:
        import offline
        t = offline.nearest_town(lat, lon)
        return t["name"] if t else None
    except Exception:  # noqa: BLE001  the search database may be missing
        return None


def _write(path, obj):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(path)


def _finish(d):
    """Close a drive: too short to matter is dropped, else it's saved with its places."""
    if not d or d["km"] < 0.3 or len(d["pts"]) < 3:
        return None
    a, b = d["pts"][0], d["pts"][-1]
    d.update(end=b[2], start_place=_place(a[0], a[1]), end_place=_place(b[0], b[1]))
    _write(DIR / f"{d['id']}.json", d)
    old = sorted(p for p in DIR.glob("2*.json"))
    for p in old[:-KEEP]:
        p.unlink(missing_ok=True)
    return d


def recover():
    """At start: a drive that was going when the power went is finished as it was."""
    try:
        d = json.loads(CURRENT.read_text())
    except (OSError, ValueError):
        return
    _finish(d)
    CURRENT.unlink(missing_ok=True)


def tick(p, kmh, now):
    """Called once a second by trip.py with the position (or None) and the speed."""
    if not enabled():
        return
    with _lock:
        d = _cur["d"]
        moving = kmh > 8
        if d is None:
            if moving and p:
                d = _cur["d"] = {"id": time.strftime("%Y%m%d-%H%M%S", time.localtime(now)), "start": now, "km": 0.0,
                                 "moving_s": 0.0, "max_kmh": 0.0, "pts": [[round(p["lat"], 5), round(p["lon"], 5), round(now), round(kmh)]]}
                _cur["still_since"] = None
            return
        if p:
            last = d["pts"][-1]
            step = _km(last[0], last[1], p["lat"], p["lon"])
            if step > 0.03 or (moving and now - last[2] > 10):
                if step < 5:  # a jump is a bad fix
                    d["km"] += step
                d["pts"].append([round(p["lat"], 5), round(p["lon"], 5), round(now), round(kmh)])
        if kmh > 0:
            d["moving_s"] += 1
            d["max_kmh"] = max(d["max_kmh"], kmh)
        if moving:
            _cur["still_since"] = None
        elif _cur["still_since"] is None:
            _cur["still_since"] = now
        if _cur["still_since"] and now - _cur["still_since"] > END_AFTER_S:
            _cur["d"] = None
            CURRENT.unlink(missing_ok=True)
            threading.Thread(target=_finish, args=(d,), daemon=True).start()
            return
        if now - _cur["saved"] > 30:
            _cur["saved"] = now
            _write(CURRENT, d)


def _summary(d):
    return {k: d.get(k) for k in ("id", "start", "end", "km", "moving_s", "max_kmh", "start_place", "end_place")}


def history(limit=50):
    out = []
    for p in sorted(DIR.glob("2*.json"), reverse=True)[:limit]:
        try:
            out.append(_summary(json.loads(p.read_text())))
        except (OSError, ValueError):
            continue
    with _lock:
        cur = _cur["d"]
    return {"on": enabled(), "drives": out, "current": _summary(cur) if cur else None}


def get(did):
    if did == "current":
        with _lock:
            return dict(_cur["d"]) if _cur["d"] else None
    p = DIR / f"{did}.json"
    if "/" in did or not p.exists():
        return None
    return json.loads(p.read_text())


def gpx(d):
    from xml.sax.saxutils import escape
    pts = "".join(f'<trkpt lat="{la}" lon="{lo}"><time>{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))}</time></trkpt>'
                  for la, lo, t, _ in d["pts"])
    places = " to ".join(x for x in (d.get("start_place"), d.get("end_place")) if x)
    name = escape(f"{places} ({d['id']})" if places else d["id"])
    return ('<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" creator="NOMAD" xmlns="http://www.topografix.com/GPX/1/1">'
            f"<trk><name>{name}</name><trkseg>{pts}</trkseg></trk></gpx>")


def delete(did=None):
    """One drive, or all of them (did None)."""
    with _lock:
        if did is None:
            for p in DIR.glob("*.json"):
                p.unlink(missing_ok=True)
            _cur["d"] = None
        elif "/" not in did:
            (DIR / f"{did}.json").unlink(missing_ok=True)
    return history()
