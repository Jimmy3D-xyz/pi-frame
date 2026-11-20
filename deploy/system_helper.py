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
import re
import shutil
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
    if req.get("action") == "first_run":
        return first_run(req)
    if req.get("action") == "factory_reset":
        return factory_reset(bool(req.get("remove_logins")))
    if req.get("action") == "ssh":
        return ssh_request(req)
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


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)


def set_wifi_country(cc: str, boot: Path = BOOT) -> str | None:
    """The Wi-Fi country (which channels may be used); also unblocks the radio."""
    if shutil.which("raspi-config"):
        r = _run(["raspi-config", "nonint", "do_wifi_country", cc])
        if r.returncode == 0:
            return None
    # without raspi-config: set it now, and on the kernel command line for later boots
    _run(["iw", "reg", "set", cc])
    _run(["rfkill", "unblock", "wifi"])
    cmdline = boot / "cmdline.txt"
    if cmdline.exists():
        line = re.sub(r" ?cfg80211\.ieee80211_regdom=\S+", "", cmdline.read_text().strip())
        cmdline.write_text(f"{line} cfg80211.ieee80211_regdom={cc}\n")
    return None


def set_timezone(tz: str) -> str | None:
    if not (Path("/usr/share/zoneinfo") / tz).is_file():
        return f"unknown time zone {tz}"
    r = _run(["timedatectl", "set-timezone", tz])
    if r.returncode != 0:  # e.g. no systemd-timedated: set it by hand
        Path("/etc/localtime").unlink(missing_ok=True)
        Path("/etc/localtime").symlink_to(Path("/usr/share/zoneinfo") / tz)
        Path("/etc/timezone").write_text(tz + "\n")
    return None


def add_login(user: str, pw_hash: str, passwd: str = "/etc/passwd") -> str | None:
    """Create the person's login and switch SSH on, only while the Pi has no login."""
    from photoframe.firstrun import RESERVED, USERNAME_RE, login_users

    if login_users(passwd):
        return "this Pi already has a login"
    if not USERNAME_RE.match(user) or user in RESERVED or not pw_hash.startswith("$6$"):
        return "that username or password can't be used"
    groups = [g for g in ("sudo", "adm", "video", "render", "audio", "gpio", "i2c", "plugdev", "netdev",
                          "systemd-journal") if _run(["getent", "group", g]).returncode == 0]
    r = _run(["useradd", "--create-home", "--shell", "/bin/bash", "--groups", ",".join(groups),
              "--password", pw_hash, user])
    if r.returncode != 0:
        return "couldn't create the login: " + (r.stderr or r.stdout).strip()[-200:]
    # SSH: host keys are made on first boot; start it now and at every boot
    _run(["systemctl", "enable", "--now", "ssh"])
    return None


def set_login_password(user: str, pw_hash: str, passwd: str = "/etc/passwd") -> str | None:
    """A new password for an existing login (only people's logins, never root or system accounts)."""
    from photoframe.firstrun import login_users

    if user not in login_users(passwd):
        return f"{user} isn't a login on this Pi"
    if not pw_hash.startswith("$6$"):
        return "that password can't be used"
    r = _run(["usermod", "--password", pw_hash, user])
    return None if r.returncode == 0 else "couldn't change the password: " + (r.stderr or r.stdout).strip()[-200:]


def set_ssh(on: bool) -> str | None:
    r = _run(["systemctl", "enable" if on else "disable", "--now", "ssh.service"])
    return None if r.returncode == 0 else f"couldn't switch SSH {'on' if on else 'off'}: " + (r.stderr or "").strip()[-200:]


def ssh_request(req: dict, passwd: str = "/etc/passwd") -> int:
    """The Maintenance tab's SSH section: create a login (only when there's none),
    change a login's password, or switch SSH on or off."""
    what = req.get("what")
    if what == "create":
        err = add_login(str(req.get("username", "")), str(req.get("hash", "")), passwd=passwd)
        done = f"Created the login {req.get('username')}, and switched SSH on"
    elif what == "password":
        err = set_login_password(str(req.get("username", "")), str(req.get("hash", "")), passwd=passwd)
        done = f"Changed the password for {req.get('username')}"
    elif what in ("on", "off"):
        err = set_ssh(what == "on")
        done = f"SSH switched {what}"
    else:
        err = "unknown request"
    if err:
        _status("failed", err[0].upper() + err[1:])
        return 1
    _status("done", done)
    return 0


def first_run(req: dict) -> int:
    """The welcome wizard's system settings."""
    problems = []
    cc = str(req.get("country", ""))
    if re.fullmatch(r"[A-Z]{2}", cc):
        if (e := set_wifi_country(cc)):
            problems.append(e)
    tz = str(req.get("timezone", ""))
    if tz:
        if (e := set_timezone(tz)):
            problems.append(e)
    ssh = req.get("ssh")
    if ssh:
        if (e := add_login(str(ssh.get("username", "")), str(ssh.get("hash", "")))):
            problems.append(e)
    if problems:
        _status("failed", "; ".join(problems).capitalize())
        return 1
    _status("done", "Set up")
    return 0


def forget_wifi() -> list[str]:
    """Delete every saved Wi-Fi network (and the setup hotspot's profile)."""
    r = _run(["nmcli", "-t", "-f", "UUID,TYPE,NAME", "connection", "show"])
    gone = []
    for line in (r.stdout or "").splitlines():
        uuid, _, rest = line.partition(":")
        kind, _, name = rest.partition(":")
        if kind == "802-11-wireless":
            _run(["nmcli", "connection", "delete", "uuid", uuid])
            gone.append(name.replace("\\:", ":"))
    return gone


def wipe_data(data: Path, owner: str = "photoframe") -> None:
    """Empty the frame's data folder (keeping the folder and its owner) and
    leave the first-run marker, so the welcome wizard runs again."""
    for child in data.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)
    marker = data / ".first-run"
    marker.touch()
    shutil.chown(marker, owner, owner)


def factory_reset(remove_logins: bool, data: Path = DATA, passwd: str = "/etc/passwd", owner: str = "photoframe") -> int:
    """Back to how a new card starts: no photos, settings, sign-ins or saved
    Wi-Fi; the welcome wizard on the next start. Optionally the SSH logins go too."""
    from photoframe.firstrun import login_users

    print("factory reset: stopping the frame")
    _run(["systemctl", "stop", "pi-photoframe.service"])
    wipe_data(data, owner)  # (nothing may be written into it as root from here on)
    print("factory reset: forgot Wi-Fi networks:", ", ".join(forget_wifi()) or "none")
    if remove_logins:
        for user in login_users(passwd):
            _run(["pkill", "-KILL", "-u", user])
            r = _run(["userdel", "--remove", user])
            print(f"factory reset: removed the login {user}" + ("" if r.returncode == 0 else f" ({r.stderr.strip()})"))
        for f in Path("/etc/sudoers.d").glob("010_*-nopasswd"):
            f.unlink(missing_ok=True)
        _run(["systemctl", "disable", "ssh.service"])
    print("factory reset: done; restarting")
    _run(["systemctl", "reboot"])
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
