"""Raspberry Pi OS security updates, as the frame sees them.

The updates themselves are installed by root each night
(deploy/security_updates.py, pi-frame-security.timer), which writes
<data>/system/security.json. This reads it for Maintenance and the on-screen
notice, and restarts the Pi when an update needs it, while the screen is off.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

FAIL_DAYS = 3  # say something when security updates haven't worked for this long
REBOOT_NAG_DAYS = 3  # ... or a restart has been waiting this long
NO_SCHEDULE_WINDOW = (3 * 60 + 50, 4 * 60 + 30)  # restart then if the screen is never scheduled off


def boot_time(stat: str = "/proc/stat") -> float:
    try:
        for line in Path(stat).read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except OSError:
        pass
    return 0.0


def _ago(secs: float) -> str:
    days = int(secs // 86400)
    if days >= 2:
        return f"{days} days"
    hours = int(secs // 3600)
    return f"{hours} hours" if hours >= 2 else "1 hour" if hours == 1 else f"{max(1, int(secs // 60))} min"


class SecurityUpdates:
    def __init__(self, data_dir: Path, installed: bool | None = None):
        self.path = Path(data_dir) / "system" / "security.json"
        # on a Pi set up by install.sh (not when trying the frame on a computer)
        self.installed = (Path("/usr/local/sbin/pi-frame-security-updates").exists()
                          if installed is None else installed)
        self.rebooting = False

    def status(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def reboot_waiting_since(self, now: float | None = None) -> float | None:
        """When a restart of the Pi became due, if it hasn't happened since."""
        t = self.status().get("reboot_t")
        if t and boot_time() < t:
            return t
        return None

    def summary(self, now: float | None = None) -> dict:
        now = now or time.time()
        s = self.status()
        return {**s, "enabled": self.installed, "reboot_waiting_since": self.reboot_waiting_since(now)}

    def health(self, now: float | None = None) -> dict | None:
        """A line for the Maintenance summary (and, when it's a warning, the screen)."""
        if not self.installed:
            return None
        now = now or time.time()
        s = self.status()
        if not s:
            return {"level": "ok", "text": "Switched on: the first check is tonight"}
        last_ok = s.get("last_ok_t")
        if not s.get("ok") and (not last_ok or now - last_ok > FAIL_DAYS * 86400):
            since = f" for {_ago(now - last_ok)}" if last_ok else ""
            return {"level": "warn", "text": f"Security updates haven't worked{since}: {s.get('message') or 'unknown error'}"}
        waiting = self.reboot_waiting_since(now)
        if waiting and now - waiting > REBOOT_NAG_DAYS * 86400:
            return {"level": "warn", "text": f"A restart for security updates has been waiting {_ago(now - waiting)}: "
                                             "Maintenance → Restart the Pi"}
        n = len(s.get("last_installed") or [])
        when = s.get("last_installed_t")
        text = (f"Up to date; {n} installed {_ago(now - when)} ago" if n and when
                else "Up to date") + f" (checked {_ago(now - s['t'])} ago)"
        if waiting:
            text += "; the Pi restarts when the screen is next off"
        return {"level": "ok", "text": text}

    def may_reboot_now(self, screen_scheduled_off: bool, schedule_on: bool, busy: bool,
                       now: float | None = None) -> bool:
        """Restart only when nobody's watching: while the screen is scheduled
        off, or (with no screen schedule) in the small hours."""
        if busy or self.rebooting or not self.reboot_waiting_since(now):
            return False
        if schedule_on:
            return screen_scheduled_off
        tm = time.localtime(now or time.time())
        minute = tm.tm_hour * 60 + tm.tm_min
        return NO_SCHEDULE_WINDOW[0] <= minute < NO_SCHEDULE_WINDOW[1]

    def run(self, frame, stop) -> None:
        """Every 10 minutes: restart the Pi if a security update needs it and it's a good moment."""
        while not stop.wait(600):
            try:
                c = frame.cfg.snapshot()
                upd = Path(frame.data_dir) / "update"
                busy = (bool(getattr(frame.backups, "running", None)) or (upd / "work").exists()
                        or (upd / "pending.zip").exists())  # a backup or a Pi Frame update in progress
                if self.may_reboot_now(not frame.screen_should_be_on(), bool(c.get("screen_schedule_enabled")), busy):
                    self.reboot()
            except Exception as e:  # noqa: BLE001
                log.warning("security update check: %s", e)

    def reboot(self) -> None:
        if not shutil.which("systemctl"):
            return
        self.rebooting = True
        log.warning("restarting the Pi to finish installing security updates")
        r = subprocess.run(["systemctl", "reboot"], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            self.rebooting = False
            log.warning("couldn't restart the Pi: %s", (r.stderr or r.stdout).strip())
