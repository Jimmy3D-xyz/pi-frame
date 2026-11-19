"""The Pi Frame logo, defined once and rendered two ways: as SVG for the web
page (favicon, header) and with Pillow for the frame's own screens.

The mark is a little framed photo: a walnut frame, a paper mat, and in the
window a dusk landscape with a π standing on the hill in front of a low sun.
Geometry is in a 64x64 box.
"""

from __future__ import annotations

import math
from functools import lru_cache

from PIL import Image, ImageDraw

WALNUT = "#2b2622"
MAT = "#f4f1ea"
SKY_TOP = "#7fa497"
SKY_BOTTOM = "#cfd9c9"
SUN = "#e8b04a"
HILL = "#3f5a50"
HILL_FAR = "#5b7f74"
PI = "#fbf8f1"

# Scene content is drawn in "window units": a 38x38 box (0..38), then mapped
# into whichever window the variant uses.
def _arc(cx, cy, r, a0, a1, n=10):
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


def _scene():
    far = [(0, 28)] + [(i * 38 / 16, 26.0 - 2.4 * math.sin(i / 16 * math.pi * 1.15)) for i in range(17)] + [(38, 38), (0, 38)]
    near = [(i * 38 / 16, 31.5 - 3.0 * math.sin((i / 16) * math.pi)) for i in range(17)] + [(38, 38), (0, 38)]
    bar = [(3.2, 12.4), (5.4, 9.4), (28.6, 8.8), (28.6, 12.4)]
    left = [(9.0, 11.6), (13.0, 11.6), (12.0, 23.0), (10.9, 28.4), (6.4, 29.0), (8.2, 23.4)]
    right = [(19.6, 11.6), (23.4, 11.6), (23.4, 23.8), (24.4, 25.0), (26.6, 24.6), (27.6, 26.4),
             (24.8, 28.4), (21.2, 28.0), (19.6, 25.0)]
    return [
        ("circle", (32.2, 25.4, 5.4), SUN),
        ("poly", far, HILL_FAR),
        ("poly", near, HILL),
        ("poly", bar, PI),
        ("poly", left, PI),
        ("poly", right, PI),
    ]


VARIANTS = {
    # full: walnut frame, paper mat, bevelled window
    "full": {"frame": (0, 0, 64, 64, 11), "mat": (6, 6, 58, 58, 5), "window": (13, 13, 51, 51, 2), "bevel": True},
    # compact (favicons, <= 48 px): thin frame, no mat, big picture
    "compact": {"frame": (0, 0, 64, 64, 12), "mat": None, "window": (6, 6, 58, 58, 7), "bevel": False},
}


def shapes(variant: str = "full"):
    v = VARIANTS[variant]
    x0, y0, x1, y1, _ = v["window"]
    k = (x1 - x0) / 38
    out = [("rrect", v["frame"], WALNUT)]
    if v["mat"]:
        out.append(("rrect", v["mat"], MAT))
    out.append(("sky", v["window"], (SKY_TOP, SKY_BOTTOM)))
    for kind, geo, fill in _scene():
        if kind == "circle":
            cx, cy, r = geo
            out.append((kind, (x0 + cx * k, y0 + cy * k, r * k), fill))
        else:
            out.append((kind, [(x0 + x * k, y0 + y * k) for x, y in geo], fill))
    return out


# ---------------------------------------------------------------- SVG
def svg(size: int | None = None, title: str = "Pi Frame", variant: str = "full") -> str:
    v = VARIANTS[variant]
    WINDOW = v["window"]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"'
             + (f' width="{size}" height="{size}"' if size else "") + ' role="img">',
             f"<title>{title}</title>",
             '<defs><linearGradient id="pfsky" x1="0" y1="0" x2="0" y2="1">'
             f'<stop offset="0" stop-color="{SKY_TOP}"/><stop offset="1" stop-color="{SKY_BOTTOM}"/></linearGradient>'
             f'<clipPath id="pfwin"><rect x="{WINDOW[0]}" y="{WINDOW[1]}" width="{WINDOW[2] - WINDOW[0]}" '
             f'height="{WINDOW[3] - WINDOW[1]}" rx="{WINDOW[4]}"/></clipPath></defs>']
    inside = []
    for kind, geo, fill in shapes(variant):
        if kind == "rrect":
            x0, y0, x1, y1, r = geo
            parts.append(f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" rx="{r}" fill="{fill}"/>')
        elif kind == "sky":
            x0, y0, x1, y1, r = geo
            parts.append(f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" rx="{r}" fill="url(#pfsky)"/>')
        elif kind == "circle":
            cx, cy, r = geo
            inside.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}"/>')
        elif kind == "poly":
            pts = " ".join(f"{x:.2f},{y:.2f}" for x, y in geo)
            inside.append(f'<polygon points="{pts}" fill="{fill}"/>')
    parts.append('<g clip-path="url(#pfwin)">' + "".join(inside) + "</g>")
    # a hairline where the mat meets the window, like a bevelled mat cut
    x0, y0, x1, y1, r = WINDOW
    if v["bevel"]:
        parts.append(f'<rect x="{x0 - .6}" y="{y0 - .6}" width="{x1 - x0 + 1.2}" height="{y1 - y0 + 1.2}" rx="{r + .6}" '
                 'fill="none" stroke="#ffffff" stroke-opacity=".9" stroke-width="1.2"/>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- Pillow
def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


@lru_cache(maxsize=16)
def mark(size: int, variant: str | None = None) -> Image.Image:
    """The logo mark as an RGBA image, drawn at 4x then smoothed down.
    Small sizes use the compact variant automatically."""
    variant = variant or ("compact" if size <= 48 else "full")
    v = VARIANTS[variant]
    WINDOW = v["window"]
    s = 4
    k = size * s / 64
    img = Image.new("RGBA", (size * s, size * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    win = Image.new("RGBA", img.size, (0, 0, 0, 0))
    wd = ImageDraw.Draw(win)
    for kind, geo, fill in shapes(variant):
        if kind == "rrect":
            x0, y0, x1, y1, r = geo
            d.rounded_rectangle([x0 * k, y0 * k, x1 * k - 1, y1 * k - 1], radius=r * k, fill=_hex(fill))
        elif kind == "sky":
            x0, y0, x1, y1, _ = geo
            top, bot = _hex(fill[0]), _hex(fill[1])
            for y in range(int(y0 * k), int(y1 * k)):
                t = (y - y0 * k) / ((y1 - y0) * k)
                wd.line([(x0 * k, y), (x1 * k, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
        elif kind == "circle":
            cx, cy, r = geo
            wd.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=_hex(fill))
        elif kind == "poly":
            wd.polygon([(x * k, y * k) for x, y in geo], fill=_hex(fill))
    x0, y0, x1, y1, r = WINDOW
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([x0 * k, y0 * k, x1 * k - 1, y1 * k - 1], radius=r * k, fill=255)
    img.paste(win, (0, 0), Image.composite(win.getchannel("A"), Image.new("L", img.size, 0), mask))
    if v["bevel"]:
        d.rounded_rectangle([(x0 - .6) * k, (y0 - .6) * k, (x1 + .6) * k, (y1 + .6) * k], radius=(r + .6) * k,
                            outline=(255, 255, 255, 230), width=max(1, int(1.2 * k)))
    return img.resize((size, size), Image.LANCZOS)
