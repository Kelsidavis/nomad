"""A small Chrome DevTools Protocol client (standard library only): enough to run JavaScript in the kiosk's page.

    page = Page(9222)               # Chromium started with --remote-debugging-port=9222
    page.eval("document.title")     # -> the value
"""
import base64
import json
import os
import socket
import struct
import urllib.request


class Page:
    def __init__(self, port=9222, url_part="127.0.0.1:8800"):
        tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5))
        tab = next((t for t in tabs if t.get("type") == "page" and url_part in t.get("url", "")), None) or next(t for t in tabs if t.get("type") == "page")
        self.url = tab["url"]
        host, _, rest = tab["webSocketDebuggerUrl"].removeprefix("ws://").partition("/")
        self.sock = socket.create_connection((host.split(":")[0], int(host.split(":")[1])), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /{rest} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise OSError(f"WebSocket handshake failed: {head[:80]!r}")
        self.buf = b""
        self.next_id = 0

    # ---- framing ----
    def _send(self, text):
        data = text.encode()
        n = len(data)
        header = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536
                                  else bytes([0x80 | 127]) + struct.pack(">Q", n))
        mask = os.urandom(4)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("DevTools connection closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _recv(self):
        """The next complete text message."""
        parts = []
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if op == 8:
                raise OSError("DevTools closed the page")
            if op == 9:  # ping
                self.sock.sendall(bytes([0x8A, 0x80]) + b"\0\0\0\0")
                continue
            parts.append(payload)
            if b0 & 0x80:
                return b"".join(parts).decode()

    # ---- protocol ----
    def call(self, method, params=None, timeout=60):
        self.next_id += 1
        mid = self.next_id
        self._send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        self.sock.settimeout(timeout)
        while True:
            msg = json.loads(self._recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})

    def eval(self, js, timeout=60):
        """Run JavaScript in the page (a promise is awaited) and return its value."""
        r = self.call("Runtime.evaluate", {"expression": js, "awaitPromise": True, "returnByValue": True, "userGesture": True}, timeout)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise RuntimeError(f"page error: {d.get('exception', {}).get('description') or d.get('text')}")
        return r.get("result", {}).get("value")
