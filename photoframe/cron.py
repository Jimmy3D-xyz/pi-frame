"""Simplified 3-field cron, matching esp32-photoframe's `rotate_cron` format.

Each rule is ``"minute hour day-of-week"``. Supported syntax per field:
``*``, ``a``, ``a-b``, ``*/n``, ``a-b/n`` and comma lists. Day-of-week 0 and 7
both mean Sunday (7 is valid inside ranges, e.g. ``5-7`` = Fri-Sun).
Evaluated in local time (the process TZ, which config.apply_timezone() sets).
"""

from __future__ import annotations

import time
from dataclasses import dataclass


class CronError(ValueError):
    pass


def _parse_field(text: str, lo: int, hi: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"empty list item in '{text}'")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError(f"bad step '{step_s}'")
            step = int(step_s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"bad range '{part}'")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
            if step != 1:  # "5/10" means 5,15,25,... up to hi
                end = hi
        else:
            raise CronError(f"bad value '{part}'")
        if start < lo or end > hi or start > end:
            raise CronError(f"'{part}' out of range {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class CronRule:
    minutes: frozenset[int]
    hours: frozenset[int]
    dows: frozenset[int]  # 0=Sunday..6=Saturday

    @classmethod
    def parse(cls, expr: str) -> "CronRule":
        fields = expr.split()
        if len(fields) != 3:
            raise CronError(f"'{expr}': expected 3 fields (minute hour day-of-week)")
        dows = _parse_field(fields[2], 0, 7)
        dows = frozenset(0 if d == 7 else d for d in dows)
        return cls(_parse_field(fields[0], 0, 59), _parse_field(fields[1], 0, 23), dows)

    def matches(self, tm: time.struct_time) -> bool:
        dow = (tm.tm_wday + 1) % 7  # struct_time: Monday=0 -> cron: Sunday=0
        return tm.tm_min in self.minutes and tm.tm_hour in self.hours and dow in self.dows


def parse_rules(exprs: list[str]) -> list[CronRule]:
    if not isinstance(exprs, list) or not 1 <= len(exprs) <= 7:
        raise CronError("rotate_cron must be a list of 1 to 7 rules")
    return [CronRule.parse(str(e)) for e in exprs]


def next_fire(rules: list[CronRule], after: float) -> float | None:
    """Epoch seconds of the first minute boundary strictly after `after`
    that matches any rule. Searches up to 8 days ahead."""
    t = (int(after) // 60 + 1) * 60
    for _ in range(8 * 24 * 60):
        tm = time.localtime(t)
        if any(r.matches(tm) for r in rules):
            return float(t)
        t += 60
    return None
