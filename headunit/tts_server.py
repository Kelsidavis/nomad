#!/usr/bin/env python3
"""Text to speech for the head unit, on 127.0.0.1:5002: Kokoro (expressive, American female voices; Bella by
default) on two cores, with every sentence cached on disk.

    POST /tts       {"text": "...", "voice": "af_bella"}   -> audio/wav (from the cache when it's there)
    POST /prefetch  {"texts": [...], "voice": "af_bella"}  -> {"queued": n}: synthesized in the background, so the
                                                             navigation prompts of a route play the moment they're due

Kokoro runs at about real time on two Pi 5 cores (a 3 s sentence takes ~3 s), hence the prefetching and the cache.
Runs from ~/.venvs/kokoro (tts.service); server.py fetches the WAV and plays it.
"""
import hashlib
import io
import json
import os
import pathlib
import queue
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import onnxruntime as ort
from kokoro_onnx import Kokoro

MODELS = pathlib.Path.home() / "models"
CACHE = pathlib.Path(os.environ.get("HEADUNIT_CACHE", pathlib.Path.home() / ".cache/headunit")) / "tts"
PORT = int(os.environ.get("HEADUNIT_TTS_PORT", 5002))
VOICES = {"af_sky", "af_bella", "af_heart", "af_nicole"}
DEFAULT_VOICE = "af_bella"
SPEED = 1.05
MAX_CACHED = 3000  # sentences (about 100 KB each)

so = ort.SessionOptions()
so.intra_op_num_threads = 2  # all four cores would push the car supply into under-voltage
so.inter_op_num_threads = 1
kokoro = Kokoro.from_session(ort.InferenceSession(str(MODELS / "kokoro/kokoro-v1.0.int8.onnx"), so,
                                                  providers=["CPUExecutionProvider"]),
                             str(MODELS / "kokoro/voices-v1.0.bin"))
lock = threading.Lock()  # one synthesis at a time keeps the CPU (and supply) load bounded
background = queue.Queue()


def cache_path(text, voice):
    return CACHE / f"{hashlib.sha1(f'{voice}|{SPEED}|{text}'.encode()).hexdigest()}.wav"


def speak(text, voice):
    """The WAV for text, from the cache or made now (and kept)."""
    voice = voice if voice in VOICES else DEFAULT_VOICE
    path = cache_path(text, voice)
    try:
        data = path.read_bytes()
        os.utime(path)  # recently used: kept when the cache is trimmed
        return data
    except OSError:
        pass
    with lock:
        audio, sr = kokoro.create(text, voice=voice, speed=SPEED, lang="en-us")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    data = buf.getvalue()
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return data


def prefetch_loop():
    while True:
        text, voice = background.get()
        try:
            if not cache_path(text, voice if voice in VOICES else DEFAULT_VOICE).exists():
                speak(text, voice)
        except Exception as e:  # noqa: BLE001 - one bad sentence mustn't stop the rest
            print(f"prefetch failed: {e}", file=sys.stderr)
        if background.empty():
            trim_cache()


def trim_cache():
    files = sorted(CACHE.glob("*.wav"), key=lambda f: f.stat().st_mtime)
    for f in files[:-MAX_CACHED]:
        f.unlink(missing_ok=True)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except (ValueError, TypeError):
            return self.reply(400, b'{"error": "send JSON"}', "application/json")
        voice = body.get("voice") or DEFAULT_VOICE
        if self.path == "/tts" and isinstance(body.get("text"), str) and body["text"].strip():
            return self.reply(200, speak(body["text"].strip()[:1000], voice), "audio/wav")
        if self.path == "/prefetch" and isinstance(body.get("texts"), list):
            texts = [t.strip()[:1000] for t in body["texts"] if isinstance(t, str) and t.strip()][:200]
            for t in texts:
                background.put((t, voice))
            return self.reply(200, json.dumps({"queued": len(texts)}).encode(), "application/json")
        return self.reply(404, b'{"error": "POST /tts {text} or /prefetch {texts}"}', "application/json")

    def reply(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=prefetch_loop, daemon=True).start()
    print(f"tts on http://127.0.0.1:{PORT} with Kokoro ({DEFAULT_VOICE} by default)", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
