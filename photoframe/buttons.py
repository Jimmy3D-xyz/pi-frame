"""Physical buttons wired to the Pi's GPIO pins.

Each button connects a GPIO pin to ground; the Pi's internal pull-ups mean no
resistors are needed. Default pins (BCM numbering, physical pin in brackets):

    Previous  GPIO 17 (pin 11)    Next  GPIO 27 (pin 13)    ground: pin 9 or 14
    Screen    GPIO 3  (pin 5)     ground: pin 6

Next button: press for the next step; hold (1 second) for a new set (a new
collage, or the next style when styles take turns).

Screen button: press to turn the screen off/on; hold to shut the Pi down
safely. On GPIO 3, pressing it while the Pi is shut down turns it back on (a
built-in Raspberry Pi feature). While the screen is off, any button just wakes it.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
import threading
import time

log = logging.getLogger(__name__)

ACTIONS = ("previous", "next", "screen")
# physical header pin for each BCM GPIO we suggest, for friendly messages
PHYSICAL = {2: 3, 3: 5, 4: 7, 5: 29, 6: 31, 12: 32, 13: 33, 16: 36, 17: 11, 18: 12, 19: 35, 20: 38, 21: 40,
            22: 15, 23: 16, 24: 18, 25: 22, 26: 37, 27: 13}


def available() -> tuple[bool, str]:
    try:
        import gpiozero  # noqa: F401  (apt: python3-gpiozero python3-lgpio)
    except ImportError:
        return False, "gpiozero isn't installed (re-run install.sh)"
    return True, ""


def lgpio_factory(work_dir: str):
    """gpiozero's lgpio pin factory, created explicitly.

    - lgpio writes small notification files to its working directory, which
      must be writable; the frame's program folder is read-only by design, so
      point it (LG_WD) at the frame's data folder.
    - Don't let gpiozero quietly fall back to its older pin libraries: on
      current kernels they fail with misleading errors ("Invalid argument").
    - The GPIO header is gpiochip0 on current kernels for every Pi (older Pi 5
      kernels used gpiochip4), so try 0 first.
    """
    os.makedirs(work_dir, exist_ok=True)
    os.environ["LG_WD"] = work_dir
    try:
        import lgpio  # noqa: F401  (apt: python3-lgpio)
    except ImportError as e:
        raise RuntimeError("the lgpio library isn't installed (re-run install.sh)") from e
    from gpiozero.pins.lgpio import LGPIOFactory

    last = None
    for chip in (0, 4):
        try:
            return LGPIOFactory(chip=chip)
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"lgpio couldn't open the GPIO pins ({last})")


_shared = None


def shared_factory(work_dir: str):
    """One lgpio pin factory for everything on the GPIO pins (buttons, presence sensor)."""
    global _shared
    if _shared is None:
        _shared = lgpio_factory(work_dir)
    return _shared


class Buttons:
    def __init__(self, frame, factory=None):
        self.frame = frame
        self.factory = factory  # a gpiozero pin factory; tests pass a MockFactory
        self._own_factory = None
        self._devices: list = []
        self._lock = threading.Lock()
        self.last: dict | None = None  # {"button", "action", "t"} for the web page
        self._held_flag: dict[str, bool] = {}  # per button: was this press a hold?
        self.error = ""

    # ---------- setup ----------
    def close(self) -> None:
        with self._lock:
            for d in self._devices:
                try:
                    d.close()
                except Exception:  # noqa: BLE001
                    pass
            self._devices = []

    def start(self) -> bool:
        """(Re)create the buttons from the current settings."""
        self.close()
        c = self.frame.cfg.snapshot()
        self.error = ""
        if not c.get("buttons_enabled"):
            return False
        ok, why = available()
        if not ok:
            self.error = why
            log.warning("buttons: %s", why)
            return False
        from gpiozero import Button

        factory = self.factory
        if factory is None:
            if self._own_factory is None:
                try:
                    self._own_factory = shared_factory(str(self.frame.data_dir / "gpio"))
                except Exception as e:  # noqa: BLE001
                    msg = str(e)
                    self.error = ("No GPIO pins found: buttons only work on a Raspberry Pi" if "gpiochip" in msg and
                                  not os.path.exists("/dev/gpiochip0") else msg)
                    log.warning("buttons: %s", self.error)
                    return False
                log.info("buttons: using lgpio (work dir %s)", self.frame.data_dir / "gpio")
            factory = self._own_factory
        pins = {a: c.get(f"button_{a}_pin") for a in ACTIONS}
        used = [p for p in pins.values() if p is not None and p >= 0]
        if len(used) != len(set(used)):
            self.error = "two buttons are set to the same GPIO pin"
            log.warning("buttons: %s", self.error)
            return False
        hold = float(c.get("button_hold_seconds", 5))
        created = []
        try:
            for action, pin in pins.items():
                if pin is None or pin < 0:
                    continue  # this button isn't fitted
                kw = {"pull_up": True, "bounce_time": 0.05, "hold_time": hold if action == "screen" else 1.0,
                      "pin_factory": factory}
                b = Button(pin, **kw)
                self._held_flag[action] = False
                b.when_held = functools.partial(self._held, action)
                b.when_released = functools.partial(self._released, action)
                created.append(b)
        except Exception as e:  # noqa: BLE001
            for b in created:
                b.close()
            msg = str(e)
            if "busy" in msg.lower() or "in use" in msg.lower() or "gpio busy" in msg.lower():
                self.error = f"a button's GPIO pin is already in use by something else ({msg})"
            elif "pin factory" in msg.lower():
                self.error = "No GPIO pins found: buttons only work on a Raspberry Pi (re-run install.sh if this is one)"
            else:
                self.error = f"couldn't use the GPIO pins ({msg})"
            log.warning("buttons: %s", self.error)
            return False
        with self._lock:
            self._devices = created
        log.info("buttons ready: %s", ", ".join(f"{a} on GPIO {p}" for a, p in pins.items() if p is not None and p >= 0))
        return True

    # ---------- events ----------
    def _held(self, action: str) -> None:
        self._held_flag[action] = True
        if action == "screen" and self.frame.cfg.get("button_hold_shutdown"):
            self._record("screen", "shut down")
            threading.Thread(target=self.shutdown, daemon=True).start()

    def _released(self, action: str) -> None:
        if self._held_flag.get(action):  # the hold already did its job
            self._held_flag[action] = False
            if action == "screen" and self.frame.cfg.get("button_hold_shutdown"):
                return
            if action == "next" and self.frame.display.screen_on:
                self._record("next", "new set")
                threading.Thread(target=self.frame.next_set, daemon=True).start()
                return
        self.press(action)

    def press(self, action: str) -> str:
        """What a short press does. Runs the work in the background so a
        slow photo change never blocks the next button press."""
        f = self.frame
        if not f.display.screen_on:
            # any button wakes a sleeping screen, rather than skipping a photo unseen
            did = "screen on"
            threading.Thread(target=f.set_screen, args=(True,), daemon=True).start()
        elif action == "next":
            did = "next step" if not f.player.playing else "stop video"
            threading.Thread(target=f.rotate, daemon=True).start()
        elif action == "previous":
            did = "previous photo"
            threading.Thread(target=f.previous, daemon=True).start()
        else:
            did = "screen off"
            threading.Thread(target=f.set_screen, args=(False,), daemon=True).start()
        self._record(action, did)
        return did

    def _record(self, button: str, action: str) -> None:
        self.last = {"button": button, "action": action, "t": time.time()}
        log.info("button: %s -> %s", button, action)

    # ---------- shutdown ----------
    def shutdown(self) -> None:
        """Show a 'shutting down' card, then power off cleanly."""
        f = self.frame
        f.show_power_card(restarting=False)
        if shutil.which("systemctl"):
            time.sleep(1.5)
            r = subprocess.run(["systemctl", "poweroff"], capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                log.warning("shutdown failed: %s (re-run install.sh to allow it)", (r.stderr or r.stdout).strip())

    def status(self) -> dict:
        ok, why = available()
        c = self.frame.cfg.snapshot()
        return {
            "available": ok, "why": why, "enabled": bool(c.get("buttons_enabled")), "active": bool(self._devices),
            "error": self.error, "last": self.last,
            "pins": {a: c.get(f"button_{a}_pin") for a in ACTIONS},
            "physical": {a: PHYSICAL.get(c.get(f"button_{a}_pin")) for a in ACTIONS},
        }
