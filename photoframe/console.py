"""Keep console text off the screen.

Putting the visible virtual terminal (tty1) into graphics mode (KD_GRAPHICS)
tells the kernel's console not to draw anything on it: no boot log, no driver
warnings in the middle of a slideshow, nothing over the shutdown screen. It's
what photo viewers and desktops do. The mode stays until changed, so it
survives the frame restarting and lasts through shutdown.

Needs root, or the CAP_SYS_TTY_CONFIG capability (the service is given it).
"""

from __future__ import annotations

import fcntl
import logging
import os

log = logging.getLogger(__name__)

KDSETMODE = 0x4B3A
KDGETMODE = 0x4B3B
KD_TEXT = 0
KD_GRAPHICS = 1


def _open(tty: str) -> int:
    # The visible console is usually crw--w---- root:tty: the tty group may
    # write but not read, so open it write-only (enough for these ioctls).
    try:
        return os.open(tty, os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        return os.open(tty, os.O_RDWR | os.O_NOCTTY)


def get_mode(tty: str = "/dev/tty1") -> int | None:
    """KD_TEXT, KD_GRAPHICS, or None if it can't be read."""
    import array

    try:
        fd = _open(tty)
    except OSError:
        return None
    try:
        buf = array.array("i", [0])
        fcntl.ioctl(fd, KDGETMODE, buf)
        return buf[0]
    except OSError:
        return None
    finally:
        os.close(fd)


last_error = ""


def set_graphics(on: bool = True, tty: str = "/dev/tty1") -> bool:
    """Switch the console to graphics (or text) mode. True if it's in that mode
    afterwards, including when it already was (the boot step sets it as root)."""
    global last_error
    want = KD_GRAPHICS if on else KD_TEXT
    try:
        fd = _open(tty)
    except OSError as e:
        last_error = f"can't open {tty}: {e.strerror or e}"
        log.debug("console: %s", last_error)
        return get_mode(tty) == want
    try:
        fcntl.ioctl(fd, KDSETMODE, want)
        last_error = ""
        return True
    except OSError as e:
        last_error = f"can't switch {tty} to {'graphics' if on else 'text'} mode: {e.strerror or e}"
        log.debug("console: %s", last_error)
        return get_mode(tty) == want
    finally:
        os.close(fd)
