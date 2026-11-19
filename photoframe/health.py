"""Behind-the-scenes information for the Maintenance tab: system, power,
storage, network, display and library health, plus an in-memory log of the
frame's recent activity.

Everything here is read-only and cheap; each function tolerates missing
tools/files (e.g. on a development PC) by returning None for that item.
"""

from __future__ import annotations

import collections
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

STARTED = time.time()


# ---------------------------------------------------------------- event log
class EventLog(logging.Handler):
    """Keeps the frame's recent log lines in memory for the web page."""

    def __init__(self, size: int = 400):
        super().__init__(level=logging.INFO)
        self.events: collections.deque = collections.deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.events.append({
                "t": record.created,
                "level": record.levelname.lower(),
                "area": record.name.split(".")[-1],
                "msg": record.getMessage(),
            })
        except Exception:  # noqa: BLE001  (logging must never raise)
            pass

    def recent(self, limit: int = 200, warnings_only: bool = False) -> list[dict]:
        items = [e for e in self.events if not warnings_only or e["level"] in ("warning", "error", "critical")]
        return items[-limit:][::-1]


EVENTS = EventLog()


def install_event_log() -> None:
    logger = logging.getLogger("photoframe")
    if EVENTS not in logger.handlers:
        logger.addHandler(EVENTS)
        if logger.level == logging.NOTSET or logger.level > logging.INFO:
            logger.setLevel(logging.INFO)


# ---------------------------------------------------------------- helpers
def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip("\x00\n ")
    except OSError:
        return None


def _run(args: list[str], timeout: float = 5) -> str | None:
    if not shutil.which(args[0]):
        return None
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


# ---------------------------------------------------------------- system
def system_info() -> dict:
    os_name = None
    osr = _read("/etc/os-release") or ""
    m = re.search(r'^PRETTY_NAME="?([^"\n]+)', osr, re.M)
    if m:
        os_name = m.group(1)
    uptime = None
    up = _read("/proc/uptime")
    if up:
        uptime = int(float(up.split()[0]))
    mem = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            mem[k] = int(v.split()[0]) * 1024
    try:
        load = os.getloadavg()
    except OSError:
        load = None
    return {
        "hostname": socket.gethostname(),
        "model": _read("/proc/device-tree/model") or platform.machine(),
        "os": os_name or platform.platform(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "uptime_s": uptime,
        "app_uptime_s": int(time.time() - STARTED),
        "load": [round(x, 2) for x in load] if load else None,
        "cpus": os.cpu_count(),
        "mem_total": mem.get("MemTotal"),
        "mem_available": mem.get("MemAvailable"),
    }


# ---------------------------------------------------------------- power
THROTTLE_BITS = {
    0: ("under_voltage_now", "Under-voltage right now"),
    1: ("freq_capped_now", "CPU speed capped right now"),
    2: ("throttled_now", "Throttled right now"),
    3: ("soft_temp_limit_now", "Temperature limit active right now"),
    16: ("under_voltage_since_boot", "Under-voltage has happened since boot"),
    17: ("freq_capped_since_boot", "CPU speed was capped since boot"),
    18: ("throttled_since_boot", "Throttled since boot"),
    19: ("soft_temp_limit_since_boot", "Temperature limit was reached since boot"),
}


def decode_throttled(value: int) -> dict:
    flags = {key: bool(value & (1 << bit)) for bit, (key, _) in THROTTLE_BITS.items()}
    notes = [text for bit, (_, text) in THROTTLE_BITS.items() if value & (1 << bit)]
    return {"raw": f"0x{value:x}", "flags": flags, "notes": notes}


def power_info() -> dict:
    temp = _read("/sys/class/thermal/thermal_zone0/temp")
    raw = _read("/sys/devices/platform/soc/soc:firmware/get_throttled")
    if raw is None:
        out = _run(["vcgencmd", "get_throttled"])
        if out and "=" in out:
            raw = out.strip().split("=", 1)[1]
    throttled = None
    if raw:
        try:
            throttled = decode_throttled(int(raw, 16) if raw.lower().startswith("0x") else int(raw, 16))
        except ValueError:
            throttled = None
    return {
        "cpu_temp_c": round(int(temp) / 1000, 1) if temp and temp.lstrip("-").isdigit() else None,
        "throttled": throttled,
    }


# ---------------------------------------------------------------- storage
SD_ERROR_RE = re.compile(r"(mmc\d.*(error|timeout|timed out))|EXT4-fs error|I/O error|Buffer I/O error|"
                         r"Journal has aborted|Remounting filesystem read-only", re.I)


def root_read_only(mounts_file: str = "/proc/1/mounts") -> bool | None:
    """Is the SD card's root file system read-only for the whole system?

    Reads PID 1's view: the frame's own view (/proc/self/mounts) always shows
    / as read-only, because its service is sandboxed with ProtectSystem=strict.
    None if that can't be read."""
    text = _read(mounts_file)
    if not text:
        return None
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 4 and f[1] == "/" and f[2] not in ("rootfs", "overlay", "tmpfs"):
            return "ro" in f[3].split(",")
    return None


def data_writable(data_dir: Path) -> bool:
    """Can the frame actually save to its own folder right now?"""
    probe = data_dir / ".write-test"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
        return True
    except OSError:
        return False


def storage_info(data_dir: Path) -> dict:
    du = shutil.disk_usage(data_dir)
    root_ro = root_read_only()
    errors = None
    kernel = _run(["journalctl", "-k", "-b", "--no-pager", "-o", "short-monotonic", "-q"], timeout=10)
    if kernel is not None:
        errors = [line.strip() for line in kernel.splitlines() if SD_ERROR_RE.search(line)][-20:]
    return {
        "total": du.total,
        "used": du.used,
        "free": du.free,
        "root_read_only": root_ro,
        "data_writable": data_writable(data_dir),
        "kernel_errors": errors,  # None = couldn't read the kernel log
    }


# ---------------------------------------------------------------- summary
def summarise(h: dict) -> list[dict]:
    """One line per area with ok / warn / bad, for the top of the tab."""
    out = []

    def add(area, level, text):
        out.append({"area": area, "level": level, "text": text})

    net = h.get("network") or {}
    if net.get("mode") == "disabled":
        add("Network", "ok", "Wi-Fi monitoring isn't running on this device")
    elif net.get("mode") == "setup":
        add("Network", "bad", "Wi-Fi setup hotspot is running: the frame isn't on your network")
    elif net.get("mode") != "online":
        add("Network", "bad", "Not connected")
    elif net.get("link", {}).get("last_ok") is False:
        add("Network", "warn", "Connected, but the last link check got no answer")
    else:
        band = net.get("band")
        rejoins = len(net.get("rejoins") or [])
        text = f"Connected{' on ' + band if band else ''}"
        if rejoins:
            text += f"; re-joined {rejoins} time{'s' if rejoins != 1 else ''} since start"
        add("Network", "warn" if rejoins > 3 else "ok", text)

    pw = h.get("power") or {}
    t = pw.get("cpu_temp_c")
    th = (pw.get("throttled") or {}).get("flags") or {}
    if th.get("under_voltage_now") or th.get("throttled_now"):
        add("Power", "bad", "Under-voltage or throttling right now: check the power supply")
    elif th.get("under_voltage_since_boot"):
        add("Power", "warn", "Under-voltage has happened since boot: the power supply may be too weak")
    elif t is not None and t >= 80:
        add("Power", "warn", f"Running hot ({t:.0f} °C)")
    elif t is not None or pw.get("throttled"):
        add("Power", "ok", "Power supply OK" + (f", {t:.0f} °C" if t is not None else ""))

    st = h.get("storage") or {}
    free_gb = (st.get("free") or 0) / 1e9
    if st.get("data_writable") is False:
        add("SD card", "bad", "The frame can't save to the SD card: it may have been locked read-only after errors")
    elif st.get("root_read_only"):
        add("SD card", "bad", "The SD card has been locked read-only after errors: replace it")
    elif st.get("kernel_errors"):
        add("SD card", "bad", f"{len(st['kernel_errors'])} SD card error(s) since boot: consider replacing the card")
    elif free_gb < 1:
        add("SD card", "warn", f"Only {free_gb:.1f} GB free")
    elif st.get("kernel_errors") is None:
        add("SD card", "ok", f"{free_gb:.1f} GB free (card error log not readable here)")
    else:
        add("SD card", "ok", f"{free_gb:.1f} GB free, no card errors since boot")

    od = h.get("onedrive") or {}
    if od.get("links"):
        links = od["links"]
        gone = [l for l in links if l.get("connected") is False]
        bad = [l for l in links if l.get("last_ok") is False]
        if gone:
            add("Synced folders", "bad", f"{gone[0].get('source', 'A service')} isn't connected: its album won't update")
        elif bad:
            add("Synced folders", "warn", f"Last sync of {bad[0].get('source', '')} '{bad[0]['folder'] or '(everything)'}': "
                                          f"{bad[0]['last_result']}")
        else:
            n = sum(l.get("files") or 0 for l in links)
            add("Synced folders", "ok", f"{len(links)} folder{'s' if len(links) != 1 else ''} in sync ({n} files)")

    b = h.get("backup")
    if b:
        add("Backups", b["level"], b["text"])

    lib = h.get("library") or {}
    failed = [j for j in lib.get("jobs", []) if j["status"] == "failed"]
    if failed:
        add("Videos", "warn", f"{len(failed)} video(s) couldn't be converted")
    return out
