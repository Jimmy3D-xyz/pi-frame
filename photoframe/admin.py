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

import base64
import hashlib
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

# Settings that belong to setting the frame up. The web page only changes them
# with the admin options unlocked; apps (Home Assistant, the image server) use
# the firmware API as before, so requests that don't come from a browser can.
SETUP_KEYS = frozenset({
    "device_name", "timezone", "ha_url", "demo_mode",
    "image_url", "access_token", "http_header_key", "http_header_value",
    "display_orientation", "display_rotation_deg", "display_hw_fade",
    "buttons_enabled", "button_previous_pin", "button_next_pin", "button_screen_pin",
    "button_hold_shutdown", "button_hold_seconds",
    "presence_enabled", "presence_pin", "presence_timeout_minutes", "presence_schedule_only",
})

# Stored secrets the settings API never shows: it answers MASK when one is set,
# and a MASK sent back leaves it as it is.
MASKED_KEYS = frozenset({"access_token", "http_header_value", "openai_api_key", "google_api_key"})
MASK = "********"


def from_browser(headers) -> bool:
    """Browsers say where a request came from; apps and scripts don't."""
    return bool(headers.get("Origin") or headers.get("Sec-Fetch-Site") or headers.get("Sec-Fetch-Mode"))


# Paths that need the admin unlocked (any method).
ADMIN_PREFIXES = (
    "/api/maintenance/update", "/api/maintenance/ssh", "/api/maintenance/reset", "/api/maintenance/wifi",
    "/api/maintenance/diagnostics", "/api/maintenance/restart", "/api/maintenance/reboot",
    "/api/backup", "/api/cloud", "/api/admin/page-password",
    "/api/wifi/connect",  # changing network (free while the setup hotspot is up)
    "/api/display/mode", "/api/display/detect",  # screen resolution (restarts the Pi)
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


# ---------------------------------------------------------------- stored passwords
# The admin and page passwords are kept scrambled (scrypt), so a settings file
# or a backup doesn't give them away. Checking one takes ~0.1 s on a Pi 4; the
# page password comes with every request, so a correct one is remembered (as a
# keyed digest, in memory only) until it changes.
PASSWORD_KEYS = ("admin_password", "http_password")
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
_PREFIX = "scrypt$"
_cache_key = secrets.token_bytes(32)
_known_good: dict[str, str] = {}  # stored hash -> digest of the password that matched it


def is_hashed(value: str) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def hash_password(password: str) -> str:
    if not password or is_hashed(password):
        return password
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"{_PREFIX}{_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def password_matches(given: str, stored: str) -> bool:
    """Does `given` match the stored password (scrambled, or plain from an old file)?"""
    if not given or not stored:
        return False
    if not is_hashed(stored):
        return hmac.compare_digest(given.encode(), stored.encode())
    digest = hmac.new(_cache_key, given.encode(), hashlib.sha256).hexdigest()
    known = _known_good.get(stored)
    if known is not None and hmac.compare_digest(known, digest):
        return True
    try:
        _, n, r, p, salt, want = stored.split("$")
        h = hashlib.scrypt(given.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                           dklen=len(base64.b64decode(want)), maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    if not hmac.compare_digest(h, base64.b64decode(want)):
        return False
    if len(_known_good) > 16:
        _known_good.clear()
    _known_good[stored] = digest
    return True


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
