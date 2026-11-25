"""Drive the screen through the display hardware (DRM/KMS, via python3-kms++).

Two screen buffers: one showing on the bottom display layer, one hidden for
drawing the next picture. A plain change draws into the hidden buffer and flips
to it at the next screen refresh (never a half-drawn picture). A fade puts the
new picture on the top layer and raises that layer's opacity once per refresh:
the hardware blends the two layers as it sends the picture to the screen, so a
fade runs at the screen's full 60 fps with almost no work for the processor.

mpv also needs the display hardware, so the frame lets go of it while a video
plays (release) and takes it back afterwards (reacquire).

Same calls as deploy/kms_fade_test.py, which proved them on the Pi.
"""

from __future__ import annotations

import fcntl
import gc
import logging
import time
from pathlib import Path

import numpy as np
from PIL import Image

from .display import BaseDisplay, _ease


def _ease_out(t: float) -> float:
    """Fast, then settling gently: a print laid down on the table."""
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def _lerp(a, b, t: float):
    return tuple(x + (y - x) * t for x, y in zip(a, b))

log = logging.getLogger(__name__)

# hand control of the display hardware to another program (mpv) and take it back
DRM_IOCTL_SET_MASTER = 0x641E
DRM_IOCTL_DROP_MASTER = 0x641F


def _card_fd(card) -> int | None:
    fd = getattr(card, "fd", None)
    if callable(fd):
        fd = fd()
    return fd if isinstance(fd, int) else None


class KmsDisplay(BaseDisplay):
    def __init__(self, pykms=None, fb_size_file: str = "/sys/class/graphics/fb0/virtual_size"):
        super().__init__()
        if pykms is None:
            import pykms  # apt: python3-kms++
        self.k = pykms
        self._fb_size_file = fb_size_file
        self._released = False
        self._dropped_master = False
        self._held = None
        self._setup()
        from . import console

        if not console.set_graphics(True):
            log.info("console graphics mode not set by the frame (%s)", console.last_error or "unknown")

    # ---------- set-up ----------
    def _pick_mode(self):
        """The resolution the boot settings chose (the console's size), else the screen's best."""
        want = None
        try:
            want = tuple(int(x) for x in Path(self._fb_size_file).read_text().split(","))
        except (OSError, ValueError):
            pass
        if want:
            modes = list(self.conn.get_modes())
            for exact in (True, False):
                for m in modes:
                    if (m.hdisplay, m.vdisplay) == want and (not exact or round(m.vrefresh) == 60):
                        return m
        return self.conn.get_default_mode()

    def _setup(self) -> None:
        k = self.k
        self.card = k.Card()
        self.res = k.ResourceManager(self.card)
        self.conn = self.res.reserve_connector()
        self.crtc = self.res.reserve_crtc(self.conn)
        self.mode = self._pick_mode()
        self.size = (self.mode.hdisplay, self.mode.vdisplay)
        fmt = k.PixelFormat.XRGB8888
        self.primary = self.res.reserve_primary_plane(self.crtc, fmt)
        self.overlay = self.res.reserve_overlay_plane(self.crtc, fmt)
        if self.primary is None or self.overlay is None:
            raise RuntimeError("the display hardware has no free layers")
        w, h = self.size
        self.fbs = [k.DumbFramebuffer(self.card, w, h, fmt) for _ in range(2)]
        self.px = [self._pixels(fb) for fb in self.fbs]
        self.front = 0
        if self._current is not None:
            self._write(0, self._current)
        else:
            self.px[0][:] = 0
        self.crtc.set_mode(self.conn, self.fbs[0], self.mode)
        self.device = f"{self.conn.fullname} (display hardware)"
        log.info("display hardware: %s %dx%d @ %d Hz, fades on the top layer", self.conn.fullname, w, h,
                 round(self.mode.vrefresh))

    def _pixels(self, fb) -> np.ndarray:
        w, h = self.size
        return np.frombuffer(fb.map(0), dtype=np.uint8).reshape(h, fb.stride(0))[:, : w * 4]

    def _write(self, idx: int, img: Image.Image) -> None:
        w, h = self.size
        np.copyto(self.px[idx], np.frombuffer(img.tobytes("raw", "BGRX"), dtype=np.uint8).reshape(h, w * 4))

    def _plane_props(self, fb) -> dict:
        w, h = self.size
        return {"FB_ID": fb.id, "CRTC_ID": self.crtc.id, "SRC_X": 0, "SRC_Y": 0, "SRC_W": w << 16, "SRC_H": h << 16,
                "CRTC_X": 0, "CRTC_Y": 0, "CRTC_W": w, "CRTC_H": h}

    # ---------- showing pictures ----------
    def show(self, img: Image.Image, fade_ms: int = 0) -> None:
        if img.size != self.size:
            img = img.resize(self.size, Image.BILINEAR)
        with self._lock:
            if self._released:  # a video has the screen: remember it for afterwards
                self._current = img
                return
            back = 1 - self.front
            self._write(back, img)
            if fade_ms > 0 and self._current is not None and self.screen_on:
                self._hw_fade(back, fade_ms)
            else:
                r = self.k.AtomicReq(self.card)
                r.add(self.primary, {"FB_ID": self.fbs[back].id})
                r.commit_sync()  # flips at the next refresh: never a half-drawn picture
            self.front = back
            self._current = img

    def _blit(self, img: Image.Image) -> None:  # used by BaseDisplay.set_power after waking
        back = 1 - self.front
        self._write(back, img)
        r = self.k.AtomicReq(self.card)
        r.add(self.primary, {"FB_ID": self.fbs[back].id})
        r.commit_sync()
        self.front = back

    def _hw_fade(self, back: int, fade_ms: int, quiet: bool = False) -> None:
        k = self.k
        r = k.AtomicReq(self.card)
        props = self._plane_props(self.fbs[back])
        props["alpha"] = 0
        r.add(self.overlay, props)
        r.commit_sync(allow_modeset=True)
        start, frames = time.monotonic(), 0
        while True:
            t = (time.monotonic() - start) * 1000 / fade_ms
            if t >= 1.0:
                break
            r = k.AtomicReq(self.card)
            r.add(self.overlay, "alpha", int(65535 * _ease(t)))
            r.commit_sync()  # one step per screen refresh
            frames += 1
        # the new picture moves to the bottom layer and the top layer switches off, in one step
        r = k.AtomicReq(self.card)
        r.add(self.primary, self._plane_props(self.fbs[back]))
        r.add(self.overlay, {"FB_ID": 0, "CRTC_ID": 0})
        r.commit_sync()
        secs = time.monotonic() - start
        if frames and not quiet:
            log.info("fade: %d frames in %.2fs (%.1f fps, display hardware)", frames, secs, frames / secs)
            self.last_fade = {"frames": frames, "secs": round(secs, 2), "fps": round(frames / secs, 1),
                              "t": time.time(), "hardware": True}

    # ---------- moving transitions ----------
    motions = True

    def _layer_props(self, fb, src, dst, alpha: float = 1.0, fb_size=None) -> dict | None:
        """Top-layer settings showing the src part of fb at dst on screen, with whatever
        sticks out past the screen's edges cut off (the hardware wants it all on screen)."""
        W, H = self.size
        FW, FH = fb_size or self.size
        sx, sy, sw, sh = (float(v) for v in src)
        dx, dy, dw, dh = (float(v) for v in dst)
        if dw < 1 or dh < 1:
            return None
        kx, ky = sw / dw, sh / dh
        unscaled = abs(kx - 1) < 0.002 and abs(ky - 1) < 0.002
        if unscaled:
            # whole pixels throughout, so the hardware copies the picture as it is. A picture a
            # fraction of a pixel off its size gets scaled, and the scaler's last line reads past
            # the bottom of the picture: a white line along a slide's edge
            kx = ky = 1.0
            dx, dy, sx, sy = float(round(dx)), float(round(dy)), float(round(sx)), float(round(sy))
            dw = sw = float(round(sw))
            dh = sh = float(round(sh))
        x0, y0, x1, y1 = max(dx, 0.0), max(dy, 0.0), min(dx + dw, W), min(dy + dh, H)
        if x1 - x0 < 2 or y1 - y0 < 2:
            return None
        sx, sy = sx + (x0 - dx) * kx, sy + (y0 - dy) * ky
        sw, sh = (x1 - x0) * kx, (y1 - y0) * ky
        sx, sy = min(max(0.0, sx), FW - 1.0), min(max(0.0, sy), FH - 1.0)
        edge = 0.0 if unscaled else 1.0  # scaled: keep a pixel clear of the picture's edge, for the same reason
        sw, sh = max(1.0, min(sw, FW - edge - sx)), max(1.0, min(sh, FH - edge - sy))
        ix0, iy0 = round(x0), round(y0)
        return {"FB_ID": fb.id, "CRTC_ID": self.crtc.id,
                "SRC_X": int(sx * 65536), "SRC_Y": int(sy * 65536), "SRC_W": int(sw * 65536), "SRC_H": int(sh * 65536),
                "CRTC_X": ix0, "CRTC_Y": iy0, "CRTC_W": max(2, round(x1) - ix0), "CRTC_H": max(2, round(y1) - iy0),
                "alpha": int(65535 * min(1.0, max(0.0, alpha)))}

    def _move(self, name: str, fb, path, ms: int, first=None, fb_size=None) -> int:
        """Move the top layer along path(t) -> (src, dst, alpha), one step per screen refresh."""
        k = self.k
        start, frames, layer_on, began = time.monotonic(), 0, False, None
        while True:
            t = (time.monotonic() - start) * 1000 / max(1, ms)
            if t >= 1.0:
                break
            props = self._layer_props(fb, *path(t), fb_size=fb_size)
            if props is None and not layer_on and not (frames == 0 and first):
                time.sleep(1 / 60)  # still off the screen: nothing to show this refresh
                continue
            r = k.AtomicReq(self.card)
            r.add(self.overlay, props if props else {"FB_ID": 0, "CRTC_ID": 0})
            if frames == 0 and first:
                first(r)
            t0 = time.monotonic()
            r.commit_sync(allow_modeset=frames == 0)
            if time.monotonic() - t0 > 0.5:  # a refresh is 17 ms: the display hardware is struggling
                self.motions = False
                raise RuntimeError(f"the display hardware took {time.monotonic() - t0:.1f}s for one step; "
                                   "moves are off until the frame restarts")
            if began is None:
                began = time.monotonic()
            layer_on = props is not None
            frames += 1
        secs = time.monotonic() - (began or start)
        if frames:
            log.info("%s: %d frames in %.2fs (%.1f fps, display hardware)", name, frames, secs, frames / secs)
            self.last_fade = {"frames": frames, "secs": round(secs, 2), "fps": round(frames / secs, 1),
                              "t": time.time(), "hardware": True, "kind": name}
        return frames

    def _finish(self, back: int) -> None:
        """The new picture on the bottom layer, the top layer off, in one step."""
        r = self.k.AtomicReq(self.card)
        r.add(self.primary, self._plane_props(self.fbs[back]))
        r.add(self.overlay, {"FB_ID": 0, "CRTC_ID": 0})
        r.commit_sync(allow_modeset=True)

    def _moving(self, img: Image.Image, fade_ms: int, run) -> None:
        """Common steps: draw img into the hidden buffer, run the motion, end with img showing.
        Anything unusual (a video has the screen, it's off, the hardware objects) and it's
        a plain change or fade instead."""
        if img.size != self.size:
            img = img.resize(self.size, Image.BILINEAR)
        with self._lock:
            if self._released or self._current is None or not self.screen_on:
                pass
            else:
                back = 1 - self.front
                self._write(back, img)
                try:
                    run(back)
                    self._finish(back)
                    self.front = back
                    self._current = img
                    return
                except Exception as e:  # noqa: BLE001
                    log.warning("moving transition failed (%s); changing without it", e)
                    try:
                        self._finish(back)
                        self.front = back
                        self._current = img
                        return
                    except Exception:  # noqa: BLE001
                        pass
        self.show(img, fade_ms)

    # The top layer must never shrink a picture: shrinking makes the hardware read more
    # pixels than it shows, and with a full-screen picture underneath at 1440p that's
    # more than it can fetch in time (the screen stops: "flip_done timed out"). So a zoom
    # whose small end is smaller than its source part uses a copy of the picture shrunk
    # in advance, and the layer only ever enlarges. (The zoom's scale changes steadily
    # from one end to the other, so its smallest scale is at one end.)
    def _shrink_factor(self, src, dst) -> float:
        return min(1.0, dst[2] / max(1.0, src[2]), dst[3] / max(1.0, src[3]))

    def _shrunk(self, img: Image.Image, k: float):
        """A display buffer holding img shrunk by k (its own, reused while the size stays)."""
        w, h = self.size
        size = (max(2, int(w * k)), max(2, int(h * k)))
        if getattr(self, "_scratch", None) is None or self._scratch[0] != size:
            self._scratch = None
            fb = self.k.DumbFramebuffer(self.card, size[0], size[1], self.k.PixelFormat.XRGB8888)
            px = np.frombuffer(fb.map(0), dtype=np.uint8).reshape(size[1], fb.stride(0))[:, : size[0] * 4]
            self._scratch = (size, fb, px)
        size, fb, px = self._scratch
        small = img.resize(size, Image.BILINEAR)
        np.copyto(px, np.frombuffer(small.tobytes("raw", "BGRX"), dtype=np.uint8).reshape(size[1], size[0] * 4))
        return fb, size, size[0] / w

    def _zoom(self, name, picture, fb, src_a, dst_a, src_b, dst_b, ms, first=None):
        """Move the top layer from (src_a at dst_a) to (src_b at dst_b), enlarging only."""
        k = min(self._shrink_factor(src_a, dst_a), self._shrink_factor(src_b, dst_b))
        size = None
        if k < 0.995:
            fb, size, k = self._shrunk(picture, k * 0.97)
        else:
            k = 1.0

        def path(t):
            e = _ease(t)
            return tuple(v * k for v in _lerp(src_a, src_b, e)), _lerp(dst_a, dst_b, e), 1.0

        self._move(name, fb, path, ms, first=first, fb_size=size)

    def zoom_in(self, img, src, dst, ms, fade_ms=0):
        full = (0, 0, *self.size)
        if img.size != self.size:
            img = img.resize(self.size, Image.BILINEAR)
        if not self.motions:
            return self.show(img, fade_ms)
        self._moving(img, fade_ms, lambda back: self._zoom("zoom in", img, self.fbs[back], src, dst, full, full, ms))

    def zoom_out(self, img, src, dst, ms, fade_ms=0):
        full = (0, 0, *self.size)
        if not self.motions or self._current is None:
            return self.show(img, fade_ms)
        old_picture = self._current

        def run(back):
            # the old picture goes on the top layer while the new one slips in underneath
            self._zoom("zoom out", old_picture, self.fbs[self.front], full, full, src, dst, ms,
                       first=lambda r: r.add(self.primary, self._plane_props(self.fbs[back])))
        self._moving(img, fade_ms, run)

    def slide_in(self, img, rect, ms, fade_ms=0, edge=None):
        if not self.motions:
            return self.show(img, fade_ms)
        W, H = self.size
        x, y, w, h = rect
        # off the given edge, else the nearest one
        offs = {"left": (-(x + w), 0), "right": (W - x, 0), "top": (0, -(y + h)), "bottom": (0, H - y)}
        dist = {"left": x + w, "right": W - x, "top": y + h, "bottom": H - y}
        (ox, oy) = offs[edge] if edge in offs else offs[min(dist, key=dist.get)]

        def path(t):
            e = 1 - _ease_out(t)
            return rect, (x + ox * e, y + oy * e, w, h), 1.0 if edge else 0.35 + 0.65 * min(1.0, t * 2.5)

        self._moving(img, fade_ms, lambda back: self._move("slide", self.fbs[back], path, ms))

    def _small_fbs(self, size):
        if getattr(self, "_small", None) is None or self._small[0] != size:
            self._small = None
            fbs = [self.k.DumbFramebuffer(self.card, size[0], size[1], self.k.PixelFormat.XRGB8888) for _ in range(2)]
            self._small = (size, fbs, [np.frombuffer(fb.map(0), dtype=np.uint8).reshape(size[1], fb.stride(0))
                                       [:, : size[0] * 4] for fb in fbs])
        return self._small[1], self._small[2]

    def play(self, frames, frame_size, img, ms, fade_ms=0, name="3D turn"):
        if not frames or not self.motions:
            return self.show(img, fade_ms)
        fw, fh = frame_size
        W, H = self.size

        def run(back):
            fbs, px = self._small_fbs((fw, fh))
            n, shown, which = len(frames), -1, 0
            start, count = time.monotonic(), 0
            while True:
                t = (time.monotonic() - start) * 1000 / max(1, ms)
                if t >= 1.0:
                    break
                i = min(n - 1, int(t * n))
                if i == shown:
                    time.sleep(0.003)
                    continue
                np.copyto(px[which], np.frombuffer(frames[i], dtype=np.uint8).reshape(fh, fw * 4))
                r = self.k.AtomicReq(self.card)
                r.add(self.primary, {"FB_ID": fbs[which].id, "CRTC_ID": self.crtc.id, "SRC_X": 0, "SRC_Y": 0,
                                     "SRC_W": fw << 16, "SRC_H": fh << 16,
                                     "CRTC_X": 0, "CRTC_Y": 0, "CRTC_W": W, "CRTC_H": H})
                t0 = time.monotonic()
                r.commit_sync(allow_modeset=count == 0)
                if time.monotonic() - t0 > 0.5:
                    self.motions = False
                    raise RuntimeError("the display hardware took too long for one step; moves are off until the "
                                       "frame restarts")
                shown, which, count = i, 1 - which, count + 1
            secs = time.monotonic() - start
            log.info("%s played: %d of %d frames in %.2fs (%.1f a second), then the on-screen info faded in",
                     name, count, n, secs, count / secs)
            self.last_fade = {"frames": count, "secs": round(secs, 2), "fps": round(count / secs, 1),
                              "t": time.time(), "hardware": True, "kind": name}
            if fade_ms > 0:  # the on-screen info fades in over the last frame
                self._hw_fade(back, fade_ms, quiet=True)

        self._moving(img, fade_ms, run)

    def play_part(self, frames, frame_size, rect, img, ms, fade_ms=0, name="3D turn"):
        """Flip-book frames on the top layer over rect (enlarged to fit it, never shrunk),
        with the old picture still on the bottom layer around it; then img."""
        if not frames or not self.motions:
            return self.show(img, fade_ms)
        fw, fh = frame_size
        x, y, w, h = rect
        w, h = max(w, fw), max(h, fh)  # (never shrink: see _shrink_factor)
        W, H = self.size
        x, y = min(max(0, x), W - w), min(max(0, y), H - h)

        def run(back):
            fbs, px = self._small_fbs((fw, fh))
            n, shown, which = len(frames), -1, 0
            start, count = time.monotonic(), 0
            while True:
                t = (time.monotonic() - start) * 1000 / max(1, ms)
                if t >= 1.0:
                    break
                i = min(n - 1, int(t * n))
                if i == shown:
                    time.sleep(0.003)
                    continue
                np.copyto(px[which], np.frombuffer(frames[i], dtype=np.uint8).reshape(fh, fw * 4))
                r = self.k.AtomicReq(self.card)
                r.add(self.overlay, {"FB_ID": fbs[which].id, "CRTC_ID": self.crtc.id, "SRC_X": 0, "SRC_Y": 0,
                                     "SRC_W": fw << 16, "SRC_H": fh << 16,
                                     "CRTC_X": x, "CRTC_Y": y, "CRTC_W": w, "CRTC_H": h, "alpha": 65535})
                t0 = time.monotonic()
                r.commit_sync(allow_modeset=count == 0)
                if time.monotonic() - t0 > 0.5:
                    self.motions = False
                    raise RuntimeError("the display hardware took too long for one step; moves are off until the "
                                       "frame restarts")
                shown, which, count = i, 1 - which, count + 1
            secs = max(time.monotonic() - start, 1e-6)
            log.info("%s played: %d of %d frames in %.2fs (%.1f a second)", name, count, n, secs, count / secs)
            self.last_fade = {"frames": count, "secs": round(secs, 2), "fps": round(count / secs, 1),
                              "t": time.time(), "hardware": True, "kind": name}

        self._moving(img, fade_ms, run)

    def _power(self, on: bool) -> None:
        try:
            r = self.k.AtomicReq(self.card)
            r.add(self.crtc, "ACTIVE", 1 if on else 0)
            r.commit_sync(allow_modeset=True)
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't switch the screen %s through the display hardware (%s)", "on" if on else "off", e)

    # ---------- handing the screen to mpv and back ----------
    def video_mode(self) -> str | None:
        """The screen setting in use, for mpv's --drm-mode: its number in the screen's list
        (mpv reads the same list), so the video player keeps it rather than switching to
        the screen's favourite, which makes the screen blink."""
        try:
            m = self.mode
            key = lambda x: (x.hdisplay, x.vdisplay, x.clock, x.htotal, x.vtotal, x.flags)  # noqa: E731
            for i, x in enumerate(self.conn.get_modes()):
                if key(x) == key(m):
                    return str(i)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _hold(self, hold: dict) -> None:
        """Keep a picture on the top layer while the video player has the screen: the
        pictures around the video, see-through where the video goes. mpv draws on the bottom
        layer, so the black picture it starts with only shows in that space, for an instant,
        instead of over the whole screen."""
        w, h = self.size
        if tuple(hold["size"]) != (w, h):
            return
        data = Path(hold["file"]).read_bytes()  # premultiplied BGRA: the layer's own format
        fb = self.k.DumbFramebuffer(self.card, w, h, self.k.PixelFormat.ARGB8888)
        px = np.frombuffer(fb.map(0), dtype=np.uint8).reshape(h, fb.stride(0))[:, : w * 4]
        np.copyto(px, np.frombuffer(data, dtype=np.uint8).reshape(h, w * 4))
        r = self.k.AtomicReq(self.card)
        r.add(self.overlay, {**self._plane_props(fb), "alpha": 65535})
        r.commit_sync()
        self._held = fb  # kept until the screen comes back
        log.info("pictures around the video kept on the top layer")

    def release(self, hold: dict | None = None) -> None:
        """Let go of the display hardware so mpv can use it. hold: the pictures around the
        video (as for mpv's overlay), kept on the top layer meanwhile."""
        with self._lock:
            if self._released:
                return
            if hold:
                try:
                    self._hold(hold)
                except Exception as e:  # noqa: BLE001
                    log.warning("couldn't keep the pictures on the top layer (%s)", e)
            self._released = True
            try:
                fd = _card_fd(self.card)
                if fd is not None:  # step aside directly: immediate, unlike waiting for Python to close it
                    fcntl.ioctl(fd, DRM_IOCTL_DROP_MASTER)
                    self._dropped_master = True
                    log.info("screen handed to the video player (stepped aside on fd %d)", fd)
                    return
                if hasattr(self.card, "drop_master"):
                    self.card.drop_master()
                    self._dropped_master = True
                    return
            except Exception as e:  # noqa: BLE001
                log.warning("couldn't step aside for the video player (%s); closing the display hardware", e)
            self._teardown()

    def _teardown(self) -> None:
        self._small = self._scratch = None
        for name in ("px", "fbs", "overlay", "primary", "crtc", "conn", "res", "card"):
            if hasattr(self, name):
                delattr(self, name)
        gc.collect()  # closes the device, so mpv can take it

    def reacquire(self) -> None:
        """After the video: take the display hardware back and redraw."""
        with self._lock:
            if not self._released:
                return
            try:
                if self._dropped_master:
                    fd = _card_fd(self.card)
                    if fd is not None:
                        fcntl.ioctl(fd, DRM_IOCTL_SET_MASTER)
                    else:
                        self.card.set_master()
                    self._dropped_master = False
                    log.info("screen taken back from the video player")
                    w, h = self.size
                    if self._current is not None:
                        self._write(self.front, self._current)
                    self.crtc.set_mode(self.conn, self.fbs[self.front], self.mode)  # mpv changed the set-up
                    r = self.k.AtomicReq(self.card)
                    r.add(self.overlay, {"FB_ID": 0, "CRTC_ID": 0})
                    r.commit_sync(allow_modeset=True)
                    self._held = None
                else:
                    self._setup()
                self._released = False
                if not self.screen_on:
                    self._power(False)
            except Exception as e:  # noqa: BLE001
                log.warning("couldn't take the screen back after the video (%s); retrying", e)
                time.sleep(1)
                try:
                    self._teardown()
                    self._setup()
                    self._released = False
                except Exception as e2:  # noqa: BLE001
                    log.error("the display hardware is unavailable (%s)", e2)

    def info(self) -> dict:
        d = super().info()
        d["kind"] = "kms"
        return d


def available() -> tuple[bool, str]:
    try:
        import pykms  # noqa: F401
    except ImportError:
        return False, "python3-kms++ isn't installed"
    if not list(Path("/dev/dri").glob("card*")):
        return False, "no display hardware device"
    return True, ""
