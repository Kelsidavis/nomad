#!/usr/bin/env python3
"""Check a running NOMAD head unit, one line per part: services, offline data, search, routing, media, weather,
emergency, the LLM and speech. Run it on the Pi (deploy.sh does, after each deploy):

    python3 /opt/headunit/tools/selftest.py [--quick]

Exits non-zero when something that should work doesn't. Stdlib only.
"""
import json
import subprocess
import sys
import time
import urllib.request

HU = "http://127.0.0.1:8800"
results = []


def check(name, fn, required=True):
    t = time.time()
    try:
        detail = fn()
        ok = True
    except Exception as e:  # noqa: BLE001 - every failure is a result here
        detail, ok = f"{type(e).__name__}: {e}", False
    results.append((ok, required))
    mark = "ok  " if ok else ("FAIL" if required else "warn")
    print(f"{mark} {name:22} {detail}  ({time.time() - t:.1f}s)", flush=True)


def get(path, timeout=30, base=HU):
    with urllib.request.urlopen(f"{base}{path}", timeout=timeout) as r:
        return json.load(r)


def post(path, body, timeout=60, base=HU, raw=False):
    req = urllib.request.Request(f"{base}{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read() if raw else json.load(r)


def services():
    names = ["headunit", "llm", "stt", "tts", "valhalla", "gpsd"]
    out = subprocess.run(["systemctl", "is-active", *names], capture_output=True, text=True).stdout.split()
    down = [n for n, s in zip(names, out) if s != "active"]
    if down:
        raise RuntimeError(f"not running: {', '.join(down)}")
    return "all running"


def power():
    out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True).stdout.strip()
    flags = int(out.split("=")[1], 16)
    if flags & 0x1:
        raise RuntimeError(f"under-voltage NOW ({out})")
    return out + (" (dipped since boot)" if flags & 0x10000 else "")


def offline_data():
    d = get("/api/offline")
    missing = [k for k, v in (("map", d.get("maps")), ("imagery", d.get("imagery")), ("terrain", d.get("terrain")),
                              ("search", d.get("search")), ("routing", d.get("routing"))) if not v]
    if missing:
        raise RuntimeError(f"missing: {', '.join(missing)}")
    return f"map, {len(d['imagery'])} imagery sets, terrain, search, routing"


def where_to_test():
    s = get("/api/state")
    p = s.get("position")
    return (p["lat"], p["lon"]) if p else (45.5152, -122.6784)  # Portland, if there's no fix


def search():
    lat, lon = where_to_test()
    r = get(f"/api/search?q=gas+station&lat={lat}&lon={lon}")["results"]
    if not r:
        raise RuntimeError("no results")
    return f"{len(r)} results, first: {r[0]['name']}"


def route():
    lat, lon = where_to_test()
    r = get(f"/api/route?from={lat},{lon}&to={lat + 0.02},{lon + 0.02}")
    return f"{r['distance'] / 1000:.1f} km, {len(r['steps'])} steps ({r.get('source')})"


def media():
    d = get("/api/media")
    return f"{len(d['rows'])} tracks, {len(d['videos'])} videos"


def weather():
    d = get("/api/weather")
    if d.get("error"):
        raise RuntimeError(d["error"])
    return f"{d.get('place')}: {d['periods'][0]['shortForecast']}" + (" (offline copy)" if d.get("stale") else "")


def emergency():
    lat, lon = where_to_test()
    d = get(f"/api/emergency?lat={lat}&lon={lon}", timeout=60)
    return f"zone: {(d.get('zone') or {}).get('zone', 'none')}, {sum(len(v) for v in d['help'].values())} places to get help"


def llm():
    r = post("/api/intent", {"text": "I need gas"})
    if r.get("action") != "navigate":
        raise RuntimeError(f"understood {r}")
    return f"'I need gas' -> navigate '{r['query']}' in {r['llm_s']}s"


def tts():
    wav = post("/tts", {"text": "Self test."}, base="http://127.0.0.1:5002", raw=True)
    if wav[:4] != b"RIFF":
        raise RuntimeError("not a WAV")
    return f"{len(wav) // 1000} KB of speech"


def stt():
    with urllib.request.urlopen("http://127.0.0.1:8178/", timeout=10) as r:
        r.read(100)
    return "whisper server answering"


def gps():
    g = get("/api/state")["gps"]
    if not g["gpsd"]:
        raise RuntimeError("gpsd not reachable")
    return f"fix mode {g['mode']}, {g['sats_used'] or 0}/{g['sats_seen'] or 0} satellites"


def news():
    d = get("/api/news")
    if not d["items"]:
        raise RuntimeError("no headlines cached yet (they download when online)")
    return f"{len(d['items'])} headlines from {len(d['feeds'])} feeds"


def obd():
    s = get("/api/obd")
    if s["status"] != "connected":
        raise RuntimeError(f"{s['status']}{': ' + s['error'] if s.get('error') else ''}")
    return f"{s['adapter']}, {s['protocol']}, {len(s['dtcs'] or [])} codes"


def mesh():
    s = get("/api/mesh")
    if s["status"] != "connected":
        raise RuntimeError(s["status"])
    return f"{s['me']['name'] if s.get('me') else 'radio'} on {s['device']}, {len(s['nodes'])} nodes"


if __name__ == "__main__":
    quick = "--quick" in sys.argv
    check("services", services)
    check("power", power, required=False)
    check("offline data", offline_data, required=False)
    check("gps", gps, required=False)
    check("search", search)
    check("routing", route)
    check("media", media)
    check("weather", weather, required=False)
    check("emergency", emergency, required=False)
    check("news", news, required=False)
    check("OBD-II", obd, required=False)
    check("LoRa mesh", mesh, required=False)
    check("speech out (Kokoro)", tts)
    check("speech in (whisper)", stt)
    if not quick:
        check("voice model (Gemma)", llm)
    failed = [ok for ok, req in results if not ok and req]
    print(f"\n{'All good' if not failed else f'{len(failed)} required check(s) failed'}: {sum(ok for ok, _ in results)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
