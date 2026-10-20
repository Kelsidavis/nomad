#!/usr/bin/env python3
"""Record from the ReSpeaker 2-mic HAT on the Pi and transcribe on the phone (DashLink /transcribe).

    voice.py                 # wait for speech, stop after a pause, print the text (repeats)
    voice.py --button        # push-to-talk on the HAT's button (GPIO17); hold while speaking
    voice.py --seconds 4     # fixed-length recording
    voice.py --once          # one utterance, then exit
    voice.py --echo          # speak the transcript back through the Pi's audio (tests the loop)

The mic is found by name in `arecord -l` (seeed / wm8960 / respeaker / keyestudio);
override with DASHLINK_MIC=plughw:2,0. Needs only alsa-utils; --button needs python3-gpiozero.
"""
import argparse
import array
import io
import math
import os
import re
import subprocess
import sys
import time
import wave

from dashlink import DashLink

RATE = 16000
CHUNK = RATE // 20 * 2  # 50 ms of 16-bit mono


def rms(chunk):
    """Loudness of a 16-bit little-endian chunk (audioop is gone as of Python 3.13)."""
    samples = array.array("h", chunk[: len(chunk) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return math.sqrt(sum(x * x for x in samples) / len(samples)) if samples else 0


def find_mic():
    if os.environ.get("DASHLINK_MIC"):
        return os.environ["DASHLINK_MIC"]
    listing = subprocess.run(["arecord", "-l"], capture_output=True, text=True).stdout
    for line in listing.splitlines():
        m = re.match(r"card (\d+): .*?device (\d+):", line)
        if m and re.search(r"seeed|wm8960|respeaker|keyestudio", line, re.I):
            return f"plughw:{m.group(1)},{m.group(2)}"
    sys.exit("No ReSpeaker/WM8960 card in `arecord -l`; set DASHLINK_MIC=plughw:N,0")


def open_mic(device):
    # plughw downmixes the HAT's two mics to mono and resamples to 16 kHz for us.
    return subprocess.Popen(
        ["arecord", "-q", "-D", device, "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw"],
        stdout=subprocess.PIPE)


def to_wav(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def record_vad(mic, threshold, silence_s=0.8, max_s=12.0, preroll_s=0.3):
    """Wait for sound above threshold, keep recording until silence_s of quiet."""
    preroll = []
    while True:
        chunk = mic.stdout.read(CHUNK)
        if not chunk:
            return b""
        if rms(chunk) > threshold:
            break
        preroll = (preroll + [chunk])[-int(preroll_s * 20):]
    frames, quiet = preroll + [chunk], 0
    while len(frames) < max_s * 20:
        chunk = mic.stdout.read(CHUNK)
        if not chunk:
            break
        frames.append(chunk)
        quiet = quiet + 1 if rms(chunk) < threshold else 0
        if quiet >= silence_s * 20:
            break
    return b"".join(frames)


def record_while(mic, keep_going, max_s=15.0):
    frames = []
    while keep_going() and len(frames) < max_s * 20:
        frames.append(mic.stdout.read(CHUNK))
    return b"".join(frames)


def calibrate(mic, seconds=1.0):
    """Threshold from the ambient level, so road noise doesn't count as speech."""
    levels = [rms(mic.stdout.read(CHUNK)) for _ in range(int(seconds * 20))]
    return max(300, int(sorted(levels)[len(levels) // 2] * 3))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--button", action="store_true", help="push-to-talk on GPIO17")
    ap.add_argument("--seconds", type=float, help="fixed recording length")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--echo", action="store_true", help="speak the transcript back")
    ap.add_argument("--model", help="whisper model on the phone, e.g. base.en or small.en-q8_0")
    ap.add_argument("--prompt", help="vocabulary hint, e.g. 'OBD, coolant, RPM, Kalama'")
    args = ap.parse_args()

    phone = DashLink()
    device = find_mic()
    print(f"mic {device}", file=sys.stderr)

    button = None
    if args.button:
        from gpiozero import Button
        button = Button(17)

    while True:
        mic = open_mic(device)
        try:
            if button:
                print("hold the button and talk", file=sys.stderr)
                button.wait_for_press()
                pcm = record_while(mic, lambda: button.is_pressed)
            elif args.seconds:
                print(f"recording {args.seconds}s", file=sys.stderr)
                pcm = record_while(mic, lambda: True, max_s=args.seconds)
            else:
                threshold = calibrate(mic)
                print(f"listening (threshold {threshold})", file=sys.stderr)
                pcm = record_vad(mic, threshold)
        finally:
            mic.terminate()

        if len(pcm) < RATE // 4 * 2:  # under 0.25 s: a click, not speech
            continue
        t = time.time()
        result = phone.transcribe(to_wav(pcm), model=args.model, prompt=args.prompt)
        took = time.time() - t
        if result.get("ok") and result.get("text"):
            print(result["text"], flush=True)
            print(f"  ({len(pcm) / 2 / RATE:.1f}s audio, {took:.1f}s round trip, {result.get('model')})", file=sys.stderr)
            if args.echo:
                phone.say(result["text"])
        else:
            print(f"  (nothing: {result.get('error', 'empty')})", file=sys.stderr)
        if args.once:
            break


if __name__ == "__main__":
    main()
