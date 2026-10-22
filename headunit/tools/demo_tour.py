#!/usr/bin/env python3
"""Record a captioned tour of the head unit, driven through the kiosk browser's DevTools port, with no personal information in it.

Run on the Pi. The kiosk's Chromium must be listening on --remote-debugging-port=9222 (the kiosk script passes extra arguments on to it:
`headunit-kiosk.sh --remote-debugging-port=9222`), the head unit must have been started with HEADUNIT_SIM=1, and wf-recorder installed:

    python3 demo_tour.py [--out ~/nomad-demo.mkv] [--city 46.1382,-122.9382] [--dry]

What keeps it private: the position is the simulator's (a spot downtown), never the GPS; recent destinations and the assistant's chat are
cleared for the tour and put back at the end; and it only visits screens that show nothing about the owner (no Wi-Fi, Bluetooth,
network, messages or drive history). Captions are drawn into the page itself (and also written to an .srt file beside the video).
Things the radio can't hear on this antenna (planes, ships) are shown with sample data, and the caption says so.
"""
import argparse
import json
import math
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from cdp import Page  # noqa: E402

HU = "http://127.0.0.1:8800"
CAPTION_JS = r"""
window.__cap = (t) => {
  let e = document.getElementById("demo-cap");
  if (!e) {
    e = document.createElement("div"); e.id = "demo-cap";
    e.style.cssText = "position:fixed;left:50%;bottom:30px;transform:translateX(-50%);z-index:99999;background:rgba(0,0,0,.78);color:#fff;font:600 26px system-ui,sans-serif;padding:10px 24px;border-radius:16px;max-width:78%;text-align:center;pointer-events:none;transition:opacity .35s;line-height:1.25";
    document.body.append(e);
  }
  e.textContent = t || ""; e.style.opacity = t ? 1 : 0;
};
"""


def sim(lat, lon, speed=0.0, track=0.0):
    req = urllib.request.Request(f"{HU}/api/sim", data=json.dumps({"lat": lat, "lon": lon, "speed_mps": speed, "track_deg": track}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()


def bearing(a, b):
    p = math.pi / 180
    y = math.sin((b[1] - a[1]) * p) * math.cos(b[0] * p)
    x = math.cos(a[0] * p) * math.sin(b[0] * p) - math.sin(a[0] * p) * math.cos(b[0] * p) * math.cos((b[1] - a[1]) * p)
    return (math.atan2(y, x) / p + 360) % 360


class Tour:
    def __init__(self, page, record_to=None, city=(46.1382, -122.9382)):
        self.p, self.city, self.t0, self.srt, self.cur = page, city, None, [], None
        self.record_to, self.rec = record_to, None

    # ---- recording and captions ----
    def start(self):
        self.p.eval(CAPTION_JS)
        if self.record_to:
            out = subprocess.run(["wlr-randr"], capture_output=True, text=True, env={**os.environ, "WAYLAND_DISPLAY": "wayland-0",
                                 "XDG_RUNTIME_DIR": "/run/user/1000"}).stdout.split()[0]
            self.rec = subprocess.Popen(["nice", "-n", "5", "wf-recorder", "-o", out, "-f", self.record_to, "-r", "15", "-c", "libx264",
                                         "-p", "preset=ultrafast", "-p", "crf=28", "-F", "scale=1280:720"],
                                        env={**os.environ, "WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000"},
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3)
        self.t0 = time.time()

    def stop(self):
        self.caption("")
        if self.rec:
            self.rec.send_signal(2)  # SIGINT: wf-recorder finishes the file
            self.rec.wait(timeout=30)
        if self.record_to:
            pathlib.Path(self.record_to).with_suffix(".srt").write_text(self._srt())

    def caption(self, text, secs=0.0):
        now = time.time() - (self.t0 or time.time())
        if self.cur:  # close the previous caption
            self.srt.append((self.cur[0], now, self.cur[1]))
        self.cur = (now, text) if text else None
        self.p.eval(f"__cap({json.dumps(text)})")
        if secs:
            time.sleep(secs)

    def _srt(self):
        def ts(x):
            return f"{int(x // 3600):02}:{int(x % 3600 // 60):02}:{int(x % 60):02},{int(x * 1000) % 1000:03}"
        return "\n".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n" for i, (a, b, t) in enumerate(self.srt, 1))

    def js(self, code, wait=0.0, timeout=60):
        r = self.p.eval(code, timeout=timeout)
        if wait:
            time.sleep(wait)
        return r

    def fly(self, lon, lat, zoom, secs=2.5, pitch=0, bearing_=0, wait=3.0):
        self.js(f"setFollow(false); map.easeTo({{center:[{lon},{lat}],zoom:{zoom},pitch:{pitch},bearing:{bearing_},duration:{int(secs * 1000)}}})", secs + wait)

    # ---- the tour ----
    def prepare(self):
        """Private state out of the way: recent places, chat; a known layer set; dark theme."""
        self.saved = self.js("""({recents: localStorage.getItem("recents"), chat: localStorage.getItem("chatLog"), settings: JSON.stringify(settings),
                                  layers: JSON.stringify(terrain), satellite, radar: radarOn, view: currentView})""")
        self.js("""localStorage.setItem("recents", "[]"); chatLog.msgs = []; save("chatLog", []);
                   for (const k of ["quakes","aircraft","ships","tsunami","hillshade","relief","contours","3d"]) if (terrain[k]) toggleLayer(k);
                   if (!terrain.trailheads) toggleLayer("trailheads");
                   if (radarOn) setRadar(false);
                   document.querySelector('.seg[data-setting="theme"] button[data-v="dark"]').click();
                   document.querySelector('.seg[data-setting="units"] button[data-v="imperial"]').click();
                   showView("home"); setPanel(false, false);""")
        sim(*self.city, 0, 0)
        time.sleep(3)

    def restore(self):
        s = self.saved
        self.js(f"""(() => {{
          const s = JSON.parse({json.dumps(s["settings"])}), layers = JSON.parse({json.dumps(s["layers"])});
          localStorage.setItem("recents", {json.dumps(s["recents"] or "[]")});
          localStorage.setItem("chatLog", {json.dumps(s["chat"] or "[]")}); chatLog.msgs = JSON.parse({json.dumps(s["chat"] or "[]")});
          for (const k of Object.keys(layers)) if (terrain[k] !== layers[k]) toggleLayer(k);
          if (satellite !== {str(s["satellite"]).lower()}) setSatellite({str(s["satellite"]).lower()});
          if (radarOn !== {str(s["radar"]).lower()}) setRadar({str(s["radar"]).lower()});
          for (const k of ["theme","units","viz","accent"]) document.querySelector(`.seg[data-setting="${{k}}"] button[data-v="${{s[k]}}"]`)?.click();
          showView("home"); setFollow(true); window.__cap("");
        }})()""")
        urllib.request.urlopen(urllib.request.Request(f"{HU}/api/sim", data=b"null", headers={"Content-Type": "application/json"}), timeout=5).read()

    def run(self):
        c = self.caption
        lat, lon = self.city
        self.prepare()
        c("NOMAD: a car head unit on a Raspberry Pi 5", 4)
        # 1. the map
        c("Offline map and aerial imagery, with large readable labels")
        self.fly(lon, lat, 17.2, 3, wait=3)
        self.fly(lon, lat, 15.2, 3, wait=2)
        self.js("setSatellite(false)", 3)
        c("Switch between aerial photos and the vector map", 3)
        self.js("setSatellite(true)", 2)
        # 2. trails, forest roads, trailheads
        c("Trails (green) and forest service roads (orange), labeled by name or number")
        self.fly(-121.90, 46.10, 13.9, 3.5, wait=4)
        self.fly(-121.93, 46.12, 14.6, 3, wait=4)
        c("Trailheads, campgrounds and viewpoints, kept for offline use")
        self.fly(-122.19, 46.20, 12.6, 3.5, wait=3)
        self.js("""(() => { const f = map.queryRenderedFeatures({layers:["trailheads"]}).find((x) => x.properties.name); if (f) showTrailhead(f.properties, f.geometry.coordinates); })()""", 5)
        self.js("showQuake(null)")
        # 3. terrain
        c("Shaded relief, elevation colors, contours and 3D terrain")
        self.js("toggleLayer('hillshade'); toggleLayer('relief')", 1)
        self.fly(-122.18, 46.20, 11.4, 3, pitch=0, wait=2)
        self.js("toggleLayer('contours'); toggleLayer('3d')", 2)
        self.fly(-122.18, 46.14, 11.3, 4, pitch=62, bearing_=20, wait=2)
        self.js("map.easeTo({bearing: 110, duration: 9000})", 9.5)
        self.js("toggleLayer('3d'); toggleLayer('contours'); toggleLayer('relief'); toggleLayer('hillshade'); map.easeTo({pitch:0,bearing:0,duration:600})", 1.5)
        # 4. earthquakes, radar, tsunami
        c("Live earthquakes from USGS: tap one for magnitude, date and time")
        self.js("toggleLayer('quakes')", 1)
        self.fly(-124.0, 45.0, 5.6, 3, wait=3)
        self.js("""(() => { const f = map.queryRenderedFeatures({layers:["quakes"]}).sort((a, b) => b.properties.mag - a.properties.mag)[0];
                    if (f) showQuake(f.properties, f.geometry.coordinates); })()""", 5)
        self.js("showQuake(null); toggleLayer('quakes')")
        c("NEXRAD weather radar", 0)
        self.js("setRadar(true)", 1)
        self.fly(-122.9, 46.1, 6.2, 3, wait=4)
        self.js("setRadar(false)")
        c("Tsunami evacuation zones and routes, offline")
        self.js("toggleLayer('tsunami')", 1)
        self.fly(-124.05, 46.35, 10.6, 3, wait=5)
        self.js("toggleLayer('tsunami')")
        # 5. planes and ships
        self.aircraft_and_ships()
        # 6. navigation
        self.navigation()
        # 7. radio
        self.radio()
        # 8. music and visualizers
        self.music()
        # 9. weather, GOES
        self.weather()
        # 10. emergency, vehicle
        c("Emergency: tsunami zone check, earthquakes and weather-radio alerts, all offline", 0)
        self.js("showView('emergency')", 6)
        c("Vehicle: compass, satellites in view, trip computer, tire pressures", 0)
        self.js("showView('vehicle')", 6)
        # 11. the assistant changes the screen
        self.assistant()
        # 12. look
        self.look()
        self.js("showView('home'); setPanel(false, false)", 1)
        self.fly(lon, lat, 14, 2, wait=2)
        c("")

    def aircraft_and_ships(self):
        c = self.caption
        live = self.js("""(async () => { const a = await api("/api/adsb").catch(() => ({aircraft: []})), s = await api("/api/ais").catch(() => ({ships: []}));
                         return [a.aircraft.filter((p) => p.lat != null).length, s.ships.filter((v) => v.lat != null).length]; })()""")
        sample = not (live[0] or live[1])
        c("Aircraft (ADS-B) and ships (AIS), received by the same radio dongle" + ("  ·  SAMPLE DATA shown" if sample else ""))
        self.js("if (!terrain.aircraft) toggleLayer('aircraft'); if (!terrain.ships) toggleLayer('ships');", 2)
        if sample:  # nothing heard: draw made-up targets, and say so (the pollers are stopped so they stay)
            self.js("""(() => {
              clearTimeout(air.timer); clearTimeout(ais.timer); const now = Date.now() / 1000;
              air.data = {running: true, aircraft: [
                {icao: "A1B2C3", call: "ASA512", alt_ft: 36000, kt: 462, track: 205, vr: 0, lat: 46.55, lon: -122.45, pos_age: 1, age: 1, km: 52, squawk: "1200", trail: [[46.7, -122.3], [46.62, -122.4], [46.55, -122.45]]},
                {icao: "A4D5E6", call: "UAL1234", alt_ft: 12500, kt: 280, track: 20, vr: 1800, lat: 46.02, lon: -123.2, pos_age: 1, age: 1, km: 28, trail: [[45.9, -123.3], [46.02, -123.2]]},
                {icao: "ABC123", call: "N77DE", alt_ft: 4500, kt: 110, track: 90, vr: 0, lat: 46.2, lon: -122.7, pos_age: 1, age: 1, km: 20, trail: [[46.2, -122.9], [46.2, -122.7]]}]};
              ais.data = {ships: [
                {mmsi: 366123456, name: "COLUMBIA TRADER", callsign: "WDE1234", shiptype: 70, type_text: "Cargo ship", lat: 46.108, lon: -122.96, sog: 6.2, cog: 330, heading: 330, status_text: "under way (engine)", dest: "LONGVIEW WA", length: 180, beam: 28, draught: 9.5, km: 4, pos_age: 3, age: 3, seen: now},
                {mmsi: 538007111, name: "PACIFIC GRACE", callsign: "V7AB9", shiptype: 80, type_text: "Tanker", lat: 46.15, lon: -122.9, sog: 0, cog: 40, heading: 40, status_text: "moored", dest: "PORTLAND OR", length: 220, beam: 32, draught: 11.2, km: 6, pos_age: 3, age: 3, seen: now},
                {mmsi: 367999000, name: "MISS KATIE", callsign: "WDK445", shiptype: 52, type_text: "Tug", lat: 46.17, lon: -122.95, sog: 3.1, cog: 300, heading: 300, status_text: "under way (engine)", dest: "", length: 28, beam: 9, km: 5, pos_age: 3, age: 3, seen: now}]};
              map.getSource("planes")?.setData(planesGeojson(air.data)); map.getSource("plane-trails")?.setData(trailsGeojson(air.data));
              map.getSource("ships")?.setData(shipsGeojson(ais.data));
            })()""")
        self.fly(-122.93, 46.2, 9.4, 3.5, wait=3)
        self.js("showPlane('A1B2C3')", 4.5)
        self.js("showShip(366123456)", 4.5)
        self.js("showShip(null); showPlane(null); showQuake(null)")
        if sample:
            self.js("""air.data = null; ais.data = null; for (const k of ["aircraft","ships"]) if (terrain[k]) toggleLayer(k); pollAir(); pollShips();""")
        else:
            self.js("for (const k of ['aircraft','ships']) if (terrain[k]) toggleLayer(k)")

    def navigation(self):
        c = self.caption
        lat, lon = self.city
        c("Offline routing: pick a destination and see the route")
        self.fly(lon, lat, 13, 2, wait=1)
        self.js("""previewRoute({name: "Johnston Ridge Observatory", address: "Mount St. Helens", lat: 46.2762, lon: -122.2166})""", 7, timeout=90)
        route = self.js("nav.route ? nav.route.coords : null")
        if not route:
            c("(the route didn't load: skipping navigation)", 2)
            return
        c("Turn-by-turn guidance, spoken directions, speed limit and live volume control")
        self.js("startNav()", 2)
        # drive the simulated truck along the route at about 30 m/s, with the camera following
        pts, speed, step_m = route, 30.0, 30.0
        d, i, t_end = 0.0, 0, time.time() + 50
        while i < len(pts) - 2 and time.time() < t_end:
            a, b = pts[i], pts[i + 1]
            seg = self._dist(a, b)
            if d + seg < step_m:
                d += seg
                i += 1
                continue
            d = 0.0
            sim(b[0], b[1], speed, bearing(a, b))
            i += 1
            time.sleep(0.9)
        c("Brightness and volume are one tap away", 0)
        self.js("document.querySelector('#bright-pop').hidden = false", 3)
        self.js("document.querySelector('#bright-pop').hidden = true; endNav(); setFollow(false)", 1)
        sim(lat, lon, 0, 0)
        time.sleep(2)

    @staticmethod
    def _dist(a, b):
        p = math.pi / 180
        x = math.sin((b[0] - a[0]) * p / 2) ** 2 + math.cos(a[0] * p) * math.cos(b[0] * p) * math.sin((b[1] - a[1]) * p / 2) ** 2
        return 12742000 * math.asin(math.sqrt(x))

    def radio(self):
        c = self.caption
        c("Radio: FM with HD Radio, station names and a visualizer", 0)
        self.js("showView('radio'); document.querySelector('#radio-tabs [data-tab=fm]').click(); radio.hd = true", 2)
        self.js("tune({freqs: [98300000], mode: 'wbfm', label: '98.3 MHz'})", 22, timeout=40)
        self.js("loadKnown(true); renderRadio()", 4)
        c("An AM band scan, and a memory of the stations heard around here", 0)
        self.js("radioStop(); document.querySelector('#radio-tabs [data-tab=am]').click(); radioScan('am')", 8)
        self.js("document.querySelector('#radio-tabs [data-tab=fm]').click()", 3)
        c("The radio is shared: aircraft, ships, tire sensors, weather-radio alerts and satellites take turns", 0)
        self.js("document.querySelector('#radio-tabs [data-tab=sdr]').click()", 7)

    def music(self):
        c = self.caption
        c("Music library and a rotating visualizer", 0)
        self.js("showView('media')", 3)
        self.js("(async () => { await loadMedia(); if (media.tracks?.length) { playIndex(Math.floor(media.tracks.length / 3)); } })()", 3, timeout=40)
        self.js("openNowPlaying()", 3)
        for name in ("mirror", "wave", "ring", "scope", "bars"):
            self.js(f"""document.querySelector('.seg[data-setting="viz"] button[data-v="{name}"]')?.click(); startVisualizer()""", 3)
        self.js("document.querySelector('.seg[data-setting=\"viz\"] button[data-v=\"auto\"]')?.click(); $('#np-full').hidden = true; audio.pause()", 1)

    def weather(self):
        c = self.caption
        c("Weather: forecast, hourly chart, alerts and GOES satellite imagery", 0)
        self.js("showView('weather')", 6)
        self.js("""document.querySelector('#view-weather').scrollTo({top: 0, behavior: 'instant'});
                   const card = document.querySelector('#wx-list .goes, #wx-list canvas, #wx-list img'); card?.scrollIntoView({block: 'center'})""", 5)
        self.js("document.querySelector('#wx-list').scrollTo({top: 0, behavior: 'smooth'})", 3)

    def assistant(self):
        c = self.caption
        c("The assistant can change the screen for you, with Undo", 0)
        self.js("showView('assistant')", 2)
        self.js("askAssistant('turn the radar on and zoom out')", 18, timeout=60)
        self.js("document.querySelector('[data-action=ui-undo]')?.click()", 4)
        self.js("askAssistant('switch to metric units')", 15, timeout=60)
        self.js("document.querySelector('[data-action=ui-undo]')?.click()", 3)
        self.js("chatLog.msgs = []; save('chatLog', []); renderChat()")

    def look(self):
        c = self.caption
        c("Day and night themes, accent colors, and brightness controls", 0)
        self.js("showView('settings')", 1)
        self.js("document.querySelector('#set-bright').scrollIntoView({block: 'center', behavior: 'instant'})", 3)
        for accent in ("teal", "orange", "purple", "blue"):
            self.js(f"document.querySelector('.seg[data-setting=\"accent\"] button[data-v=\"{accent}\"]').click()", 1.4)
        self.js("document.querySelector('.seg[data-setting=\"theme\"] button[data-v=\"light\"]').click()", 4)
        self.js("showView('home'); setFollow(false); map.jumpTo({center: [%f, %f], zoom: 15})" % (self.city[1], self.city[0]), 4)
        self.js("document.querySelector('.seg[data-setting=\"theme\"] button[data-v=\"dark\"]').click()", 3)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(pathlib.Path.home() / "nomad-demo.mkv"))
    ap.add_argument("--city", default="46.1382,-122.9382", help="lat,lon of the spot downtown the simulated truck sits at")
    ap.add_argument("--dry", action="store_true", help="run the tour without recording")
    ap.add_argument("--port", type=int, default=9222)
    a = ap.parse_args()
    city = tuple(float(x) for x in a.city.split(","))
    page = Page(a.port)
    tour = Tour(page, None if a.dry else a.out, city)
    tour.start()
    try:
        tour.run()
    finally:
        try:
            tour.restore()
        finally:
            tour.stop()
    print("done:", a.out)


if __name__ == "__main__":
    main()
