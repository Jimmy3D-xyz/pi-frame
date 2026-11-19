"""Presence sensor: turn the screen on when someone is in the room, and off a
while after they've left.

Made for 24 GHz mmWave radar modules such as the Waveshare HMMD (S3KM1110) or
HLK-LD2420: they sense people even when sitting still, and their OT2 pin is
high while someone is there. Wiring (3.3 V only, never 5 V):

    VCC -> pin 17 (3.3 V)    GND -> pin 14    OT2 -> pin 15 (GPIO 22)

Works around the rest of the frame:
- by default it only wakes the screen within the screen schedule's on-hours;
- if you turn the screen off by hand while you're there, it stays off until
  you've left and someone comes in again;
- a playing video, or the Wi-Fi setup / update screens, keep the screen on.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)


class Presence:
    def __init__(self, frame, factory=None, clock=time.monotonic):
        self.frame = frame
        self.factory = factory  # a gpiozero pin factory; tests pass a MockFactory
        self.clock = clock
        self.dev = None
        self.error = ""
        self.last_seen: float | None = None
        self.armed = True          # may wake the screen on the next arrival
        self.off_by_presence = False

    # ---------- set-up ----------
    def close(self) -> None:
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:  # noqa: BLE001
                pass
            self.dev = None

    def start(self) -> bool:
        self.close()
        c = self.frame.cfg.snapshot()
        self.error = ""
        if not c.get("presence_enabled"):
            return False
        pin = c.get("presence_pin")
        used = {c.get(f"button_{a}_pin") for a in ("previous", "next", "screen")} if c.get("buttons_enabled") else set()
        if pin in used:
            self.error = "the presence sensor is set to the same GPIO pin as a button"
            return False
        try:
            from gpiozero import DigitalInputDevice

            factory = self.factory
            if factory is None:
                from .buttons import shared_factory

                factory = shared_factory(str(self.frame.data_dir / "gpio"))
            # the sensor drives its pin high or low itself; a pull-down keeps it low if unplugged
            self.dev = DigitalInputDevice(pin, pull_up=False, pin_factory=factory)
        except Exception as e:  # noqa: BLE001
            self.error = f"couldn't use GPIO {pin} for the presence sensor ({e})"
            log.warning("presence: %s", self.error)
            return False
        self.last_seen = self.clock()  # assume someone's there at start-up: no instant switch-off
        self.armed = True
        log.info("presence sensor ready on GPIO %s (screen off after %s min without anyone)", pin,
                 c.get("presence_timeout_minutes"))
        return True

    # ---------- what's happening ----------
    def present(self) -> bool | None:
        if self.dev is None:
            return None
        try:
            return bool(self.dev.is_active)
        except Exception:  # noqa: BLE001
            return None

    def manual(self, on: bool) -> None:
        """The screen was switched by hand (button or website)."""
        if on:
            self.last_seen = self.clock()  # treat it as someone being here
            self.armed = True
        else:
            self.armed = False  # stay off until they've left and someone comes back
        self.off_by_presence = False

    def tick(self) -> str | None:
        """One check; returns what it did (for logs and tests)."""
        if self.dev is None:
            return None
        f, c = self.frame, self.frame.cfg.snapshot()
        now = self.clock()
        active = self.present()
        if active is None:
            return None
        if active:
            self.last_seen = now
        else:
            self.armed = True  # nobody there now: the next arrival may wake the screen
        allowed = not c.get("presence_schedule_only", True) or f.screen_should_be_on()
        busy = f.notice in ("wifi", "update", "power")
        if active and self.armed and not f.display.screen_on and allowed and not busy:
            f._power(True)
            self.off_by_presence = False
            log.info("presence: someone's here; screen on")
            return "on"
        quiet_for = now - (self.last_seen or now)
        if (f.display.screen_on and not active and quiet_for >= c["presence_timeout_minutes"] * 60
                and not f.player.playing and not busy):
            f.player.stop()
            f._power(False)
            self.off_by_presence = True
            log.info("presence: nobody seen for %d min; screen off", c["presence_timeout_minutes"])
            return "off"
        return None

    def status(self) -> dict:
        c = self.frame.cfg.snapshot()
        p = self.present()
        return {"enabled": bool(c.get("presence_enabled")), "active": self.dev is not None, "error": self.error,
                "present": p, "pin": c.get("presence_pin"),
                "quiet_seconds": None if p or self.last_seen is None else round(self.clock() - self.last_seen),
                "off_by_presence": self.off_by_presence}

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(1.0):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                log.warning("presence: %s", e)
