"""Demo mode: the frame shows itself off, like a TV in a shop.

On a frame with no photos yet (once it's set up), or whenever "Demo mode" is
switched on, the frame runs a slideshow of sample photos (public-domain
paintings from the Art Institute of Chicago and NASA photographs; see
data/demo/credits.json) with collages and short "about this frame" slides
between them. Each sample carries a small credit and "Sample photo" tag, so
nobody mistakes it for their own.

The samples live with the app, not in the albums: they're never in the
slideshow history, the photo count or backups, and they disappear from the
screen as soon as the first real photo arrives.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

from . import screens
from .overlay import _glass

DEMO_DIR = Path(__file__).resolve().parent / "data" / "demo"
PHOTO_SECONDS, COLLAGE_SECONDS, SWAP_SECONDS, CARD_SECONDS = 20, 14, 9, 15


@dataclass
class Sample:
    path: Path
    title: str
    creator: str
    date: str
    size: tuple[int, int]

    @property
    def caption(self) -> str:
        who = self.creator + (f", {self.date}" if self.date and not self.creator.startswith("NASA") else "")
        return f"{self.title} · {who}"


def samples() -> list[Sample]:
    try:
        credits = json.loads((DEMO_DIR / "credits.json").read_text())
    except (OSError, ValueError):
        return []
    out = []
    for c in credits:
        p = DEMO_DIR / c["file"]
        if p.exists():
            out.append(Sample(p, c["title"], c["creator"], c.get("date", ""), tuple(c.get("size") or (1, 1))))
    return out


# What the info slides say: (title, text, with a QR code to the frame's page?)
CARDS = [
    ("This is Pi Frame", "A frame for your own photos and videos. Everything here is a sample: scan to add yours.", True),
    ("Photos from your phone", "Upload from the frame's page, or keep an album in step with a OneDrive, Google Drive, "
                               "Dropbox or NAS folder.", True),
    ("Clock, weather and stories", "The time, today's weather, and when and where each photo was taken, "
                                   "in a corner of your choosing.", False),
    ("Collages and Story mode", "A few photos together, then each one in turn. Videos play too, with photos "
                                "around them when they don't fill the screen.", False),
    ("Looks after itself", "Switches off at night or when the room's empty, backs itself up, and keeps "
                           "itself up to date.", False),
]


def info_card(size: tuple[int, int], title: str, text: str, url: str | None, backdrop: Image.Image) -> Image.Image:
    """A shop-window slide: a blurred, darkened sample photo, big words, and
    (for some) a QR code to the frame's page."""
    w, h = size
    img = ImageOps.fit(backdrop.convert("RGB"), size, Image.LANCZOS)
    img = img.filter(ImageFilter.GaussianBlur(max(8, min(w, h) // 30)))
    img = Image.blend(img, Image.new("RGB", size, screens.BG), 0.62)
    d = ImageDraw.Draw(img)
    unit = min(w, h)
    margin = int(unit * 0.08)
    screens.draw_wordmark(img, margin, margin, int(unit * 0.07))
    d = ImageDraw.Draw(img)
    portrait = h > w
    qr_side = int(unit * (0.34 if portrait else 0.36)) if url else 0
    text_w = w - 2 * margin - ((qr_side + margin) if url and not portrait else 0)
    title_f = screens._font("serif", int(unit * 0.085))
    body_f = screens._font("sans", int(unit * 0.04))
    lines_t = screens._wrap(d, title, title_f, text_w)
    lines_b = screens._wrap(d, text, body_f, text_w)
    block_h = len(lines_t) * int(title_f.size * 1.18) + int(unit * 0.03) + len(lines_b) * int(body_f.size * 1.45)
    if portrait and url:
        y = int(h * 0.24)
    else:
        y = max(int(margin * 2.2), (h - block_h) // 2)
    for line in lines_t:
        d.text((margin, y), line, font=title_f, fill=screens.INK)
        y += int(title_f.size * 1.18)
    y += int(unit * 0.03)
    for line in lines_b:
        d.text((margin, y), line, font=body_f, fill=screens.SOFT)
        y += int(body_f.size * 1.45)
    if url:
        pad = max(8, int(unit * 0.02))
        qr = screens._matted(screens.qr_image(url, qr_side), pad)
        if portrait:
            qx, qy = (w - qr.width) // 2, min(h - margin - qr.height, y + int(unit * 0.08))
        else:
            qx, qy = w - margin - qr.width, (h - qr.height) // 2
        img.paste(qr, (qx, qy))
        cap_f = screens._font("sans", int(unit * 0.028))
        cap = "Scan to add your photos"
        cw = d.textlength(cap, font=cap_f)
        d.text((qx + (qr.width - cw) / 2, qy + qr.height + int(unit * 0.02)), cap, font=cap_f, fill=screens.SOFT)
    return img


def draw_caption(img: Image.Image, text: str, overlay_corner: str = "bottom-left") -> Image.Image:
    """'Sample photo' and the credit, on frosted glass in the corner opposite the on-screen info."""
    img = img.copy()
    w, h = img.size
    unit = min(w, h)
    tag_f = screens._font("sans", max(11, int(unit * 0.02)))
    cap_f = screens._font("sans", max(11, int(unit * 0.022)))
    d = ImageDraw.Draw(img)
    margin = int(unit * 0.035)
    pad = int(unit * 0.016)
    max_w = int(w * 0.46)
    cap = text
    while d.textlength(cap, font=cap_f) > max_w and len(cap) > 12:
        cap = cap[:-2].rstrip() + "…" if not cap.endswith("…") else cap[:-3].rstrip() + "…"
    tag = "SAMPLE PHOTO"
    box_w = int(max(d.textlength(cap, font=cap_f), d.textlength(tag, font=tag_f))) + 2 * pad
    box_h = int(tag_f.size * 1.5 + cap_f.size * 1.3) + 2 * pad
    right = not overlay_corner.endswith("right")
    bottom = overlay_corner.startswith("bottom")
    x0 = w - margin - box_w if right else margin
    y0 = h - margin - box_h if bottom else margin
    _glass(img, (x0, y0, x0 + box_w, y0 + box_h), int(pad * 1.2))
    d = ImageDraw.Draw(img)
    d.text((x0 + pad, y0 + pad), tag, font=tag_f, fill=screens.SUN)
    d.text((x0 + pad, y0 + pad + int(tag_f.size * 1.5)), cap, font=cap_f, fill=screens.INK)
    return img


class Demo:
    """The sequence: photos, collages and info slides, arranged to show off every kind
    of transition: 3D turns between photos, a photo shrinking into a collage, prints
    swapped into it, and a photo growing out of it again."""

    # (what, how it arrives)
    SCRIPT = [
        ("card", "fade"),
        ("photo", "fade"),
        ("photo", "3d"),
        ("collage", "zoom-out"),  # a collage with the photo on screen in it: the photo shrinks into its space
        ("swap", "slide"),        # one of its photos replaced, like a print laid on top
        ("swap", "slide"),
        ("pick", "zoom-in"),      # one of its photos grows to fill the screen
        ("photo", "3d"),
        ("photo", "3d"),
        ("card", "fade"),
        ("photo", "fade"),
        ("collage", "zoom-out"),
        ("swap", "slide"),
        ("pick", "zoom-in"),
        ("photo", "3d"),
    ]

    def __init__(self):
        self.samples = samples()
        self._order: list[Sample] = []
        self._card = 0
        self._step = 0
        self._peeked = None
        self.photo: Sample | None = None          # the photo on screen, if it's a photo
        self.collage: list[Sample] | None = None  # the collage on screen (or just before the photo on screen)

    def available(self) -> bool:
        return len(self.samples) >= 3

    def _next_sample(self, skip: set | None = None) -> Sample:
        if not self._order:
            self._order = random.sample(self.samples, len(self.samples))
        for i, s in enumerate(self._order):
            if not skip or s.path not in skip:
                return self._order.pop(i)
        return self._order.pop(0)

    def peek(self) -> tuple[str, object, int, str]:
        """What next() will return, without moving on."""
        if self._peeked is None:
            self._peeked = self._make()
        return self._peeked

    def next(self) -> tuple[str, object, int, str]:
        """The next slide, as (what, item, seconds, how it arrives):
        ("photo", Sample, …) / ("collage", [Sample], …) / ("card", (title, text, qr), …) /
        ("swap", (space, Sample), …): the collage's photo `space` replaced by Sample."""
        step, self._peeked = self.peek(), None
        kind, item = step[0], step[1]
        if kind == "photo":
            self.photo = item
        elif kind == "collage":
            self.photo, self.collage = None, list(item)
        elif kind == "swap":
            self.collage[item[0]] = item[1]
        else:
            self.photo = self.collage = None
        return step

    def _make(self) -> tuple[str, object, int, str]:
        kind, how = self.SCRIPT[self._step % len(self.SCRIPT)]
        self._step += 1
        if kind == "card":
            c = CARDS[self._card % len(CARDS)]
            self._card += 1
            return "card", c, CARD_SECONDS, "fade"
        if kind == "collage":
            n = random.choice((3, 4, 5))
            chosen: list[Sample] = [self.photo] if self.photo else []
            while len(chosen) < n:
                chosen.append(self._next_sample({s.path for s in chosen}))
            random.shuffle(chosen)
            return "collage", chosen, COLLAGE_SECONDS, how if self.photo else "fade"
        if kind == "swap" and self.collage and not self.photo:
            space = (self._step * 7) % len(self.collage)
            return "swap", (space, self._next_sample({s.path for s in self.collage})), SWAP_SECONDS, how
        if kind == "pick" and self.collage and not self.photo:
            return "photo", random.choice(self.collage), PHOTO_SECONDS, how
        if kind in ("swap", "pick"):  # nothing to swap into: just a photo
            how = "fade"
        skip = {self.photo.path} if self.photo else set()
        return "photo", self._next_sample(skip), PHOTO_SECONDS, how if self.photo else "fade"
