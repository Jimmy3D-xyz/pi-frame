"""Draws photoframe/static/wiring.png: how to wire the three buttons.
Run after changing the default pins: python3 deploy/make_wiring.py"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from photoframe.screens import _font  # noqa: E402

W, H = 1100, 840
BG, INK, SOFT = (247, 248, 246), (34, 48, 47), (92, 106, 104)
GND = (45, 45, 45)
SCREEN, PREV, NEXT = (226, 140, 40), (59, 130, 196), (46, 160, 90)
EVEN = {2: "5V", 4: "5V", 6: "GND", 8: "GPIO14", 10: "GPIO15", 12: "GPIO18", 14: "GND", 16: "GPIO23"}
ODD = {1: "3V3", 3: "GPIO2", 5: "GPIO3", 7: "GPIO4", 9: "GND", 11: "GPIO17", 13: "GPIO27", 15: "GPIO22"}
USED = {5: SCREEN, 9: GND, 11: PREV, 13: NEXT}

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)
title, body, small, tiny, num = (_font("serif", 34), _font("sans", 21), _font("sans", 17), _font("sans", 14),
                                 _font("sans", 15))
d.text((40, 28), "Wiring the buttons", font=title, fill=INK)
d.text((40, 76), "Each button joins a GPIO pin to ground (GND). No resistors needed.", font=body, fill=SOFT)

# ---- header, as seen on the Pi with the USB ports to the right: pin 1 at the left, odd pins on the inner row
hx0, hy0, pitch = 200, 170, 90
col_x = lambda c: hx0 + 45 + c * pitch
top_y, bot_y = hy0 + 40, hy0 + 110
d.text((hx0, hy0 - 32), "Pi header (pins 1–16; the rest aren't used)", font=small, fill=SOFT)
d.rounded_rectangle([hx0, hy0, hx0 + 8 * pitch, hy0 + 150], radius=12, fill=(30, 36, 35))
d.text((hx0 + 8 * pitch + 14, hy0 + 62), "→ USB ports\n   this way", font=tiny, fill=SOFT)
pin_xy = {}
for c in range(8):
    for n, y, names in ((2 * c + 2, top_y, EVEN), (2 * c + 1, bot_y, ODD)):
        x = col_x(c)
        pin_xy[n] = (x, y)
        colour = USED.get(n)
        if colour:
            d.ellipse([x - 19, y - 19, x + 19, y + 19], fill=colour, outline=(255, 255, 255), width=3)
        else:
            d.rounded_rectangle([x - 16, y - 16, x + 16, y + 16], radius=4, fill=(120, 124, 118))
        tw = d.textlength(str(n), font=num)
        d.text((x - tw / 2, y - 9), str(n), font=num, fill=(255, 255, 255))
        if n % 2 == 0:  # even-pin names above the header
            tw = d.textlength(names[n], font=tiny)
            d.text((x - tw / 2, hy0 - 4 - 16 + 0), "", font=tiny)
# ---- buttons
by = 600
btn = {"screen": (250, SCREEN, "Screen / power", ["tap: screen off / on", "hold 5 s: shut down"]),
       "prev": (710, PREV, "Previous", ["previous photo"]),
       "next": (930, NEXT, "Next", ["next photo", "(stops a video)"])}
for key, (bx, colour, name, lines) in btn.items():
    d.ellipse([bx - 36, by - 36, bx + 36, by + 36], fill=(226, 229, 225), outline=(190, 196, 192), width=3)
    d.ellipse([bx - 24, by - 24, bx + 24, by + 24], fill=colour)
    tw = d.textlength(name, font=body)
    d.text((bx - tw / 2, by + 46), name, font=body, fill=INK)
    for i, l in enumerate(lines):
        tw = d.textlength(l, font=small)
        d.text((bx - tw / 2, by + 76 + i * 22), l, font=small, fill=SOFT)

def path(points, colour):
    d.line(points, fill=colour, width=6, joint="curve")
    for p in points[1:-1]:
        d.ellipse([p[0] - 3, p[1] - 3, p[0] + 3, p[1] + 3], fill=colour)

pin_bottom = lambda n: (pin_xy[n][0], pin_xy[n][1] + 19)
label_gap = hy0 + 180  # wires start below the pin names
# signal wires (each button's left leg)
path([(250 - 14, by - 36), (236, 400), (pin_xy[5][0], 400), pin_bottom(5)], SCREEN)
path([(710 - 14, by - 36), (696, 430), (pin_xy[11][0], 430), pin_bottom(11)], PREV)
path([(930 - 14, by - 36), (916, 460), (pin_xy[13][0], 460), pin_bottom(13)], NEXT)
# shared ground (each button's right leg) along a bus below the buttons, up to pin 9
bus = by + 150
legs = {250: 100, 710: 92, 930: 92}  # each ground leg clears its label
for bx, off in legs.items():
    path([(bx + 20, by + 30), (bx + off, by + 30), (bx + off, bus)], GND)
path([(250 + 100, bus), (930 + 92, bus)], GND)
path([(pin_xy[9][0], bus), pin_bottom(9)], GND)
d.text((250 + 100, bus + 8), "one shared ground wire to pin 9", font=tiny, fill=SOFT)

for n, name in ODD.items():  # odd-pin names just below the header
    x = pin_xy[n][0]
    colour = USED.get(n)
    tw = d.textlength(name, font=tiny)
    d.rounded_rectangle([x - tw / 2 - 5, hy0 + 154, x + tw / 2 + 5, hy0 + 174], radius=5, fill=BG)
    d.text((x - tw / 2, hy0 + 156), name, font=tiny, fill=colour if colour and colour != GND else (INK if colour else SOFT))


d.text((40, H - 50), "Tip: female-to-female jumper wires push straight onto the header pins.", font=small, fill=SOFT)
d.text((40, H - 26), "With the screen button on GPIO 3, pressing it also turns a shut-down Pi back on.", font=small, fill=SOFT)
out = Path(__file__).resolve().parents[1] / "photoframe" / "static" / "wiring.png"
img.save(out, optimize=True)
print("wrote", out)
