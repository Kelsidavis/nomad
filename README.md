# NOMAD

**Navigation, Observation, Media & Autonomous Data**: an offline-first head unit for a car or truck, on a
Raspberry Pi 5 with a touchscreen. Maps, routing, search, voice assistant, music, radio and weather all run on the
Pi itself and keep working with no signal; it goes online only for what it doesn't have.

| | |
|---|---|
| **Navigation** | Offline vector maps (Protomaps), aerial imagery (USGS), place search (OpenStreetMap, 18 M places), routing (Valhalla), turn-by-turn voice with prompts timed by speed, the speed limit of the road you're on (OpenStreetMap, offline) with an over-the-limit warning, gas, food, coffee and rest areas ahead on the route with one-tap stops ("Truck, gas on the way"), reroutes, home button, heading-up 3D view |
| **Map layers** | Satellite, NEXRAD weather radar, shaded relief, elevation colors, contours (ft/m), 3D terrain |
| **Voice** | Push the mic: whisper.cpp speech-to-text, a fixed command grammar plus Gemma 4 E2B for anything else ("Truck, I need gas"), conversation without the keyword, timers and reminders ("Truck, remind me to call Mom in 20 minutes"), all spoken back in Kokoro's Bella or Sky voice |
| **Assistant** | A chat tab with Gemma 4 E2B, its reasoning streamed live as it thinks; it knows the road, the weather, the engine and what's playing |
| **Everyday** | Voice timers and reminders, a guide to every voice command ("Truck, help"), Settings → System with a one-tap health check |
| **Media** | A 25,000-track library (artists, albums, songs, search, album art), browse by mood, style, genre and decade (Plex's tags), mixes from a described vibe ("chill night drive", "something like Blink-182"; the voice model picks the tags), playlists, spectrum visualizers, full-screen now playing, video; `tools/plex_music.py` copies a Plex library and converts lossless to Opus |
| **Radio (SDR)** | FM/AM with band scan, NOAA weather radio, an EMS/fire/marine/air/ham scanner, satellite pass predictions with Doppler-corrected tuning, and automatic capture: Meteor-M2 weather images (SatDump) and ISS SSTV pictures |
| **LoRa mesh** | A Meshtastic radio (USB or Wi-Fi) for off-grid texts with no cell signal: messages read aloud, quick replies, send your location, direct messages, nodes heard with distance and signal, nodes on the map, opt-in position sharing; "Truck, tell the mesh I'm on my way" |
| **Aircraft (ADS-B)** | Planes overhead from their 1090 MHz broadcasts with the same SDR (rtl_adsb, decoded here): on the map with heading, callsign, altitude and trail, a list by distance, emergency squawks in red; "Truck, what plane is that?" |
| **Weather** | NWS forecast and alerts, an hourly temperature and rain chart, GOES satellite pictures and animations, radar on the map, alerts and arrival weather along your route; cached for when you're offline; "Truck, will it rain?" |
| **News** | RSS and Atom headlines (NPR, BBC, Guardian, Ars Technica to start; add any feed), downloaded in the background and kept for offline; "Truck, read the news" |
| **Emergency** | Tsunami evacuation zones, routes and assembly areas (WA, OR; offline) with "am I in a zone?", one-tap routes to high ground, open FEMA shelters, Red Cross, community centers, hospitals and fire stations nearby, USGS earthquakes on the map with spoken alerts; "Truck, evacuate" |
| **Fuel** | Weekly average prices for your state, region and the US (EIA, free key), a fill-up log with real mpg, cost per mile and monthly spending, and what you paid at each station shown when you search for gas; "Truck, I put in 18.5 gallons at 3.89", "how much is gas?" |
| **Vehicle** | OBD-II through any ELM327 adapter (USB, Bluetooth or Wi-Fi): rpm, temperatures, voltage, fuel level, live and trip mpg, check-engine codes with descriptions (read, clear), spoken warnings (overheating, charging, low fuel, new codes), a trip computer (two trips, odometer, oil-change reminder), drive history (each drive's route, distance and towns, on the map or as GPX; kept on the Pi, can be turned off), "Truck, any engine codes?"; GPS (gpsd), satellite sky plot, g-meter, compass, Pi power and temperature |
| **Boot** | NOMAD splash screen, quiet boot, and a session that is only the head unit: no desktop, panel or file manager |
| **Look** | Android Auto-style layout from 800x480 up to 1080p, collapsible panels, dark/light by sunrise, accent color from the album art |

Everything is plain and hackable: a Python standard-library server (`headunit/server.py`) and one web page
(`headunit/static/`) shown full-screen by Chromium, plus a few small services for speech and the LLM.

## Hardware

- **Raspberry Pi 5, 8 GB** (the LLM and maps need the RAM), a 256 GB+ microSD card (the offline data is ~35 GB).
- **Touchscreen.** Designed for a 1024x600 7" LCD; anything from 800x480 to 1080p works.
- **Power: a solid 5 V / 5 A supply.** This matters. A marginal car supply sags under the LLM's load and the Pi
  under-volts and freezes; the services are limited to 2-3 CPU cores to stay inside a weak supply's limits.
- **Microphone:** a WM8960 2-mic HAT (ReSpeaker / Keyestudio); its speaker output is the audio out.
- **GPS:** a USB GPS puck behind a CP210x serial bridge (an active external antenna is optional).
- Optional: an RTL-SDR (tested design: Nooelec NESDR SMArt v5) for the radio tab; a second one with a
  SAWbird+ NOAA LNA and a 137 MHz antenna for satellite images; an Android phone running DashLink
  (`dashlink-android/`) for its cameras, sensors and storage.

## Set up

On a fresh Raspberry Pi OS (trixie, 64-bit, desktop) card:

```sh
git clone https://github.com/Kelsidavis/nomad.git && cd nomad
cp nomad.conf.example nomad.conf          # your settings: name, home, GPS, Plex... (gitignored)
sudo sh headunit/setup-pi.sh              # packages, mic HAT, GPS + GPS time, SDR, services, kiosk
```

`setup-pi.sh` is safe to run again. It installs the head unit to `/opt/headunit` and starts it in a Chromium kiosk
with the desktop session (exit with Alt+F4). Then add the models and the offline data below.

### Models

| What | Where | How |
|---|---|---|
| LLM: Gemma 4 E2B (QAT Q4_0) + its MTP draft | `~/models/` | build [llama.cpp](https://github.com/ggml-org/llama.cpp) in `~/llama.cpp` (`cmake -B build -DGGML_NATIVE=ON && cmake --build build -j2`); download `gemma-4-E2B_q4_0-it.gguf` (google/gemma-4-E2B-it-qat-q4_0-gguf) and `mtp-gemma-4-E2B-it-Q4_0.gguf` (ggml-org/gemma-4-E2B-it-GGUF) |
| Speech to text: whisper.cpp base.en | `~/models/ggml-base.en-q5_1.bin` | build [whisper.cpp](https://github.com/ggml-org/whisper.cpp) v1.9.5 in `~/whisper.cpp` (`whisper-server` target); model from huggingface.co/ggerganov/whisper.cpp |
| Voice: Kokoro | `~/models/kokoro/` | installed by `setup-pi.sh` (kokoro-onnx, `kokoro-v1.0.int8.onnx`, `voices-v1.0.bin`) |

`tools/bench_intents.py` scores LLMs on voice commands, if you want to try others (on the Pi 5: Gemma 4 E2B and
Qwen3.5-2B 29/30; Gemma with MTP answers a command in ~2.4 s on two cores).

### Offline data

All of it goes in `~/.local/share/headunit/` on the Pi. Build it on a bigger machine and copy it over, or fetch
straight on the Pi. The examples cover the Western US; use any bounding box.

| Data | File | Built with | Size (W. US) |
|---|---|---|---|
| Street map (vector, z0-15) | `maps/<name>.pmtiles` | `pmtiles extract https://build.protomaps.com/<date>.pmtiles west.pmtiles --bbox=-125,31,-102,49.1` | 4.9 GB |
| Aerial imagery, region (z0-12) | `imagery/region.mbtiles` | `tools/fetch_imagery.py -- region.mbtiles -125,31,-102,49.1 0 12` | 1.6 GB |
| Aerial imagery, sharper (z13-15 or 16) | `imagery/<name>.mbtiles` | `tools/fetch_imagery.py -- home.mbtiles <w,s,e,n> 13 16` (any number of files; the sharpest draws on top) | 1-20 GB |
| Elevation (z0-11) | `terrain/terrain.mbtiles` | `tools/fetch_imagery.py --source terrain -- terrain.mbtiles -125,31,-102,49.1 0 11` | 2.4 GB |
| Place search | `search.sqlite` | the `osmium tags-filter` command in `tools/build_search.py`, then `tools/build_search.py` | 2.4 GB |
| Routing | `valhalla/valhalla_tiles.tar` + `valhalla.json` + `valhalla_tiles/*.sqlite` | `tools/build-valhalla.sh` in `ghcr.io/valhalla/valhalla:3.9.1` with a Geofabrik `.osm.pbf` (Docker needs ~16 GB RAM) | 3 GB |
| Tsunami evacuation (WA, OR) | `emergency/*.geojson` | `tools/fetch_emergency.py` (WA DNR and Oregon DOGAMI) | 7 MB |
| Music | `~/Music/` | any `Artist/Album/track` folders; `tools/plex_music.py` copies a Plex library with clean names (`library.json`) and covers | — |

Missing pieces fall back to online services (OSM tiles, Photon/Nominatim, OSRM, AWS terrain) when there's a
connection. `HEADUNIT_OFFLINE=1` turns the fallbacks off, to check the offline data.

## Configure

`nomad.conf` (copy `nomad.conf.example`) holds what's yours; it's gitignored, so the repo stays shareable.

| Key | What |
|---|---|
| `PI_HOST` | `user@host` of the Pi, for `deploy.sh` |
| `NOMAD_NAME` | Bluetooth name and the voice keyword ("Truck, navigate home"); default `Truck` |
| `HEADUNIT_HOME` | `lat,lon` for weather and satellite passes before there's a GPS fix or a home set in Settings |
| `GPS_SERIAL` | the GPS bridge's serial for its udev rule; empty = whichever CP210x is plugged in at setup |
| `PLEX_*`, `MUSIC_OUT` | defaults for `tools/plex_music.py` |
| `OBD_DEVICE` | the OBD-II adapter: `auto` (USB ports, then paired Bluetooth adapters), `/dev/ttyUSB1`, `bt:<mac>`, `tcp:192.168.0.10:35000`, or `off`; `tools/elm327_sim.py` pretends to be one |
| `MESH_DEVICE` | the Meshtastic radio: `auto` (a USB board, never the GPS or OBD port), `/dev/ttyACM0`, `tcp:<host>`, or `off` |
| `EIA_API_KEY` | free key from eia.gov/opendata for the weekly fuel price averages |
| `NEWS_FEEDS` | the starting feed list, `Name|url, Name|url` (after that, edit it on the News screen) |

Everything else is in the Settings screen: units, theme, accent color, voice, visualizer, home, Wi-Fi networks,
data use (Auto saves data on a metered connection like a phone hotspot; Offline never goes online), phone
notification privacy, and Bluetooth pairing. `tools/selftest.py` on the Pi checks every part (deploy.sh runs it).

## Develop

Edit in the repo, then `./deploy.sh` sends it to the Pi in `nomad.conf` and restarts the head unit
(`--all` also updates the service units and `/etc/nomad.conf`). Reload the screen to see UI changes.

| Service | Port | What |
|---|---|---|
| `headunit` | 8800 | `server.py`: the UI, map tiles, search, routing, media, radio, weather, voice and assistant APIs |
| `llm` | 8090 | llama-server with Gemma 4 E2B (2 threads, MTP) |
| `stt` | 8178 | whisper.cpp server (base.en) |
| `tts` | 5002 | `tts_server.py`: Kokoro, with a sentence cache and prefetching of a route's prompts |
| `valhalla` | 8002 | routing, in Docker |

- `HEADUNIT_SIM=1` enables `POST /api/sim {"lat","lon","speed_mps","track_deg"}`, a fake GPS fix for testing
  navigation indoors.
- To use the UI from a laptop: `ssh -L 8800:127.0.0.1:8800 <pi>` and open http://127.0.0.1:8800; set
  Settings → Speak through → This screen to hear it there.
- The phone app, DashLink, is optional and documented in [dashlink-android/README.md](dashlink-android/README.md).

## License

GPL-3.0-or-later: see [LICENSE](LICENSE). Bundled libraries, fonts and the data and models NOMAD uses keep their
own licenses: see [THIRD_PARTY.md](THIRD_PARTY.md).
