"""AIS: ships and boats from their VHF broadcasts (161.975 and 162.025 MHz), for the map.

rtl_ais (the rtl-ais package) tunes both channels at once and decodes the frames into NMEA sentences; this reads
them (position reports, static data with name, type, size and destination) and keeps the vessels heard in the last
hour. It is one of the SDR's time slices (sdr.py): a minute or so every five. Range is line of sight, so ships on
a river or the coast show up from tens of km away with a decent antenna and none through hills.
"""
import math
import threading
import time

import sdr

KEEP_S = 3600
_lock = threading.Lock()
_vessels = {}  # mmsi -> state
_frag = {}  # (seq id, channel) -> {"n": parts, "parts": {i: payload}}

NAV_STATUS = {0: "under way (engine)", 1: "at anchor", 2: "not under command", 3: "restricted maneuverability",
              4: "constrained by draft", 5: "moored", 6: "aground", 7: "fishing", 8: "under way (sailing)"}


def ship_type(code):
    """Words for an AIS ship-type number."""
    if code is None:
        return None
    fixed = {30: "Fishing", 31: "Towing", 32: "Towing (large)", 33: "Dredging", 34: "Diving ops", 35: "Military", 36: "Sailing",
             37: "Pleasure craft", 50: "Pilot vessel", 51: "Search and rescue", 52: "Tug", 53: "Port tender", 54: "Anti-pollution",
             55: "Law enforcement", 58: "Medical transport", 59: "Special craft"}
    if code in fixed:
        return fixed[code]
    groups = {2: "Wing in ground", 4: "High-speed craft", 6: "Passenger ship", 7: "Cargo ship", 8: "Tanker", 9: "Other"}
    return groups.get(code // 10)


# ---- decoding the 6-bit payload ----
def _bits(payload, fill=0):
    out = []
    for ch in payload:
        v = ord(ch) - 48
        if v > 40:
            v -= 8
        out.append(format(v & 63, "06b"))
    s = "".join(out)
    return s[:-fill] if fill else s


def _u(b, start, n):
    seg = b[start:start + n]
    return int(seg, 2) if len(seg) == n else None


def _i(b, start, n):
    v = _u(b, start, n)
    return v - (1 << n) if v is not None and v >= 1 << (n - 1) else v


def _text(b, start, n):
    seg = b[start:start + n]
    chars = []
    for k in range(0, len(seg) - 5, 6):
        v = int(seg[k:k + 6], 2)
        chars.append(chr(v + 64) if v < 32 else chr(v))
    return "".join(chars).replace("@", " ").strip(" @")


def _latlon(b, lon_at, lat_at, lon_bits=28, lat_bits=27):
    lon, lat = _i(b, lon_at, lon_bits), _i(b, lat_at, lat_bits)
    if lon is None or lat is None or lon == 0x6791AC0 or lat == 0x3412140:  # 181° and 91°: "not available"
        return None, None
    lon, lat = lon / 600000, lat / 600000
    return (lat, lon) if abs(lat) <= 90 and abs(lon) <= 180 else (None, None)


def decode(payload, fill=0):
    """A dict of what the message says (always 'mmsi' and 'type'), or None for messages we don't use."""
    b = _bits(payload, fill)
    t = _u(b, 0, 6)
    if t is None or len(b) < 38:
        return None
    m = {"type": t, "mmsi": _u(b, 8, 30)}
    if t in (1, 2, 3) and len(b) >= 137:
        m["status"] = _u(b, 38, 4)
        sog, cog, hdg = _u(b, 50, 10), _u(b, 116, 12), _u(b, 128, 9)
        m["sog"] = sog / 10 if sog is not None and sog < 1023 else None
        m["cog"] = cog / 10 if cog is not None and cog < 3600 else None
        m["heading"] = hdg if hdg is not None and hdg < 360 else None
        m["lat"], m["lon"] = _latlon(b, 61, 89)
    elif t in (18, 19) and len(b) >= 133:
        sog, cog, hdg = _u(b, 46, 10), _u(b, 112, 12), _u(b, 124, 9)
        m["sog"] = sog / 10 if sog is not None and sog < 1023 else None
        m["cog"] = cog / 10 if cog is not None and cog < 3600 else None
        m["heading"] = hdg if hdg is not None and hdg < 360 else None
        m["lat"], m["lon"] = _latlon(b, 57, 85)
        if t == 19 and len(b) >= 271:
            m["name"], m["shiptype"] = _text(b, 143, 120), _u(b, 263, 8)
    elif t == 5 and len(b) >= 422:
        m.update(callsign=_text(b, 70, 42), name=_text(b, 112, 120), shiptype=_u(b, 232, 8),
                 bow=_u(b, 240, 9), stern=_u(b, 249, 9), port=_u(b, 258, 6), starboard=_u(b, 264, 6),
                 draught=(_u(b, 294, 8) or 0) / 10 or None, dest=_text(b, 302, 120))
    elif t == 24 and len(b) >= 160:
        if _u(b, 38, 2) == 0:
            m["name"] = _text(b, 40, 120)
        else:
            m.update(shiptype=_u(b, 40, 8), callsign=_text(b, 90, 42), bow=_u(b, 132, 9), stern=_u(b, 141, 9),
                     port=_u(b, 150, 6), starboard=_u(b, 156, 6))
    else:
        return None
    return m


def ingest_line(line, now=None):
    """One NMEA line (!AIVDM,...): assemble multi-part messages, decode, fold into the vessel table."""
    i = line.find("!AIVDM")
    if i < 0 and "!AIVDO" not in line:
        return
    f = line[max(i, line.find("!AIVDO")):].split(",")
    if len(f) < 7:
        return
    try:
        n, k = int(f[1]), int(f[2])
    except ValueError:
        return
    payload, fill_s = f[5], f[6].split("*")[0]
    fill = int(fill_s) if fill_s.isdigit() else 0
    if n > 1:
        key = (f[3], f[4])
        entry = _frag.setdefault(key, {"n": n, "parts": {}, "t": time.time()})
        entry["parts"][k] = (payload, fill)
        if len(entry["parts"]) < n:
            return
        del _frag[key]
        payload = "".join(entry["parts"][j][0] for j in sorted(entry["parts"]))
        fill = entry["parts"][max(entry["parts"])][1]
    for stale in [x for x, e in _frag.items() if time.time() - e["t"] > 30]:
        del _frag[stale]
    m = decode(payload, fill)
    if m and m.get("mmsi"):
        ingest(m, now)


def ingest(m, now=None):
    now = now or time.time()
    with _lock:
        v = _vessels.setdefault(m["mmsi"], {"mmsi": m["mmsi"], "msgs": 0, "first": now})
        v["msgs"] += 1
        v["seen"] = now
        for k in ("name", "callsign", "dest", "bow", "stern", "port", "starboard", "draught", "shiptype"):
            if m.get(k):
                v[k] = m[k]
        if m.get("lat") is not None:
            v.update(lat=round(m["lat"], 5), lon=round(m["lon"], 5), pos_t=now)
            for k in ("sog", "cog", "heading", "status"):
                v[k] = m.get(k)


def _km(a, b, c, d):
    p = math.pi / 180
    x = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(x))


def status(ref=None):
    """The vessels heard in the last hour, nearest to ref=(lat, lon) first."""
    now = time.time()
    with _lock:
        for k in [k for k, v in _vessels.items() if now - v["seen"] > KEEP_S]:
            del _vessels[k]
        ships = []
        for v in _vessels.values():
            q = dict(v)
            q["age"] = round(now - v["seen"])
            q["pos_age"] = round(now - v["pos_t"]) if v.get("pos_t") else None
            q["type_text"] = ship_type(v.get("shiptype"))
            q["status_text"] = NAV_STATUS.get(v.get("status"))
            if v.get("bow") is not None and v.get("stern") is not None:
                q["length"] = v["bow"] + v["stern"] or None
                q["beam"] = (v.get("port") or 0) + (v.get("starboard") or 0) or None
            q["km"] = round(_km(v["lat"], v["lon"], *ref), 1) if ref and v.get("lat") is not None else None
            ships.append(q)
    ships.sort(key=lambda q: (q["km"] is None, q["km"] or 0))
    return {"ships": ships}


SLICE = sdr.Slice(
    "ais", "Ships (AIS)", ["rtl_ais"], every=300, run=75,
    pipeline=lambda: [["sh", "-c", "exec rtl_ais -n 2>&1"]],  # (the NMEA log goes to stderr)
    parse=ingest_line, detail="Boats and ships on 161.975 and 162.025 MHz, for the map")
