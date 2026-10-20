#!/usr/bin/env python3
"""Head unit backend: serves the touch UI and bridges it to gpsd, the phone (DashLink), the HAT mic
and the online map services. Python stdlib only.

    server.py                         # http://127.0.0.1:8800, shown by Chromium in kiosk mode
    HEADUNIT_BIND=0.0.0.0 server.py   # reachable from other machines; it drives the mic and
                                      # dashcam, so only do this on a trusted network

Offline data (see offline.py) comes first: the street map, aerial imagery, place search and routing
from ~/.local/share/headunit. Without it, map tiles come from OpenStreetMap (cached in
~/.cache/headunit/tiles), search from Photon/Nominatim and routing from OSRM, which need internet.
HEADUNIT_OFFLINE=1 never goes online (to test the offline data).
"""
import json
import math
import mimetypes
import os
import pathlib
import queue
import re
import socket
import subprocess
import tempfile
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
STATIC = HERE / "static"
for d in (os.environ.get("DASHLINK_DIR", "/opt/dashlink"), HERE.parent / "pi"):
    if (pathlib.Path(d) / "dashlink.py").exists():
        sys.path.insert(0, str(d))
        break
from dashlink import DashLink  # noqa: E402
import voice  # noqa: E402
import offline  # noqa: E402
import intent  # noqa: E402
import radio  # noqa: E402
import config  # noqa: E402
import bt  # noqa: E402
import emergency  # noqa: E402
import network  # noqa: E402
import music  # noqa: E402
import news  # noqa: E402
import obd  # noqa: E402
import trip  # noqa: E402
import adsb  # noqa: E402
import fuel  # noqa: E402

PORT = int(os.environ.get("HEADUNIT_PORT", 8800))
BIND = os.environ.get("HEADUNIT_BIND", "127.0.0.1")
CACHE = pathlib.Path(os.environ.get("HEADUNIT_CACHE", pathlib.Path.home() / ".cache/headunit"))
MUSIC_DIRS = [pathlib.Path.home() / "Music", pathlib.Path("/mnt/phone/share/music")]
VIDEO_DIRS = [pathlib.Path.home() / "Videos", pathlib.Path("/mnt/phone/share/videos")]
MEDIA_DIRS = MUSIC_DIRS + VIDEO_DIRS  # a media id is "<index in MEDIA_DIRS>/<path under it>"
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav"}
VIDEO_EXT = {".mp4", ".m4v", ".webm", ".mkv", ".mov"}
COVERS = ("cover.jpg", "folder.jpg", "cover.png", "front.jpg")
mimetypes.add_type("audio/ogg", ".opus")
mimetypes.add_type("video/webm", ".mkv")  # Chromium plays Matroska under this type; video/x-matroska it refuses
USER_AGENT = "pi-headunit/0.1 (personal car head unit)"
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_MAX_AGE_S = 30 * 86400
class _Offline:  # truthy when online fallbacks aren't allowed: HEADUNIT_OFFLINE=1, or Settings → Data use (network.py)
    def __bool__(self):
        return not network.allowed("ask")


OFFLINE_ONLY = _Offline()
SIM = os.environ.get("HEADUNIT_SIM") == "1"  # testing indoors: POST /api/sim sets a fake position
HOME = config.home()  # for satellite passes and weather with no GPS fix and no home set in Settings
HOME_FILE = pathlib.Path.home() / ".local/share/headunit/home.json"  # the home the driver set (POST /api/home)

phone = DashLink(timeout=3)
# Speech runs on the Pi (stt.service, tts.service); the phone is optional (cameras, dashcam, sensors).
STT = os.environ.get("HEADUNIT_STT", "http://127.0.0.1:8178")
TTS = os.environ.get("HEADUNIT_TTS", "http://127.0.0.1:5002")


def gforce_from_gps(prev, cur, last):
    """[lateral, longitudinal] g from two fixes, about a second apart: speed change for braking and accelerating,
    speed x turn rate for cornering (right and forward positive). Smoothed, since GPS speed is noisy."""
    try:
        dt = cur["recv"] - prev["recv"]
        v0, v1 = prev["speed"], cur["speed"]
    except (TypeError, KeyError):
        return None
    if not 0.3 < dt < 3:
        return last
    lon = (v1 - v0) / dt / 9.81
    lat = 0.0
    if v1 > 2 and prev.get("track") is not None and cur.get("track") is not None:  # heading is noise when slow
        turn = (cur["track"] - prev["track"] + 540) % 360 - 180
        lat = v1 * math.radians(turn) / dt / 9.81
    g = [max(-1.5, min(1.5, lat)), max(-1.5, min(1.5, lon))]
    return [round(0.5 * a + 0.5 * b, 3) for a, b in zip(g, last)] if last else g


class State:
    """Latest readings from every source; the UI gets a snapshot twice a second."""

    def __init__(self):
        self.lock = threading.Lock()
        self.tpv = None          # gpsd fix, with "recv" = our receive time
        self.sky = {}
        self.speaking = False    # a spoken prompt is playing (the page ducks the music)
        self.online = None       # internet reachable, checked every minute (net_loop)
        self.gforce = None       # [lateral, longitudinal] in g, from successive GPS fixes (gforce_from_gps)
        self.gpsd_up = False
        self.phone_all = None    # DashLink /all, with "recv"
        self.phone_status = None
        self.dashcam = None
        self.phone_online = False
        self.system = {}
        self.sim = None

    def set(self, **kw):
        with self.lock:
            self.__dict__.update(kw)

    def position(self, now):
        """Best current position: the USB GPS when it has a fix, else the phone's."""
        if self.sim:
            return {**self.sim, "source": "gps", "mode": 3, "accuracy_m": 5, "age_s": 0}
        tpv = self.tpv
        if tpv and tpv.get("mode", 0) >= 2 and now - tpv["recv"] < 3:
            return {"source": "gps", "lat": tpv["lat"], "lon": tpv["lon"],
                    "speed_mps": tpv.get("speed"), "track_deg": tpv.get("track"),
                    "alt_m": tpv.get("altMSL", tpv.get("alt")), "mode": tpv["mode"],
                    "accuracy_m": tpv.get("eph") or tpv.get("epx"), "age_s": round(now - tpv["recv"], 1)}
        loc = (self.phone_all or {}).get("location") or {}
        if loc.get("fix") and loc.get("age_ms", 1e9) < 10000 and now - self.phone_all["recv"] < 5:
            return {"source": "phone", "lat": loc["lat"], "lon": loc["lon"],
                    "speed_mps": loc.get("speed_mps"), "track_deg": loc.get("bearing_deg"),
                    "alt_m": loc.get("altitude_m"), "mode": 3 if loc.get("altitude_m") is not None else 2,
                    "accuracy_m": loc.get("accuracy_m"), "age_s": round(loc["age_ms"] / 1000, 1)}
        return None

    def snapshot(self):
        now = time.time()
        with self.lock:
            sensors = (self.phone_all or {}).get("sensors") or {}
            sky = self.sky
            return {
                "t": now,
                "speaking": self.speaking,
                "online": self.online,
                "data": network.data_use()["effective"],
                "quake_alerts": [{k: q[k] for k in ("id", "mag", "place", "km", "tsunami", "time")} for q in emergency.alerts()[:3]],
                "position": self.position(now),
                "gps": {"gpsd": self.gpsd_up, "mode": (self.tpv or {}).get("mode", 0),
                        "sats_used": sky.get("uSat"), "sats_seen": sky.get("nSat"),
                        # [azimuth, elevation, signal dB-Hz, used] for each satellite with a known position
                        "sky": [[x["az"], x["el"], x.get("ss") or 0, bool(x.get("used"))]
                                for x in sky.get("satellites") or [] if x.get("az") is not None and x.get("el") is not None],
                        "g": self.gforce if self.tpv and now - self.tpv["recv"] < 3 else None},
                "phone": {"online": self.phone_online, "status": self.phone_status,
                          "orientation": sensors.get("orientation"),
                          "linear_accel": (sensors.get("linear_accel") or {}).get("values"),
                          "baro_altitude_m": sensors.get("baro_altitude_m"),
                          "location": (self.phone_all or {}).get("location")},
                "dashcam": self.dashcam,
                "system": self.system,
                "obd": _obd_brief(),
                "mesh": _mesh["brief"],
                "alerts": trip.status()["alerts"],
                "road": _road["v"] if now - _road["t"] < 15 else None,
            }


def _obd_brief():
    """The engine values for the live state (the full picture, with codes, is /api/obd)."""
    s = obd.status()
    return {"status": s["status"], "mil": s["mil"], "codes": len(s["dtcs"] or []), "mpg": s["mpg"], "gph": s["gph"],
            "trip_mpg": round(s["trip"]["km"] * 0.621371 / s["trip"]["gal"], 1) if s["trip"]["gal"] > 0.02 else None,
            "v": {k: v["v"] for k, v in s["values"].items() if time.time() - v["t"] < 15}}


# LoRa: mesh_bridge.py (mesh.service) holds the Meshtastic radio; this keeps its brief for the live state.
MESH = "http://127.0.0.1:8805"
_mesh = {"brief": None}


def mesh_call(path, body=None, timeout=4):
    req = urllib.request.Request(MESH + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise OSError(json.load(e).get("error") or str(e)) from None


# The road under the truck and its speed limit (offline.road_at), looked up every couple of seconds while moving.
_road = {"v": None, "t": 0}


def road_loop():
    last, last_t = None, 0
    while True:
        time.sleep(2)
        now = time.time()
        p = state.position(now)
        if not p:
            continue
        moving = (p.get("speed_mps") or 0) > 2
        if last and now - last_t < 20 and offline._km(last[0], last[1], p["lat"], p["lon"]) < (0.015 if moving else 0.05):
            _road["t"] = now  # still on it
            continue
        try:
            _road["v"] = offline.road_at(p["lat"], p["lon"], p.get("track_deg") if moving else None)
            _road["t"], last, last_t = now, (p["lat"], p["lon"]), now
        except (OSError, ValueError):
            pass  # routing not running


def mesh_loop():
    while True:
        try:
            _mesh["brief"] = mesh_call("/brief", timeout=3)
        except (OSError, ValueError):
            _mesh["brief"] = None  # mesh.service isn't running
        time.sleep(4)


def _state_code():
    """The state the forecast is for ("Longview, WA" -> "WA"), for the regional fuel prices."""
    try:
        return json.loads(radio.FORECAST.read_text())["place"].rsplit(", ", 1)[1][:2]
    except (OSError, ValueError, KeyError, IndexError):
        return None


def _with_engine(context):
    """The page's context line plus what the engine says, for the voice model."""
    e = obd.summary()
    return f"{context}; {e}" if e and context else e or context


state = State()


def gps_loop():
    """Follow gpsd's JSON stream; reconnect whenever it goes away."""
    while True:
        try:
            with socket.create_connection(("127.0.0.1", 2947), timeout=10) as s:
                s.sendall(b'?WATCH={"enable":true,"json":true}\n')
                state.set(gpsd_up=True)
                for line in s.makefile("r", encoding="utf-8", errors="replace"):
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("class") == "TPV":
                        msg["recv"] = time.time()
                        state.set(tpv=msg, gforce=gforce_from_gps(state.tpv, msg, state.gforce))
                    elif msg.get("class") == "SKY":
                        # gpsd sends a SKY every fix with only uSat (from GGA); the satellite list comes only
                        # when the receiver sends GSV, every few seconds. Keep the last full count in between.
                        sats = msg.get("satellites")
                        if sats:
                            msg.setdefault("nSat", len(sats))
                            msg.setdefault("uSat", sum(1 for x in sats if x.get("used")))
                        else:
                            msg.setdefault("nSat", state.sky.get("nSat"))
                            msg["satellites"] = state.sky.get("satellites") or []  # for the sky plot
                        state.set(sky=msg)
        except OSError:
            pass
        state.set(gpsd_up=False)
        time.sleep(3)


def phone_loop():
    """Poll the phone: sensors + location at 2 Hz, battery and dashcam every 5 s."""
    last_slow = 0
    while True:
        try:
            frame = phone.all()
            frame["recv"] = time.time()
            state.set(phone_all=frame, phone_online=True)
            if time.time() - last_slow > 5:
                last_slow = time.time()
                state.set(phone_status=phone.status(), dashcam=phone.dashcam())
            time.sleep(0.5)
        except (OSError, ValueError):
            state.set(phone_online=False)
            time.sleep(3)


def system_loop():
    while True:
        info = {}
        try:
            out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3).stdout
            bits = int(out.strip().split("=")[1], 16)
            info["undervolt_now"] = bool(bits & 0x1)
            info["throttled_now"] = bool(bits & 0x6)
            info["undervolt_since_boot"] = bool(bits & 0x10000)
        except (OSError, IndexError, ValueError, subprocess.SubprocessError):
            pass
        try:
            info["cpu_temp_c"] = int(pathlib.Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
        except (OSError, ValueError):
            pass
        info["load"] = os.getloadavg()[0]
        try:
            mem = dict(line.split(":", 1) for line in pathlib.Path("/proc/meminfo").read_text().splitlines())
            info["mem_available_mb"] = int(mem["MemAvailable"].split()[0]) // 1024
        except (OSError, KeyError, ValueError):
            pass
        state.set(system=info)
        time.sleep(5)


# --- voice -------------------------------------------------------------------------------------

mic_lock = threading.Lock()


def record_command(wait_s=5.0, max_s=10.0, silence_s=0.8, on_phase=lambda phase: None):
    """Record one utterance from the HAT: wait for speech, stop after a pause. on_phase("speech") fires when the
    driver starts talking."""
    mic = voice.open_mic(voice.find_mic())
    try:
        read = lambda: mic.stdout.read(voice.CHUNK)  # noqa: E731
        # 0.3 s of room noise sets the threshold, so road noise doesn't count as speech.
        threshold = max(300, int(sorted(voice.rms(read()) for _ in range(6))[3] * 3))
        preroll, frames, quiet = [], [], 0
        for _ in range(int(wait_s * 20)):
            chunk = read()
            if voice.rms(chunk) > threshold:
                frames = preroll + [chunk]
                on_phase("speech")
                break
            preroll = (preroll + [chunk])[-6:]
        else:
            return b""
        while len(frames) < max_s * 20:
            chunk = read()
            if not chunk:
                break
            frames.append(chunk)
            quiet = quiet + 1 if voice.rms(chunk) < threshold else 0
            if quiet >= silence_s * 20:
                break
        return b"".join(frames)
    finally:
        mic.terminate()


speech = queue.Queue()


def speech_loop():
    """Speak prompts and replies one at a time: Piper on the Pi (tts.service), played through PipeWire with pw-play
    (plain aplay reaches only the raw HDMI device: pipewire-alsa isn't installed). While it plays, state.speaking
    tells the page to duck the music."""
    while True:
        text, voice = speech.get()
        try:
            req = urllib.request.Request(f"{TTS}/tts", data=json.dumps({"text": text, "voice": voice}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                wav = r.read()
            with tempfile.NamedTemporaryFile(suffix=".wav") as f:
                f.write(wav)
                f.flush()
                state.set(speaking=True)
                time.sleep(0.35)  # the page has a moment to bring the music down first
                subprocess.run(["pw-play", f.name], check=True, timeout=120)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            print(f"say failed: {e}", file=sys.stderr)
        finally:
            state.set(speaking=False)


def transcribe(wav, prompt=""):
    """Speech to text on the Pi (whisper.cpp server, stt.service). Returns {"ok", "text"}."""
    boundary = f"headunit{time.time_ns()}"
    parts = [("response_format", b"json"), ("temperature", b"0"), ("prompt", prompt.encode())]
    body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n'.encode() + v + b"\r\n"
                    for k, v in parts)
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
             f"Content-Type: audio/wav\r\n\r\n").encode() + wav + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{STT}/inference", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = json.load(r).get("text", "")
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"speech to text not running: {e}"}
    # whisper marks non-speech as [BLANK_AUDIO], (wind), etc.
    text = " ".join(w for w in text.replace("[BLANK_AUDIO]", "").split() if not w.startswith(("(", "["))).strip()
    return {"ok": True, "text": text}


# --- web services ------------------------------------------------------------------------------

def fetch(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


tile_slots = threading.Semaphore(4)  # be polite to the OSM tile servers


def tile(z, x, y):
    """Cached OSM tile. Serves a stale copy when offline; None when we have nothing."""
    path = CACHE / "tiles" / str(z) / str(x) / f"{y}.png"
    if path.exists() and (OFFLINE_ONLY or time.time() - path.stat().st_mtime < TILE_MAX_AGE_S):
        return path.read_bytes()
    if OFFLINE_ONLY:
        return None
    try:
        with tile_slots:
            data = fetch(TILE_URL.format(z=z, x=x, y=y), timeout=8)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")  # tiles load in parallel
        tmp.write_bytes(data)
        tmp.replace(path)
        return data
    except (OSError, urllib.error.URLError):
        return path.read_bytes() if path.exists() else None


search_lock = threading.Lock()
last_search = [0.0]


def search(q, lat=None, lon=None):
    """Offline index first; online only when it has nothing (or isn't installed)."""
    results = offline.search(q, lat, lon)
    if results or OFFLINE_ONLY:
        return results or []
    return online_search(q, lat, lon)


def online_search(q, lat=None, lon=None):
    """Place search near us: Photon (ranks by distance, good with "pizza"-style queries), with
    Nominatim as the fallback. Both are free OSM services, so keep to about one request a second."""
    with search_lock:
        time.sleep(max(0.0, 1.0 - (time.time() - last_search[0])))
        last_search[0] = time.time()
        try:
            return photon(q, lat, lon)
        except (OSError, ValueError, KeyError, urllib.error.URLError):
            time.sleep(1)
            return nominatim(q, lat, lon)


def photon(q, lat, lon):
    params = {"q": q, "limit": 10, "lang": "en"}
    if lat is not None:
        params.update(lat=lat, lon=lon)
    out = []
    for f in json.loads(fetch("https://photon.komoot.io/api/?" + urllib.parse.urlencode(params)))["features"]:
        pr = f["properties"]
        street = " ".join(x for x in (pr.get("housenumber"), pr.get("street")) if x)
        name = pr.get("name") or street or pr.get("city") or q
        address = ", ".join(x for x in (street if street != name else None, pr.get("city") or pr.get("county"),
                                        pr.get("state")) if x)
        out.append({"name": name, "address": address,
                    "lat": f["geometry"]["coordinates"][1], "lon": f["geometry"]["coordinates"][0]})
    return out


def nominatim(q, lat, lon):
    params = {"q": q, "format": "jsonv2", "limit": 10}
    if lat is not None:  # prefer (not limit to) about 50 km around us
        params["viewbox"] = f"{lon - 0.6},{lat + 0.45},{lon + 0.6},{lat - 0.45}"
    out = []
    for r in json.loads(fetch("https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params))):
        parts = r.get("display_name", "").split(", ")
        out.append({"name": r.get("name") or parts[0], "address": ", ".join(parts[1:4]),
                    "lat": float(r["lat"]), "lon": float(r["lon"])})
    if lat is not None:
        out.sort(key=lambda o: (o["lat"] - lat) ** 2 + ((o["lon"] - lon) * math.cos(math.radians(lat))) ** 2)
    return out


def route(frm, to, units="km"):
    """Local Valhalla first; the online OSRM demo server when it isn't running."""
    try:
        return offline.route(frm, to, units)
    except (OSError, urllib.error.URLError, KeyError, ValueError) as e:
        if OFFLINE_ONLY:
            raise ValueError(f"offline routing failed: {e}")
    return online_route(frm, to)


def online_route(frm, to):
    """Driving route from OSRM, with spoken-style instructions per step."""
    url = (f"https://router.project-osrm.org/route/v1/driving/{frm[1]},{frm[0]};{to[1]},{to[0]}"
           "?overview=full&geometries=geojson&steps=true")
    data = json.loads(fetch(url, timeout=15))
    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError(data.get("message") or data.get("code") or "no route")
    r = data["routes"][0]
    steps = []
    for leg in r["legs"]:
        for s in leg["steps"]:
            m = s["maneuver"]
            steps.append({"lat": m["location"][1], "lon": m["location"][0], "type": m["type"],
                          "modifier": m.get("modifier", ""), "name": s.get("name") or s.get("ref") or "",
                          "distance": s["distance"], "duration": s["duration"],
                          "instruction": instruction(m, s)})
    return {"distance": r["distance"], "duration": r["duration"],
            "coords": [[c[1], c[0]] for c in r["geometry"]["coordinates"]], "steps": steps, "source": "online"}


def instruction(m, step):
    kind, mod = m["type"], m.get("modifier", "")
    name = step.get("name") or step.get("ref") or ""
    onto = f" onto {name}" if name else ""
    if kind == "depart":
        return f"Head {compass_word(m.get('bearing_after', 0))}" + (f" on {name}" if name else "")
    if kind == "arrive":
        return "Arrive at your destination" + (f", on the {mod}" if mod in ("left", "right") else "")
    if kind in ("roundabout", "rotary", "roundabout turn"):
        n = m.get("exit")
        return f"At the roundabout, take the {ordinal(n)} exit{onto}" if n else f"Enter the roundabout{onto}"
    if kind in ("new name", "continue"):
        return f"Continue{onto}" if mod in ("straight", "") else f"Keep {mod}{onto}"
    if kind == "merge":
        return f"Merge{onto}"
    if kind == "on ramp":
        side = mod.replace("slight ", "").replace("sharp ", "")
        return f"Take the ramp on the {side}{onto}" if side in ("left", "right") else f"Take the ramp{onto}"
    if kind == "off ramp":
        return f"Take the exit{onto}"
    if kind == "fork":
        return f"Keep {mod.replace('slight ', '')} at the fork{onto}"
    if kind == "end of road":
        return f"At the end of the road, turn {mod}{onto}"
    if mod == "uturn":
        return f"Make a U-turn{onto}"
    if mod == "straight":
        return f"Continue straight{onto}"
    return f"Turn {mod}{onto}" if mod else f"Continue{onto}"


def compass_word(deg):
    return ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"][
        int((deg + 22.5) % 360 // 45)]


def ordinal(n):
    return {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}.get(n, f"{n}th")


# --- media -------------------------------------------------------------------------------------

library = {"t": 0, "tracks": [], "videos": [], "tags": {"artists": {}, "albums": {}}}
library_lock = threading.Lock()
TRACK_NO = re.compile(r"^(?:(\d)[-.])?(\d{1,3})(?:\s*[-._]\s*|\s+)")  # "01 - Title", "1-03 Title", "07. Title"
NOISE = re.compile(r"\s*\[[^\]]*\]|\s*\((?:[^)]*(?:flac|mp3|bit|khz|kbps|web|cd ?rip|discography)[^)]*)\)", re.I)
YEAR = re.compile(r"^\(?((?:19|20)\d\d)\)?\s*(?:-\s*)?")


def clean_folder(name, artist=""):
    """'(2021) Elvis Presley - Back in Nashville [24Bit-96kHz]' -> ('Back in Nashville', 2021): for files that
    library.json (from plex_music.py) doesn't cover."""
    name = NOISE.sub("", name).strip()
    name = re.split(r"\s+-?\s*discography\b", name, flags=re.I)[0].strip(" -") or name
    year = 0
    if m := YEAR.match(name):
        year, name = int(m.group(1)), name[m.end():] or name
    if artist and name.lower().startswith(artist.lower() + " - "):
        name = name[len(artist) + 3:]
    return name.strip(), year


def terrain_online(z, x, y):
    """Elevation outside the downloaded area (or before it's downloaded): AWS Terrain Tiles, kept in the cache."""
    if OFFLINE_ONLY or not (0 <= z <= 15 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        return None
    p = CACHE / "terrain" / str(z) / str(x) / f"{y}.png"
    try:
        return p.read_bytes()
    except OSError:
        pass
    try:
        data = fetch(offline.TERRAIN_URL.format(z=z, x=x, y=y), timeout=15)
    except (OSError, urllib.error.URLError):
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(p)
    return data


def net_loop():
    """Is there internet? A HEAD to the weather service every minute (it's used anyway); for the rail's status light."""
    while True:
        if not network.allowed("ask"):  # Data use: Offline means not even this
            state.set(online=None)
            time.sleep(60)
            continue
        try:
            req = urllib.request.Request("https://api.weather.gov/", method="HEAD", headers={"User-Agent": USER_AGENT})
            urllib.request.urlopen(req, timeout=8).close()
            state.set(online=True)
        except urllib.error.HTTPError:
            state.set(online=True)  # it answered
        except (OSError, urllib.error.URLError):
            state.set(online=False)
        time.sleep(60)


def here():
    """(lat, lon) for satellite passes and weather: the GPS fix, else home; None when neither is known."""
    p = state.position(time.time())
    if p:
        return p["lat"], p["lon"]
    try:
        h = json.loads(HOME_FILE.read_text())
        return h["lat"], h["lon"]
    except (OSError, ValueError, KeyError):
        return HOME


def scan_library():
    """Music and videos from the folder layout (Artist/Album/01 - Title.opus, as plex_music.py writes it).
    No tag reading: a walk of 25k files takes about a second, so it can simply run again."""
    tracks, videos = [], []
    tags = {"artists": {}, "albums": {}}  # Plex's moods, styles, genres and similar artists, from library.json
    for root_i, root in enumerate(MEDIA_DIRS):
        is_music = root in MUSIC_DIRS
        try:
            lib = json.loads((root / "library.json").read_text()) if is_music else {}
            index = lib.get("tracks", {})
            tags["artists"].update(lib.get("artists", {}))
            tags["albums"].update(lib.get("albums", {}))
        except (OSError, ValueError):
            index = {}
        try:
            for dirpath, dirs, files in os.walk(root):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                rel_dir = pathlib.PurePosixPath(pathlib.Path(dirpath).relative_to(root))
                parts = rel_dir.parts
                cover = next((c for c in COVERS if c in files), None)
                for f in sorted(files):
                    stem, ext = os.path.splitext(f)
                    ext = ext.lower()
                    rid = f"{root_i}/{rel_dir / f}" if parts else f"{root_i}/{f}"
                    if is_music and ext in AUDIO_EXT:
                        rel = rid.partition("/")[2]
                        if meta := index.get(rel):
                            artist, album_artist = meta["artist"], meta["album_artist"] or meta["artist"]
                            album, title, year = meta["album"], meta["title"], meta["year"]
                            n = meta["disc"] * 1000 + meta["track"]
                            pop = meta.get("pop", 0)
                        else:  # no index: make what we can of the folder names
                            m = TRACK_NO.match(stem)
                            title = stem[m.end():] if m and m.end() < len(stem) else stem
                            artist = clean_folder(parts[0])[0] if len(parts) >= 2 else ""
                            if artist and title.lower().startswith(artist.lower() + " - "):
                                title = title[len(artist) + 3:]
                            album, year = clean_folder(parts[1] if len(parts) >= 2 else (parts[0] if parts else ""),
                                                       artist)
                            album_artist, title = artist, title.replace("_", " ").strip()
                            pop = 0
                            n = (int(m.group(1) or 1) * 1000 + int(m.group(2))) if m else 0
                        tracks.append({
                            "id": rid, "title": title, "artist": artist, "album_artist": album_artist,
                            "album": album, "year": year, "n": n,
                            "folder": str(rel_dir) if parts else "",  # voice search matches on this too
                            "art": f"{root_i}/{rel_dir}" if cover else "", "pop": pop,
                        })
                    elif not is_music and ext in VIDEO_EXT:
                        videos.append({"id": rid, "title": stem.replace("_", " ").replace(".", " ").strip(),
                                       "folder": str(rel_dir) if parts else ""})
        except OSError:
            continue  # the phone's share isn't mounted
    # Sent as rows of a table: 25k tracks as objects would be 12 MB of JSON, as rows about a quarter of that.
    cols = ["id", "title", "artist", "album_artist", "album", "year", "n", "art"]
    body = json.dumps({"cols": cols, "rows": [[t[c] if c != "art" else int(bool(t["art"])) for c in cols]
                                              for t in tracks], "videos": videos,
                       "categories": music.categories(tracks, tags)}, ensure_ascii=False).encode()
    with library_lock:
        library.update(t=time.time(), tracks=tracks, videos=videos, body=body, tags=tags)
    return library


def library_loop():
    """Rescan every 10 minutes, so music copied onto the Pi shows up without a restart."""
    while True:
        try:
            scan_library()
        except OSError as e:
            print(f"library scan failed: {e}", file=sys.stderr)
        time.sleep(600)


def media_library():
    if not library["t"] or (not library["tracks"] and time.time() - library["t"] > 30):
        scan_library()  # first request, or still empty: music may have just been copied
    return library


def media_path(media_id, exts=AUDIO_EXT | VIDEO_EXT):
    root_i, _, rel = media_id.partition("/")
    root = MEDIA_DIRS[int(root_i)].resolve()
    p = (root / rel).resolve()
    if root not in p.parents or p.suffix.lower() not in exts:
        raise ValueError("bad media id")
    return p


def cover_path(album_id):
    """The cover image in an album folder (album_id = "<root>/<folder>")."""
    root_i, _, rel = album_id.partition("/")
    root = MEDIA_DIRS[int(root_i)].resolve()
    d = (root / rel).resolve()
    if d != root and root not in d.parents:
        raise ValueError("bad album")
    for c in COVERS:
        if (d / c).is_file():
            return d / c
    raise FileNotFoundError("no cover")


def volume(level=None):
    env = {**os.environ, "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")}
    if level is not None:
        subprocess.run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{max(0, min(100, level)) / 100:.2f}"],
                       env=env, timeout=3, check=False)
    out = subprocess.run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], capture_output=True, text=True,
                         env=env, timeout=3).stdout  # "Volume: 0.40 [MUTED]"
    try:
        return {"level": round(float(out.split()[1]) * 100), "muted": "MUTED" in out}
    except (IndexError, ValueError):
        return {"level": None, "muted": False}


# --- HTTP --------------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("HEADUNIT_LOG"):
            super().log_message(fmt, *args)

    def send(self, code, body, ctype="application/json", cache=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache or "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        multi = urllib.parse.parse_qs(url.query)
        q = {k: v[0] for k, v in multi.items()}
        path = url.path
        try:
            if path == "/api/emergency":  # the Emergency screen: where the truck is, or ?lat=&lon= (somewhere you're headed)
                loc = (float(q["lat"]), float(q["lon"])) if "lat" in q else here()
                if not loc:
                    return self.send(200, {"error": "Waiting for a GPS fix (or set a home in Settings)"})
                return self.send(200, emergency.summary(*loc))
            if path == "/api/quakes":  # the past week's quakes as GeoJSON, for the map layer
                return self.send(200, {"type": "FeatureCollection", "features": [
                    {"type": "Feature", "geometry": {"type": "Point", "coordinates": [q["lon"], q["lat"]]},
                     "properties": {"mag": q["mag"], "place": q["place"], "hours": round((time.time() - q["time"]) / 3600, 1)}}
                    for q in emergency.quakes()]}, cache="max-age=60")
            if path.startswith("/emergency/"):  # /emergency/zones.geojson etc.: tsunami evacuation layers
                return self.file(emergency.layer_path(path[11:].removesuffix(".geojson")), "max-age=86400")
            if path == "/api/where":  # ?lat=&lon=: the nearest town, for "where are we?" and the assistant's context
                return self.send(200, offline.nearest_town(float(q["lat"]), float(q["lon"])) or {})
            if path == "/api/wifi":  # networks around, the one we're on, and the data-use setting
                return self.send(200, network.wifi(q.get("rescan") == "1"))
            if path == "/api/config":  # what the page needs from nomad.conf
                return self.send(200, {"name": config.NAME})
            if path == "/api/bt":
                return self.send(200, bt.status())
            if path == "/api/radio":
                loc = here()
                ps = [{**p, "auto": radio.will_capture(p)} for p in radio.passes(*loc)] if loc else []
                return self.send(200, {**radio.status(), "passes": ps, "tle_age_days": radio.tle_age_days(),
                                       "capture": radio.capture.state, "captures": radio.captures(), **radio.radio_settings()})
            if path == "/api/radio/capture":  # ?id=<capture folder>&name=<file in it>
                return self.file(radio.capture_file(q["id"], q["name"]), "max-age=86400")
            if path == "/api/radio/stream":  # ?f=hz[&f=hz...]&mode=wbfm|am|nfm|air[&sq=][&label=]
                return self.radio_stream(multi.get("f", []), q.get("mode", "wbfm"), q.get("sq", 0), q.get("label", ""))
            if path.startswith("/wx/radar/"):  # /wx/radar/z/x/y.png
                z, x, y = (int(v) for v in path[len("/wx/radar/"):].removesuffix(".png").split("/"))
                return self.send(200, radio.radar_tile(z, x, y, offline=not network.allowed("ask")), "image/png", "max-age=240")
            if path == "/api/wx/goes/frames":  # ?view=: the animation frames on hand
                return self.send(200, {"frames": radio.goes_frames(q.get("view", "pnw"))})
            if path == "/api/wx/goes/frame":  # ?view=&stamp=
                return self.file(radio.goes_frame(q.get("view", "pnw"), q.get("stamp")), "max-age=86400")
            if path == "/api/wx/goes":  # ?view=pnw|pnw-ir|west|fd|fd-east
                got = radio.goes_image(q.get("view", "pnw"), offline=not network.allowed("ask"))
                if not got:
                    return self.send(503, {"error": "no GOES image yet (needs internet once)"})
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(got[0])))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Image-Age", str(round(got[1])))  # seconds since NOAA's copy was fetched
                self.end_headers()
                self.wfile.write(got[0])
                return
            if path == "/api/weather":
                loc = here()
                if not loc:
                    return self.send(200, {"error": "Set a home in Settings (or wait for a GPS fix) for the forecast"})
                return self.send(200, radio.forecast(*loc) or {"error": "no forecast yet (needs internet once)"})
            if path == "/api/events":
                return self.events()
            if path == "/api/state":
                return self.send(200, state.snapshot())
            if path.startswith("/tiles/"):
                z, x, y = (int(p) for p in path[7:].removesuffix(".png").split("/"))
                if not (0 <= z <= 19 and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
                    return self.send(404, {"error": "bad tile"})
                data = tile(z, x, y)
                return (self.send(200, data, "image/png", "max-age=86400") if data
                        else self.send(404, {"error": "offline"}))
            if path == "/api/offline":
                return self.send(200, offline.status())
            if path.startswith("/maps/") and path.endswith(".pmtiles"):
                p = (offline.DATA / "maps" / path[6:]).resolve()
                if p.parent != (offline.DATA / "maps").resolve() or not p.is_file():
                    return self.send(404, {"error": "no such map"})
                return self.file(p, "max-age=86400")
            if path.startswith("/terrain/"):  # /terrain/z/x/y.png: elevation for hillshade, colors, contours, 3D
                z, x, y = (int(v) for v in path[9:].removesuffix(".png").split("/"))
                data = offline.terrain_tile(z, x, y) or terrain_online(z, x, y)
                return (self.send(200, data, "image/png", "max-age=86400") if data
                        else self.send(404, {"error": "no elevation here"}))
            if path.startswith("/imagery/"):
                name, z, x, y = path[9:].removesuffix(".jpg").split("/")
                data = offline.imagery_tile(name, int(z), int(x), int(y))
                return (self.send(200, data, "image/jpeg", "max-age=86400") if data
                        else self.send(404, {"error": "no imagery"}))
            if path == "/api/search":
                lat, lon = (float(q["lat"]), float(q["lon"])) if "lat" in q else (None, None)
                return self.send(200, {"results": search(q["q"], lat, lon)})
            if path == "/api/route":
                frm = [float(v) for v in q["from"].split(",")]
                to = [float(v) for v in q["to"].split(",")]
                return self.send(200, route(frm, to, q.get("units", "km")))
            if path == "/api/media":
                return self.send(200, media_library()["body"], "application/json")
            if path == "/api/volume":
                return self.send(200, volume())
            if path == "/api/media/tag":  # ?kind=mood|style|genre|decade&tag=: the tracks with it, as library indices
                lib = media_library()
                return self.send(200, {"tracks": music.tag_tracks(lib["tracks"], lib["tags"], q["kind"], q["tag"])})
            if path == "/api/playlists":
                return self.send(200, {"playlists": music.playlists()})
            if path == "/api/mesh":  # the LoRa mesh: our node, the others, messages
                try:
                    return self.send(200, mesh_call("/state"))
                except (OSError, ValueError):
                    return self.send(200, {"status": "not running", "nodes": [], "messages": [], "channels": [], "prefs": {}})
            if path == "/api/adsb":  # aircraft heard on 1090 MHz; asking keeps the receiver on for a while
                adsb.want()
                return self.send(200, adsb.status())
            if path == "/api/fuel":  # fill-ups with mpg, money spent, stations' prices, and the weekly averages
                return self.send(200, {**fuel.summary(), "prices": fuel.prices(_state_code())})
            if path == "/api/trip":  # odometer, trips A and B, oil change
                return self.send(200, trip.status())
            if path == "/api/obd":  # everything from the OBD adapter, codes included
                return self.send(200, obd.status())
            if path == "/api/news":  # ?feed=<url>: the cached headlines, newest first
                return self.send(200, news.headlines(q.get("feed")))
            if path == "/api/media/file":
                return self.file(media_path(q["id"]))
            if path == "/api/media/art":
                try:
                    return self.file(cover_path(q["album"]), "max-age=86400")
                except FileNotFoundError:
                    return self.send(404, {"error": "no cover"})
            if path == "/api/cameras":
                return self.send(200, phone.cameras())
            if path == "/api/camera/mjpeg":
                return self.mjpeg(q.get("cam"), int(q.get("fps", 10)))
            if path == "/api/camera/snapshot":
                return self.send(200, phone.snapshot(q.get("cam")), "image/jpeg")
            return self.static(path)
        except urllib.error.HTTPError as e:
            return self.send(e.code, {"error": e.read().decode(errors="replace")[:300] or str(e)})
        except (KeyError, ValueError) as e:
            return self.send(400, {"error": f"bad request: {e}"})
        except (OSError, urllib.error.URLError) as e:
            return self.send(502, {"error": str(e)})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/voice":
                return self.voice()
            if path == "/api/assistant":  # {messages, context, think}: the answer streams as JSON lines
                return self.assistant(self.body_json())
            if path == "/api/say":  # {text, voice}
                body = self.body_json()
                speech.put((body["text"], body.get("voice")))
                return self.send(200, {"ok": True})
            if path == "/api/tts":  # {text, voice} -> the WAV, for a screen that plays speech itself
                body = self.body_json()
                req = urllib.request.Request(f"{TTS}/tts", data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    return self.send(200, r.read(), "audio/wav")
            if path == "/api/say/prefetch":  # {texts, voice}: a route's prompts, made ahead so they play on time
                body = self.body_json()
                req = urllib.request.Request(f"{TTS}/prefetch", data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    return self.send(200, json.load(r))
            if path in ("/api/intent", "/api/chat"):
                body = self.body_json()
                try:
                    if path == "/api/intent":
                        return self.send(200, intent.command(body["text"]))
                    return self.send(200, intent.chat(body["text"], _with_engine(str(body.get("context", ""))[:400])))
                except (OSError, urllib.error.URLError) as e:
                    return self.send(503, {"error": f"voice model not running: {e}"})
            if path == "/api/volume":
                return self.send(200, volume(int(self.body_json()["level"])))
            if path == "/api/sim" and SIM:
                state.set(sim=self.body_json() or None)
                return self.send(200, {"ok": True})
            if path == "/api/media/vibe":  # {text}: a mix for a described vibe (the voice model picks the tags)
                lib = media_library()
                try:
                    return self.send(200, music.vibe(self.body_json()["text"], lib["tracks"], lib["tags"]))
                except (OSError, urllib.error.URLError) as e:
                    return self.send(503, {"error": f"voice model not running: {e}"})
            if path == "/api/playlists/create":  # {name, tracks: [track ids]}
                body = self.body_json()
                lists, pl = music.playlist_create(body.get("name"), body.get("tracks") or [])
                return self.send(200, {"playlists": lists, "created": pl})
            if path == "/api/playlists/add":  # {id, tracks}
                body = self.body_json()
                return self.send(200, {"playlists": music.playlist_add(body["id"], body.get("tracks") or [])})
            if path == "/api/playlists/remove":  # {id, track}
                body = self.body_json()
                return self.send(200, {"playlists": music.playlist_remove_track(body["id"], body["track"])})
            if path == "/api/playlists/delete":  # {id}
                return self.send(200, {"playlists": music.playlist_delete(self.body_json()["id"])})
            if path == "/api/wifi/connect":  # {ssid, password}
                body = self.body_json()
                return self.send(200, network.connect(body["ssid"], body.get("password") or None))
            if path == "/api/wifi/forget":  # {ssid}
                return self.send(200, network.forget(self.body_json()["ssid"]))
            if path in ("/api/mesh/send", "/api/mesh/settings"):  # {text, to?, channel?} / {share?, speak?}
                try:
                    return self.send(200, mesh_call(path[len("/api/mesh"):], self.body_json(), timeout=10))
                except (OSError, ValueError) as e:
                    return self.send(400, {"error": str(e)[:160]})
            if path == "/api/adsb":  # {always}: keep receiving with the screen elsewhere
                adsb.set_always(self.body_json().get("always"))
                return self.send(200, adsb.status())
            if path == "/api/fuel/add":  # {gallons, price? | total?, full?, station?: {name, lat, lon}}
                body = self.body_json()
                try:
                    _, entry = fuel.add(body["gallons"], body.get("price"), body.get("total"), body.get("full", True),
                                        body.get("station"), body.get("grade", "Regular"))
                except (KeyError, ValueError, TypeError) as e:
                    return self.send(400, {"error": str(e)})
                return self.send(200, {**fuel.summary(), "added": entry})
            if path == "/api/fuel/delete":  # {id}
                fuel.delete(self.body_json()["id"])
                return self.send(200, fuel.summary())
            if path == "/api/fuel/prices":  # fetch the averages again now
                return self.send(200, fuel.prices(_state_code(), force=True))
            if path == "/api/route/weather":  # {points: [[lat, lon, km_along]], arrive}: alerts on the way, weather on arrival
                if not network.allowed("ask"):
                    return self.send(200, {"alerts": [], "arrival": None, "offline": True})
                body = self.body_json()
                return self.send(200, radio.route_weather(body.get("points") or [], float(body.get("arrive") or time.time())))
            if path == "/api/trip/reset":  # {which: a | b}
                return self.send(200, trip.reset(self.body_json().get("which", "a")))
            if path == "/api/trip/odometer":  # {km}
                return self.send(200, trip.set_odometer(float(self.body_json()["km"])))
            if path == "/api/trip/oil":  # {every_km?}: the oil was just changed (or only the interval, with set_only)
                body = self.body_json()
                try:
                    return self.send(200, trip.oil_changed(body.get("every_km")))
                except ValueError as e:
                    return self.send(400, {"error": str(e)})
            if path == "/api/obd/codes":  # read the check-engine codes now
                return self.send(200, obd.read_codes())
            if path == "/api/obd/clear":  # clear them (and the readiness monitors)
                return self.send(200, obd.clear_codes())
            if path == "/api/obd/scan":  # nearby Bluetooth OBD adapters
                return self.send(200, {"devices": obd.scan_bluetooth()})
            if path == "/api/obd/pair":  # {mac}
                ok = obd.pair_bluetooth(self.body_json()["mac"])
                return self.send(200 if ok else 400, {"ok": ok} if ok else {"error": "Pairing failed: is the adapter plugged in, and the ignition on?"})
            if path == "/api/news/refresh":
                if not network.allowed("ask"):
                    return self.send(200, {**news.headlines(), "note": "Offline (Settings → Data use): the last headlines"})
                news.refresh(force=True)
                return self.send(200, news.headlines())
            if path == "/api/news/add":  # {url, name?}
                body = self.body_json()
                try:
                    return self.send(200, {"feeds": news.add_feed(body["url"], body.get("name"))})
                except (OSError, ValueError) as e:
                    return self.send(400, {"error": str(e)[:160]})
            if path == "/api/news/remove":  # {url}
                return self.send(200, {"feeds": news.remove_feed(self.body_json()["url"])})
            if path == "/api/data":  # {mode: auto | normal | saver | offline}
                return self.send(200, network.set_data_use(self.body_json().get("mode")))
            if path == "/api/bt/pair":
                return self.send(200, bt.pair())
            if path == "/api/bt/forget":  # {mac}
                return self.send(200, bt.forget(self.body_json().get("mac")))
            if path == "/api/home":  # {name, address, lat, lon}, or {} to clear
                h = self.body_json()
                if h.get("lat") is None:
                    HOME_FILE.unlink(missing_ok=True)
                else:
                    HOME_FILE.parent.mkdir(parents=True, exist_ok=True)
                    HOME_FILE.write_text(json.dumps({k: h.get(k) for k in ("name", "address", "lat", "lon")}))
                return self.send(200, {"ok": True})
            if path == "/api/radio/stop":
                radio.tuner.stop()
                return self.send(200, {"ok": True})
            if path == "/api/radio/settings":
                return self.send(200, radio.save_settings(self.body_json().get("auto")))
            if path == "/api/radio/fmscan":
                return self.send(200, {"stations": radio.fm_scan()})
            if path == "/api/dashcam/lock":
                return self.send(200, phone.dashcam_lock("headunit"))
            return self.send(404, {"error": "not found"})
        except urllib.error.HTTPError as e:
            return self.send(e.code, {"error": e.read().decode(errors="replace")[:300] or str(e)})
        except (KeyError, ValueError) as e:
            return self.send(400, {"error": f"bad request: {e}"})
        except (OSError, urllib.error.URLError) as e:
            return self.send(502, {"error": str(e)})

    def voice(self):
        """Listen once, streaming each stage as a JSON line so the screen can follow along:
        listening -> speech (the driver started talking) -> heard (done listening) -> the transcript."""
        if not mic_lock.acquire(blocking=False):
            return self.send(409, {"error": "already listening"})
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.close_connection = True
            phase = lambda name, **kw: (self.wfile.write(json.dumps({"phase": name, **kw}).encode() + b"\n"),  # noqa: E731
                                        self.wfile.flush())
            phase("listening")
            try:
                pcm = record_command(on_phase=phase)
            except SystemExit as e:  # voice.find_mic exits when there's no HAT
                return phase("done", ok=False, error=str(e), heard=False)
        except OSError:
            return  # the screen went away
        finally:
            mic_lock.release()
        try:
            if len(pcm) < voice.RATE // 4 * 2:
                return phase("done", ok=True, text="", heard=False)
            phase("heard")
            t = time.time()
            result = transcribe(voice.to_wav(pcm), prompt=f"{config.NAME}, navigate to, play, pause, next, zoom, dashcam.")
            result.update(round_trip_s=round(time.time() - t, 2), audio_s=round(len(pcm) / 2 / voice.RATE, 2), heard=True)
            phase("done", **result)
        except OSError:
            pass

    def assistant(self, body):
        """The Assistant tab: Gemma's thinking and answer as they're generated, one JSON line per piece:
        {"t": "think" | "text", "d": "..."}, then {"t": "done", "tokens": n, "s": seconds}."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        t, n = time.time(), 0
        try:
            for kind, text in intent.assistant_stream(body.get("messages") or [], _with_engine(str(body.get("context", ""))[:400]),
                                                      body.get("think", True)):
                n += 1
                self.wfile.write(json.dumps({"t": kind, "d": text}).encode() + b"\n")
                self.wfile.flush()
            self.wfile.write(json.dumps({"t": "done", "tokens": n, "s": round(time.time() - t, 1)}).encode() + b"\n")
        except BrokenPipeError:
            pass  # Stop: the page let go, and closing our end stops llama-server generating
        except (OSError, urllib.error.URLError) as e:
            try:
                self.wfile.write(json.dumps({"t": "error", "d": f"The model isn't running: {e}"}).encode() + b"\n")
            except OSError:
                pass

    def radio_stream(self, freqs, mode, squelch, label):
        """The tuned station as an endless MP3; the tuner stops when the page lets go of it."""
        try:
            gen, out = radio.tuner.start(freqs, mode, squelch, label)
        except (OSError, ValueError) as e:
            return self.send(503, {"error": str(e)})
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.close_connection = True
        try:
            while chunk := out.read(4096):
                self.wfile.write(chunk)
        except OSError:
            pass
        finally:
            radio.tuner.stop(gen)

    def events(self):
        """Server-sent events: a state snapshot every 0.5 s while the UI is connected."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                self.wfile.write(b"data: " + json.dumps(state.snapshot()).encode() + b"\n\n")
                self.wfile.flush()
                time.sleep(0.5)
        except OSError:
            pass

    def mjpeg(self, cam, fps):
        """Pass a phone camera's MJPEG stream through, so the UI never talks to the phone directly."""
        with urllib.request.urlopen(phone.mjpeg_url(cam, fps), timeout=15) as up:
            self.send_response(200)
            self.send_header("Content-Type", up.headers.get("Content-Type", "multipart/x-mixed-replace"))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                while chunk := up.read1(65536):
                    self.wfile.write(chunk)
            except OSError:
                pass

    def file(self, p, cache="no-cache"):
        """Send a file, with Range support so audio can seek."""
        size = p.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else max(0, size - int(b))
            end = min(int(b), size - 1) if a and b else size - 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", mimetypes.guess_type(p.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", cache)
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0 and (chunk := f.read(min(65536, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except OSError:
                pass

    def static(self, path):
        p = (STATIC / (path.lstrip("/") or "index.html")).resolve()
        if STATIC.resolve() not in p.parents or not p.is_file():
            return self.send(404, {"error": "not found"})
        return self.file(p, "no-cache")


def main():
    for loop in (gps_loop, phone_loop, system_loop, speech_loop, intent.warm_loop, library_loop,
                 lambda: radio.capture_loop(here), lambda: emergency.quake_loop(here),
                 lambda: radio.weather_loop(here), net_loop, news.news_loop, obd.obd_loop, mesh_loop, road_loop,
                 lambda: trip.trip_loop(lambda: state.position(time.time())), lambda: adsb.adsb_loop(here)):
        threading.Thread(target=loop, daemon=True).start()
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    server.daemon_threads = True
    print(f"head unit on http://{BIND}:{PORT}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
