"""Screen resolutions: what the HDMI screen offers, and the boot setting."""

from __future__ import annotations

import re
from glob import glob
from pathlib import Path

MODE_RE = re.compile(r"^\d{3,4}x\d{3,4}$")


def screen_modes(sysfs: str = "/sys/class/drm") -> tuple[str | None, list[str]]:
    """(connector, modes) for the connected HDMI screen, best mode first."""
    for status in sorted(glob(f"{sysfs}/card*-HDMI-A-*/status")):
        conn = Path(status).parent
        try:
            if Path(status).read_text().strip() != "connected":
                continue
            modes, seen = [], set()
            for m in (conn / "modes").read_text().split():
                m = m.rstrip("i")  # interlaced modes aren't offered
                if MODE_RE.match(m) and m not in seen:
                    seen.add(m)
                    modes.append(m)
            name = conn.name.split("-", 1)[1]  # card1-HDMI-A-1 -> HDMI-A-1
            return name, up_to_native(modes)
        except OSError:
            continue
    return None, []


def set_mode_in_cmdline(text: str, connector: str, mode: str) -> str:
    """Replace (or add) the video= setting for this connector, always 32-bit."""
    params = [p for p in text.split() if not p.startswith(f"video={connector}:")]
    params.append(f"video={connector}:{mode}-32@60")
    return " ".join(params) + "\n"


def up_to_native(modes: list[str]) -> list[str]:
    """Drop modes bigger than the screen's native (first, preferred) one.
    Many monitors accept a 4K signal and shrink it to fit; that's more than
    twice the work for the Pi with nothing to gain."""
    if not modes:
        return modes
    nw, nh = (int(x) for x in modes[0].split("x"))
    return [m for m in modes if (lambda w, h: w * h <= nw * nh and w <= nw and h <= nh)(*map(int, m.split("x")))]


def detect_screens(sysfs: str = "/sys/class/drm") -> int:
    """Make the kernel read the HDMI screens' details again (needs root).
    Returns how many ports were re-checked."""
    n = 0
    for status in sorted(glob(f"{sysfs}/card*-HDMI-A-*/status")):
        try:
            Path(status).write_text("detect")
            n += 1
        except OSError:
            pass
    return n


def current_mode_setting(cmdline: str) -> str | None:
    m = re.search(r"video=HDMI-A-\d:(\d{3,4}x\d{3,4})", cmdline)
    return m.group(1) if m else None
