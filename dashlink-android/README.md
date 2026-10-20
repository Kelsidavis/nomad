# DashLink (optional)

Turns an old Android phone (tested: LG G7 ThinQ, Android 10) into a sensor, camera and storage co-processor for
NOMAD. The phone runs the DashLink app; the Pi talks to it over USB (adb), or over Wi-Fi / Bluetooth tethering with
a token. NOMAD works without it: speech, voice and the LLM run on the Pi.

## Build

    ./fetch-deps.sh     # whisper.cpp v1.9.5 and ncnn into app/src/main/cpp (not in the repo)
    JAVA_HOME=/path/to/jdk-17 ./gradlew assembleRelease

The object detector model isn't in the repo: YOLO11 is AGPL-3.0 (Ultralytics). Export it yourself
(`pip install ultralytics`, `yolo export model=yolo11n.pt format=ncnn`) and copy `yolo11n.ncnn.param` and
`yolo11n.ncnn.bin` into `app/src/main/assets/`.

## Phone
1. Install: `adb install app/build/outputs/apk/release/app-release.apk` (or copy the APK over and open it).
2. Open DashLink once and allow microphone + location. Tap "Allow running in background".
3. The service starts on boot and after app updates. Leave USB debugging on, with the Pi authorized ("Always allow").

## Pi

`setup-pi.sh` installs the Pi side but doesn't enable it. To use the phone:

    sudo systemctl enable --now dashlink-adb dashcam phone-storage
    python3 /opt/dashlink/dashlink.py all

| Service | Does |
|---|---|
| `dashlink-adb` | keeps `localhost:8780` forwarded to the phone over USB |
| `dashcam` | heartbeat: the phone records while this runs, i.e. while the Pi (the car) is on |
| `phone-storage` | mounts the phone's storage at `/mnt/phone` |

Over USB (adb forward) no token is needed. Over Wi-Fi or tethering, pass the token shown in the app:
`DASHLINK_HOST=<phone ip> DASHLINK_TOKEN=<token> python3 pi/dashlink.py status`.

## API (port 8780)
| Endpoint | What |
|---|---|
| `GET /status` | battery %, temp, charging, hottest CPU/skin sensor, free RAM, IPs |
| `GET /sensors` | accel, linear_accel, gravity, gyro, mag, rotation, pressure, light, orientation (heading/pitch/roll), baro altitude |
| `GET /location` | GPS fix: lat, lon, speed, bearing, altitude, accuracy, satellites |
| `GET /all` | sensors + location in one frame |
| `GET /stream?hz=10` | the same as server-sent events |
| `POST /tts` `{"text", "rate", "pitch", "play"}` | returns a WAV (play it on the Pi) or, with `play:true`, speaks on the phone |
| `POST /stt?max_ms=8000&lang=en-US&offline=1` | listens on the phone mics; returns `{ok, text, alternatives}` |
| `POST /transcribe?model=&lang=en&prompt=` (body: WAV) | whisper.cpp on audio recorded elsewhere; returns `{ok, text, audio_ms, elapsed_ms}` |
| `GET /models` | installed whisper models and the default |
| `GET /camera/list` | cameras (G7: 0 main rear, 1 front, 2 wide rear), which are open/recording |
| `GET /camera/snapshot?cam=2` | one 1280x720 JPEG |
| `GET /camera/mjpeg?cam=2&fps=10` | live MJPEG (`mpv`, VLC, `ffmpeg -i`, or an `<img>` tag) |
| `POST /dashcam/start?cams=2,1&keepalive_s=30` | heartbeat; records the listed cameras until heartbeats stop |
| `POST /dashcam/stop`, `POST /dashcam/lock`, `GET /dashcam` | stop, protect current+previous segments, status |
| WebDAV `/files/` | the app's storage: `models/`, `dashcam/`, `share/` (general use) |

Whisper models for on-phone transcription go in the app folder (from huggingface.co/ggerganov/whisper.cpp):

    adb push ggml-base.en.bin /sdcard/Android/data/dev.dashlink/files/models/

Measured on the G7 (SDM845, 4 threads), voice commands of 1-4 s:

| Model | Time per command | Accuracy on 9 test commands |
|---|---|---|
| tiny.en | 0.3-0.7 s | 8/9 ("Tune to" → "2 to") |
| base.en (default) | 0.4-0.9 s | 9/9 |
| small.en-q8_0 | 1.5-3.5 s | 9/9, with punctuation |

Notes:
- Orientation is the phone's own; apply the mounting offset on the Pi.
- `baro_altitude_m` uses standard sea-level pressure, so treat it as relative (good for climb/descent).
- Use `/status` to protect the battery: have the Pi cut USB power when `battery.temp_c` gets high.

## Cameras and dashcam
- Two cameras can run at once on the G7 (a third fails to configure), e.g. road + cabin.
  Opening more than two is refused with 409 rather than knocking a recording camera off.
- Segments: one minute each, 1080p H.264, 8 Mbps rear / 4 Mbps front, named
  `YYYY-MM-DD_HH-MM-SS_cam<N>.mp4` with a `.csv` of GPS, heading and acceleration per second.
- Loop recording keeps the folder under 30 GB and at least 3 GB free, deleting the oldest first.
  Locked segments (`-LOCKED`) are never deleted. A lock happens on `POST /dashcam/lock` or
  automatically on an impact over 3 g.
- Recording stops 30 s after the last heartbeat, so it ends on its own when the Pi powers down.

## Storage
`/mnt/phone` on the Pi is the phone's app storage over WebDAV. Measured 31-34 MB/s both ways
(the USB link is 480 Mb/s). Put music, map tiles, logs etc. in `/mnt/phone/share/`.
