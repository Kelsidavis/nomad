"""OBD-II through an ELM327 adapter (or a clone: Vgate, OBDLink, the $10 blue ones): engine data for the Vehicle
screen and the assistant, check-engine codes (read and clear), and fuel economy from the MAF sensor.

The adapter can be USB (a serial port), classic Bluetooth (RFCOMM; pair it from Settings, PIN 1234 or 0000) or
Wi-Fi (TCP, usually 192.168.0.10:35000). OBD_DEVICE in nomad.conf picks one:

    auto (default)        USB serial ports that answer like an ELM327 (never the GPS), then paired Bluetooth
                          devices named like an OBD adapter
    /dev/ttyUSB1          a serial port (OBD_BAUD, default: tries 38400 and 115200)
    bt:AA:BB:CC:DD:EE:FF  a Bluetooth adapter (RFCOMM channel 1)
    tcp:192.168.0.10:35000  a Wi-Fi adapter
    off                   don't look

One thread owns the adapter: it polls the fast values (rpm, speed, load, throttle, MAF) every round and the slow
ones (temperatures, fuel level, voltage) every few seconds, and runs the reads and clears asked for in between.
"""
import glob
import os
import pathlib
import queue
import re
import select
import socket
import subprocess
import termios
import threading
import time

import config

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
RUN = DATA / "run"  # which serial port each radio holds (mesh_bridge.py writes mesh.port), so neither probes the other's

# (key, pid, label, unit, decode(bytes) -> value, fast)
PIDS = [
    ("rpm", 0x0C, "Engine speed", "rpm", lambda b: (b[0] * 256 + b[1]) / 4, True),
    ("speed", 0x0D, "Vehicle speed", "km/h", lambda b: b[0], True),
    ("load", 0x04, "Engine load", "%", lambda b: b[0] * 100 / 255, True),
    ("throttle", 0x11, "Throttle", "%", lambda b: b[0] * 100 / 255, True),
    ("maf", 0x10, "Mass air flow", "g/s", lambda b: (b[0] * 256 + b[1]) / 100, True),
    ("map", 0x0B, "Manifold pressure", "kPa", lambda b: b[0], True),
    ("coolant", 0x05, "Coolant", "°C", lambda b: b[0] - 40, False),
    ("intake", 0x0F, "Intake air", "°C", lambda b: b[0] - 40, False),
    ("ambient", 0x46, "Outside air", "°C", lambda b: b[0] - 40, False),
    ("oil", 0x5C, "Oil", "°C", lambda b: b[0] - 40, False),
    ("fuel", 0x2F, "Fuel level", "%", lambda b: b[0] * 100 / 255, False),
    ("voltage", 0x42, "Battery (ECU)", "V", lambda b: (b[0] * 256 + b[1]) / 1000, False),
    ("timing", 0x0E, "Timing advance", "°", lambda b: b[0] / 2 - 64, False),
    ("stft1", 0x06, "Short fuel trim B1", "%", lambda b: b[0] * 100 / 128 - 100, False),
    ("ltft1", 0x07, "Long fuel trim B1", "%", lambda b: b[0] * 100 / 128 - 100, False),
    ("stft2", 0x08, "Short fuel trim B2", "%", lambda b: b[0] * 100 / 128 - 100, False),
    ("ltft2", 0x09, "Long fuel trim B2", "%", lambda b: b[0] * 100 / 128 - 100, False),
    ("runtime", 0x1F, "Since engine start", "s", lambda b: b[0] * 256 + b[1], False),
]
BY_KEY = {p[0]: p for p in PIDS}
SLOW_EVERY = 5.0
GAS_G_PER_GAL = 14.7 * 2800  # stoichiometric air:fuel × grams of gasoline per US gallon
BT_NAMES = re.compile(r"obd|elm|v-?link|vgate|icar|veepeak|konnwei|carista|bafx|scan", re.I)

# Common generic codes. Anything else shows as its number; the manufacturer's P1xxx codes vary by make.
DTC_TEXT = {
    "P0010": "Camshaft actuator circuit (bank 1)", "P0011": "Camshaft timing over-advanced (bank 1)",
    "P0016": "Crankshaft/camshaft position correlation (bank 1)", "P0101": "Mass air flow sensor range/performance",
    "P0102": "Mass air flow sensor low", "P0103": "Mass air flow sensor high", "P0106": "Manifold pressure sensor range/performance",
    "P0113": "Intake air temperature sensor high", "P0117": "Coolant temperature sensor low",
    "P0118": "Coolant temperature sensor high", "P0121": "Throttle position sensor range/performance",
    "P0128": "Coolant below thermostat temperature (thermostat stuck open?)", "P0131": "O2 sensor low voltage (bank 1, sensor 1)",
    "P0133": "O2 sensor slow response (bank 1, sensor 1)", "P0135": "O2 sensor heater circuit (bank 1, sensor 1)",
    "P0141": "O2 sensor heater circuit (bank 1, sensor 2)", "P0155": "O2 sensor heater circuit (bank 2, sensor 1)",
    "P0171": "System too lean (bank 1)", "P0172": "System too rich (bank 1)", "P0174": "System too lean (bank 2)",
    "P0175": "System too rich (bank 2)", "P0200": "Injector circuit", "P0300": "Random/multiple cylinder misfire",
    "P0301": "Cylinder 1 misfire", "P0302": "Cylinder 2 misfire", "P0303": "Cylinder 3 misfire", "P0304": "Cylinder 4 misfire",
    "P0305": "Cylinder 5 misfire", "P0306": "Cylinder 6 misfire", "P0307": "Cylinder 7 misfire", "P0308": "Cylinder 8 misfire",
    "P0325": "Knock sensor circuit (bank 1)", "P0335": "Crankshaft position sensor circuit",
    "P0340": "Camshaft position sensor circuit", "P0401": "EGR flow insufficient", "P0402": "EGR flow excessive",
    "P0420": "Catalyst efficiency below threshold (bank 1)", "P0430": "Catalyst efficiency below threshold (bank 2)",
    "P0440": "Evaporative emission system", "P0441": "Evap system incorrect purge flow", "P0442": "Evap system small leak",
    "P0446": "Evap vent control circuit", "P0449": "Evap vent valve/solenoid circuit",
    "P0455": "Evap system large leak (check the gas cap)", "P0456": "Evap system very small leak",
    "P0461": "Fuel level sensor range/performance", "P0480": "Cooling fan relay circuit", "P0496": "Evap high purge flow",
    "P0500": "Vehicle speed sensor", "P0505": "Idle air control system", "P0506": "Idle speed lower than expected",
    "P0507": "Idle speed higher than expected", "P0521": "Oil pressure sensor range/performance",
    "P0562": "System voltage low", "P0563": "System voltage high", "P0600": "Serial communication link",
    "P0606": "Control module processor", "P0700": "Transmission control system (read the transmission codes)",
    "P0715": "Transmission input speed sensor", "P0720": "Transmission output speed sensor",
    "P0740": "Torque converter clutch circuit", "P0741": "Torque converter clutch stuck off",
    "P0750": "Shift solenoid A", "P0755": "Shift solenoid B", "P2135": "Throttle position sensors disagree",
    "P2138": "Accelerator pedal position sensors disagree", "P2187": "System too lean at idle (bank 1)",
    "P2188": "System too rich at idle (bank 1)", "P2096": "Downstream fuel trim too lean (bank 1)",
    "P2097": "Downstream fuel trim too rich (bank 1)", "U0100": "Lost communication with the engine computer",
    "U0101": "Lost communication with the transmission computer", "U0121": "Lost communication with the ABS module",
    "U0140": "Lost communication with the body control module",
}

_state = {"status": "starting", "adapter": None, "elm": None, "protocol": None, "vin": None, "values": {},
          "supported": [], "mil": None, "dtcs": None, "pending": None, "codes_t": 0, "error": None,
          "trip": {"km": 0.0, "gal": 0.0, "since": None}, "mpg": None, "gph": None}
_lock = threading.Lock()
_jobs = queue.Queue()


# ---- links: a serial port, RFCOMM or TCP; all just bytes in and out ----
class _Link:
    def __init__(self, desc):
        self.desc = desc

    def close(self):
        try:
            os.close(self.fd) if hasattr(self, "fd") else self.sock.close()
        except OSError:
            pass

    def write(self, data):
        if hasattr(self, "fd"):
            os.write(self.fd, data)
        else:
            self.sock.sendall(data)

    def read(self, n, timeout):
        src = self.fd if hasattr(self, "fd") else self.sock
        if not select.select([src], [], [], timeout)[0]:
            return b""
        return os.read(self.fd, n) if hasattr(self, "fd") else self.sock.recv(n)


def _serial(path, baud):
    link = _Link(f"{path} @ {baud}")
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    speed = getattr(termios, f"B{baud}")
    attrs[0] = 0  # iflag: raw
    attrs[1] = 0  # oflag
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0  # lflag: no echo, not canonical
    attrs[4] = attrs[5] = speed
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    link.fd = fd
    return link


def _rfcomm(mac):
    link = _Link(f"Bluetooth {mac}")
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    s.settimeout(10)
    s.connect((mac, 1))
    s.setblocking(False)
    link.sock = s
    return link


def _tcp(host, port):
    link = _Link(f"Wi-Fi {host}:{port}")
    s = socket.create_connection((host, int(port)), timeout=6)
    s.setblocking(False)
    link.sock = s
    return link


# ---- the ELM327 conversation ----
class Elm:
    def __init__(self, link):
        self.link = link
        self.can = True

    def cmd(self, text, timeout=3.0):
        """Send a command, return the reply lines (without the echo, "SEARCHING..." and the > prompt)."""
        self.link.write(text.encode() + b"\r")
        buf, end = b"", time.time() + timeout
        while b">" not in buf and time.time() < end:
            buf += self.link.read(512, max(0.05, end - time.time()))
        if b">" not in buf:
            raise TimeoutError(f"no answer to {text}")
        lines = [ln.strip() for ln in buf.decode("ascii", "replace").replace(">", "").replace("\n", "\r").split("\r")]
        return [ln for ln in lines if ln and ln != text and not ln.startswith("SEARCHING")]

    def setup(self):
        self.link.write(b"\r")  # end anything half-sent
        time.sleep(0.2)
        while self.link.read(1024, 0.2):
            pass
        ident = " ".join(self.cmd("ATZ", timeout=5))  # reset: "ELM327 v1.5"
        if "ELM" not in ident.upper() and "OBD" not in ident.upper():
            ident = " ".join(self.cmd("ATI"))
            if "ELM" not in ident.upper():
                raise OSError(f"not an ELM327 ({ident[:40]!r})")
        for c in ("ATE0", "ATL0", "ATS0", "ATH0", "ATAT1", "ATSP0"):  # no echo, linefeeds, spaces or headers; auto protocol
            self.cmd(c)
        return re.search(r"ELM327\s*v?[\d.]+", ident, re.I).group(0) if re.search(r"ELM327\s*v?[\d.]+", ident, re.I) else ident[:30]

    def query(self, mode, pid=None, timeout=4.0):
        """The data bytes of the first ECU's answer to mode/pid, or None (NO DATA, unsupported)."""
        req = f"{mode:02X}" + (f"{pid:02X}" if pid is not None else "")
        lines = self.cmd(req, timeout)
        if any(x in ln for ln in lines for x in ("NO DATA", "UNABLE", "ERROR", "STOPPED", "?", "BUS INIT")):
            return None
        data = _join_frames(lines)
        want = f"{mode + 0x40:02X}" + (f"{pid:02X}" if pid is not None else "")
        i = data.find(want)
        if i < 0:
            return None
        rest = data[i + len(want):]
        return bytes.fromhex(rest[: len(rest) // 2 * 2])

    def protocol(self):
        dp = " ".join(self.cmd("ATDP"))
        dpn = " ".join(self.cmd("ATDPN")).lstrip("A")
        self.can = dpn[:1] in "6789ABC"
        return dp.replace("AUTO, ", "")


def _join_frames(lines):
    """One hex string from an answer: ISO-TP multi-frame lines ("0:4902...", "1:...") joined in order, a byte-count
    line ("014") dropped, and only the first ECU when several answer."""
    frames = [ln for ln in lines if re.match(r"^[0-9A-F]:", ln)]
    if frames:
        return "".join(ln[2:] for ln in frames)  # in the order they came (the index wraps after F)
    hexes = [ln.replace(" ", "") for ln in lines if re.fullmatch(r"[0-9A-F ]+", ln) and len(ln.replace(" ", "")) > 3]
    return hexes[0] if hexes else ""


def _dtcs(raw, can):
    """Codes from a mode 03/07 answer: two bytes each (CAN puts a count byte first)."""
    if not raw:
        return []
    if can:
        raw = raw[1:]
    out = []
    for i in range(0, len(raw) - 1, 2):
        a, b = raw[i], raw[i + 1]
        if a == 0 and b == 0:
            continue
        out.append(f"{'PCBU'[a >> 6]}{(a >> 4) & 3}{a & 0xF:X}{b:02X}")
    return out


def describe(code):
    return DTC_TEXT.get(code) or {"P1": "Manufacturer-specific powertrain code", "P3": "Manufacturer-specific powertrain code",
                                  "B": "Body code", "C": "Chassis code", "U": "Network communication code"}.get(
        code[:2] if code[:2] in ("P1", "P3") else code[0], "Powertrain code")


# ---- finding the adapter ----
def _gps_port():
    try:
        return os.path.realpath("/dev/gps0")
    except OSError:
        return None


def _candidates():
    dev = (config.get("OBD_DEVICE") or "auto").strip()
    bauds = [int(config.get("OBD_BAUD"))] if config.get("OBD_BAUD") else [38400, 115200, 9600]
    if dev == "off":
        return []
    if dev.startswith("bt:"):
        return [lambda: _rfcomm(dev[3:])]
    if dev.startswith("tcp:"):
        host, _, port = dev[4:].partition(":")
        return [lambda: _tcp(host, port or 35000)]
    if dev != "auto":
        return [lambda b=b: _serial(dev, b) for b in bauds]
    taken = {_gps_port()}
    try:
        taken.add(os.path.realpath((RUN / "mesh.port").read_text().strip()))
    except OSError:
        pass
    ports = [p for p in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*") + glob.glob("/dev/rfcomm*"))
             if os.path.realpath(p) not in taken]
    out = [lambda p=p, b=b: _serial(p, b) for p in ports for b in bauds]
    out += [lambda m=m: _rfcomm(m) for m in _paired_obd()]
    return out


def _paired_obd():
    try:
        lines = subprocess.run(["bluetoothctl", "devices", "Paired"], capture_output=True, text=True, timeout=8).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []
    return [m.group(1) for ln in lines if (m := re.match(r"Device ([0-9A-F:]{17}) (.*)", ln.strip())) and BT_NAMES.search(m.group(2))]


def scan_bluetooth(seconds=12):
    """Nearby Bluetooth devices that look like OBD adapters: [{"mac", "name", "paired"}]."""
    try:
        subprocess.run(["bluetoothctl", "--timeout", str(seconds), "scan", "on"], capture_output=True, timeout=seconds + 5)
        lines = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True, timeout=8).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []
    paired = set(_paired_obd())
    found = [{"mac": m.group(1), "name": m.group(2), "paired": m.group(1) in paired}
             for ln in lines if (m := re.match(r"Device ([0-9A-F:]{17}) (.*)", ln.strip()))]
    return [d for d in found if BT_NAMES.search(d["name"])]


def pair_bluetooth(mac, pins=("1234", "0000", "6789")):
    """Pair and trust a Bluetooth ELM327, answering its PIN request (the usual ones in turn). True when paired."""
    for pin in pins:
        p = subprocess.Popen(["bluetoothctl"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        out = []
        done = threading.Event()

        def reader():
            for line in p.stdout:
                out.append(line)
                if re.search(r"Enter PIN code|PIN code:", line, re.I):
                    p.stdin.write(pin + "\n")
                    p.stdin.flush()
                if re.search(r"Pairing successful|Failed to pair|AlreadyExists", line):
                    done.set()
        threading.Thread(target=reader, daemon=True).start()
        try:
            for c in ("agent KeyboardOnly", "default-agent", f"pair {mac}"):
                p.stdin.write(c + "\n")
                p.stdin.flush()
                time.sleep(0.5)
            done.wait(25)
            text = "".join(out)
            ok = "Pairing successful" in text or "AlreadyExists" in text
            if ok:
                p.stdin.write(f"trust {mac}\n")
                p.stdin.flush()
                time.sleep(1)
        finally:
            try:
                p.stdin.write("quit\n")
                p.stdin.flush()
            except OSError:
                pass
            p.wait(timeout=5)
        if ok:
            with _lock:
                _state["error"] = None
            _jobs.put(("reconnect", None))
            return True
    return False


# ---- the loop ----
def _set(**kw):
    with _lock:
        _state.update(kw)


def status():
    with _lock:
        s = dict(_state)
        s["values"] = dict(s["values"])
        s["trip"] = dict(s["trip"])
    return s


def read_codes(wait=20):
    """Ask the loop to read the codes now; returns the state when done (or after wait seconds)."""
    ev = threading.Event()
    _jobs.put(("codes", ev))
    ev.wait(wait)
    return status()


def clear_codes(wait=20):
    """Clear the codes and turn off the check-engine light (mode 04). Also resets the emission readiness monitors."""
    ev = threading.Event()
    _jobs.put(("clear", ev))
    ev.wait(wait)
    return status()


def _connect():
    for make in _candidates():
        link = None
        try:
            link = make()
            elm = Elm(link)
            version = elm.setup()
            _set(status="connecting", adapter=link.desc, elm=version, error=None)
            if hasattr(link, "fd"):
                RUN.mkdir(parents=True, exist_ok=True)
                (RUN / "obd.port").write_text(link.desc.split(" @ ")[0])
            if elm.query(0x01, 0x00, timeout=12) is None:  # the first query also finds the protocol (slow)
                link.close()
                _set(status="no ecu", error="The adapter answers but the truck doesn't: is the ignition on?")
                return None
            return elm
        except (OSError, TimeoutError, ValueError, termios.error) as e:
            if link:
                link.close()
            _set(error=str(e)[:120])
    return None


def _supported(elm):
    out = set()
    for base in (0x00, 0x20, 0x40, 0x60):
        b = elm.query(0x01, base)
        if not b or len(b) < 4:
            break
        bits = int.from_bytes(b[:4], "big")
        out |= {base + i + 1 for i in range(32) if bits >> (31 - i) & 1}
        if not bits & 1:
            break
    return out


def _vin(elm):
    try:
        b = elm.query(0x09, 0x02, timeout=5)
    except (OSError, TimeoutError):
        return None
    if not b:
        return None
    vin = bytes(c for c in b if 32 < c < 127).decode()[-17:]
    return vin if len(vin) == 17 else None


def _codes(elm):
    mil = elm.query(0x01, 0x01)
    stored = _dtcs(elm.query(0x03), elm.can)
    pending = _dtcs(elm.query(0x07), elm.can)
    _set(mil=bool(mil and mil[0] & 0x80), dtcs=[{"code": c, "text": describe(c)} for c in stored],
         pending=[{"code": c, "text": describe(c)} for c in pending], codes_t=time.time())


def _fuel(values, dt):
    """Instant mpg (or gallons an hour at a stop) and the trip average, from MAF and speed."""
    maf, kmh = values.get("maf"), values.get("speed")
    if maf is None or kmh is None:
        return
    gph = maf["v"] * 3600 / GAS_G_PER_GAL
    mph = kmh["v"] * 0.621371
    with _lock:
        tr = _state["trip"]
        if tr["since"] is None:
            tr["since"] = time.time()
        if 0 < dt < 10:
            tr["km"] += kmh["v"] * dt / 3600
            tr["gal"] += gph * dt / 3600
        _state["gph"] = round(gph, 2)
        _state["mpg"] = round(mph / gph, 1) if gph > 0.05 and mph > 3 else None


def obd_loop():
    if (config.get("OBD_DEVICE") or "auto").strip() == "off":
        _set(status="off")
        return
    while True:
        _set(status="searching")
        elm = _connect()
        if not elm:
            with _lock:
                if _state["status"] == "searching":
                    _state["status"] = "no adapter"
            try:  # look again in a while, or when an adapter was just paired
                _jobs.get(timeout=30)
            except queue.Empty:
                pass
            continue
        try:
            proto = elm.protocol()
            sup = _supported(elm)
            usable = [p for p in PIDS if p[1] in sup] or [p for p in PIDS if p[0] in ("rpm", "speed", "coolant")]
            _set(status="connected", protocol=proto, supported=[p[0] for p in usable], vin=_vin(elm))
            _codes(elm)
            fast = [p for p in usable if p[5]]
            slow = [p for p in usable if not p[5]]
            last_slow, last_round, si = 0.0, time.time(), 0
            fails = 0
            while True:
                try:
                    job, ev = _jobs.get_nowait()
                    if job == "codes":
                        _codes(elm)
                    elif job == "clear":
                        elm.query(0x04)
                        time.sleep(1)
                        _codes(elm)
                    elif job == "reconnect":
                        raise OSError("reconnecting")
                    if ev:
                        ev.set()
                except queue.Empty:
                    pass
                todo = list(fast)
                if time.time() - last_slow > SLOW_EVERY and slow:  # one slow value per round, so each round stays quick
                    todo.append(slow[si % len(slow)])
                    si += 1
                    if si % len(slow) == 0:
                        last_slow = time.time()
                got = 0
                for key, pid, label, unit, dec, _fast in todo:
                    b = elm.query(0x01, pid, timeout=2.5)
                    if b is None:
                        continue
                    try:
                        v = dec(b)
                    except IndexError:
                        continue
                    got += 1
                    with _lock:
                        _state["values"][key] = {"v": round(v, 2), "unit": unit, "label": label, "t": time.time()}
                now = time.time()
                _fuel(_state["values"], now - last_round)
                last_round = now
                fails = 0 if got else fails + 1
                if fails >= 5:  # ignition off, or the adapter went away
                    raise OSError("the truck stopped answering")
                if not fast:
                    time.sleep(1)
        except (OSError, TimeoutError, ValueError) as e:
            elm.link.close()
            (RUN / "obd.port").unlink(missing_ok=True)
            _set(status="searching", error=str(e)[:120])
            time.sleep(5)


def summary():
    """A line for the assistant: "engine 1800 rpm, coolant 90 °C, ..." (or None without OBD)."""
    s = status()
    if s["status"] != "connected":
        return None
    v = s["values"]
    parts = []
    for k, fmt in (("rpm", "engine {:.0f} rpm"), ("coolant", "coolant {:.0f} °C"), ("fuel", "fuel {:.0f}%"),
                   ("voltage", "battery {:.1f} V"), ("oil", "oil {:.0f} °C")):
        if k in v and time.time() - v[k]["t"] < 60:
            parts.append(fmt.format(v[k]["v"]))
    if s["mpg"]:
        parts.append(f"{s['mpg']} mpg now")
    if s["dtcs"]:
        parts.append("check engine codes: " + ", ".join(f"{d['code']} ({d['text']})" for d in s["dtcs"]))
    elif s["mil"] is False:
        parts.append("no check-engine codes")
    return "; ".join(parts) or None
