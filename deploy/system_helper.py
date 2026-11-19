#!/usr/bin/env python3
"""Pi Frame system helper (runs as root).

    system_helper.py request   handle /var/lib/pi-photoframe/system/request.json
                               (started by pi-frame-system.path when the web page asks)
    system_helper.py refresh   at boot: if the screen size changed, remake the boot logo

The frame itself runs unprivileged and can't change boot settings; it drops a
request here instead. Only resolutions the screen itself reports (its EDID
list) are accepted, and cmdline.txt is backed up before the first change.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from photoframe.sysmodes import detect_screens, screen_modes, set_mode_in_cmdline  # noqa: E402
DATA = Path("/var/lib/pi-photoframe")
BOOT = Path("/boot/firmware") if Path("/boot/firmware").is_dir() else Path("/boot")
def _status(state: str, message: str) -> None:
    d = DATA / "system"
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "status.json.tmp"
    tmp.write_text(json.dumps({"state": state, "message": message, "t": time.time()}))
    tmp.chmod(0o644)
    tmp.replace(d / "status.json")


def handle_request(boot: Path = BOOT, sysfs: str = "/sys/class/drm", reboot: bool = True) -> int:
    req_path = DATA / "system" / "request.json"
    try:
        req = json.loads(req_path.read_text())
    except (OSError, ValueError):
        return 0
    finally:
        req_path.unlink(missing_ok=True)
    if req.get("action") == "detect":
        n = detect_screens(sysfs)
        time.sleep(1.5)  # give the screen a moment to answer
        conn, modes = screen_modes(sysfs)
        _status("done", f"Screen re-checked: {modes[0].replace('x', ' × ')} native" if modes else
                "Screen re-checked, but no HDMI screen answered")
        return 0 if n else 1
    if req.get("action") == "splash":  # e.g. the screen's orientation changed
        rc = remake_bootsplash()
        _status("done" if rc == 0 else "failed", "Boot screen updated" if rc == 0 else "Couldn't update the boot screen")
        return rc
    if req.get("action") != "set_mode":
        _status("failed", "Unknown request")
        return 1
    mode = str(req.get("mode", ""))
    connector, modes = screen_modes(sysfs)
    if not connector:
        _status("failed", "No HDMI screen is connected")
        return 1
    if mode not in modes:
        _status("failed", f"The screen doesn't offer {mode}")
        return 1
    cmdline = boot / "cmdline.txt"
    backup = boot / "cmdline.txt.pre-resolution"
    if not backup.exists():
        backup.write_text(cmdline.read_text())
    new = set_mode_in_cmdline(cmdline.read_text(), connector, mode)
    tmp = cmdline.with_name("cmdline.txt.tmp")
    tmp.write_text(new)
    tmp.replace(cmdline)
    # make the boot images for the new size now, so the first boot is already right
    _status("preparing", f"Preparing the boot screen for {mode.replace('x', ' × ')}")
    remake_bootsplash(mode)
    _status("restarting", f"Resolution set to {mode}; restarting")
    if reboot:
        time.sleep(1)
        subprocess.run(["systemctl", "reboot"], check=False)
    return 0


SPLASH_PNG = Path("/usr/share/pi-photoframe/splash.png")
SPLASH_META = Path("/usr/share/pi-photoframe/splash.json")


def remake_bootsplash(mode: str | None = None) -> int:
    """Remake the boot images (for `mode`, e.g. '2560x1440', or the current screen)."""
    if not SPLASH_PNG.exists():
        return 0  # the boot screen isn't set up
    args = [sys.executable, str(ROOT / "deploy" / "bootsplash.py"), "install"] + ([mode] if mode else [])
    return subprocess.run(args).returncode


def splash_out_of_date(fb: str = "/sys/class/graphics/fb0/virtual_size") -> str | None:
    """Why the boot images don't suit the screen any more, or None."""
    if not SPLASH_PNG.exists():
        return None
    try:
        w, h = (int(x) for x in Path(fb).read_text().split(","))
    except (OSError, ValueError):
        return None
    try:
        meta = json.loads(SPLASH_META.read_text())
    except (OSError, ValueError):
        from PIL import Image  # made by an older version: go by the picture's size

        with Image.open(SPLASH_PNG) as im:
            meta = {"size": list(im.size)}
    if meta.get("size") != [w, h]:
        return f"screen is now {w}x{h}"
    try:
        cfg = json.loads((DATA / "config.json").read_text()).get("config", {})
    except (OSError, ValueError):
        cfg = {}
    orient, rot = cfg.get("display_orientation", "landscape"), int(cfg.get("display_rotation_deg", 0))
    if "orientation" in meta and (meta["orientation"], meta.get("rotation", 0)) != (orient, rot):
        return f"screen is now {orient}, turned {rot}°"
    return None


def refresh_bootsplash(fb: str = "/sys/class/graphics/fb0/virtual_size") -> int:
    """At start-up: after a new screen or a change made some other way, remake
    the boot images to fit."""
    connector, _ = screen_modes()
    if connector is None:
        return 0  # no screen connected: the console's stand-in size isn't a real screen's
    why = splash_out_of_date(fb)
    if not why:
        return 0
    print(f"{why}: remaking the boot logo")
    return remake_bootsplash()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "request":
        sys.exit(handle_request())
    if cmd == "refresh":
        sys.exit(refresh_bootsplash())
    print(__doc__)
    sys.exit(2)
