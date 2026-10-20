#!/usr/bin/env python3
"""LoRa: a Meshtastic node (Heltec, RAK WisBlock, T-Beam, T-Echo...) plugged into USB, or on Wi-Fi, for off-grid text
messages and positions with other Meshtastic users: no cell signal needed, a few km between nodes, much further
through the mesh. Runs in ~/.venvs/mesh (the meshtastic package) as mesh.service; the head unit (server.py) uses
its little HTTP API on 127.0.0.1:8805:

    GET  /state     the node, the mesh's nodes, recent messages, settings
    GET  /brief     the few things the live state needs (status, newest message, nodes with positions)
    POST /send      {"text", "to"?: node id (default everyone), "channel"?: index}
    POST /settings  {"share"?: bool (send the truck's GPS position to the mesh), "speak"?: bool}

MESH_DEVICE in nomad.conf: auto (default: the first USB port that looks like a Meshtastic board, never the GPS or
the OBD adapter), /dev/ttyACM0, tcp:<host>, or off.
"""
import json
import os
import pathlib
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import config  # noqa: E402

DATA = pathlib.Path(os.environ.get("HEADUNIT_DATA", pathlib.Path.home() / ".local/share/headunit"))
RUN = DATA / "run"  # which serial port each radio has, so the OBD and mesh searches leave each other's alone
MSGS = DATA / "mesh-messages.json"
PREFS = DATA / "mesh.json"
PORT = 8805
KEEP = 300
SHARE_EVERY = 300  # seconds between position reports when sharing (sooner after moving a km)
HEADUNIT = "http://127.0.0.1:8800"

_lock = threading.Lock()
_st = {"status": "starting", "device": None, "error": None, "iface": None}
_msgs = []


def _load():
    global _msgs
    try:
        _msgs = json.loads(MSGS.read_text())[-KEEP:]
    except (OSError, ValueError):
        _msgs = []


def _save_msgs():
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = MSGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(_msgs[-KEEP:], ensure_ascii=False))
    tmp.replace(MSGS)


def prefs():
    try:
        return {"share": False, "speak": True, **json.loads(PREFS.read_text())}
    except (OSError, ValueError):
        return {"share": False, "speak": True}


def _name(iface, node_id):
    n = (iface.nodes or {}).get(node_id) if iface else None
    u = (n or {}).get("user") or {}
    return u.get("longName") or u.get("shortName") or node_id


def _on_receive(packet, interface):
    d = packet.get("decoded") or {}
    if d.get("portnum") != "TEXT_MESSAGE_APP" or not d.get("text"):
        return
    me = (interface.getMyNodeInfo() or {}).get("user", {}).get("id")
    m = {"id": packet.get("id") or int(time.time() * 1000), "from": packet.get("fromId"), "from_name": _name(interface, packet.get("fromId")),
         "to": packet.get("toId"), "dm": packet.get("toId") not in (None, "^all") and packet.get("toId") == me,
         "channel": packet.get("channel", 0), "text": d["text"][:500], "t": packet.get("rxTime") or time.time(),
         "snr": packet.get("rxSnr"), "hops": (packet.get("hopStart") or 0) - (packet.get("hopLimit") or 0) if packet.get("hopStart") else None,
         "mine": False}
    with _lock:
        _msgs.append(m)
        _save_msgs()


def _ports():
    from meshtastic import util
    taken = {os.path.realpath(p) for p in ("/dev/gps0",) if os.path.exists(p)}
    try:
        taken.add(os.path.realpath((RUN / "obd.port").read_text().strip()))
    except OSError:
        pass
    return [p for p in util.findPorts(True) if os.path.realpath(p) not in taken]


def _connect():
    from meshtastic import serial_interface, tcp_interface
    dev = (config.get("MESH_DEVICE") or "auto").strip()
    if dev.startswith("tcp:"):
        tries = [("tcp", dev[4:])]
    elif dev == "auto":
        tries = [("serial", p) for p in _ports()]
    else:
        tries = [("serial", dev)]
    if not tries:
        _st.update(status="no radio", error=None)
        return None
    for kind, where in tries:
        try:
            iface = (tcp_interface.TCPInterface(hostname=where, timeout=20) if kind == "tcp"
                     else serial_interface.SerialInterface(devPath=where, timeout=20))
            if kind == "serial":
                RUN.mkdir(parents=True, exist_ok=True)
                (RUN / "mesh.port").write_text(where)
            _st.update(status="connected", device=where, error=None, iface=iface)
            return iface
        except Exception as e:  # noqa: BLE001  the library raises all sorts when a port isn't a node
            _st.update(error=f"{where}: {str(e)[:100]}")
    _st["status"] = "no radio"
    return None


def _alive(iface):
    try:
        return iface.isConnected.is_set() and not getattr(iface, "_closing", False)
    except AttributeError:
        return True


def radio_loop():
    if (config.get("MESH_DEVICE") or "auto").strip() == "off":
        _st["status"] = "off"
        return
    last_share, last_pos = 0.0, None
    while True:
        iface = _st["iface"]
        if not iface or not _alive(iface):
            if iface:
                try:
                    iface.close()
                except Exception:  # noqa: BLE001
                    pass
                _st.update(iface=None, status="searching")
                (RUN / "mesh.port").unlink(missing_ok=True)
            iface = _connect()
            if not iface:
                time.sleep(30)
                continue
        if prefs()["share"]:
            pos = _here()
            moved = pos and last_pos and _km(pos, last_pos) > 1
            if pos and (time.time() - last_share > SHARE_EVERY or moved):
                try:
                    iface.sendPosition(latitude=pos[0], longitude=pos[1], altitude=int(pos[2] or 0))
                    last_share, last_pos = time.time(), pos
                except Exception as e:  # noqa: BLE001
                    _st["error"] = f"position: {str(e)[:100]}"
        time.sleep(5)


def _here():
    try:
        with urllib.request.urlopen(f"{HEADUNIT}/api/state", timeout=3) as r:
            p = json.load(r).get("position")
        return (p["lat"], p["lon"], p.get("alt_m")) if p else None
    except (OSError, ValueError, KeyError):
        return None


def _km(a, b):
    import math
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def _nodes(iface):
    out = []
    me = (iface.getMyNodeInfo() or {}).get("user", {}).get("id")
    for nid, n in (iface.nodes or {}).items():
        u, p, dm = n.get("user") or {}, n.get("position") or {}, n.get("deviceMetrics") or {}
        out.append({"id": nid, "name": u.get("longName") or nid, "short": u.get("shortName") or "", "hw": u.get("hwModel"),
                    "me": nid == me, "lat": p.get("latitude"), "lon": p.get("longitude"), "alt": p.get("altitude"),
                    "heard": n.get("lastHeard"), "snr": n.get("snr"), "hops": n.get("hopsAway"), "battery": dm.get("batteryLevel")})
    out.sort(key=lambda n: (not n["me"], -(n["heard"] or 0)))
    return out


def _channels(iface):
    out = []
    for ch in getattr(iface.localNode, "channels", None) or []:
        role = getattr(ch, "role", 0)
        if role:  # 0 = disabled
            out.append({"index": ch.index, "name": ch.settings.name or ("Primary" if role == 1 else f"Channel {ch.index}")})
    return out or [{"index": 0, "name": "Primary"}]


def state():
    iface = _st["iface"]
    base = {"status": _st["status"], "device": _st["device"], "error": _st["error"], "prefs": prefs()}
    with _lock:
        base["messages"] = list(_msgs[-100:])
    if not iface:
        return {**base, "nodes": [], "channels": [], "me": None}
    try:
        me = iface.getMyNodeInfo() or {}
        return {**base, "nodes": _nodes(iface), "channels": _channels(iface),
                "me": {"id": me.get("user", {}).get("id"), "name": me.get("user", {}).get("longName")}}
    except Exception as e:  # noqa: BLE001
        return {**base, "nodes": [], "channels": [], "me": None, "error": str(e)[:120]}


def brief():
    s = state()
    last = next((m for m in reversed(s["messages"]) if not m["mine"]), None)
    return {"status": s["status"], "speak": s["prefs"]["speak"], "last": last, "count": len(s["nodes"]),
            "nodes": [[n["short"] or n["name"][:4], n["lat"], n["lon"], n["heard"], n["name"]] for n in s["nodes"]
                      if n["lat"] is not None and not n["me"]]}


def send(text, to=None, channel=0):
    iface = _st["iface"]
    if not iface:
        raise OSError("No Meshtastic radio connected")
    text = text.strip()[:228]  # one LoRa packet
    pkt = iface.sendText(text, destinationId=to or "^all", wantAck=True, channelIndex=int(channel or 0))
    me = (iface.getMyNodeInfo() or {}).get("user", {})
    m = {"id": getattr(pkt, "id", None) or int(time.time() * 1000), "from": me.get("id"), "from_name": me.get("longName") or "Me",
         "to": to or "^all", "to_name": _name(iface, to) if to else None, "dm": bool(to), "channel": int(channel or 0),
         "text": text, "t": time.time(), "mine": True}
    with _lock:
        _msgs.append(m)
        _save_msgs()
    return m


def set_prefs(body):
    p = prefs()
    p.update({k: bool(body[k]) for k in ("share", "speak") if k in body})
    DATA.mkdir(parents=True, exist_ok=True)
    PREFS.write_text(json.dumps(p))
    return p


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/state":
            return self.reply(200, state())
        if self.path == "/brief":
            return self.reply(200, brief())
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/send":
                return self.reply(200, send(body["text"], body.get("to"), body.get("channel", 0)))
            if self.path == "/settings":
                return self.reply(200, set_prefs(body))
        except (OSError, ValueError, KeyError) as e:
            return self.reply(400, {"error": str(e)[:160]})
        self.reply(404, {"error": "not found"})


def main():
    from pubsub import pub  # (comes with meshtastic)
    pub.subscribe(_on_receive, "meshtastic.receive")
    _load()
    threading.Thread(target=radio_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    print(f"mesh bridge on 127.0.0.1:{PORT}", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
