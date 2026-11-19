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

    def _hw_fade(self, back: int, fade_ms: int) -> None:
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
        r.add(self.primary, {"FB_ID": self.fbs[back].id})
        r.add(self.overlay, {"FB_ID": 0, "CRTC_ID": 0})
        r.commit_sync()
        secs = time.monotonic() - start
        if frames:
            log.info("fade: %d frames in %.2fs (%.1f fps, display hardware)", frames, secs, frames / secs)
            self.last_fade = {"frames": frames, "secs": round(secs, 2), "fps": round(frames / secs, 1),
                              "t": time.time(), "hardware": True}

    def _power(self, on: bool) -> None:
        try:
            r = self.k.AtomicReq(self.card)
            r.add(self.crtc, "ACTIVE", 1 if on else 0)
            r.commit_sync(allow_modeset=True)
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't switch the screen %s through the display hardware (%s)", "on" if on else "off", e)

    # ---------- handing the screen to mpv and back ----------
    def release(self) -> None:
        """Let go of the display hardware so mpv can use it."""
        with self._lock:
            if self._released:
                return
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
