#!/usr/bin/env python3
"""Pi Frame: install Raspberry Pi OS security updates, every night.

Run as root by pi-frame-security.timer (03:40). Only Debian's security
updates are installed (52pi-frame-unattended.conf limits unattended-upgrade
to them): fixes for the parts that open photos and videos (Pillow, the HEIC
decoder, ffmpeg), SSH, OpenSSL and the rest of the system. Feature updates,
and the kernel and firmware from Raspberry Pi's own archive, are left alone,
so the frame doesn't change under you.

Writes <data>/system/security.json for the frame (Maintenance, and the
on-screen notice) and restarts Pi Frame when something was installed. When a
core part changed (libc, OpenSSL, systemd...) it notes that a restart of the
Pi is wanted; the frame does that while the screen is off.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DATA = Path(os.environ.get("PF_DATA_DIR", "/var/lib/pi-photoframe"))
RESTART_CMD = os.environ.get("PF_RESTART_CMD", "systemctl restart pi-photoframe").split()

# A change to one of these is only fully in use after the Pi restarts.
CORE = re.compile(r"^(libc6|libc-bin|libssl\d.*|openssl|systemd|systemd-.*|libsystemd\d|udev|libudev\d|dbus|"
                  r"dbus-.*|libdbus-1-\d|network-manager|libnm\d|wpasupplicant|avahi-daemon|linux-image-.*|"
                  r"raspi-firmware|libgnutls\d+|libgcrypt\d+)$")
ENV = {**os.environ, "DEBIAN_FRONTEND": "noninteractive", "APT_LISTCHANGES_FRONTEND": "none"}


def run(cmd: list[str], timeout: int) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=ENV)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]} isn't installed"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} took too long"


def installed_packages() -> dict[str, str]:
    code, out = run(["dpkg-query", "-W", "-f", "${Package}\t${Version}\t${db:Status-Abbrev}\n"], 120)
    pkgs = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].startswith("ii"):
            pkgs[parts[0]] = parts[1]
    return pkgs


def last_line(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    errs = [l for l in lines if l.startswith(("E:", "Err", "ERROR", "W:"))]
    return ((errs or lines or [""])[-1])[:300]


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.chmod(0o644)
    os.replace(tmp, path)


def check(data: Path = DATA, now: float | None = None, reboot_flag: Path = Path("/run/reboot-required")) -> dict:
    now = now or time.time()
    status_path = data / "system" / "security.json"
    log_path = data / "system" / "security-log.txt"
    prev = load(status_path)
    before = installed_packages()
    log = [f"== security updates {time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}"]

    code, out = run(["apt-get", "update", "-q", "-o", "DPkg::Lock::Timeout=600"], 1800)
    log.append(out)
    ok, message = code == 0, ""
    if code != 0:
        message = "Couldn't reach the update servers: " + last_line(out)
    else:
        code, out = run(["unattended-upgrade", "-v"], 3 * 3600)
        log.append(out)
        if code != 0:
            ok, message = False, "Installing didn't work: " + last_line(out)

    after = installed_packages()
    changed = sorted(p for p, v in after.items() if before.get(p) != v)
    status = {
        "t": now,
        "ok": ok,
        "message": message,
        "installed": changed,
        "last_ok_t": now if ok else prev.get("last_ok_t"),
        "failures": 0 if ok else int(prev.get("failures") or 0) + 1,
        "last_installed_t": now if changed else prev.get("last_installed_t"),
        "last_installed": changed or prev.get("last_installed") or [],
        # when a restart of the Pi became due (the frame compares it with the boot time)
        "reboot_t": now if (any(CORE.match(p) for p in changed) or reboot_flag.exists()) else prev.get("reboot_t"),
    }
    save(status_path, status)
    log.append(f"installed: {', '.join(changed) or 'nothing'}; ok={ok} {message}")
    try:
        log_path.write_text("\n".join(log)[-200_000:])
        log_path.chmod(0o644)
    except OSError:
        pass
    return status


def main() -> int:
    status = check()
    if status["installed"]:
        # Pi Frame picks up the new libraries (photos, videos, network) when it starts again
        subprocess.run(RESTART_CMD, check=False)
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
