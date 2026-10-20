#!/usr/bin/env python3
"""Draws the boot splash logo (nomad/logo.png): a compass-needle mark and NOMAD in Nunito Sans, on transparent.
Needs Pillow (the Pi's ~/.venvs/sat has it): make_logo.py [font.ttf]"""
import pathlib
import sys

from PIL import Image, ImageDraw, ImageFont

FONT = sys.argv[1] if len(sys.argv) > 1 else "/usr/share/fonts/truetype/nunito-sans/NunitoSans-VariableFont_YTLC,opsz,wdth,wght.ttf"
ACCENT, TEXT, DIM = (76, 141, 255), (238, 241, 245), (154, 163, 175)
S = 2  # drawn at 2x, then scaled down for smooth edges


def font(size, weight):
    f = ImageFont.truetype(FONT, size * S)
    try:  # a variable font: pick the weight by its style name
        names = [n.decode() if isinstance(n, bytes) else n for n in f.get_variation_names()]
        f.set_variation_by_name(next(n for n in names if n.lower().replace(" ", "") == weight.lower()))
    except (OSError, ValueError, StopIteration, AttributeError):
        pass
    return f


title, sub = font(132, "ExtraBold"), font(24, "SemiBold")
SUB = "Navigation · Observation · Media · Autonomous Data"
text_w = max(title.getbbox("NOMAD")[2], sub.getbbox(SUB)[2]) / S
W, H = int(250 + text_w + 30), 300
img = Image.new("RGBA", (W * S, H * S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
# the mark: a compass needle (the map's car arrow) in a ring
cx, cy, r = 120 * S, 150 * S, 92 * S
d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ACCENT, width=10 * S)
d.polygon([(cx, cy - 62 * S), (cx + 40 * S, cy + 48 * S), (cx, cy + 26 * S), (cx - 40 * S, cy + 48 * S)], fill=TEXT)
d.text((250 * S, 50 * S), "NOMAD", font=title, fill=TEXT)
d.text((256 * S, 216 * S), SUB, font=sub, fill=DIM)
out = pathlib.Path(__file__).resolve().parent / "nomad" / "logo.png"
img.resize((W, H), Image.LANCZOS).save(out)
print(out)
