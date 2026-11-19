"""Photos around a video that doesn't fill the screen.

A landscape video on a portrait screen leaves bands above and below it; a
portrait (phone) video on a landscape screen leaves tall spaces at the sides.
Those spaces get photos, chosen to suit their shape, with the same dark mat as
the collages. The video plays in its window (mpv's margin options) and the
photos sit on mpv's overlay layer, a picture with a transparent window, which
the graphics chip draws with each video frame at almost no cost.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageOps

from .collage import BACKGROUND

MIN_BAR = 0.12  # a space must be at least this share of the screen to be worth filling


def plan(screen: tuple[int, int], video: tuple[int, int]):
    """Where the video goes (fitted, centred) and the spaces around it.
    Returns (window, spaces); spaces is empty when there's nothing worth filling."""
    W, H = screen
    vw, vh = video
    s = min(W / vw, H / vh)
    dw, dh = round(vw * s), round(vh * s)
    x0, y0 = (W - dw) // 2, (H - dh) // 2
    window = (x0, y0, x0 + dw, y0 + dh)
    if y0 >= H * MIN_BAR:  # bands above and below
        return window, [(0, 0, W, y0), (0, y0 + dh, W, H)]
    if x0 >= W * MIN_BAR:  # spaces at the sides
        return window, [(0, 0, x0, H), (x0 + dw, 0, W, H)]
    return window, []


def pick(spaces, candidates: list[tuple[object, float]]) -> list:
    """For each space, the candidate (item, aspect) whose shape suits it best; all different."""
    chosen, left = [], list(candidates)
    for x0, y0, x1, y1 in spaces:
        want = (x1 - x0) / max(1, y1 - y0)
        if not left:
            break
        best = min(left, key=lambda c: abs(math.log(want / c[1])))
        chosen.append(best[0])
        left.remove(best)
    return chosen


def compose(screen: tuple[int, int], window, spaces, photos: list[Image.Image]) -> Image.Image:
    """The surround picture: photos in the spaces, a dark mat around them, and
    a fully transparent window where the video shows through."""
    W, H = screen
    gap = max(4, int(min(W, H) * 0.012))
    img = Image.new("RGBA", (W, H), BACKGROUND + (255,))
    for (x0, y0, x1, y1), photo in zip(spaces, photos):
        box = (x0 + gap, y0 + gap, x1 - gap, y1 - gap)
        size = (max(1, box[2] - box[0]), max(1, box[3] - box[1]))
        img.paste(ImageOps.fit(photo.convert("RGB"), size, Image.LANCZOS, centering=(0.5, 0.4)), box[:2])
    ImageDraw.Draw(img).rectangle([window[0], window[1], window[2] - 1, window[3] - 1], fill=(0, 0, 0, 0))
    return img


def margins(window_on_panel, panel: tuple[int, int]) -> dict:
    """mpv's margin ratios that put the video exactly in the window (panel coordinates)."""
    x0, y0, x1, y1 = window_on_panel
    W, H = panel
    return {"left": x0 / W, "right": (W - x1) / W, "top": y0 / H, "bottom": (H - y1) / H}
