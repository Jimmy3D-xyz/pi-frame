#!/usr/bin/env python3
"""Early in boot (pi-frame-splash.service, before drivers load): switch the
screen to graphics mode so no more console text appears, and redraw the Pi
Frame splash cleanly over anything the kernel printed in the first seconds.
The frame itself takes over the screen about 15 seconds later."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SPLASH = Path("/usr/share/pi-photoframe/splash.png")


def allow_frame(tty: str = "/dev/tty1") -> bool:
    """Put the console's standard permissions back (owner root, group tty may
    write), so the frame (in the tty group) can keep it in graphics mode too.
    Something (e.g. a login prompt that ran on it before) can leave it root-only."""
    import grp
    import os

    try:
        os.chown(tty, 0, grp.getgrnam("tty").gr_gid)
        os.chmod(tty, 0o620)
        return True
    except (OSError, KeyError):
        return False


def main(fb: str = "/dev/fb0", splash: Path = SPLASH, geometry=None, tty: str = "/dev/tty1") -> int:
    from photoframe import console

    console.set_graphics(True)  # from now on, no console text on screen
    allow_frame(tty)
    # Never wait for the screen: this runs before udev loads drivers, so on a
    # setup where the screen's driver comes from udev, waiting would deadlock
    # the boot. The screen's driver is normally already loaded (from the
    # initramfs); if not, just leave the kernel's logo as it is.
    if not Path(fb).exists() or not splash.exists():
        return 0
    from PIL import Image

    from photoframe.display import FramebufferDisplay

    disp = FramebufferDisplay(fb, geometry)
    img = Image.open(splash).convert("RGB")
    if img.size != disp.size:
        # made for another screen size (the remake runs later this boot):
        # shrink or grow to fit without stretching, centred on black
        from PIL import ImageOps

        img = ImageOps.pad(img, disp.size, Image.BILINEAR, color=(0, 0, 0))
    disp.show(img)
    return 0


if __name__ == "__main__":
    sys.exit(main())
