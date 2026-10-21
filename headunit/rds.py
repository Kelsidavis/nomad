"""RDS: the station name, the scrolling text and the program type that analog FM stations send on a 57 kHz subcarrier,
decoded by redsea (github.com/windytan/redsea; setup-pi.sh builds it). radio.py's tuner runs the FM demodulator at
171 kHz (the whole multiplex, not just the audio) and feeds it both to ffmpeg, which makes the sound, and to redsea, which
prints one JSON line per RDS group. This keeps the newest value of each field: the 8-character station name, the 64-character
radio text (artist and title, or an advert), the program type (Pop music, Talk, News...), the call sign where the station
id gives one, and whether a traffic announcement is on. A station sends its name within a second or two and its text
every few seconds; one without RDS just never fills in.
"""
import json
import threading
import time

_lock = threading.Lock()
_state = {}


def reset(freq=None):
    with _lock:
        _state.clear()
        _state.update(active=freq is not None, freq=freq, started=time.time(), groups=0, ps=None, radiotext=None, prog_type=None, pi=None,
                      callsign=None, tp=None, ta=None, stereo=None, is_music=None, country=None)


def stop():
    with _lock:
        _state["active"] = False


def ingest_line(line):
    """One JSON line from redsea."""
    if not line.startswith("{") or not _state:
        return
    d = json.loads(line)
    with _lock:
        _state["groups"] += 1
        _state["updated"] = time.time()
        for k in ("pi", "prog_type", "callsign", "tp", "ta", "is_music", "country"):
            if d.get(k) is not None:
                _state[k] = d[k]
        if d.get("ps"):
            _state["ps"] = d["ps"].strip() or _state["ps"]
        if d.get("radiotext"):
            _state["radiotext"] = d["radiotext"].strip() or _state["radiotext"]
        if isinstance(d.get("di"), dict) and "stereo" in d["di"]:
            _state["stereo"] = d["di"]["stereo"]


def status():
    with _lock:
        return dict(_state)
