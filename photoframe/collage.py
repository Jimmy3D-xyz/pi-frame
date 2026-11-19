"""Collages of 3, 4 or 5 photos.

Layouts are defined for a landscape screen as fractions of the screen
(x0, y0, x1, y1); for a portrait screen they're turned on their side. For each
collage, every layout for that many photos is tried with every way of placing
the photos, and the one where the photos' shapes best match their spaces wins,
so portrait photos go in tall spaces and landscape photos in wide ones.
"""

from __future__ import annotations

import itertools
import math

from PIL import Image, ImageOps

BACKGROUND = (26, 25, 23)  # a dark walnut mat between the photos

LAYOUTS: dict[int, list[list[tuple[float, float, float, float]]]] = {
    3: [
        [(0, 0, .62, 1), (.62, 0, 1, .5), (.62, .5, 1, 1)],          # one big, two stacked
        [(0, 0, 1 / 3, 1), (1 / 3, 0, 2 / 3, 1), (2 / 3, 0, 1, 1)],  # three tall
        [(0, 0, .5, .5), (0, .5, .5, 1), (.5, 0, 1, 1)],             # two stacked, one big
    ],
    4: [
        [(0, 0, .5, .5), (.5, 0, 1, .5), (0, .5, .5, 1), (.5, .5, 1, 1)],                 # 2 x 2
        [(0, 0, .6, 1), (.6, 0, 1, 1 / 3), (.6, 1 / 3, 1, 2 / 3), (.6, 2 / 3, 1, 1)],      # big + 3
        [(0, 0, .25, 1), (.25, 0, .5, 1), (.5, 0, .75, 1), (.75, 0, 1, 1)],                # four tall
    ],
    5: [
        [(0, 0, .5, 1), (.5, 0, .75, .5), (.75, 0, 1, .5), (.5, .5, .75, 1), (.75, .5, 1, 1)],  # big + 2x2
        [(0, 0, .5, .55), (.5, 0, 1, .55), (0, .55, 1 / 3, 1), (1 / 3, .55, 2 / 3, 1), (2 / 3, .55, 1, 1)],
        [(0, 0, 1 / 3, 1), (1 / 3, 0, 2 / 3, .5), (1 / 3, .5, 2 / 3, 1), (2 / 3, 0, 1, .5), (2 / 3, .5, 1, 1)],
    ],
}


def _cells(layout, size, gap, margin):
    """Fractional layout -> pixel boxes, with even gaps between photos."""
    w, h = size
    iw, ih = w - 2 * margin, h - 2 * margin
    boxes = []
    for x0, y0, x1, y1 in layout:
        bx0 = margin + x0 * iw + (gap / 2 if x0 > 0 else 0)
        by0 = margin + y0 * ih + (gap / 2 if y0 > 0 else 0)
        bx1 = margin + x1 * iw - (gap / 2 if x1 < 1 else 0)
        by1 = margin + y1 * ih - (gap / 2 if y1 < 1 else 0)
        boxes.append((round(bx0), round(by0), round(bx1), round(by1)))
    return boxes


def _transpose(layout):
    return [(y0, x0, y1, x1) for x0, y0, x1, y1 in layout]


def plan(aspects: list[float], size: tuple[int, int]):
    """Pick the layout and placement whose spaces best fit the photos' shapes.
    Returns (boxes, order) where order[i] is the photo for box i."""
    n = len(aspects)
    unit = min(size)
    gap = max(4, int(unit * 0.012))
    portrait = size[1] > size[0]
    best = None
    for layout in LAYOUTS[n]:
        lay = _transpose(layout) if portrait else layout
        boxes = _cells(lay, size, gap, gap)
        cell_aspects = [(b[2] - b[0]) / max(1, b[3] - b[1]) for b in boxes]
        for order in itertools.permutations(range(n)):
            # how much of each photo would be cropped away, summed (log scale)
            cost = sum(abs(math.log(cell_aspects[i] / aspects[order[i]])) for i in range(n))
            if best is None or cost < best[0]:
                best = (cost, boxes, order)
    return best[1], best[2]


def compose(images: list[Image.Image], size: tuple[int, int]) -> Image.Image:
    if len(images) not in LAYOUTS:
        raise ValueError(f"collages take 3, 4 or 5 photos, not {len(images)}")
    boxes, order = plan([im.width / im.height for im in images], size)
    canvas = Image.new("RGB", size, BACKGROUND)
    for box, idx in zip(boxes, order):
        tile = (box[2] - box[0], box[3] - box[1])
        canvas.paste(ImageOps.fit(images[idx], tile, Image.LANCZOS, centering=(0.5, 0.4)), box[:2])
    return canvas
