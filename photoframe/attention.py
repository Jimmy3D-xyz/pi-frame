"""The on-screen notice: a small panel when the frame needs someone to look at it.

It's for problems that won't sort themselves out (a backup or update that
keeps failing, a synced folder whose sign-in ran out, the SD card filling up,
security updates not installing), taken from the Maintenance summary. A
problem has to last an hour before it's shown, so passing hiccups never are.
The panel only says where to look, not what's wrong, so visitors see nothing
private. "Hide until something new" (Maintenance) hides what's there now; a
different problem shows it again. It goes by itself once fixed.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

DELAY = 3600  # a problem must last this long before it's shown
CHECK_EVERY = 300

# Which Maintenance summary lines need a person (area -> the levels that count).
# Left out: the network (the frame has its own screens for that) and passing
# things like one failed video.
WATCH = {
    "SD card": {"warn", "bad"},
    "Power": {"bad"},
    "Synced folders": {"bad"},
    "Backups": {"warn", "bad"},
    "Updates": {"warn", "bad"},
    "Security updates": {"warn", "bad"},
}


def pick(summary: list[dict]) -> list[dict]:
    return [s for s in summary if s.get("level") in WATCH.get(s.get("area"), ())]


class Attention:
    def __init__(self, cfg, data_dir: Path, on_change=None, clock=time.time):
        self.cfg = cfg
        self.path = Path(data_dir) / "attention.json"
        self.on_change = on_change
        self.clock = clock
        self.source = None  # set by the web app: returns the Maintenance summary lines
        self._lock = threading.Lock()
        self._first_seen: dict[str, float] = {}
        self.items: list[dict] = []  # problems that have lasted DELAY
        self._hidden: set[str] = set(self._load().get("hidden", []))
        self._showing = False

    @staticmethod
    def key(item: dict) -> str:
        # by area and level, not the wording: "No backup for 5 days" becoming
        # "6 days" isn't something new
        return f"{item['area']}:{item['level']}"

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"hidden": sorted(self._hidden)}))
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("couldn't save the on-screen notice state: %s", e)

    def update(self, summary: list[dict]) -> None:
        now = self.clock()
        current = pick(summary)
        with self._lock:
            keys = {self.key(i) for i in current}
            self._first_seen = {k: t for k, t in self._first_seen.items() if k in keys}
            for k in keys:
                self._first_seen.setdefault(k, now)
            self.items = [dict(i, since=self._first_seen[self.key(i)]) for i in current
                          if now - self._first_seen[self.key(i)] >= DELAY]
            if self._hidden - keys:  # fixed problems no longer need hiding
                self._hidden &= keys
                self._save()
        self._changed()

    def visible(self) -> list[dict]:
        with self._lock:
            return [i for i in self.items if self.key(i) not in self._hidden]

    def showing(self) -> bool:
        return bool(self.cfg.get("attention_on_screen")) and bool(self.visible())

    def hide(self) -> None:
        """Hide what's there now, until something new comes up."""
        with self._lock:
            self._hidden |= {self.key(i) for i in self.items}
            self._save()
        self._changed()

    def show_again(self) -> None:
        with self._lock:
            self._hidden.clear()
            self._save()
        self._changed()

    def state(self) -> dict:
        with self._lock:
            items = list(self.items)
            hidden = [i for i in items if self.key(i) in self._hidden]
        return {"items": items, "hidden": bool(hidden) and len(hidden) == len(items),
                "on_screen": self.showing(), "enabled": bool(self.cfg.get("attention_on_screen"))}

    def _changed(self) -> None:
        now = self.showing()
        if now != self._showing:
            self._showing = now
            log.info("on-screen notice %s", "shown" if now else "cleared")
            if self.on_change:
                try:
                    self.on_change()
                except Exception as e:  # noqa: BLE001
                    log.warning("couldn't redraw for the on-screen notice: %s", e)

    def check(self) -> None:
        if self.source is None:
            return
        try:
            self.update(self.source())
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't check for the on-screen notice: %s", e)

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(CHECK_EVERY):
            self.check()
