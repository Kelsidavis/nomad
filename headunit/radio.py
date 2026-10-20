"""The radio tab: channel lists, the SDR tuner, FM band scan, satellite passes and the NWS forecast.

The SDR is a Nooelec NESDR SMArt v5 (RTL2832U + R820T2; AM through direct sampling). The tuner runs
rtl_fm -> ffmpeg -> MP3 and server.py streams that to the page's <audio>, so radio goes through the same player,
volume and visualizer as music. One stream at a time: the dongle has one tuner.
"""
import calendar
import json
import os
import re
import pathlib
import shutil
import statistics
import subprocess
import threading
import time
import urllib.request

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
TLE = DATA / "tle.txt"
FORECAST = DATA / "forecast.json"
SAT_PYTHON = pathlib.Path.home() / ".venvs/sat/bin/python"  # sgp4 lives here (see README)
SAT_PASSES = pathlib.Path(__file__).resolve().parent / "tools/sat_passes.py"
SSTV_DECODE = pathlib.Path(__file__).resolve().parent / "tools/sstv_decode.py"
CAPTURES = DATA / "sat"          # one folder per captured pass: meta.json plus images and audio
SETTINGS = DATA / "radio.json"   # {"auto": "off" | "idle" | "always"}
AUTO_MIN_EL = {"lrpt": 20, "nfm": 15}  # lower passes are too short and weak to be worth the SDR
USER_AGENT = "pi-headunit/0.1 (personal car head unit)"
RTL_IDS = {("0bda", "2838"), ("0bda", "2832")}  # RTL2832U dongles

# rtl_fm arguments per mode; everything is resampled to 48 kHz mono for ffmpeg
MODES = {
    "wbfm": ["-M", "wbfm"],                             # broadcast FM (with de-emphasis)
    "am": ["-M", "am", "-s", "24000", "-E", "direct2"],  # broadcast AM: direct sampling, Q branch (below 28.8 MHz)
    "nfm": ["-M", "fm", "-s", "12500"],                 # NOAA weather, public safety, marine, ham
    "air": ["-M", "am", "-s", "12500"],                 # aviation is AM
}

def _ch(group, name, mhz, mode, note=""):
    return {"group": group, "name": name, "f": round(mhz * 1e6), "mode": mode, "note": note}

# Channels that are the same everywhere in the US. County dispatch varies by place (and is often P25 digital,
# which this simple FM chain can't decode); add those from RadioReference as presets.
CHANNELS = [
    *[_ch("weather", f"NOAA WX{i + 1}", 162.400 + i * 0.025, "nfm", "NOAA Weather Radio") for i in range(7)],
    _ch("ems", "VMED28", 155.3400, "nfm", "ambulance to hospital"),
    _ch("ems", "VMED29", 155.3475, "nfm", "ambulance to hospital"),
    *[_ch("ems", f"MED-{i + 1}", 463.000 + i * 0.025, "nfm", "EMS (UHF MED)") for i in range(8)],
    _ch("ems", "VFIRE21", 154.2800, "nfm", "fire mutual aid"),
    _ch("ems", "VFIRE22", 154.2650, "nfm", "fire mutual aid"),
    _ch("ems", "VFIRE23", 154.2950, "nfm", "fire mutual aid"),
    _ch("ems", "VCALL10", 155.7525, "nfm", "interoperability calling"),
    _ch("ems", "VTAC11", 151.1375, "nfm", "interoperability"),
    _ch("marine", "Marine 16", 156.800, "nfm", "distress and calling"),
    _ch("marine", "Marine 13", 156.650, "nfm", "bridge to bridge (river traffic)"),
    _ch("marine", "Marine 22A", 157.100, "nfm", "Coast Guard"),
    _ch("marine", "Marine 9", 156.450, "nfm", "boater calling"),
    _ch("air", "Air emergency", 121.500, "air", "guard"),
    _ch("air", "Unicom", 122.800, "air", "small airports"),
    _ch("ham", "2 m calling", 146.520, "nfm", "ham simplex"),
    _ch("ham", "70 cm calling", 446.000, "nfm", "ham simplex"),
    _ch("ham", "GMRS 1", 462.5625, "nfm", "FRS/GMRS"),
    _ch("ham", "ISS voice", 145.800, "nfm", "only during a pass"),
]
GROUPS = {"weather": "Weather radio", "ems": "EMS & fire", "marine": "Marine", "air": "Air", "ham": "Ham & GMRS"}


# Other programs that use the dongle in the background (adsb.stop): they let go before anything here tunes it, and
# stay off it until SDR_HOLD has passed (a scan or the start of a capture isn't "tuned" yet).
SDR_USERS = []
SDR_HOLD = [0.0]


def release_sdr(hold_s=10):
    SDR_HOLD[0] = max(SDR_HOLD[0], time.time() + hold_s)
    for stop in SDR_USERS:
        stop()


def sdr_present():
    for d in pathlib.Path("/sys/bus/usb/devices").glob("*"):
        try:
            if ((d / "idVendor").read_text().strip(), (d / "idProduct").read_text().strip()) in RTL_IDS:
                return True
        except OSError:
            continue
    return False


def status():
    return {"sdr": sdr_present(), "rtl_fm": bool(shutil.which("rtl_fm")), "tuned": tuner.tuned,
            "channels": CHANNELS, "groups": GROUPS, "modes": list(MODES)}


class Tuner:
    def __init__(self):
        self.lock = threading.Lock()
        self.procs = ()
        self.tuned = None
        self.gen = 0

    def start(self, freqs, mode, squelch=0, label=""):
        """Tune and return (generation, mp3 stream). More than one frequency scans them (needs squelch).
        Listening wins over an automatic satellite capture: one running is stopped."""
        capture.abort()
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if not sdr_present():
            raise OSError("SDR not connected: plug in the Nooelec v5")
        if not shutil.which("rtl_fm"):
            raise OSError("rtl-sdr isn't installed (sudo apt install rtl-sdr)")
        freqs = [int(f) for f in freqs][:32]
        release_sdr()
        cmd = ["rtl_fm", *MODES[mode], *[a for f in freqs for a in ("-f", str(f))], "-r", "48000",
               "-l", str(int(squelch or (60 if len(freqs) > 1 else 0))), "-"]
        enc = ["ffmpeg", "-v", "error", "-f", "s16le", "-ar", "48000", "-ac", "1", "-i", "-",
               "-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "-"]
        with self.lock:
            self._stop()
            rtl = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            mp3 = subprocess.Popen(enc, stdin=rtl.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            rtl.stdout.close()
            self.procs = (rtl, mp3)
            self.gen += 1
            self.tuned = {"freqs": freqs, "mode": mode, "label": label, "since": time.time()}
            return self.gen, mp3.stdout

    def stop(self, gen=None):
        with self.lock:
            if gen is None or gen == self.gen:  # a stream that was already replaced leaves the new one alone
                self._stop()

    def _stop(self):
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
        self.procs = ()
        self.tuned = None


tuner = Tuner()


def fm_scan():
    """Stations on the FM band, strongest first: one rtl_power sweep (stops the tuner: one dongle)."""
    if not sdr_present():
        raise OSError("SDR not connected: plug in the Nooelec v5")
    tuner.stop()
    release_sdr(hold_s=75)
    out = subprocess.run(["rtl_power", "-f", "87.8M:108M:25k", "-i", "2", "-1", "-g", "30"],
                         capture_output=True, text=True, timeout=60).stdout
    power = {}
    for line in out.splitlines():  # date, time, low Hz, high Hz, step Hz, samples, dB, dB, ...
        parts = [p.strip() for p in line.split(",")]
        lo, step = float(parts[2]), float(parts[4])
        for i, db in enumerate(parts[6:]):
            power[lo + i * step] = float(db)
    if not power:
        return []
    floor = statistics.median(power.values())
    found = []
    for k in range(101):  # US FM channels: 87.9 to 107.9 MHz, every 200 kHz
        f = 87.9e6 + k * 200e3
        near = [db for hz, db in power.items() if abs(hz - f) <= 60e3]
        if near and max(near) > floor + 8:
            found.append({"f": round(f), "db": round(max(near) - floor, 1)})
    return sorted(found, key=lambda s: -s["db"])


# ---- satellites ----
_passes = {"t": 0, "key": None, "list": []}


def refresh_tle(max_age_s=2 * 86400):
    """Celestrak's current orbits for the satellites the tab knows, when online and older than two days."""
    if TLE.exists() and time.time() - TLE.stat().st_mtime < max_age_s:
        return
    try:
        text = ""
        for group in ("stations", "weather", "amateur"):
            req = urllib.request.Request(f"https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle",
                                         headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as r:
                text += r.read().decode()
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = TLE.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(TLE)
    except OSError:
        pass  # offline: the old orbits stay good for a week or two


def passes(lat, lon, hours=48):
    """Upcoming passes over (lat, lon), recomputed every 20 minutes or after moving ~20 km."""
    key = (round(lat, 1), round(lon, 1))
    if time.time() - _passes["t"] < 1200 and _passes["key"] == key:
        return _passes["list"]
    refresh_tle()
    if not TLE.exists() or not SAT_PYTHON.exists():
        return []
    out = subprocess.run([str(SAT_PYTHON), str(SAT_PASSES), "--tle", str(TLE), "--lat", str(lat), "--lon", str(lon),
                          "--hours", str(hours)], capture_output=True, text=True, timeout=60)
    try:
        found = json.loads(out.stdout)
    except ValueError:
        return _passes["list"]
    _passes.update(t=time.time(), key=key, list=found)
    return found


def tle_age_days():
    return round((time.time() - TLE.stat().st_mtime) / 86400, 1) if TLE.exists() else None


# ---- weather (National Weather Service; kept for when there's no internet) ----
WX_CACHE = pathlib.Path(os.environ.get("HEADUNIT_CACHE", pathlib.Path.home() / ".cache/headunit")) / "wx"
RADAR = ("https://mapservices.weather.noaa.gov/eventdriven/rest/services/radar/radar_base_reflectivity/MapServer/export"
         "?bbox={},{},{},{}&bboxSR=3857&imageSR=3857&size=256,256&format=png32&transparent=true&f=image")
STAR = "https://cdn.star.nesdis.noaa.gov"
# NOAA STAR's GOES images: the latest at full size, and the folder of timestamped frames (a frame every 5 minutes
# for sectors, 10 for full disk) that the animations are built from, at a smaller size.
GOES = {
    "pnw": {"dir": "GOES18/ABI/SECTOR/pnw/GEOCOLOR", "latest": "1200x1200.jpg", "frame": "600x600.jpg", "keep": 24},
    "pnw-ir": {"dir": "GOES18/ABI/SECTOR/pnw/13", "latest": "1200x1200.jpg", "frame": "600x600.jpg", "keep": 24},
    "west": {"dir": "GOES18/ABI/CONUS/GEOCOLOR", "latest": "1250x750.jpg", "frame": "625x375.jpg", "keep": 24},
    "fd": {"dir": "GOES18/ABI/FD/GEOCOLOR", "latest": "1808x1808.jpg", "frame": "678x678.jpg", "keep": 24},
    "fd-east": {"dir": "GOES19/ABI/FD/GEOCOLOR", "latest": "1808x1808.jpg", "frame": "678x678.jpg", "keep": 24},
}
FRAME_NAME = re.compile(r'href="(\d{11})_[^"]+-(\d+x\d+\.jpg)"')  # 20262930951_GOES18-ABI-pnw-GEOCOLOR-600x600.jpg
EMPTY_PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b494441"
                          "54789c6360000200000500017a5eab3f0000000049454e44ae426082")  # 1x1, transparent


def _cached(path, url, max_age_s):
    """(bytes, age in seconds) from the cache when fresh, else from url; a stale copy when offline, else None."""
    try:
        age = time.time() - path.stat().st_mtime
        if age < max_age_s:
            return path.read_bytes(), age
    except OSError:
        age = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = r.read()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{threading.get_ident()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return data, 0
    except OSError:
        return (path.read_bytes(), age) if age is not None else None


def radar_tile(z, x, y, offline=False):
    """NEXRAD base reflectivity (NOAA's RIDGE 1 km mosaic) as a 256 px web-mercator tile, refreshed every 4 min."""
    if not (0 <= z <= 10 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise ValueError("radar tiles go to zoom 10")
    half = 20037508.342789244
    span = 2 * half / 2 ** z
    x0, y1 = -half + x * span, half - y * span
    got = _cached(WX_CACHE / "radar" / str(z) / str(x) / f"{y}.png", RADAR.format(x0, y1 - span, x0 + span, y1),
                  10 ** 9 if offline else 240)
    return got[0] if got else EMPTY_PNG


def goes_image(view, offline=False):
    """(jpeg, age in seconds) of the latest GOES image for a view in GOES, or None. offline: only what's cached."""
    if view not in GOES:
        raise ValueError(f"view must be one of {', '.join(GOES)}")
    v = GOES[view]
    return _cached(WX_CACHE / f"goes-{view}.jpg", f"{STAR}/{v['dir']}/{v['latest']}", 10 ** 9 if offline else 600)


def update_animation(view):
    """Download the newest frames of a view (what's not here yet) and drop the ones beyond the last v["keep"]."""
    v = GOES[view]
    folder = WX_CACHE / "anim" / view
    req = urllib.request.Request(f"{STAR}/{v['dir']}/", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        listing = r.read().decode(errors="replace")
    stamps = sorted({m.group(1) for m in FRAME_NAME.finditer(listing) if m.group(2) == v["frame"]})[-v["keep"]:]
    folder.mkdir(parents=True, exist_ok=True)
    have = {f.stem for f in folder.glob("*.jpg")}
    for stamp in stamps:
        if stamp in have:
            continue
        name = next(m.group(0)[6:-1] for m in FRAME_NAME.finditer(listing) if m.group(1) == stamp and m.group(2) == v["frame"])
        _cached(folder / f"{stamp}.jpg", f"{STAR}/{v['dir']}/{name}", 10 ** 9)
    for f in folder.glob("*.jpg"):
        if f.stem not in stamps:
            f.unlink(missing_ok=True)


def goes_frames(view):
    """The animation frames on hand, oldest first: [{"stamp", "t"}] (t: Unix time, from the UTC stamp YYYYDDDHHMM)."""
    if view not in GOES:
        raise ValueError(f"view must be one of {', '.join(GOES)}")
    out = []
    for f in sorted((WX_CACHE / "anim" / view).glob("*.jpg")):
        s = f.stem
        t = calendar.timegm(time.strptime(s, "%Y%j%H%M"))
        out.append({"stamp": s, "t": t})
    return out


def goes_frame(view, stamp):
    if view not in GOES or not re.fullmatch(r"\d{11}", stamp or ""):
        raise ValueError("bad frame")
    return WX_CACHE / "anim" / view / f"{stamp}.jpg"


def _nws(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def forecast(lat, lon, max_age_s=1800):
    """The NWS forecast and active alerts for (lat, lon), refreshed every 30 minutes when online."""
    try:
        cached = json.loads(FORECAST.read_text())
    except (OSError, ValueError):
        cached = None
    fresh = cached and time.time() - cached["t"] < max_age_s and abs(cached["lat"] - lat) < 0.3 and abs(cached["lon"] - lon) < 0.3
    if fresh:
        return cached
    try:
        point = _nws(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")["properties"]
        periods = _nws(point["forecast"])["properties"]["periods"]
        alerts = _nws(f"https://api.weather.gov/alerts/active?point={lat:.4f},{lon:.4f}")["features"]
        try:  # the next 24 hours, for the Weather screen's chart
            hourly = [{"t": h["startTime"], "temp": h.get("temperature"), "rain": (h.get("probabilityOfPrecipitation") or {}).get("value"),
                       "sky": h.get("shortForecast")} for h in _nws(point["forecastHourly"], timeout=10)["properties"]["periods"][:24]]
        except (OSError, KeyError, ValueError):
            hourly = []
        loc = point.get("relativeLocation", {}).get("properties", {})
        out = {"t": time.time(), "lat": lat, "lon": lon, "place": f"{loc.get('city', '')}, {loc.get('state', '')}".strip(", "),
               "periods": [{k: p.get(k) for k in ("name", "temperature", "temperatureUnit", "windSpeed", "windDirection",
                                                    "shortForecast", "isDaytime")} |
                           {"rain": (p.get("probabilityOfPrecipitation") or {}).get("value")} for p in periods[:8]],
               "alerts": [{k: a["properties"].get(k) for k in ("event", "headline", "severity", "ends")} for a in alerts[:5]],
               "hourly": hourly}
        DATA.mkdir(parents=True, exist_ok=True)
        FORECAST.write_text(json.dumps(out))
        return out
    except (OSError, KeyError, ValueError):
        return {**cached, "stale": True} if cached else None


# ---- weather on the way: alerts at points along a route, and the forecast where and when it arrives ----
_route_cache = {}  # (lat, lon) to 0.1° -> (time, alerts)


def _alerts_at(lat, lon):
    key = (round(lat, 1), round(lon, 1))
    hit = _route_cache.get(key)
    if hit and time.time() - hit[0] < 900:
        return hit[1]
    feats = _nws(f"https://api.weather.gov/alerts/active?point={lat:.3f},{lon:.3f}", timeout=8)["features"]
    out = [{"id": f["properties"].get("id") or f.get("id"), "event": f["properties"].get("event"),
            "headline": f["properties"].get("headline"), "severity": f["properties"].get("severity"),
            "area": f["properties"].get("areaDesc"), "ends": f["properties"].get("ends") or f["properties"].get("expires")}
           for f in feats[:6]]
    _route_cache[key] = (time.time(), out)
    return out


def _arrival(lat, lon, arrive_ts):
    """The hourly forecast at (lat, lon) for the hour of arrive_ts, or None."""
    from datetime import datetime
    try:
        point = _nws(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}", timeout=8)["properties"]
        for per in _nws(point["forecastHourly"], timeout=8)["properties"]["periods"][:48]:
            start = datetime.fromisoformat(per["startTime"]).timestamp()
            end = datetime.fromisoformat(per["endTime"]).timestamp()
            if start <= arrive_ts < end or start > arrive_ts:
                return {"temp": per.get("temperature"), "unit": per.get("temperatureUnit"), "sky": per.get("shortForecast"),
                        "rain": (per.get("probabilityOfPrecipitation") or {}).get("value"), "wind": per.get("windSpeed"),
                        "place": point.get("relativeLocation", {}).get("properties", {}).get("city")}
    except (OSError, KeyError, ValueError, IndexError):
        pass
    return None


def route_weather(points, arrive_ts):
    """points: [[lat, lon, km_along], ...] along the route (the last is the destination). Returns {"alerts": [...]
    (each once, with where along the route it starts), "arrival": the hourly forecast at the destination at
    arrive_ts, or None}. Needs the internet; a point that fails is skipped. NWS can take several seconds for a point
    it hasn't been asked about lately, so everything is asked at once."""
    from concurrent.futures import ThreadPoolExecutor

    def one(pt):
        try:
            return pt[2], _alerts_at(pt[0], pt[1])
        except (OSError, KeyError, ValueError):
            return pt[2], []
    alerts, seen = [], set()
    with ThreadPoolExecutor(8) as ex:
        arrival = ex.submit(_arrival, points[-1][0], points[-1][1], arrive_ts) if points else None
        for km, found in ex.map(one, points[:12]):
            for a in found:
                if a["id"] not in seen:
                    seen.add(a["id"])
                    alerts.append({**a, "km": round(km)})
        return {"alerts": alerts, "arrival": arrival.result() if arrival else None}


# ---- automatic satellite captures ----
def radio_settings():
    try:
        return {"auto": "idle", **json.loads(SETTINGS.read_text())}
    except (OSError, ValueError):
        return {"auto": "idle"}


def save_settings(auto=None):
    cur = radio_settings()
    if auto not in (None, "off", "idle", "always"):
        raise ValueError("auto must be off, idle or always")
    if auto:
        cur["auto"] = auto
    DATA.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(cur))
    return cur


def will_capture(p):
    return p["max_el"] >= AUTO_MIN_EL.get(p["mode"], 90)


class Capture:
    """Records one pass with the SDR, then decodes it: Meteor LRPT with SatDump, FM audio with the SSTV decoder."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"state": "idle"}
        self.proc = None
        self.stopping = threading.Event()
        self.done = set()  # (name, aos) already captured or skipped

    def busy(self):
        return self.state["state"] != "idle"

    def abort(self):
        if self.state["state"] == "capturing":
            self.stopping.set()
            with self.lock:
                if self.proc:
                    self.proc.terminate()

    def run(self, p):
        name, aos, los = p["name"], p["aos"], p["los"]
        folder = CAPTURES / f"{time.strftime('%Y%m%d-%H%M', time.localtime(aos))}-{re.sub(r'[^A-Za-z0-9]+', '-', name).strip('-')}"
        folder.mkdir(parents=True, exist_ok=True)
        meta = {**p, "id": folder.name, "status": "capturing", "files": [], "note": ""}
        write_meta(folder, meta)
        self.stopping.clear()
        self.state = {"state": "capturing", "pass": p, "id": folder.name}
        tuner.stop()
        release_sdr()
        try:
            secs = max(10, los + 20 - time.time())
            if p["mode"] == "lrpt":
                raw = folder / "baseband.cu8"
                self._record(["rtl_sdr", "-f", str(p["freq_hz"]), "-s", "288000", "-g", "40",
                              "-n", str(int(288000 * secs)), str(raw)], secs)
            else:
                raw = folder / "audio.wav"
                rtl = subprocess.Popen(["rtl_fm", "-M", "fm", "-f", str(p["freq_hz"]), "-s", "24000", "-r", "48000", "-"],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self._record(["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", "48000", "-ac", "1", "-i", "-",
                              "-t", str(int(secs)), str(raw)], secs, stdin=rtl.stdout, also=rtl)
            if self.stopping.is_set():
                meta.update(status="stopped", note="stopped for the radio")
                raw.unlink(missing_ok=True)
                return write_meta(folder, meta)
            self.state = {"state": "decoding", "pass": p, "id": folder.name}
            meta["status"] = "decoding"
            write_meta(folder, meta)
            meta.update(self._decode(p, folder, raw))
        except (OSError, subprocess.SubprocessError) as e:
            meta.update(status="failed", note=str(e)[:300])
        finally:
            self.state = {"state": "idle"}
        write_meta(folder, meta)

    def _record(self, cmd, secs, stdin=None, also=None):
        with self.lock:
            self.proc = subprocess.Popen(cmd, stdin=stdin, stderr=subprocess.DEVNULL)
        if stdin:
            stdin.close()
        try:
            self.proc.wait(timeout=secs + 30)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
        for pr in (self.proc, also):
            if pr and pr.poll() is None:
                pr.terminate()
                pr.wait(timeout=5)
        with self.lock:
            self.proc = None

    def _decode(self, p, folder, raw):
        # Low priority on two cores: decoding is heavy and the car supply has little headroom.
        slow = ["nice", "-n", "19", "taskset", "-c", "2,3"]
        if p["mode"] == "lrpt":
            out = subprocess.run([*slow, "satdump", "meteor_m2-x_lrpt", "baseband", str(raw), str(folder / "products"),
                                  "--samplerate", "288000", "--baseband_format", "cu8"],
                                 capture_output=True, text=True, timeout=3600)
            images = sorted(str(f.relative_to(folder)) for f in folder.glob("products/**/*.png"))
            if images:
                raw.unlink(missing_ok=True)
                return {"status": "done", "files": images, "preview": pick_preview(images)}
            return {"status": "no signal", "note": (out.stdout + out.stderr)[-300:].strip()}
        out = subprocess.run([*slow, str(SAT_PYTHON), str(SSTV_DECODE), str(raw), str(folder)],
                             capture_output=True, text=True, timeout=1800)
        try:
            images = json.loads(out.stdout or "[]")
        except ValueError:
            images = []
        clip = folder / "audio.opus"  # keep the sound, small
        subprocess.run([*slow, "ffmpeg", "-v", "error", "-y", "-i", str(raw), "-c:a", "libopus", "-b:a", "24k", str(clip)],
                       timeout=600, check=False)
        if clip.exists():
            raw.unlink(missing_ok=True)
        files = [*images, *(["audio.opus"] if clip.exists() else [])]
        return {"status": "done", "files": files, "preview": images[0] if images else ""}


def pick_preview(images):
    """SatDump writes a dozen products; the false-color composite is the one to show."""
    for want in ("221", "rgb", "MSU-MR-1"):
        for f in images:
            if want.lower() in f.lower() and "map" not in f.lower():
                return f
    return images[0]


def write_meta(folder, meta):
    (folder / "meta.json").write_text(json.dumps(meta))


capture = Capture()


def captures(limit=30):
    out = []
    for m in sorted(CAPTURES.glob("*/meta.json"), reverse=True)[:limit]:
        try:
            out.append(json.loads(m.read_text()))
        except (OSError, ValueError):
            continue
    return out


def capture_file(cid, name):
    root = CAPTURES.resolve()
    f = (root / cid / name).resolve()
    if root not in f.parents or not f.is_file():
        raise ValueError("bad capture file")
    return f


def prune_raw():
    """Recordings of passes that didn't decode are kept for a look, but only the latest one (~0.5 GB each)."""
    for raw in sorted(CAPTURES.glob("*/baseband.cu8"))[:-1]:
        raw.unlink(missing_ok=True)


def capture_loop(where):
    """Every 20 s: if a pass worth capturing is starting, record it (setting: off, idle = when the radio's free,
    always). where() gives (lat, lon)."""
    while True:
        try:
            auto = radio_settings()["auto"]
            if auto != "off" and not capture.busy() and sdr_present() and shutil.which("rtl_sdr"):
                now, loc = time.time(), where()
                for p in passes(*loc) if loc else []:
                    key = (p["name"], p["aos"])
                    if key in capture.done or not will_capture(p) or not (p["aos"] - 40 <= now < p["los"] - 90):
                        continue
                    capture.done.add(key)
                    if tuner.tuned and auto == "idle":
                        continue  # someone's listening
                    prune_raw()
                    threading.Thread(target=capture.run, args=(p,), daemon=True).start()
                    break
        except Exception as e:  # noqa: BLE001 - keep the scheduler alive whatever one pass does
            print(f"capture loop: {e}")
        time.sleep(20)


def weather_loop(where):
    """Keep the weather ready for when there's no signal: every 10 minutes, the GOES-18 pictures, the forecast and
    alerts (for where() = (lat, lon)) and the satellite orbits, whenever there's a connection. Each fetch is skipped
    while its copy is still fresh, and a failure (offline) just leaves the last copy."""
    import network  # (here: radio.py is also used on its own by tools)
    while True:
        if network.allowed("background"):  # the pictures are a few MB an hour: not on a metered connection
            for view in GOES:
                goes_image(view)
                try:
                    update_animation(view)
                except (OSError, ValueError, StopIteration):
                    pass  # offline: the frames on hand still play
        if network.allowed("ask"):  # the forecast and orbits are a few KB
            loc = where()
            if loc:
                forecast(*loc)
            refresh_tle()
        time.sleep(600)
