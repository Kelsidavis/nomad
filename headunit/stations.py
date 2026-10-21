"""The radio's memory of stations: what each frequency turned out to be, and where and how well it was heard.

Everything the radio learns about a station is kept in DATA/stations.json, so it survives restarts and works with no internet:
its name (the pieces of a rotating RDS name joined, or the HD station name), the call sign, program type and RDS id, whether it
has HD Radio (and its programs), and a short list of sightings: where the truck was (GPS), when, and how strong it was (the
FM scan's dB above the noise, HD signal quality, or how much of the RDS data got through). The scan lists use it to put names on
bare frequencies; the FM tab shows the stations known around where the truck is, scanned or not, which is the useful thing on a
drive: "98.3 The Peak! was heard 15 km back".
"""
import json
import math
import os
import pathlib
import threading
import time

import hdradio
import radio
import rds

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
FILE = DATA / "stations.json"
SIGHTINGS = 12          # most recent places kept per station
SAME_PLACE_KM = 3.0     # sightings closer than this are one place
_lock = threading.Lock()
_db = {}
_saved_at = 0.0
_dirty = False


def _load():
    try:
        _db.update(json.loads(FILE.read_text()))
    except (OSError, ValueError):
        pass


def _save(force=False):
    global _saved_at, _dirty
    if not _dirty or (not force and time.time() - _saved_at < 10):
        return
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_db))
    tmp.replace(FILE)
    _saved_at, _dirty = time.time(), False


def _km(a, b, c, d):
    p = math.pi / 180
    x = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(x))


def key(band, f):
    return f"{band}:{int(round(f))}"


def _usable(name):
    """A name worth keeping: not empty, not just a frequency or 'FM'."""
    n = (name or "").strip()
    if len(n) < 3:
        return False
    return not all(ch.isdigit() or ch in ". " for ch in n.lower().replace("fm", "").replace("am", "").replace("mhz", "")) \
        and n.lower() not in ("fm", "am", "hd", "radio")


def note(band, f, pos=None, now=None, *, name=None, hd_name=None, pi=None, pty=None, callsign=None, hd=None, programs=None,
         db=None, quality=None):
    """Fold what was learned about one station into its entry. `pos` is (lat, lon) or None; db is a dB-above-noise figure
    and quality 0..1 for how clean the reception was; either becomes the strength of this sighting."""
    global _dirty
    now = now or time.time()
    k = key(band, f)
    with _lock:
        e = _db.setdefault(k, {"band": band, "f": int(round(f)), "first": now, "sightings": []})
        if _usable(name) and (not e.get("name") or (len(name) > len(e["name"]) and e["name"] in name)):
            e["name"] = name.strip()
        for field, val in (("hd_name", hd_name), ("pi", pi), ("pty", pty), ("callsign", callsign), ("programs", programs)):
            if val:
                e[field] = val
        if hd is not None:
            e["hd"] = bool(hd)
        e["seen"] = now
        strength = db if db is not None else (round(quality * 30, 1) if quality is not None else None)  # (RDS/HD quality on a rough dB scale)
        if pos and pos[0] is not None:
            same = next((s for s in e["sightings"] if _km(s["lat"], s["lon"], pos[0], pos[1]) < SAME_PLACE_KM), None)
            if same:
                same["t"] = now
                if strength is not None:
                    same["db"] = max(strength, same.get("db", -99))
            else:
                e["sightings"].append({"lat": round(pos[0], 4), "lon": round(pos[1], 4), "t": now, **({"db": strength} if strength is not None else {})})
                e["sightings"] = e["sightings"][-SIGHTINGS:]
        if strength is not None:
            e["best_db"] = max(strength, e.get("best_db", -99))
        _dirty = True
    _save()


def note_scan(band, found, pos, now=None):
    """The stations a band scan found, with their strengths and where the truck was."""
    for s in found:
        note(band, s["f"], pos, now, db=s["db"])
    _save(force=True)


def lookup(band, f):
    with _lock:
        e = _db.get(key(band, f))
        return dict(e) if e else None


def _distance(e, pos):
    """km from pos to the nearest place the station was heard, or None."""
    if not pos or not e["sightings"]:
        return None
    return min(_km(s["lat"], s["lon"], pos[0], pos[1]) for s in e["sightings"])


def _public(e, pos):
    last = max(e["sightings"], key=lambda s: s["t"]) if e["sightings"] else None
    return {"band": e["band"], "f": e["f"], "name": e.get("name") or e.get("hd_name"), "hd": e.get("hd"), "programs": e.get("programs"),
            "pty": e.get("pty"), "callsign": e.get("callsign"), "best_db": e.get("best_db"), "seen": e.get("seen"),
            "km": None if (d := _distance(e, pos)) is None else round(d, 1),
            "last": {"lat": last["lat"], "lon": last["lon"], "t": last["t"], "db": last.get("db")} if last else None}


def annotate(band, found, pos):
    """The scan's stations with what is known about each: name, HD, and how far from here it was heard before."""
    out = []
    for s in found:
        e = lookup(band, s["f"])
        out.append({**s, **({k: v for k, v in _public(e, pos).items() if k not in ("f", "band")} if e else {})})
    return out


def nearby(band, pos, radius_km=80):
    """Stations known around pos (heard within radius_km of it), strongest first."""
    with _lock:
        entries = [dict(e, sightings=list(e["sightings"])) for e in _db.values() if e["band"] == band]
    out = []
    for e in entries:
        d = _distance(e, pos)
        if d is not None and d <= radius_km and (e.get("name") or e.get("hd_name")):
            out.append(_public(e, pos))
    return sorted(out, key=lambda s: (-(s["best_db"] or 0), s["km"] or 0))


def loop(here):
    """While a station plays, keep its entry current from the RDS and HD decoders (every few seconds)."""
    while True:
        time.sleep(5)
        try:
            t = radio.tuner.tuned
            if not t or len(t["freqs"]) != 1:
                continue
            f, pos = t["freqs"][0], here()
            if t["mode"] == "wbfm":
                r = rds.status()
                if r.get("active") or r.get("ps"):
                    secs = max(1, time.time() - (r.get("started") or time.time()))
                    full = rds.status().get("ps_full")
                    note("fm", f, pos, name=full, pi=r.get("pi"), pty=r.get("prog_type"), callsign=r.get("callsign"),
                         quality=min(1.0, (r.get("groups", 0) / secs) / 11.4) if r.get("groups") else None)
            elif t["mode"] in ("hd", "hdam"):
                h = hdradio.status()
                band = "fm" if t["mode"] == "hd" else "am"
                if h.get("synced"):
                    note(band, f, pos, hd=True, hd_name=h.get("station"), name=h.get("station"),
                         programs=[{"n": s["n"], "type": s["type"]} for s in h.get("services", [])] or None,
                         db=h.get("mer"))
                elif h.get("waiting_s", 0) > 20:
                    note(band, f, pos, hd=False)
        except Exception as e:  # noqa: BLE001 - a bookkeeping error must never touch the radio
            print(f"stations loop: {e}")


_load()
