"""First run: the welcome wizard on a frame written from the ready-made image."""
import base64
import importlib.util
import json
import shutil
import time
from pathlib import Path

import pytest

from photoframe import firstrun
from photoframe.config import ConfigStore
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.network import HOTSPOT_IP, WifiStatus
from photoframe.storage import Storage
from photoframe.web import create_app

PANEL = (800, 480)
REPO = Path(__file__).resolve().parents[1]
NO_USERS = "root:x:0:0:root:/root:/bin/bash\nphotoframe:x:999:999::/var/lib/pi-photoframe:/usr/sbin/nologin\n"
WITH_USER = NO_USERS + "alex:x:1000:1000:,,,:/home/alex:/bin/bash\n"


class FakeWifi:
    def __init__(self, mode="setup"):
        self.status = WifiStatus(mode=mode, hotspot_ssid="PiFrame-AB12", hotspot_password="12345678")
        self.last_portal_hit = 0.0
        self.connected = None

    def connect(self, ssid, pw, hidden=False):
        self.connected = (ssid, pw, hidden)

    def scan(self):
        return []


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    monkeypatch.setattr(firstrun, "login_users", lambda passwd="/etc/passwd": [])
    (tmp_path / firstrun.MARKER).touch()
    cfg = ConfigStore(tmp_path)
    frame = Frame(cfg, Storage(tmp_path, PANEL), FileDisplay(PANEL, str(tmp_path / "f.png")), tmp_path)
    frame.wifi = FakeWifi()
    app = create_app(frame)
    asked = []
    app.config["SETUP_HELPER"] = lambda d, req: (asked.append(req), {"state": "done"})[1]
    return frame, app.test_client(), tmp_path, asked


ANSWERS = {"name": "Living room", "country": "AU", "timezone": "Australia/Melbourne", "password": "pagepass1",
           "wifi": {"ssid": "Home", "password": "wifipass1"}}


def test_new_frame_opens_the_wizard(env):
    frame, client, tmp, _ = env
    for path in ("/", "/setup"):
        assert b"Welcome to your Pi Frame" in client.get(path, headers={"Host": HOTSPOT_IP}).data
    st = client.get("/api/setup", headers={"Host": HOTSPOT_IP}).get_json()
    assert st["first_run"] and st["wifi_mode"] == "setup" and not st["has_login"]


def test_wizard_sets_everything_then_joins_wifi(env):
    frame, client, tmp, asked = env
    r = client.post("/api/setup", json={**ANSWERS, "ssh": {"username": "alex", "password": "sshpass12"}},
                    headers={"Host": HOTSPOT_IP})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["joining"] == "Home" and frame.wifi.connected == ("Home", "wifipass1", False)
    req = asked[0]
    assert req["action"] == "first_run" and req["country"] == "AU" and req["timezone"] == "Australia/Melbourne"
    assert req["ssh"]["username"] == "alex" and req["ssh"]["hash"].startswith("$6$")
    assert "sshpass12" not in json.dumps(req)  # only the hash goes to the helper
    assert frame.cfg.get("device_name") == "Living room" and frame.cfg.get("http_password") == "pagepass1"
    assert not firstrun.is_first_run(tmp)
    # done: the normal page, with its password, and the wizard can't run again
    frame.wifi.status = WifiStatus(mode="online", ssid="Home")
    assert client.get("/").status_code == 401
    auth = {"Authorization": "Basic " + base64.b64encode(b"x:pagepass1").decode()}
    assert b"Welcome to your Pi Frame" not in client.get("/", headers=auth).data
    assert client.post("/api/setup", json=ANSWERS, headers=auth).status_code == 409


def test_wizard_answers_are_checked(env):
    frame, client, tmp, asked = env
    for bad, msg in [({"country": "Australia"}, "country"), ({"timezone": "Mars/Base"}, "time zone"),
                     ({"name": ""}, "name"), ({"password": "short"}, "8 characters"),
                     ({"ssh": {"username": "root", "password": "longenough"}}, "username"),
                     ({"ssh": {"username": "alex", "password": "x"}}, "SSH password"),
                     ({"wifi": {"ssid": "Home", "password": "short"}}, "Wi-Fi passwords")]:
        r = client.post("/api/setup", json={**ANSWERS, **bad}, headers={"Host": HOTSPOT_IP})
        assert r.status_code == 400 and msg in r.get_json()["message"], (bad, r.get_json())
    assert not asked and firstrun.is_first_run(tmp)


def test_wizard_over_ethernet_needs_no_wifi_and_helper_problems_are_shown(env):
    frame, client, tmp, asked = env
    frame.wifi.status = WifiStatus(mode="online", ssid="Wired connection 1")
    answers = {k: v for k, v in ANSWERS.items() if k != "wifi"}
    app_helper = client.application.config
    app_helper["SETUP_HELPER"] = lambda d, req: {"state": "failed", "message": "Unknown time zone"}
    r = client.post("/api/setup", json=answers)
    assert r.status_code == 400 and "time zone" in r.get_json()["message"] and firstrun.is_first_run(tmp)
    assert not frame.cfg.get("http_password")  # nothing half-applied: the page stays open to try again
    app_helper["SETUP_HELPER"] = lambda d, req: {"state": "done"}
    r = client.post("/api/setup", json=answers)
    assert r.status_code == 200 and "joining" not in r.get_json() and frame.wifi.connected is None


def test_frames_set_up_before_never_see_the_wizard(env):
    frame, client, tmp, _ = env
    firstrun.finish(tmp)
    frame.wifi.status = WifiStatus(mode="online", ssid="Home")
    assert b"Welcome to your Pi Frame" not in client.get("/").data
    assert client.post("/api/setup", json=ANSWERS).status_code == 409


def test_login_users(tmp_path):
    p = tmp_path / "passwd"
    p.write_text(NO_USERS + "nobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n")
    assert firstrun.login_users(str(p)) == []
    p.write_text(WITH_USER)
    assert firstrun.login_users(str(p)) == ["alex"]
    with pytest.raises(firstrun.SetupError, match="already has a login"):
        firstrun.check({**ANSWERS, "ssh": {"username": "sam", "password": "longenough"}}, passwd=str(p))


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_password_hash():
    h = firstrun.hash_password("sshpass12")
    assert h.startswith("$6$") and "sshpass12" not in h


# ---------------------------------------------------------------- the root helper
def helper():
    spec = importlib.util.spec_from_file_location("system_helper", REPO / "deploy" / "system_helper.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_helper_adds_a_login_only_when_there_is_none(tmp_path, monkeypatch):
    h = helper()
    calls = []

    class R:
        returncode = 0
        stdout = stderr = ""

    monkeypatch.setattr(h, "_run", lambda cmd, **kw: (calls.append(cmd), R())[1])
    p = tmp_path / "passwd"
    p.write_text(WITH_USER)
    assert "already has a login" in h.add_login("sam", "$6$abc$def", passwd=str(p))
    assert not calls
    p.write_text(NO_USERS)
    assert "can't be used" in h.add_login("root", "$6$abc$def", passwd=str(p))
    assert "can't be used" in h.add_login("sam", "plaintext", passwd=str(p))
    assert h.add_login("sam", "$6$abc$def", passwd=str(p)) is None
    useradd = next(c for c in calls if c[0] == "useradd")
    assert useradd[-1] == "sam" and "$6$abc$def" in useradd and "sudo" in useradd[useradd.index("--groups") + 1]
    assert ["systemctl", "enable", "--now", "ssh"] in calls


def test_helper_wifi_country_without_raspi_config(tmp_path, monkeypatch):
    h = helper()
    monkeypatch.setattr(h.shutil, "which", lambda name: None)
    monkeypatch.setattr(h, "_run", lambda cmd, **kw: None)
    (tmp_path / "cmdline.txt").write_text("console=tty3 root=PARTUUID=x cfg80211.ieee80211_regdom=GB quiet\n")
    h.set_wifi_country("AU", boot=tmp_path)
    line = (tmp_path / "cmdline.txt").read_text()
    assert line.count("ieee80211_regdom") == 1 and "cfg80211.ieee80211_regdom=AU" in line and "\n" not in line[:-1]


# ---------------------------------------------------------------- reset to a fresh frame
def test_reset_needs_password_and_confirmation(env):
    frame, client, tmp, asked = env
    firstrun.finish(tmp)
    frame.wifi.status = WifiStatus(mode="online", ssid="Home")
    assert client.post("/api/maintenance/reset", json={"confirm": "RESET"}).status_code == 403
    frame.cfg.update({"http_password": "pagepass1"})
    auth = {"Authorization": "Basic " + base64.b64encode(b"x:pagepass1").decode()}
    assert client.post("/api/maintenance/reset", json={"confirm": "yes"}, headers=auth).status_code == 400
    r = client.post("/api/maintenance/reset", json={"confirm": "reset", "remove_logins": True}, headers=auth)
    assert r.status_code == 200 and frame.notice == "reset"
    for _ in range(60):
        if asked:
            break
        time.sleep(0.1)
    assert asked == [{"action": "factory_reset", "remove_logins": True}]


def test_helper_reset_erases_data_wifi_and_optionally_logins(tmp_path, monkeypatch):
    import getpass

    h = helper()
    data = tmp_path / "data"
    (data / "albums" / "Default").mkdir(parents=True)
    (data / "albums" / "Default" / "a.jpg").write_bytes(b"x")
    (data / "config.json").write_text("{}")
    (data / "cloud").mkdir()
    (data / "cloud" / "rclone.conf").write_text("[x]")
    calls = []

    class R:
        def __init__(self, out=""):
            self.returncode, self.stdout, self.stderr = 0, out, ""

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["nmcli", "-t", "-f"]:
            return R("u1:802-11-wireless:Home 5G\nu2:802-3-ethernet:Wired connection 1\nu3:802-11-wireless:Cafe\\:Guest\n")
        return R()

    monkeypatch.setattr(h, "_run", run)
    passwd = tmp_path / "passwd"
    passwd.write_text(WITH_USER)
    me = getpass.getuser()
    h.factory_reset(False, data=data, passwd=str(passwd), owner=me)
    assert sorted(p.name for p in data.iterdir()) == [".first-run"] and data.is_dir()
    assert ["nmcli", "connection", "delete", "uuid", "u1"] in calls and ["nmcli", "connection", "delete", "uuid", "u3"] in calls
    assert ["nmcli", "connection", "delete", "uuid", "u2"] not in calls  # the cable stays
    assert not any(c[0] == "userdel" for c in calls) and ["systemctl", "reboot"] in calls
    calls.clear()
    h.factory_reset(True, data=data, passwd=str(passwd), owner=me)
    assert ["userdel", "--remove", "alex"] in calls and ["systemctl", "disable", "ssh.service"] in calls
