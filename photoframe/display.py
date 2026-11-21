"""Display backends.

FramebufferDisplay drives the HDMI screen through /dev/fb0, which Raspberry Pi
OS provides on top of the KMS driver. No X11/Wayland desktop is needed, which
keeps the Pi Zero 2 W's 512 MB free for image work.

FileDisplay writes PNGs instead and is used for development and tests.
"""

from __future__ import annotations

import fcntl
import logging
import mmap
import os
import struct
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

FBIOGET_VSCREENINFO = 0x4600
FBIOBLANK = 0x4611
FBIO_WAITFORVSYNC = 0x40044620
FB_BLANK_UNBLANK = 0
FB_BLANK_POWERDOWN = 4


class BaseDisplay:
    size: tuple[int, int]

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Image.Image | None = None
        self.screen_on = True
        self.last_fade: dict | None = None
        # Optional extra commands (e.g. HDMI-CEC standby for monitors that
        # support it). Set in the systemd unit, never over the network.
        self._off_cmd = os.environ.get("PHOTOFRAME_SCREEN_OFF_CMD", "")
        self._on_cmd = os.environ.get("PHOTOFRAME_SCREEN_ON_CMD", "")

    # subclasses implement _blit(img) and _power(on)
    def _blit(self, img: Image.Image) -> None:
        raise NotImplementedError

    def _power(self, on: bool) -> None:
        pass

    def show(self, img: Image.Image, fade_ms: int = 0) -> None:
        """Show a panel-sized RGB image, optionally cross-fading from the last one."""
        if img.size != self.size:
            img = img.resize(self.size, Image.BILINEAR)
        with self._lock:
            prev = self._current
            if fade_ms > 0 and prev is not None and self.screen_on:
                self._fade(prev, img, fade_ms)
            self._blit(img)
            self._current = img

    # ---------- moving transitions (kms.py does them on the display hardware) ----------
    # Rectangles are (x, y, width, height) on the panel. Screens that can't move
    # pictures around smoothly just fade.
    motions = False

    def zoom_in(self, img: Image.Image, src, dst, ms: int, fade_ms: int = 0) -> None:
        """The new picture grows from `dst` on screen (showing its `src` part) to fill the screen."""
        self.show(img, fade_ms)

    def zoom_out(self, img: Image.Image, src, dst, ms: int, fade_ms: int = 0) -> None:
        """The picture on screen (its `src` part) shrinks into `dst` of the new one."""
        self.show(img, fade_ms)

    def slide_in(self, img: Image.Image, rect, ms: int, fade_ms: int = 0, edge: str | None = None) -> None:
        """The `rect` part of the new picture slides in from the nearest edge, like a print laid down."""
        self.show(img, fade_ms)

    def play(self, frames: list[bytes], frame_size, img: Image.Image, ms: int, fade_ms: int = 0,
             name: str = "3D turn") -> None:
        """Play drawn-ahead frames (BGRX bytes, frame_size big) like a flip-book, then show img."""
        self.show(img, fade_ms)

    def play_part(self, frames: list[bytes], frame_size, rect, img: Image.Image, ms: int, fade_ms: int = 0,
                  name: str = "3D turn") -> None:
        """Like play(), with the frames covering just `rect` of the screen (one photo of a collage)."""
        self.show(img, fade_ms)

    def _fade(self, a: Image.Image, b: Image.Image, fade_ms: int) -> None:
        # Blend in RGB space. Step count adapts to how fast this Pi manages a frame.
        start = time.monotonic()
        arr_a = np.asarray(a, dtype=np.uint16)
        arr_b = np.asarray(b, dtype=np.uint16)
        t = 0.0
        while t < 1.0:
            alpha = int(t * 256)
            frame = ((arr_a * (256 - alpha) + arr_b * alpha) >> 8).astype(np.uint8)
            self._blit(Image.fromarray(frame, "RGB"))
            t = (time.monotonic() - start) * 1000 / fade_ms

    def info(self) -> dict:
        return {"kind": type(self).__name__.replace("Display", "").lower(), "size": list(self.size),
                "bpp": getattr(self, "bpp", None), "device": getattr(self, "device", None),
                "screen_on": self.screen_on, "last_fade": self.last_fade}

    def set_power(self, on: bool) -> None:
        with self._lock:
            if on == self.screen_on:
                return
            cmd = self._on_cmd if on else self._off_cmd
            self._power(on)
            if cmd:
                try:
                    subprocess.run(cmd, shell=True, timeout=15, check=False)
                except Exception as e:  # noqa: BLE001
                    log.warning("screen %s command failed: %s", "on" if on else "off", e)
            self.screen_on = on
            if on and self._current is not None:
                self._blit(self._current)  # some drivers lose the buffer while blanked


def _ease(t: float) -> float:
    """Smoothstep: starts and ends gently, which reads as smoother than linear."""
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


class FramebufferDisplay(BaseDisplay):
    def __init__(self, device: str = "/dev/fb0", geometry: tuple[int, int, int, int, int] | None = None):
        """geometry=(width, height, bpp, stride, red_offset) skips probing (tests)."""
        super().__init__()
        self.device = device
        self._fd = os.open(device, os.O_RDWR)
        if geometry is None:
            vinfo = bytearray(160)
            fcntl.ioctl(self._fd, FBIOGET_VSCREENINFO, vinfo)
            xres, yres, _xv, _yv, _xo, _yo, bpp = struct.unpack_from("7I", vinfo, 0)
            red_offset = struct.unpack_from("I", vinfo, 32)[0]
            try:
                stride = int((Path("/sys/class/graphics") / Path(device).name / "stride").read_text().strip())
            except OSError:
                stride = xres * bpp // 8
        else:
            xres, yres, bpp, stride, red_offset = geometry
        if bpp not in (16, 32):
            raise RuntimeError(f"unsupported framebuffer depth {bpp} bpp")
        self.size = (xres, yres)
        self.bpp = bpp
        self.stride = stride
        self._rawmode = "RGB565" if bpp == 16 else ("BGRX" if red_offset == 16 else "RGBX")
        # Map the framebuffer so frames are copied straight into video memory.
        self._mm = mmap.mmap(self._fd, stride * yres, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        row_bytes = xres * bpp // 8
        self._view = np.frombuffer(self._mm, dtype=np.uint8).reshape(yres, stride)[:, :row_bytes]
        self._vsync = True
        self._native_current: np.ndarray | None = None
        # fades are split into horizontal bands, one per core (numpy lets the threads run in parallel)
        self._workers = max(1, min(4, os.cpu_count() or 1))
        self._pool = None
        if self._workers > 1:
            from concurrent.futures import ThreadPoolExecutor

            self._pool = ThreadPoolExecutor(self._workers, thread_name_prefix="fade")
        log.info("framebuffer %s: %dx%d %dbpp stride=%d (%s)", device, xres, yres, bpp, stride, self._rawmode)
        # stop the kernel's console drawing text (driver warnings and the like) over the photos
        from . import console

        if geometry is None and not console.set_graphics(True):
            log.warning("couldn't put the console in graphics mode, so kernel messages may appear over photos "
                        "(%s; re-run install.sh)", console.last_error or "unknown reason")
        if bpp == 16:
            log.info("16-bit framebuffer: photos use 65k colours; see README for switching to 32-bit")

    # ---------- pixel formats ----------
    def _pack565(self, rgb: np.ndarray, out: np.ndarray | None = None) -> np.ndarray:
        r = rgb[..., 0].astype(np.uint16)
        g = rgb[..., 1].astype(np.uint16)
        b = rgb[..., 2].astype(np.uint16)
        px = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        if out is not None:
            np.copyto(out, px)
            return out
        return px

    def _native(self, img: Image.Image) -> np.ndarray:
        """Image -> array in the framebuffer's own byte layout, shape (h, row_bytes)."""
        h = self.size[1]
        if self._rawmode == "RGB565":
            px = self._pack565(np.asarray(img)).astype("<u2")
            return px.view(np.uint8).reshape(h, -1)
        return np.frombuffer(img.tobytes("raw", self._rawmode), dtype=np.uint8).reshape(h, -1)

    # ---------- output ----------
    def _wait_vsync(self) -> None:
        if not self._vsync:
            return
        try:
            fcntl.ioctl(self._fd, FBIO_WAITFORVSYNC, struct.pack("I", 0))
        except OSError:
            self._vsync = False  # driver doesn't support it; carry on without

    def _put(self, native: np.ndarray) -> None:
        self._wait_vsync()
        np.copyto(self._view, native)

    def _blit(self, img: Image.Image) -> None:
        native = self._native(img)
        self._put(native)
        self._native_current = native

    def _fade(self, a: Image.Image, b: Image.Image, fade_ms: int) -> None:
        """Cross-fade with preallocated buffers and fixed-point maths."""
        start = time.monotonic()
        if self._rawmode == "RGB565":
            frames = self._fade_565(a, b, fade_ms, start)
        else:
            frames = self._fade_32(a, b, fade_ms, start)
        secs = time.monotonic() - start
        if frames:
            log.info("fade: %d frames in %.2fs (%.1f fps)", frames, secs, frames / secs)
            self.last_fade = {"frames": frames, "secs": round(secs, 2), "fps": round(frames / secs, 1), "t": time.time()}

    def _fade_32(self, a: Image.Image, b: Image.Image, fade_ms: int, start: float) -> int:
        """32-bit screens: blend straight into the screen's memory, in three
        passes per frame (old*128 + (new-old)*alpha, then >>7 as bytes), split
        into horizontal bands across the Pi's cores."""
        cur = self._native_current if self._native_current is not None else self._native(a)
        rows = cur.shape[0]
        base7 = cur.astype(np.int16) << 7          # old picture x 128 (fits: 255 x 128 = 32640)
        diff = self._native(b).astype(np.int16)
        np.subtract(diff, cur, out=diff, casting="unsafe")  # new - old
        work = np.empty_like(base7)
        n = self._workers
        bands = [slice(i * rows // n, (i + 1) * rows // n) for i in range(n)]
        view = self._view
        refresh = 1 / 60

        def band(sl: slice, alpha: int) -> None:
            w = work[sl]
            np.multiply(diff[sl], alpha, out=w)
            np.add(w, base7[sl], out=w)          # = old*(128-alpha) + new*alpha, always 0..32640
            np.right_shift(w, 7, out=view[sl], casting="unsafe")

        frames, last = 0, None
        while True:
            now = time.monotonic()
            t = (now - start) * 1000 / fade_ms
            if t >= 1.0:
                break
            alpha = int(_ease(t) * 128)
            # waiting for the next screen refresh only helps when frames are quicker than refreshes
            if last is not None and now - last < 1.5 * refresh:
                self._wait_vsync()
            last = time.monotonic()
            if self._pool is not None:
                list(self._pool.map(lambda sl: band(sl, alpha), bands))
            else:
                band(slice(None), alpha)
            frames += 1
        return frames

    def _fade_565(self, a: Image.Image, b: Image.Image, fade_ms: int, start: float) -> int:
        # 16-bit pixels can't be blended packed; blend RGB, then pack.
        base = np.asarray(a, dtype=np.int16)
        diff = np.asarray(b, dtype=np.int16) - base
        work = np.empty_like(base)
        rgb = np.empty(base.shape, dtype=np.uint8)
        packed = np.empty(base.shape[:2], dtype="<u2")
        out = packed.view(np.uint8).reshape(self.size[1], -1)
        frames = 0
        while True:
            t = (time.monotonic() - start) * 1000 / fade_ms
            if t >= 1.0:
                break
            alpha = int(_ease(t) * 128)  # 7-bit weight keeps diff*alpha inside int16
            np.multiply(diff, alpha, out=work)
            np.right_shift(work, 7, out=work)
            np.add(work, base, out=work)
            np.copyto(rgb, work, casting="unsafe")
            self._pack565(rgb, packed)
            self._put(out)
            frames += 1
        return frames

    def _power(self, on: bool) -> None:
        try:
            fcntl.ioctl(self._fd, FBIOBLANK, FB_BLANK_UNBLANK if on else FB_BLANK_POWERDOWN)
        except OSError as e:
            log.warning("FBIOBLANK failed (%s); relying on PHOTOFRAME_SCREEN_*_CMD", e)


class HeadlessDisplay(BaseDisplay):
    """No screen connected: everything else keeps working (website, syncs,
    uploads); pictures are composed at the last known screen size but not drawn."""

    def __init__(self, size: tuple[int, int]) -> None:
        super().__init__()
        self.size = size
        self.device = "no screen connected"

    def _blit(self, img: Image.Image) -> None:
        pass

    def _power(self, on: bool) -> None:
        pass

    def info(self) -> dict:
        d = super().info()
        d["kind"] = "none"
        return d


def screen_connected() -> bool | None:
    """Is a screen plugged into an HDMI port? None if this machine has no HDMI ports."""
    from glob import glob

    statuses = glob("/sys/class/drm/card*-HDMI-A-*/status")
    if not statuses:
        return None
    for st in statuses:
        try:
            if open(st).read().strip() == "connected":
                return True
        except OSError:
            continue
    return False


class FileDisplay(BaseDisplay):
    """Writes each frame to a PNG. PHOTOFRAME_DISPLAY=file:1920x1080:/tmp/frame.png"""

    def __init__(self, size: tuple[int, int], path: str):
        super().__init__()
        self.size = size
        self.path = path
        self.frames = 0

    def _blit(self, img: Image.Image) -> None:
        self.frames += 1

    def show(self, img: Image.Image, fade_ms: int = 0) -> None:
        super().show(img, 0)
        img.save(self.path)


def make_display(spec: str, hardware: bool = True, headless_size: tuple[int, int] = (1920, 1080)) -> BaseDisplay:
    """spec: 'fb' | 'fb:/dev/fb1' | 'kms' | 'file:WxH:/path.png'.

    With 'fb' (the default) and hardware=True, the display hardware is used
    when it can be (60 fps fades on a second display layer), falling back to
    the framebuffer if it isn't available or doesn't start."""
    if spec.startswith("file"):
        _, dims, path = spec.split(":", 2)
        w, h = (int(x) for x in dims.lower().split("x"))
        return FileDisplay((w, h), path)
    if screen_connected() is False:
        log.warning("no screen is connected: running without one (the website and everything else keep "
                    "working; the frame restarts by itself when a screen is plugged in)")
        return HeadlessDisplay(headless_size)
    if (spec == "fb" and hardware) or spec == "kms":
        from . import kms

        ok, why = kms.available()
        if ok:
            try:
                return kms.KmsDisplay()
            except Exception as e:  # noqa: BLE001
                why = str(e) or type(e).__name__
        log.warning("smooth hardware fades aren't available (%s); using the framebuffer", why)
    dev = spec.split(":", 1)[1] if ":" in spec else "/dev/fb0"
    return FramebufferDisplay(dev)
