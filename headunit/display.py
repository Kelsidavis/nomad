"""Screen brightness. A display with its own backlight (the Pi's DSI touchscreens) goes through brightnessctl; an
HDMI monitor through DDC/CI (ddcutil, `sudo apt install ddcutil`); anything else is dimmed by the page itself (a black
layer over the UI: status() says "software" and the page does it). BRIGHTNESS in nomad.conf forces one of
backlight, ddc or software; the default picks the first that works.

Two levels are kept, one for the day and one for the night, and the page says which it is (from the sunrise it
already works out for the theme); with Auto off the day level is always used. Kept in DATA/display.json, so the
screen comes back at its level after a reboot, before any page is open.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import threading
import time

import config

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
FILE = DATA / "display.json"
MIN = 5  # never fully dark: a screen you can't see can't be turned back up
_lock = threading.Lock()
_wake = threading.Event()
_state = {"auto": True, "day": 100, "night": 45, "period": "day", "backend": None, "applied": None, "error": None}
_backend_at = 0.0


def _run(*cmd, timeout=10):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _backlight_get():
    if not shutil.which("brightnessctl"):
        return None
    r = _run("brightnessctl", "-m", "-c", "backlight", "info")
    m = re.search(r",(\d+)%,", r.stdout) if r.returncode == 0 else None
    return int(m.group(1)) if m else None


def _backlight_set(pct):
    r = _run("brightnessctl", "-q", "-c", "backlight", "set", f"{pct}%")
    if r.returncode:
        raise OSError(r.stderr.strip() or "brightnessctl failed")


def _ddc_get():
    if not shutil.which("ddcutil"):
        return None
    r = _run("ddcutil", "getvcp", "10", "--brief", timeout=20)
    m = re.search(r"VCP 10 C (\d+) (\d+)", r.stdout) if r.returncode == 0 else None
    return round(int(m.group(1)) * 100 / int(m.group(2))) if m and int(m.group(2)) else None


def _ddc_set(pct):
    r = _run("ddcutil", "setvcp", "10", str(pct), timeout=20)
    if r.returncode:
        raise OSError((r.stderr or r.stdout).strip()[:200] or "ddcutil failed")


BACKENDS = {"backlight": (_backlight_get, _backlight_set), "ddc": (_ddc_get, _ddc_set)}


def _load():
    try:
        saved = json.loads(FILE.read_text())
    except (OSError, ValueError):
        return
    for k in ("auto", "day", "night"):
        if k in saved:
            _state[k] = saved[k]


def _save():
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: _state[k] for k in ("auto", "day", "night")}))
    tmp.replace(FILE)


def _detect():
    """The first backend that answers, remembered; software when none does (looked at again every few minutes, in
    case a monitor was off or just plugged in)."""
    global _backend_at
    forced = (config.get("BRIGHTNESS") or "auto").strip().lower()
    if _state["backend"] and (_state["backend"] != "software" or time.time() - _backend_at < 300):
        return _state["backend"]
    for name in ([forced] if forced in BACKENDS else [] if forced == "software" else list(BACKENDS)):
        try:
            if BACKENDS[name][0]() is not None:
                _state["backend"] = name
                return name
        except (OSError, subprocess.SubprocessError):
            continue
    _state["backend"] = "software"
    _backend_at = time.time()
    return "software"


def level():
    """The level to show now: the night one when Auto is on and it's night, else the day one."""
    return _state["night"] if _state["auto"] and _state["period"] == "night" else _state["day"]


def status():
    with _lock:
        return {"backend": _detect(), "auto": _state["auto"], "day": _state["day"], "night": _state["night"],
                "period": _state["period"], "level": level(), "min": MIN, "error": _state["error"]}


def update(auto=None, day=None, night=None, period=None):
    """Change what was given; the screen follows in the background (a DDC write takes a second or two)."""
    with _lock:
        if auto is not None:
            _state["auto"] = bool(auto)
        for k, v in (("day", day), ("night", night)):
            if v is not None:
                _state[k] = max(MIN, min(100, int(round(float(v)))))
        if period in ("day", "night"):
            _state["period"] = period
        _save()
    _wake.set()
    return status()


def _worker():
    """Apply the latest level to the hardware; changes made while one write is running are folded into the next."""
    while True:
        _wake.wait()
        _wake.clear()
        with _lock:
            backend, want = _detect(), level()
        if backend == "software" or want == _state["applied"]:
            continue
        try:
            BACKENDS[backend][1](want)
            _state.update(applied=want, error=None)
        except (OSError, subprocess.SubprocessError) as e:
            _state["error"] = str(e)[:200]
            time.sleep(5)  # the monitor may be waking up or off: try again, but not in a tight loop
            _wake.set()


def start():
    """Read what was saved and keep the screen at it (call once, at startup)."""
    _load()
    threading.Thread(target=_worker, daemon=True).start()
    _wake.set()
