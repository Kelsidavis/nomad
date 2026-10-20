"""Bluetooth pairing for the head unit, from Settings: a five-minute window in which the Pi shows up under its name
(NOMAD_NAME in nomad.conf, "Truck" by default) and accepts a phone ("just works": the phone shows a confirm prompt);
whatever pairs is trusted, so it reconnects by itself after that. bluetoothctl does the work (BlueZ; the Pi's audio server provides music and hands-free).
"""
import re
import subprocess
import threading
import time

import config

ALIAS = config.NAME
WINDOW_S = 300

_lock = threading.Lock()
_until = 0.0


def _ctl(*args, timeout=8):
    try:
        return subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def devices():
    """Paired devices: [{"mac", "name", "connected"}]."""
    out = []
    for line in _ctl("devices", "Paired").splitlines():
        m = re.match(r"Device ([0-9A-F:]{17}) (.*)", line.strip())
        if m:
            info = _ctl("info", m.group(1))
            out.append({"mac": m.group(1), "name": m.group(2), "connected": "Connected: yes" in info})
    return out


def status():
    show = _ctl("show")
    return {"powered": "Powered: yes" in show, "pairing": time.time() < _until,
            "pairing_left_s": max(0, round(_until - time.time())), "alias": ALIAS, "devices": devices()}


def pair():
    """Open the pairing window (or keep the one already open)."""
    global _until
    with _lock:
        if time.time() < _until:
            return status()
        _until = time.time() + WINDOW_S
    threading.Thread(target=_window, daemon=True).start()
    return status()


def _window():
    global _until
    # One bluetoothctl session holds the agent that answers the phone's pairing request.
    agent = subprocess.Popen(["bluetoothctl"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, text=True)
    try:
        for cmd in (f"system-alias {ALIAS}", "power on", "agent NoInputNoOutput", "default-agent", "pairable on",
                    f"discoverable-timeout {WINDOW_S}", "discoverable on"):
            agent.stdin.write(cmd + "\n")
            agent.stdin.flush()
        while time.time() < _until:
            for d in devices():
                if "Trusted: yes" not in _ctl("info", d["mac"]):
                    _ctl("trust", d["mac"])
            time.sleep(4)
    finally:
        for cmd in ("discoverable off", "pairable off", "quit"):
            try:
                agent.stdin.write(cmd + "\n")
                agent.stdin.flush()
            except OSError:
                break
        try:
            agent.wait(timeout=5)
        except subprocess.TimeoutExpired:
            agent.kill()
        _until = 0.0


def forget(mac):
    if not re.fullmatch(r"[0-9A-F:]{17}", mac or ""):
        raise ValueError("bad device address")
    _ctl("remove", mac)
    return status()
