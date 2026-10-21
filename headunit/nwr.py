"""Emergency broadcasts from NOAA Weather Radio (162.400 to 162.550 MHz), decoded with multimon-ng.

The "all hazards" radio carries every kind of alert, not just weather: tsunami and earthquake warnings, civil
emergencies, evacuations, shelter in place, hazardous materials, law enforcement and AMBER alerts, 911 outages, and
the national Emergency Alert System. Each starts with a SAME header (ZCZC-ORG-EEE-PSSCCC+TTTT-JJJHHMM-CALLSIGN-):
who sent it, the event code, the counties it covers (FIPS), how long it lasts, when it was issued. It works with no
internet at all, which is when it matters.

This is one of the SDR's time slices (sdr.py): a short listen every few minutes, and a much busier one when the
truck is offline, since weather radio is then the only alert source. rtl_fm scans the seven channels and stops on
the one with a signal. NWR_FREQ in nomad.conf fixes one (162.55 for example).
"""
import calendar
import json
import os
import pathlib
import re
import threading
import time

import config
import durable
import sdr

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
FILE = DATA / "nwr.json"
CHANNELS = [162.400, 162.425, 162.450, 162.475, 162.500, 162.525, 162.550]
_lock = threading.Lock()
_alerts = []  # newest first

# SAME event codes: (what it is, how serious): "emergency" (act now), "warning", "watch", "info", "test"
EVENTS = {
    # not weather
    "CEM": ("Civil Emergency Message", "emergency"), "CDW": ("Civil Danger Warning", "emergency"),
    "EVI": ("Evacuation Immediate", "emergency"), "SPW": ("Shelter in Place Warning", "emergency"),
    "HMW": ("Hazardous Materials Warning", "emergency"), "NUW": ("Nuclear Power Plant Warning", "emergency"),
    "RHW": ("Radiological Hazard Warning", "emergency"), "LEW": ("Law Enforcement Warning", "emergency"),
    "LAE": ("Local Area Emergency", "emergency"), "CAE": ("Child Abduction Emergency (AMBER)", "emergency"),
    "BLU": ("Blue Alert", "emergency"), "TOE": ("911 Telephone Outage Emergency", "warning"),
    "FRW": ("Fire Warning", "emergency"), "EQW": ("Earthquake Warning", "emergency"),
    "VOW": ("Volcano Warning", "emergency"), "TSW": ("Tsunami Warning", "emergency"), "TSA": ("Tsunami Watch", "watch"),
    "AVW": ("Avalanche Warning", "emergency"), "AVA": ("Avalanche Watch", "watch"),
    "EAN": ("Emergency Action Notification (national)", "emergency"), "EAT": ("Emergency Action Termination", "info"),
    "NIC": ("National Information Center", "info"), "NPT": ("National Periodic Test", "test"),
    "NMN": ("Network Message Notification", "info"), "ADR": ("Administrative Message", "info"),
    "DMO": ("Practice/Demo Warning", "test"), "RMT": ("Required Monthly Test", "test"), "RWT": ("Required Weekly Test", "test"),
    # weather
    "TOR": ("Tornado Warning", "emergency"), "TOA": ("Tornado Watch", "watch"), "SVR": ("Severe Thunderstorm Warning", "warning"),
    "SVA": ("Severe Thunderstorm Watch", "watch"), "SVS": ("Severe Weather Statement", "info"),
    "FFW": ("Flash Flood Warning", "emergency"), "FFA": ("Flash Flood Watch", "watch"), "FFS": ("Flash Flood Statement", "info"),
    "FLW": ("Flood Warning", "warning"), "FLA": ("Flood Watch", "watch"), "FLS": ("Flood Statement", "info"),
    "EWW": ("Extreme Wind Warning", "emergency"), "HWW": ("High Wind Warning", "warning"), "HWA": ("High Wind Watch", "watch"),
    "SMW": ("Special Marine Warning", "warning"), "SPS": ("Special Weather Statement", "info"),
    "BZW": ("Blizzard Warning", "warning"), "WSW": ("Winter Storm Warning", "warning"), "WSA": ("Winter Storm Watch", "watch"),
    "ICW": ("Ice Storm Warning", "warning"), "LSW": ("Lake Effect Snow Warning", "warning"), "FSW": ("Flash Freeze Warning", "warning"),
    "FZW": ("Freeze Warning", "warning"), "DSW": ("Dust Storm Warning", "warning"), "CFW": ("Coastal Flood Warning", "warning"),
    "CFA": ("Coastal Flood Watch", "watch"), "SSW": ("Storm Surge Warning", "emergency"), "SSA": ("Storm Surge Watch", "watch"),
    "HUW": ("Hurricane Warning", "emergency"), "HUA": ("Hurricane Watch", "watch"), "TRW": ("Tropical Storm Warning", "warning"),
    "TRA": ("Tropical Storm Watch", "watch"), "HLS": ("Hurricane Local Statement", "info"),
}
ORIGINATORS = {"EAS": "Emergency Alert System", "CIV": "civil authorities", "WXR": "National Weather Service", "PEP": "national primary station"}
HEADER = re.compile(r"ZCZC-([A-Z]{3})-([A-Z0-9]{3})-((?:\d{6}-?)+)\+(\d{4})-(\d{7})-([A-Za-z0-9/ ]{1,9})-")


def parse(line, now=None):
    """A multimon-ng line ("EAS: ZCZC-WXR-TOR-053015+0045-2931600-KEC83/NWS-") as an alert dict, or None."""
    m = HEADER.search(line)
    if not m:
        return None
    org, code, locs, dur, issued, sender = m.groups()
    now = now or time.time()
    name, level = EVENTS.get(code, (f"Alert {code}", "warning"))
    hh, mm = int(dur[:2]), int(dur[2:])
    day, hour, minute = int(issued[:3]), int(issued[3:5]), int(issued[5:7])
    utc = time.gmtime(now)
    # the issue time is a day of the year and UTC clock time: this year, or last if that lands in the future
    try:
        t = calendar.timegm((utc.tm_year, 1, 1, hour, minute, 0)) + (day - 1) * 86400
        if t > now + 86400:
            t = calendar.timegm((utc.tm_year - 1, 1, 1, hour, minute, 0)) + (day - 1) * 86400
    except (ValueError, OverflowError):
        t = now
    return {"org": org, "origin": ORIGINATORS.get(org, org), "code": code, "event": name, "level": level,
            "counties": [x for x in locs.split("-") if x], "minutes": hh * 60 + mm, "issued": t, "heard": now,
            "sender": sender.strip(), "key": f"{code}|{locs}|{issued}|{sender}"}


def ingest_line(line, now=None):
    a = parse(line, now)
    if not a:
        return
    with _lock:
        for old in _alerts:
            if old["key"] == a["key"]:  # the header is sent three times, and again each time the alert is replayed
                old["heard"] = a["heard"]
                old["count"] = old.get("count", 1) + 1
                return
        a["count"] = 1
        _alerts.insert(0, a)
        del _alerts[40:]
        DATA.mkdir(parents=True, exist_ok=True)
        durable.write_text(FILE, json.dumps(_alerts))


def _load():
    try:
        _alerts.extend(json.loads(FILE.read_text()))
    except (OSError, ValueError):
        pass


def status(now=None):
    """Alerts from the last two days, with whether each is still in force."""
    now = now or time.time()
    with _lock:
        out = []
        for a in _alerts:
            if now - a["heard"] > 172800:
                continue
            q = dict(a)
            q["active"] = now < a["issued"] + a["minutes"] * 60 + 300
            out.append(q)
    return {"alerts": out, "channels": CHANNELS}


def _pipeline():
    pinned = config.get("NWR_FREQ")
    freqs = [str(pinned)] if pinned else [f"{c:.3f}M" for c in CHANNELS]
    rtl = ["rtl_fm", *[a for f in freqs for a in ("-f", f)], "-M", "fm", "-s", "22050", "-l", "50" if len(freqs) > 1 else "0", "-E", "dc", "-"]
    return [rtl, ["multimon-ng", "-q", "-t", "raw", "-a", "EAS", "/dev/stdin"]]


def _gap(ctx):
    return 420 if ctx.get("online") is True else 75


_load()
SLICE = sdr.Slice(
    "nwr", "Emergency broadcasts (NOAA Weather Radio)", ["rtl_fm", "multimon-ng"], every=420, run=40, pipeline=_pipeline,
    parse=ingest_line, every_when=_gap,
    detail="Alerts of every kind on 162.4-162.55 MHz (weather, tsunami, civil emergency, AMBER...); often when offline")
