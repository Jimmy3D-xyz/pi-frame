"""First run: setting up a frame written from the ready-made Pi Frame image.

The image has no Wi-Fi settings, no login and SSH switched off. It leaves a
marker (`.first-run` in the data folder); while it's there the frame starts
its setup hotspot straight away and its web page opens the welcome wizard:
Wi-Fi, the frame's name, country, time zone, a page password and, if wanted,
an SSH login.

The frame runs unprivileged, so the system settings (Wi-Fi country, time
zone, the login) are handed to the root helper (deploy/system_helper.py,
action "first_run"). The helper creates a login only while the Pi has none,
so this can't be used to add one later.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from zoneinfo import available_timezones

MARKER = ".first-run"
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
RESERVED = {"root", "photoframe", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail", "news", "uucp",
            "proxy", "www-data", "backup", "list", "irc", "nobody", "systemd-network", "messagebus", "avahi",
            "sshd", "admin", "sudo", "adm", "video", "audio", "users", "staff"}
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


class SetupError(ValueError):
    pass


def is_first_run(data_dir: Path) -> bool:
    return (data_dir / MARKER).exists()


def finish(data_dir: Path) -> None:
    (data_dir / MARKER).unlink(missing_ok=True)


def login_users(passwd: str = "/etc/passwd") -> list[str]:
    """People's accounts on the Pi (not system ones)."""
    out = []
    try:
        lines = Path(passwd).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        f = line.split(":")
        if len(f) < 7:
            continue
        try:
            uid = int(f[2])
        except ValueError:
            continue
        if 1000 <= uid < 60000 and not f[6].endswith(("nologin", "false")):
            out.append(f[0])
    return out


def hash_password(password: str) -> str:
    """A SHA-512 crypt hash for /etc/shadow (Python 3.13 has no crypt module)."""
    r = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=password + "\n", capture_output=True,
                       text=True, timeout=20)
    h = r.stdout.strip()
    if r.returncode != 0 or not h.startswith("$6$"):
        raise SetupError("couldn't prepare the login password")
    return h


def check(answers: dict, passwd: str = "/etc/passwd") -> dict:
    """Validate the wizard's answers. Returns a clean copy."""
    out: dict = {}
    name = str(answers.get("name") or "").strip()
    if not name or len(name) > 40 or not name.isprintable():
        raise SetupError("Give the frame a name (up to 40 characters).")
    out["name"] = name
    country = str(answers.get("country") or "").strip().upper()
    if not COUNTRY_RE.match(country):
        raise SetupError("Choose the country the frame is in (it decides which Wi-Fi channels it may use).")
    out["country"] = country
    tz = str(answers.get("timezone") or "").strip()
    if tz not in available_timezones():
        raise SetupError("Choose a time zone.")
    out["timezone"] = tz
    pw = str(answers.get("password") or "")
    if pw and not 8 <= len(pw) <= 128:
        raise SetupError("The page password needs at least 8 characters.")
    out["password"] = pw
    ssh = answers.get("ssh")
    if ssh:
        user = str(ssh.get("username") or "").strip()
        spw = str(ssh.get("password") or "")
        if not USERNAME_RE.match(user) or user in RESERVED:
            raise SetupError("For the SSH login, use a username of lower-case letters and numbers, "
                             "starting with a letter (e.g. alex).")
        if len(spw) < 8:
            raise SetupError("The SSH password needs at least 8 characters.")
        if login_users(passwd):
            raise SetupError("This Pi already has a login, so another can't be added here.")
        out["ssh"] = {"username": user, "password": spw}
    wifi = answers.get("wifi")
    if wifi:
        ssid = str(wifi.get("ssid") or "").strip()
        wpw = str(wifi.get("password") or "")
        if not ssid or len(ssid) > 32:
            raise SetupError("Choose a Wi-Fi network (names are up to 32 characters).")
        if wpw and not 8 <= len(wpw) <= 63:
            raise SetupError("Wi-Fi passwords are 8 to 63 characters.")
        out["wifi"] = {"ssid": ssid, "password": wpw, "hidden": bool(wifi.get("hidden"))}
    return out


def ask_helper(data_dir: Path, request: dict, timeout: float = 60) -> dict:
    """Hand a request to the root helper and wait for its answer."""
    d = data_dir / "system"
    d.mkdir(exist_ok=True)
    status = d / "status.json"
    before = status.stat().st_mtime if status.exists() else 0
    tmp = d / "request.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds a password hash
    with os.fdopen(fd, "w") as f:
        json.dump({**request, "t": time.time()}, f)
    os.replace(tmp, d / "request.json")
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(0.5)
        try:
            if status.stat().st_mtime > before:
                st = json.loads(status.read_text())
                if st.get("state") in ("done", "failed"):
                    return st
        except (OSError, ValueError):
            pass
    (d / "request.json").unlink(missing_ok=True)
    return {"state": "failed", "message": "The system helper didn't answer, so the Wi-Fi country, time zone "
                                          "and login weren't set. Re-run install.sh over SSH to repair it."}
