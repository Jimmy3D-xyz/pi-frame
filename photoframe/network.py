"""Wi-Fi fallback: if the frame can't get online, it starts its own hotspot
and shows QR codes on screen so a phone can join it and pick a network.

Mirrors the firmware's provisioning AP, using NetworkManager (the default on
Raspberry Pi OS Bookworm and later) through `nmcli`. The service user is
allowed to manage networking by the polkit rule the installer adds.
"""

from __future__ import annotations

import collections
import errno
import logging
import os
import random
import select
import struct
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

HOTSPOT_CON = "photoframe-setup"
HOTSPOT_IP = "10.42.0.1"  # NetworkManager's default for shared (hotspot) mode
BOOT_GRACE = 120  # seconds to wait for Wi-Fi after starting
HOTSPOT_CHANNEL = 6
SCAN_RETRIES, SCAN_RETRY_WAIT = 3, 4  # seconds; before the setup hotspot, when the first look finds nothing
NO_SAVED_GRACE = 15  # ...when there's no saved Wi-Fi network to wait for
# A frame that's never been set up (first run, no saved Wi-Fi): straight to the
# setup hotspot, unless it's already online by cable.
MAX_CONNECTING = 300  # never cut off a connection attempt sooner than this
DROPOUT_GRACE = 300  # how long a lost connection may last before setup mode
RETRY_EVERY = 180  # in setup mode, retry saved networks this often...
IDLE_BEFORE_RETRY = 180  # ...but only if nobody has used the setup page lately
LINK_CHECK_EVERY = 15  # while on Wi-Fi, check traffic actually flows this often
FIRST_LINK_CHECK = 5  # first check this many seconds after the frame starts
LINK_FAILS_BEFORE_ACTION = 2  # consecutive failed checks before re-joining
REJOIN_COOLDOWN = 60  # don't re-join more often than this
PROBE_TARGETS_EXTRA = ("1.1.1.1", "8.8.8.8")  # tried after the router


def _nmcli(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout, check=False)


def _split(line: str) -> list[str]:
    """Split nmcli -t output: ':' separated, with '\\:' escaping."""
    out, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def local_addresses() -> list[str]:
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


def frame_urls() -> list[str]:
    urls = [f"http://{socket.gethostname()}.local/"]
    urls += [f"http://{ip}/" for ip in local_addresses() if ip != HOTSPOT_IP]
    return urls


# ---------------------------------------------------------------- link checks
def default_gateway(iface: str, route_file: str = "/proc/net/route") -> str | None:
    try:
        lines = Path(route_file).read_text().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        f = line.split()
        if len(f) >= 3 and f[0] == iface and f[1] == "00000000":
            return socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
    return None


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def _icmp_ping(host: str, timeout: float, raw: bool) -> bool:
    kind = socket.SOCK_RAW if raw else socket.SOCK_DGRAM
    with socket.socket(socket.AF_INET, kind, socket.IPPROTO_ICMP) as s:
        ident = os.getpid() & 0xFFFF
        header = struct.pack("!BBHHH", 8, 0, 0, ident, 1)
        payload = b"pi-photoframe"
        packet = struct.pack("!BBHHH", 8, 0, _checksum(header + payload), ident, 1) + payload
        s.sendto(packet, (host, 0))
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            ready, _, _ = select.select([s], [], [], left)
            if not ready:
                return False
            data, addr = s.recvfrom(1024)
            if addr[0] != host:
                continue
            if raw:
                data = data[(data[0] & 0x0F) * 4:]  # strip the IP header
            if data and data[0] == 0:  # echo reply
                return True
        return False


def _tcp_probe(host: str, timeout: float) -> bool:
    """Needs no permissions: any answer (accepted OR refused) proves traffic
    flows to that host; only silence means it doesn't."""
    for port in (53, 80):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except ConnectionRefusedError:
            return True
        except OSError as e:
            if e.errno in (errno.ECONNREFUSED, errno.ECONNRESET):
                return True
    return False


_probe_method: str | None = None


def reachable(host: str, timeout: float = 2.0) -> bool:
    """Ping `host`, using the first method this system allows."""
    global _probe_method
    methods = [_probe_method] if _probe_method else ["icmp", "icmp-raw", "tcp"]
    for m in methods:
        try:
            if m == "icmp":
                ok = _icmp_ping(host, timeout, raw=False)
            elif m == "icmp-raw":
                ok = _icmp_ping(host, timeout, raw=True)
            else:
                ok = _tcp_probe(host, timeout)
        except PermissionError:
            continue
        except OSError:
            ok = False
        if _probe_method is None:
            _probe_method = m
            log.info("link check method: %s", m)
        return ok
    return False


def _iw_link(iface: str) -> dict:
    """Signal (dBm) and link speeds from `iw`, if it's installed."""
    try:
        out = subprocess.run(["iw", "dev", iface, "link"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    info = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("signal:"):
            try:
                info["signal_dbm"] = int(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("tx bitrate:"):
            info["tx_rate"] = line.split(":", 1)[1].strip().split(" MBit/s")[0] + " Mbit/s"
        elif line.startswith("rx bitrate:"):
            info["rx_rate"] = line.split(":", 1)[1].strip().split(" MBit/s")[0] + " Mbit/s"
    return info


def probe_method() -> str | None:
    return _probe_method


@dataclass
class Network:
    ssid: str
    signal: int
    secure: bool


@dataclass
class WifiStatus:
    mode: str = "unknown"  # online | offline | setup | connecting | disabled
    ssid: str = ""
    hotspot_ssid: str = ""
    hotspot_password: str = ""
    message: str = ""
    networks: list[Network] = field(default_factory=list)


class WifiManager:
    """Watches connectivity and runs the setup hotspot. `on_change` is called
    with the WifiStatus whenever the mode changes, so the frame can redraw."""

    def __init__(self, iface: str = "wlan0", on_change=None):
        self.iface = iface
        self.on_change = on_change or (lambda status: None)
        self.status = WifiStatus()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.last_portal_hit = 0.0
        self.link_ok: bool | None = None
        self.link = {"last_check": None, "last_ok": None, "fails_in_a_row": 0, "checks": 0, "failed_checks": 0}
        self.rejoins: collections.deque = collections.deque(maxlen=20)
        self.online_since: float | None = None
        self.new_frame = False  # set by __main__ on a frame that hasn't been set up yet
        suffix = Path(f"/sys/class/net/{iface}/address")
        try:
            tail = suffix.read_text().strip().replace(":", "")[-4:].upper()
        except OSError:
            tail = f"{random.randint(0, 0xFFFF):04X}"
        self.hotspot_ssid = f"PiFrame-{tail}"

    @staticmethod
    def available(iface: str = "wlan0") -> bool:
        return shutil.which("nmcli") is not None and Path(f"/sys/class/net/{iface}").exists()

    # ---------- queries ----------
    def active(self) -> list[tuple[str, str, str]]:
        """(name, type, device) of active connections, excluding loopback."""
        r = _nmcli("-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active")
        rows = [tuple(_split(line)[:3]) for line in r.stdout.splitlines() if line]
        return [row for row in rows if row[1] != "loopback" and row[2] != "lo"]

    def device_state(self) -> str:
        """NetworkManager's state for the Wi-Fi device, e.g. 'connected',
        'disconnected', 'connecting (getting IP configuration)'."""
        r = _nmcli("-t", "-f", "DEVICE,STATE", "device")
        for line in r.stdout.splitlines():
            parts = _split(line)
            if len(parts) >= 2 and parts[0] == self.iface:
                return parts[1]
        return ""

    def saved_wifi(self) -> list[str]:
        """Wi-Fi networks NetworkManager knows (not counting the setup hotspot)."""
        r = _nmcli("-t", "-f", "NAME,TYPE", "connection", "show")
        out = []
        for line in r.stdout.splitlines():
            f = _split(line)
            if len(f) >= 2 and f[1] == "802-11-wireless" and f[0] != HOTSPOT_CON:
                out.append(f[0])
        return out

    def _on_wifi(self, name: str) -> bool:
        return any(n == name and d == self.iface for n, _t, d in self.active())

    def online(self) -> tuple[bool, str]:
        """Connected to a real network (Wi-Fi client or Ethernet)?"""
        for name, ctype, _dev in self.active():
            if name == HOTSPOT_CON:
                continue
            if ctype in ("802-11-wireless", "wifi", "802-3-ethernet", "ethernet"):
                return True, name
        return False, ""

    def check_now(self) -> list[dict]:
        """Ping each target now and report each result (for the Maintenance tab)."""
        gw = default_gateway(self.iface)
        out = []
        for label, host in ([("Router", gw)] if gw else []) + [("Internet (1.1.1.1)", "1.1.1.1"),
                                                             ("Internet (8.8.8.8)", "8.8.8.8")]:
            t = time.monotonic()
            ok = reachable(host)
            out.append({"target": label, "host": host, "ok": ok, "ms": round((time.monotonic() - t) * 1000)})
        return out

    def details(self) -> dict:
        """Current connection: access point, band, signal, speed, addresses."""
        info: dict = {"iface": self.iface, "gateway": default_gateway(self.iface)}
        ap = next((a for a in self.access_points() if a["active"]), None)
        if ap:
            info.update(bssid=ap["bssid"], freq=ap["freq"], signal_pct=ap["signal"], ssid=ap["ssid"],
                        band="5 GHz" if ap["freq"] >= 5000 else "2.4 GHz" if ap["freq"] else None)
        link = _iw_link(self.iface)
        info.update(link)
        try:
            out = subprocess.run(["ip", "-4", "-o", "addr", "show", self.iface], capture_output=True, text=True,
                                 timeout=5).stdout
            info["ip"] = out.split("inet ", 1)[1].split("/", 1)[0] if "inet " in out else None
        except (OSError, subprocess.TimeoutExpired, IndexError):
            info["ip"] = None
        return info

    def traffic_ok(self) -> bool:
        """True if the router or a public DNS server answers."""
        targets = [t for t in (default_gateway(self.iface),) if t] + list(PROBE_TARGETS_EXTRA)
        return any(reachable(t) for t in targets)

    def access_points(self) -> list[dict]:
        """All visible access points, from NetworkManager's last scan."""
        r = _nmcli("-t", "-f", "ACTIVE,BSSID,FREQ,SIGNAL,SSID", "device", "wifi", "list",
                   "ifname", self.iface, "--rescan", "no")
        aps = []
        for line in r.stdout.splitlines():
            p = _split(line)
            if len(p) < 5:
                continue
            try:
                freq = int(p[2].split()[0])
                signal = int(p[3])
            except (ValueError, IndexError):
                freq, signal = 0, 0
            aps.append({"active": p[0] == "yes", "bssid": p[1], "freq": freq, "signal": signal, "ssid": p[4]})
        return aps

    def rejoin(self, con_name: str) -> str:
        """Re-join the network, preferring a different access point (e.g. the
        router's other band). Returns a description for the log."""
        aps = self.access_points()
        current = next((a for a in aps if a["active"]), None)
        if current:
            others = [a for a in aps if a["ssid"] == current["ssid"] and a["bssid"] != current["bssid"]]
            # prefer 5 GHz, then the strongest signal
            others.sort(key=lambda a: (a["freq"] >= 5000, a["signal"]), reverse=True)
            if others:
                target = others[0]
                r = _nmcli("connection", "up", "id", con_name, "ap", target["bssid"], timeout=60)
                if r.returncode == 0:
                    return f"moved from {current['bssid']} ({current['freq']} MHz) to {target['bssid']} ({target['freq']} MHz)"
                log.warning("couldn't switch access point: %s", (r.stderr or r.stdout).strip())
        r = _nmcli("connection", "up", "id", con_name, timeout=60)
        return "re-joined the same access point" if r.returncode == 0 else f"re-join failed: {(r.stderr or r.stdout).strip()}"

    def scan(self) -> list[Network]:
        _nmcli("device", "wifi", "rescan", "ifname", self.iface, timeout=20)
        time.sleep(3)
        r = _nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "ifname", self.iface, timeout=20)
        best: dict[str, Network] = {}
        for line in r.stdout.splitlines():
            parts = _split(line)
            if len(parts) < 3 or not parts[0] or parts[0] == self.hotspot_ssid:
                continue
            try:
                sig = int(parts[1])
            except ValueError:
                sig = 0
            net = Network(parts[0], sig, parts[2] not in ("", "--"))
            if parts[0] not in best or sig > best[parts[0]].signal:
                best[parts[0]] = net
        return sorted(best.values(), key=lambda n: -n.signal)

    # ---------- hotspot ----------
    def _set(self, **kw) -> None:
        with self._lock:
            changed = kw.get("mode") and kw["mode"] != self.status.mode
            for k, v in kw.items():
                setattr(self.status, k, v)
        if changed or "message" in kw:
            try:
                self.on_change(self.status)
            except Exception as e:  # noqa: BLE001
                log.warning("wifi on_change failed: %s", e)

    def start_hotspot(self, reason: str) -> None:
        # Scan first: a Pi's single radio can't scan while it's an AP, so this
        # list is what the setup page offers. On a new frame the radio has only
        # just been switched on and the first look often finds nothing yet.
        networks = self.scan()
        for attempt in range(SCAN_RETRIES):
            if networks:
                break
            log.info("no Wi-Fi networks found yet; looking again (%d)", attempt + 1)
            _nmcli("radio", "wifi", "on")
            time.sleep(SCAN_RETRY_WAIT)
            networks = self.scan()
        password = self.status.hotspot_password or f"{random.randint(0, 99_999_999):08d}"
        # 2.4 GHz channel 6: allowed everywhere, even before the Pi is told its
        # country. Left to choose, NetworkManager may pick 5 GHz, where a Pi
        # with no country set may only listen: the hotspot "starts" but no phone sees it.
        args = ("device", "wifi", "hotspot", "ifname", self.iface, "con-name", HOTSPOT_CON,
                "ssid", self.hotspot_ssid, "band", "bg", "channel", str(HOTSPOT_CHANNEL), "password", password)
        r = _nmcli(*args)
        if r.returncode != 0:
            # a new Pi keeps its Wi-Fi radio switched off until it's told the country
            log.warning("setup hotspot didn't start (%s); switching the Wi-Fi radio on and trying again",
                        r.stderr.strip())
            _nmcli("radio", "wifi", "on")
            time.sleep(3)
            r = _nmcli(*args)
        if r.returncode != 0:
            log.error("could not start setup hotspot: %s", r.stderr.strip())
            self._set(mode="offline", message="Couldn't start the setup hotspot.")
            return
        # Don't let NetworkManager bring the hotspot back by itself after a reboot,
        # and use plain WPA2: newer NetworkManager may offer WPA3 (SAE), which
        # the Pi's Wi-Fi chip and many phones don't manage as a hotspot.
        _nmcli("connection", "modify", HOTSPOT_CON, "connection.autoconnect", "no",
               "802-11-wireless-security.key-mgmt", "wpa-psk", "802-11-wireless-security.proto", "rsn",
               "802-11-wireless-security.pairwise", "ccmp", "802-11-wireless-security.group", "ccmp",
               "802-11-wireless-security.pmf", "disable")
        _nmcli("connection", "up", HOTSPOT_CON, timeout=30)
        log.info("setup hotspot %s started", self.hotspot_ssid)
        self._set(
            mode="setup", hotspot_ssid=self.hotspot_ssid, hotspot_password=password,
            networks=networks, message=reason,
        )

    def stop_hotspot(self) -> None:
        _nmcli("connection", "down", HOTSPOT_CON)
        _nmcli("connection", "delete", HOTSPOT_CON)

    # ---------- connecting ----------
    def connect(self, ssid: str, password: str, hidden: bool = False) -> threading.Thread:
        """Called from the setup page. Runs in the background because the
        phone is about to lose its connection to the hotspot."""

        def work():
            self._set(mode="connecting", ssid=ssid, message=f"Connecting to {ssid}…")
            time.sleep(2)  # let the web response reach the phone first
            self.stop_hotspot()
            _nmcli("device", "wifi", "rescan", "ifname", self.iface, timeout=20)
            time.sleep(4)
            args = ["device", "wifi", "connect", ssid, "ifname", self.iface]
            if password:
                args += ["password", password]
            if hidden:
                args += ["hidden", "yes"]
            r = _nmcli(*args, timeout=60)
            ok, name = self.online()
            if r.returncode == 0 and ok:
                log.info("connected to %s", ssid)
                self._set(mode="online", ssid=name, message="")
                return
            detail = (r.stderr or r.stdout).strip().splitlines()
            log.warning("could not connect to %s: %s", ssid, detail)
            # Remove the failed profile so it doesn't keep being retried.
            _nmcli("connection", "delete", ssid)
            self.start_hotspot(f"Couldn't connect to {ssid}. Check the password and try again.")

        t = threading.Thread(target=work, daemon=True)
        t.start()
        return t  # lets callers (and tests) wait for the attempt to finish

    def _retry_saved(self) -> bool:
        """Leave setup mode briefly and let NetworkManager try saved networks."""
        log.info("retrying saved Wi-Fi networks")
        self.stop_hotspot()
        for _ in range(12):
            time.sleep(5)
            ok, name = self.online()
            if ok:
                self._set(mode="online", ssid=name, message="")
                return True
        return False

    # ---------- monitor ----------
    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # Clean up a hotspot left over from before a restart.
        if any(n == HOTSPOT_CON for n, _, _ in self.active()):
            self.stop_hotspot()
        started = time.monotonic()
        offline_since: float | None = None
        not_trying = 0
        link_fails = 0
        next_link_check = time.monotonic() + FIRST_LINK_CHECK
        last_rejoin = -1e9
        last_retry = time.monotonic()
        no_saved: bool | None = None  # checked once, the first time Wi-Fi isn't up
        while not self._stop.is_set():
            mode = self.status.mode
            if mode == "connecting":
                self._stop.wait(5)
                continue
            if mode == "setup":
                idle = time.time() - self.last_portal_hit > IDLE_BEFORE_RETRY
                if idle and time.monotonic() - last_retry > RETRY_EVERY:
                    last_retry = time.monotonic()
                    if not self._retry_saved():  # same password, so the QR stays valid
                        self.start_hotspot("Still couldn't connect to your saved Wi-Fi.")
                self._stop.wait(15)
                continue

            ok, name = self.online()
            now = time.monotonic()
            if ok:
                not_trying = 0
                if offline_since is not None or mode != "online":
                    waited = now - (offline_since if offline_since is not None else started)
                    log.info("Wi-Fi online via %s (after %.0fs)", name, waited)
                offline_since = None
                if mode != "online" or self.status.ssid != name:
                    self.online_since = time.time()
                    self._set(mode="online", ssid=name, message="")
                # Connected isn't the same as working: check traffic actually
                # flows, and re-join if it doesn't (seen on routers that park a
                # new client on a band that doesn't pass traffic for minutes).
                if self._on_wifi(name) and now >= next_link_check:
                    next_link_check = now + LINK_CHECK_EVERY
                    good = self.traffic_ok()
                    self.link["checks"] += 1
                    self.link["last_check"] = time.time()
                    self.link["last_ok"] = good
                    if good:
                        if link_fails:
                            log.info("link check: traffic flowing again")
                        link_fails = 0
                        self.link_ok = True
                    else:
                        link_fails += 1
                        self.link["failed_checks"] += 1
                        self.link_ok = False
                    self.link["fails_in_a_row"] = link_fails
                    if not good:
                        log.warning("link check: connected to %s but router and internet don't answer (%d)", name, link_fails)
                        if link_fails >= LINK_FAILS_BEFORE_ACTION and now - last_rejoin >= REJOIN_COOLDOWN:
                            last_rejoin = now
                            link_fails = 0
                            result = self.rejoin(name)
                            self.rejoins.append({"t": time.time(), "result": result})
                            log.warning("link check: re-joining Wi-Fi: %s", result)
                            next_link_check = time.monotonic() + min(10, LINK_CHECK_EVERY)  # re-check soon
            else:
                if offline_since is None:
                    offline_since = now
                    if mode == "online":
                        self._set(mode="offline", message="Wi-Fi connection lost")
                booting = now - started < BOOT_GRACE + 5
                limit = BOOT_GRACE if booting or mode == "unknown" else DROPOUT_GRACE
                if mode == "unknown" and no_saved is None:
                    no_saved = not self.saved_wifi()
                    if no_saved:
                        log.info("no saved Wi-Fi networks: starting the setup hotspot %s",
                                 "now (a new frame)" if self.new_frame else "soon")
                if mode == "unknown" and no_saved:
                    limit = 0 if self.new_frame else min(limit, NO_SAVED_GRACE)
                waited = now - offline_since
                state = self.device_state()
                # Don't cut off a connection NetworkManager is still making
                # (slow DHCP, weak signal, DFS channel scans...), up to a point.
                still_trying = state.startswith("connecting") and waited < MAX_CONNECTING
                if waited < limit or still_trying:
                    not_trying = 0
                    if waited >= limit:
                        log.info("Wi-Fi still connecting (%s) after %.0fs; waiting", state, waited)
                else:
                    not_trying += 1
                if not_trying >= 2:  # two checks in a row, so one odd nmcli answer can't trigger it
                    not_trying = 0
                    reason = (
                        "Let's get your frame on Wi-Fi." if mode == "unknown" and no_saved
                        else "The frame couldn't connect to Wi-Fi." if mode == "unknown"
                        else "The frame lost its Wi-Fi connection."
                    )
                    log.warning("no Wi-Fi after %.0fs (device %s); starting setup hotspot", waited, state or "unknown")
                    self.start_hotspot(reason)
                    last_retry = time.monotonic()
                    offline_since = None
            self._stop.wait(5)
