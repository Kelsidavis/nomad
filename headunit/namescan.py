"""Names for the stations a scan found but nobody has listened to: one of the SDR's time slices (sdr.py).

When the dongle would otherwise sit with ADS-B, this tunes the strongest nearby FM station that has no name yet (stations.py) for about
fifteen seconds, reads its RDS (a station sends its 8-character name every second or two, though many rotate it in pieces), saves
what it heard, and moves on to the next a minute later. A station that sends no RDS is marked so it isn't tried again for a day.
Nothing is played: this is the same rtl_fm -> redsea pipeline as the radio's, without the audio.
"""
import json
import time

import radio
import rds
import sdr
import stations

where = lambda: None  # server.py sets this to the truck's position
RUN_S = 15
cur = {}


def _target():
    pos = where()
    if not pos:
        return None
    todo = stations.unnamed("fm", pos)
    return todo[0] if todo else None


def _pipeline():
    f = _target()
    cur.clear()
    cur.update(f=f, ps=[], pi=None, pty=None, callsign=None, groups=0, started=time.time())
    return [["rtl_fm", "-M", "fm", "-s", "171000", "-A", "fast", "-f", str(f), "-r", "171000", "-"],
            ["redsea", "-r", "171000"]]


def _parse(line):
    if not line.startswith("{"):
        return
    d = json.loads(line)
    cur["groups"] += 1
    for src, dst in (("pi", "pi"), ("prog_type", "pty"), ("callsign", "callsign")):
        if d.get(src):
            cur[dst] = d[src]
    ps = (d.get("ps") or "").strip()
    if ps and ps not in cur["ps"] and len(cur["ps"]) < 8:
        cur["ps"].append(ps)


def _after(lines):
    f = cur.get("f")
    if not f:
        return
    secs = max(1.0, time.time() - cur["started"])
    name = rds.join_ps(cur["ps"])
    stations.note("fm", f, where(), name=name, pi=cur["pi"], pty=cur["pty"], callsign=cur["callsign"],
                  quality=min(1.0, cur["groups"] / secs / 11.4) if cur["groups"] else None)
    stations.mark_tried("fm", f, bool(name))


SLICE = sdr.Slice(
    "names", "Station names (RDS)", ["rtl_fm", "redsea"], every=60, run=RUN_S, pipeline=_pipeline, parse=_parse,
    when=lambda ctx: _target() is not None, after=_after,
    detail="Listens to unnamed FM stations a few seconds each to learn their names, when nothing else needs the radio")
