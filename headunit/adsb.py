"""ADS-B: aircraft overhead from their 1090 MHz broadcasts, with the same SDR as the radio. rtl_adsb (in the rtl-sdr
package) demodulates the frames; this decodes them (identity, position, altitude, speed and heading) and keeps the
aircraft heard in the last minute for the map and the Radio screen.

The SDR does one thing at a time: ADS-B runs while the map's Aircraft layer or the Radio screen's Aircraft tab is
open (or always, from that tab) and the radio isn't tuned or capturing a satellite; listening wins and ADS-B waits.
Range depends on the antenna: the Nooelec's telescopic whip pulled out to about 6.9 cm (a quarter wave) and
mounted high, or a 1090 MHz antenna, hears planes 50-150 km away; the truck's AM/FM antenna hears little.
"""
import math
import shutil
import subprocess
import threading
import time

import radio

CHARS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ##### ###############0123456789######"
NZ = 15
KEEP_S = 60
_lock = threading.Lock()
_planes = {}  # icao -> state
_st = {"proc": None, "wanted_until": 0.0, "always": False, "frames": 0, "good": 0, "started": None, "error": None}
_ref = {"pos": None}  # (lat, lon) to decode positions against
_ctl = threading.Lock()  # starting and stopping rtl_adsb, so a satellite capture can never find it half-started


# ---- decoding (Mode S extended squitter, DF17/18) ----
def crc_ok(bits):
    """The 24-bit parity of a 112-bit frame is zero when it arrived intact."""
    gen = 0x1FFF409
    data = bits
    for i in range(88):
        if data & (1 << (111 - i)):
            data ^= gen << (111 - i - 24)
    return data & 0xFFFFFF == 0


def _nl(lat):
    """Longitude zones at a latitude (the CPR NL function)."""
    if lat == 0:
        return 59
    if abs(lat) == 87:
        return 2
    if abs(lat) > 87:
        return 1
    return math.floor(2 * math.pi / math.acos(1 - (1 - math.cos(math.pi / (2 * NZ))) / math.cos(math.pi / 180 * lat) ** 2))


def cpr_local(f, lat_cpr, lon_cpr, ref_lat, ref_lon):
    """An airborne position from one message, given a reference within ~300 km (the truck)."""
    yz, xz = lat_cpr / 131072, lon_cpr / 131072
    dlat = 360 / (4 * NZ - f)
    j = math.floor(ref_lat / dlat) + math.floor(0.5 + (ref_lat % dlat) / dlat - yz)
    lat = dlat * (j + yz)
    nl = _nl(lat) - f
    dlon = 360 / nl if nl > 0 else 360
    m = math.floor(ref_lon / dlon) + math.floor(0.5 + (ref_lon % dlon) / dlon - xz)
    return lat, dlon * (m + xz)


def _alt(code12):
    if not code12:
        return None
    if code12 & 0x10:  # Q bit: 25 ft steps
        n = ((code12 & 0xFE0) >> 1) | (code12 & 0x0F)
        return n * 25 - 1000
    return None  # Gillham-coded (old transponders, rare in ADS-B)


def decode(hexmsg):
    """One frame (28 hex digits) -> {"icao", ...fields} or None."""
    if len(hexmsg) != 28:
        return None
    try:
        bits = int(hexmsg, 16)
    except ValueError:
        return None
    df = bits >> 107
    if df not in (17, 18) or not crc_ok(bits):
        return None
    icao = f"{(bits >> 80) & 0xFFFFFF:06X}"
    me = (bits >> 24) & ((1 << 56) - 1)
    tc = me >> 51
    get = lambda start, n: (me >> (56 - start - n)) & ((1 << n) - 1)  # noqa: E731  ME bits [start, start+n)
    out = {"icao": icao, "tc": tc}
    if 1 <= tc <= 4:
        call = "".join(CHARS[get(8 + 6 * i, 6)] for i in range(8)).replace("#", "").strip()
        out["call"] = call or None
        out["cat"] = (tc, get(5, 3))
    elif 9 <= tc <= 18:
        out["alt_ft"] = _alt(get(8, 12))
        out["cpr"] = (get(21, 1), get(22, 17), get(39, 17))
    elif tc == 19:
        st = get(5, 3)
        if st in (1, 2):
            k = 4 if st == 2 else 1  # supersonic subtype counts in 4-knot steps
            dew, vew, dns, vns = get(13, 1), get(14, 10), get(24, 1), get(25, 10)
            if vew and vns:
                vx = (vew - 1) * k * (-1 if dew else 1)
                vy = (vns - 1) * k * (-1 if dns else 1)
                out["kt"] = round(math.hypot(vx, vy))
                out["track"] = round(math.degrees(math.atan2(vx, vy)) % 360, 1)
        svr, vr = get(36, 1), get(37, 9)
        if vr:
            out["vr"] = (vr - 1) * 64 * (-1 if svr else 1)
    elif tc == 28 and get(5, 3) == 1:  # emergency / squawk status
        out["squawk"] = _squawk(get(11, 13))
        out["emergency"] = get(8, 3) or None
    return out


def _squawk(id13):
    """The 4-digit squawk from a 13-bit identity code (C1 A1 C2 A2 C4 A4 X B1 D1 B2 D2 B4 D4)."""
    b = [(id13 >> (12 - i)) & 1 for i in range(13)]
    c1, a1, c2, a2, c4, a4, _x, b1, d1, b2, d2, b4, d4 = b
    a, bb, c, d = a4 * 4 + a2 * 2 + a1, b4 * 4 + b2 * 2 + b1, c4 * 4 + c2 * 2 + c1, d4 * 4 + d2 * 2 + d1
    return f"{a}{bb}{c}{d}"


def _km(a, b, c, d):
    la1, lo1, la2, lo2 = map(math.radians, (a, b, c, d))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def ingest(hexmsg, now=None):
    """Fold one frame into the aircraft table."""
    now = now or time.time()
    _st["frames"] += 1
    m = decode(hexmsg)
    if not m:
        return
    _st["good"] += 1
    with _lock:
        p = _planes.setdefault(m["icao"], {"icao": m["icao"], "msgs": 0, "first": now})
        p["msgs"] += 1
        p["seen"] = now
        for k in ("call", "alt_ft", "kt", "track", "vr", "squawk", "emergency"):
            if m.get(k) is not None:
                p[k] = m[k]
        if "cpr" in m and _ref["pos"]:
            f, la, lo = m["cpr"]
            lat, lon = cpr_local(f, la, lo, *_ref["pos"])
            # a local decode is only right within ~300 km; anything further, or a jump, is noise
            if _km(lat, lon, *_ref["pos"]) < 400 and (not p.get("lat") or now - p.get("pos_t", 0) > 30
                                                      or _km(lat, lon, p["lat"], p["lon"]) < 10):
                p.update(lat=round(lat, 5), lon=round(lon, 5), pos_t=now)
                trail = p.setdefault("trail", [])
                if not trail or _km(lat, lon, trail[-1][0], trail[-1][1]) > 0.5:
                    trail.append([round(lat, 4), round(lon, 4)])
                    del trail[:-40]


# ---- the receiver ----
def _reader(proc):
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("*") and line.endswith(";"):
            ingest(line[1:-1].upper())
    _st["proc"] = None


def _start():
    if _st["proc"]:
        return
    if not shutil.which("rtl_adsb"):
        _st["error"] = "rtl_adsb isn't installed (sudo apt install rtl-sdr)"
        return
    if not radio.sdr_present():
        _st["error"] = "SDR not connected"
        return
    proc = subprocess.Popen(["rtl_adsb"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    _st.update(proc=proc, started=time.time(), error=None)
    threading.Thread(target=_reader, args=(proc,), daemon=True).start()


def stop():
    """Let go of the SDR (the radio wants it)."""
    with _ctl:
        _stop()


def _stop():
    p = _st["proc"]
    if p:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
    _st["proc"] = None


def want(seconds=20):
    """The screen is showing aircraft: keep receiving for a while."""
    _st["wanted_until"] = time.time() + seconds


def set_always(on):
    _st["always"] = bool(on)


def _busy():
    return radio.tuner.tuned or radio.capture.busy() or time.time() < radio.SDR_HOLD[0]


def adsb_loop(where):
    """where() -> (lat, lon) of the truck, the reference for positions. ADS-B is the lowest priority on the SDR: the
    tuner and above all a satellite capture take it whenever they want it (they call stop() first), and ADS-B only
    comes back once they're done. The busy check is made again under the lock that starts it, so a capture that
    begins just as this loop decides to start can't end up sharing the dongle with rtl_adsb."""
    radio.SDR_USERS.append(stop)
    while True:
        _ref["pos"] = where() or _ref["pos"]
        wanted = _st["always"] or time.time() < _st["wanted_until"]
        with _ctl:
            if wanted and not _busy():
                _start()
            elif _st["proc"] and (not wanted or _busy()):
                _stop()
        with _lock:
            for k in [k for k, p in _planes.items() if time.time() - p["seen"] > KEEP_S * 5]:
                del _planes[k]
        time.sleep(2)


def status():
    now, ref = time.time(), _ref["pos"]
    with _lock:
        planes = []
        for p in _planes.values():
            if now - p["seen"] > KEEP_S:
                continue
            q = {k: v for k, v in p.items() if k != "trail"}
            q["age"] = round(now - p["seen"], 1)
            q["pos_age"] = round(now - p["pos_t"], 1) if p.get("pos_t") else None
            q["km"] = round(_km(p["lat"], p["lon"], *ref), 1) if ref and p.get("lat") is not None else None
            q["trail"] = p.get("trail", [])[-20:]
            planes.append(q)
    planes.sort(key=lambda q: (q["km"] is None, q["km"] or 0))
    busy = radio.tuner.tuned or radio.capture.busy()
    return {"running": bool(_st["proc"]), "always": _st["always"], "busy": bool(busy), "error": _st["error"],
            "sdr": radio.sdr_present(), "frames": _st["frames"], "good": _st["good"], "aircraft": planes}
