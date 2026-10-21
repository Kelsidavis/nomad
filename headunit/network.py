"""Wi-Fi from Settings (NetworkManager's nmcli; setup-pi.sh lets the netdev group use it without a password), and
how much the head unit may use the internet ("data use"):

    normal   everything, including background downloads (weather pictures and animations, radar)
    saver    what you ask for (a search fallback, opening a picture), but no background downloads
    offline  never goes online
    auto     saver on a metered connection (a phone hotspot or Bluetooth tethering, as NetworkManager judges it),
             normal otherwise
"""
import json
import os
import pathlib
import subprocess
import time
import durable

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
PREFS = DATA / "network.json"
MODES = ("auto", "normal", "saver", "offline")
_metered = {"t": 0, "v": False}


def _nm(*args, timeout=20):
    """nmcli's terse output as rows of fields (':' separated, '\\:' escaped)."""
    out = subprocess.run(["nmcli", "-t", *args], capture_output=True, text=True, timeout=timeout)
    if out.returncode:
        raise OSError((out.stderr or out.stdout).strip().replace("Error: ", "") or "nmcli failed")
    rows = []
    for line in out.stdout.splitlines():
        fields, cur, esc = [], "", False
        for ch in line:
            if esc:
                cur, esc = cur + ch, False
            elif ch == "\\":
                esc = True
            elif ch == ":":
                fields.append(cur)
                cur = ""
            else:
                cur += ch
        rows.append(fields + [cur])
    return rows


def wifi(rescan=False):
    """{"networks": [{"ssid", "signal", "secure", "active", "saved"}], "active", "metered", "mode", "effective"}."""
    saved = {r[0] for r in _nm("-f", "NAME,TYPE", "connection", "show") if len(r) > 1 and r[1] == "802-11-wireless"}
    nets = {}
    for in_use, ssid, signal, security in (r[:4] for r in _nm("-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                                                                   "--rescan", "yes" if rescan else "auto", timeout=30)):
        if not ssid:
            continue  # hidden networks
        n = nets.get(ssid)
        if not n or int(signal or 0) > n["signal"] or in_use == "*":
            nets[ssid] = {"ssid": ssid, "signal": int(signal or 0), "secure": bool(security.strip()),
                          "active": in_use == "*" or bool(n and n["active"]), "saved": ssid in saved}
    active = next((n["ssid"] for n in nets.values() if n["active"]), None)
    return {"networks": sorted(nets.values(), key=lambda n: (not n["active"], not n["saved"], -n["signal"])),
            "active": active, "metered": metered(force=True), **data_use()}


def connect(ssid, password=None):
    """Join a network: a saved one by name, else a new one with its password."""
    saved = {r[0] for r in _nm("-f", "NAME,TYPE", "connection", "show") if len(r) > 1 and r[1] == "802-11-wireless"}
    if ssid in saved and not password:
        _nm("connection", "up", "id", ssid, timeout=45)
    else:
        _nm("device", "wifi", "connect", ssid, *(["password", password] if password else []), timeout=45)
    return wifi()


def forget(ssid):
    _nm("connection", "delete", "id", ssid)
    return wifi()


def metered(force=False):
    """Is the connection we're on metered? (NetworkManager's GENERAL.METERED: "yes", "yes (guessed)", "no ...")"""
    if force or time.time() - _metered["t"] > 60:
        try:
            out = subprocess.run(["nmcli", "-t", "-g", "METERED", "general", "status"], capture_output=True, text=True, timeout=5).stdout
            if not out.strip():  # older nmcli: ask the device that holds the default route
                dev = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, timeout=5).stdout.split()
                dev = dev[dev.index("dev") + 1] if "dev" in dev else "wlan0"
                out = subprocess.run(["nmcli", "-t", "-g", "GENERAL.METERED", "device", "show", dev], capture_output=True, text=True, timeout=5).stdout
            _metered.update(t=time.time(), v=out.strip().startswith("yes"))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return _metered["v"]


def data_use():
    try:
        mode = json.loads(PREFS.read_text()).get("data", "auto")
    except (OSError, ValueError):
        mode = "auto"
    mode = mode if mode in MODES else "auto"
    effective = ("saver" if metered() else "normal") if mode == "auto" else mode
    return {"mode": mode, "effective": effective}


def set_data_use(mode):
    if mode not in MODES:
        raise ValueError(f"data use must be one of {', '.join(MODES)}")
    DATA.mkdir(parents=True, exist_ok=True)
    durable.write_text(PREFS, json.dumps({"data": mode}))
    return data_use()


def allowed(kind="ask"):
    """May we go online? kind "ask": something the driver asked for; "background": downloads nobody's waiting on."""
    if os.environ.get("HEADUNIT_OFFLINE") == "1":
        return False
    eff = data_use()["effective"]
    return eff == "normal" or (eff == "saver" and kind == "ask")
