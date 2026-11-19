from datetime import datetime

import pytest
from PIL import Image

from photoframe.config import ConfigError, ConfigStore
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.monitor import Monitor, in_night
from photoframe.storage import Storage
from photoframe.web import create_app

# the developer's H24T7, as ddcutil reports it
DETECT_BRIEF = "Display 1\n   I2C bus:  /dev/i2c-20\n   DRM connector:           card1-HDMI-A-1\n   Monitor:                 SKG:H24T7:\n"
CAPS = ("   Feature: 05 (Restore factory brightness/contrast defaults)\n   Feature: 10 (Brightness)\n"
        "   Feature: 12 (Contrast)\n   Feature: 60 (Input Source)\n   Feature: D6 (Power mode)\n")


class FakeDDC:
    """Stands in for ddcutil talking to the H24T7."""

    def __init__(self, detect=DETECT_BRIEF, caps=CAPS):
        self.values = {"10": 90, "D6": 1}
        self.writes = []
        self.detect, self.caps = detect, caps

    def __call__(self, args, timeout=15):
        if args[0] == "detect":
            return self.detect
        args = args[2:] if args[:1] == ["--bus"] else args
        if args[0] == "capabilities":
            return self.caps
        if args[0] == "getvcp":
            return f"VCP code 0x{args[1]} (Brightness                    ): current value =    {self.values.get(args[1], 0)}, max value =   100\n"
        if args[0] == "setvcp":
            self.values[args[1]] = int(args[2])
            self.writes.append((args[1], int(args[2])))
            return ""
        raise RuntimeError("unexpected")


@pytest.fixture
def frame(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    monkeypatch.setattr("photoframe.monitor.time.sleep", lambda s: None)
    cfg = ConfigStore(tmp_path)
    cfg.update({"transition": "none"})
    f = Frame(cfg, Storage(tmp_path, (800, 480)), FileDisplay((800, 480), str(tmp_path / "f.png")), tmp_path)
    f.storage.add("Default", "a.jpg", Image.new("RGB", (800, 600), (200, 200, 200)))
    f.ddc = FakeDDC()
    f.monitor = Monitor(cfg, runner=f.ddc, on_change=f.refresh_overlay)
    return f


def test_finds_the_h24t7(frame):
    m = frame.monitor
    assert m.probe() and m.bus == 20 and m.model == "H24T7"
    assert {"10", "12", "D6"} <= m.features and m.available
    assert m.get("10") == (90, 100)
    # the full (non-brief) detect format works too
    full = FakeDDC(detect="Display 1\n   I2C bus:  /dev/i2c-20\n   EDID synopsis:\n      Mfg id: SKG - UNK\n      Model: H24T7\n")
    m2 = Monitor(frame.cfg, runner=full)
    assert m2.probe() and m2.model == "H24T7"
    silent = Monitor(frame.cfg, runner=FakeDDC(detect="No displays found.\n"))
    assert not silent.probe() and "DDC/CI" in silent.error


def test_writes_only_when_the_target_changes(frame):
    m, ddc = frame.monitor, frame.ddc
    assert m.apply() is False and ddc.writes == []  # -1: the monitor's own setting is left alone
    frame.cfg.update({"monitor_brightness": 90})
    assert m.apply() and ddc.writes == [("10", 90)]
    assert m.apply() is False and m.apply() is False and len(ddc.writes) == 1  # no needless rewrites
    frame.cfg.update({"monitor_brightness": 60})
    assert m.apply() and ddc.writes[-1] == ("10", 60)


def test_night_level(frame):
    assert in_night(datetime(2026, 10, 4, 22, 0), "21:00", "07:00")  # crosses midnight
    assert in_night(datetime(2026, 10, 4, 6, 59), "21:00", "07:00")
    assert not in_night(datetime(2026, 10, 4, 7, 0), "21:00", "07:00")
    assert in_night(datetime(2026, 10, 4, 14, 0), "13:00", "15:00") and not in_night(datetime(2026, 10, 4, 16, 0), "13:00", "15:00")
    m, ddc = frame.monitor, frame.ddc
    frame.cfg.update({"monitor_brightness": 90, "monitor_night_enabled": True, "monitor_night_brightness": 35})
    m.apply(datetime(2026, 10, 4, 12, 0))
    m.apply(datetime(2026, 10, 4, 21, 30))
    m.apply(datetime(2026, 10, 4, 23, 0))  # still night: no write
    m.apply(datetime(2026, 10, 5, 7, 5))
    assert ddc.writes == [("10", 90), ("10", 35), ("10", 90)]  # just the two changes a day


def test_standby_follows_the_screen(frame):
    ddc = frame.ddc
    frame.cfg.update({"monitor_brightness": 80})
    frame.monitor.apply()
    frame.rotate()
    frame.set_screen(False)
    assert frame.display.screen_on is False
    assert ("D6", 4) not in ddc.writes  # standby is off by default: just the picture signal
    frame.set_screen(True)
    frame.cfg.update({"monitor_standby": True})
    frame.set_screen(False)
    import time
    for _ in range(50):
        if ("D6", 4) in ddc.writes:
            break
        time.sleep(0.01)
    assert ("D6", 4) in ddc.writes  # the monitor itself goes into standby
    ddc.writes.clear()
    frame.set_screen(True)
    assert ddc.writes[0] == ("D6", 1) and ("10", 80) in ddc.writes  # woken, brightness written again


def test_no_ddc_darkens_the_picture(frame, tmp_path):
    frame.monitor = Monitor(frame.cfg, runner=FakeDDC(detect="No displays found.\n"), on_change=frame.refresh_overlay)
    frame.cfg.update({"monitor_brightness": 50, "overlay_clock": False, "overlay_date": False})
    frame.monitor.apply()
    assert frame.monitor.software_level == 0.5
    frame.rotate()
    shown = Image.open(tmp_path / "f.png").convert("L").getpixel((400, 240))
    assert 90 <= shown <= 110  # the light grey photo, at half brightness
    frame.cfg.update({"monitor_brightness": 5})
    frame.monitor.apply()
    assert frame.monitor.software_level == 0.15  # never so dark it can't be seen


def test_settings_and_web(frame):
    with pytest.raises(ConfigError):
        frame.cfg.update({"monitor_night_from": "25:00"})
    with pytest.raises(ConfigError):
        frame.cfg.update({"monitor_brightness": 150})
    c = create_app(frame).test_client()
    st = c.get("/api/monitor").get_json()
    assert st["available"] and st["model"] == "H24T7" and st["brightness"] == 90 and st["standby_supported"]
    assert c.post("/api/config", json={"monitor_brightness": 70}).status_code == 200
    import time
    for _ in range(100):
        if ("10", 70) in frame.ddc.writes:
            break
        time.sleep(0.01)
    assert ("10", 70) in frame.ddc.writes  # applied straight away
    html = c.get("/").get_data(as_text=True)
    assert 'id="mon-b"' in html and 'data-ovl="monitor_standby"' in html
