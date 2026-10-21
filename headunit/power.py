"""Low-power mode and the power log, the head unit's side of powerguard.py.

powerguard (root, sees the supply) tells this process when the voltage dips: POST /api/power {"shed": minutes}. While shedding, the SDR
listeners and ADS-B are paused (sdr.pause) so the dongle and its USB power stop drawing; "shed": 0 ends it. status() also gives the
recent power events from DATA/power.log for Settings -> System.
"""
import json
import os
import pathlib
import time

import radio
import sdr

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
LOG = DATA / "power.log"
_until = [0.0]


def shed(minutes):
    """Pause (minutes > 0) or resume (0) the extra radio work; the dongle is let go of at once."""
    _until[0] = time.time() + float(minutes) * 60 if minutes else 0.0
    sdr.PAUSED[0] = _until[0]
    if minutes:
        sdr.abort()
        radio.release_sdr(0)  # stops ADS-B too


def shedding():
    return time.time() < _until[0]


def events(limit=30):
    try:
        lines = LOG.read_text().splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return [e for e in out if e.get("type") not in ("start", "stop")] or out[:5]


def status():
    ev = events(60)
    day = time.time() - 86400
    return {"shedding": shedding(), "until": _until[0] or None,
            "events": ev[:12], "dips_24h": sum(1 for e in ev if e.get("type") == "undervoltage" and e.get("t", 0) > day),
            "lost_24h": sum(1 for e in ev if e.get("type") == "unclean-shutdown" and e.get("t", 0) > day)}
