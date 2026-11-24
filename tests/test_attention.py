"""The on-screen notice, and Raspberry Pi OS security updates."""

import importlib.util
import json
import time
from pathlib import Path

import pytest
from PIL import Image

from photoframe.attention import DELAY, Attention, pick
from photoframe.config import ConfigStore
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.overlay import Overlay
from photoframe.secupdates import SecurityUpdates
from photoframe.storage import Storage
from photoframe.web import create_app

from adminhelp import unlock

ROOT = Path(__file__).resolve().parent.parent
BACKUP_BAD = {"area": "Backups", "level": "warn", "text": "The last backup didn't work: NAS offline"}
NET_BAD = {"area": "Network", "level": "bad", "text": "Not connected"}
OK = {"area": "SD card", "level": "ok", "text": "20 GB free"}


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- what counts
def test_only_problems_that_need_a_person():
    summary = [OK, NET_BAD, BACKUP_BAD, {"area": "Videos", "level": "warn", "text": "1 couldn't be converted"},
               {"area": "Power", "level": "warn", "text": "Under-voltage has happened since boot"},
               {"area": "Power", "level": "bad", "text": "Under-voltage right now"},
               {"area": "Security updates", "level": "warn", "text": "haven't worked for 4 days"}]
    assert [i["area"] for i in pick(summary)] == ["Backups", "Power", "Security updates"]


def test_shown_after_an_hour_and_gone_when_fixed(tmp_path):
    cfg = ConfigStore(tmp_path)
    clock, changes = Clock(), []
    a = Attention(cfg, tmp_path, on_change=lambda: changes.append(a.showing()), clock=clock)
    a.update([BACKUP_BAD])
    assert not a.showing()  # not straight away: passing hiccups never show
    clock.t += DELAY - 1
    a.update([BACKUP_BAD])
    assert not a.showing()
    clock.t += 2
    a.update([dict(BACKUP_BAD, text="The last backup didn't work: still offline")])  # same problem, new wording
    assert a.showing() and changes == [True]
    a.update([OK])
    assert not a.showing() and changes == [True, False]
    # it comes back after another hour, not at once
    a.update([BACKUP_BAD])
    assert not a.showing()


def test_hide_until_something_new(tmp_path):
    cfg = ConfigStore(tmp_path)
    clock = Clock()
    a = Attention(cfg, tmp_path, clock=clock)
    a.update([BACKUP_BAD])
    clock.t += DELAY
    a.update([BACKUP_BAD])
    a.hide()
    assert not a.showing() and a.state()["hidden"]
    # remembered across a restart
    a = Attention(cfg, tmp_path, clock=clock)
    a.update([BACKUP_BAD])
    clock.t += DELAY
    a.update([BACKUP_BAD])
    assert not a.showing()
    # something new shows it again
    sd = {"area": "SD card", "level": "warn", "text": "Only 0.8 GB free"}
    a.update([BACKUP_BAD, sd])
    clock.t += DELAY
    a.update([BACKUP_BAD, sd])
    assert a.showing() and [i["area"] for i in a.visible()] == ["SD card"]
    # once the backup is fixed, a later backup problem counts as new again
    a.update([sd])
    assert json.loads((tmp_path / "attention.json").read_text())["hidden"] == []


def test_can_be_switched_off(tmp_path):
    cfg = ConfigStore(tmp_path)
    clock = Clock()
    a = Attention(cfg, tmp_path, clock=clock)
    a.update([BACKUP_BAD])
    clock.t += DELAY
    a.update([BACKUP_BAD])
    assert a.showing()
    cfg.update({"attention_on_screen": False})
    assert not a.showing() and a.state()["items"]  # still listed in Maintenance


def test_the_panel_on_screen(tmp_path):
    cfg = ConfigStore(tmp_path)
    cfg.update({"overlay_clock": False, "overlay_date": False, "overlay_photo_info": False})
    ov = Overlay(cfg, tmp_path)
    base = Image.new("RGB", (1920, 1080), (20, 90, 160))
    assert ov.apply(base) is base  # nothing to draw

    class Showing:
        def showing(self):
            return True

    ov.attention = Showing()
    out = ov.apply(base)
    assert out is not base
    top = out.crop((660, 0, 1260, 160))
    assert top.getextrema() != base.crop((660, 0, 1260, 160)).getextrema()  # drawn at the top centre
    assert out.crop((0, 900, 400, 1080)).getextrema() == base.crop((0, 900, 400, 1080)).getextrema()
    # with the info panels at the top, it goes to the bottom
    cfg.update({"overlay_corner": "top-left"})
    out = ov.apply(base)
    assert out.crop((660, 960, 1260, 1080)).getextrema() != base.crop((660, 960, 1260, 1080)).getextrema()


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    cfg = ConfigStore(tmp_path)
    frame = Frame(cfg, Storage(tmp_path, (1920, 1080)), FileDisplay((1920, 1080), str(tmp_path / "f.png")), tmp_path)
    return frame, create_app(frame).test_client()


def test_maintenance_shows_it_and_the_admin_hides_it(web):
    frame, client = web
    clock = Clock()
    frame.attention.clock = clock
    frame.attention.update([BACKUP_BAD])
    clock.t += DELAY
    frame.attention.update([BACKUP_BAD])
    h = client.get("/api/maintenance").get_json()
    assert h["attention"]["on_screen"] and h["attention"]["items"][0]["area"] == "Backups"
    assert client.post("/api/maintenance/attention/hide").status_code == 403  # an admin option
    unlock(client, frame)
    r = client.post("/api/maintenance/attention/hide").get_json()
    assert r["hidden"] and not r["on_screen"]
    assert client.post("/api/maintenance/attention/show").get_json()["on_screen"]
    # the frame checks the same summary as the Maintenance tab
    assert isinstance(frame.attention.source(), list)


# ---------------------------------------------------------------- security updates (the frame's side)
def write_status(tmp_path, **s):
    (tmp_path / "system").mkdir(exist_ok=True)
    (tmp_path / "system" / "security.json").write_text(json.dumps(s))


def test_security_update_lines(tmp_path, monkeypatch):
    now = time.time()
    sec = SecurityUpdates(tmp_path, installed=False)
    assert sec.health() is None  # not set up (e.g. trying the frame on a computer)
    sec = SecurityUpdates(tmp_path, installed=True)
    assert "first check is tonight" in sec.health()["text"]
    monkeypatch.setattr("photoframe.secupdates.boot_time", lambda: now - 10 * 86400)
    write_status(tmp_path, t=now - 3600, ok=True, last_ok_t=now - 3600, last_installed=["libheif1", "ffmpeg"],
                 last_installed_t=now - 3 * 86400)
    h = sec.health(now)
    assert h["level"] == "ok" and "2 installed 3 days ago" in h["text"]
    # one bad night is fine; days of it aren't
    write_status(tmp_path, t=now, ok=False, message="Couldn't reach the update servers", last_ok_t=now - 86400)
    assert sec.health(now)["level"] == "ok"
    write_status(tmp_path, t=now, ok=False, message="Couldn't reach the update servers", last_ok_t=now - 5 * 86400)
    h = sec.health(now)
    assert h["level"] == "warn" and "5 days" in h["text"] and "update servers" in h["text"]
    # a restart that's been waiting too long
    write_status(tmp_path, t=now, ok=True, last_ok_t=now, reboot_t=now - 4 * 86400)
    assert "restart" in sec.health(now)["text"] and sec.health(now)["level"] == "warn"
    # ... and once the Pi has restarted, it's done
    monkeypatch.setattr("photoframe.secupdates.boot_time", lambda: now - 3600)
    assert sec.reboot_waiting_since(now) is None and sec.health(now)["level"] == "ok"


def test_restarts_only_while_nobody_is_watching(tmp_path, monkeypatch):
    now = time.mktime(time.strptime("2026-11-25 14:00", "%Y-%m-%d %H:%M"))
    night = time.mktime(time.strptime("2026-11-26 04:00", "%Y-%m-%d %H:%M"))
    sec = SecurityUpdates(tmp_path, installed=True)
    monkeypatch.setattr("photoframe.secupdates.boot_time", lambda: now - 86400)
    write_status(tmp_path, t=now, ok=True, last_ok_t=now, reboot_t=now - 600)
    # with a screen schedule: only while it's off
    assert not sec.may_reboot_now(screen_scheduled_off=False, schedule_on=True, busy=False, now=now)
    assert sec.may_reboot_now(screen_scheduled_off=True, schedule_on=True, busy=False, now=now)
    assert not sec.may_reboot_now(screen_scheduled_off=True, schedule_on=True, busy=True, now=now)  # backup/update
    # without one: in the small hours
    assert not sec.may_reboot_now(screen_scheduled_off=False, schedule_on=False, busy=False, now=now)
    assert sec.may_reboot_now(screen_scheduled_off=False, schedule_on=False, busy=False, now=night)
    # nothing waiting: never
    write_status(tmp_path, t=now, ok=True, last_ok_t=now)
    assert not sec.may_reboot_now(screen_scheduled_off=True, schedule_on=True, busy=False, now=now)


def test_maintenance_summary_has_security_updates(web, monkeypatch):
    frame, client = web
    frame.security.installed = True
    now = time.time()
    write_status(frame.data_dir, t=now, ok=False, message="Installing didn't work: dpkg error", last_ok_t=now - 9 * 86400)
    lines = {x["area"]: x for x in client.get("/api/maintenance").get_json()["summary"]}
    assert lines["Security updates"]["level"] == "warn" and "dpkg error" in lines["Security updates"]["text"]


# ---------------------------------------------------------------- the nightly root script
def load_script():
    spec = importlib.util.spec_from_file_location("security_updates", ROOT / "deploy" / "security_updates.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_nightly_script_records_what_it_installed(tmp_path, monkeypatch):
    mod = load_script()
    versions = [{"ffmpeg": "7.1.1", "libc6": "2.41-1", "libheif1": "1.19"},
                {"ffmpeg": "7.1.2", "libc6": "2.41-2", "libheif1": "1.19"}]
    calls = []
    monkeypatch.setattr(mod, "installed_packages", lambda: versions[min(len(calls) // 2, 1)] if calls else versions[0])

    def run(cmd, timeout):
        calls.append(cmd)
        return 0, "ok"

    monkeypatch.setattr(mod, "run", run)
    st = mod.check(tmp_path, now=1000.0, reboot_flag=tmp_path / "no-flag")
    assert calls[0][:2] == ["apt-get", "update"] and calls[1][0] == "unattended-upgrade"
    assert st["ok"] and st["installed"] == ["ffmpeg", "libc6"] and st["last_installed_t"] == 1000.0
    assert st["reboot_t"] == 1000.0  # libc changed: the Pi should restart
    saved = json.loads((tmp_path / "system" / "security.json").read_text())
    assert saved == st and (tmp_path / "system" / "security-log.txt").exists()


def test_nightly_script_when_offline(tmp_path, monkeypatch):
    mod = load_script()
    monkeypatch.setattr(mod, "installed_packages", lambda: {"ffmpeg": "7.1.1"})
    monkeypatch.setattr(mod, "run", lambda cmd, timeout: (100, "Err:1 http://deb.debian.org trixie-security\nE: Some index files failed"))
    mod.save(tmp_path / "system" / "security.json", {"last_ok_t": 500.0, "failures": 2, "last_installed": ["x"],
                                                     "last_installed_t": 400.0})
    st = mod.check(tmp_path, now=1000.0, reboot_flag=tmp_path / "no-flag")
    assert not st["ok"] and st["failures"] == 3 and st["last_ok_t"] == 500.0
    assert "update servers" in st["message"] and "index files failed" in st["message"]
    assert st["last_installed"] == ["x"] and st["last_installed_t"] == 400.0 and st["reboot_t"] is None


def test_only_security_updates_are_allowed():
    conf = (ROOT / "deploy" / "52pi-frame-unattended.conf").read_text()
    assert "#clear Unattended-Upgrade::Origins-Pattern;" in conf
    patterns = [l.strip() for l in conf.splitlines() if l.strip().startswith('"origin=')]
    assert patterns and all("label=Debian-Security" in p for p in patterns)
    assert 'APT::Periodic::Unattended-Upgrade "0";' in conf
    install = (ROOT / "deploy" / "install.sh").read_text()
    assert "unattended-upgrades" in install and "enable_now pi-frame-security.timer" in install
