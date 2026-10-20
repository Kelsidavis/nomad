#!/usr/bin/env python3
"""Client for the DashLink app on the phone. Python stdlib only.

Library:
    from dashlink import DashLink
    phone = DashLink()                      # localhost:8780 via `adb forward`
    phone.say("Turn left in 200 feet")      # renders on the phone, plays on the Pi
    print(phone.listen()["text"])           # phone mics + Android recognizer
    for frame in phone.stream(hz=10): ...   # sensors + GPS

CLI:
    dashlink.py status | sensors | location | all
    dashlink.py stream [hz]
    dashlink.py say "text" [--on-phone]
    dashlink.py listen [max_ms]
    dashlink.py transcribe file.wav [model]
    dashlink.py cameras | snapshot out.jpg [cam]
    dashlink.py dashcam [status|stop|lock]
    dashlink.py dashcam run [cams, e.g. 2,1]   # heartbeat loop: record while this runs
    dashlink.py detect [start [cam] [fps]|stop|once [cam]|image file.jpg|watch]
"""
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request


class DashLink:
    def __init__(self, host=None, port=8780, token=None, timeout=10):
        self.base = f"http://{host or os.environ.get('DASHLINK_HOST', '127.0.0.1')}:{port}"
        self.token = token or os.environ.get("DASHLINK_TOKEN")
        self.timeout = timeout

    def _request(self, path, data=None, params=None, timeout=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        return urllib.request.urlopen(req, timeout=timeout or self.timeout)

    def _json(self, path, **kw):
        with self._request(path, **kw) as r:
            return json.load(r)

    def info(self):
        return self._json("/")

    def status(self):
        return self._json("/status")

    def sensors(self):
        return self._json("/sensors")

    def location(self):
        return self._json("/location")

    def all(self):
        return self._json("/all")

    def stream(self, hz=10):
        """Yield combined sensor + location frames until the connection drops."""
        with self._request("/stream", params={"hz": hz}, timeout=30) as r:
            for line in r:
                if line.startswith(b"data: "):
                    yield json.loads(line[6:])

    def tts_wav(self, text, rate=1.0, pitch=1.0):
        body = json.dumps({"text": text, "rate": rate, "pitch": pitch}).encode()
        with self._request("/tts", data=body, timeout=60) as r:
            return r.read()

    def say(self, text, rate=1.0, pitch=1.0, on_phone=False, player=("aplay", "-q")):
        """Speak text. By default the WAV is played on the Pi so it goes through the car audio."""
        if on_phone:
            body = json.dumps({"text": text, "rate": rate, "pitch": pitch, "play": True}).encode()
            with self._request("/tts", data=body, timeout=60) as r:
                return json.load(r)["ok"]
        wav = self.tts_wav(text, rate, pitch)
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            f.write(wav)
            f.flush()
            subprocess.run([*player, f.name], check=True)
        return True

    def transcribe(self, wav, model=None, lang="en", prompt=None):
        """Run whisper on the phone over audio recorded on the Pi. Returns {"ok", "text", ...}."""
        params = {k: v for k, v in {"model": model, "lang": lang, "prompt": prompt}.items() if v}
        req = urllib.request.Request(f"{self.base}/transcribe?{urllib.parse.urlencode(params)}",
                                     data=wav, method="POST", headers={"Content-Type": "audio/wav"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            return json.load(e)

    def cameras(self):
        return self._json("/camera/list")

    def snapshot(self, cam=None):
        """One JPEG (bytes) from a phone camera; None = the open camera or the main rear one."""
        with self._request("/camera/snapshot", params={"cam": cam} if cam else None, timeout=20) as r:
            return r.read()

    def mjpeg_url(self, cam=None, fps=10):
        """URL for a live MJPEG feed (works in mpv, VLC, ffmpeg, or an <img> tag)."""
        q = {k: v for k, v in {"cam": cam, "fps": fps, "token": self.token}.items() if v}
        return f"{self.base}/camera/mjpeg?{urllib.parse.urlencode(q)}"

    def dashcam(self):
        return self._json("/dashcam")

    def dashcam_heartbeat(self, cams=None, keepalive_s=30):
        """Tell the phone the car is on. It records while these keep coming."""
        params = {"keepalive_s": keepalive_s}
        if cams:
            params["cams"] = ",".join(cams)
        return self._json("/dashcam/start", data=b"", params=params, timeout=30)

    def dashcam_stop(self):
        return self._json("/dashcam/stop", data=b"")

    def dashcam_lock(self, reason="manual"):
        """Protect the current and previous segments from loop deletion."""
        return self._json("/dashcam/lock", data=b"", params={"reason": reason})

    def detect_start(self, cam=None, fps=5, model="yolo11n", size=640, thresh=0.35):
        """Run object detection continuously on a phone camera (CPU, ~15 fps max for yolo11n)."""
        params = {k: v for k, v in {"cam": cam, "fps": fps, "model": model, "size": size, "thresh": thresh}.items() if v}
        return self._json("/detect/start", data=b"", params=params, timeout=30)

    def detect_stop(self):
        return self._json("/detect/stop", data=b"")

    def detections(self):
        """Status plus the newest result: {"latest": {"objects": [{"label", "prob", "box": [x, y, w, h]}]}}."""
        return self._json("/detect")

    def detect_stream(self):
        """Yield each new detection result as it happens."""
        with self._request("/detect/stream", timeout=60) as r:
            for line in r:
                if line.startswith(b"data: "):
                    d = json.loads(line[6:])
                    if not d.get("idle"):
                        yield d

    def detect_once(self, cam=None, **kw):
        params = {"cam": cam, **kw} if cam else kw
        return self._json("/detect/once", data=b"", params=params or None, timeout=30)

    def detect_image(self, jpeg, **kw):
        """Detect objects in an image taken elsewhere (e.g. a Pi camera)."""
        req = urllib.request.Request(f"{self.base}/detect/image?{urllib.parse.urlencode(kw)}", data=jpeg,
                                     method="POST", headers={"Content-Type": "image/jpeg"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def listen(self, max_ms=8000, lang="en-US", offline=True):
        """Listen on the phone's mics. Returns {"ok", "text", "alternatives", ...}."""
        params = {"max_ms": max_ms, "lang": lang, "offline": int(offline)}
        return self._json("/stt", data=b"", params=params, timeout=max_ms / 1000 + 20)


def main(argv):
    if not argv:
        print(__doc__)
        return 1
    phone, cmd, args = DashLink(), argv[0], argv[1:]
    if cmd in ("status", "sensors", "location", "all", "info"):
        print(json.dumps(getattr(phone, cmd)(), indent=2))
    elif cmd == "stream":
        for frame in phone.stream(hz=float(args[0]) if args else 10):
            print(json.dumps(frame), flush=True)
    elif cmd == "say":
        on_phone = "--on-phone" in args
        phone.say(" ".join(a for a in args if a != "--on-phone"), on_phone=on_phone)
    elif cmd == "listen":
        print(json.dumps(phone.listen(max_ms=int(args[0]) if args else 8000), indent=2))
    elif cmd == "transcribe":
        with open(args[0], "rb") as f:
            print(json.dumps(phone.transcribe(f.read(), model=args[1] if len(args) > 1 else None), indent=2))
    elif cmd == "cameras":
        print(json.dumps(phone.cameras(), indent=2))
    elif cmd == "snapshot":
        with open(args[0], "wb") as f:
            f.write(phone.snapshot(args[1] if len(args) > 1 else None))
    elif cmd == "detect":
        sub = args[0] if args else "status"
        if sub == "start":
            print(json.dumps(phone.detect_start(args[1] if len(args) > 1 else None,
                                                int(args[2]) if len(args) > 2 else 5), indent=2))
        elif sub == "stop":
            print(json.dumps(phone.detect_stop(), indent=2))
        elif sub == "once":
            print(json.dumps(phone.detect_once(args[1] if len(args) > 1 else None), indent=2))
        elif sub == "image":
            with open(args[1], "rb") as f:
                print(json.dumps(phone.detect_image(f.read()), indent=2))
        elif sub == "watch":
            for d in phone.detect_stream():
                labels = ", ".join(f"{o['label']} {o['prob']:.0%}" for o in d["objects"]) or "-"
                print(f"{d['infer_ms']:4d} ms  {labels}", flush=True)
        else:
            print(json.dumps(phone.detections(), indent=2))
    elif cmd == "dashcam":
        sub = args[0] if args else "status"
        if sub == "run":
            run_dashcam(phone, args[1].split(",") if len(args) > 1 else None)
        else:
            print(json.dumps({"status": phone.dashcam, "stop": phone.dashcam_stop,
                              "lock": phone.dashcam_lock}[sub](), indent=2))
    else:
        print(__doc__)
        return 1
    return 0


def run_dashcam(phone, cams, every_s=10, keepalive_s=30):
    """Heartbeat loop. Run it while the car is on (the Pi is on ignition power); when it stops,
    or the Pi loses power, the phone stops recording within keepalive_s."""
    import signal
    import time

    def stop(*_):
        try:
            phone.dashcam_stop()
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_state = None
    while True:
        try:
            st = phone.dashcam_heartbeat(cams, keepalive_s)
            state = [c["camera"] for c in st["channels"]] or st.get("last_stop")
        except OSError as e:  # phone unplugged or rebooting; keep trying
            state = f"phone unreachable: {e}"
        if state != last_state:
            print(f"dashcam: {state}", flush=True)
            last_state = state
        time.sleep(every_s)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
