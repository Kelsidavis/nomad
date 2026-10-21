#!/usr/bin/env python3
"""Power guard (powerguard.service, runs as root): watches the Pi's supply and does something about it.

Every two seconds it reads the firmware's under-voltage flag and the 5 V rail. When the voltage dips (the firmware's "Undervoltage
detected"), it
  - tells the head unit to pause the SDR listeners and ADS-B for ten minutes (the dongle and its USB power are the biggest extra load),
  - caps the CPU's top speed for the same time (a Pi 5 draws much less at 1.5 GHz than at 2.4), and
  - writes the event down: when, the voltage, the CPU speed, the load, the temperature, what the SDR was doing.
Ten quiet minutes after the last dip, everything goes back to normal. It also tells a hard power loss from a clean shutdown: it keeps a
heartbeat file, and a start-up that finds one without a "clean" mark means the last run ended without a shutdown, so that is logged too
(a power cut, or the watchdog resetting a frozen Pi). The log is DATA/power.log, one JSON object per line, shown in Settings → System.
"""
import glob
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
import urllib.request

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", "/home/k/.local/share/headunit"))
LOG = DATA / "power.log"
HEART = DATA / "power.heartbeat"
API = "http://127.0.0.1:8800/api/power"
SHED_MIN = float(os.environ.get("POWERGUARD_SHED_MIN", 10))  # minutes of reduced load after a dip, renewed by each new dip
CAP_KHZ = 1500000      # CPU top speed while shedding
LOW_VOLTS = 4.80       # a reading under this is logged even with no firmware flag
TICK_S = 2
_orig_max = {}


def run(*cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def read_throttled():
    m = re.search(r"0x([0-9a-fA-F]+)", run("vcgencmd", "get_throttled"))
    return int(m.group(1), 16) if m else 0


def read_volts():
    m = re.search(r"EXT5V_V volt\(\d+\)=([\d.]+)V", run("vcgencmd", "pmic_read_adc", "EXT5V_V"))
    return float(m.group(1)) if m else None


def read_temp():
    m = re.search(r"temp=([\d.]+)", run("vcgencmd", "measure_temp"))
    return float(m.group(1)) if m else None


def cpu_khz():
    try:
        return int(pathlib.Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").read_text()) // 1
    except (OSError, ValueError):
        return None


def log_event(kind, **fields):
    DATA.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"t": round(time.time(), 1), "type": kind, **fields}, separators=(",", ":"))
    with open(LOG, "a") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    print(line, flush=True)
    try:  # keep the log short
        lines = LOG.read_text().splitlines()
        if len(lines) > 600:
            LOG.write_text("\n".join(lines[-400:]) + "\n")
    except OSError:
        pass


def api(body):
    try:
        req = urllib.request.Request(API, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=4) as r:
            return json.load(r)
    except (OSError, ValueError):
        return None


def sdr_state():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8800/api/sdr", timeout=3) as r:
            return json.load(r).get("running")
    except (OSError, ValueError):
        return None


def cap_cpu(on):
    for path in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_max_freq"):
        try:
            p = pathlib.Path(path)
            if on:
                _orig_max.setdefault(path, p.read_text().strip())
                p.write_text(str(CAP_KHZ))
            elif path in _orig_max:
                p.write_text(_orig_max.pop(path))
        except OSError:
            pass


def heartbeat(clean=False, **extra):
    tmp = HEART.with_name(HEART.name + ".tmp")
    tmp.write_text(json.dumps({"t": round(time.time(), 1), "clean": clean, **extra}))
    os.replace(tmp, HEART)


def check_last_run():
    """A heartbeat with no clean mark: the last run didn't end with a shutdown."""
    try:
        last = json.loads(HEART.read_text())
    except (OSError, ValueError):
        return
    if not last.get("clean"):
        log_event("unclean-shutdown", last_seen=last.get("t"), volts=last.get("volts"), throttled=last.get("throttled"),
                  shedding=last.get("shedding"), note="power lost or the watchdog reset a frozen Pi")


def main():
    check_last_run()
    stop = {"now": False}

    def term(*_):
        stop["now"] = True

    signal.signal(signal.SIGTERM, term)
    signal.signal(signal.SIGINT, term)
    prev_flag, shed_until, last_low_log, last_beat = False, 0.0, 0.0, 0.0
    heartbeat(clean=False)
    log_event("start", volts=read_volts(), throttled=hex(read_throttled()))
    while not stop["now"]:
        now = time.time()
        thr, volts = read_throttled(), read_volts()
        under = bool(thr & 1)
        if under and not prev_flag:
            shed_until = now + SHED_MIN * 60
            cap_cpu(True)
            api({"shed": SHED_MIN})
            log_event("undervoltage", volts=volts, throttled=hex(thr), cpu_khz=cpu_khz(), load=os.getloadavg()[0],
                      temp_c=read_temp(), sdr=sdr_state(), action=f"paused the SDR and capped the CPU for {SHED_MIN:g} min")
        elif under and now < shed_until:
            shed_until = now + SHED_MIN * 60  # still dipping: keep it going
            api({"shed": SHED_MIN})
        prev_flag = under
        if volts is not None and volts < LOW_VOLTS and now - last_low_log > 30:
            last_low_log = now
            log_event("low-voltage", volts=volts, throttled=hex(thr), cpu_khz=cpu_khz(), load=os.getloadavg()[0])
        if shed_until and now >= shed_until:
            shed_until = 0.0
            cap_cpu(False)
            api({"shed": 0})
            log_event("recovered", volts=volts, note="quiet again: back to normal")
        if now - last_beat > 30:
            last_beat = now
            heartbeat(clean=False, volts=volts, throttled=hex(thr), shedding=bool(shed_until))
        time.sleep(TICK_S)
    cap_cpu(False)
    heartbeat(clean=True)
    log_event("stop", note="clean shutdown")


if __name__ == "__main__":
    sys.exit(main())
