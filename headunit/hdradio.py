"""HD Radio (NRSC-5): digital audio and station information from FM (and weak AM) broadcasts, decoded by nrsc5
(github.com/theori-io/nrsc5; setup-pi.sh builds it). radio.py's tuner runs `nrsc5 | ffmpeg` in place of rtl_fm when
a station is tuned in HD, and sends nrsc5's log here: the station's name, the song title, artist and album, genre,
slogan and scrolling message, the programs on offer (HD1, HD2, HD3...), the bit rate and the signal quality (MER and
BER). Audio takes about ten seconds to start, since the decoder has to find and lock onto the digital signal first;
a station with no HD signal never locks, and the page then falls back to analog.
"""
import re
import threading
import time

_lock = threading.Lock()
_state = {}


def reset(freq=None, program=0, am=False):
    with _lock:
        _state.clear()
        _state.update(active=freq is not None, freq=freq, program=program, am=am, started=time.time(), synced=False, locked_at=None,
                      station=None, country=None, facility=None, title=None, artist=None, album=None, genre=None, slogan=None,
                      message=None, bitrate=None, mer=None, ber=None, services={}, lines=0)


def stop():
    with _lock:
        _state["active"] = False


def _clean(s):
    s = (s or "").strip()
    return s or None


def ingest_line(line):
    """One line of nrsc5's log ("01:07:34 Title: Some Song")."""
    m = re.match(r"^\d\d:\d\d:\d\d (.*)$", line.strip())
    if not m or not _state:
        return
    text = m.group(1)
    with _lock:
        _state["lines"] = _state.get("lines", 0) + 1
        if text == "Synchronized":
            _state["synced"] = True
            _state["locked_at"] = _state["locked_at"] or time.time()
        elif text == "Lost synchronization":
            _state["synced"] = False
        elif text.startswith("Station name: "):
            _state["station"] = _clean(text[14:])
        elif text.startswith("Country: "):
            c = re.match(r"Country: (\w+), FCC facility ID: (\d+)", text)
            if c:
                _state["country"], _state["facility"] = c.group(1), int(c.group(2))
        elif text.startswith(("Title: ", "Artist: ", "Album: ", "Genre: ", "Slogan: ", "Message: ")):
            key, _, val = text.partition(": ")
            _state[key.lower()] = _clean(val)
        elif text.startswith("Audio bit rate: "):
            b = re.match(r"Audio bit rate: ([\d.]+) kbps", text)
            if b:
                _state["bitrate"] = round(float(b.group(1)))
        elif text.startswith("MER: "):
            mer = re.match(r"MER: (-?[\d.]+) dB \(lower\), (-?[\d.]+) dB \(upper\)", text)
            if mer:
                _state["mer"] = round((float(mer.group(1)) + float(mer.group(2))) / 2, 1)
        elif text.startswith("BER: "):
            ber = re.match(r"BER: ([\d.]+)", text)
            if ber:
                _state["ber"] = float(ber.group(1))
        elif text.startswith("Audio service "):
            s = re.match(r"Audio service (\d+): (\w+), type: ([\w ]+?),", text)
            if s:
                _state["services"][int(s.group(1))] = s.group(3)


def status():
    with _lock:
        s = dict(_state)
        s["services"] = [{"n": n, "type": t if t != "None" else None} for n, t in sorted(_state.get("services", {}).items())]
    s["waiting_s"] = round(time.time() - s["started"]) if s.get("active") and not s.get("synced") and s.get("started") else 0
    return s
