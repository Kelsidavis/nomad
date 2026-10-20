#!/usr/bin/env python3
"""Upcoming passes of the satellites the SDR tab can listen to, over the head unit's position.

    sat_passes.py --tle tle.txt --lat 45.52 --lon -122.68 [--alt 10] [--hours 48] [--min-el 10]

Prints JSON: [{"name", "freq_hz", "mode", "aos", "los", "max_el", "max_t", "aos_az", "los_az", "doppler_hz": [aos, max, los]}]
with times as Unix seconds. Uses sgp4 (in the head unit's ~/.venvs/sat); the TLEs come from Celestrak via radio.py.
"""
import argparse
import json
import math
import sys
import time

from sgp4.api import Satrec, jday

C = 299792.458  # km/s
EARTH_A, EARTH_F = 6378.137, 1 / 298.257223563  # WGS-84

# name in the TLE file -> downlink the SDR tab tunes to
SATS = {
    "ISS (ZARYA)": (145_800_000, "nfm", "voice, SSTV"),
    "METEOR-M2 3": (137_900_000, "lrpt", "weather images (LRPT)"),
    "METEOR-M2 4": (137_900_000, "lrpt", "weather images (LRPT)"),
    "SO-50": (436_795_000, "nfm", "FM repeater"),
}


def gmst(jd):
    t = (jd - 2451545.0) / 36525
    return math.radians((280.46061837 + 360.98564736629 * (jd - 2451545.0) + 0.000387933 * t * t) % 360)


def observer_ecef(lat, lon, alt_km):
    lat, lon = math.radians(lat), math.radians(lon)
    e2 = EARTH_F * (2 - EARTH_F)
    n = EARTH_A / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return ((n + alt_km) * math.cos(lat) * math.cos(lon), (n + alt_km) * math.cos(lat) * math.sin(lon),
            (n * (1 - e2) + alt_km) * math.sin(lat))


def look(sat, t, lat, lon, obs):
    """(azimuth deg, elevation deg, range rate km/s) of the satellite at Unix time t."""
    jd, fr = jday(*time.gmtime(t)[:6])
    fr += (t % 1) / 86400
    err, r, v = sat.sgp4(jd, fr)
    if err:
        return None
    th = gmst(jd + fr)
    c, s = math.cos(th), math.sin(th)
    # TEME -> Earth-fixed (ignoring polar motion: well under a km)
    x, y, z = c * r[0] + s * r[1], -s * r[0] + c * r[1], r[2]
    w = 7.2921150e-5  # Earth's rotation, rad/s
    vx, vy, vz = c * v[0] + s * v[1] + w * y, -s * v[0] + c * v[1] - w * x, v[2]
    dx, dy, dz = x - obs[0], y - obs[1], z - obs[2]
    la, lo = math.radians(lat), math.radians(lon)
    south = math.sin(la) * math.cos(lo) * dx + math.sin(la) * math.sin(lo) * dy - math.cos(la) * dz
    east = -math.sin(lo) * dx + math.cos(lo) * dy
    up = math.cos(la) * math.cos(lo) * dx + math.cos(la) * math.sin(lo) * dy + math.sin(la) * dz
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    az = (math.degrees(math.atan2(east, -south)) + 360) % 360
    return az, math.degrees(math.asin(up / rng)), (dx * vx + dy * vy + dz * vz) / rng


def passes(sat, name, lat, lon, alt_km, t0, hours, min_el):
    freq, mode, what = SATS[name]
    obs = observer_ecef(lat, lon, alt_km)
    out, t, step, end = [], t0, 30, t0 + hours * 3600
    prev = look(sat, t, lat, lon, obs)
    while t < end and prev:
        t += step
        cur = look(sat, t, lat, lon, obs)
        if not cur:
            break
        if prev[1] < 0 <= cur[1]:  # rising: walk to the set, keeping the peak
            aos = refine(sat, t - step, t, lat, lon, obs)
            peak, pt, tt = cur, t, t
            while True:
                tt += 10
                c = look(sat, tt, lat, lon, obs)
                if not c or c[1] < 0:
                    break
                if c[1] > peak[1]:
                    peak, pt = c, tt
            los = refine(sat, tt - 10, tt, lat, lon, obs, rising=False)
            if peak[1] >= min_el:
                a, l = look(sat, aos, lat, lon, obs), look(sat, los, lat, lon, obs)
                out.append({"name": name, "freq_hz": freq, "mode": mode, "what": what,
                            "aos": round(aos), "los": round(los), "max_el": round(peak[1]), "max_t": round(pt),
                            "aos_az": round(a[0]), "los_az": round(l[0]),
                            # the received frequency drifts by this much (falls through the pass)
                            "doppler_hz": [round(-freq * x[2] / C) for x in (a, peak, l)]})
            t = tt
            cur = look(sat, t, lat, lon, obs)
        prev = cur
    return out


def refine(sat, a, b, lat, lon, obs, rising=True):
    """The horizon crossing between a and b, to a second."""
    for _ in range(12):
        m = (a + b) / 2
        above = look(sat, m, lat, lon, obs)[1] >= 0
        if above == rising:
            b = m
        else:
            a = m
    return (a + b) / 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tle", required=True)
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--alt", type=float, default=10, help="metres")
    ap.add_argument("--hours", type=float, default=48)
    ap.add_argument("--min-el", type=float, default=10)
    args = ap.parse_args()
    lines = [l.rstrip() for l in open(args.tle) if l.strip()]
    found, seen = [], set()
    for i in range(len(lines) - 2):
        name = lines[i].strip()
        if name in SATS and name not in seen and lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
            seen.add(name)  # the ISS is in more than one Celestrak group
            sat = Satrec.twoline2rv(lines[i + 1], lines[i + 2])
            found += passes(sat, name, args.lat, args.lon, args.alt / 1000, time.time(), args.hours, args.min_el)
    json.dump(sorted(found, key=lambda p: p["aos"]), sys.stdout)


if __name__ == "__main__":
    main()
