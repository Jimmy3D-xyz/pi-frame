import io
import os
import stat
import time
from pathlib import Path

import pytest
from PIL import Image

from photoframe import network, screens
from photoframe.config import ConfigStore
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.network import HOTSPOT_IP, WifiManager, WifiStatus
from photoframe.storage import Storage
from photoframe.web import create_app

PANEL = (1920, 1080)
_real_sleep = time.sleep


def jpeg(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 60)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    cfg = ConfigStore(tmp_path)
    cfg.update({"transition": "none"})
    disp = FileDisplay(PANEL, str(tmp_path / "frame.png"))
    frame = Frame(cfg, Storage(tmp_path, PANEL), disp, tmp_path)
    return frame, create_app(frame).test_client(), tmp_path


def test_wifi_qr_escaping():
    assert screens.wifi_qr_payload('My;Net', 'pa:ss"1') == 'WIFI:T:WPA;S:My\\;Net;P:pa\\:ss\\"1;;'
    assert screens.wifi_qr_payload("Open", "") == "WIFI:T:nopass;S:Open;;"


def test_cards_render_both_orientations():
    for size in [(1920, 1080), (1080, 1920), (800, 480)]:
        assert screens.wifi_setup_card(size, "PiFrame-AB12", "12345678", "http://10.42.0.1/setup", "x").size == size
        assert screens.empty_card(size, ["http://piframe.local/"]).size == size


def test_empty_card_then_upload_shows_photo(env):
    frame, client, tmp = env
    ok, _ = frame.rotate()
    assert not ok and frame.notice == "empty"
    assert client.get("/api/status").get_json()["notice"] == "empty"
    client.post(
        "/api/upload",
        data={"album": "Default", "image": (io.BytesIO(jpeg(1600, 1200)), "a.jpg")},
        content_type="multipart/form-data",
    )
    for _ in range(150):
        if frame.notice is None and frame.state.get("current"):
            break
        _real_sleep(0.1)
    assert frame.notice is None and frame.state["current"]["filepath"] == "Default/a.jpg"


class FakeWifi:
    def __init__(self):
        self.status = WifiStatus(mode="setup", hotspot_ssid="PiFrame-AB12", hotspot_password="12345678",
                                 networks=[network.Network("Home", 80, True)])
        self.last_portal_hit = 0.0
        self.connected = None

    def connect(self, ssid, pw, hidden=False):
        self.connected = (ssid, pw, hidden)

    def scan(self):
        return self.status.networks


def test_captive_portal_and_connect(env):
    frame, client, _ = env
    frame.cfg.update({"http_password": "secret"})
    frame.wifi = FakeWifi()
    frame.on_wifi_change(frame.wifi.status)
    assert frame.notice == "wifi"
    assert not frame.rotate()[0]  # rotation paused while the setup card shows
    # phone connectivity probe -> redirected to setup
    r = client.get("/generate_204", headers={"Host": "connectivitycheck.gstatic.com"})
    assert r.status_code == 302 and r.headers["Location"] == f"http://{HOTSPOT_IP}/setup"
    # setup page and wifi API work without the frame password
    assert client.get("/setup", headers={"Host": HOTSPOT_IP}).status_code == 200
    assert client.get("/api/wifi/networks", headers={"Host": HOTSPOT_IP}).get_json()[0]["ssid"] == "Home"
    assert client.post("/api/wifi/connect", json={"ssid": "Home", "password": "short"},
                       headers={"Host": HOTSPOT_IP}).status_code == 400
    r = client.post("/api/wifi/connect", json={"ssid": "Home", "password": "longenough"}, headers={"Host": HOTSPOT_IP})
    assert r.status_code == 200 and frame.wifi.connected == ("Home", "longenough", False)
    # the normal UI still needs the password
    assert client.get("/", headers={"Host": HOTSPOT_IP}).status_code == 401
    # back online -> card cleared
    frame.wifi.status = WifiStatus(mode="online", ssid="Home")
    frame.on_wifi_change(frame.wifi.status)
    assert frame.notice is None
    assert client.get("/generate_204", headers={"Host": "connectivitycheck.gstatic.com"}).status_code == 401


FAKE_NMCLI = r'''#!/usr/bin/env python3
import json, sys, os
state_path = os.environ["FAKE_NM_STATE"]
st = json.load(open(state_path))
a = sys.argv[1:]
with open(state_path + ".log", "a") as lf:  # separate file: read-only calls never rewrite state
    lf.write(json.dumps(a) + "\n")
def save():
    tmp = state_path + ".tmp%d" % os.getpid()
    json.dump(st, open(tmp, "w")); os.replace(tmp, state_path)
if "ACTIVE,BSSID,FREQ,SIGNAL,SSID" in a:
    for ap in st.get("aps", []):
        print(ap)
elif a[:3] == ["-t", "-f", "DEVICE,STATE"]:
    print("wlan0:" + st.get("state", "disconnected")); print("lo:connected (externally)")
elif a[:4] == ["-t", "-f", "NAME,TYPE,DEVICE", "connection"]:
    for c in st["active"]:
        print(":".join(c))
elif "list" in a and "wifi" in a:
    print("Home:80:WPA2"); print("Cafe\\:Guest:40:"); print("Home:60:WPA2")
elif a[:3] == ["device", "wifi", "hotspot"]:
    st["active"] = [["photoframe-setup", "802-11-wireless", "wlan0"]]
elif a[:2] == ["connection", "down"]:
    st["active"] = []
elif a[:3] == ["device", "wifi", "connect"]:
    if st.get("good_password") in a:
        st["active"] = [[a[3], "802-11-wireless", "wlan0"]]
    else:
        save(); print("Error: Secrets were required", file=sys.stderr); sys.exit(4)
    save()
if a[:3] == ["device", "wifi", "hotspot"] or a[:2] == ["connection", "down"]:
    save()
'''


def test_wifi_manager_with_fake_nmcli(tmp_path, monkeypatch):
    import json
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    nm = bin_dir / "nmcli"
    nm.write_text(FAKE_NMCLI)
    nm.chmod(nm.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "nm.json"
    state.write_text(json.dumps({"active": [], "good_password": "correcthorse"}))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_NM_STATE", str(state))
    monkeypatch.setattr(network.time, "sleep", lambda s: None)
    monkeypatch.setattr(network, "BOOT_GRACE", 0)

    seen = []
    mgr = WifiManager("wlan0", on_change=lambda s: seen.append((s.mode, s.message)))
    nets = mgr.scan()
    assert [n.ssid for n in nets] == ["Home", "Cafe:Guest"] and nets[0].secure and not nets[1].secure

    mgr.start_hotspot("The frame couldn't connect to Wi-Fi.")
    assert mgr.status.mode == "setup" and len(mgr.status.hotspot_password) == 8
    pw = mgr.status.hotspot_password

    def run_connect(password):
        mgr.connect("Home", password).join(timeout=20)

    run_connect("wrongpass1")
    assert mgr.status.mode == "setup" and "Couldn't connect to Home" in mgr.status.message
    assert mgr.status.hotspot_password == pw  # QR on screen stays valid
    run_connect("correcthorse")
    assert mgr.status.mode == "online" and mgr.online() == (True, "Home")
    assert ("connecting", "Connecting to Home…") in seen


def test_welcome_screen_then_photos(env):
    frame, client, tmp = env
    client.post(
        "/api/upload",
        data={"album": "Default", "image": (io.BytesIO(jpeg(1600, 1200)), "b.jpg")},
        content_type="multipart/form-data",
    )
    frame.cfg.update({"welcome_seconds": 1})
    frame.wifi = FakeWifi()
    frame.wifi.status = WifiStatus(mode="unknown")
    frame.startup()
    assert frame.notice == "welcome"
    assert client.get("/api/status").get_json()["notice"] == "welcome"
    frame.wifi.status = WifiStatus(mode="online", ssid="Home")
    frame.on_wifi_change(frame.wifi.status)
    assert frame.notice == "welcome"  # redrawn with the IP, not dismissed
    for _ in range(150):
        if frame.notice is None and frame.state.get("current"):
            break
        _real_sleep(0.1)
    assert frame.notice is None and frame.state["current"]["filepath"].endswith("b.jpg")


def test_wifi_setup_replaces_welcome(env):
    frame, client, tmp = env
    frame.cfg.update({"welcome_seconds": 1})
    frame.wifi = FakeWifi()
    frame.startup()
    frame.on_wifi_change(frame.wifi.status)  # FakeWifi starts in setup mode
    _real_sleep(1.3)
    assert frame.notice == "wifi"  # the welcome timer didn't take over


def test_welcome_can_be_turned_off(env):
    frame, client, _ = env
    assert client.post("/api/config", json={"welcome_seconds": 0}).status_code == 200
    assert client.post("/api/config", json={"welcome_seconds": 9999}).status_code == 400
    frame.startup()
    for _ in range(200):  # wait for the screen to be drawn (slow machines take longer)
        if frame.notice:
            break
        _real_sleep(0.05)
    assert frame.notice == "empty"  # straight to content (no photos here)


def _fake_nm(tmp_path, monkeypatch, state):
    import json
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    nm = bin_dir / "nmcli"
    nm.write_text(FAKE_NMCLI)
    nm.chmod(nm.stat().st_mode | stat.S_IEXEC)
    st = tmp_path / "nm.json"
    st.write_text(json.dumps(state))
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_NM_STATE", str(st))
    return st


def test_hotspot_waits_while_still_connecting(tmp_path, monkeypatch):
    """A slow connection at boot must not be cut off by the setup hotspot."""
    import json
    import threading
    st = _fake_nm(tmp_path, monkeypatch, {"active": [], "state": "connecting (getting IP configuration)"})
    monkeypatch.setattr(network, "BOOT_GRACE", 0)
    monkeypatch.setattr(network.time, "sleep", lambda s: None)
    mgr = WifiManager("wlan0")
    assert mgr.device_state().startswith("connecting")
    orig_wait = mgr._stop.wait
    monkeypatch.setattr(mgr._stop, "wait", lambda t: orig_wait(0.05))
    t = threading.Thread(target=mgr.run, daemon=True)
    t.start()
    _real_sleep(0.6)
    assert mgr.status.mode != "setup"  # still waiting for NetworkManager
    # the connection completes: frame goes online, no hotspot was ever started
    data = json.loads(st.read_text())
    data.update(active=[["netplan-wlan0-Home", "802-11-wireless", "wlan0"]], state="connected")
    tmp = st.with_suffix(".new")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, st)
    for _ in range(100):
        if mgr.status.mode == "online":
            break
        _real_sleep(0.05)
    mgr.stop()
    assert mgr.status.mode == "online" and mgr.status.ssid == "netplan-wlan0-Home"
    calls = [json.loads(l) for l in Path(str(st) + ".log").read_text().splitlines()]
    assert not any(c[:3] == ["device", "wifi", "hotspot"] for c in calls)


def test_hotspot_starts_when_really_disconnected(tmp_path, monkeypatch):
    import threading
    _fake_nm(tmp_path, monkeypatch, {"active": [], "state": "disconnected"})
    monkeypatch.setattr(network, "BOOT_GRACE", 0)
    monkeypatch.setattr(network.time, "sleep", lambda s: None)
    mgr = WifiManager("wlan0")
    orig_wait = mgr._stop.wait
    monkeypatch.setattr(mgr._stop, "wait", lambda t: orig_wait(0.05))
    threading.Thread(target=mgr.run, daemon=True).start()
    for _ in range(100):
        if mgr.status.mode == "setup":
            break
        _real_sleep(0.05)
    mgr.stop()
    assert mgr.status.mode == "setup"
    # always 2.4 GHz channel 6 with plain WPA2: a new Pi (no country set yet) can't
    # transmit on 5 GHz, and WPA3 hotspots trip up the Pi's chip and many phones
    import json

    calls = [json.loads(line) for line in (tmp_path / "nm.json.log").read_text().splitlines()]
    hotspot = next(c for c in calls if c[:3] == ["device", "wifi", "hotspot"])
    assert hotspot[hotspot.index("band") + 1] == "bg" and hotspot[hotspot.index("channel") + 1] == "6"
    modify = next(c for c in calls if c[:2] == ["connection", "modify"])
    assert modify[modify.index("802-11-wireless-security.key-mgmt") + 1] == "wpa-psk"
    assert modify[modify.index("802-11-wireless-security.pmf") + 1] == "disable"


# ---------------- link check: connected but no traffic ----------------
def test_default_gateway_parsing(tmp_path):
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
        "wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0\n")
    assert network.default_gateway("wlan0", str(route)) == "192.168.1.1"
    assert network.default_gateway("eth0", str(route)) is None


def test_probes_answer_and_silence():
    # a closed port on this machine answers "refused": that proves traffic flows
    assert network._tcp_probe("127.0.0.1", 1.0) is True
    assert network.reachable("127.0.0.1") is True


def test_probe_silence_means_no_traffic(monkeypatch):
    # On a home network, an unreachable host gives silence (a timeout), or the
    # kernel reports no route. Both must count as "no traffic".
    import socket as _s

    def timeout(*a, **k):
        raise _s.timeout("timed out")

    monkeypatch.setattr(network.socket, "create_connection", timeout)
    assert network._tcp_probe("192.168.1.1", 0.1) is False

    def no_route(*a, **k):
        raise OSError(113, "No route to host")

    monkeypatch.setattr(network.socket, "create_connection", no_route)
    assert network._tcp_probe("192.168.1.1", 0.1) is False


AP_LIST = [
    "yes:02\\:00\\:00\\:00\\:00\\:40:2427 MHz:72:Home 5G",
    "no:02\\:00\\:00\\:00\\:00\\:3F:5240 MHz:80:Home 5G",
    "no:AA\\:BB\\:CC\\:DD\\:EE\\:FF:2412 MHz:40:Neighbour",
]


def test_rejoin_prefers_other_band(tmp_path, monkeypatch):
    import json
    st = _fake_nm(tmp_path, monkeypatch, {"active": [["netplan-wlan0-Home 5G", "802-11-wireless", "wlan0"]],
                                          "state": "connected", "aps": AP_LIST})
    mgr = WifiManager("wlan0")
    aps = mgr.access_points()
    assert aps[0] == {"active": True, "bssid": "02:00:00:00:00:40", "freq": 2427, "signal": 72, "ssid": "Home 5G"}
    msg = mgr.rejoin("netplan-wlan0-Home 5G")
    assert "to 02:00:00:00:00:3F (5240 MHz)" in msg
    calls = [json.loads(l) for l in Path(str(st) + ".log").read_text().splitlines()]
    assert ["connection", "up", "id", "netplan-wlan0-Home 5G", "ap", "02:00:00:00:00:3F"] in calls


def test_link_check_rejoins_when_no_traffic(tmp_path, monkeypatch):
    """Reproduces the real problem: connected to the 2.4 GHz radio, but no
    traffic flows. The frame should notice and move to 5 GHz within ~2 checks."""
    import json
    import threading
    st = _fake_nm(tmp_path, monkeypatch, {"active": [["netplan-wlan0-Home 5G", "802-11-wireless", "wlan0"]],
                                          "state": "connected", "aps": AP_LIST})
    monkeypatch.setattr(network, "LINK_CHECK_EVERY", 0)
    monkeypatch.setattr(network, "FIRST_LINK_CHECK", 0)
    monkeypatch.setattr(network, "REJOIN_COOLDOWN", 1000)  # so exactly one re-join happens
    mgr = WifiManager("wlan0")
    checks = []
    monkeypatch.setattr(mgr, "traffic_ok", lambda: checks.append(1) or False)
    orig_wait = mgr._stop.wait
    monkeypatch.setattr(mgr._stop, "wait", lambda t: orig_wait(0.02))
    threading.Thread(target=mgr.run, daemon=True).start()
    log_file = Path(str(st) + ".log")
    for _ in range(200):
        calls = [json.loads(l) for l in log_file.read_text().splitlines()] if log_file.exists() else []
        if any(c[:2] == ["connection", "up"] for c in calls):
            break
        _real_sleep(0.02)
    mgr.stop()
    _real_sleep(0.1)
    ups = [c for c in calls if c[:2] == ["connection", "up"]]
    assert ups == [["connection", "up", "id", "netplan-wlan0-Home 5G", "ap", "02:00:00:00:00:3F"]]
    assert len(checks) >= network.LINK_FAILS_BEFORE_ACTION and mgr.link_ok is False
    assert mgr.status.mode == "online"  # never went to the setup hotspot


def test_link_check_leaves_healthy_wifi_alone(tmp_path, monkeypatch):
    import json
    import threading
    st = _fake_nm(tmp_path, monkeypatch, {"active": [["netplan-wlan0-Home 5G", "802-11-wireless", "wlan0"]],
                                          "state": "connected", "aps": AP_LIST})
    monkeypatch.setattr(network, "LINK_CHECK_EVERY", 0)
    monkeypatch.setattr(network, "FIRST_LINK_CHECK", 0)
    mgr = WifiManager("wlan0")
    monkeypatch.setattr(mgr, "traffic_ok", lambda: True)
    orig_wait = mgr._stop.wait
    monkeypatch.setattr(mgr._stop, "wait", lambda t: orig_wait(0.02))
    threading.Thread(target=mgr.run, daemon=True).start()
    _real_sleep(0.8)
    mgr.stop()
    _real_sleep(0.1)
    calls = [json.loads(l) for l in Path(str(st) + ".log").read_text().splitlines()]
    assert not any(c[:2] == ["connection", "up"] for c in calls) and mgr.link_ok is True


def test_hotspot_looks_again_when_the_first_scan_finds_nothing(monkeypatch):
    """A new frame's radio has only just been switched on: the first look is often empty."""
    import subprocess

    mgr = WifiManager("wlan0")
    looks = iter([[], [], [network.Network("Home", 80, True)]])
    monkeypatch.setattr(mgr, "scan", lambda: next(looks))
    monkeypatch.setattr(network.time, "sleep", lambda s: None)
    calls = []
    monkeypatch.setattr(network, "_nmcli", lambda *a, **k: (calls.append(a), subprocess.CompletedProcess(a, 0, "", ""))[1])
    mgr.start_hotspot("test")
    assert mgr.status.mode == "setup" and [n.ssid for n in mgr.status.networks] == ["Home"]
    assert calls.count(("radio", "wifi", "on")) == 2
