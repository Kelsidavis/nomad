"""ACARS: the short text messages aircraft and airlines exchange on 131 MHz: position and weather reports, engine and
gate data, now and then a crew's "ETA 1432". acarsdec (built from github.com/TLeconte/acarsdec; setup-pi.sh does it)
decodes four US channels at once into JSON lines. One of the SDR's time slices (sdr.py): a minute every five while
planes are about (ADS-B hears some), every fifteen otherwise. Messages are kept for the Radio screen's Aircraft tab and
shown on a tapped plane when its flight number matches.
"""
import json
import threading
import time

import sdr

FREQS = ("131.550", "131.525", "131.725", "131.825")  # the US channels, all within 300 kHz of each other
KEEP = 200
_lock = threading.Lock()
_messages = []  # newest first

LABELS = {"H1": "Flight management", "5Z": "Airline operations", "5U": "Weather", "10": "Departure", "11": "Arrival", "12": "Position",
          "13": "Gate", "14": "Position", "15": "Position", "16": "Departure", "20": "Position", "22": "Position", "23": "Fuel",
          "80": "Report", "83": "Position", "B9": "Message", "B6": "Message", "C1": "Clearance", "Q0": "Link test", "_d": "Ack", "SA": "Status",
          "SQ": "Squitter", "H2": "Weather", "A9": "Message", "RA": "Request", "AA": "Message", "30": "Free text", "CA": "Clearance", "F3": "Flight plan"}


def ingest_line(line, now=None):
    if not line.startswith("{"):
        return
    d = json.loads(line)
    text = (d.get("text") or "").strip()
    if not text and not d.get("libacars"):
        return
    now = now or time.time()
    m = {"t": now, "freq": d.get("freq"), "level": d.get("level"), "tail": (d.get("tail") or "").lstrip("."), "flight": d.get("flight"),
         "label": d.get("label"), "kind": LABELS.get(d.get("label")), "text": text, "msgno": d.get("msgno")}
    with _lock:
        for o in _messages[:20]:  # the same message heard on two channels, or twice
            if (o["tail"], o["flight"], o["text"], o["msgno"]) == (m["tail"], m["flight"], m["text"], m["msgno"]):
                return
        _messages.insert(0, m)
        del _messages[KEEP:]


def status():
    with _lock:
        return {"messages": list(_messages)}


def _pipeline():
    return [["acarsdec", "-o", "4", "-A", "-e", "-r", "0", *FREQS]]


def _gap(ctx):
    return 300 if ctx.get("aircraft") else 900


SLICE = sdr.Slice(
    "acars", "Aircraft messages (ACARS)", ["acarsdec"], every=300, run=60, pipeline=_pipeline, parse=ingest_line, every_when=_gap,
    detail="Text messages from aircraft on 131.5-131.8 MHz")
