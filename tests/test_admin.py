"""The admin login, and the checks that keep other websites out."""

import base64
import json
import socket

import pytest

from photoframe import admin as adm
from photoframe.cloud import CloudError, OneDrive
from photoframe.config import ConfigStore
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.storage import Storage
from photoframe.web import create_app

PANEL = (1920, 1080)
ADMIN = "admin-pass-1"


def is_pw(stored, plain):
    return adm.is_hashed(stored) and adm.password_matches(plain, stored)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    cfg = ConfigStore(tmp_path)
    frame = Frame(cfg, Storage(tmp_path, PANEL), FileDisplay(PANEL, str(tmp_path / "f.png")), tmp_path)
    clock = Clock()
    # the admin login runs on a clock the tests can move on
    real = adm.AdminSessions
    monkeypatch.setattr(adm, "AdminSessions", lambda: real(clock=clock))
    return frame, None, clock


def make(env):
    frame = env[0]
    app = create_app(frame)
    return app.test_client(), app.config["ADMIN_SESSIONS"]


def cookie(r) -> str:
    return next(h for h in r.headers.getlist("Set-Cookie") if h.startswith(adm.COOKIE + "="))


# ---------------------------------------------------------------- the admin login
def test_admin_options_need_an_unlock(env):
    frame, _, clock = env
    client, _ = make(env)
    # no admin password yet: admin options are refused, with a way forward
    r = client.get("/api/maintenance/ssh")
    assert r.status_code == 403 and r.get_json()["admin"] == "no-password"
    assert client.get("/api/admin").get_json() == {"unlocked": False, "seconds_left": 0, "minutes": 15,
                                                    "password_set": False, "page_password_set": False}
    # setting the first one needs no unlock, and unlocks
    assert client.post("/api/admin/password", json={"password": "short"}).status_code == 400
    r = client.post("/api/admin/password", json={"password": ADMIN})
    assert r.status_code == 200 and r.get_json()["unlocked"] and is_pw(frame.cfg.get("admin_password"), ADMIN)
    c = cookie(r)
    assert "HttpOnly" in c and "SameSite=Strict" in c and "Max-Age" not in c and "Expires" not in c
    assert client.get("/api/maintenance/ssh").status_code == 200
    # everyday things never needed it
    assert client.get("/api/status").status_code == 200 and client.post("/api/next-set").status_code != 403
    # lock: refused again
    assert client.post("/api/admin/lock").get_json()["unlocked"] is False
    r = client.get("/api/maintenance/ssh")
    assert r.status_code == 403 and r.get_json()["admin"] == "locked"
    # someone else can't change the password without unlocking
    other = client.application.test_client()
    assert other.post("/api/admin/password", json={"password": "another-pass"}).status_code == 403
    assert is_pw(frame.cfg.get("admin_password"), ADMIN)
    assert client.post("/api/admin/unlock", json={"password": ADMIN}).status_code == 200
    assert client.get("/api/maintenance/ssh").status_code == 200


def test_unlock_runs_out_after_15_minutes_unused(env):
    frame, _, clock = env
    frame.cfg.update({"admin_password": ADMIN})
    client, _ = make(env)
    client.post("/api/admin/unlock", json={"password": ADMIN})
    clock.t += 14 * 60
    assert client.get("/api/backup").status_code == 200  # using it keeps it open
    clock.t += 14 * 60
    assert client.get("/api/admin").get_json()["unlocked"] is True
    clock.t += 16 * 60
    assert client.get("/api/backup").status_code == 403
    assert client.get("/api/admin").get_json()["unlocked"] is False


def test_wrong_passwords_are_slowed_down(env):
    frame, _, clock = env
    frame.cfg.update({"admin_password": ADMIN})
    client, _ = make(env)
    for _ in range(2):
        assert client.post("/api/admin/unlock", json={"password": "guess"}).status_code == 403
    r = client.post("/api/admin/unlock", json={"password": "guess"})
    assert r.status_code == 403 and "Wait 5 seconds" in r.get_json()["message"]
    # even the right password has to wait
    assert client.post("/api/admin/unlock", json={"password": ADMIN}).status_code == 429
    clock.t += 5
    assert client.post("/api/admin/unlock", json={"password": "guess"}).status_code == 403
    clock.t += 9
    assert client.post("/api/admin/unlock", json={"password": ADMIN}).status_code == 429  # now 10 s
    clock.t += 1
    assert client.post("/api/admin/unlock", json={"password": ADMIN}).status_code == 200
    # and the count starts again
    assert client.post("/api/admin/unlock", json={"password": "guess"}).status_code == 403
    assert client.post("/api/admin/unlock", json={"password": "guess"}).status_code == 403


def test_page_password_guesses_are_slowed_down_too(env):
    frame, _, clock = env
    frame.cfg.update({"http_password": "pagepass"})
    client, _ = make(env)
    bad = {"Authorization": "Basic " + base64.b64encode(b"x:nope").decode()}
    good = {"Authorization": "Basic " + base64.b64encode(b"x:pagepass").decode()}
    for _ in range(3):
        assert client.get("/api/status", headers=bad).status_code == 401
    assert client.get("/api/status", headers=good).status_code == 429
    clock.t += 6
    assert client.get("/api/status", headers=good).status_code == 200


def test_changing_the_admin_password_locks_everyone_else(env):
    frame, _, clock = env
    frame.cfg.update({"admin_password": ADMIN})
    a, sessions = make(env)
    b = a.application.test_client()
    a.post("/api/admin/unlock", json={"password": ADMIN})
    b.post("/api/admin/unlock", json={"password": ADMIN})
    assert a.post("/api/admin/password", json={"password": "new-admin-pw"}).status_code == 200
    assert a.get("/api/backup").status_code == 200
    assert b.get("/api/backup").status_code == 403
    assert b.post("/api/admin/unlock", json={"password": ADMIN}).status_code == 403
    assert b.post("/api/admin/unlock", json={"password": "new-admin-pw"}).status_code == 200


def test_settings_that_belong_to_the_admin(env):
    frame, _, clock = env
    frame.cfg.update({"admin_password": ADMIN})
    client, _ = make(env)
    for body in ({"http_password": "x" * 8}, {"update_repo": "someone/else"}, {"update_auto": "install"}, {"update_channel": "test"},
                 {"backup_where": "nas"}, {"device_name": "Hall", "update_time": "01:00"}):
        r = client.post("/api/config", json=body)
        assert r.status_code == 403 and r.get_json()["admin"] == "locked", body
    assert client.post("/api/config", json={"device_name": "Hall"}).status_code == 200  # everyday settings
    client.post("/api/admin/unlock", json={"password": ADMIN})
    assert client.post("/api/config", json={"update_auto": "off"}).status_code == 200
    # the admin password only changes through its own form (it needs the old unlock rules)
    r = client.post("/api/config", json={"admin_password": "sneaky-new"})
    assert r.status_code == 400 and is_pw(frame.cfg.get("admin_password"), ADMIN)
    # and never shows up
    assert "admin_password" not in client.get("/api/config").get_json()


def test_page_password_is_set_under_admin(env):
    frame, _, clock = env
    frame.cfg.update({"admin_password": ADMIN})
    client, _ = make(env)
    assert client.post("/api/admin/page-password", json={"password": "everyday"}).status_code == 403
    client.post("/api/admin/unlock", json={"password": ADMIN})
    assert client.post("/api/admin/page-password", json={"password": "abc"}).status_code == 400
    r = client.post("/api/admin/page-password", json={"password": "everyday"})
    assert r.status_code == 200 and is_pw(frame.cfg.get("http_password"), "everyday")
    auth = {"Authorization": "Basic " + base64.b64encode(b"x:everyday").decode()}
    assert client.get("/api/status").status_code == 401
    st = client.get("/api/admin", headers=auth).get_json()
    assert st["unlocked"] and st["page_password_set"]
    assert client.post("/api/admin/page-password", json={"password": ""}, headers=auth).get_json()["page_password_set"] is False
    assert client.get("/api/status").status_code == 200


def test_the_image_server_cant_change_admin_settings(tmp_path):
    cfg = ConfigStore(tmp_path)
    cfg.update({"admin_password": ADMIN, "http_password": "pagepass"})
    cfg.apply_remote_payload({"config": {"device_name": "From server", "http_password": "", "admin_password": "x",
                                         "update_repo": "evil/repo", "update_auto": "install", "backup_where": "nas"}})
    assert cfg.get("device_name") == "From server"
    assert is_pw(cfg.get("http_password"), "pagepass") and is_pw(cfg.get("admin_password"), ADMIN)
    assert cfg.get("update_repo") == "Jimmy3D-xyz/pi-frame" and cfg.get("update_auto") == "notify"
    assert cfg.get("backup_where") == "frame"


def test_frames_with_a_page_password_keep_it_for_admin(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"config": {"http_password": "oldpass1", "device_name": "Hall"}}))
    cfg = ConfigStore(tmp_path)
    assert is_pw(cfg.get("admin_password"), "oldpass1") and is_pw(cfg.get("http_password"), "oldpass1")
    # once saved, it stays the admin's own (removing the page password later doesn't reset it)
    cfg.update({"http_password": ""})
    assert is_pw(ConfigStore(tmp_path).get("admin_password"), "oldpass1")
    (tmp_path / "config.json").write_text(json.dumps({"config": {"device_name": "Hall"}}))
    assert ConfigStore(tmp_path).get("admin_password") == ""


# ---------------------------------------------------------------- other websites
def test_other_websites_cant_make_changes(env):
    frame, _, clock = env
    frame.cfg.update({"admin_password": ADMIN})
    client, _ = make(env)
    client.post("/api/admin/unlock", json={"password": ADMIN})
    evil = {"Origin": "http://evil.example"}
    r = client.post("/api/maintenance/update/install", headers=evil)
    assert r.status_code == 403 and "frame's own page" in r.get_json()["message"]
    assert client.post("/api/next-set", headers=evil).status_code == 403
    assert client.post("/api/next-set", headers={"Origin": "null"}).status_code == 403
    assert client.post("/api/next-set", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.delete("/api/backup/file/x.tgz", headers=evil).status_code == 403
    # the frame's own page, and apps and scripts (no Origin), are fine
    assert client.post("/api/next-set", headers={"Origin": "http://localhost"}).status_code != 403
    assert client.post("/api/next-set", headers={"Sec-Fetch-Site": "same-origin"}).status_code != 403
    assert client.post("/api/next-set").status_code != 403
    # reading is fine (the browser keeps the answer from the other site anyway)
    assert client.get("/api/status", headers=evil).status_code == 200


def test_only_the_frames_own_names(env, monkeypatch):
    frame, _, clock = env
    client, _ = make(env)
    me = socket.gethostname()
    for host in ("localhost", f"{me}.local", me, "192.168.1.50", "192.168.1.50:8080", "[fe80::1]",
                 "piframe.tail1234.ts.net"):
        assert client.get("/api/status", headers={"Host": host}).status_code == 200, host
    r = client.get("/api/status", headers={"Host": "attacker.example"})
    assert r.status_code == 403 and b"own address" in r.data
    assert client.get("/", headers={"Host": "rebind.attacker.example:80"}).status_code == 403
    monkeypatch.setenv("PHOTOFRAME_ALLOWED_HOSTS", "frame.example.org, other.lan")
    assert client.get("/api/status", headers={"Host": "frame.example.org"}).status_code == 200


def test_host_and_origin_helpers():
    names = {"piframe", "piframe.local", "localhost"}
    assert adm.host_allowed("PiFrame.local.", names) and adm.host_allowed("10.0.0.2:80", names)
    assert not adm.host_allowed("", names) and not adm.host_allowed("piframe.evil.com", names)
    assert not adm.cross_site(None, None, "piframe.local")
    assert not adm.cross_site("http://piframe.local", None, "piframe.local")
    assert adm.cross_site("http://piframe.local:8080", None, "piframe.local")
    assert adm.cross_site("http://evil.com", "same-origin", "piframe.local")
    assert adm.is_admin_path("/api/backup") and adm.is_admin_path("/api/cloud/links/abc")
    assert adm.is_admin_path("/api/wifi/connect") and not adm.is_admin_path("/api/wifi/status")
    assert not adm.is_admin_path("/api/backups-are-not-a-thing") and not adm.is_admin_path("/api/maintenance")


# ---------------------------------------------------------------- synced folders
def test_folder_browsing_only_uses_connected_accounts(tmp_path, monkeypatch):
    od = OneDrive(tmp_path, Storage(tmp_path, PANEL))
    ran = []
    monkeypatch.setattr(od, "_rclone", lambda *a, **k: ran.append(a) or "[]")
    for remote in (":local", ":http,url='http://192.168.1.1'", "", "onedrive"):
        with pytest.raises(CloudError, match="isn't connected"):
            od.list_folders("/", remote)
        with pytest.raises(CloudError, match="isn't connected"):
            od.count_media("/", True, remote)
    assert not ran
    od._set_remote("onedrive", {"type": "onedrive"})
    assert od.list_folders("", "onedrive") == [] and ran


# ---------------------------------------------------------------- setting-up settings and stored secrets
BROWSER = {"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}


def test_setup_settings_need_the_admin_on_the_web_page(env):
    frame, _, clock = env
    frame.cfg.update({"admin_password": ADMIN})
    client, _ = make(env)
    for body in ({"display_orientation": "portrait"}, {"device_name": "Hall"}, {"timezone": "UTC"},
                 {"buttons_enabled": True}, {"presence_pin": 23}, {"image_url": "http://x/"}, {"demo_mode": True},
                 {"display_hw_fade": False}):
        r = client.post("/api/config", json=body, headers=BROWSER)
        assert r.status_code == 403 and r.get_json()["admin"] == "locked", body
    # everyday settings from the page are fine
    assert client.post("/api/config", json={"transition_ms": 800, "overlay_clock": False}, headers=BROWSER).status_code == 200
    # Home Assistant and the image server (no browser headers) keep the firmware API
    assert client.post("/api/config", json={"device_name": "Hall", "display_orientation": "portrait"}).status_code == 200
    assert frame.cfg.get("device_name") == "Hall"
    # but never the admin's own settings
    assert client.post("/api/config", json={"update_repo": "evil/repo"}).status_code == 403
    client.post("/api/admin/unlock", json={"password": ADMIN})
    assert client.post("/api/config", json={"device_name": "Lounge"}, headers=BROWSER).status_code == 200


def test_stored_secrets_are_never_shown(env):
    frame, _, clock = env
    frame.cfg.update({"access_token": "tok-123", "http_header_value": "Bearer abc", "openai_api_key": "sk-1",
                      "google_api_key": ""})
    client, _ = make(env)
    body = client.get("/api/config").get_json()
    assert body["access_token"] == adm.MASK and body["http_header_value"] == adm.MASK and body["openai_api_key"] == adm.MASK
    assert body["google_api_key"] == ""
    assert "tok-123" not in json.dumps(body) and "sk-1" not in json.dumps(body)
    # sending the mask back (a form saved without touching the field) changes nothing
    assert client.post("/api/config", json={"access_token": adm.MASK, "image_url": "http://s/img"}).status_code == 200
    assert frame.cfg.get("access_token") == "tok-123" and frame.cfg.get("image_url") == "http://s/img"
    client.post("/api/config", json={"access_token": "new-tok"})
    assert frame.cfg.get("access_token") == "new-tok"
    client.post("/api/config", json={"access_token": ""})
    assert frame.cfg.get("access_token") == ""


def test_screen_resolution_is_an_admin_option(env):
    frame, _, clock = env
    client, _ = make(env)
    assert client.post("/api/display/mode", json={"mode": "1920x1080"}).status_code == 403
    assert client.post("/api/display/detect").status_code == 403
    assert client.get("/api/display/modes").status_code != 403  # looking is fine


# ---------------------------------------------------------------- 0.34.1
def test_passwords_are_stored_scrambled(tmp_path):
    cfg = ConfigStore(tmp_path)
    cfg.update({"admin_password": "admin-pass-1", "http_password": "pagepass"})
    text = (tmp_path / "config.json").read_text()
    assert "admin-pass-1" not in text and "pagepass" not in text
    stored = json.loads(text)["config"]
    assert adm.is_hashed(stored["admin_password"]) and adm.is_hashed(stored["http_password"])
    assert stored["admin_password"] != stored["http_password"]  # each has its own salt
    assert adm.password_matches("admin-pass-1", cfg.get("admin_password"))
    assert not adm.password_matches("admin-pass-2", cfg.get("admin_password"))
    assert not adm.password_matches("", cfg.get("admin_password"))
    # an old file (or an old backup restored) with plain passwords is scrambled when the frame starts
    (tmp_path / "config.json").write_text(json.dumps({"config": {"admin_password": "plain-admin",
                                                                  "http_password": "plain-page"}}))
    cfg = ConfigStore(tmp_path)
    assert "plain-admin" not in (tmp_path / "config.json").read_text()
    assert is_pw(cfg.get("admin_password"), "plain-admin") and is_pw(cfg.get("http_password"), "plain-page")
    # clearing still works (the forgotten-password steps)
    cfg.update({"http_password": ""})
    assert cfg.get("http_password") == ""


def test_page_password_still_works_for_every_request(env):
    frame, _, clock = env
    frame.cfg.update({"http_password": "pagepass"})
    client, _ = make(env)
    good = {"Authorization": "Basic " + base64.b64encode(b"x:pagepass").decode()}
    for _ in range(20):  # remembered after the first check, so this stays quick
        assert client.get("/api/status", headers=good).status_code == 200
    frame.cfg.update({"http_password": "changed1"})
    assert client.get("/api/status", headers=good).status_code == 401


def test_other_websites_cant_show_the_page(env):
    frame, _, clock = env
    client, _ = make(env)
    for path in ("/", "/api/status", "/setup"):
        r = client.get(path)
        assert r.headers["X-Frame-Options"] == "SAMEORIGIN", path
        assert "frame-ancestors 'self'" in r.headers["Content-Security-Policy"]
    # the live view has no controls, so a Home Assistant dashboard can show it
    r = client.get("/live")
    assert "X-Frame-Options" not in r.headers and "Content-Security-Policy" not in r.headers
