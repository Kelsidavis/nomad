#!/usr/bin/env python3
"""A pretend ELM327 on a CAN truck, over TCP like a Wi-Fi adapter, for working on OBD without a vehicle:

    tools/elm327_sim.py [--port 35000] [--codes P0171,P0420]
    OBD_DEVICE=tcp:127.0.0.1:35000 (in nomad.conf or the environment) to point the head unit at it

The engine idles, revs and drives in a slow loop; mode 03 reports --codes until a mode 04 clears them.
"""
import argparse
import math
import socketserver
import threading
import time

T0 = time.time()
STATE = {"codes": [], "pending": ["P0442"]}


def engine():
    t = time.time() - T0
    cruise = max(0.0, math.sin(t / 40))  # 0 at a stop, 1 at 65 mph
    rpm = 650 + 1500 * cruise + 300 * math.sin(t / 3) * cruise
    kmh = 105 * cruise
    return {
        0x04: [int(255 * (0.18 + 0.4 * cruise))],
        0x05: [40 + min(90, int(20 + t * 2))],
        0x06: [128 + int(4 * math.sin(t))], 0x07: [131], 0x08: [127], 0x09: [130],
        0x0B: [int(30 + 50 * cruise)],
        0x0C: list(int(rpm * 4).to_bytes(2, "big")),
        0x0D: [int(kmh)],
        0x0E: [int((14 + 64) * 2)],
        0x0F: [40 + 25],
        0x10: list(int((4 + 26 * cruise) * 100).to_bytes(2, "big")),
        0x11: [int(255 * (0.16 + 0.2 * cruise))],
        0x1F: list(int(t).to_bytes(2, "big")),
        0x2F: [int(255 * 0.62)],
        0x42: list(int(14200 + 100 * math.sin(t / 7)).to_bytes(2, "big")),
        0x46: [40 + 12],
    }


def bitmask(base, pids):
    bits = 0
    for p in pids:
        if base < p <= base + 32:
            bits |= 1 << (32 - (p - base))
    if any(p > base + 32 for p in pids):
        bits |= 1  # more after this block
    return bits.to_bytes(4, "big")


def dtc_bytes(codes):
    out = []
    for c in codes:
        a = ("PCBU".index(c[0]) << 6) | (int(c[1]) << 4) | int(c[2], 16)
        out += [a, int(c[3:], 16)]
    return [len(codes)] + out


def answer(cmd):
    cmd = cmd.strip().upper().replace(" ", "")
    if cmd.startswith("AT"):
        if cmd in ("ATZ", "ATWS"):
            return ["", "ELM327 v1.5"]
        if cmd == "ATI":
            return ["ELM327 v1.5"]
        if cmd == "ATDP":
            return ["AUTO, ISO 15765-4 (CAN 11/500)"]
        if cmd == "ATDPN":
            return ["A6"]
        if cmd == "ATRV":
            return ["14.2V"]
        return ["OK"]
    try:
        mode = int(cmd[:2], 16)
        pid = int(cmd[2:4], 16) if len(cmd) >= 4 else None
    except ValueError:
        return ["?"]
    hexs = lambda bs: "".join(f"{b:02X}" for b in bs)  # noqa: E731
    if mode == 0x01:
        e = engine()
        if pid in (0x00, 0x20, 0x40, 0x60):
            pids = list(e) + [0x01]
            return [f"41{pid:02X}{bitmask(pid, pids).hex().upper()}"] if any(p > pid for p in pids) else ["NO DATA"]
        if pid == 0x01:
            return [f"4101{0x80 | len(STATE['codes']) if STATE['codes'] else 0:02X}076500"]
        if pid in e:
            return [f"41{pid:02X}{hexs(e[pid])}"]
        return ["NO DATA"]
    if mode in (0x03, 0x07):
        codes = STATE["codes"] if mode == 3 else STATE["pending"]
        return [f"{mode + 0x40:02X}{hexs(dtc_bytes(codes))}"]
    if mode == 0x04:
        STATE["codes"] = []
        return ["44"]
    if mode == 0x09 and pid == 0x02:  # a multi-frame VIN, as a CAN ECU sends it
        vin = "1GCEK19J08Z000000".encode()
        data = "490201" + vin.hex().upper()
        return ["014", f"0:{data[:12]}", f"1:{data[12:26]}", f"2:{data[26:40]}"]
    return ["NO DATA"]


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        buf = b""
        while True:
            ch = self.request.recv(1)
            if not ch:
                return
            if ch != b"\r":
                buf += ch
                continue
            cmd, buf = buf.decode(errors="replace"), b""
            if not cmd.strip():
                self.wfile.write(b"\r>")
                continue
            time.sleep(0.03)  # a real adapter takes a few tens of ms
            self.wfile.write(("\r".join(answer(cmd)) + "\r\r>").encode())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=35000)
    ap.add_argument("--codes", default="P0171,P0420", help="stored codes (comma separated)")
    a = ap.parse_args()
    STATE["codes"] = [c.strip().upper() for c in a.codes.split(",") if c.strip()]
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", a.port), Handler) as srv:
        print(f"pretend ELM327 on 127.0.0.1:{a.port}")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
