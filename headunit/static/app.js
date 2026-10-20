// Head unit UI: map on the right, app panel on the left, everything driven by /api/events.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const RAD = Math.PI / 180;

// ---- settings (per screen, so localStorage is fine; the UI works without it) ----
const DEFAULTS = { units: "imperial", theme: "auto", clock: "12", prompts: "on", orient: "heading", viz: "bars", vizColor: "album",
  accent: "blue", voice: "af_bella", speakOn: "truck", notify: "hide" };
const settings = { ...DEFAULTS, ...load("settings", {}) };
function load(key, fallback) {
  try { return JSON.parse(localStorage.getItem("hu." + key)) ?? fallback; } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem("hu." + key, JSON.stringify(value)); } catch { /* private mode etc. */ }
}

async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.method = "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const r = await fetch(path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

let toastTimer;
function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), ms);
}

// Music down to a third while the head unit speaks (server.py's state.speaking), smoothly back after.
let duckTimer;
function duck(on) {
  clearInterval(duckTimer);
  const to = on ? 0.3 : 1;
  duckTimer = setInterval(() => {
    const v = audio.volume + (to - audio.volume) * 0.35;
    audio.volume = Math.abs(v - to) < 0.02 ? to : v;
    if (audio.volume === to) clearInterval(duckTimer);
  }, 40);
}
// Speech plays through the truck's speakers (server.py), or on this screen when Settings says so: a screen
// used from elsewhere (the tunnel on a laptop) can't hear the truck.
const localSpeech = [];
function speak(text) {
  if (settings.speakOn !== "here") return api("/api/say", { json: { text, voice: settings.voice } }).catch(() => {});
  localSpeech.push(text);
  if (localSpeech.length === 1) playLocalSpeech();
}
async function playLocalSpeech() {
  while (localSpeech.length) {
    try {
      const r = await fetch("/api/tts", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: localSpeech[0], voice: settings.voice }) });
      const clip = new Audio(URL.createObjectURL(await r.blob()));
      duck(true);
      await new Promise((done) => { clip.onended = clip.onerror = done; clip.play().catch(done); });
    } catch { /* skip it */ }
    duck(false);
    localSpeech.shift();
  }
}
function say(text) {
  if (settings.prompts === "on") speak(text);
}

// ---- formatting ----
function fmtDist(m) {
  if (settings.units === "imperial") {
    if (m < 160) return { v: String(Math.max(50, Math.round(m / 0.3048 / 50) * 50)), u: "ft" };
    const mi = m / 1609.344;
    return { v: mi < 10 ? mi.toFixed(1) : String(Math.round(mi)), u: "mi" };
  }
  if (m < 1000) return { v: String(Math.max(10, Math.round(m / 10) * 10)), u: "m" };
  const km = m / 1000;
  return { v: km < 10 ? km.toFixed(1) : String(Math.round(km)), u: "km" };
}
const distText = (m) => { const d = fmtDist(m); return `${d.v} ${d.u}`; };
// Distances the way a person says them: "500 feet", "a quarter mile", "1 and a half miles"; "300 meters", "2 kilometers".
function spokenDist(m) {
  if (settings.units === "imperial") {
    const mi = m / 1609.344;
    if (mi < 0.19) return `${Math.max(100, Math.round(m / 0.3048 / 100) * 100)} feet`;
    if (mi < 0.37) return "a quarter mile";
    if (mi < 0.62) return "half a mile";
    if (mi < 0.88) return "three quarters of a mile";
    const r = mi < 5 ? Math.round(mi * 2) / 2 : Math.round(mi);
    return r === 1 ? "1 mile" : r % 1 ? `${Math.floor(r)} and a half miles` : `${r} miles`;
  }
  if (m < 950) return `${Math.max(50, Math.round(m / 50) * 50)} meters`;
  const km = m < 5000 ? Math.round(m / 500) / 2 : Math.round(m / 1000);
  return km === 1 ? "1 kilometer" : `${km} kilometers`;
}
function fmtDur(s) {
  const min = Math.max(1, Math.round(s / 60));
  return min < 60 ? `${min} min` : `${Math.floor(min / 60)} h ${min % 60} min`;
}
// ampm: add AM/PM in 12-hour mode, for times that aren't obviously now (satellite passes, spoken answers).
function fmtClock(d, ampm = false) {
  const h = d.getHours(), m = String(d.getMinutes()).padStart(2, "0");
  if (settings.clock === "24") return `${String(h).padStart(2, "0")}:${m}`;
  return `${h % 12 || 12}:${m}${ampm ? (h < 12 ? " AM" : " PM") : ""}`;
}
const speedVal = (mps) => (mps == null ? "--" : String(Math.round(Math.max(0, mps) * (settings.units === "imperial" ? 2.23694 : 3.6))));
// The truck's own speed (OBD) when the adapter's connected: right at a crawl and with no GPS fix; else the GPS's.
const speedNow = (s) => s?.obd?.v?.speed != null ? s.obd.v.speed / 3.6
  : s?.position ? (s.position.speed_mps < 0.5 ? 0 : s.position.speed_mps) : null;
const speedUnit = () => (settings.units === "imperial" ? "mph" : "km/h");
const altVal = (m) => (m == null ? "--" : String(Math.round(settings.units === "imperial" ? m * 3.28084 : m)));
const cardinal = (deg) => ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(((deg % 360) + 360) % 360 / 45) % 8];

function bind(name, value) {
  for (const el of $$(`[data-bind="${name}"]`)) {
    if (el.tagName === "INPUT") { if (document.activeElement !== el) el.value = value; }
    else if (el.tagName === "use") el.setAttribute("href", value);
    else if (el.dataset.bind === "track-progress") el.style.width = value;
    else el.textContent = value;
  }
}

// ---- theme: auto follows sunrise/sunset at the current position ----
function sunTimes(date, lat, lon) {
  const dayMs = 864e5, J1970 = 2440588, J2000 = 2451545, J0 = 0.0009, e = RAD * 23.4397;
  const toDays = (d) => d.valueOf() / dayMs - 0.5 + J1970 - J2000;
  const fromJ = (j) => new Date((j + 0.5 - J1970) * dayMs);
  const lw = RAD * -lon, phi = RAD * lat;
  const n = Math.round(toDays(date) - J0 - lw / (2 * Math.PI));
  const ds = J0 + lw / (2 * Math.PI) + n;
  const M = RAD * (357.5291 + 0.98560028 * ds);
  const L = M + RAD * (1.9148 * Math.sin(M) + 0.02 * Math.sin(2 * M) + 0.0003 * Math.sin(3 * M)) + RAD * 102.9372 + Math.PI;
  const dec = Math.asin(Math.sin(e) * Math.sin(L));
  const noon = J2000 + ds + 0.0053 * Math.sin(M) - 0.0069 * Math.sin(2 * L);
  const w = Math.acos((Math.sin(RAD * -0.833) - Math.sin(phi) * Math.sin(dec)) / (Math.cos(phi) * Math.cos(dec)));
  const set = J2000 + J0 + (w + lw) / (2 * Math.PI) + n + 0.0053 * Math.sin(M) - 0.0069 * Math.sin(2 * L);
  return [fromJ(noon - (set - noon)), fromJ(set)];
}
function applyTheme() {
  let theme = settings.theme;
  if (theme === "auto") {
    const now = new Date(), p = lastPos;
    if (p) {
      const [rise, set] = sunTimes(now, p.lat, p.lon);
      theme = now > rise && now < set ? "light" : "dark";
    } else theme = now.getHours() >= 7 && now.getHours() < 19 ? "light" : "dark";
  }
  if (document.documentElement.dataset.theme !== theme) {
    document.documentElement.dataset.theme = theme;
    restyleMap();
  }
  applyAccent();
}

// Accent color: a preset, or "album" (the playing cover's most vivid color). Blue is the theme's own.
const ACCENTS = { blue: null, teal: [20, 184, 166], green: [34, 197, 94], orange: [245, 158, 11], red: [239, 68, 68],
  purple: [168, 85, 247], pink: [236, 72, 153] };
function applyAccent() {
  const root = document.documentElement.style;
  let rgb = settings.accent === "album" ? media?.albumRGB : ACCENTS[settings.accent];
  if (!rgb) {
    root.removeProperty("--accent");
    root.removeProperty("--accent-text");
    return;
  }
  // Keep it readable: lighter on the dark theme, darker on the light one.
  const lum = (c) => (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255;
  const dark = document.documentElement.dataset.theme !== "light";
  for (let i = 0; i < 6 && (dark ? lum(rgb) < 0.42 : lum(rgb) > 0.5); i++) {
    rgb = rgb.map((v) => Math.round(dark ? v + (255 - v) * 0.18 : v * 0.82));
  }
  root.setProperty("--accent", `rgb(${rgb.join(" ")})`);
  root.setProperty("--accent-text", lum(rgb) > 0.6 ? "#11151a" : "#ffffff");
}

// ---- views ----
let currentView = "home";
function showView(name) {
  if (name !== "home") setPanel(false, false);  // the other views live in the panel
  currentView = name;
  document.body.dataset.view = name;  // the media view widens the panel (style.css); the map has to follow
  if (map) setTimeout(() => map.resize(), 280);
  for (const b of $$(".rail-btn[data-view]")) b.classList.toggle("active", b.dataset.view === name);
  for (const v of $$(".view")) v.classList.toggle("active", v.id === "view-" + name);
  setCameras(name === "cameras");
  if (name === "media") loadMedia();
  if (name === "radio") loadRadio();
  if (name === "weather") loadWeather();
  if (name === "assistant") renderChat();
  if (name === "settings") { paintBt(); paintWifi(); loadTrip(); }
  if (name === "emergency") loadEmergency();
  if (name === "news") loadNews();
  renderNav();
}

// ---- map: MapLibre with the offline vector map and imagery when installed, else online OSM tiles ----
let lastPos = load("lastPos", null);
let offlineData = { maps: [], imagery: {} };
let satellite = load("satellite", false);
let home = load("home", null);  // {name, address, lat, lon}: Settings → Home
let radarOn = load("radar", false);
// Elevation layers (server.py /terrain: the downloaded DEM, else AWS when online). mlcontour makes contour lines
// from the same tiles in a worker.
const terrain = { hillshade: false, relief: false, contours: false, "3d": false, tsunami: false, quakes: false, ...load("terrainLayers", {}) };
let dem = null;  // mlcontour.DemSource, made in initMap
const DEM_MAXZOOM = 11;  // NEXRAD radar over the map (radio.py radar_tile), new frames every 5 min
const radarStamp = () => Math.floor(Date.now() / 300000);
let map = null, carMarker = null, destMarker = null, heading = 0, follow = true;
const pmProtocol = new pmtiles.Protocol();
maplibregl.addProtocol("pmtiles", pmProtocol.tile);

const nav = { route: null, dest: null, active: false, idx: 0, offCount: 0, lastReroute: 0, spoken: {} };

function routeColors() {
  const cs = getComputedStyle(document.documentElement);
  return { line: cs.getPropertyValue("--route").trim(), casing: cs.getPropertyValue("--route-casing").trim() };
}
function routeGeojson() {
  const features = nav.route ? [{ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: nav.route.coords.map(([la, lo]) => [lo, la]) } }] : [];
  return { type: "FeatureCollection", features };
}
function imageryLayers(sources) {
  // Coarse region-wide photos underneath, sharper home-area photos (limited by their bounds) on top.
  return Object.entries(offlineData.imagery).sort((a, b) => a[1].maxzoom - b[1].maxzoom).map(([name, m]) => {
    sources["img-" + name] = { type: "raster", tiles: [`${location.origin}/imagery/${name}/{z}/{x}/{y}.jpg`], tileSize: 256,
      minzoom: m.minzoom, maxzoom: m.maxzoom, bounds: m.bounds, attribution: "Imagery: USGS The National Map" };
    return { id: "img-" + name, type: "raster", source: "img-" + name, minzoom: m.minzoom, paint: { "raster-fade-duration": 0 } };
  });
}
function buildStyle() {
  const theme = document.documentElement.dataset.theme === "light" ? "light" : "dark";
  const origin = location.origin, c = routeColors(), sources = {};
  let layers;
  if (offlineData.maps.length) {
    sources.protomaps = { type: "vector", url: `pmtiles://${origin}/maps/${offlineData.maps[0]}`, attribution: "© OpenStreetMap contributors, Protomaps" };
    layers = basemaps.layers("protomaps", basemaps.namedFlavor(theme), { lang: "en" });
    if (satellite && Object.keys(offlineData.imagery).length) {
      // Hybrid: photos in place of the land and water fills, roads and labels on top.
      const firstRoad = layers.findIndex((l) => l["source-layer"] === "roads");
      layers = [...layers.slice(0, firstRoad).filter((l) => l.type === "background"), ...imageryLayers(sources),
        ...layers.slice(firstRoad).filter((l) => l["source-layer"] !== "buildings")];
    }
  } else {
    sources.osm = { type: "raster", tiles: [`${origin}/tiles/{z}/{x}/{y}.png`], tileSize: 256, maxzoom: 19, attribution: "© OpenStreetMap contributors" };
    // Swapping brightness min and max inverts the tiles for night; the hue turn keeps water blue.
    const night = { "raster-brightness-min": 0.9, "raster-brightness-max": 0.08, "raster-hue-rotate": 180, "raster-saturation": -0.45 };
    layers = [{ id: "osm", type: "raster", source: "osm", paint: theme === "dark" ? night : {} }];
    if (satellite) layers.push(...imageryLayers(sources));
  }
  if (dem && (terrain.hillshade || terrain.relief || terrain.contours || terrain["3d"])) {
    sources.dem = { type: "raster-dem", encoding: "terrarium", tiles: [dem.sharedDemProtocolUrl], tileSize: 256, maxzoom: DEM_MAXZOOM,
      attribution: "Terrain: Mapzen, USGS 3DEP, SRTM" };
    const dark = theme === "dark", under = [];
    if (terrain.relief) under.push({ id: "relief", type: "color-relief", source: "dem", paint: {
      "color-relief-opacity": dark ? 0.45 : 0.55,
      "color-relief-color": ["interpolate", ["linear"], ["elevation"], 0, "#2e7d4f", 300, "#6ea64b", 700, "#c7b95a",
        1200, "#b98745", 1800, "#9a6a4a", 2500, "#d9d2cc", 3200, "#ffffff"] } });
    if (terrain.hillshade) under.push({ id: "hillshade", type: "hillshade", source: "dem", paint: {
      "hillshade-exaggeration": 0.55, "hillshade-shadow-color": dark ? "#000000" : "#473b24",
      "hillshade-highlight-color": dark ? "#5a5a5a" : "#ffffff", "hillshade-accent-color": dark ? "#000000" : "#5c4b2f" } });
    const roads = layers.findIndex((l) => l["source-layer"] === "roads" || l.id === "route-casing");
    layers.splice(roads < 0 ? layers.length : roads, 0, ...under);
    if (terrain.contours) {
      const ft = settings.units === "imperial";
      sources.contours = { type: "vector", maxzoom: 15, tiles: [dem.contourProtocolUrl({
        multiplier: ft ? 3.28084 : 1, elevationKey: "ele", levelKey: "level", contourLayer: "contours",
        // [minor, major] interval per zoom
        thresholds: ft ? { 9: [1000, 5000], 10: [500, 2000], 11: [200, 1000], 12: [100, 500], 14: [40, 200], 15: [20, 100] }
          : { 9: [300, 1500], 10: [200, 1000], 11: [100, 500], 12: [50, 250], 14: [20, 100], 15: [10, 50] } })] };
      const line = dark ? "rgba(255, 214, 150, 0.32)" : "rgba(120, 82, 40, 0.38)";
      const labels = layers.findIndex((l) => l.type === "symbol");
      layers.splice(labels < 0 ? layers.length : labels, 0,
        { id: "contours", type: "line", source: "contours", "source-layer": "contours",
          paint: { "line-color": line, "line-width": ["match", ["get", "level"], 1, 1.2, 0.6] } });
      layers.push({ id: "contour-labels", type: "symbol", source: "contours", "source-layer": "contours",
        filter: [">", ["get", "level"], 0], layout: { "symbol-placement": "line", "text-size": 11,
          "text-field": ["concat", ["number-format", ["get", "ele"], {}], ft ? " ft" : " m"], "text-font": ["Noto Sans Regular"] },
        paint: { "text-color": dark ? "#e8c890" : "#6b4a22", "text-halo-color": dark ? "#000" : "#fff", "text-halo-width": 1 } });
    }
  }
  if (terrain.tsunami) {  // evacuation zones by risk, routes, where to go (WA DNR, OR DOGAMI; tools/fetch_emergency.py)
    for (const n of ["zones", "routes", "assembly", "sirens"]) sources[`em-${n}`] = { type: "geojson", data: `${origin}/emergency/${n}.geojson` };
    const labels = layers.findIndex((l) => l.type === "symbol");
    layers.splice(labels < 0 ? layers.length : labels, 0,
      { id: "em-zones", type: "fill", source: "em-zones", paint: { "fill-color": ["match", ["get", "zone"], "local", "#fb8c00", "#e53935"],
        "fill-opacity": ["match", ["get", "zone"], "local", 0.18, 0.28] } },
      { id: "em-zones-edge", type: "line", source: "em-zones", paint: { "line-color": "#e53935", "line-width": 1, "line-opacity": 0.6 } },
      { id: "em-routes", type: "line", source: "em-routes", layout: { "line-cap": "round" },
        paint: { "line-color": "#2ecc71", "line-width": ["interpolate", ["linear"], ["zoom"], 10, 2, 16, 6], "line-dasharray": [2, 1] } });
    layers.push(
      { id: "em-sirens", type: "circle", source: "em-sirens", minzoom: 11, paint: { "circle-radius": 4, "circle-color": "#ffd54f", "circle-stroke-width": 1, "circle-stroke-color": "#000" } },
      { id: "em-assembly", type: "circle", source: "em-assembly", paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, 4, 15, 9],
        "circle-color": ["match", ["get", "kind"], "Vertical evacuation structure", "#9c27b0", "Post-tsunami assembly area", "#2ecc71", "Post-tsunami shelter", "#2ecc71", "#4c8dff"],
        "circle-stroke-width": 2, "circle-stroke-color": "#fff" } },
      { id: "em-assembly-label", type: "symbol", source: "em-assembly", minzoom: 12, layout: { "text-field": ["get", "name"], "text-size": 12,
        "text-offset": [0, 1.2], "text-anchor": "top", "text-font": ["Noto Sans Medium"] },
        paint: { "text-color": theme === "dark" ? "#9ff0c0" : "#106b35", "text-halo-color": theme === "dark" ? "#000" : "#fff", "text-halo-width": 1.2 } });
  }
  if (terrain.quakes) {  // USGS, past week, M2.5+: size by magnitude, red when recent
    sources.quakes = { type: "geojson", data: `${origin}/api/quakes`, attribution: "Earthquakes: USGS" };
    layers.push({ id: "quakes", type: "circle", source: "quakes", paint: {
      "circle-radius": ["interpolate", ["exponential", 1.6], ["get", "mag"], 2.5, 4, 5, 12, 7, 26],
      "circle-color": ["interpolate", ["linear"], ["get", "hours"], 0, "#ff1744", 24, "#ff9100", 168, "#ffd54f"],
      "circle-opacity": 0.65, "circle-stroke-width": 1, "circle-stroke-color": "#000" } });
  }
  // LoRa mesh nodes with a position (mesh_bridge.py); filled from the live state
  sources.mesh = { type: "geojson", data: meshGeojson() };
  layers.push(
    { id: "mesh", type: "circle", source: "mesh", paint: { "circle-radius": 7, "circle-color": ["case", ["==", ["get", "old"], 1], "#8a8f98", "#00bfa5"],
      "circle-stroke-width": 2, "circle-stroke-color": "#fff" } },
    { id: "mesh-label", type: "symbol", source: "mesh", layout: { "text-field": ["get", "short"], "text-size": 12, "text-offset": [0, 1.3],
      "text-anchor": "top", "text-font": ["Noto Sans Medium"] },
      paint: { "text-color": theme === "dark" ? "#a7ffeb" : "#00695c", "text-halo-color": theme === "dark" ? "#000" : "#fff", "text-halo-width": 1.2 } });
  if (radarOn) {  // under the labels, over everything else
    sources.radar = { type: "raster", tiles: [`${origin}/wx/radar/{z}/{x}/{y}.png?t=${radarStamp()}`], tileSize: 256, maxzoom: 10,
      attribution: "Radar: NOAA/NWS" };
    const at = layers.findIndex((l) => l.type === "symbol");
    layers.splice(at < 0 ? layers.length : at, 0, { id: "radar", type: "raster", source: "radar",
      paint: { "raster-opacity": 0.72, "raster-fade-duration": 0 } });
  }
  sources.route = { type: "geojson", data: routeGeojson() };
  const lineLayout = { "line-join": "round", "line-cap": "round" };
  layers.push(
    { id: "route-casing", type: "line", source: "route", layout: lineLayout, paint: { "line-color": c.casing, "line-width": ["interpolate", ["linear"], ["zoom"], 8, 5, 17, 18] } },
    { id: "route-line", type: "line", source: "route", layout: lineLayout, paint: { "line-color": c.line, "line-width": ["interpolate", ["linear"], ["zoom"], 8, 3, 17, 11] } },
  );
  return { version: 8, glyphs: `${origin}/vendor/map-assets/fonts/{fontstack}/{range}.pbf`,
    sprite: `${origin}/vendor/map-assets/sprites/${theme}`, sources, layers,
    ...(sources.dem && terrain["3d"] ? { terrain: { source: "dem", exaggeration: 1.4 } } : {}) };
}
function restyleMap() {
  if (map) map.setStyle(buildStyle());
}
function toggleLayer(key) {
  if (key === "satellite") return setSatellite(!satellite), paintLayers();
  if (key === "radar") return setRadar(!radarOn), paintLayers();
  terrain[key] = !terrain[key];
  save("terrainLayers", terrain);
  if (key === "3d" && terrain["3d"]) map.easeTo({ pitch: Math.max(map.getPitch(), 55), duration: 600 });
  paintLayers();
  restyleMap();
}
function paintLayers() {
  const on = { satellite, radar: radarOn, ...terrain };
  for (const b of $$("#layers-menu [data-layer]")) b.classList.toggle("on", !!on[b.dataset.layer]);
  $('#layers-menu [data-layer="satellite"]').hidden = !Object.keys(offlineData.imagery || {}).length;
  $("#layers-btn").classList.toggle("on", Object.values(on).some(Boolean));
}
function setRadar(on) {
  radarOn = on;
  save("radar", on);
  paintRadarChip();
  restyleMap();
}
function paintRadarChip() {
  const chip = $("#radar-chip");
  chip.hidden = !radarOn;
  chip.querySelector("span").textContent = `Radar · ${fmtClock(new Date(radarStamp() * 300000), true)}`;
}
setInterval(() => {  // a new radar frame every 5 minutes: point the source at the new tiles
  if (!radarOn || !map?.getSource("radar") || map.getSource("radar").tiles[0].endsWith(`t=${radarStamp()}`)) return;
  map.getSource("radar").setTiles([`${location.origin}/wx/radar/{z}/{x}/{y}.png?t=${radarStamp()}`]);
  paintRadarChip();
}, 30000);
function setSatellite(on) {
  satellite = on;
  save("satellite", on);
  restyleMap();
}

async function initMap() {
  try { offlineData = await api("/api/offline"); } catch { /* old server: online tiles only */ }
  dem = new mlcontour.DemSource({ url: `${location.origin}/terrain/{z}/{x}/{y}.png`, encoding: "terrarium",
    maxzoom: DEM_MAXZOOM, worker: true });
  dem.setupMaplibre(maplibregl);
  paintLayers();
  paintRadarChip();
  map = new maplibregl.Map({
    container: "map", style: buildStyle(), attributionControl: false, maxPitch: 60, fadeDuration: 0,
    // where the truck last was, else home, else the whole US until the GPS has a fix
    center: lastPos ? [lastPos.lon, lastPos.lat] : home ? [home.lon, home.lat] : [-98.6, 39.8], zoom: lastPos ? 16 : home ? 12 : 3.5,
  });
  map.addControl(new maplibregl.AttributionControl({ compact: true }), "top-right");
  map.on("dragstart", () => setFollow(false));
  map.on("rotatestart", (e) => { if (e.originalEvent) setFollow(false); });
  map.on("contextmenu", (e) => previewRoute({ name: "Dropped pin", address: `${e.lngLat.lat.toFixed(5)}, ${e.lngLat.lng.toFixed(5)}`, lat: e.lngLat.lat, lon: e.lngLat.lng }));
}

function setFollow(on) {
  follow = on;
  $("#recenter").hidden = on;
}

function followView(ll) {
  // Navigating: heading-up and tilted, with the car low on the screen so more road ahead shows.
  const h = map.getContainer().clientHeight;
  const navigating = nav.active;
  map.easeTo({
    center: [ll[1], ll[0]], duration: 500, easing: (t) => t, essential: true,
    bearing: navigating && settings.orient !== "north" ? heading : 0,
    pitch: navigating ? 45 : 0,
    padding: { top: navigating ? h * 0.4 : 0, bottom: 0, left: 0, right: 0 },
  });
}

function updatePosition(p) {
  $("#no-fix").hidden = !!p;
  if (!p || !map) return;
  const ll = [p.lon, p.lat];
  const moving = (p.speed_mps ?? 0) > 1.5;
  if (moving && p.track_deg != null) heading = p.track_deg;
  if (!carMarker) {
    const el = document.createElement("div");
    el.className = "car-arrow";
    el.innerHTML = `<svg viewBox="0 0 48 48"><path d="M24 3 41 43 24 34 7 43z" style="fill:var(--accent)" stroke="#fff" stroke-width="3" stroke-linejoin="round"/></svg>`;
    carMarker = new maplibregl.Marker({ element: el, rotationAlignment: "map", pitchAlignment: "map" }).setLngLat(ll).addTo(map);
  } else carMarker.setLngLat(ll);
  carMarker.setRotation(heading);
  if (!lastPos || Math.abs(lastPos.lat - p.lat) + Math.abs(lastPos.lon - p.lon) > 0.0005) save("lastPos", { lat: p.lat, lon: p.lon });
  const first = !lastPos;
  lastPos = { lat: p.lat, lon: p.lon };
  if (first) map.jumpTo({ center: ll, zoom: 16 });
  else if (follow) followView([p.lat, p.lon]);
  if (nav.active) navProgress(p);
}

// ---- navigation ----
function clearRoute() {
  map.getSource("route")?.setData(routeGeojson());
  destMarker?.remove();
  destMarker = null;
}
function drawRoute(r) {
  map.getSource("route")?.setData(routeGeojson());
  destMarker?.remove();
  const el = document.createElement("div");
  el.className = "dest-marker";
  el.innerHTML = `<svg><use href="#i-gps"/></svg>`;
  const end = r.coords[r.coords.length - 1];
  destMarker = new maplibregl.Marker({ element: el, anchor: "bottom" }).setLngLat([end[1], end[0]]).addTo(map);
}

function prepareRoute(r) {
  // Flat local projection in meters, cumulative distance per vertex, and where each step falls on the line.
  const lat0 = r.coords[0][0], kx = 111320 * Math.cos(lat0 * RAD), ky = 110540;
  r.proj = (lat, lon) => [lon * kx, lat * ky];
  r.pts = r.coords.map(([la, lo]) => r.proj(la, lo));
  r.cum = [0];
  for (let i = 1; i < r.pts.length; i++) r.cum.push(r.cum[i - 1] + Math.hypot(r.pts[i][0] - r.pts[i - 1][0], r.pts[i][1] - r.pts[i - 1][1]));
  let from = 0;
  r.stepAlong = r.steps.map((s) => {
    const p = r.proj(s.lat, s.lon);
    let best = from, bd = Infinity;
    for (let i = from; i < r.pts.length; i++) {
      const d = Math.hypot(r.pts[i][0] - p[0], r.pts[i][1] - p[1]);
      if (d < bd) { bd = d; best = i; }
      if (d > bd + 3000) break;
    }
    from = best;
    return r.cum[best];
  });
  return r;
}

function snap(r, p, startIdx) {
  const q = r.proj(p.lat, p.lon);
  const scan = (a, b) => {
    let best = { dist: Infinity, idx: 0, along: 0 };
    for (let i = a; i < b; i++) {
      const [ax, ay] = r.pts[i], [bx, by] = r.pts[i + 1];
      const dx = bx - ax, dy = by - ay, len2 = dx * dx + dy * dy || 1;
      const t = Math.max(0, Math.min(1, ((q[0] - ax) * dx + (q[1] - ay) * dy) / len2));
      const d = Math.hypot(q[0] - (ax + t * dx), q[1] - (ay + t * dy));
      if (d < best.dist) best = { dist: d, idx: i, along: r.cum[i] + t * Math.sqrt(len2) };
    }
    return best;
  };
  const n = r.pts.length - 1;
  let s = scan(Math.max(0, startIdx - 3), Math.min(n, startIdx + 400));
  if (s.dist > 100) s = scan(0, n);
  return s;
}

async function fetchRoute(dest) {
  if (!lastPos) throw new Error("No GPS fix yet");
  const units = settings.units === "imperial" ? "mi" : "km";
  const r = await api(`/api/route?from=${lastPos.lat},${lastPos.lon}&to=${dest.lat},${dest.lon}&units=${units}`);
  return prepareRoute(r);
}

async function previewRoute(dest) {
  closeSearch();
  showView("home");
  try {
    toast("Finding route…", 8000);
    const r = await fetchRoute(dest);
    $("#toast").hidden = true;
    Object.assign(nav, { route: r, dest, active: false });
    drawRoute(r);
    setFollow(false);
    const b = new maplibregl.LngLatBounds();
    r.coords.forEach(([la, lo]) => b.extend([lo, la]));
    map.fitBounds(b, { padding: 70, bearing: 0, pitch: 0, duration: 800 });
    renderNav();
  } catch (e) {
    toast(`Route failed: ${e.message}`);
  }
}

function startNav() {
  if (!nav.route) return;
  Object.assign(nav, { active: true, idx: 0, offCount: 0, spoken: {}, saidArrival: false, lastReroute: Date.now() });
  addRecent(nav.dest);
  map.easeTo({ zoom: 17, duration: 600 });
  setFollow(true);
  if (lastPos) followView([lastPos.lat, lastPos.lon]);
  const r = nav.route, first = r.steps[0];
  prefetchPrompts(r, lastState?.position?.speed_mps ?? 0);
  const eta = fmtClock(new Date(Date.now() + r.duration * 1000), true);
  say(`Starting route to ${nav.dest.name}: ${fmtDur(r.duration).replace("min", "minutes").replace(" h ", " hours ")}, arriving at ${eta}. ` +
      `${first?.verbal_pre || first?.instruction || ""} ${r.stepAlong[1] > 400 ? first?.verbal_post || "" : ""}`.trim());
  renderNav();
}

function endNav(arrived = false) {
  const last = nav.route?.steps[nav.route.steps.length - 1];
  Object.assign(nav, { route: null, dest: null, active: false });
  clearRoute();
  if (arrived && !nav.saidArrival) say(last?.type === "arrive" && last.instruction ? `${last.instruction}.` : "You have arrived");
  map.easeTo({ bearing: 0, pitch: 0, padding: { top: 0, bottom: 0, left: 0, right: 0 }, duration: 600 });
  setFollow(true);
  renderNav();
}

function navProgress(p) {
  const r = nav.route;
  const s = snap(r, p, nav.idx);
  nav.idx = s.idx;
  nav.along = s.along;
  // Off the line for three fixes in a row: ask for a new route from here (at most every 20 s).
  nav.offCount = s.dist > 60 ? nav.offCount + 1 : 0;
  if (nav.offCount >= 3 && Date.now() - nav.lastReroute > 20000) {
    nav.lastReroute = Date.now();
    toast("Rerouting…");
    fetchRoute(nav.dest).then((nr) => {
      if (!nav.active) return;
      Object.assign(nav, { route: nr, idx: 0, offCount: 0, spoken: {} });
      drawRoute(nr);
      prefetchPrompts(nr, p.speed_mps ?? 0);
      say(`Rerouting. ${nr.steps[1]?.verbal_alert || nr.steps[0]?.verbal_post || nr.steps[0]?.instruction || ""}`);
    }).catch(() => {});
  }
  const remaining = r.cum[r.cum.length - 1] - s.along;
  if (remaining < 25 && s.dist < 60) return endNav(true);

  const k = r.stepAlong.findIndex((a, i) => i > 0 && a > s.along + 5);
  nav.step = k;
  if (k > 0) promptTurn(r, k, r.stepAlong[k] - s.along, p.speed_mps ?? 0);
  renderNav();
}

// Turn-by-turn voice for maneuver k, toTurn metres ahead at speed v (m/s). Up to three prompts, timed by speed:
// early (about 2 mi out on the highway, half a mile in town), approaching, and now (about 5 s before). The leg
// after a turn starts with "Continue for 3 miles" when it's long. Each prompt is said once; passing a later
// stage skips the earlier ones, and short legs get only the "now" prompt (with "then ..." for a quick next turn).
const promptStages = (v) => ({ early: v > 22 ? 3200 : v > 13 ? 1200 : 600, near: v > 22 ? 800 : 220, now: Math.min(160, Math.max(40, v * 5)) });
// The words for maneuver k: "now", and "in <distance>, ..." for a distance d metres out.
function promptText(r, k, stage, d) {
  const st = r.steps[k], lower = (t) => t && t[0].toLowerCase() + t.slice(1);
  if (stage === "now") {
    const next = r.steps[k + 1];
    const then = next && next.type !== "arrive" && r.stepAlong[k + 1] - r.stepAlong[k] < 250 ? `, then ${lower(next.instruction)}` : "";
    return `${(st.verbal_pre || st.instruction).replace(/\.$/, "")}${then}`;
  }
  return `In ${spokenDist(d)}, ${lower(st.verbal_alert || st.instruction).replace(/^in [\d.]+ (?:miles?|feet|kilometers?|meters?), /, "")}`;
}
// Said at the stage's distance when it's just been crossed (the version made ahead), else at the real distance.
const promptDist = (toTurn, at) => (toTurn > at * 0.8 ? at : toTurn);
function promptTurn(r, k, toTurn, v) {
  const st = r.steps[k], prev = r.steps[k - 1], leg = r.stepAlong[k] - r.stepAlong[k - 1];
  const { early, near, now } = promptStages(v);
  if (leg - toTurn < 120 && leg > 3000 && !nav.spoken[k + "c"]) {  // just turned onto a long stretch
    nav.spoken[k + "c"] = true;
    if (prev.verbal_post) return say(prev.verbal_post);
  }
  if (toTurn <= now) {
    if (nav.spoken[k + "n"]) return;
    nav.spoken[k + "n"] = nav.spoken[k + "a"] = nav.spoken[k + "e"] = true;
    if (st.type === "arrive") nav.saidArrival = true;
    return say(promptText(r, k, "now"));
  }
  if (toTurn <= near && leg > near + 120) {
    if (nav.spoken[k + "a"]) return;
    nav.spoken[k + "a"] = nav.spoken[k + "e"] = true;
    return say(promptText(r, k, "near", promptDist(toTurn, near)));
  }
  if (toTurn <= early && leg > early + 150 && toTurn > near * 1.5 && !nav.spoken[k + "e"]) {
    nav.spoken[k + "e"] = true;
    say(promptText(r, k, "early", promptDist(toTurn, early)));
  }
}
// Every prompt a route will need, made ahead by the voice (Kokoro takes about as long as the sentence lasts).
function prefetchPrompts(r, v) {
  const { early, near } = promptStages(v), texts = [];
  for (let k = 1; k < r.steps.length; k++) {
    const leg = r.stepAlong[k] - r.stepAlong[k - 1];
    texts.push(promptText(r, k, "now"));
    if (leg > near + 120) texts.push(promptText(r, k, "near", near));
    if (leg > early + 150) texts.push(promptText(r, k, "early", early));
    if (leg > 3000 && r.steps[k - 1].verbal_post) texts.push(r.steps[k - 1].verbal_post);
  }
  const last = r.steps[r.steps.length - 1];
  if (last?.instruction) texts.push(`${last.instruction}.`);
  api("/api/say/prefetch", { json: { texts, voice: settings.voice } }).catch(() => {});
}

function maneuverIcon(svg, step) {
  let id = "i-turn", rot = 0;
  if (!step) { /* keep defaults */ } else if (step.type === "arrive") id = "i-flag";
  else if (["roundabout", "rotary", "roundabout turn"].includes(step.type)) id = "i-roundabout";
  else if (step.modifier === "uturn") id = "i-uturn";
  else rot = { "slight right": 40, right: 90, "sharp right": 135, "slight left": -40, left: -90, "sharp left": -135 }[step.modifier] ?? 0;
  svg.querySelector("use").setAttribute("href", "#" + id);
  svg.style.transform = `rotate(${rot}deg)`;
}

function renderNav() {
  const card = $("#nav-card");
  const mode = nav.active ? "active" : nav.route ? "preview" : "idle";
  $(".nav-idle", card).hidden = mode !== "idle";
  $(".nav-preview", card).hidden = mode !== "preview";
  $(".nav-active", card).hidden = mode !== "active";
  $("#map-search").hidden = mode === "active";
  $("#map-banner").hidden = !(mode === "active" && currentView !== "home");
  if (mode === "idle") return renderRecents();
  const r = nav.route;
  if (mode === "preview") {
    $("#pv-name").textContent = nav.dest.name;
    $("#pv-time").textContent = fmtDur(r.duration);
    $("#pv-dist").textContent = distText(r.distance) + (nav.dest.address ? ` · ${nav.dest.address}` : "");
    return;
  }
  const along = nav.along ?? 0, total = r.cum[r.cum.length - 1];
  const k = nav.step > 0 ? nav.step : Math.min(1, r.steps.length - 1);
  const step = r.steps[k];
  const toTurn = Math.max(0, (r.stepAlong[k] ?? total) - along);
  const leftS = r.duration * Math.max(0, total - along) / total;
  for (const [icon, dist, text] of [["#man-icon", "#man-dist", "#man-text"], ["#banner-icon", "#banner-dist", "#banner-text"]]) {
    maneuverIcon($(icon), step);
    $(dist).textContent = distText(toTurn);
    $(text).textContent = step?.instruction || "";
  }
  const next = r.steps[k + 1];
  $("#nav-then").textContent = next && r.stepAlong[k + 1] - r.stepAlong[k] < 300 ? `Then: ${next.instruction}` : "";
  $("#nav-eta").textContent = fmtClock(new Date(Date.now() + leftS * 1000));
  $("#nav-left-time").textContent = fmtDur(leftS);
  const d = fmtDist(Math.max(0, total - along));
  $("#nav-left-dist").textContent = d.v;
  $("#nav-left-unit").textContent = d.u;
}

// recent destinations
function addRecent(dest) {
  if (dest === home) return;  // home has its own button
  const list = load("recents", []).filter((d) => d.name !== dest.name || Math.abs(d.lat - dest.lat) > 1e-4);
  list.unshift({ name: dest.name, address: dest.address || "", lat: dest.lat, lon: dest.lon });
  save("recents", list.slice(0, 8));
}
// Home: one tap (or "truck, take me home") to navigate there. Kept here and on the Pi, which uses it for
// weather and satellite passes when there's no GPS fix.
function setHome(place) {
  const label = place && (place.address || (place.name !== "Home" && place.name !== "Dropped pin" ? place.name : ""));
  home = place && { name: "Home", address: label || "", lat: place.lat, lon: place.lon };
  save("home", home);
  api("/api/home", { json: home || {} }).catch(() => {});
  toast(home ? `Home set${home.address ? `: ${home.address}` : ""}` : "Home cleared", 2000);
  renderRecents();
  paintHome();
}
function goHome() {
  if (!home) { toast("No home set yet: Settings → Home, or Set as home on a route"); return false; }
  previewRoute(home).then(() => { if (nav.route && !nav.active) startNav(); });
  return true;
}
function paintHome() {
  $("#home-addr").textContent = home ? home.address || `${home.lat.toFixed(5)}, ${home.lon.toFixed(5)}` : "Not set";
  $("#home-clear").hidden = !home;
}
function renderRecents() {
  const box = $("#recents");
  const homeBtn = document.createElement("button");
  homeBtn.className = "recent home";
  homeBtn.innerHTML = `<svg><use href="#i-home"/></svg><span></span>`;
  homeBtn.querySelector("span").textContent = home ? `Home${home.address ? ` · ${home.address}` : ""}` : "Set a home address";
  homeBtn.onclick = () => (home ? goHome() : showView("settings"));
  box.replaceChildren(homeBtn, ...load("recents", []).filter((d) => !home || Math.hypot(d.lat - home.lat, d.lon - home.lon) > 2e-4).slice(0, 3).map((d) => {
    const b = document.createElement("button");
    b.className = "recent";
    b.innerHTML = `<svg><use href="#i-star"/></svg><span></span>`;
    b.querySelector("span").textContent = d.name + (d.address ? ` · ${d.address}` : "");
    b.onclick = () => previewRoute(d);
    return b;
  }));
}

// ---- search overlay with on-screen keyboard ----
let query = "", kbLayer = "abc", searchMode = "places";  // places (map) or music
const LAYERS = {
  abc: ["qwertyuiop", "asdfghjkl", "zxcvbnm"],
  ABC: ["QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM"],
  num: ["1234567890", "-/:;()$&@\"", ".,?!'_#%*+"],
  sym: ["[]{}^~<>=|", "\\€£¥•`", ".,?!'_#%*+"],
};
let kbShiftOnce = false;  // Shift for one letter; tap twice (caps) to stay
function openSearch(text = "", mode = "places") {
  query = text;
  searchMode = mode;
  $("#search-overlay").dataset.mode = mode;
  $("#search-text").textContent = query;
  $("#search-overlay").hidden = false;
  renderKeyboard();
  if (mode === "music") return loadMedia().then(() => showMusicSearch(query));
  if (mode === "pass") { showTyped(); return ($("#search-results").innerHTML = `<div class="empty">Password for “${escapeHtml(wifiTarget)}”, then Join.</div>`); }
  if (mode === "vibe") return ($("#search-results").innerHTML = `<div class="empty">Describe it: “chill night drive”, “90s singalong”, “something like Fleetwood Mac”…</div>`);
  if (mode === "odo") return ($("#search-results").innerHTML = `<div class="empty">The odometer reading in ${settings.units === "imperial" ? "miles" : "km"}, then Set.</div>`);
  if (mode === "mesh") return ($("#search-results").innerHTML = `<div class="empty">${mesh.to ? `To ${escapeHtml(nodeName(mesh.to))} only` : "To everyone on the mesh"} · up to 228 characters, then Send.</div>`);
  if (mode === "feed") return ($("#search-results").innerHTML = `<div class="empty">The feed's address, like npr.org/rss, then Add.</div>`);
  if (mode === "plname") return ($("#search-results").innerHTML = `<div class="empty">Name the playlist, then Save.</div>`);
  if (mode === "chat") return ($("#search-results").innerHTML = `<div class="empty">Type a question, then tap Send.</div>`);
  if (mode === "freq") return ($("#search-results").innerHTML = `<div class="empty">Type a frequency: 98.5 (FM), 1190 (AM, in kHz), 155.34 or 162.55 (narrow FM).</div>`);
  if (query) runSearch(); else showSearchRecents();
}
function closeSearch() { $("#search-overlay").hidden = true; }
function renderKeyboard() {
  const kb = $("#keyboard"), letters = kbLayer === "abc" || kbLayer === "ABC";
  const rows = LAYERS[kbLayer].map((chars, i) => {
    const keys = [...chars].map((c) => `<button class="key" data-k="${c.replace(/&/g, "&amp;").replace(/"/g, "&quot;")}">${escapeHtml(c)}</button>`);
    if (i === 2) {
      keys.unshift(letters ? `<button class="key wide${kbLayer === "ABC" ? " on" : ""}" data-k="shift" aria-label="Shift">⇧</button>`
        : `<button class="key wide" data-k="sympage">${kbLayer === "num" ? "#+=" : "123"}</button>`);
      keys.push(`<button class="key wide" data-k="back" aria-label="Delete"><svg><use href="#i-back"/></svg></button>`);
    }
    return `<div class="kb-row">${keys.join("")}</div>`;
  });
  const go = { chat: "Send", pass: "Join", vibe: "Mix", plname: "Save", feed: "Add", mesh: "Send", odo: "Set" }[searchMode] || "Search";
  rows.push(`<div class="kb-row"><button class="key wide" data-k="layer">${letters ? "123" : "ABC"}</button>
    <button class="key space" data-k=" ">space</button><button class="key wide go" data-k="go">${go}</button></div>`);
  kb.innerHTML = rows.join("");
}
function showTyped() {  // passwords show as dots, the last character for a moment
  $("#search-text").textContent = searchMode === "pass" ? "•".repeat(Math.max(0, query.length - 1)) + query.slice(-1) : query;
}
$("#keyboard").addEventListener("click", (e) => {
  const k = e.target.closest(".key")?.dataset.k;
  if (k == null) return;
  if (k === "back") query = query.slice(0, -1);
  else if (k === "layer") { kbLayer = kbLayer === "abc" || kbLayer === "ABC" ? "num" : "abc"; return renderKeyboard(); }
  else if (k === "shift") { kbShiftOnce = kbLayer === "abc"; kbLayer = kbLayer === "abc" ? "ABC" : "abc"; return renderKeyboard(); }
  else if (k === "sympage") { kbLayer = kbLayer === "num" ? "sym" : "num"; return renderKeyboard(); }
  else if (k === "go") return runSearch();
  else {
    query += k;
    if (kbShiftOnce && kbLayer === "ABC") { kbShiftOnce = false; kbLayer = "abc"; renderKeyboard(); }
  }
  showTyped();
  if (searchMode === "music") showMusicSearch(query.trim());  // the library is local: filter as you type
});
function showSearchRecents() {
  const list = load("recents", []);
  const box = $("#search-results");
  if (!list.length) return (box.innerHTML = `<div class="empty">Type a place, business or address, or tap the mic and say it.</div>`);
  box.replaceChildren(...list.map((d) => resultItem(d)));
}
function resultItem(d) {
  const b = document.createElement("button");
  b.className = "list-item";
  b.innerHTML = `<div><div class="t"></div><div class="s"></div></div><div class="d"></div>`;
  b.querySelector(".t").textContent = d.name;
  b.querySelector(".s").textContent = d.address || "";
  if (lastPos) {
    const m = Math.hypot((d.lat - lastPos.lat) * 110540, (d.lon - lastPos.lon) * 111320 * Math.cos(lastPos.lat * RAD));
    b.querySelector(".d").textContent = distText(m);
  }
  b.onclick = () => previewRoute(d);
  return b;
}
async function runSearch() {
  const q = query.trim();
  if (searchMode === "music") return showMusicSearch(q);
  if (searchMode === "freq") return tuneTyped(q);
  if (searchMode === "chat") { closeSearch(); return askAssistant(q); }
  if (searchMode === "pass") { closeSearch(); return wifiJoin(wifiTarget, query); }
  if (searchMode === "vibe") { closeSearch(); return q && makeVibe(q); }
  if (searchMode === "feed") { closeSearch(); return q && addFeed(q); }
  if (searchMode === "mesh") { closeSearch(); return meshSend(query); }
  if (searchMode === "odo") { closeSearch(); return setOdometer(q); }
  if (searchMode === "plname") { closeSearch(); return createPlaylist(q); }
  if (!q) return;
  const box = $("#search-results");
  box.innerHTML = `<div class="empty">Searching…</div>`;
  try {
    const pos = lastPos ? `&lat=${lastPos.lat}&lon=${lastPos.lon}` : "";
    const { results } = await api(`/api/search?q=${encodeURIComponent(q)}${pos}`);
    if (!results.length) return (box.innerHTML = `<div class="empty">No places found for “${escapeHtml(q)}”.</div>`);
    box.replaceChildren(...results.map(resultItem));
    return results;
  } catch (e) {
    box.innerHTML = `<div class="empty">Search needs internet (try the phone's hotspot).<br>${escapeHtml(e.message)}</div>`;
  }
}
const escapeHtml = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

// ---- media: library, browser, player, visualizer, video ----
const audio = $("#audio");
const media = {
  tracks: [], videos: [], artists: [], albums: [], loaded: 0,
  queue: [], order: [], pos: -1,  // queue: track indices being played; order: the queue's play order (shuffled or not)
  shuffle: load("shuffle", false), repeat: load("repeat", "off"),  // off | all | one
  tab: load("mediaTab", "artists"), stack: [],  // stack: drill-down levels inside the tab ({type, key})
  categories: {}, playlists: [], idIndex: new Map(),
  flip: false,  // the player folded or unfolded against this list's default (tapping the art or title)
};
const fold = (s) => s.normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/[‐‑–—]/g, "-").toLowerCase();
const dirOf = (id) => id.slice(0, id.lastIndexOf("/"));
const artUrl = (t) => (t?.art ? `/api/media/art?album=${encodeURIComponent(dirOf(t.id))}` : "");
const curTrack = () => media.tracks[media.queue[media.order[media.pos]]];

async function loadMedia(force) {
  if (!force && Date.now() - media.loaded < 60000) return;
  try {
    const { cols, rows, videos, categories } = await api("/api/media");
    media.tracks = rows.map((r) => {
      const t = Object.fromEntries(cols.map((c, i) => [c, r[i]]));
      t.folder = `${t.album_artist} ${t.album}`;  // what voice search matches on, with the title
      t.key = fold(`${t.title} ${t.artist} ${t.album_artist} ${t.album}`);
      return t;
    });
    media.videos = videos;
    media.categories = categories || {};
    media.idIndex = new Map(media.tracks.map((t, i) => [t.id, i]));
    await loadPlaylists();
    indexLibrary();
    media.loaded = Date.now();
    api("/api/volume").then((v) => v.level != null && bind("volume", v.level)).catch(() => {});
    renderBrowser();
  } catch (e) {
    $("#media-list").innerHTML = `<div class="empty">Couldn't load music: ${escapeHtml(e.message)}</div>`;
  }
}

// Artists and albums from the tracks: an album is (album artist, album name), tracks in disc/track order.
function indexLibrary() {
  const albums = new Map(), artists = new Map();
  media.tracks.forEach((t, i) => {
    const artist = t.album_artist || t.artist || "Unknown artist";
    const key = `${artist}\u0000${t.album}`;
    let al = albums.get(key);
    if (!al) albums.set(key, (al = { key, name: t.album || "Unknown album", artist, year: t.year, tracks: [], art: "" }));
    al.tracks.push(i);
    if (!al.art) al.art = artUrl(t);
    if (!artists.has(artist)) artists.set(artist, { key: artist, name: artist, albums: [], count: 0 });
  });
  for (const al of albums.values()) {
    al.tracks.sort((a, b) => media.tracks[a].n - media.tracks[b].n || media.tracks[a].title.localeCompare(media.tracks[b].title));
    const ar = artists.get(al.artist);
    ar.albums.push(al);
    ar.count += al.tracks.length;
  }
  const byName = (a, b) => fold(a.name).replace(/^the /, "").localeCompare(fold(b.name).replace(/^the /, ""));
  media.artists = [...artists.values()].sort(byName);
  for (const ar of media.artists) ar.albums.sort((a, b) => (a.year || 9999) - (b.year || 9999) || byName(a, b));
  media.albums = [...albums.values()].sort(byName);
  const titles = media.tracks.map((t) => fold(t.title));
  media.songs = media.tracks.map((_, i) => i).sort((a, b) => titles[a].localeCompare(titles[b]));
}

// ---- browser ----
const TABS = { artists: "Artists", albums: "Albums", songs: "Songs", browse: "Browse", playlists: "Playlists", videos: "Videos" };
function renderBrowser() {
  for (const b of $$("#media-tabs [data-tab]")) b.classList.toggle("on", b.dataset.tab === media.tab && !media.stack.length);
  const level = media.stack[media.stack.length - 1];
  layoutPlayer();
  const crumb = $("#media-crumb");
  crumb.hidden = !level;
  let rows, kind;
  if (level?.type === "artist") {
    const ar = media.artists.find((a) => a.key === level.key);
    rows = ar?.albums || [];
    kind = "album";
    $("#crumb-title").textContent = ar?.name || "";
  } else if (level?.type === "album") {
    const al = media.albums.find((a) => a.key === level.key);
    rows = al?.tracks || [];
    kind = "albumtrack";
    $("#crumb-title").textContent = al ? `${al.name}${al.year ? ` · ${al.year}` : ""}` : "";
  } else if (level?.type === "search") {
    rows = level.results;
    kind = "mixed";
    $("#crumb-title").textContent = `“${level.q}”`;
  } else if (level?.type === "tags") {  // every mood (or style...)
    rows = (media.categories?.[level.key] || []).map(([tag, n]) => ({ kind: "tag", tagKind: level.key, tag, n }));
    kind = "browse";
    $("#crumb-title").textContent = TAG_TITLES[level.key];
  } else if (level && ["tag", "mix", "playlist"].includes(level.type)) {
    rows = levelTracks(level);
    kind = "track";
    $("#crumb-title").textContent = level.type === "playlist" ? media.playlists.find((p) => p.id === level.key)?.name || "" : level.title;
  } else if (media.tab === "browse") {
    rows = browseRows();
    kind = "browse";
  } else if (media.tab === "playlists") {
    rows = media.playlists || [];
    kind = "playlist";
  } else {
    kind = { artists: "artist", albums: "album", songs: "track", videos: "video" }[media.tab];
    rows = { artists: media.artists, albums: media.albums, songs: media.songs, videos: media.videos }[media.tab] || [];
  }
  const tracksHere = !!level && !["search", "tags"].includes(level.type);
  $("#crumb-play").hidden = $("#crumb-shuffle").hidden = !tracksHere;
  $("#crumb-save").hidden = !level || level.type === "tags" || level.type === "playlist";
  $("#crumb-delete").hidden = level?.type !== "playlist";
  if (media.tab === "playlists" && !level && !rows.length) {
    return ($("#media-list").innerHTML = `<div class="empty">No playlists yet. Open an album, a mood or a vibe mix and tap <b>Save</b>, or tap ＋ while a song plays.</div>`);
  }
  if (!rows.length) {
    const msg = media.tab === "videos" && !level
      ? "No videos yet. Put video files in <b>~/Videos</b> on the Pi."
      : media.tracks.length ? "Nothing here." : "No music yet. Copy music into <b>~/Music</b> on the Pi.";
    return ($("#media-list").innerHTML = `<div class="empty">${msg}</div>`);
  }
  virtualList($("#media-list"), rows, (item) => mediaRow(kind, item));
}

function mediaRow(kind, item) {
  const b = document.createElement("button");
  b.className = "list-item media-row";
  let art = "", t = "", s = "", onclick;
  if (kind === "mixed") return mediaRow(item.kind, item.item);
  if (kind === "browse") return browseRow(item);
  if (kind === "playlist") return playlistRow(item);
  if (kind === "artist") {
    art = item.albums.find((a) => a.art)?.art || "";
    t = item.name;
    s = `${item.albums.length} album${item.albums.length === 1 ? "" : "s"} · ${item.count} song${item.count === 1 ? "" : "s"}`;
    onclick = () => drill("artist", item.key);
    b.classList.add("round");
  } else if (kind === "album") {
    art = item.art;
    t = item.name;
    s = [item.artist, item.year].filter(Boolean).join(" · ");
    onclick = () => drill("album", item.key);
  } else if (kind === "track" || kind === "albumtrack") {
    const tr = media.tracks[item];
    if (kind === "albumtrack") b.dataset.no = tr.n % 1000 || "";
    art = kind === "track" ? artUrl(tr) : "";
    t = tr.title;
    s = kind === "track" ? `${tr.artist} · ${tr.album}` : tr.artist !== tr.album_artist ? tr.artist : "";
    onclick = () => {
      const level = media.stack[media.stack.length - 1];
      const list = level ? levelTracks(level) : media.songs;
      playQueue(list, list.indexOf(item));
    };
    if (curTrack() === tr) b.classList.add("playing");
  } else if (kind === "video") {
    t = item.title;
    s = item.folder;
    onclick = () => playVideo(item);
    b.classList.add("video");
  }
  b.innerHTML = `<span class="thumb"></span><span class="txt"><span class="t"></span><span class="s"></span></span>`;
  const thumb = b.querySelector(".thumb");
  if (art) thumb.style.backgroundImage = `url("${art}")`;
  else thumb.innerHTML = kind === "albumtrack" ? `<span class="no">${b.dataset.no || ""}</span>`
    : `<svg><use href="${kind === "video" ? "#i-film" : kind === "artist" ? "#i-person" : "#i-music"}"/></svg>`;
  b.querySelector(".t").textContent = t;
  b.querySelector(".s").textContent = s;
  b.onclick = onclick;
  return b;
}

// Inside albums, artists, search results and Songs the player shrinks to a mini bar, so the list gets the room;
// tapping the art or title folds or unfolds it anywhere.
function layoutPlayer() {
  const level = media.stack[media.stack.length - 1];
  const collapse = (!!level || media.tab === "songs") !== media.flip;
  const el = $(".media-layout");
  if (el.classList.contains("collapsed") === collapse) return;
  el.classList.toggle("collapsed", collapse);
  requestAnimationFrame(() => $("#media-list").repaint?.());  // the list's height changed
}
function togglePlayer() {
  media.flip = !media.flip;
  layoutPlayer();
}

// Only the rows on screen exist, so 25,000 songs scroll as smoothly as 25. Row height comes from --row-rem.
function virtualList(box, items, render) {
  box.innerHTML = `<div class="vl-space"></div>`;
  box.scrollTop = 0;
  const space = box.firstChild;
  const css = getComputedStyle(box);
  const rowH = parseFloat(getComputedStyle(document.documentElement).fontSize) * (parseFloat(css.getPropertyValue("--row-rem")) || 4.2);
  space.style.height = `${items.length * rowH}px`;
  let shown = "";
  const paint = () => {
    const first = Math.max(0, Math.floor(box.scrollTop / rowH) - 6);
    const last = Math.min(items.length, Math.ceil((box.scrollTop + box.clientHeight) / rowH) + 6);
    if (shown === `${first}:${last}`) return;
    shown = `${first}:${last}`;
    const rows = [];
    for (let i = first; i < last; i++) {
      const el = render(items[i]);
      el.style.cssText = `position:absolute;left:0;right:0;top:${i * rowH}px;height:${rowH}px`;
      rows.push(el);
    }
    space.replaceChildren(...rows);
  };
  box.onscroll = () => requestAnimationFrame(paint);
  box.repaint = () => { shown = ""; paint(); };
  paint();
}

function drill(type, key) {
  media.stack.push({ type, key });
  media.flip = false;
  renderBrowser();
}
function mediaBack() {
  media.stack.pop();
  media.flip = false;
  renderBrowser();
}
function playLevel(shuffled) {
  const list = levelTracks(media.stack[media.stack.length - 1]);
  if (!list.length) return;
  if (shuffled !== media.shuffle) toggleShuffle();
  playQueue(list, media.shuffle ? -1 : 0);
}

// ---- browse (moods, styles, genres, decades, vibe mixes) and playlists (music.py) ----
const VIBES = ["Chill drive", "Road trip singalong", "Workout energy", "Late night", "Sunday morning", "Rainy day", "Party"];
const TAG_TITLES = { mood: "Moods", style: "Styles", decade: "Decades", genre: "Genres" };
const levelTracks = (level) => level.type === "album" ? media.albums.find((a) => a.key === level.key)?.tracks || []
  : level.type === "artist" ? (media.artists.find((a) => a.key === level.key)?.albums || []).flatMap((al) => al.tracks)
  : level.type === "search" ? level.results.filter((r) => r.kind === "track").map((r) => r.item)
  : level.type === "playlist" ? (media.playlists.find((p) => p.id === level.key)?.tracks || []).map((id) => media.idIndex.get(id)).filter((i) => i != null)
  : level.tracks || [];
function browseRows() {
  const rows = [{ kind: "vibe-ask" }, ...VIBES.map((v) => ({ kind: "vibe", text: v }))];
  for (const [k, title] of Object.entries(TAG_TITLES)) {
    const tags = media.categories?.[k] || [];
    if (!tags.length) continue;
    rows.push({ kind: "head", text: title, more: tags.length > 8 ? k : null });
    rows.push(...(k === "decade" ? tags : tags.slice(0, 8)).map(([tag, n]) => ({ kind: "tag", tagKind: k, tag, n })));
  }
  return rows;
}
function browseRow(r) {
  if (r.kind === "head") {
    const d = document.createElement("div");
    d.className = "list-head";
    d.innerHTML = `<span></span>${r.more ? `<button class="btn">All</button>` : ""}`;
    d.querySelector("span").textContent = r.text;
    if (r.more) d.querySelector("button").onclick = () => drill("tags", r.more);
    return d;
  }
  const b = document.createElement("button");
  b.className = "list-item media-row";
  b.innerHTML = `<span class="thumb"><svg><use href="#i-${r.kind === "tag" ? "music" : "eq"}"/></svg></span><span class="txt"><span class="t"></span><span class="s"></span></span>`;
  b.querySelector(".t").textContent = r.kind === "vibe-ask" ? "Describe a vibe…" : r.kind === "vibe" ? r.text : r.tag;
  b.querySelector(".s").textContent = r.kind === "vibe-ask" ? "Type or say what you're in the mood for, and I'll make a mix"
    : r.kind === "vibe" ? "Mix" : `${r.n} songs`;
  if (r.kind !== "tag") b.classList.add("vibe-row");
  b.onclick = () => (r.kind === "vibe-ask" ? openSearch("", "vibe") : r.kind === "vibe" ? makeVibe(r.text) : openTag(r.tagKind, r.tag));
  return b;
}
async function openTag(kind, tag) {
  const { tracks } = await api(`/api/media/tag?kind=${kind}&tag=${encodeURIComponent(tag)}`).catch(() => ({ tracks: [] }));
  media.stack.push({ type: "tag", key: `${kind}:${tag}`, title: `${TAG_TITLES[kind].replace(/s$/, "")} · ${tag}`, tracks });
  media.flip = false;
  renderBrowser();
}
// A mix from a described vibe; plays it, and leaves it open to save as a playlist.
async function makeVibe(text, play = true) {
  toast(`Making a “${text}” mix…`, 20000);
  let r;
  try {
    r = await api("/api/media/vibe", { json: { text } });
  } catch (e) {
    toast(`Couldn't make a mix: ${e.message}`);
    return null;
  }
  $("#toast").hidden = true;
  if (!r.tracks.length) { toast(`Nothing in the library fits “${text}”`); return null; }
  showView("media");
  media.stack = [{ type: "mix", key: `mix:${Date.now()}`, title: r.name, tracks: r.tracks }];
  media.flip = false;
  renderBrowser();
  if (play) playQueue(r.tracks, 0);
  return r;
}
async function loadPlaylists() {
  media.playlists = (await api("/api/playlists").catch(() => ({ playlists: [] }))).playlists;
}
function playlistRow(p) {
  const b = document.createElement("button");
  b.className = "list-item media-row";
  const first = p.tracks.map((id) => media.idIndex.get(id)).find((i) => i != null && media.tracks[i].art);
  b.innerHTML = `<span class="thumb"><svg><use href="#i-music"/></svg></span><span class="txt"><span class="t"></span><span class="s"></span></span>`;
  if (first != null) { b.querySelector(".thumb").style.backgroundImage = `url("${artUrl(media.tracks[first])}")`; b.querySelector(".thumb").innerHTML = ""; }
  b.querySelector(".t").textContent = p.name;
  b.querySelector(".s").textContent = `${p.tracks.length} song${p.tracks.length === 1 ? "" : "s"}`;
  b.onclick = () => drill("playlist", p.id);
  return b;
}
// Save: the open album, artist, mix, category or search (or one song) into a playlist, new or existing.
let pendingSave = [];
function openSaveSheet(trackIdxs) {
  pendingSave = trackIdxs.map((i) => media.tracks[i]?.id).filter(Boolean);
  if (!pendingSave.length) return toast("Nothing to save");
  const box = $("#pl-list");
  const newRow = document.createElement("button");
  newRow.className = "list-item media-row";
  newRow.innerHTML = `<span class="thumb"><svg><use href="#i-plus"/></svg></span><span class="txt"><span class="t">New playlist…</span><span class="s">Name it with the keyboard</span></span>`;
  newRow.onclick = () => { $("#pl-sheet").hidden = true; openSearch(media.stack.at(-1)?.title || "", "plname"); };
  box.replaceChildren(newRow, ...media.playlists.map((p) => {
    const b = playlistRow(p);
    b.onclick = async () => {
      media.playlists = (await api("/api/playlists/add", { json: { id: p.id, tracks: pendingSave } })).playlists;
      $("#pl-sheet").hidden = true;
      toast(`Added ${pendingSave.length} song${pendingSave.length === 1 ? "" : "s"} to ${p.name}`);
    };
    return b;
  }));
  $("#pl-count").textContent = `${pendingSave.length} song${pendingSave.length === 1 ? "" : "s"}`;
  $("#pl-sheet").hidden = false;
}
async function createPlaylist(name) {
  const r = await api("/api/playlists/create", { json: { name: name || "Playlist", tracks: pendingSave } }).catch((e) => toast(e.message));
  if (!r) return;
  media.playlists = r.playlists;
  toast(`Saved “${r.created.name}”`);
  renderBrowser();
}
async function deletePlaylist(id) {
  const p = media.playlists.find((x) => x.id === id);
  if (!p || !confirm(`Delete the playlist “${p.name}”?`)) return;
  media.playlists = (await api("/api/playlists/delete", { json: { id } })).playlists;
  mediaBack();
}

// Row height and the layout change with the screen: draw the list again.
window.addEventListener("resize", () => { if (currentView === "media") renderBrowser(); });
$("#media-tabs").addEventListener("click", (e) => {
  const tab = e.target.closest("[data-tab]")?.dataset.tab;
  if (!tab) return;
  media.tab = tab;
  media.stack = [];
  media.flip = false;
  save("mediaTab", tab);
  renderBrowser();
});

// Search over the library: artists, then albums, then songs whose words all match.
function findMusic(q, limit = 200) {
  const words = fold(q).split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  const hit = (s) => words.every((w) => fold(s).includes(w));
  const out = [];
  for (const ar of media.artists) if (hit(ar.name)) out.push({ kind: "artist", item: ar });
  for (const al of media.albums) if (hit(`${al.name} ${al.artist}`)) out.push({ kind: "album", item: al });
  for (const i of media.songs) {
    if (out.length >= limit) break;
    if (words.every((w) => media.tracks[i].key.includes(w))) out.push({ kind: "track", item: i });
  }
  return out;
}
function showMusicSearch(q) {
  const results = findMusic(q);
  const box = $("#search-results");
  if (!q) return (box.innerHTML = `<div class="empty">Type an artist, album or song.</div>`);
  if (!results.length) return (box.innerHTML = `<div class="empty">Nothing matches “${escapeHtml(q)}”.</div>`);
  box.replaceChildren(...results.slice(0, 60).map((r) => {
    const row = mediaRow(r.kind, r.item);
    const go = row.onclick;
    row.onclick = () => {
      closeSearch();
      showView("media");
      media.stack = [{ type: "search", q, results }];
      media.flip = false;
      renderBrowser();
      go();
    };
    return row;
  }));
}

// ---- player ----
function playQueue(list, start = 0) {
  if (!list.length) return;
  media.queue = list.slice();
  makeOrder(start < 0 ? null : start);
  playPos(0 <= media.pos ? media.pos : 0);
}
// first: the queue position to play first (it stays first when shuffled); null for none, left out to keep the current one.
function makeOrder(first) {
  const n = media.queue.length;
  const cur = first === undefined ? media.order[media.pos] : first;
  media.order = [...Array(n).keys()];
  if (media.shuffle) {
    for (let i = n - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [media.order[i], media.order[j]] = [media.order[j], media.order[i]];
    }
    if (cur != null) media.order.splice(media.order.indexOf(cur), 1), media.order.unshift(cur);
  }
  media.pos = cur == null ? (media.shuffle ? 0 : -1) : media.order.indexOf(cur);
  for (const b of $$('[data-action="shuffle"]')) b.classList.toggle("on", media.shuffle);
}
function playPos(pos) {
  media.pos = pos;
  const t = curTrack();
  if (!t) return;
  stopVideo();
  radio.on = null;  // music replaces the radio (the server stops the tuner when its stream is dropped)
  audio.src = `/api/media/file?id=${encodeURIComponent(t.id)}`;
  // AbortError: the next track replaced this one before it started (fast skipping); not worth a message.
  audio.play().catch((e) => e.name !== "AbortError" && toast(`Can't play: ${e.message}`));
  startVisualizer();
  showNowPlaying(t);
}
function playIndex(i) {  // voice: play this track, then the rest of its album
  const al = media.albums.find((a) => a.tracks.includes(i));
  const list = al ? al.tracks : [i];
  playQueue(list, list.indexOf(i));
}
function showNowPlaying(t) {
  bind("track-title", t.title);
  bind("track-sub", [t.artist, t.album].filter(Boolean).join(" — "));
  const url = artUrl(t);
  pickAlbumColor(url);
  document.body.dataset.music = "loaded";  // shows the now-playing bar over the map when the panel is hidden
  for (const el of $$("[data-art]")) {
    el.classList.toggle("has-art", !!url);
    el.style.backgroundImage = url ? `url("${url}")` : "";
  }
  $("#media-list").repaint?.();
  if ("mediaSession" in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({ title: t.title, artist: t.artist, album: t.album,
      artwork: url ? [{ src: url, sizes: "600x600", type: "image/jpeg" }] : [] });
  }
}
function skipTrack(dir) {
  if (radio.on) return radioStep(dir);
  if (!media.queue.length) return loadMedia(true).then(() => media.songs?.length && playQueue(media.songs, media.shuffle ? -1 : 0));
  if (dir < 0 && audio.currentTime > 4) { audio.currentTime = 0; return; }
  const n = media.order.length;
  playPos((media.pos + dir + n) % n);
}
function playPause() {
  if (radio.on) return radioStop();
  if (!audio.src) return skipTrack(1);
  audio.paused ? audio.play() : audio.pause();
}
function toggleShuffle() {
  media.shuffle = !media.shuffle;
  save("shuffle", media.shuffle);
  if (media.queue.length) makeOrder();
  else for (const b of $$('[data-action="shuffle"]')) b.classList.toggle("on", media.shuffle);
}
function cycleRepeat() {
  media.repeat = { off: "all", all: "one", one: "off" }[media.repeat];
  save("repeat", media.repeat);
  paintRepeat();
  toast({ off: "Repeat off", all: "Repeat all", one: "Repeat this song" }[media.repeat], 1400);
}
function paintRepeat() {
  for (const b of $$('[data-action="repeat"]')) {
    b.classList.toggle("on", media.repeat !== "off");
    b.querySelector("use").setAttribute("href", media.repeat === "one" ? "#i-repeat-one" : "#i-repeat");
  }
}
const mmss = (s) => (Number.isFinite(s) ? `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}` : "0:00");
audio.addEventListener("play", () => { bind("play-icon", "#i-pause"); startVisualizer(); });
audio.addEventListener("pause", () => bind("play-icon", "#i-play"));
audio.addEventListener("ended", () => {
  if (radio.on) { toast("The radio stopped"); radio.on = null; return renderRadio(); }
  if (media.repeat === "one") return audio.play();
  if (media.pos === media.order.length - 1 && media.repeat === "off") return bind("play-icon", "#i-play");
  skipTrack(1);
});
audio.addEventListener("timeupdate", () => {
  bind("track-progress", audio.duration ? `${(audio.currentTime / audio.duration) * 100}%` : "0");
  bind("t-cur", mmss(audio.currentTime));
  bind("t-dur", mmss(audio.duration));
});
audio.addEventListener("error", () => {
  if (radio.on) { toast("Couldn't tune: is the SDR plugged in?"); radio.on = null; return renderRadio(); }
  if (audio.src) toast("This file couldn't be played");
});
for (const el of $$(".seek")) el.addEventListener("pointerdown", (e) => {
  if (!audio.duration) return;
  const r = e.currentTarget.getBoundingClientRect();
  audio.currentTime = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * audio.duration;
});
function openNowPlaying() {
  $("#np-full").hidden = false;
  bind("viz-name", VIZ[settings.viz]);
  startVisualizer();
}

let volTimer;
document.addEventListener("input", (e) => {
  if (e.target.dataset.bind !== "volume") return;
  bind("volume", e.target.value);
  clearTimeout(volTimer);
  volTimer = setTimeout(() => api("/api/volume", { json: { level: Number(e.target.value) } }).catch(() => {}), 120);
});
function nudgeVolume(delta) {
  const el = $('[data-bind="volume"]');
  const level = Math.max(0, Math.min(100, Number(el.value || 50) + delta));
  bind("volume", level);
  api("/api/volume", { json: { level } }).catch(() => {});
}

// ---- visualizer: a Web Audio analyser on the player, drawn at 30 fps while music plays and it's on screen ----
// Style and color are settings (viz, vizColor); the button on the album art cycles the style.
const VIZ = { bars: "Bars", mirror: "Mirror", wave: "Wave", scope: "Scope", ring: "Ring", off: "Off" };
let viz = null;
function startVisualizer() {
  if (settings.viz === "off") return;
  if (!viz) {
    try {
      const ctx = new AudioContext();
      const an = ctx.createAnalyser();
      an.fftSize = 2048;
      an.smoothingTimeConstant = 0.78;
      ctx.createMediaElementSource(audio).connect(an).connect(ctx.destination);
      viz = { ctx, an, bins: new Uint8Array(an.frequencyBinCount), wave: new Uint8Array(an.fftSize), peaks: [], last: 0, running: false };
    } catch (e) {
      return console.warn("visualizer off:", e);
    }
  }
  if (viz.ctx.state === "suspended") viz.ctx.resume();
  if (!viz.running) { viz.running = true; requestAnimationFrame(drawViz); }
}
function cycleViz() {
  const keys = Object.keys(VIZ);
  settings.viz = keys[(keys.indexOf(settings.viz) + 1) % keys.length];
  save("settings", settings);
  paintSettings();
  bind("viz-name", VIZ[settings.viz]);
  if ($("#np-full").hidden) toast(`Visualizer: ${VIZ[settings.viz]}`, 1200);
  startVisualizer();
}
function clearViz() {
  for (const c of $$("canvas.viz")) c.getContext("2d").clearRect(0, 0, c.width, c.height);
}
function bands(N) {  // N log-spaced levels (0..1) from 40 Hz to 16 kHz, with falling peaks
  viz.an.getByteFrequencyData(viz.bins);
  const binHz = viz.ctx.sampleRate / viz.an.fftSize, level = [];
  for (let b = 0; b < N; b++) {
    const lo = Math.floor(40 * 400 ** (b / N) / binHz), hi = Math.max(lo + 1, Math.floor(40 * 400 ** ((b + 1) / N) / binHz));
    let m = 0;
    for (let k = lo; k < hi && k < viz.bins.length; k++) m = Math.max(m, viz.bins[k]);
    level.push((m / 255) ** 1.4);
    viz.peaks[b] = Math.max(level[b], (viz.peaks[b] || 0) - 0.012);
  }
  return level;
}
function vizColor(b, N) {
  if (settings.vizColor === "rainbow") return `hsl(${Math.round(200 + (b / N) * 300)} 85% 60%)`;
  if (settings.vizColor === "album" && media.albumColor) return media.albumColor;
  return getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
}
function drawViz(now) {
  if (audio.paused || settings.viz === "off") { viz.running = false; clearViz(); return; }
  // Keep running while music plays even with no visualizer on screen (Settings, say), so one shows up again as soon
  // as its view does; with nothing visible a frame costs only this check.
  requestAnimationFrame(drawViz);
  const canvases = $$("canvas.viz").filter((c) => c.offsetParent);
  if (!canvases.length || now - viz.last < 33) return;
  viz.last = now;
  const N = 36;
  const level = bands(N);
  viz.an.getByteTimeDomainData(viz.wave);
  for (const c of canvases) {
    // the strips on the home card and over the map are too flat for a ring: bars there
    const style = settings.viz === "ring" && c.classList.contains("wide") ? "bars" : settings.viz;
    c.dataset.style = style;  // ring and scope use the whole art (style.css)
    c.parentElement.dataset.viz = style;
    const w = (c.width = c.clientWidth * devicePixelRatio), h = (c.height = c.clientHeight * devicePixelRatio);
    const g = c.getContext("2d");
    g.clearRect(0, 0, w, h);
    if (style === "bars" || style === "mirror") {
      const bw = w / N;
      for (let b = 0; b < N; b++) {
        g.fillStyle = vizColor(b, N);
        const bh = Math.max(2, level[b] * h * (style === "mirror" ? 0.5 : 1)), x = b * bw + bw * 0.18;
        g.globalAlpha = 0.85;
        if (style === "bars") g.fillRect(x, h - bh, bw * 0.64, bh);
        else g.fillRect(x, h / 2 - bh, bw * 0.64, bh * 2);
        if (style === "bars") { g.globalAlpha = 0.5; g.fillRect(x, h - viz.peaks[b] * h - 3, bw * 0.64, 2); }
      }
    } else if (style === "wave") {
      const pts = level.map((v, b) => [(b / (N - 1)) * w, h - Math.max(0.02, v) * h * 0.95]);
      g.beginPath();
      g.moveTo(0, h);
      g.lineTo(...pts[0]);
      for (let i = 1; i < pts.length; i++) {  // smooth: curve through the midpoints
        const mx = (pts[i - 1][0] + pts[i][0]) / 2, my = (pts[i - 1][1] + pts[i][1]) / 2;
        g.quadraticCurveTo(pts[i - 1][0], pts[i - 1][1], mx, my);
      }
      g.lineTo(w, pts[pts.length - 1][1]);
      g.lineTo(w, h);
      g.closePath();
      const grad = g.createLinearGradient(0, 0, w, 0);
      for (let b = 0; b < N; b += 5) grad.addColorStop(b / N, vizColor(b, N));
      g.fillStyle = grad;
      g.globalAlpha = 0.55;
      g.fill();
      g.globalAlpha = 1;
      g.strokeStyle = grad;
      g.lineWidth = 2 * devicePixelRatio;
      g.stroke();
    } else if (style === "scope") {
      g.beginPath();
      const n = viz.wave.length;
      for (let i = 0; i < n; i += 4) {
        const x = (i / n) * w, y = h / 2 + ((viz.wave[i] - 128) / 128) * h * 0.42;
        i ? g.lineTo(x, y) : g.moveTo(x, y);
      }
      g.strokeStyle = vizColor(N / 2, N);
      g.lineWidth = 2.5 * devicePixelRatio;
      g.shadowColor = g.strokeStyle;
      g.shadowBlur = 8 * devicePixelRatio;
      g.stroke();
    } else if (style === "ring") {
      const cx = w / 2, cy = h / 2, r0 = Math.min(w, h) * 0.3, len = Math.min(w, h) * 0.18;
      g.lineWidth = Math.max(2, ((2 * Math.PI * r0) / (N * 2)) * 0.55);
      g.lineCap = "round";
      for (let i = 0; i < N * 2; i++) {  // mirrored left and right, lows at the top
        const b = i < N ? i : 2 * N - 1 - i, a = (i / (N * 2)) * 2 * Math.PI - Math.PI / 2;
        const r1 = r0 + Math.max(0.03, level[b]) * len;
        g.strokeStyle = vizColor(b, N);
        g.globalAlpha = 0.9;
        g.beginPath();
        g.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0);
        g.lineTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1);
        g.stroke();
      }
    }
  }
}
// The cover's most vivid color, for the "Album" visualizer color.
function pickAlbumColor(url) {
  media.albumColor = media.albumRGB = null;
  if (!url) return applyAccent();
  const img = new Image();
  img.onload = () => {
    const c = document.createElement("canvas");
    c.width = c.height = 24;
    const g = c.getContext("2d");
    g.drawImage(img, 0, 0, 24, 24);
    const d = g.getImageData(0, 0, 24, 24).data, px = [];
    for (let i = 0; i < d.length; i += 4) {
      const [r, gr, b] = [d[i], d[i + 1], d[i + 2]], mx = Math.max(r, gr, b), mn = Math.min(r, gr, b);
      px.push([(mx - mn) * (mx / 255), r, gr, b]);  // saturation weighted by brightness
    }
    px.sort((a, b) => b[0] - a[0]);
    const top = px.slice(0, 40);
    if (top[0][0] < 40) return applyAccent();  // a gray cover: keep the default accent
    const avg = [1, 2, 3].map((k) => Math.round(top.reduce((s, p) => s + p[k], 0) / top.length));
    media.albumRGB = avg;
    applyAccent();
    const lift = 200 / Math.max(...avg, 1);  // bright enough to read on the dark art gradient
    media.albumColor = `rgb(${avg.map((v) => Math.min(255, Math.round(v * Math.max(1, lift)))).join(" ")})`;
  };
  img.src = url;
}

// ---- assistant: talk to Gemma directly, its thinking streamed as it goes (server.py /api/assistant) ----
const chatLog = { msgs: load("chatLog", []), think: load("chatThink", true), speak: load("chatSpeak", false), busy: null };
function renderChat() {
  const box = $("#chat-log");
  $("#chat-think").classList.toggle("on", chatLog.think);
  $("#chat-speak").classList.toggle("on", chatLog.speak);
  $("#chat-stop").hidden = !chatLog.busy;
  if (!chatLog.msgs.length) {
    box.innerHTML = `<div class="empty chat-hello"><b>Ask me anything.</b> I run right here on the truck, offline.
      Tap the box to type, or the mic to talk. With <b>Think</b> on, you can watch me reason before I answer.</div>`;
    return;
  }
  box.replaceChildren(...chatLog.msgs.map((m, i) => chatBubble(m, i)));
  box.scrollTop = box.scrollHeight;
}
function chatBubble(m, i) {
  const d = document.createElement("div");
  d.className = `bubble ${m.role}`;
  if (m.role === "assistant" && m.think) {
    const t = document.createElement("details");
    t.className = "thinking";
    t.open = !!m.live && !m.content;  // open while thinking, folded once the answer starts
    t.innerHTML = `<summary></summary><div class="think-text"></div>`;
    t.querySelector("summary").textContent = m.live && !m.content ? "Thinking…" : `Thought for ${m.thinkS ?? "a few"} s`;
    t.querySelector(".think-text").textContent = m.think;
    d.append(t);
  }
  const body = document.createElement("div");
  body.className = "bubble-text";
  body.innerHTML = m.role === "assistant" ? richText(m.content || (m.live ? "" : "…")) : escapeHtml(m.content);
  if (m.live && !m.think && !m.content) body.innerHTML = `<span class="typing"><i></i><i></i><i></i></span>`;
  d.append(body);
  if (m.error) d.classList.add("error");
  return d;
}
// Just enough formatting for chat answers: **bold**, `code`, bullet and numbered lists, paragraphs.
function richText(s) {
  return escapeHtml(s)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/^(?:\s*[-*•] .*(?:\n|$))+/gm, (block) => `<ul>${block.trim().split("\n").map((l) => `<li>${l.replace(/^\s*[-*•] /, "")}</li>`).join("")}</ul>`)
    .replace(/^(?:\s*\d+\. .*(?:\n|$))+/gm, (block) => `<ol>${block.trim().split("\n").map((l) => `<li>${l.replace(/^\s*\d+\. /, "")}</li>`).join("")}</ol>`)
    .replace(/\n{2,}/g, "<br><br>").replace(/\n/g, "<br>");
}
async function askAssistant(text) {
  text = text.trim();
  if (!text || chatLog.busy) return;
  chatLog.msgs.push({ role: "user", content: text });
  const reply = { role: "assistant", content: "", think: "", live: true };
  chatLog.msgs.push(reply);
  const ctrl = (chatLog.busy = new AbortController());
  renderChat();
  const t0 = Date.now();
  let thinkEnd = null, paint = 0;
  try {
    const r = await fetch("/api/assistant", { method: "POST", signal: ctrl.signal, headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: chatLog.msgs.slice(0, -1).map(({ role, content }) => ({ role, content })),
        context: carContext(), think: chatLog.think }) });
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      for (let i; (i = buf.indexOf("\n")) >= 0; buf = buf.slice(i + 1)) {
        const msg = JSON.parse(buf.slice(0, i));
        if (msg.t === "think") reply.think += msg.d;
        else if (msg.t === "text") { if (thinkEnd == null) thinkEnd = Date.now(); reply.content += msg.d; }
        else if (msg.t === "error") { reply.content = msg.d; reply.error = true; }
      }
      if (Date.now() - paint > 120) { paint = Date.now(); reply.thinkS = Math.round(((thinkEnd ?? Date.now()) - t0) / 1000); renderChat(); }
    }
  } catch (e) {
    if (e.name !== "AbortError") { reply.content ||= `Couldn't reach the model: ${e.message}`; reply.error = true; }
    else reply.content ||= "(stopped)";
  }
  reply.live = false;
  reply.thinkS = Math.round(((thinkEnd ?? Date.now()) - t0) / 1000);
  chatLog.busy = null;
  chatLog.msgs = chatLog.msgs.slice(-40);
  save("chatLog", chatLog.msgs);
  renderChat();
  if (chatLog.speak && reply.content && !reply.error) {
    const plain = reply.content.replace(/[*`#]/g, "").replace(/\s+/g, " ").slice(0, 600);
    speak(plain);
  }
}
async function chatMic() {
  setVoice("listening", "Listening…", "");
  try {
    const r = await listen((phase) => {
      if (phase === "speech") setVoice("speech", "Hearing you…");
      if (phase === "heard") { earcon(); setVoice("heard", "Got it"); }
    });
    hideVoice(600);
    if (r?.text) askAssistant(r.text.replace(KEYWORD, ""));
  } catch (e) {
    setVoice("error", `Voice failed: ${e.message}`);
    hideVoice(3000);
  }
}

// ---- Wi-Fi and data use (network.py) ----
let wifiTarget = "", wifiInfo = null;
async function paintWifi(rescan) {
  try { wifiInfo = await api(`/api/wifi${rescan ? "?rescan=1" : ""}`); } catch (e) { $("#wifi-state").textContent = e.message; return; }
  const w = wifiInfo;
  $("#wifi-state").textContent = w.active ? `Connected to ${w.active}${w.metered ? " (metered)" : ""}` : "Not connected";
  for (const b of $$("#data-seg button")) b.classList.toggle("on", b.dataset.v === w.mode);
  $("#data-note").textContent = { auto: `Now: ${w.effective === "saver" ? "saving data (metered connection)" : "normal"}`,
    normal: "Background downloads on any connection", saver: "No background downloads; what you open still loads",
    offline: "Never goes online: maps, routing, voice and music still work" }[w.mode];
  const box = $("#wifi-list");
  if (box.hidden) return;
  box.replaceChildren(...w.networks.map((n) => {
    const b = document.createElement("button");
    b.className = "list-item media-row" + (n.active ? " playing" : "");
    b.innerHTML = `<span class="thumb"><svg><use href="#i-wifi"/></svg></span><span class="txt"><span class="t"></span><span class="s"></span></span><span class="sat-el"></span>`;
    b.querySelector(".t").textContent = n.ssid;
    b.querySelector(".s").textContent = [n.active ? "connected" : n.saved ? "saved" : "", n.secure ? "secured" : "open"].filter(Boolean).join(" · ");
    b.querySelector(".sat-el").textContent = `${n.signal}%`;
    b.onclick = () => wifiPick(n);
    return b;
  }), ...(w.networks.length ? [] : [note("No networks found.")]));
}
function wifiPick(n) {
  if (n.active) {
    if (n.saved && confirm(`Forget “${n.ssid}”? The truck won't join it again until you enter the password.`)) wifiForget(n.ssid);
    return;
  }
  if (n.saved || !n.secure) return wifiJoin(n.ssid, "");
  wifiTarget = n.ssid;
  kbLayer = "abc";
  openSearch("", "pass");
}
async function wifiJoin(ssid, password) {
  toast(`Joining ${ssid}…`, 20000);
  try {
    wifiInfo = await api("/api/wifi/connect", { json: { ssid, password } });
    toast(`Connected to ${ssid}`);
  } catch (e) {
    toast(`Couldn't join ${ssid}: ${e.message}`, 5000);
  }
  paintWifi();
}
async function wifiForget(ssid) {
  try { await api("/api/wifi/forget", { json: { ssid } }); toast(`Forgot ${ssid}`); } catch (e) { toast(e.message); }
  paintWifi();
}
$("#data-seg").addEventListener("click", async (e) => {
  const v = e.target.closest("button")?.dataset.v;
  if (!v) return;
  try { await api("/api/data", { json: { mode: v } }); } catch (err) { toast(err.message); }
  paintWifi();
});

// ---- Bluetooth: pair a phone from Settings (bt.py) ----
let btPoll;
async function paintBt(st) {
  try { st ||= await api("/api/bt"); } catch { return; }
  const pairing = st.pairing;
  $("#bt-pair").textContent = pairing ? `Pairing… ${Math.floor(st.pairing_left_s / 60)}:${String(st.pairing_left_s % 60).padStart(2, "0")}` : "Pair a phone";
  $("#bt-pair").classList.toggle("primary", !pairing);
  $("#bt-state").textContent = pairing ? `On your phone: Bluetooth → Pair new device → “${st.alias}”`
    : st.devices.length ? st.devices.map((d) => `${d.name}${d.connected ? " (connected)" : ""}`).join(" · ")
    : st.powered ? "No phone paired yet" : "Bluetooth is off";
  $("#bt-forget").hidden = pairing || !st.devices.length;
  clearTimeout(btPoll);
  if (pairing || currentView === "settings") btPoll = setTimeout(() => paintBt(), pairing ? 2000 : 10000);
}
async function btPair() {
  try { paintBt(await api("/api/bt/pair", { method: "POST" })); } catch (e) { toast(`Bluetooth: ${e.message}`); }
}
async function btForget() {
  const st = await api("/api/bt").catch(() => null);
  for (const d of st?.devices || []) await api("/api/bt/forget", { json: { mac: d.mac } }).catch(() => {});
  toast("Forgot the paired phone", 1600);
  paintBt();
}

// ---- emergency: tsunami zone, where to go, help nearby, shelters, earthquakes (emergency.py) ----
let emergencyInfo = null;
const HELP_TITLES = { community: "Community centers", redcross: "Red Cross", hospital: "Hospitals", fire: "Fire stations" };
async function loadEmergency() {
  $("#em-list").innerHTML = `<div class="empty">Looking around…</div>`;
  try {
    emergencyInfo = await api("/api/emergency");
  } catch (e) {
    $("#em-list").innerHTML = `<div class="empty">Couldn't load: ${escapeHtml(e.message)}</div>`;
    return;
  }
  if (!radio.weather) radio.weather = await api("/api/weather").catch(() => null);
  renderEmergency();
}
function renderEmergency() {
  const e = emergencyInfo, rows = [];
  if (!e) return;
  if (e.error) return ($("#em-list").innerHTML = `<div class="empty">${escapeHtml(e.error)}</div>`);
  const banner = document.createElement("div");
  banner.className = "em-banner " + (e.zone ? "danger" : "ok");
  banner.textContent = e.zone ? e.zone.advice : e.have_tsunami_data ? "Not in a tsunami evacuation zone." : "No tsunami zone data installed (tools/fetch_emergency.py).";
  rows.push(banner);
  for (const a of radio.weather?.alerts || []) rows.push(Object.assign(document.createElement("div"), { className: "wx-alert", textContent: `⚠ ${a.event}: ${a.headline || ""}` }));
  for (const q of lastState?.quake_alerts || []) rows.push(Object.assign(document.createElement("div"), { className: "wx-alert", textContent: `⚠ Earthquake M${q.mag.toFixed(1)}, ${q.place}${q.tsunami ? " (tsunami possible)" : ""}` }));
  if (e.assembly.length) {
    rows.push(head(e.zone ? "Go here: assembly areas and high ground" : "Tsunami assembly areas nearby"));
    rows.push(...e.assembly.map((a) => placeRow(a.name, [a.kind, a.address, a.elev_ft != null && `${a.elev_ft} ft up`].filter(Boolean).join(" · "), a, "#i-flag")));
  } else if (e.zone) rows.push(note("No mapped assembly areas near here: head inland and uphill, out of the zone, on foot if you can."));
  rows.push(head("Open emergency shelters (FEMA)"));
  rows.push(...(e.shelters.length ? e.shelters.map((s) => placeRow(s.name, `${s.address}${s.org ? ` · ${s.org}` : ""}`, s, "#i-home"))
    : [note("None open nearby right now (this needs internet).")]));
  for (const [k, title] of Object.entries(HELP_TITLES)) {
    if (!e.help[k]?.length) continue;
    rows.push(head(title));
    rows.push(...e.help[k].map((p) => placeRow(p.name, p.address || p.kind, p, k === "hospital" ? "#i-plus" : "#i-home")));
  }
  rows.push(head("Earthquakes in the past week, within 500 km"));
  rows.push(...(e.quakes.length ? e.quakes.map((q) => {
    const d = document.createElement("div");
    d.className = "list-item media-row quake-row";
    d.innerHTML = `<span class="thumb mag"></span><span class="txt"><span class="t"></span><span class="s"></span></span>`;
    d.querySelector(".mag").textContent = q.mag.toFixed(1);
    d.querySelector(".mag").style.background = q.mag >= 5 ? "var(--bad)" : q.mag >= 4 ? "var(--warn)" : "";
    d.querySelector(".t").textContent = q.place;
    const ago = (Date.now() / 1000 - q.time) / 3600;
    d.querySelector(".s").textContent = `${ago < 1 ? `${Math.round(ago * 60)} min` : ago < 48 ? `${Math.round(ago)} h` : `${Math.round(ago / 24)} days`} ago · ${distText(q.km * 1000)} away · ${Math.round(q.depth_km)} km deep`;
    return d;
  }) : [note("None (or no internet yet to fetch them).")]));
  $("#em-list").replaceChildren(...rows);
}
function placeRow(title, sub, p, icon) {
  const b = document.createElement("button");
  b.className = "list-item media-row";
  b.innerHTML = `<span class="thumb"><svg><use href="${icon}"/></svg></span><span class="txt"><span class="t"></span><span class="s"></span></span><span class="sat-el"></span>`;
  b.querySelector(".t").textContent = title;
  b.querySelector(".s").textContent = sub || "";
  b.querySelector(".sat-el").textContent = distText(p.km * 1000);
  b.onclick = () => previewRoute({ name: title, address: sub || "", lat: p.lat, lon: p.lon });
  return b;
}
// "Truck, evacuate" / "nearest shelter": straight to the closest one.
async function emergencyGo(kind) {
  const e = (emergencyInfo = await api("/api/emergency").catch(() => null));
  if (!e || e.error) return e?.error || "Can't tell where we are yet";
  const pick = kind === "evacuate" ? e.assembly[0] : kind === "shelter" ? e.shelters[0] || e.help.community[0] || e.help.redcross[0] : e.help[kind]?.[0];
  if (!pick) return kind === "evacuate" ? (e.zone ? "No mapped assembly area near here: head inland and uphill" : "You're not in a tsunami zone") : "Nothing found nearby";
  await previewRoute({ name: pick.name, address: pick.address || "", lat: pick.lat, lon: pick.lon });
  if (nav.route) startNav();
  return `Navigating to ${pick.name}`;
}
// Earthquake alerts from the server (USGS feed): said once each.
const quakesSaid = new Set(load("quakesSaid", []));
function checkQuakeAlerts(s) {
  for (const q of s.quake_alerts || []) {
    if (quakesSaid.has(q.id)) continue;
    quakesSaid.add(q.id);
    save("quakesSaid", [...quakesSaid].slice(-50));
    const msg = `Earthquake alert: magnitude ${q.mag.toFixed(1)}, ${q.place}, ${spokenDist(q.km * 1000)} away.` +
      (q.tsunami ? " A tsunami may be possible: if you're near the coast, go to high ground." : "");
    setVoice("error", msg);
    hideVoice(12000);
    speak(msg);  // even with direction prompts off
  }
}

// ---- collapsing: the side panel (full-screen map) and the home cards, both remembered ----
function setPanel(hidden, remember = true) {
  if (document.body.classList.contains("panel-hidden") === hidden) return;
  document.body.classList.toggle("panel-hidden", hidden);
  if (remember) save("panelHidden", hidden);
  if (map) setTimeout(() => map.resize(), 280);
}
const folds = load("folds", {});
function paintFolds() {
  for (const c of $$("[data-fold]")) c.classList.toggle("folded", !!folds[c.dataset.fold]);
}
function toggleFold(btn) {
  const key = btn.closest("[data-fold]").dataset.fold;
  folds[key] = !folds[key];
  save("folds", folds);
  paintFolds();
}
paintFolds();
if (load("panelHidden", false)) setPanel(true, false);
function paintMute() {
  const b = $("#mute-btn");
  b.classList.toggle("on", settings.prompts !== "on");
  b.querySelector("use").setAttribute("href", settings.prompts === "on" ? "#i-volume" : "#i-mute");
}
paintMute();
paintHome();
if (home) api("/api/home", { json: home }).catch(() => {});  // the Pi's copy, in case it was set on another screen

// ---- radio: the SDR (radio.py). A tuned station streams into the same player as music. ----
const radio = {
  info: null, tab: load("radioTab", "fm"), on: null,  // on: {f, freqs, mode, label, sub} while radio plays
  fm: load("fmStations", []), presets: load("radioPresets", []), weather: null, loaded: 0,
  last: load("radioLast", { f: 98.5e6, mode: "wbfm" }),
};
const RADIO_STEP = { wbfm: 200e3, am: 10e3, nfm: 12.5e3, air: 25e3 };
const fmtFreq = (f, mode) => (mode === "am" ? { v: String(Math.round(f / 1e3)), u: "kHz" }
  : { v: (f / 1e6).toFixed(mode === "wbfm" ? 1 : f % 1e4 ? 4 : 3), u: "MHz" });
const freqText = (f, mode) => { const x = fmtFreq(f, mode); return `${x.v} ${x.u}`; };

async function loadRadio(force) {
  if (radio.tab === "mesh") loadMesh();
  if (!force && Date.now() - radio.loaded < 120000) return renderRadio();
  try {
    radio.info = await api("/api/radio");
    radio.loaded = Date.now();
  } catch (e) {
    $("#radio-list").innerHTML = `<div class="empty">Couldn't load the radio: ${escapeHtml(e.message)}</div>`;
    return;
  }
  renderRadio();
}

function renderRadio() {
  for (const b of $$("#radio-tabs [data-tab]")) b.classList.toggle("on", b.dataset.tab === radio.tab);
  const info = radio.info, warn = $("#radio-warn");
  warn.hidden = !info || (info.sdr && info.rtl_fm);
  if (info && !info.sdr) warn.textContent = "SDR not connected: plug the Nooelec v5 into a USB 2 port.";
  else if (info && !info.rtl_fm) warn.textContent = "SDR software missing: sudo apt install rtl-sdr";
  paintDial();
  const box = $("#radio-list"), rows = [];
  const presets = radio.presets.filter((p) => (radio.tab === "fm" ? p.mode === "wbfm" : radio.tab === "am" ? p.mode === "am" : false));
  if (radio.tab === "fm" || radio.tab === "am") {
    if (presets.length) rows.push(head("Presets"), ...presets.map((p) => chRow(p.label || freqText(p.f, p.mode), freqText(p.f, p.mode), p)));
    if (radio.tab === "fm") {
      rows.push(head("Stations", radio.fm.length ? "Scan again" : "Scan the band", radioScan));
      if (radio.fm.length) rows.push(...radio.fm.map((s) => chRow(freqText(s.f, "wbfm"), `${s.db} dB above the noise`, { f: s.f, mode: "wbfm" })));
      else rows.push(note("Tap Scan to find the stations you can receive here, or tune with − and +."));
    } else rows.push(note("AM 530–1700 kHz. Tune with − and + (10 kHz), or tap the frequency to type one. The truck's antenna works best for AM."));
  } else if (radio.tab === "weather") {
    rows.push(head("NOAA Weather Radio", "Scan all", () => tune({ freqs: chans("weather").map((c) => c.f), mode: "nfm", label: "Scanning NOAA weather" })));
    rows.push(...chans("weather").map((c) => chRow(c.name, `${freqText(c.f, "nfm")} · ${c.note}`, c)));
    rows.push(note("The forecast, alerts and satellite pictures are in the Weather tab."));
  } else if (radio.tab === "scanner") {
    for (const [g, title] of Object.entries(info?.groups || {})) {
      if (g === "weather") continue;
      const list = chans(g);
      rows.push(head(title, "Scan", () => tune({ freqs: list.map((c) => c.f), mode: list[0].mode, label: `Scanning ${title}` })));
      rows.push(...list.map((c) => chRow(c.name, `${freqText(c.f, c.mode)} · ${c.note}`, c)));
    }
    rows.push(note("Local county dispatch varies (and is often P25 digital, which this can't decode). Add local channels as presets with ★."));
  } else if (radio.tab === "sats") {
    rows.push(...satRows());
  } else if (radio.tab === "mesh") {
    rows.push(...meshRows());
  }
  box.replaceChildren(...rows);
}
const chans = (g) => (radio.info?.channels || []).filter((c) => c.group === g);
function head(title, action, fn) {
  const d = document.createElement("div");
  d.className = "list-head";
  d.innerHTML = `<span></span>${action ? `<button class="btn"></button>` : ""}`;
  d.querySelector("span").textContent = title;
  if (action) { d.querySelector("button").textContent = action; d.querySelector("button").onclick = fn; }
  return d;
}
function note(text) {
  const d = document.createElement("div");
  d.className = "empty";
  d.textContent = text;
  return d;
}
function chRow(title, sub, ch, icon = "#i-radio") {
  const b = document.createElement("button");
  b.className = "list-item media-row" + (radio.on && radio.on.freqs.length === 1 && radio.on.f === ch.f && radio.on.mode === ch.mode ? " playing" : "");
  b.innerHTML = `<span class="thumb"><svg><use href="${icon}"/></svg></span><span class="txt"><span class="t"></span><span class="s"></span></span>`;
  b.querySelector(".t").textContent = title;
  b.querySelector(".s").textContent = sub;
  b.onclick = () => tune({ freqs: [ch.f], mode: ch.mode, label: ch.name || ch.label || freqText(ch.f, ch.mode) });
  return b;
}

// Tune: the stream URL goes into the music player, so pause, volume and the visualizer all work.
async function tune({ freqs, mode, label, sub }) {
  if (radio.info && !radio.info.sdr) await loadRadio(true);  // maybe it was plugged in since
  if (!radio.info?.sdr) return toast("SDR not connected: plug in the Nooelec v5");
  stopVideo();
  media.queue = []; media.order = []; media.pos = -1;  // the music queue is over; next/previous now tune
  const f = freqs[0];
  radio.on = { f, freqs, mode, label, sub: sub || (freqs.length > 1 ? `${freqs.length} channels` : freqText(f, mode)) };
  if (freqs.length === 1) { radio.last = { f, mode }; save("radioLast", radio.last); }
  const qs = new URLSearchParams({ mode, label });
  for (const x of freqs) qs.append("f", Math.round(x));
  audio.src = `/api/radio/stream?${qs}`;
  audio.play().catch((e) => e.name !== "AbortError" && toast(`Can't tune: ${e.message}`));
  startVisualizer();
  bind("track-title", label);
  bind("track-sub", radio.on.sub);
  pickAlbumColor(null);
  for (const el of $$("[data-art]")) { el.classList.remove("has-art"); el.style.backgroundImage = ""; }
  document.body.dataset.music = "radio";
  renderRadio();
}
function radioStop() {
  if (!radio.on) return;
  radio.on = null;
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  api("/api/radio/stop", { method: "POST" }).catch(() => {});
  bind("track-title", "Nothing playing");
  bind("track-sub", "");
  renderRadio();
}
function radioStep(dir) {  // next/previous while radio plays: the next preset or scanned station, else a tuning step
  const { f, mode } = radio.on || radio.last;
  const list = [...radio.presets.filter((p) => p.mode === mode), ...(mode === "wbfm" ? radio.fm.map((s) => ({ ...s, mode })) : [])]
    .map((p) => p.f).filter((x, i, a) => a.indexOf(x) === i).sort((a, b) => a - b);
  let next = dir > 0 ? list.find((x) => x > f) : list.reverse().find((x) => x < f);
  if (next == null) next = f + dir * RADIO_STEP[mode];
  const [lo, hi] = mode === "am" ? [530e3, 1700e3] : mode === "wbfm" ? [87.9e6, 107.9e6] : [24e6, 1766e6];
  next = Math.min(hi, Math.max(lo, next));
  const preset = radio.presets.find((p) => p.f === next && p.mode === mode);
  tune({ freqs: [next], mode, label: preset?.label || freqText(next, mode) });
}
function paintDial() {
  const { f, mode } = radio.on || radio.last;
  const x = fmtFreq(f, mode);
  $("#radio-freq").textContent = radio.on?.freqs.length > 1 ? "Scan" : x.v;
  $("#radio-unit").textContent = radio.on?.freqs.length > 1 ? "" : x.u;
  $("#radio-label").textContent = radio.on ? radio.on.label : "Not playing";
  $("#radio-sub").textContent = radio.on ? radio.on.sub : `${{ wbfm: "FM", am: "AM", nfm: "Narrow FM", air: "Aviation AM" }[mode]} · tap play to listen`;
  $("#radio-play-icon").setAttribute("href", radio.on ? "#i-pause" : "#i-play");
  $("#radio-star").classList.toggle("on", radio.presets.some((p) => p.f === f && p.mode === mode));
}
function togglePreset() {
  const { f, mode } = radio.on || radio.last;
  const i = radio.presets.findIndex((p) => p.f === f && p.mode === mode);
  if (i >= 0) radio.presets.splice(i, 1);
  else radio.presets.push({ f, mode, label: radio.on?.freqs.length === 1 ? radio.on.label : freqText(f, mode) });
  save("radioPresets", radio.presets);
  toast(i >= 0 ? "Preset removed" : `Saved ${freqText(f, mode)}`, 1400);
  renderRadio();
}
async function radioScan() {
  if (!radio.info?.sdr) return toast("SDR not connected: plug in the Nooelec v5");
  radioStop();
  toast("Scanning the FM band…", 8000);
  try {
    radio.fm = (await api("/api/radio/fmscan", { method: "POST" })).stations;
    save("fmStations", radio.fm);
    toast(`Found ${radio.fm.length} stations`);
  } catch (e) {
    toast(`Scan failed: ${e.message}`);
  }
  renderRadio();
}

// Satellites: passes in the next two days, from current orbits (Celestrak, refreshed every two days).
function satRows() {
  const info = radio.info || {}, ps = info.passes || [], now = Date.now() / 1000, rows = [];
  // automatic capture: off, when the radio's free, always
  const auto = document.createElement("div");
  auto.className = "list-head sat-auto";
  auto.innerHTML = `<span>Capture passes automatically</span><div class="seg">${[["off", "Off"], ["idle", "When free"], ["always", "Always"]]
    .map(([v, t]) => `<button data-v="${v}" class="${info.auto === v ? "on" : ""}">${t}</button>`).join("")}</div>`;
  auto.querySelector(".seg").onclick = async (e) => {
    const v = e.target.closest("button")?.dataset.v;
    if (!v) return;
    try { Object.assign(info, await api("/api/radio/settings", { json: { auto: v } })); } catch (err) { toast(err.message); }
    renderRadio();
  };
  rows.push(auto);
  const cap = info.capture;
  if (cap && cap.state !== "idle") {
    const d = document.createElement("div");
    d.className = "wx-alert cap-now";
    const left = Math.max(0, Math.round((cap.pass.los - now) / 60));
    d.textContent = cap.state === "capturing" ? `● Recording ${cap.pass.name.replace(" (ZARYA)", "")} · ${left} min left` : `Decoding ${cap.pass.name.replace(" (ZARYA)", "")}…`;
    rows.push(d);
  }
  const age = info.tle_age_days;
  rows.push(head("Upcoming passes"));
  rows.push(note(`Over ${lastPos ? "your position" : "your home"}, above 10°. Orbits ${age == null ? "not downloaded yet (needs internet once)" : age < 1 ? "updated today" : `${age} days old`}. ● marks passes that get recorded: Meteor-M2 above 20° (weather images), the ISS and SO-50 above 15° (audio, and SSTV pictures when the ISS sends them).`));
  for (const p of ps.filter((p) => p.los > now).slice(0, 30)) {
    const live = p.aos <= now, mins = Math.round((p.aos - now) / 60);
    const when = live ? "Overhead now" : mins < 60 ? `in ${mins} min` : `${new Date(p.aos * 1000).toLocaleDateString([], { weekday: "short" })} ${fmtClock(new Date(p.aos * 1000), true)}`;
    const b = document.createElement("button");
    b.className = "list-item media-row sat-row" + (live ? " live" : "");
    b.innerHTML = `<span class="thumb"><svg><use href="#i-sat"/></svg></span><span class="txt"><span class="t"></span><span class="s"></span></span><span class="sat-el"></span>`;
    b.querySelector(".t").textContent = `${p.auto && info.auto !== "off" ? "● " : ""}${p.name.replace(" (ZARYA)", "")} · ${when}`;
    b.querySelector(".s").textContent = `${Math.round((p.los - p.aos) / 60)} min, ${cardinal(p.aos_az)} → ${cardinal(p.los_az)} · ${freqText(p.freq_hz, "nfm")} · ${p.what}`;
    b.querySelector(".sat-el").textContent = `${p.max_el}°`;
    b.onclick = () => {
      if (!live) return toast(`${p.name} rises ${when}; tap again then`, 2000);
      tune({ freqs: [p.freq_hz + dopplerNow(p, now)], mode: "nfm", label: p.name.replace(" (ZARYA)", ""), sub: `${freqText(p.freq_hz, "nfm")} · Doppler-corrected` });
    };
    rows.push(b);
  }
  if (!ps.length) rows.push(note("No passes predicted yet."));
  // what was captured
  const caps = info.captures || [];
  rows.push(head("Captures"));
  if (!caps.length) rows.push(note("Nothing captured yet. Passes are recorded automatically once the SDR is plugged in."));
  for (const c of caps) {
    const b = document.createElement("button");
    b.className = "list-item media-row cap-row";
    const pics = c.files.filter((f) => f.endsWith(".png")).length, audioClip = c.files.includes("audio.opus");
    b.innerHTML = `<span class="thumb"><svg><use href="#i-sat"/></svg></span><span class="txt"><span class="t"></span><span class="s"></span></span>`;
    if (c.preview) b.querySelector(".thumb").style.backgroundImage = `url("${capUrl(c, c.preview)}")`;
    b.querySelector(".t").textContent = `${c.name.replace(" (ZARYA)", "")} · ${new Date(c.aos * 1000).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" })} ${fmtClock(new Date(c.aos * 1000), true)} · ${c.max_el}°`;
    b.querySelector(".s").textContent = c.status === "done"
      ? [pics && `${pics} image${pics === 1 ? "" : "s"}`, audioClip && "audio"].filter(Boolean).join(" · ") || "nothing decoded"
      : c.status + (c.note ? `: ${c.note}` : "");
    if (c.files.length) b.onclick = () => openCapture(c);
    rows.push(b);
  }
  return rows;
}
const capUrl = (c, name) => `/api/radio/capture?id=${encodeURIComponent(c.id)}&name=${encodeURIComponent(name)}`;
// A capture full screen: its images one at a time (preview first), and its audio.
let capView = null;
function openCapture(c) {
  const pics = c.files.filter((f) => f.endsWith(".png"));
  if (c.preview) pics.sort((a, b) => (b === c.preview) - (a === c.preview));
  capView = { c, pics, i: 0 };
  $("#cap-play").hidden = true;
  const clip = $("#cap-audio");
  clip.hidden = !c.files.includes("audio.opus");
  if (!clip.hidden) clip.src = capUrl(c, "audio.opus");
  $("#cap-viewer").hidden = false;
  showCapImage(0);
}
function showCapImage(d) {
  if (!capView) return;
  if (capView.goes) {  // step through the animation frames, paused
    stopGoesAnim();
    return loadGoesFrames(capView.view).then((ok) => ok && showGoesFrame((goesAnim.i + d + goesAnim.frames.length) % goesAnim.frames.length));
  }
  const { c, pics } = capView;
  capView.i = pics.length ? (capView.i + d + pics.length) % pics.length : 0;
  const name = pics[capView.i];
  $("#cap-img").hidden = !name;
  if (name) $("#cap-img").src = capUrl(c, name);
  $("#cap-title").textContent = `${c.name.replace(" (ZARYA)", "")} · ${fmtClock(new Date(c.aos * 1000), true)}${name ? ` · ${name.split("/").pop().replace(/\.png$/, "").replace(/_/g, " ")} (${capView.i + 1}/${pics.length})` : ""}`;
}
function dopplerNow(p, now) {  // straight lines between the shifts at rise, peak and set
  const [a, m, l] = p.doppler_hz;
  return now < p.max_t ? a + ((m - a) * (now - p.aos)) / (p.max_t - p.aos) : m + ((l - m) * (now - p.max_t)) / (p.los - p.max_t);
}

// GOES-18 (NOAA's western weather satellite): the latest picture, cached on the Pi for when there's no internet.
const GOES_VIEWS = { pnw: "Northwest", "pnw-ir": "Infrared", west: "West US", fd: "Earth (West)", "fd-east": "Earth (East)" };
// Animation: the frames the Pi keeps (radio.py update_animation), played on a loop with a pause on the newest.
const goesAnim = { timer: null, frames: [], i: 0, imgs: [] };
function stopGoesAnim() {
  clearInterval(goesAnim.timer);
  goesAnim.timer = null;
  for (const b of $$(".goes-play, #cap-play")) b.classList.remove("on");
  $("#cap-play use").setAttribute("href", "#i-play");
}
const goesViewerOpen = () => capView?.goes && !$("#cap-viewer").hidden;
function showGoesFrame(i) {  // frame i of the loaded animation, on the card and in the viewer
  const f = goesAnim.frames[i];
  if (!f) return;
  goesAnim.i = i;
  const src = goesAnim.imgs[i].src, label = `${fmtClock(new Date(f.t * 1000), true)} · ${i + 1}/${goesAnim.frames.length}`;
  const img = $(".goes-pic img");
  if (img) img.src = src;
  if ($(".goes-age")) $(".goes-age").textContent = label;
  if (goesViewerOpen()) {
    $("#cap-img").src = src;
    $("#cap-title").textContent = `GOES · ${GOES_VIEWS[capView.view]} · ${label}`;
  }
}
async function loadGoesFrames(view) {
  if (goesAnim.view === view && goesAnim.frames.length && Date.now() - goesAnim.loaded < 300000) return true;
  const { frames } = await api(`/api/wx/goes/frames?view=${view}`).catch(() => ({ frames: [] }));
  if (frames.length < 2) { toast("No animation frames yet: they build up over the next hour online", 2600); return false; }
  Object.assign(goesAnim, { view, frames, loaded: Date.now(), i: frames.length - 1,
    imgs: frames.map((f) => Object.assign(new Image(), { src: `/api/wx/goes/frame?view=${view}&stamp=${f.stamp}` })) });
  return true;
}
async function toggleGoesAnim() {
  if (goesAnim.timer) return stopGoesAnim();
  const view = radio.goesView;
  if (!(await loadGoesFrames(view))) return;
  for (const b of $$(".goes-play, #cap-play")) b.classList.add("on");
  $("#cap-play use").setAttribute("href", "#i-pause");
  let hold = 0;
  goesAnim.timer = setInterval(() => {
    if (radio.goesView !== view) return stopGoesAnim();
    if (goesAnim.i === goesAnim.frames.length - 1 && hold++ < 6) return;  // linger on the newest frame
    hold = 0;
    showGoesFrame((goesAnim.i + 1) % goesAnim.frames.length);
  }, 220);
}
function goesCard() {
  const d = document.createElement("div");
  d.className = "goes-card";
  radio.goesView ||= new Date().getHours() >= 7 && new Date().getHours() < 19 ? "pnw" : "pnw-ir";  // GeoColor by day
  stopGoesAnim();
  d.innerHTML = `<div class="goes-pic" data-action="goes-view"><img alt=""><span class="goes-age"></span>
      <button class="goes-play" data-action="goes-anim" aria-label="Animate"><svg><use href="#i-play"/></svg></button></div>
    <div class="seg">${Object.entries(GOES_VIEWS).map(([v, t]) => `<button data-v="${v}" class="${v === radio.goesView ? "on" : ""}">${t}</button>`).join("")}</div>`;
  const img = d.querySelector("img"), age = d.querySelector(".goes-age");
  fetch(`/api/wx/goes?view=${radio.goesView}`).then(async (r) => {
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
    const a = Number(r.headers.get("X-Image-Age") || 0);
    img.src = URL.createObjectURL(await r.blob());
    age.textContent = `GOES-18 · ${a > 1200 ? `${Math.round(a / 60)} min old (offline)` : "latest"}`;
  }).catch((e) => (age.textContent = String(e.message || e)));
  d.querySelector(".seg").onclick = (e) => {
    const v = e.target.closest("button")?.dataset.v;
    if (v) { radio.goesView = v; renderWeather(); }
  };
  return d;
}
function openGoes(view) {  // full screen; an animation that's playing keeps playing there
  const img = $(".goes-pic img");
  if (!img?.src) return;
  $("#cap-audio").hidden = true;
  capView = { goes: true, view };
  $("#cap-play").hidden = false;
  $("#cap-img").hidden = false;
  $("#cap-img").src = img.src;
  $("#cap-title").textContent = `GOES · ${GOES_VIEWS[view]} · ${$(".goes-age").textContent.replace("GOES-18 · ", "")}`;
  $("#cap-viewer").hidden = false;
}
// The Weather tab: satellite pictures (and their animations), alerts, the forecast; all kept for offline use.
async function loadWeather(force) {
  renderWeather();
  if (!force && radio.weather && Date.now() - (radio.weatherAt || 0) < 300000) return;
  radio.weather = await api("/api/weather").catch(() => radio.weather);
  radio.weatherAt = Date.now();
  if (currentView === "weather") renderWeather();
}
function renderWeather() {
  const rows = weatherRows();
  rows.push(head("NOAA Weather Radio", "Listen", () => { showView("radio"); radio.tab = "weather"; save("radioTab", "weather"); renderRadio(); }));
  rows.push(note("Live broadcasts and alerts on 162.40–162.55 MHz, with the SDR."));
  $("#wx-list").replaceChildren(...rows);
}
function weatherRows() {
  const w = radio.weather, rows = [head("Satellite · NOAA GOES"), goesCard()];
  if (!w || w.error) return [note(w?.error || "Loading the forecast…")];
  rows.push(head(`Forecast · ${w.place}${w.stale ? " (offline: last update)" : ""}`));
  for (const a of w.alerts || []) {
    const d = document.createElement("div");
    d.className = "wx-alert";
    d.textContent = `⚠ ${a.event}: ${a.headline || ""}`;
    rows.push(d);
  }
  for (const p of w.periods || []) {
    const d = document.createElement("div");
    d.className = "list-item media-row wx-row";
    d.innerHTML = `<span class="thumb"><svg><use href="#i-cloud"/></svg></span><span class="txt"><span class="t"></span><span class="s"></span></span><b class="wx-temp"></b>`;
    d.querySelector(".t").textContent = p.name;
    d.querySelector(".s").textContent = `${p.shortForecast}${p.rain ? ` · ${p.rain}% rain` : ""} · wind ${p.windDirection} ${p.windSpeed}`;
    d.querySelector(".wx-temp").textContent = `${p.temperature}°`;
    rows.push(d);
  }
  return rows;
}

function radioNudge(dir) {  // the − and + on the dial: one tuning step (just moves the dial when stopped)
  const { f, mode } = radio.on?.freqs.length === 1 ? radio.on : radio.last;
  const [lo, hi] = mode === "am" ? [530e3, 1700e3] : mode === "wbfm" ? [87.9e6, 107.9e6] : [24e6, 1766e6];
  const next = Math.min(hi, Math.max(lo, f + dir * RADIO_STEP[mode]));
  if (radio.on) return tune({ freqs: [next], mode, label: freqText(next, mode) });
  radio.last = { f: next, mode };
  save("radioLast", radio.last);
  paintDial();
}
function tuneTyped(text) {  // "98.5" (FM), "1190" (AM, kHz), "155.34" (narrow FM)
  const v = parseFloat(text.replace(",", "."));
  if (!v) return toast("Type a frequency, like 98.5 or 1190");
  const [f, mode] = v >= 530 && v <= 1700 && !text.includes(".") ? [v * 1e3, "am"] : v >= 87.5 && v <= 108 ? [v * 1e6, "wbfm"]
    : v >= 118 && v < 137 ? [v * 1e6, "air"] : [v * 1e6, "nfm"];
  closeSearch();
  tune({ freqs: [Math.round(f)], mode, label: freqText(f, mode) });
}

$("#radio-tabs").addEventListener("click", (e) => {
  const tab = e.target.closest("[data-tab]")?.dataset.tab;
  if (!tab) return;
  radio.tab = tab;
  save("radioTab", tab);
  renderRadio();
});
// "in 12 min" counts down, and a capture's progress shows up
setInterval(() => { if (currentView === "radio" && radio.tab === "sats") loadRadio(true); }, 30000);


// ---- news: RSS and Atom headlines (news.py), cached for when there's no signal ----
const news = { data: null, feed: load("newsFeed", ""), open: null, manage: false };
async function loadNews(refresh) {
  renderNews();
  try {
    news.data = refresh ? await api("/api/news/refresh", { json: {} }) : await api("/api/news");
    if (news.data.note) toast(news.data.note);
  } catch (e) { if (!news.data) news.data = { error: e.message }; }
  if (currentView === "news") renderNews();
}
function agoText(t) {
  const m = Math.round((Date.now() / 1000 - t) / 60);
  return !t ? "" : m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
}
function renderNews() {
  const d = news.data, box = $("#news-list");
  const chips = $("#news-feeds");
  if (!d) return (box.innerHTML = `<div class="empty">Loading…</div>`);
  if (d.error) return (box.innerHTML = `<div class="empty">${escapeHtml(d.error)}</div>`);
  chips.replaceChildren(...[{ name: "All", url: "" }, ...d.feeds].map((f) => {
    const b = document.createElement("button");
    b.textContent = f.name;
    b.classList.toggle("on", news.feed === f.url);
    b.onclick = () => { news.feed = f.url; save("newsFeed", f.url); news.open = null; renderNews(); };
    return b;
  }));
  if (news.manage) return renderNewsFeeds();
  const items = d.items.filter((i) => !news.feed || d.feeds.find((f) => f.url === news.feed)?.name === i.feed);
  $("#news-updated").textContent = d.updated ? `Updated ${agoText(d.updated)}` : "";
  if (!items.length) {
    return (box.innerHTML = `<div class="empty">${d.feeds.length ? "No headlines yet: they download when there's a connection (Settings → Data use)." : "No feeds. Tap Feeds to add one."}</div>`);
  }
  box.replaceChildren(...items.map((it) => {
    const b = document.createElement("div");
    b.className = "news-item" + (news.open === it.link + it.title ? " open" : "");
    b.innerHTML = `<button class="news-head"><span class="t"></span><span class="s"></span></button>
      <div class="news-body"><p></p><div class="news-acts"><button class="btn"><svg><use href="#i-chat"/></svg>Read aloud</button></div></div>`;
    b.querySelector(".t").textContent = it.title;
    b.querySelector(".s").textContent = [it.feed, agoText(it.t)].filter(Boolean).join(" · ");
    b.querySelector("p").textContent = it.summary || "No summary in the feed.";
    b.querySelector(".news-head").onclick = () => { news.open = news.open === it.link + it.title ? null : it.link + it.title; renderNews(); };
    b.querySelector(".news-acts .btn").onclick = () => say(`${it.title}. ${it.summary || ""}`.slice(0, 700));
    return b;
  }));
}
function renderNewsFeeds() {
  const box = $("#news-list");
  const rows = [head("Feeds", "Add feed", () => openSearch("", "feed"))];
  for (const f of news.data.feeds) {
    const d = document.createElement("div");
    d.className = "list-item media-row";
    d.innerHTML = `<span class="thumb"><svg><use href="#i-news"/></svg></span><span class="txt"><span class="t"></span><span class="s"></span></span><button class="btn danger">Remove</button>`;
    d.querySelector(".t").textContent = f.name;
    d.querySelector(".s").textContent = news.data.errors?.[f.url] ? `Last fetch failed: ${news.data.errors[f.url]}` : f.url;
    d.querySelector("button").onclick = async () => {
      if (!confirm(`Remove ${f.name}?`)) return;
      news.data.feeds = (await api("/api/news/remove", { json: { url: f.url } })).feeds;
      if (news.feed === f.url) news.feed = "";
      loadNews();
    };
    rows.push(d);
  }
  rows.push(note("Any RSS or Atom feed: a news site, a local paper, a blog. Type its address, like npr.org/rss or the full feed link."));
  box.replaceChildren(...rows);
}
async function addFeed(url) {
  toast("Checking the feed…", 20000);
  try {
    await api("/api/news/add", { json: { url } });
    toast("Feed added");
  } catch (e) { toast(`Couldn't add it: ${e.message}`, 5000); }
  loadNews();
}
// "truck, read the news": the top headlines, spoken
async function readHeadlines(n = 5) {
  if (!news.data?.items) await loadNews();
  const items = news.data?.items?.slice(0, n) || [];
  if (!items.length) { say("No headlines yet"); return "No headlines yet"; }
  say(`Here are the headlines. ${items.map((i) => `From ${i.feed}: ${i.title}.`).join(" ")}`);
  return `Reading ${items.length} headlines`;
}

// ---- video ----
const video = $("#video");
function playVideo(v) {
  audio.pause();
  $("#video-stage").hidden = false;
  $("#video-title").textContent = v.title;
  video.src = `/api/media/file?id=${encodeURIComponent(v.id)}`;
  video.play().catch((e) => e.name !== "AbortError" && toast(`Can't play: ${e.message}`));
}
function stopVideo() {
  if ($("#video-stage").hidden) return;
  video.pause();
  video.removeAttribute("src");
  video.load();
  $("#video-stage").hidden = true;
}

// ---- cameras ----
const BLANK = "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";
let camsOn = false;
function setCameras(on) {
  camsOn = on;
  for (const fig of $$(".cam")) {
    const img = $("img", fig), msg = $(".cam-msg", fig);
    if (on && lastState?.phone?.online) {
      msg.textContent = "Connecting…";
      img.onload = () => (msg.textContent = "");
      img.onerror = () => { if (camsOn) msg.textContent = "Camera unavailable (busy or phone disconnected)"; };
      img.src = `/api/camera/mjpeg?cam=${fig.dataset.cam}&fps=10&t=${Date.now()}`;
    } else {
      img.onerror = null;
      img.src = BLANK;  // closes the stream so the phone stops encoding it
      msg.textContent = on ? "Phone not connected" : "";
    }
  }
}

// ---- vehicle gauges ----
function drawCompass(deg) {
  const c = $("#compass"), g = c.getContext("2d"), r = c.width / 2;
  const cs = getComputedStyle(document.documentElement);
  g.clearRect(0, 0, c.width, c.height);
  g.save();
  g.translate(r, r);
  g.rotate(-(deg ?? 0) * RAD);
  g.strokeStyle = cs.getPropertyValue("--line");
  g.lineWidth = 2;
  for (let a = 0; a < 360; a += 15) {
    g.beginPath();
    const long = a % 90 === 0;
    g.moveTo(0, -r + 8);
    g.lineTo(0, -r + (long ? 26 : 18));
    g.strokeStyle = long ? cs.getPropertyValue("--text") : cs.getPropertyValue("--text-2");
    g.stroke();
    g.rotate(15 * RAD);
  }
  g.restore();
  // Letters ride the rotating dial but stay upright.
  g.font = "600 26px system-ui";
  g.textAlign = "center";
  g.textBaseline = "middle";
  ["N", "E", "S", "W"].forEach((t, i) => {
    const a = (i * 90 - (deg ?? 0)) * RAD;
    g.fillStyle = t === "N" ? cs.getPropertyValue("--bad") : cs.getPropertyValue("--text");
    g.fillText(t, r + Math.sin(a) * (r - 46), r - Math.cos(a) * (r - 46));
  });
  g.fillStyle = cs.getPropertyValue("--accent");
  g.beginPath();
  g.moveTo(r, r - 34); g.lineTo(r + 16, r + 20); g.lineTo(r, r + 10); g.lineTo(r - 16, r + 20);
  g.closePath();
  g.fill();
}
// g: [lateral, longitudinal] in g (right and forward positive); from the GPS (server.py gforce_from_gps).
function drawG(gv) {
  const c = $("#gmeter"), g = c.getContext("2d"), r = c.width / 2, scale = (r - 14) / 0.6;
  const cs = getComputedStyle(document.documentElement);
  g.clearRect(0, 0, c.width, c.height);
  g.strokeStyle = cs.getPropertyValue("--line");
  g.lineWidth = 2;
  for (const ring of [0.2, 0.4, 0.6]) { g.beginPath(); g.arc(r, r, ring * scale, 0, 2 * Math.PI); g.stroke(); }
  g.beginPath(); g.moveTo(r, 10); g.lineTo(r, c.height - 10); g.moveTo(10, r); g.lineTo(c.width - 10, r); g.stroke();
  if (!gv) return ($("#g-total").textContent = "--");
  const gx = Math.max(-0.6, Math.min(0.6, gv[0])), gy = Math.max(-0.6, Math.min(0.6, gv[1]));
  g.fillStyle = cs.getPropertyValue("--accent");
  g.beginPath(); g.arc(r + gx * scale, r - gy * scale, 11, 0, 2 * Math.PI); g.fill();
  $("#g-total").textContent = Math.hypot(gv[0], gv[1]).toFixed(2);
}
// Where the satellites are: north up, the horizon at the edge, overhead in the middle. Used ones in the accent
// color, bigger for a stronger signal.
function drawSky(sats) {
  const c = $("#skyplot"), g = c.getContext("2d"), r = c.width / 2, R = r - 24;
  const cs = getComputedStyle(document.documentElement);
  g.clearRect(0, 0, c.width, c.height);
  g.strokeStyle = cs.getPropertyValue("--line");
  g.lineWidth = 2;
  for (const k of [1, 2 / 3, 1 / 3]) { g.beginPath(); g.arc(r, r, R * k, 0, 2 * Math.PI); g.stroke(); }
  g.beginPath(); g.moveTo(r, r - R); g.lineTo(r, r + R); g.moveTo(r - R, r); g.lineTo(r + R, r); g.stroke();
  g.fillStyle = cs.getPropertyValue("--text-2");
  g.font = "600 15px system-ui, sans-serif";
  g.textAlign = "center";
  g.textBaseline = "middle";
  for (const [t, x, y] of [["N", r, 11], ["E", c.width - 10, r], ["S", r, c.height - 11], ["W", 10, r]]) g.fillText(t, x, y);
  let used = 0;
  for (const [az, el, ss, u] of sats || []) {
    const d = (R * (90 - Math.max(0, el))) / 90, a = az * RAD;
    used += u;
    g.globalAlpha = u ? 1 : 0.45;
    g.fillStyle = u ? cs.getPropertyValue("--accent") : cs.getPropertyValue("--text-2");
    g.beginPath(); g.arc(r + d * Math.sin(a), r - d * Math.cos(a), 4 + (Math.min(ss, 45) / 45) * 6, 0, 2 * Math.PI); g.fill();
  }
  g.globalAlpha = 1;
  $("#sky-count").textContent = sats?.length ? `${used}/${sats.length}` : "--";
}
function kvRows(rows) {
  return rows.map((r) => (typeof r === "string" ? `<div class="kv-head">${r}</div>`
    : `<div class="kv-row"><span>${r[0]}</span><span class="${r[2] || ""}">${r[1]}</span></div>`)).join("");
}
function renderVehicle(s) {
  const p = s.position, sys = s.system || {}, ph = s.phone || {}, batt = ph.status?.battery;
  renderObd(s.obd);
  if (!tripInfo || Date.now() - (renderVehicle.tripAt || 0) > 5000) { renderVehicle.tripAt = Date.now(); loadTrip(); }
  drawCompass(p ? heading : null);
  const acc = ph.linear_accel;  // the phone's accelerometer, when one is connected
  drawG(s.gps.g || (acc ? [acc[0] / 9.81, acc[1] / 9.81] : null));
  drawSky(s.gps.sky);
  const src = p ? (p.source === "gps" ? "USB GPS" : "Phone GPS") : s.gps.gpsd ? "Searching for satellites" : "gpsd not running";
  const power = sys.undervolt_now ? ["Under-voltage now", "bad"] : sys.undervolt_since_boot ? ["Dipped since boot", "warn-t"] : ["OK", "good"];
  $("#vehicle-kv").innerHTML = kvRows([
    "Position",
    ["Source", src, p ? (p.source === "gps" ? "good" : "warn-t") : "bad"],
    ["Fix", p ? (p.mode >= 3 ? "3D" : "2D") : "none"],
    ["Satellites (USB GPS)", s.gps.sats_seen != null ? `${s.gps.sats_used ?? 0} used / ${s.gps.sats_seen} seen` : "--"],
    ["Accuracy", p?.accuracy_m != null ? `± ${distText(p.accuracy_m)}` : "--"],
    ["Coordinates", p ? `${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}` : "--"],
    ["Altitude", p?.alt_m != null ? `${altVal(p.alt_m)} ${settings.units === "imperial" ? "ft" : "m"}` : "--"],
    ["Heading", p ? `${Math.round(heading)}° ${cardinal(heading)}` : "--"],
    "Head unit",
    ["Power", power[0], power[1]],
    ["CPU temperature", sys.cpu_temp_c != null ? `${sys.cpu_temp_c.toFixed(0)} °C` : "--", sys.cpu_temp_c > 75 ? "bad" : ""],
    ["Load", sys.load != null ? sys.load.toFixed(2) : "--"],
    ["Free memory", sys.mem_available_mb != null ? `${sys.mem_available_mb} MB` : "--"],
    "Phone",
    ["Link", ph.online ? "Connected" : "Not connected", ph.online ? "good" : "bad"],
    ["Battery", batt ? `${batt.percent}% · ${batt.plugged === "none" ? "not charging" : "charging"}` : "--"],
    ["Battery temperature", batt ? `${batt.temp_c.toFixed(0)} °C` : "--", batt?.temp_c > 45 ? "bad" : ""],
    ["Hottest sensor", ph.status?.max_thermal_c != null ? `${ph.status.max_thermal_c.toFixed(0)} °C` : "--"],
  ]);
}


// ---- OBD-II (obd.py): engine gauges, check-engine codes, fuel economy ----
const OBD_TILES = [
  ["rpm", "rpm", (v) => Math.round(v)],
  ["coolant", "coolant", (v) => tempText(v)],
  ["voltage", "volts", (v) => v.toFixed(1)],
  ["fuel", "fuel", (v) => `${Math.round(v)}%`],
  ["load", "load", (v) => `${Math.round(v)}%`],
  ["throttle", "throttle", (v) => `${Math.round(v)}%`],
  ["intake", "intake air", (v) => tempText(v)],
  ["oil", "oil", (v) => tempText(v)],
];
const tempText = (c) => settings.units === "imperial" ? `${Math.round(c * 9 / 5 + 32)}°F` : `${Math.round(c)}°C`;
let obdFull = null;
function renderObd(o) {
  const card = $("#obd-card");
  if (!o || o.status === "off") return (card.hidden = true);
  card.hidden = false;
  const live = o.status === "connected";
  $("#obd-state").textContent = { connected: "Connected", searching: "Looking for an adapter…", connecting: "Connecting…",
    "no adapter": "No adapter found", "no ecu": "Adapter found; the truck isn't answering (ignition on?)", starting: "Starting…" }[o.status] || o.status;
  $("#obd-state").className = live ? "good" : "";
  const tiles = $("#obd-tiles");
  tiles.hidden = !live;
  const v = o.v || {};
  const econ = o.mpg != null ? [`${settings.units === "imperial" ? o.mpg : (235.21 / o.mpg).toFixed(1)}`, settings.units === "imperial" ? "mpg now" : "L/100 km now"]
    : o.gph != null ? [settings.units === "imperial" ? o.gph.toFixed(2) : (o.gph * 3.785).toFixed(1), settings.units === "imperial" ? "gal/h idle" : "L/h idle"] : null;
  const trip = o.trip_mpg != null ? [settings.units === "imperial" ? o.trip_mpg : (235.21 / o.trip_mpg).toFixed(1), settings.units === "imperial" ? "mpg trip" : "L/100 km trip"] : null;
  const items = [...(econ ? [econ] : []), ...(trip ? [trip] : []),
    ...OBD_TILES.filter(([k]) => v[k] != null).map(([k, label, f]) => [f(v[k]), label, k])];
  tiles.innerHTML = items.slice(0, 8).map(([val, label, k]) => {
    const warn = (k === "coolant" && v.coolant > 110) || (k === "voltage" && (v.voltage < 12.2 || v.voltage > 15)) ? " warn" : "";
    return `<div class="obd-tile${warn}"><b>${escapeHtml(String(val))}</b><span>${escapeHtml(label)}</span></div>`;
  }).join("");
  const cel = $("#obd-cel");
  cel.hidden = !live;
  cel.className = "btn obd-cel " + (o.codes ? "bad" : o.mil === false ? "good" : "");
  cel.querySelector("span").textContent = o.codes ? `Check engine: ${o.codes} code${o.codes === 1 ? "" : "s"}` : o.mil === false ? "No engine codes" : "Engine codes: tap to read";
  $("#obd-find").hidden = live;
}
async function showObdCodes(refresh) {
  $("#obd-codes").hidden = false;
  $("#obd-codes").innerHTML = `<div class="empty">Reading codes…</div>`;
  obdFull = await (refresh ? api("/api/obd/codes", { json: {} }) : api("/api/obd")).catch((e) => ({ error: e.message }));
  paintObdCodes();
}
function paintObdCodes() {
  const o = obdFull, box = $("#obd-codes");
  if (o.error && !o.dtcs) return (box.innerHTML = `<div class="empty">${escapeHtml(o.error)}</div>`);
  const row = (d, pending) => `<div class="kv-row"><span><b>${d.code}</b>${pending ? " · pending" : ""}</span><span>${escapeHtml(d.text)}</span></div>`;
  box.innerHTML = [
    ...(o.dtcs || []).map((d) => row(d, false)),
    ...(o.pending || []).map((d) => row(d, true)),
    ...(!(o.dtcs || []).length && !(o.pending || []).length ? [`<div class="empty">No stored or pending codes.</div>`] : []),
    `<div class="obd-acts">${o.vin ? `<small>VIN ${o.vin} · ${escapeHtml(o.protocol || "")}</small>` : ""}
      <button class="btn" data-action="obd-reread">Read again</button>
      ${(o.dtcs || []).length ? `<button class="btn danger" data-action="obd-clear">Clear codes</button>` : ""}</div>`,
  ].join("");
}
async function obdClear() {
  if (!confirm("Clear the check-engine codes and turn the light off? Fix the cause first, or they'll come back. This also resets the emissions readiness monitors (an inspection needs a few days of driving after).")) return;
  obdFull = await api("/api/obd/clear", { json: {} }).catch((e) => ({ ...obdFull, error: e.message }));
  paintObdCodes();
  toast(obdFull.dtcs?.length ? "Some codes are still there" : "Codes cleared");
}
async function obdFind() {
  const box = $("#obd-codes");
  box.hidden = false;
  box.innerHTML = `<div class="empty">Looking for Bluetooth OBD adapters for 12 seconds… (plug it in with the ignition on)</div>`;
  const { devices = [] } = await api("/api/obd/scan", { json: {} }).catch(() => ({}));
  if (!devices.length) {
    box.innerHTML = `<div class="empty">None found. A USB adapter is found by itself; a Wi-Fi one needs OBD_DEVICE=tcp:192.168.0.10:35000 in nomad.conf.</div>`;
    return;
  }
  box.replaceChildren(...devices.map((d) => {
    const b = document.createElement("button");
    b.className = "list-item media-row";
    b.innerHTML = `<span class="thumb"><svg><use href="#i-car"/></svg></span><span class="txt"><span class="t"></span><span class="s"></span></span>`;
    b.querySelector(".t").textContent = d.name;
    b.querySelector(".s").textContent = d.paired ? "Paired: connecting" : "Tap to pair (PIN 1234 or 0000, tried for you)";
    b.onclick = async () => {
      toast(`Pairing ${d.name}…`, 30000);
      try { await api("/api/obd/pair", { json: { mac: d.mac } }); toast(`Paired ${d.name}`); box.hidden = true; } catch (e) { toast(e.message, 5000); }
    };
    return b;
  }));
}
// "truck, any engine codes?" / "check engine light"
async function obdSay() {
  const o = await api("/api/obd/codes", { json: {} }).catch(() => null);
  let msg;
  if (!o || o.status !== "connected") msg = "The OBD adapter isn't connected";
  else if (o.dtcs?.length) msg = `${o.dtcs.length} code${o.dtcs.length === 1 ? "" : "s"}: ${o.dtcs.map((d) => `${d.code.split("").join(" ")}, ${d.text}`).join(". ")}`;
  else msg = o.pending?.length ? `No stored codes, but ${o.pending.length} pending: ${o.pending.map((d) => d.text).join(", ")}` : "No engine codes";
  say(msg);
  return msg;
}


// ---- LoRa: the Meshtastic mesh (mesh_bridge.py): messages and nodes, off-grid ----
const mesh = { state: null, to: null, seen: load("meshSeen", 0) };
const QUICK = ["On my way", "Running late", "Arrived", "OK", "Call me when you can"];
async function loadMesh() {
  mesh.state = await api("/api/mesh").catch((e) => ({ status: "not running", error: e.message, nodes: [], messages: [], channels: [], prefs: {} }));
  if (currentView === "radio" && radio.tab === "mesh") renderRadio();
}
function meshRows() {
  const s = mesh.state, rows = [];
  if (!s) { loadMesh(); return [note("Loading…")]; }
  if (s.status !== "connected") {
    rows.push(note({ "not running": "The mesh service isn't running (sudo systemctl enable --now mesh).", "no radio": "No Meshtastic radio found. Plug one in by USB (Heltec, RAK, T-Beam...), or set MESH_DEVICE=tcp:<address> for one on Wi-Fi.",
      off: "LoRa is off (MESH_DEVICE=off in nomad.conf).", searching: "Looking for the radio…", starting: "Starting…" }[s.status] || s.status));
    rows.push(note("Meshtastic sends short texts and positions over LoRa, with no cell signal: several km between nodes, much further through the mesh."));
    return rows;
  }
  const meNode = s.nodes.find((n) => n.me);
  rows.push(head(`Messages · ${meNode?.name || "this radio"}`, mesh.to ? `To ${nodeName(mesh.to)} ✕` : "Write", () => (mesh.to ? (mesh.to = null, renderRadio()) : openSearch("", "mesh"))));
  const msgs = s.messages.slice(-30);
  if (!msgs.length) rows.push(note("No messages yet. What others send on the mesh shows up here, and is read aloud."));
  for (const m of msgs) {
    const d = document.createElement("div");
    d.className = "mesh-msg" + (m.mine ? " mine" : "") + (m.dm ? " dm" : "");
    d.innerHTML = `<div class="mesh-meta"></div><div class="mesh-text"></div>`;
    d.querySelector(".mesh-meta").textContent = [m.mine ? (m.to_name ? `You → ${m.to_name}` : "You") : m.from_name, m.dm ? "direct" : "",
      fmtClock(new Date(m.t * 1000), true), m.snr != null && !m.mine ? `SNR ${m.snr}` : ""].filter(Boolean).join(" · ");
    d.querySelector(".mesh-text").textContent = m.text;
    if (!m.mine && m.from) d.onclick = () => { mesh.to = m.from; renderRadio(); toast(`Replies go to ${m.from_name}`); };
    rows.push(d);
  }
  const quick = document.createElement("div");
  quick.className = "chips mesh-quick";
  for (const q of [...QUICK, "📍 My location"]) {  // (setup-pi.sh installs an emoji font)
    const b = document.createElement("button");
    b.textContent = q;
    b.onclick = () => (q.startsWith("📍") ? meshSendLocation() : meshSend(q));
    quick.append(b);
  }
  const type = document.createElement("button");
  type.textContent = "Type…";
  type.className = "on";
  type.onclick = () => openSearch("", "mesh");
  quick.prepend(type);
  rows.push(quick);
  const others = s.nodes.filter((n) => !n.me);
  rows.push(head(`Nodes heard · ${others.length}`));
  for (const n of others) {
    const b = document.createElement("button");
    b.className = "list-item media-row";
    b.innerHTML = `<span class="thumb mesh-short"></span><span class="txt"><span class="t"></span><span class="s"></span></span><span class="sat-el"></span>`;
    b.querySelector(".mesh-short").textContent = n.short || "?";
    b.querySelector(".t").textContent = n.name;
    const far = n.lat != null && lastPos ? distText(haversine(lastPos.lat, lastPos.lon, n.lat, n.lon)) : null;
    b.querySelector(".s").textContent = [n.heard ? `heard ${agoText(n.heard)}` : "", n.hops === 0 ? "direct" : n.hops ? `${n.hops} hop${n.hops === 1 ? "" : "s"}` : "",
      n.snr != null ? `SNR ${n.snr}` : "", n.battery != null ? (n.battery > 100 ? "powered" : `${n.battery}%`) : ""].filter(Boolean).join(" · ");
    b.querySelector(".sat-el").textContent = far || "";
    b.onclick = () => { mesh.to = n.id; openSearch("", "mesh"); };
    rows.push(b);
  }
  rows.push(head("Settings"));
  rows.push(meshToggle("share", "Share the truck's position", "Every 5 minutes (sooner after moving), so others on the mesh see it on their maps"));
  rows.push(meshToggle("speak", "Read messages aloud", "Spoken when they arrive, like turn prompts"));
  return rows;
}
const nodeName = (id) => mesh.state?.nodes.find((n) => n.id === id)?.name || id;
function meshToggle(key, title, sub) {
  const b = document.createElement("button");
  b.className = "list-item media-row";
  const on = !!mesh.state.prefs?.[key];
  b.innerHTML = `<span class="txt"><span class="t"></span><span class="s"></span></span><span class="switch${on ? " on" : ""}"></span>`;
  b.querySelector(".t").textContent = title;
  b.querySelector(".s").textContent = sub;
  b.onclick = async () => {
    mesh.state.prefs = await api("/api/mesh/settings", { json: { [key]: !on } }).catch((e) => (toast(e.message), mesh.state.prefs));
    renderRadio();
  };
  return b;
}
async function meshSend(text) {
  if (!text?.trim()) return;
  try {
    await api("/api/mesh/send", { json: { text, to: mesh.to } });
    toast(mesh.to ? `Sent to ${nodeName(mesh.to)}` : "Sent to the mesh");
  } catch (e) { toast(`Not sent: ${e.message}`, 5000); }
  loadMesh();
}
function meshSendLocation() {
  if (!lastState?.position) return toast("No GPS fix to send");
  const p = lastState.position;
  meshSend(`📍 ${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}${whereNow ? ` (${whereText()})` : ""}`);
}
function meshGeojson() {
  return { type: "FeatureCollection", features: (lastState?.mesh?.nodes || []).map(([short, lat, lon, heard, name]) => ({
    type: "Feature", geometry: { type: "Point", coordinates: [lon, lat] },
    properties: { short, name, old: heard && Date.now() / 1000 - heard > 7200 ? 1 : 0 } })) };
}
let meshMapKey = "";
function checkMesh(s) {
  const b = s.mesh;
  const key = JSON.stringify(b?.nodes || []);
  if (map && key !== meshMapKey) { meshMapKey = key; map.getSource("mesh")?.setData(meshGeojson()); }
  const m = b?.last;
  if (!m || m.t <= mesh.seen) return;
  const first = !mesh.seen;
  mesh.seen = m.t;
  save("meshSeen", m.t);
  if (first) return;  // the backlog on the first start isn't news
  toast(`Mesh · ${m.from_name}: ${m.text}`, 8000);
  if (b.speak) say(`Mesh message from ${m.from_name}: ${m.text}`);
  if (currentView === "radio" && radio.tab === "mesh") loadMesh();
}
function haversine(la1, lo1, la2, lo2) {
  const r = Math.PI / 180, a = Math.sin((la2 - la1) * r / 2) ** 2 + Math.cos(la1 * r) * Math.cos(la2 * r) * Math.sin((lo2 - lo1) * r / 2) ** 2;
  return 12742000 * Math.asin(Math.sqrt(a));
}


// ---- trip computer and engine warnings (trip.py) ----
const MI = 1.609344;
const distUnit = (km, digits = 1) => km == null ? "--" : settings.units === "imperial" ? `${(km / MI).toFixed(digits)} mi` : `${km.toFixed(digits)} km`;
const econText = (mpg) => mpg == null ? "--" : settings.units === "imperial" ? `${mpg} mpg` : `${(235.21 / mpg).toFixed(1)} L/100 km`;
let tripInfo = null;
async function loadTrip() {
  tripInfo = await api("/api/trip").catch(() => tripInfo);
  if (currentView === "vehicle") renderTrip();
  if (currentView === "settings") paintTripSettings();
}
function renderTrip() {
  const t = tripInfo, box = $("#trip-kv");
  if (!t) return;
  const dur = (s) => s < 3600 ? `${Math.round(s / 60)} min` : `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
  const rows = [];
  for (const k of ["a", "b"]) {
    const x = t.trips[k];
    rows.push(`<div class="trip-row"><b>Trip ${k.toUpperCase()}</b><div class="trip-stats"><span>${distUnit(x.km)}</span><span>${econText(x.mpg)}</span>
      <span>${x.avg_kmh != null ? (settings.units === "imperial" ? `${Math.round(x.avg_kmh / MI)} mph avg` : `${Math.round(x.avg_kmh)} km/h avg`) : "--"}</span>
      <span>${dur(x.moving_s)} moving</span></div><button class="btn" data-action="trip-reset" data-trip="${k}">Reset</button></div>`);
  }
  rows.push(`<div class="trip-row foot"><span>Odometer ${t.odo_km != null ? distUnit(t.odo_km, 0) : "not set (Settings)"}</span>
    <span>${t.oil_left_km == null ? "" : t.oil_left_km > 0 ? `Oil change in ${distUnit(t.oil_left_km, 0)}` : `<b class="bad">Oil change due</b>`}</span></div>`);
  box.innerHTML = rows.join("");
}
function paintTripSettings() {
  const t = tripInfo;
  if (!t) return;
  $("#odo-state").textContent = t.odo_km != null ? `${distUnit(t.odo_km, 0)} (counts up as you drive)` : "Set it once from the dash; it counts up from there";
  $("#oil-state").textContent = t.oil_left_km == null ? "Set the odometer first" : t.oil_left_km > 0 ? `Next in ${distUnit(t.oil_left_km, 0)}` : "Due now";
  const every = Math.round(t.oil_every_km / MI);
  for (const b of $$("#oil-seg button")) b.classList.toggle("on", Math.abs(+b.dataset.v - every) < 50);
}
async function setOdometer(text) {
  const v = parseFloat(text.replace(/,/g, ""));
  if (!(v >= 0)) return toast("Type the odometer reading, like 182340");
  tripInfo = await api("/api/trip/odometer", { json: { km: settings.units === "imperial" ? v * MI : v } });
  paintTripSettings();
  toast("Odometer set");
}
$("#oil-seg").addEventListener("click", async (e) => {
  const v = e.target.closest("button")?.dataset.v;
  if (!v || !tripInfo) return;
  if (tripInfo.odo_km == null) return toast("Set the odometer first");
  // only the interval: count from the last change
  tripInfo = await api("/api/trip/oil", { json: { every_km: +v * MI } }).catch((err) => (toast(err.message), tripInfo));
  paintTripSettings();
});
// Each warning is shown and said once while it lasts (the server keeps one id per occurrence).
const alertsSaid = new Set(load("alertsSaid", []));
function checkAlerts(s) {
  for (const a of s.alerts || []) {
    if (alertsSaid.has(a.id)) continue;
    alertsSaid.add(a.id);
    save("alertsSaid", [...alertsSaid].slice(-40));
    toast(`${a.level === "bad" ? "⚠ " : ""}${a.text}`, a.level === "bad" ? 12000 : 7000);
    say(a.text);
  }
}
function paintEngineQuick(s) {
  const o = s.obd, live = o?.status === "connected";
  for (const el of $$(".quick-card .eng")) el.hidden = !live;
  if (!live) return;
  const v = o.v || {};
  bind("eng-econ", o.mpg != null ? (settings.units === "imperial" ? String(o.mpg) : (235.21 / o.mpg).toFixed(1)) : "--");
  bind("eng-econ-unit", settings.units === "imperial" ? "mpg" : "L/100km");
  bind("eng-cool", v.coolant != null ? tempText(v.coolant) : "--");
  bind("eng-fuel", v.fuel != null ? `${Math.round(v.fuel)}%` : "--");
  bind("eng-volt", v.voltage != null ? v.voltage.toFixed(1) : "--");
}

// ---- live state ----
let lastState = null;
function onState(s) {
  const wasOnline = lastState?.phone?.online;
  if (s.speaking !== lastState?.speaking) duck(s.speaking);
  lastState = s;
  const p = s.position;
  updatePosition(p);
  updateWhere(p);
  checkQuakeAlerts(s);
  checkMesh(s);
  checkAlerts(s);
  paintEngineQuick(s);
  $("#st-net").setAttribute("class", "st " + (s.data === "offline" ? "off" : s.online ? (s.data === "saver" ? "warn" : "ok") : "off"));
  $("#st-net title").textContent = s.data === "offline" ? "Offline (Settings → Data use)" : !s.online ? "Offline: maps, routing, voice and music still work"
    : s.data === "saver" ? "Online, saving data" : "Online";
  bind("speed", speedVal(speedNow(s)));
  bind("speed-unit", speedUnit());
  bind("heading", p ? cardinal(heading) : "--");
  bind("altitude", altVal(p?.alt_m));
  bind("alt-unit", settings.units === "imperial" ? "ft" : "m");
  const batt = s.phone?.status?.battery;
  bind("phone-batt", s.phone?.online && batt ? `${batt.percent}%` : "--");
  bind("sats", s.gps?.mode >= 2 ? `${s.gps.sats_used ?? 0}/${s.gps.sats_seen ?? "?"}` : "--");

  $("#st-gps").setAttribute("class", "st " + (p ? (p.source === "gps" && p.mode >= 3 ? "ok" : "warn") : s.gps.gpsd ? "off" : "bad"));
  $("#st-phone").setAttribute("class", "st " + (s.phone?.online ? "ok" : "off"));
  $('.rail-btn[data-view="cameras"]').hidden = !s.phone?.online;  // its cameras come from the phone (DashLink)
  const pw = $("#st-power");
  pw.hidden = !(s.system?.undervolt_now || s.system?.undervolt_since_boot);
  pw.setAttribute("class", "st " + (s.system?.undervolt_now ? "bad" : "warn"));

  const dc = s.dashcam, dcState = $("#dc-state");
  if (!s.phone?.online || !dc) { dcState.textContent = "Phone not connected"; dcState.className = "dc-state"; $("#dc-sub").textContent = ""; }
  else {
    dcState.textContent = dc.recording ? `Recording ${dc.channels.map((c) => ({ 0: "rear", 1: "cabin", 2: "road" })[c.camera] || `cam ${c.camera}`).join(" + ")}` : "Not recording";
    dcState.className = "dc-state" + (dc.recording ? " rec" : "");
    $("#dc-sub").textContent = `${dc.segments} clips · ${dc.locked} saved · ${(dc.bytes / 1e9).toFixed(1)} of ${(dc.max_bytes / 1e9).toFixed(0)} GB`;
  }
  if (currentView === "vehicle") renderVehicle(s);
  if (currentView === "cameras" && wasOnline !== s.phone?.online) setCameras(true);
}

function connect() {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => { try { onState(JSON.parse(e.data)); } catch (err) { console.error(err); } };
}

// ---- voice ----
let listening = false;
// Listen once. The server streams each stage as a line of JSON (listening, speech, heard, done); onPhase sees them
// all, and the "done" line (with the transcript) is returned.
async function listen(onPhase = () => {}) {
  if (listening) return null;
  listening = true;
  $("#mic-btn").classList.add("listening");
  try {
    const r = await fetch("/api/voice", { method: "POST" });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`);
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "", last = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      for (let i; (i = buf.indexOf("\n")) >= 0; buf = buf.slice(i + 1)) {
        const msg = JSON.parse(buf.slice(0, i));
        if (msg.phase === "heard") $("#mic-btn").classList.remove("listening");
        onPhase(msg.phase, msg);
        if (msg.phase === "done") last = msg;
      }
    }
    if (!last) throw new Error("voice stopped early");
    if (last.ok === false) throw new Error(last.error || "speech to text failed");
    return last;
  } finally {
    listening = false;
    $("#mic-btn").classList.remove("listening");
  }
}

// Two falling notes when it stops listening, so the driver knows without looking.
function earcon() {
  try {
    const ctx = viz?.ctx || (earcon.ctx ||= new AudioContext()), t = ctx.currentTime;
    for (const [f, dt] of [[988, 0], [740, 0.11]]) {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t + dt);
      g.gain.exponentialRampToValueAtTime(0.22, t + dt + 0.015);
      g.gain.exponentialRampToValueAtTime(0.0001, t + dt + 0.1);
      o.connect(g).connect(ctx.destination);
      o.start(t + dt);
      o.stop(t + dt + 0.12);
    }
  } catch { /* no audio: the card still shows it */ }
}

// The hovering voice card: what stage it's at, what it heard, and what it did.
let voiceHide;
function setVoice(stage, line, heard) {
  const card = $("#voice-card");
  clearTimeout(voiceHide);
  card.className = `voice-card ${stage}`;
  card.hidden = false;
  $("#voice-state").textContent = line;
  if (heard !== undefined) $("#voice-text").textContent = heard ? `“${heard.trim()}”` : "";
  $("#voice-icon").setAttribute("href", stage === "done" ? "#i-check" : stage === "error" ? "#i-close" : "#i-mic");
}
function hideVoice(ms) {
  clearTimeout(voiceHide);
  voiceHide = setTimeout(() => ($("#voice-card").hidden = true), ms);
}
async function voiceCommand() {
  setVoice("listening", "Listening…", "");
  try {
    const r = await listen((phase) => {
      if (phase === "speech") setVoice("speech", "Hearing you…");
      if (phase === "heard") { earcon(); setVoice("heard", "Got it"); }
    });
    if (!r) return hideVoice(0);
    if (!r.heard || !r.text) { setVoice("error", "Didn't hear anything", ""); return hideVoice(2500); }
    setVoice("thinking", "Working on it…", r.text);
    const reply = await handleUtterance(r.text);
    setVoice("done", reply, r.text);
    hideVoice(Math.max(4000, reply.length * 70));  // long enough to read a chat answer
  } catch (e) {
    setVoice("error", `Voice failed: ${e.message}`);
    hideVoice(4000);
  }
}

// "Truck, ..." (NOMAD_NAME in nomad.conf) is a command; anything else is conversation.
let NAME = "Truck";
const keywordRe = (name) => {
  const w = name.toLowerCase().replace(/[^a-z0-9 ]/g, "");
  const alts = w === "truck" ? "truck|trucks|trunk" : `${w}s?`;  // whisper sometimes hears trunk or trucks
  return new RegExp(`^\\W*(?:hey,? )?(?:${alts})\\b[\\s,.:;!-]*`, "i");
};
let KEYWORD = keywordRe(NAME);
api("/api/config").then((c) => { NAME = c.name || NAME; KEYWORD = keywordRe(NAME); }).catch(() => {});
async function handleUtterance(raw) {
  const m = raw.match(KEYWORD);
  if (!m) return chat(raw);
  const rest = raw.slice(m[0].length).trim();
  if (!rest) return `Say “${NAME.toLowerCase()}” and then the command`;
  return runCommand(rest);
}

// Where we are in words ("in Longview", "6 miles N of Woodland"), refreshed after moving a kilometer or so.
let whereNow = null, whereAt = null;
function updateWhere(p) {
  if (!p || (whereAt && Math.hypot(p.lat - whereAt.lat, p.lon - whereAt.lon) < 0.01)) return;
  whereAt = { lat: p.lat, lon: p.lon };
  api(`/api/where?lat=${p.lat}&lon=${p.lon}`).then((w) => (whereNow = w?.name ? w : null)).catch(() => {});
}
const whereText = (w = whereNow) => (!w ? "" : w.km < 2.5 ? `in ${w.name}` : `${spokenDist(w.km * 1000)} ${w.dir} of ${w.name}`);
// The forecast, kept current in the background for the voice and the assistant (the server caches it).
function refreshWeather() { api("/api/weather").then((w) => { if (!w.error) { radio.weather = w; radio.weatherAt = Date.now(); } }).catch(() => {}); }
setInterval(refreshWeather, 600000);
setTimeout(refreshWeather, 5000);
function weatherText() {
  const p = radio.weather?.periods?.[0];
  if (!p) return "";
  const alerts = (radio.weather.alerts || []).map((a) => a.event).join(", ");
  return `weather ${p.name.toLowerCase()}: ${p.shortForecast.toLowerCase()}, ${p.temperature}°${p.temperatureUnit}${p.rain ? `, ${p.rain}% rain` : ""}` +
    (alerts ? `; active alerts: ${alerts}` : "");
}
function carContext() {
  const parts = [`${fmtClock(new Date(), true)}, ${new Date().toLocaleDateString([], { weekday: "long" })}`];
  if (whereNow) parts.push(whereText());
  if (weatherText()) parts.push(weatherText());
  if (nav.route && nav.dest) parts.push(`navigating to ${nav.dest.name}, arriving ${$("#nav-eta").textContent}, ${$("#nav-left-time").textContent} left`);
  const t = curTrack();
  if (t && audio.src && !audio.paused) parts.push(`playing “${t.title}” by ${t.artist} from the album ${t.album}${t.year ? ` (${t.year})` : ""}`);
  if (speedNow(lastState) != null) parts.push(`${speedVal(speedNow(lastState))} ${settings.units === "imperial" ? "mph" : "km/h"}`);
  return parts.join("; ");
}

async function chat(raw) {
  let r;
  try {
    r = await api("/api/chat", { json: { text: raw, context: carContext() } });
  } catch (e) {
    return `Can't talk right now: ${e.message}`;
  }
  if (r.reply) speak(r.reply);  // spoken even with direction prompts off
  return r.reply || "…";
}

async function runCommand(raw, fromModel = false) {
  const text = raw.toLowerCase().replace(/[.,!?]/g, "").replace(/\s+/g, " ").trim();
  let m;
  if (/^(?:(?:navigate|directions|drive|go|head|take me|get me)(?: me)? (?:to )?)?(?:my )?(?:home|house)$|^(?:take me|go|head|drive) home$/.test(text)) {
    return goHome() ? "Navigating home" : "No home set yet";
  }
  if (/evacuat|tsunami|high ground|assembly area/.test(text)) return emergencyGo("evacuate");
  if ((m = text.match(/(?:nearest|closest|find|take me to|navigate to|where is) (?:an? |the )?(?:nearest |closest )?(emergency shelter|shelter|red cross|hospital|emergency room|er|fire station|community cent(?:er|re))/))) {
    return emergencyGo({ "red cross": "redcross", hospital: "hospital", "emergency room": "hospital", er: "hospital", "fire station": "fire" }[m[1]] ||
      (m[1].startsWith("community") ? "community" : "shelter"));
  }
  if ((m = text.match(/^(?:navigate|directions|take me|drive|go|get directions)(?: me)? to (?:the )?(.+)$/))) {
    query = m[1];
    const results = await runSearch();
    if (!results?.length) return `No places found for “${m[1]}”`;
    await previewRoute(results[0]);
    if (nav.route) startNav();
    return `Navigating to ${results[0].name}`;
  }
  if (/^(?:find (?:me )?(?:some )?|(?:the )?nearest |closest |i need |we need |low on )(gas|fuel|a gas station|gas station)/.test(text)) {
    query = "gas";
    const results = await runSearch();
    closeSearch();
    if (!results?.length) return "No gas stations found nearby";
    await previewRoute(results[0]);
    if (nav.route) startNav();
    const msg = `Navigating to ${results[0].name}`;
    say(msg);
    return msg;
  }
  if (/^(?:find|search(?: for)?|where is) (.+)$/.test(text)) {
    openSearch(text.replace(/^(?:find|search(?: for)?|where is) /, ""));
    return "Searching";
  }
  if (/(cancel|stop|end|exit) (the )?(navigation|route|directions)/.test(text)) { endNav(); return "Navigation ended"; }
  if (/^(pause|stop)( the)?( music| song)?$/.test(text)) { audio.pause(); return "Paused"; }
  if (/^(next|skip)( this| the)?( song| track)?$/.test(text)) { skipTrack(1); return "Next track"; }
  if (/^(previous|last|go back)( a| one)?( song| track)?$/.test(text)) { skipTrack(-1); return "Previous track"; }
  if ((m = text.match(/^play (?:some |the )?(.+?)(?: music| songs)?$/)) && !/^(music|something|a song)$/.test(m[1])) {
    await loadMedia();
    const hits = findMusic(m[1].replace(/ by /, " "), 1);  // an artist beats an album beats a song
    const h = hits[0];
    if (!h || /^(something|anything|music|songs|stuff) /.test(m[1])) {  // "play something chill": a vibe mix
      const r = await makeVibe(m[1].replace(/^(something|anything|music|songs|stuff) (for |that'?s |to )?/, ""));
      return r ? `Playing a ${r.name} mix` : `Nothing matching “${m[1]}”`;
    }
    if (h.kind === "artist") { if (!media.shuffle) toggleShuffle(); playQueue(h.item.albums.flatMap((al) => al.tracks), -1); return `Shuffling ${h.item.name}`; }
    if (h.kind === "album") { playQueue(h.item.tracks, 0); return `Playing ${h.item.name}`; }
    playIndex(h.item);
    return `Playing ${media.tracks[h.item].title}`;
  }
  if (/^(play|resume)( the)?( music| something| a song)?$/.test(text)) {
    if (audio.src) { audio.play(); return "Playing"; }
    await loadMedia();
    if (!media.tracks.length) return "No music found";
    if (!media.shuffle) toggleShuffle();
    playQueue(media.songs, -1);
    return `Playing ${curTrack().title}`;
  }
  if (/zoom in/.test(text)) { map.zoomIn(); return "Zoomed in"; }
  if (/zoom out/.test(text)) { map.zoomOut(); return "Zoomed out"; }
  if (/(save|lock|keep|mark)( the| that)? ?(clip|video|footage|dashcam|that)?$/.test(text) && /save|lock|keep|mark/.test(text)) {
    await lockClip();
    return "Clip saved";
  }
  if ((m = text.match(/(?:show|open|go to)(?: the)? (cameras?|map|home|media|music|vehicle|car|settings)/))) {
    const v = { camera: "cameras", cameras: "cameras", map: "home", home: "home", media: "media", music: "media", vehicle: "vehicle", car: "vehicle", settings: "settings" }[m[1]];
    showView(v);
    return `Showing ${v}`;
  }
  if (/(volume up|louder|turn it up)/.test(text)) { nudgeVolume(10); return "Volume up"; }
  if (/(volume down|quieter|turn it down)/.test(text)) { nudgeVolume(-10); return "Volume down"; }
  if (/(how fast|my speed|what speed)/.test(text)) {
    const msg = speedNow(lastState) != null ? `${speedVal(speedNow(lastState))} ${settings.units === "imperial" ? "miles per hour" : "kilometers per hour"}` : "No GPS fix";
    say(msg);
    return msg;
  }
  if (/^(read|tell me|what'?s|give me)( me)? (the )?(news|headlines)|^(news|headlines)$/.test(text)) return readHeadlines();
  if (/engine (code|light|trouble)|check engine|trouble code|diagnos/.test(text)) return obdSay();
  if (/oil change/.test(text)) {
    await loadTrip();
    const left = tripInfo?.oil_left_km;
    const msg = left == null ? "Set the odometer in Settings first" : left > 0 ? `Oil change in ${distUnit(left, 0).replace("mi", "miles").replace("km", "kilometers")}` : "The oil change is due";
    say(msg);
    return msg;
  }
  if (/(gas|fuel) (mileage|economy)|my mileage|how many (mpg|miles per gallon)/.test(text)) {
    await loadTrip();
    const a = tripInfo?.trips.a, now = lastState?.obd?.mpg;
    const msg = a?.mpg ? `This trip: ${econText(a.mpg)} over ${distUnit(a.km)}${now ? `; right now ${econText(now)}` : ""}` : "No fuel data yet: it needs the OBD adapter";
    say(msg.replace(/mpg/g, "miles per gallon"));
    return msg;
  }
  if (/^reset (the )?trip( a| b)?$/.test(text)) {
    const which = /trip b$/.test(text) ? "b" : "a";
    tripInfo = await api("/api/trip/reset", { json: { which } });
    return `Trip ${which.toUpperCase()} reset`;
  }
  if ((m = text.match(/^(?:send (?:a )?(?:mesh|lora) (?:message )?|tell the mesh |mesh message )(.+)$/))) { await meshSend(m[1]); return `Sent to the mesh: ${m[1]}`; }
  if (/^(read|any|check)( the| my)? (mesh|lora) (messages?)?/.test(text)) {
    await loadMesh();
    const ms = (mesh.state?.messages || []).filter((x) => !x.mine).slice(-3);
    const msg = ms.length ? ms.map((x) => `${x.from_name} said: ${x.text}`).join(". ") : "No mesh messages";
    say(msg);
    return msg;
  }
  if (/what time/.test(text)) { const msg = `It's ${fmtClock(new Date(), true)}`; say(msg); return msg; }
  if (/where (are|am) (we|i)|what town|where is this/.test(text)) {
    const msg = whereNow ? `We're ${whereText()}` : lastPos ? "Somewhere without a town nearby" : "No GPS fix yet";
    say(msg);
    return msg;
  }
  if (/weather|forecast|going to rain/.test(text)) {
    const w = (radio.weather = await api("/api/weather").catch(() => null));
    const p = w?.periods?.[0];
    if (!p) return "No forecast yet (it needs internet once)";
    const alert = w.alerts?.[0] ? ` Alert: ${w.alerts[0].event}.` : "";
    const msg = `${p.name}: ${p.shortForecast.toLowerCase()}, ${p.temperature} degrees${p.rain ? `, ${p.rain} percent chance of rain` : ""}.${alert}`;
    say(msg);
    return msg;
  }
  if (/(when will (we|i) (get|arrive)|what's (the|my) eta|^eta$|how (much )?long (until|till|to go))/.test(text)) {
    const msg = nav.route ? `Arriving at ${$("#nav-eta").textContent}, ${$("#nav-left-time").textContent} to go` : "Not navigating";
    say(msg);
    return msg;
  }
  if (fromModel) return `Didn't understand “${raw.trim()}”`;
  return askModel(raw);
}

// Anything the grammar above misses goes to the local LLM (intent.py), whose answer is turned back into a
// phrase the grammar knows.
const MODEL_PHRASES = {
  navigate: (q) => q && `navigate to ${q}`, cancel_navigation: () => "cancel navigation",
  play_music: (q) => (q ? `play ${q}` : "play music"), pause: () => "pause", next_track: () => "next",
  previous_track: () => "previous", volume_up: () => "volume up", volume_down: () => "volume down",
  zoom_in: () => "zoom in", zoom_out: () => "zoom out", show_screen: (q) => q && `show ${q}`,
  save_clip: () => "save clip", ask_speed: () => "how fast", ask_time: () => "what time", ask_eta: () => "eta",
};
async function askModel(raw) {
  let r;
  try {
    r = await api("/api/intent", { json: { text: raw } });
  } catch {
    return `Didn't understand “${raw.trim()}”`;
  }
  const phrase = MODEL_PHRASES[r.action]?.(r.query.toLowerCase().replace(/[.,!?]/g, "").trim());
  return phrase ? runCommand(phrase, true) : `Didn't understand “${raw.trim()}”`;
}

async function lockClip() {
  try {
    await api("/api/dashcam/lock", { method: "POST" });
    toast("Clip saved — it won't be overwritten");
  } catch (e) {
    toast(`Couldn't save clip: ${e.message}`);
  }
}

// ---- wiring ----
const ACTIONS = {
  search: () => openSearch(),
  "search-close": closeSearch,
  "voice-search": async () => {
    $("#search-text").textContent = "Listening…";
    try {
      const r = await listen();
      query = r?.text ? r.text.replace(/[.!?]$/, "").replace(/^(navigate|take me|directions) to |^play /i, "") : query;
    } catch (e) { toast(e.message); }
    $("#search-text").textContent = query;
    if (query) runSearch();
  },
  "nav-start": startNav,
  "set-home": () => nav.dest && setHome(nav.dest),
  "home-here": () => (lastPos ? setHome({ name: "Home", lat: lastPos.lat, lon: lastPos.lon, address: "" }) : toast("No GPS fix yet")),
  "home-clear": () => setHome(null),
  "voice-mute": () => {
    settings.prompts = settings.prompts === "on" ? "off" : "on";
    save("settings", settings);
    paintSettings();
    paintMute();
    toast(settings.prompts === "on" ? "Spoken directions on" : "Spoken directions muted", 1400);
  },
  "nav-cancel": () => endNav(),
  "zoom-in": () => map.zoomIn(),
  "zoom-out": () => map.zoomOut(),
  recenter: () => { setFollow(true); if (lastPos) followView([lastPos.lat, lastPos.lon]); },
  playpause: playPause,
  next: () => skipTrack(1),
  prev: () => skipTrack(-1),
  shuffle: toggleShuffle,
  repeat: cycleRepeat,
  "media-back": mediaBack,
  "media-save": () => openSaveSheet(levelTracks(media.stack.at(-1))),
  "media-delete": () => deletePlaylist(media.stack.at(-1)?.key),
  "np-add": () => { const t = curTrack(); if (t) openSaveSheet([media.idIndex.get(t.id)]); else toast("Nothing playing"); },
  "pl-close": () => ($("#pl-sheet").hidden = true),
  "news-refresh": () => loadNews(true),
  "trip-reset": async (btn) => { if (!confirm(`Reset trip ${btn.dataset.trip.toUpperCase()}?`)) return; tripInfo = await api("/api/trip/reset", { json: { which: btn.dataset.trip } }); renderTrip(); },
  "odo-set": () => { kbLayer = "num"; openSearch("", "odo"); },
  "oil-done": async () => {
    if (!confirm("Mark the oil as changed now?")) return;
    tripInfo = await api("/api/trip/oil", { json: {} }).catch((e) => (toast(e.message), tripInfo));
    paintTripSettings();
    toast("Oil change recorded");
  },
  "obd-cel": () => ($("#obd-codes").hidden ? showObdCodes(false) : ($("#obd-codes").hidden = true)),
  "obd-reread": () => showObdCodes(true),
  "obd-clear": obdClear,
  "obd-find": obdFind,
  "news-manage": () => { news.manage = !news.manage; $("#news-manage").textContent = news.manage ? "Done" : "Feeds"; renderNews(); },
  "player-toggle": togglePlayer,
  "radio-up": () => radioNudge(1),
  "radio-down": () => radioNudge(-1),
  "radio-toggle": () => (radio.on ? radioStop() : tune({ ...radio.last, freqs: [radio.last.f], label: radio.presets.find((p) => p.f === radio.last.f)?.label || freqText(radio.last.f, radio.last.mode) })),
  "radio-preset": togglePreset,
  "cap-next": () => showCapImage(1),
  "cap-prev": () => showCapImage(-1),
  "cap-close": () => { $("#cap-viewer").hidden = true; $("#cap-audio").pause(); },  // a GOES animation keeps playing on the card
  "radio-enter": () => openSearch("", "freq"),
  "chat-type": () => openSearch("", "chat"),
  "bt-pair": btPair,
  "wifi-list": () => { $("#wifi-list").hidden = !$("#wifi-list").hidden; $("#wifi-btn").textContent = $("#wifi-list").hidden ? "Choose network" : "Done"; paintWifi(true); },
  emergency: () => showView("emergency"),
  "em-refresh": loadEmergency,
  "wx-refresh": () => loadWeather(true),
  "bt-forget": btForget,
  "chat-mic": chatMic,
  "chat-stop": () => chatLog.busy?.abort(),
  "chat-think": () => { chatLog.think = !chatLog.think; save("chatThink", chatLog.think); renderChat(); },
  "chat-speak": () => { chatLog.speak = !chatLog.speak; save("chatSpeak", chatLog.speak); renderChat(); },
  "chat-clear": () => {  // stops an answer in progress too
    chatLog.busy?.abort();
    chatLog.msgs = [];
    save("chatLog", []);
    renderChat();
  },
  "viz-cycle": cycleViz,
  "np-open": openNowPlaying,
  "np-close": () => ($("#np-full").hidden = true),
  "panel-toggle": () => setPanel(!document.body.classList.contains("panel-hidden")),
  "media-play": () => playLevel(false),
  "media-shuffle": () => playLevel(true),
  "media-search": () => openSearch("", "music"),
  "video-close": stopVideo,
  "dashcam-lock": lockClip,
  satellite: () => setSatellite(!satellite),
  radar: () => setRadar(!radarOn),
  layers: () => { $("#layers-menu").hidden = !$("#layers-menu").hidden; paintLayers(); },
  "goes-view": () => openGoes(radio.goesView),
  "goes-anim": toggleGoesAnim,
  "clear-recents": () => { save("recents", []); renderRecents(); toast("Recent places cleared"); },
  reload: () => location.reload(),
};
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action]");
  if (btn && ACTIONS[btn.dataset.action]) ACTIONS[btn.dataset.action](btn);
  const view = e.target.closest(".rail-btn[data-view]");
  // Home again while on Home: hide or show the panel (full-screen map)
  if (view?.dataset.view === "home" && currentView === "home") setPanel(!document.body.classList.contains("panel-hidden"));
  else if (view) showView(view.dataset.view);
  const fold = e.target.closest(".fold-btn");
  if (fold) toggleFold(fold);
});
$("#mic-btn").addEventListener("click", voiceCommand);
$("#layers-menu").addEventListener("click", (e) => {
  const b = e.target.closest("[data-layer]");
  if (b) toggleLayer(b.dataset.layer);
});
document.addEventListener("pointerdown", (e) => {  // tap anywhere else closes the layers menu
  if (!$("#layers-menu").hidden && !e.target.closest("#layers-menu, #layers-btn")) $("#layers-menu").hidden = true;
});
$("#voice-card").addEventListener("click", () => hideVoice(0));

function paintSettings() {
  for (const seg of $$(".seg[data-setting]")) {
    for (const b of $$("button", seg)) b.classList.toggle("on", b.dataset.v === settings[seg.dataset.setting]);
  }
}
paintSettings();
for (const seg of $$(".seg[data-setting]")) {
  const key = seg.dataset.setting;
  seg.addEventListener("click", (e) => {
    const v = e.target.closest("button")?.dataset.v;
    if (!v) return;
    settings[key] = v;
    save("settings", settings);
    paintSettings();
    if (key === "viz") startVisualizer();
    applyTheme();
    tick();
    if (lastState) onState(lastState);
    renderNav();
  });
}

function tick() {
  $("#clock").textContent = fmtClock(new Date());
  applyTheme();
}
tick();
setInterval(tick, 10000);
renderNav();
initMap().then(connect);
loadMedia();
paintRepeat();
