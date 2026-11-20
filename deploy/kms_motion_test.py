#!/usr/bin/env python3
"""Pi Frame: which fancier transitions can this Pi show smoothly?

Tries, on the real screen, each way the frame could animate a photo, and
reports the frame rate (60 fps is perfect):

  [3] zoom:   a photo grows from a spot in a collage to fill the screen
              (the display chip resizes it as it goes), then shrinks back
  [4] slide:  a photo slides in from the edge into its spot
  [5-7] flip-book: pre-drawn frames played back, as a 3D card flip would be,
              three ways: full resolution from display memory, full
              resolution copied in frame by frame, and 720p enlarged by the chip
  [8] how long the Pi takes to draw one frame of a 3D card flip

    scp kms_motion_test.py yourname@piframe.local:      (from the computer)
    sudo systemctl stop pi-photoframe                   (on the Pi: so nothing else draws)
    python3 kms_motion_test.py
    sudo systemctl start pi-photoframe

Takes about 2 minutes. Results are also saved to ~/kms_motion_test.log.
Ctrl+C stops it. It changes nothing permanently.
"""
import gc
import os
import sys
import time
import traceback
from pathlib import Path

LOG = os.path.expanduser("~/kms_motion_test.log")
open(LOG, "w").close()
RESULTS = []


def say(text=""):
    print(text, flush=True)
    with open(LOG, "a") as f:
        f.write(text + "\n")


def step(n, text):
    say(f"\n[{n}] {text}")


def trace():
    say("    " + traceback.format_exc().rstrip().replace("\n", "\n    "))


def result(name, frames, secs, extra=""):
    fps = frames / secs if secs else 0
    RESULTS.append((name, fps, extra))
    say(f"    {name}: {frames} frames in {secs:.2f} s = {fps:.1f} fps{('  ' + extra) if extra else ''}")
    return fps


def ease(t):
    return t * t * (3 - 2 * t)


try:
    import numpy as np
    from PIL import Image, ImageOps
    import pykms
except ImportError as e:
    sys.exit(f"Missing: {e}. Run: sudo apt install -y python3-kms++ python3-numpy python3-pil")


def meminfo():
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            out[k] = int(v.split()[0]) * 1024
    except OSError:
        pass
    return out


MB = 1024 * 1024

# ---------------------------------------------------------------- 1. this Pi
step(1, "This Pi")
mi = meminfo()
model = Path("/proc/device-tree/model").read_text().strip("\0\n") if Path("/proc/device-tree/model").exists() else "?"
say(f"    {model}")
say(f"    memory: {mi.get('MemTotal', 0) // MB} MB, {mi.get('MemAvailable', 0) // MB} MB available")
say(f"    display memory (CMA): {mi.get('CmaTotal', 0) // MB} MB, {mi.get('CmaFree', 0) // MB} MB free")

# ---------------------------------------------------------------- 2. the screen and a test picture
step(2, "Opening the display hardware")
try:
    card = pykms.Card()
    res = pykms.ResourceManager(card)
    conn = res.reserve_connector()
    crtc = res.reserve_crtc(conn)
    want = None
    try:
        want = tuple(int(x) for x in Path("/sys/class/graphics/fb0/virtual_size").read_text().split(","))
    except (OSError, ValueError):
        pass
    mode = next((m for m in conn.get_modes() if want and (m.hdisplay, m.vdisplay) == want and round(m.vrefresh) == 60),
                None) or conn.get_default_mode()
    W, H = mode.hdisplay, mode.vdisplay
    fmt = pykms.PixelFormat.XRGB8888
    primary = res.reserve_primary_plane(crtc, fmt)
    overlay = res.reserve_overlay_plane(crtc, fmt)
    say(f"    {conn.fullname}, {W}x{H} @ {round(mode.vrefresh)} Hz")
except Exception:
    trace()
    sys.exit("FAILED at step 2")

FRAME_BYTES = W * H * 4


def fb_pixels(fb, w, h):
    return np.frombuffer(fb.map(0), dtype=np.uint8).reshape(h, fb.stride(0))[:, : w * 4].reshape(h, w, 4)


def bgrx(img):
    return np.frombuffer(img.convert("RGB").tobytes("raw", "BGRX"), dtype=np.uint8).reshape(img.height, img.width, 4)


def load_picture():
    """A sample photo from Pi Frame (demo mode's), else a colourful pattern."""
    for d in (Path("/opt/pi-photoframe/photoframe/data/demo"), Path(__file__).resolve().parents[1] / "photoframe/data/demo"):
        pics = sorted(d.glob("*.jpg")) if d.is_dir() else []
        wide = [p for p in pics if (lambda im: im.width > im.height)(Image.open(p))]
        if wide:
            return Image.open(wide[0]).convert("RGB"), wide[0].name
    x = np.linspace(0, 255, W, dtype=np.uint8)[None, :].repeat(H, 0)
    y = np.linspace(0, 255, H, dtype=np.uint8)[:, None].repeat(W, 1)
    return Image.fromarray(np.dstack([x, y, 255 - x])), "a test pattern"


try:
    pic, pic_name = load_picture()
    photo = ImageOps.fit(pic, (W, H), Image.LANCZOS)
    say(f"    test picture: {pic_name}")
    fb_bg = pykms.DumbFramebuffer(card, W, H, fmt)
    fb_photo = pykms.DumbFramebuffer(card, W, H, fmt)
    fb_pixels(fb_bg, W, H)[:] = bgrx(photo.point(lambda v: v * 0.25))  # a dim "collage" behind
    fb_pixels(fb_photo, W, H)[:] = bgrx(photo)
    crtc.set_mode(conn, fb_bg, mode)
    time.sleep(1)
except Exception:
    trace()
    sys.exit("FAILED at step 2")


def show_overlay(fb, src, dst, alpha=65535, modeset=False):
    """Put fb's src rectangle (x, y, w, h, in pixels) on screen at dst (x, y, w, h)."""
    r = pykms.AtomicReq(card)
    sx, sy, sw, sh = src
    dx, dy, dw, dh = dst
    r.add(overlay, {"FB_ID": fb.id, "CRTC_ID": crtc.id,
                    "SRC_X": int(sx * 65536), "SRC_Y": int(sy * 65536),
                    "SRC_W": int(sw * 65536), "SRC_H": int(sh * 65536),
                    "CRTC_X": int(dx), "CRTC_Y": int(dy), "CRTC_W": max(1, int(dw)), "CRTC_H": max(1, int(dh)),
                    "alpha": alpha})
    r.commit_sync(allow_modeset=modeset)


def hide_overlay():
    r = pykms.AtomicReq(card)
    r.add(overlay, {"FB_ID": 0, "CRTC_ID": 0})
    r.commit_sync(allow_modeset=True)


def lerp(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


# a spot in a collage: the right-hand third, a bit taller than wide
tile = (W * 0.62, H * 0.08, W * 0.34, H * 0.62)
tile_aspect = tile[2] / tile[3]
crop_w = min(W, H * tile_aspect)
crop = ((W - crop_w) / 2, 0, crop_w, H)  # the part of the photo the spot shows
full = (0, 0, W, H)

# ---------------------------------------------------------------- 3. zoom
step(3, "Zoom: a photo grows from its spot in a collage to fill the screen, and back (3 times)")
zoom_fps = []
try:
    show_overlay(fb_photo, crop, tile, modeset=True)
    time.sleep(1)
    for rnd in range(3):
        for direction in ("in", "out"):
            start, frames = time.monotonic(), 0
            while True:
                t = (time.monotonic() - start) / 1.0
                if t >= 1:
                    break
                e = ease(t) if direction == "in" else 1 - ease(t)
                show_overlay(fb_photo, lerp(crop, full, e), lerp(tile, full, e))
                frames += 1
            zoom_fps.append(result(f"zoom {direction} {rnd + 1}", frames, time.monotonic() - start))
            time.sleep(1)
except KeyboardInterrupt:
    say("    stopped")
except Exception:
    trace()
    say("    FAILED: the display chip didn't accept a resized photo (zooms would need another way)")

# ---------------------------------------------------------------- 4. slide
step(4, "Slide: a photo slides in from the right into its spot, and out again (3 times)")
try:
    for rnd in range(3):
        for direction in ("in", "out"):
            start, frames = time.monotonic(), 0
            while True:
                t = (time.monotonic() - start) / 0.8
                if t >= 1:
                    break
                e = ease(t) if direction == "in" else 1 - ease(t)
                x = W + (tile[0] - W) * e  # from just off the right edge to the spot
                vis = max(1.0, min(tile[2], W - x))  # what's on screen; the rest is cut off
                src = (crop[0], crop[1], crop[2] * vis / tile[2], crop[3])
                show_overlay(fb_photo, src, (x, tile[1], vis, tile[3]))
                frames += 1
            result(f"slide {direction} {rnd + 1}", frames, time.monotonic() - start)
            time.sleep(0.6)
    hide_overlay()
except KeyboardInterrupt:
    say("    stopped")
except Exception:
    trace()
    say("    FAILED")


# ---------------------------------------------------------------- flip-book frames
def flip_frames(n, w, h, src_img):
    """n distinct frames: the photo panning sideways (stands in for a pre-drawn 3D flip)."""
    base = bgrx(ImageOps.fit(src_img, (w, h), Image.BILINEAR))
    step_px = max(1, w // (n * 3))
    return [np.roll(base, i * step_px, axis=1) for i in range(n)]


def show_primary(fb, w, h, modeset=False):
    r = pykms.AtomicReq(card)
    props = {"FB_ID": fb.id}
    if (w, h) != (W, H):  # enlarged by the display chip
        props.update({"CRTC_ID": crtc.id, "SRC_X": 0, "SRC_Y": 0, "SRC_W": w << 16, "SRC_H": h << 16,
                      "CRTC_X": 0, "CRTC_Y": 0, "CRTC_W": W, "CRTC_H": H})
    r.add(primary, props)
    r.commit_sync(allow_modeset=modeset)


N = 60  # one second at 60 fps

# ---------------------------------------------------------------- 5. flip-book from display memory
step(5, f"Flip-book, full resolution, every frame in display memory ({N} frames = {N * FRAME_BYTES // MB} MB)")
fbs = []
try:
    for i in range(N):
        try:
            fbs.append(pykms.DumbFramebuffer(card, W, H, fmt))
        except Exception:
            say(f"    display memory ran out after {len(fbs)} frames")
            break
    say(f"    {len(fbs)} frames fit; display memory now {meminfo().get('CmaFree', 0) // MB} MB free")
    if len(fbs) >= 10:
        for fb, frame in zip(fbs, flip_frames(len(fbs), W, H, pic)):
            fb_pixels(fb, W, H)[:] = frame
        for rnd in range(2):
            start = time.monotonic()
            for fb in fbs:
                show_primary(fb, W, H)
            result(f"display memory, round {rnd + 1}", len(fbs), time.monotonic() - start, f"({len(fbs)} frames)")
    else:
        result("display memory", 0, 1, "not enough display memory")
except KeyboardInterrupt:
    say("    stopped")
except Exception:
    trace()
finally:
    show_primary(fb_bg, W, H)
    del fbs
    gc.collect()

# ---------------------------------------------------------------- 6. flip-book copied in
need = N * FRAME_BYTES + 400 * MB
avail = meminfo().get("MemAvailable", 0)
n_copy = N if avail > need else max(10, int((avail - 400 * MB) // FRAME_BYTES))
step(6, f"Flip-book, full resolution, frames in ordinary memory, copied in one at a time ({n_copy} frames)")
try:
    frames = flip_frames(n_copy, W, H, pic)
    two = [pykms.DumbFramebuffer(card, W, H, fmt) for _ in range(2)]
    px = [fb_pixels(fb, W, H) for fb in two]
    for rnd in range(2):
        start, copy_t = time.monotonic(), 0.0
        for i, frame in enumerate(frames):
            c0 = time.monotonic()
            np.copyto(px[i % 2], frame)
            copy_t += time.monotonic() - c0
            show_primary(two[i % 2], W, H)
        result(f"copied in, round {rnd + 1}", len(frames), time.monotonic() - start,
               f"(copying took {copy_t / len(frames) * 1000:.1f} ms a frame)")
    show_primary(fb_bg, W, H)
    del frames, two, px
    gc.collect()
except KeyboardInterrupt:
    say("    stopped")
except Exception:
    trace()

# ---------------------------------------------------------------- 7. flip-book at 720p, enlarged
SW, SH = 1280, round(1280 * H / W)
step(7, f"Flip-book, {SW}x{SH} frames enlarged to the screen by the display chip ({N} frames)")
fbs = []
try:
    for _ in range(N):
        try:
            fbs.append(pykms.DumbFramebuffer(card, SW, SH, fmt))
        except Exception:
            break
    for fb, frame in zip(fbs, flip_frames(len(fbs), SW, SH, pic)):
        fb_pixels(fb, SW, SH)[:] = frame
    for rnd in range(2):
        start = time.monotonic()
        for fb in fbs:
            show_primary(fb, SW, SH)
        result(f"enlarged, round {rnd + 1}", len(fbs), time.monotonic() - start, f"({len(fbs)} frames)")
except KeyboardInterrupt:
    say("    stopped")
except Exception:
    trace()
    say("    FAILED: the display chip didn't enlarge the main layer")
finally:
    try:
        crtc.set_mode(conn, fb_bg, mode)
    except Exception:
        pass
    del fbs
    gc.collect()


# ---------------------------------------------------------------- 8. drawing a 3D flip frame
def find_coeffs(dst, src):
    """Perspective coefficients mapping output points dst to input points src."""
    m = []
    for (x, y), (u, v) in zip(dst, src):
        m.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        m.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    a = np.array(m, dtype=float)
    b = np.array(src, dtype=float).reshape(8)
    return np.linalg.solve(a, b)


def card_flip_frame(img, angle_deg):
    w, h = img.size
    a = np.radians(angle_deg)
    half = w / 2 * abs(np.cos(a))
    lean = h * 0.12 * np.sin(a)  # the nearer edge looks taller
    dst = [(w / 2 - half, -lean), (w / 2 + half, lean), (w / 2 + half, h - lean), (w / 2 - half, h + lean)]
    coeffs = find_coeffs(dst, [(0, 0), (w, 0), (w, h), (0, h)])
    out = Image.new("RGB", (w, h), (20, 20, 22))
    out.paste(img.transform((w, h), Image.PERSPECTIVE, tuple(coeffs), Image.BICUBIC, fillcolor=(20, 20, 22)))
    return out


step(8, "Drawing one frame of a 3D card flip (the Pi would do this in advance)")
try:
    for (w, h), label in (((W, H), "full resolution"), ((SW, SH), f"{SW}x{SH}")):
        img = ImageOps.fit(pic, (w, h), Image.BILINEAR)
        times = []
        for ang in (20, 45, 70):
            t0 = time.monotonic()
            card_flip_frame(img, ang)
            times.append(time.monotonic() - t0)
        ms = sorted(times)[1] * 1000
        say(f"    {label}: {ms:.0f} ms a frame, so about {ms * 60 / 1000:.0f} s to draw a one-second flip")
except Exception:
    trace()

# ---------------------------------------------------------------- summary
say("\n=== Summary")
for name, fps, extra in RESULTS:
    mark = "smooth" if fps >= 57 else "ok" if fps >= 45 else "not smooth" if fps > 0 else "didn't work"
    say(f"    {name:34s} {fps:5.1f} fps   {mark}")
say("\nDone. Start the frame again with:  sudo systemctl start pi-photoframe")
say(f"(Results saved to {LOG}.)")
