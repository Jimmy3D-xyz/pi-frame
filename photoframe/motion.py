"""Moving transitions: zooms between a collage and its photos, prints sliding
into a living collage, and 3D turns (card flip, tip away, cube) drawn ahead of time.

Zooms and slides are done by the display hardware (kms.py moves and scales
its top layer once per refresh), so they cost almost nothing. 3D turns can't
be: each frame is a perspective drawing that takes the Pi about a fifth of a
second. So they're drawn in the background while the photo before is on
screen, at about half the screen's resolution (the hardware scales them up),
and played back like a flip-book. If they aren't ready in time, it's a fade.

Geometry here works in "logical" coordinates (the upright picture people
see); rect_to_panel() turns a rectangle onto the physical panel, matching
render.to_panel().
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time

import numpy as np
from PIL import Image, ImageEnhance

log = logging.getLogger(__name__)

KINDS_3D = ("flip", "tip", "cube")
NAMES = {"flip": "3D card flip", "tip": "3D tip away", "cube": "3D cube turn"}
BACKDROP = (14, 14, 16)
FRAME_LONG_SIDE = 1280  # 3D frames are drawn this big; the hardware scales them up
FRAMES = 78             # the default: 1.3 seconds at 60 a second (frames_for() sets it for the speed chosen)

Rect = tuple[int, int, int, int]  # x, y, width, height


# ---------- where a photo sits ----------
def fit_crop(img_size, target, centering=(0.5, 0.5)) -> tuple[float, float, float, float]:
    """The part of a photo that ImageOps.fit(img, target, centering=…) keeps: (x0, y0, x1, y1)."""
    w, h = img_size
    ta = target[0] / max(1, target[1])
    if w / max(1, h) > ta:  # wider than the space: the sides go
        cw = h * ta
        x0 = (w - cw) * centering[0]
        return x0, 0.0, x0 + cw, float(h)
    ch = w / ta
    y0 = (h - ch) * centering[1]
    return 0.0, y0, float(w), y0 + ch


def photo_mapping(img_size, size, scale_mode: str) -> tuple[float, float, float]:
    """How render.place() puts a photo on the screen: screen = offset + scale * photo. Returns (ox, oy, scale)."""
    w, h = img_size
    W, H = size
    if scale_mode == "cover":
        x0, y0, x1, _ = fit_crop(img_size, size, (0.5, 0.45))
        s = W / max(1e-6, x1 - x0)
        return -x0 * s, -y0 * s, s
    s = min(W / w, H / h)
    return (W - w * s) / 2, (H - h * s) / 2, s


def tile_source(img_size, box, size, scale_mode: str) -> Rect:
    """Where the part of a photo shown in a collage space (box: x0, y0, x1, y1)
    is, once that photo fills the screen on its own. Zooms run between the two."""
    tile = (box[2] - box[0], box[3] - box[1])
    cx0, cy0, cx1, cy1 = fit_crop(img_size, tile, (0.5, 0.4))
    ox, oy, s = photo_mapping(img_size, size, scale_mode)
    x0, y0 = max(0.0, ox + cx0 * s), max(0.0, oy + cy0 * s)
    x1, y1 = min(size[0], ox + cx1 * s), min(size[1], oy + cy1 * s)
    return round(x0), round(y0), max(2, round(x1 - x0)), max(2, round(y1 - y0))


def box_rect(box) -> Rect:
    return box[0], box[1], box[2] - box[0], box[3] - box[1]


def panel_rotation(logical, panel, extra_rotation: int) -> int:
    """The clockwise turn render.to_panel() gives a logical picture."""
    rot = extra_rotation % 360
    if (logical[0] > logical[1]) != (panel[0] > panel[1]) and logical[0] != logical[1]:
        rot = (rot + 90) % 360
    return rot


def rect_to_panel(rect: Rect, logical, panel, extra_rotation: int) -> Rect:
    """A rectangle of the logical picture, on the physical panel."""
    x, y, w, h = rect
    W, H = logical
    rot = panel_rotation(logical, panel, extra_rotation)
    if rot == 90:
        x, y, w, h = H - (y + h), x, h, w
        W, H = H, W
    elif rot == 180:
        x, y = W - (x + w), H - (y + h)
    elif rot == 270:
        x, y, w, h = y, W - (x + w), h, w
        W, H = H, W
    sx, sy = panel[0] / W, panel[1] / H
    return round(x * sx), round(y * sy), max(2, round(w * sx)), max(2, round(h * sy))


def edge_to_panel(edge: str, logical, panel, extra_rotation: int) -> str:
    """Which edge of the panel a logical edge (left/right/top/bottom) ends up on."""
    order = ["top", "right", "bottom", "left"]  # clockwise
    rot = panel_rotation(logical, panel, extra_rotation)
    return order[(order.index(edge) + rot // 90) % 4]


# ---------- 3D turns ----------
def _coeffs(dst, src):
    """Perspective coefficients mapping output points dst to input points src."""
    m = []
    for (x, y), (u, v) in zip(dst, src):
        m.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        m.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    return tuple(np.linalg.solve(np.array(m, dtype=float), np.array(src, dtype=float).reshape(8)))


def _project(points, size, focal):
    """3D points (x, y, depth) to the screen, seen from straight in front."""
    cx, cy = size[0] / 2, size[1] / 2
    out = []
    for x, y, z in points:
        k = focal / (focal + z)
        out.append((cx + (x - cx) * k, cy + (y - cy) * k))
    return out


def _draw_face(canvas: Image.Image, img: Image.Image, quad, shade: float = 1.0, opacity: float = 1.0) -> None:
    """Draw a picture onto the canvas, stretched over a four-cornered shape (tl, tr, br, bl)."""
    w, h = img.size
    xs, ys = [p[0] for p in quad], [p[1] for p in quad]
    if max(xs) - min(xs) < 2 or max(ys) - min(ys) < 2:
        return
    if shade < 0.999:
        img = ImageEnhance.Brightness(img).enhance(max(0.0, shade))
    coeffs = _coeffs(quad, [(0, 0), (w, 0), (w, h), (0, h)])
    face = img.convert("RGBA").transform(canvas.size, Image.PERSPECTIVE, coeffs, Image.BILINEAR)
    mask = face.getchannel("A")
    if opacity < 0.999:
        mask = mask.point(lambda v, k=opacity: int(v * k))
    canvas.paste(face.convert("RGB"), (0, 0), mask)


def _ease(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


def frame_3d(kind: str, old: Image.Image, new: Image.Image, t: float) -> Image.Image:
    """One frame (t from 0 to 1) of a 3D turn from old to new (same size)."""
    w, h = old.size
    focal = 2.2 * max(w, h)
    e = _ease(t)
    if kind == "tip":  # the old photo tips over backwards, away from you, revealing the new one
        canvas = ImageEnhance.Brightness(new).enhance(0.55 + 0.45 * e)
        a = math.radians(84 * (e ** 1.15))
        top_y, depth = h - h * math.cos(a), h * math.sin(a)
        quad = _project([(0, top_y, depth), (w, top_y, depth), (w, h, 0), (0, h, 0)], (w, h), focal)
        _draw_face(canvas, old, quad, shade=1 - 0.45 * e, opacity=1 - max(0.0, (e - 0.55) / 0.45))
        return canvas
    canvas = Image.new("RGB", (w, h), BACKDROP)
    if kind == "flip":  # a card turning over: the new photo is on its back
        a = math.pi * e
        face, a = (old, a) if a <= math.pi / 2 else (new, a - math.pi)
        cos, sin = math.cos(a), math.sin(a)
        hw = w / 2
        lx, lz = w / 2 - hw * cos, -hw * sin  # left edge comes towards you
        rx, rz = w / 2 + hw * cos, hw * sin
        # the whole card also drops back a little mid-turn, so its near edge stays on screen
        push = 0.55 * w * math.sin(math.pi * e)
        quad = _project([(lx, 0, lz + push), (rx, 0, rz + push), (rx, h, rz + push), (lx, h, lz + push)],
                        (w, h), focal)
        _draw_face(canvas, face, quad, shade=1 - 0.35 * abs(sin))
        return canvas
    # cube: the old photo is the front of a box turning left, the new one its right side
    a = (math.pi / 2) * e
    half = w / 2
    centre_z = half
    faces = []
    for img, ang in ((old, -a), (new, math.pi / 2 - a)):
        c, s = math.cos(ang), math.sin(ang)
        # the face's two side edges, turned about the box's centre (x, depth)
        pts = []
        for u in (-half, half):
            x = w / 2 + u * c + half * s
            z = centre_z + u * s - half * c
            pts.append((x, z))
        (lx, lz), (rx, rz) = pts
        if rx - lx < 1:  # facing away
            continue
        push = 0.18 * w * math.sin(math.pi * e)
        quad = _project([(lx, 0, lz + push), (rx, 0, rz + push), (rx, h, rz + push), (lx, h, lz + push)],
                        (w, h), focal)
        faces.append(((lz + rz) / 2, img, quad, c))
    for _depth, img, quad, c in sorted(faces, key=lambda f: -f[0]):  # far side first
        _draw_face(canvas, img, quad, shade=0.55 + 0.45 * c)
    return canvas


def frames_for(ms: int, logical, panel, rate: int = 60) -> int:
    """Enough frames for a new one every screen refresh, as long as they fit
    comfortably in memory (at most a third of what's free)."""
    want = max(12, round(ms * rate / 1000))
    w, h = frame_size(logical, panel)
    try:
        with open("/proc/meminfo") as f:
            avail = next(int(line.split()[1]) * 1024 for line in f if line.startswith("MemAvailable:"))
        want = min(want, max(12, avail // 3 // (w * h * 4)))
    except (OSError, StopIteration, ValueError):
        pass
    return want


def frame_size(logical, panel) -> tuple[int, int]:
    """How big 3D frames are drawn (logical orientation): about half a 1440p screen."""
    k = min(1.0, FRAME_LONG_SIDE / max(logical))
    return max(2, round(logical[0] * k)), max(2, round(logical[1] * k))


class Turn:
    """A 3D turn drawn in the background. ready() once all frames are done;
    frames are BGRX bytes in the panel's orientation, `size` big."""

    def __init__(self, kind: str, old: Image.Image, new, logical, panel, extra_rotation: int,
                 key=None, frames: int | None = None, threads: int = 3, ms: int = 1300):
        """new: the next picture (logical size), or a function that makes it (run in the background
        too); either way it's kept as new_full, so it needn't be made twice."""
        self.kind, self.key, self.ms = kind, key, ms
        self.frames: list[bytes] = []
        self.size = (0, 0)
        self.new_full: Image.Image | None = None
        self.error: str | None = None
        self._done = threading.Event()
        self._cancel = False
        self.started = time.monotonic()
        self.seconds = 0.0
        self._job = (old.copy(), new, logical, panel, extra_rotation, frames or FRAMES, threads)
        threading.Thread(target=self._run, name=f"3d-{kind}", daemon=True).start()

    def _run(self) -> None:
        try:
            try:  # drawing is background work: let the slideshow and web page come first
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 12)
            except (OSError, AttributeError):
                pass
            old, new, logical, panel, extra, n, threads = self._job
            if callable(new):
                new = new()
            self.new_full = new
            fs = frame_size(logical, panel)
            old, new = old.convert("RGB").resize(fs, Image.BILINEAR), new.convert("RGB").resize(fs, Image.BILINEAR)
            rot = panel_rotation(logical, panel, extra)
            fw, fh = old.size
            psize = (fh, fw) if rot in (90, 270) else (fw, fh)

            def one(i):
                if self._cancel:
                    return b""
                img = frame_3d(self.kind, old, new, (i + 1) / (n + 1))
                if rot:
                    img = img.rotate(-rot, expand=True)
                return img.tobytes("raw", "BGRX")

            if threads > 1:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(threads, thread_name_prefix=f"3d-{self.kind}") as pool:
                    frames = list(pool.map(one, range(n)))
            else:
                frames = [one(i) for i in range(n)]
            if not self._cancel:
                self.frames, self.size = frames, psize
            self.seconds = time.monotonic() - self.started
            log.info("%s drawn ahead for the next picture: %d frames in %.1fs",
                     NAMES.get(self.kind, self.kind), len(self.frames), self.seconds)
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            log.warning("couldn't draw the 3D %s (%s); it'll be a fade", self.kind, e)
        finally:
            self._job = None
            self._done.set()

    def ready(self) -> bool:
        return self._done.is_set() and bool(self.frames)

    def wait(self, timeout: float) -> bool:
        self._done.wait(timeout)
        return self.ready()

    def cancel(self) -> None:
        self._cancel = True
        self.frames = []
