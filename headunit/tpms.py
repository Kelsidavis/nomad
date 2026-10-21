"""Tire pressure from the truck's own TPMS sensors, with rtl_433 (the rtl-433 package) on 315 MHz (433.92 in Europe).

The sensors only transmit while the wheels turn (about every minute), so this is one of the SDR's time slices
(sdr.py) that runs only while the truck is moving. Every sensor heard is kept; the ones that belong to this truck
are told apart from the cars passing by with the wheel they are assigned to (Vehicle screen: tap a sensor, pick a
wheel), and a sensor heard again and again over many minutes is flagged as probably yours. A tire under TPMS_LOW_PSI
(28 by default; set it to your door-jamb pressure minus about 20%) counts as low. Kept in DATA/tpms.json.
"""
import json
import os
import pathlib
import threading
import time

import config
import sdr

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
FILE = DATA / "tpms.json"
WHEELS = ("FL", "FR", "RL", "RR")
# rtl_433's TPMS decoders (rtl_433 -R help): without this list it would listen for every device it knows
PROTOCOLS = (59, 60, 82, 88, 89, 90, 95, 110, 123, 140, 156, 168, 180, 186, 201, 203, 208, 212, 225, 226, 241, 248, 252, 257, 275)
_lock = threading.Lock()
_sensors = {}  # "model:id" -> state
_assigned = {}  # "model:id" -> "FL"..."RR"


def _load():
    try:
        saved = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return
    _assigned.update(saved.get("assigned", {}))
    for k, v in saved.get("sensors", {}).items():
        _sensors[k] = v


def _save():
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"assigned": _assigned, "sensors": _sensors}))
    tmp.replace(FILE)


def low_psi():
    try:
        return float(config.get("TPMS_LOW_PSI") or 28)
    except ValueError:
        return 28.0


def ingest_line(line, now=None):
    """One JSON line from rtl_433; only TPMS readings with a pressure are used."""
    if not line.startswith("{"):
        return
    d = json.loads(line)
    if "pressure_kPa" in d:
        psi = d["pressure_kPa"] * 0.1450377
    elif "pressure_PSI" in d:
        psi = d["pressure_PSI"]
    elif "pressure_bar" in d:
        psi = d["pressure_bar"] * 14.50377
    else:
        return
    now = now or time.time()
    key = f"{d.get('model', 'TPMS')}:{d.get('id', '?')}"
    with _lock:
        s = _sensors.setdefault(key, {"key": key, "model": d.get("model", "TPMS"), "id": str(d.get("id", "?")), "first": now, "heard": 0})
        s.update(psi=round(psi, 1), seen=now, rssi=d.get("rssi"))
        if "temperature_C" in d:
            s["temp_c"] = round(d["temperature_C"], 1)
        elif "temperature_F" in d:
            s["temp_c"] = round((d["temperature_F"] - 32) * 5 / 9, 1)
        if s.get("last_listen") != SLICE.listens:  # counts once per listen, however many times it repeats
            s["last_listen"] = SLICE.listens
            s["heard"] += 1
        _save()


def assign(key, wheel):
    """Say which wheel a sensor is on (wheel None or "" forgets it). One sensor per wheel."""
    if wheel and wheel not in WHEELS:
        raise ValueError(f"wheel must be one of {', '.join(WHEELS)}")
    with _lock:
        for k in [k for k, w in _assigned.items() if w == wheel]:
            del _assigned[k]
        if wheel:
            _assigned[key] = wheel
        else:
            _assigned.pop(key, None)
        _save()
    return status()


def forget(key):
    with _lock:
        _sensors.pop(key, None)
        _assigned.pop(key, None)
        _save()
    return status()


def status():
    now, low = time.time(), low_psi()
    with _lock:
        sensors = []
        for k, s in _sensors.items():
            q = dict(s)
            q["wheel"] = _assigned.get(k)
            q["age"] = round(now - s["seen"])
            # heard on at least three separate listens over ten minutes or more: it travels with the truck
            q["probably_yours"] = s["heard"] >= 3 and s["seen"] - s["first"] > 600
            sensors.append(q)
    sensors.sort(key=lambda q: (q["wheel"] is None, -q["heard"], q["age"]))
    tires = {q["wheel"]: {"psi": q["psi"], "temp_c": q.get("temp_c"), "age": q["age"], "low": q["psi"] < low}
             for q in sensors if q["wheel"]}
    return {"tires": tires, "sensors": sensors, "low_psi": low, "wheels": list(WHEELS),
            "alerts": [f"{w} {t['psi']:.0f} psi" for w, t in tires.items() if t["low"] and t["age"] < 1800]}


def _pipeline():
    freq = config.get("TPMS_FREQ") or "315M"
    return [["rtl_433", "-f", freq, "-s", "250k", "-F", "json", "-M", "level", "-T", str(SLICE.run),
             *[a for p in PROTOCOLS for a in ("-R", str(p))]]]


_load()
SLICE = sdr.Slice(
    "tpms", "Tire pressure (TPMS)", ["rtl_433"], every=240, run=90, pipeline=_pipeline, parse=ingest_line,
    when=lambda ctx: ctx.get("moving"), detail="The truck's tire sensors on 315 MHz, while it is moving")
