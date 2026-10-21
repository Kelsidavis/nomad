"""Sharing the SDR: one dongle, many things worth hearing.

The order of who gets it:
  1. a satellite capture, then the radio you tune (radio.py): they take it whenever they want it;
  2. the "slices" here: short listens that come around on a timer (ships on 162 MHz, the truck's tire sensors on
     315 MHz, aircraft text messages on 131 MHz, the weather radio's emergency alerts), each with a tool of its own;
  3. ADS-B (adsb.py), the filler that has the dongle the rest of the time.
A slice that is due while a pass is about to start waits for it; one running when the radio is tuned is cut short.

Each slice is a pipeline of programs whose last output is read line by line into the slice's parser. Slices can
be turned off (or are, when their program isn't installed) from the Radio screen; the choice is kept in
DATA/sdr.json.
"""
import json
import os
import pathlib
import shutil
import signal
import subprocess
import threading
import time

import radio

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
SETTINGS = DATA / "sdr.json"
SLICES = []
_run = {"slice": None, "procs": [], "since": 0.0}
_lock = threading.Lock()


class Slice:
    """name: its key; label: what to call it; tools: programs it needs; every: seconds between listens; run: how long
    one listen is; pipeline(): the commands (lists), each piped into the next; parse(line): one line of the last
    one's output; when(ctx): whether now is a good time (moving, planes about, ...); every_when(ctx): a different
    gap for the moment (listen more often offline, say)."""

    def __init__(self, name, label, tools, every, run, pipeline, parse, when=None, every_when=None, detail=""):
        self.name, self.label, self.tools, self.every, self.run = name, label, tools, every, run
        self.pipeline, self.parse, self.when, self.every_when, self.detail = pipeline, parse, when, every_when, detail
        self.last_end = 0.0
        self.last_start = 0.0
        self.lines = 0  # lines the last listen produced
        self.listens = 0
        self.error = None
        SLICES.append(self)

    def missing(self):
        return [t for t in self.tools if not shutil.which(t)]

    def gap(self, ctx):
        return self.every_when(ctx) if self.every_when else self.every


def _settings():
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def enabled(s):
    return _settings().get(s.name, True) and not s.missing()


def set_enabled(name, on):
    cur = _settings()
    cur[name] = bool(on)
    DATA.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(cur))


def busy():
    """A slice has the dongle."""
    return _run["slice"] is not None


def abort():
    """Let go of the dongle now (the radio or a satellite capture wants it)."""
    with _lock:
        procs, _run["procs"] = _run["procs"], []
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    for p in procs:
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def listen(s, seconds=None):
    """Run slice s for its time (or `seconds`), feeding its output to its parser. Returns the lines read."""
    seconds = seconds or s.run
    procs, prev = [], None
    for cmd in s.pipeline():
        p = subprocess.Popen(cmd, stdin=prev.stdout if prev else subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, bufsize=1, start_new_session=True)
        if prev:
            prev.stdout.close()
        procs.append(p)
        prev = p
    with _lock:
        _run.update(slice=s, procs=procs, since=time.time())
    n = [0]

    def reader():
        for line in prev.stdout:
            line = line.strip()
            if line:
                n[0] += 1
                try:
                    s.parse(line)
                except Exception as e:  # noqa: BLE001 - one odd line must not end the listen
                    s.error = f"{type(e).__name__}: {e}"[:160]

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    end = time.time() + seconds
    while time.time() < end and procs and all(p.poll() is None for p in procs[:1]) and _run["procs"]:
        if radio.tuner.tuned or radio.capture.busy():  # the radio or a pass wants the dongle
            break
        time.sleep(0.5)
    abort()
    t.join(timeout=2)
    with _lock:
        _run.update(slice=None, procs=[])
    s.last_end, s.lines, s.listens = time.time(), n[0], s.listens + 1
    return n[0]


def _pass_soon(within, where):
    """A satellite pass worth capturing begins within `within` seconds (or is on): leave the dongle for it."""
    try:
        if radio.radio_settings()["auto"] == "off":
            return False
        loc, now = where(), time.time()
        for p in radio.passes(*loc) if loc else []:
            if radio.will_capture(p) and p["aos"] - 40 < now + within and now < p["los"] - 90:
                return True
    except Exception:  # noqa: BLE001 - a failed prediction must not stop the listening
        pass
    return False


def sdr_loop(where, context):
    """where() -> (lat, lon) or None; context() -> {"moving": bool, "online": bool, "aircraft": int} for the slices'
    when() tests. Every few seconds: if the dongle is free and a slice is due, give it the dongle for its time."""
    radio.SDR_USERS.append(abort)
    while True:
        time.sleep(3)
        if busy() or radio.tuner.tuned or radio.capture.busy() or time.time() < radio.SDR_HOLD[0] or not radio.sdr_present():
            continue
        ctx, now = context(), time.time()
        due = []
        for s in SLICES:
            gap = s.gap(ctx)
            if enabled(s) and now - s.last_end >= gap and (s.when is None or s.when(ctx)):
                due.append(((now - s.last_end) / gap, s))
        if not due:
            continue
        s = max(due, key=lambda d: d[0])[1]
        if _pass_soon(s.run + 15, where):
            continue
        _run["slice"] = s  # claimed first: adsb.py stays off the dongle while this is set
        radio.release_sdr(0)  # stops ADS-B
        s.last_start = time.time()
        try:
            listen(s)
        except OSError as e:
            s.error = f"listen: {e}"[:160]
            s.last_end = time.time()
        finally:
            _run["slice"] = None


def status():
    ctx_now = time.time()
    return {"running": _run["slice"].name if _run["slice"] else None,
            "slices": [{"name": s.name, "label": s.label, "detail": s.detail, "enabled": enabled(s), "missing": s.missing(),
                        "every": s.every, "run": s.run, "last": s.last_end or None, "lines": s.lines, "listens": s.listens,
                        "next_in": max(0, round(s.every - (ctx_now - s.last_end))) if s.last_end else 0, "error": s.error}
                       for s in SLICES]}
