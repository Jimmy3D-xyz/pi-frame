import importlib.util
from pathlib import Path

from PIL import Image

from photoframe import display as dmod
from photoframe.config import ConfigStore
from photoframe.frame import Frame
from photoframe.storage import Storage
from photoframe.web import create_app

ROOT = Path(__file__).resolve().parents[1]


def headless_frame(tmp_path, monkeypatch, size=(2560, 1440)):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    monkeypatch.setattr(dmod, "screen_connected", lambda: False)
    disp = dmod.make_display("fb", headless_size=size)
    cfg = ConfigStore(tmp_path)
    cfg.update({"transition": "none"})
    return Frame(cfg, Storage(tmp_path, disp.size), disp, tmp_path)


def test_no_screen_runs_headless_at_the_last_size(tmp_path, monkeypatch):
    f = headless_frame(tmp_path, monkeypatch)
    assert isinstance(f.display, dmod.HeadlessDisplay) and f.display.size == (2560, 1440)
    assert f.display.info()["kind"] == "none"
    f.storage.add("Default", "a.jpg", Image.new("RGB", (3000, 2000), (200, 0, 0)))
    ok, _ = f.rotate()
    assert ok and f.current_thumb  # pictures are still composed: the website's preview works
    c = create_app(f).test_client()
    assert c.get("/").status_code == 200 and c.get("/api/status").status_code == 200  # the website works
    assert f.player.simulate is True  # videos are skipped: there's nothing to play them on


def test_plugging_in_a_screen_restarts_the_frame(tmp_path, monkeypatch):
    f = headless_frame(tmp_path, monkeypatch)
    assert f.screen_watch_step(False) is False
    assert f.screen_watch_step(True) is True  # started headless, screen plugged in: restart


def test_reconnecting_restarts_too(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    disp = dmod.FileDisplay((320, 200), str(tmp_path / "f.png"))
    f = Frame(ConfigStore(tmp_path), Storage(tmp_path, (320, 200)), disp, tmp_path)
    assert f.screen_watch_step(True) is False   # running with a screen: nothing to do
    assert f.screen_watch_step(False) is False  # unplugged: just noted
    assert f.screen_watch_step(True) is True    # plugged back in: restart to set it up again
    assert f.screen_watch_step(None) is False   # a machine without HDMI: never


def test_boot_logo_left_alone_without_a_screen(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("system_helper", ROOT / "deploy" / "system_helper.py")
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    called = []
    monkeypatch.setattr(h, "screen_modes", lambda sysfs="/sys/class/drm": (None, []))
    monkeypatch.setattr(h, "splash_out_of_date", lambda fb=None: called.append(1) or "screen is now 1024x768")
    monkeypatch.setattr(h, "remake_bootsplash", lambda mode=None: called.append(2) or 0)
    assert h.refresh_bootsplash() == 0 and called == []  # not even checked
