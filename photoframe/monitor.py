"""The monitor's own settings over the HDMI cable (DDC/CI, via ddcutil):
real backlight brightness, a dimmer night level, and the monitor's standby.

Monitors keep these settings in a small memory chip that wears with constant
rewriting, so the frame only writes when the target actually changes: when you
let go of the slider, and at the start and end of the night period.

Monitors without DDC/CI (most small portable screens) can't be dimmed this
way; the frame darkens the picture instead (see Frame), and says so.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from glob import glob

log = logging.getLogger(__name__)

BRIGHTNESS, CONTRAST, POWER = "10", "12", "D6"
POWER_ON, POWER_STANDBY = 1, 4  # MCCS D6: 1 = on, 4 = off (DPM, wakes on request)


def _minutes(hhmm: str) -> int:
    h, m = (int(x) for x in hhmm.split(":"))
    return h * 60 + m


def in_night(now: datetime, start: str, end: str) -> bool:
    """Is `now` within start..end (which may cross midnight)?"""
    t, a, b = now.hour * 60 + now.minute, _minutes(start), _minutes(end)
    return a <= t < b if a <= b else (t >= a or t < b)


class Monitor:
    def __init__(self, cfg, runner=None, on_change=None):
        self.cfg = cfg
        self.on_change = on_change or (lambda: None)  # picture needs redrawing (software dimming)
        self._run = runner or self._ddcutil
        self._lock = threading.Lock()
        self.bus: int | None = None
        self.model: str | None = None
        self.features: set[str] = set()
        self.checked = False
        self.error = ""
        self.applied: int | None = None  # brightness last written, to avoid needless writes
        self.software_level: float = 1.0  # picture darkening when there's no DDC/CI (1.0 = none)

    # ---------- ddcutil ----------
    @staticmethod
    def _ddcutil(args: list[str], timeout: float = 15) -> str:
        r = subprocess.run(["ddcutil", *args], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip()
                               else f"ddcutil failed ({r.returncode})")
        return r.stdout

    def _bus_args(self) -> list[str]:
        return ["--bus", str(self.bus)] if self.bus is not None else []

    def probe(self) -> bool:
        """Find the monitor and what it supports. Cheap to call again."""
        with self._lock:
            self.checked = True
            if not shutil.which("ddcutil") and self._run == self._ddcutil:
                self.error = "ddcutil isn't installed (re-run install.sh)"
                return False
            if not glob("/dev/i2c-*") and self._run == self._ddcutil:
                self.error = "the HDMI control channel isn't available (re-run install.sh, then restart)"
                return False
            try:
                out = self._run(["detect", "--brief"])
            except Exception as e:  # noqa: BLE001
                self.error = f"couldn't look for the monitor ({e})"
                return False
            m = re.search(r"/dev/i2c-(\d+)", out)
            if not m or "Invalid display" in out:
                self.bus, self.error = None, ("the monitor didn't answer over DDC/CI "
                                              "(check DDC/CI is on in the monitor's own menu)")
                return False
            self.bus = int(m.group(1))
            # "Monitor: SKG:H24T7:" (brief) or "Model: H24T7" (full)
            mm = re.search(r"Monitor:\s*([^\n]+)", out)
            if mm:
                parts = [p.strip() for p in mm.group(1).split(":")]
                self.model = parts[1] if len(parts) > 1 and parts[1] else parts[0]
            else:
                mm = re.search(r"Model:\s*([^\n]+)", out)
                self.model = mm.group(1).strip() if mm else None
            try:
                caps = self._run(self._bus_args() + ["capabilities"])
                self.features = {f.upper() for f in re.findall(r"Feature:\s*([0-9A-Fa-f]{2})", caps)}
            except Exception:  # noqa: BLE001
                self.features = {BRIGHTNESS}  # it answered; assume the basics
            self.error = ""
            return True

    @property
    def available(self) -> bool:
        if not self.checked:
            self.probe()
        return self.bus is not None and BRIGHTNESS in self.features

    def get(self, code: str) -> tuple[int, int] | None:
        try:
            out = self._run(self._bus_args() + ["getvcp", code])
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            return None
        m = re.search(r"current value\s*=\s*(\d+),\s*max value\s*=\s*(\d+)", out)
        return (int(m.group(1)), int(m.group(2))) if m else None

    def set(self, code: str, value: int) -> bool:
        try:
            self._run(self._bus_args() + ["setvcp", code, str(int(value))])
            return True
        except Exception as e:  # noqa: BLE001
            self.error = f"the monitor didn't accept the change ({e})"
            log.warning("monitor: %s", self.error)
            return False

    # ---------- what the frame uses ----------
    def target(self, now: datetime | None = None) -> int:
        c = self.cfg.snapshot()
        now = now or datetime.now()
        if c.get("monitor_night_enabled") and in_night(now, c["monitor_night_from"], c["monitor_night_to"]):
            return int(c["monitor_night_brightness"])
        return int(c["monitor_brightness"])

    def apply(self, now: datetime | None = None, force: bool = False) -> bool:
        """Bring the brightness to its target; writes only if it changed."""
        level = self.target(now)
        if level < 0:  # "leave the monitor's own setting alone"
            self.software_level = 1.0
            return False
        if self.available:
            self.software_level = 1.0
            if level == self.applied and not force:
                return False
            if self.set(BRIGHTNESS, level):
                log.info("monitor brightness set to %d", level)
                self.applied = level
                return True
            return False
        # no DDC/CI: darken the picture instead (never below 15%, so it's still visible)
        new = max(0.15, level / 100)
        changed = abs(new - self.software_level) > 1e-3
        self.software_level = new
        return changed

    def standby(self, on: bool) -> bool:
        """The monitor's own power: on, or standby. Only if enabled and supported."""
        if not self.cfg.get("monitor_standby") or not self.available or POWER not in self.features:
            return False
        ok = self.set(POWER, POWER_ON if on else POWER_STANDBY)
        if ok and on:
            self.applied = None  # some monitors forget their brightness in standby: write it again
            time.sleep(1.5)
            self.apply(force=True)
        return ok

    def status(self) -> dict:
        info = {"available": self.available, "model": self.model, "error": self.error,
                "features": sorted(self.features), "standby_supported": POWER in self.features,
                "software_level": self.software_level, "target": self.target()}
        if self.available:
            b = self.get(BRIGHTNESS)
            if b:
                info["brightness"], info["max"] = b
        return info

    def run(self, stop: threading.Event) -> None:
        """Apply at start-up, then check each minute for the night period starting or ending."""
        time.sleep(5)
        while not stop.is_set():
            try:
                if self.apply() and not self.available:
                    self.on_change()  # software dimming changed: redraw the picture
            except Exception as e:  # noqa: BLE001
                log.warning("monitor: %s", e)
            stop.wait(60 - (time.time() % 60) + 1)
