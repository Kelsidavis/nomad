#!/usr/bin/env python3
"""Find and decode SSTV pictures in a recording (the ISS sends them during events, usually PD-120 or PD-180).

    sstv_decode.py recording.wav outdir

Writes outdir/sstv-1.png, -2.png, ... and prints their names as JSON. Uses the sstv package in ~/.venvs/sat.
"""
import json
import pathlib
import sys

import sstv


def main():
    wav, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    names = []
    for i, img in enumerate(sstv.decode_from_wav(str(wav)), 1):
        img = getattr(img, "image", img)  # a decoded result or the picture itself
        name = f"sstv-{i}.png"
        img.save(out / name)
        names.append(name)
    json.dump(names, sys.stdout)


if __name__ == "__main__":
    main()
