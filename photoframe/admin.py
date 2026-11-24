"""The admin login, and the checks that keep other websites out.

The frame's page has two levels:

- everyday use (photos, albums, the slideshow, the screen): open to anyone on
  the home network, or behind the optional page password (HTTP basic auth,
  which the image server and Home Assistant use too);
- admin (updates, backups, SSH, reset, Wi-Fi, synced-folder sign-ins and the
  passwords): unlocked with the admin password for 15 minutes at a time.

An unlock is a random token in a cookie marked SameSite=Strict, so browsers
never send it with a request another website starts. It isn't kept on disk:
restarting the frame locks it.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import socket
import threading
import time
from urllib.parse import urlsplit

COOKIE = "pf_admin"
IDLE_SECONDS = 15 * 60  # locks again after this long without being used

# Settings only the admin may change (through /api/config), and that the image
# server's X-Config-Payload may never change.
ADMIN_KEYS = frozenset({
    "http_password", "admin_password",
    "update_auto", "update_time", "update_repo",
    "backup_auto", "backup_where", "backup_folder", "backup_kind", "backup_every", "backup_time", "backup_keep",
})

# Paths that need the admin unlocked (any method).
ADMIN_PREFIXES = (
    "/api/maintenance/update", "/api/maintenance/ssh", "/api/maintenance/reset", "/api/maintenance/wifi",
    "/api/maintenance/diagnostics", "/api/maintenance/restart", "/api/maintenance/reboot",
    "/api/backup", "/api/cloud", "/api/admin/page-password",
    "/api/wifi/connect",  # changing network (free while the setup hotspot is up)
)


def is_admin_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in ADMIN_PREFIXES)


class Throttle:
    """Slows down password guessing: after 3 wrong tries from one address,
    each further try has to wait (5 s, 10 s, 20 s, … up to 5 minutes)."""

    FREE_TRIES = 3
    MAX_WAIT = 300

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._fails: dict[str, tuple[int, float]] = {}  # address -> (wrong tries, may try again at)

    def wait(self, who: str) -> int:
        """Seconds until `who` may try again (0 = now)."""
        with self._lock:
            n, until = self._fails.get(who, (0, 0.0))
            return max(0, int(until - self._clock() + 0.999))

    def failed(self, who: str) -> None:
        with self._lock:
            n, _ = self._fails.get(who, (0, 0.0))
            n += 1
            delay = 0 if n < self.FREE_TRIES else min(self.MAX_WAIT, 5 * 2 ** (n - self.FREE_TRIES))
            self._fails[who] = (n, self._clock() + delay)
            if len(self._fails) > 1000:  # don't grow without end
                self._fails.pop(next(iter(self._fails)))

    def succeeded(self, who: str) -> None:
        with self._lock:
            self._fails.pop(who, None)


class AdminSessions:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._tokens: dict[str, float] = {}  # token -> last used
        self.throttle = Throttle(clock)

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            self._tokens[token] = self._clock()
        return token

    def valid(self, token: str | None, touch: bool = True) -> bool:
        if not token:
            return False
        with self._lock:
            self._prune()
            if token not in self._tokens:
                return False
            if touch:
                self._tokens[token] = self._clock()
            return True

    def left(self, token: str | None) -> int:
        """Seconds until this unlock runs out (0 = locked)."""
        with self._lock:
            t = self._tokens.get(token or "")
            return 0 if t is None else max(0, int(t + IDLE_SECONDS - self._clock()))

    def end(self, token: str | None) -> None:
        with self._lock:
            self._tokens.pop(token or "", None)

    def end_all(self, keep: str | None = None) -> None:
        with self._lock:
            self._tokens = {t: v for t, v in self._tokens.items() if t == keep}

    def _prune(self) -> None:
        now = self._clock()
        for t in [t for t, used in self._tokens.items() if now - used > IDLE_SECONDS]:
            del self._tokens[t]


def password_matches(given: str, wanted: str) -> bool:
    return bool(given) and bool(wanted) and hmac.compare_digest(given.encode(), wanted.encode())


# ---------------------------------------------------------------- other websites

def _host_only(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("["):  # [::1]:80
        return host[1:host.find("]")] if "]" in host else host
    if host.count(":") == 1:
        host = host.split(":")[0]
    return host.rstrip(".")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def own_names() -> set[str]:
    """The names the frame answers to: its hostname (and .local), localhost,
    plus any in PHOTOFRAME_ALLOWED_HOSTS (comma-separated)."""
    name = socket.gethostname().lower()
    names = {"localhost", name, f"{name}.local"}
    extra = os.environ.get("PHOTOFRAME_ALLOWED_HOSTS", "")
    names |= {_host_only(h) for h in extra.split(",") if h.strip()}
    return names


def host_allowed(host: str, names: set[str] | None = None) -> bool:
    """Is this request addressed to the frame itself? Stops DNS rebinding: a
    website pointing its own name at the frame's address, so the browser treats
    the frame as part of that site. IP addresses are always fine (a website
    can't make the browser think an IP address is its own); so are Tailscale
    names (*.ts.net)."""
    h = _host_only(host)
    if not h:
        return False
    if _is_ip(h) or h.endswith(".ts.net"):
        return True
    return h in (names if names is not None else own_names())


def cross_site(origin: str | None, fetch_site: str | None, host: str) -> bool:
    """Did another website start this request? Browsers say where a request
    came from (Origin, Sec-Fetch-Site); apps and scripts send neither, and are
    let through."""
    if fetch_site and fetch_site.lower() == "cross-site":
        return True
    if not origin:
        return False
    if origin == "null":
        return True
    try:
        o = urlsplit(origin)
    except ValueError:
        return True
    return (o.netloc or "").lower() != (host or "").lower()
