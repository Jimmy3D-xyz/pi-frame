import importlib.util
from pathlib import Path

import numpy as np
from PIL import Image

from photoframe import console

ROOT = Path(__file__).resolve().parents[1]


def test_set_graphics_uses_kdsetmode(tmp_path, monkeypatch):
    tty = tmp_path / "tty1"
    tty.write_bytes(b"")
    calls = []
    opened = []
    real_open = console.os.open
    monkeypatch.setattr(console.os, "open", lambda p, flags: opened.append(flags) or real_open(p, flags))
    monkeypatch.setattr(console.fcntl, "ioctl", lambda fd, req, arg: calls.append((req, arg)))
    assert console.set_graphics(True, str(tty)) is True
    assert console.set_graphics(False, str(tty)) is True
    assert calls == [(0x4B3A, 1), (0x4B3A, 0)]  # KDSETMODE: graphics, then text
    import os
    assert opened[0] & os.O_ACCMODE == os.O_WRONLY  # write-only: the console allows the tty group only that


def test_set_graphics_failure_is_quiet(tmp_path, monkeypatch):
    assert console.set_graphics(True, str(tmp_path / "missing")) is False

    def denied(*a):
        raise PermissionError(1, "Operation not permitted")

    (tmp_path / "tty1").write_bytes(b"")
    monkeypatch.setattr(console.fcntl, "ioctl", denied)
    assert console.set_graphics(True, str(tmp_path / "tty1")) is False


def test_early_splash_locks_console_and_redraws(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("console_splash", ROOT / "deploy" / "console_splash.py")
    cs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs)
    events = []
    monkeypatch.setattr(console, "set_graphics", lambda on=True, tty="/dev/tty1": events.append(on) or True)
    w, h = 320, 200
    fb = tmp_path / "fb0"
    fb.write_bytes(b"\0" * w * h * 4)
    splash = tmp_path / "splash.png"
    Image.new("RGB", (640, 400), (200, 80, 40)).save(splash)  # different size: scaled to the screen
    assert cs.main(str(fb), splash, geometry=(w, h, 32, w * 4, 16)) == 0
    assert events and events[0] is True  # console switched to graphics mode before drawing
    px = np.frombuffer(fb.read_bytes(), np.uint8).reshape(h, w, 4)
    assert tuple(px[100, 160, :3]) == (40, 80, 200)  # BGRX: the splash is on screen


def test_early_splash_never_waits_for_a_missing_screen(tmp_path, monkeypatch):
    import time
    spec = importlib.util.spec_from_file_location("console_splash", ROOT / "deploy" / "console_splash.py")
    cs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs)
    monkeypatch.setattr(console, "set_graphics", lambda on=True, tty="/dev/tty1": True)
    t = time.monotonic()
    assert cs.main(str(tmp_path / "no-fb"), tmp_path / "no.png") == 0
    assert time.monotonic() - t < 1  # returns straight away (waiting could deadlock the boot)



def test_already_in_graphics_mode_counts_as_success(tmp_path, monkeypatch):
    """The boot step (root) already set graphics mode; if the frame itself isn't
    allowed to, that's fine and not worth a warning."""
    import array
    (tmp_path / "tty1").write_bytes(b"")

    def ioctl(fd, req, arg):
        if req == 0x4B3A:  # KDSETMODE: not permitted
            raise PermissionError(1, "Operation not permitted")
        if req == 0x4B3B:  # KDGETMODE: graphics
            arg[0] = 1

    monkeypatch.setattr(console.fcntl, "ioctl", ioctl)
    assert console.set_graphics(True, str(tmp_path / "tty1")) is True
    assert "not permitted" in console.last_error  # the reason is kept for the log if it matters

    def ioctl_text(fd, req, arg):
        if req == 0x4B3A:
            raise PermissionError(1, "Operation not permitted")
        if req == 0x4B3B:
            arg[0] = 0  # still text mode: a real failure

    monkeypatch.setattr(console.fcntl, "ioctl", ioctl_text)
    assert console.set_graphics(True, str(tmp_path / "tty1")) is False



def test_boot_step_restores_console_permissions(tmp_path, monkeypatch):
    import grp
    import os
    spec = importlib.util.spec_from_file_location("console_splash", ROOT / "deploy" / "console_splash.py")
    cs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs)
    tty = tmp_path / "tty1"
    tty.write_bytes(b"")
    os.chmod(tty, 0o600)  # root-only, as found on the developer's Pi
    calls = []
    monkeypatch.setattr(os, "chown", lambda p, u, g: calls.append((str(p), u, g)))
    assert cs.allow_frame(str(tty))
    assert calls == [(str(tty), 0, grp.getgrnam("tty").gr_gid)]  # owner root, group tty
    assert oct(tty.stat().st_mode & 0o777) == "0o620"  # the tty group may write
