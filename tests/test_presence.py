import pytest

gpiozero = pytest.importorskip("gpiozero")
from gpiozero.pins.mock import MockFactory  # noqa: E402
from PIL import Image  # noqa: E402

from photoframe.config import ConfigError, ConfigStore  # noqa: E402
from photoframe.display import FileDisplay  # noqa: E402
from photoframe.frame import Frame  # noqa: E402
from photoframe.presence import Presence  # noqa: E402
from photoframe.storage import Storage  # noqa: E402
from photoframe.web import create_app  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    cfg = ConfigStore(tmp_path)
    cfg.update({"transition": "none", "presence_enabled": True, "presence_timeout_minutes": 5})
    f = Frame(cfg, Storage(tmp_path, (320, 200)), FileDisplay((320, 200), str(tmp_path / "f.png")), tmp_path)
    f.storage.add("Default", "a.jpg", Image.new("RGB", (400, 300)))
    f.rotate()
    factory, clock = MockFactory(), Clock()
    f.presence = Presence(f, factory=factory, clock=clock)
    assert f.presence.start()
    pin = factory.pin(22)
    yield f, pin, clock
    f.presence.close()


def test_leaving_then_arriving(rig):
    f, pin, clock = rig
    pin.drive_high()  # someone's in the room
    assert f.presence.tick() is None and f.display.screen_on
    pin.drive_low()  # they leave
    clock.t += 4 * 60
    assert f.presence.tick() is None and f.display.screen_on  # not yet: 4 of 5 minutes
    clock.t += 61
    assert f.presence.tick() == "off" and not f.display.screen_on
    pin.drive_high()  # someone comes in
    assert f.presence.tick() == "on" and f.display.screen_on


def test_manual_off_sticks_while_youre_there(rig):
    f, pin, clock = rig
    pin.drive_high()
    f.set_screen(False)  # turned off by hand, while in the room
    for _ in range(3):
        clock.t += 10
        assert f.presence.tick() is None and not f.display.screen_on  # stays off
    pin.drive_low()  # they leave...
    f.presence.tick()
    pin.drive_high()  # ...and someone comes back
    assert f.presence.tick() == "on"


def test_manual_on_counts_as_someone_here(rig):
    f, pin, clock = rig
    pin.drive_low()
    f.set_screen(False)
    f.set_screen(True)  # turned on by hand, though the sensor sees nobody
    clock.t += 4 * 60
    assert f.presence.tick() is None and f.display.screen_on  # the 5 minutes start from the button press
    clock.t += 61
    assert f.presence.tick() == "off"


def test_respects_the_screen_schedule(rig, monkeypatch):
    f, pin, clock = rig
    pin.drive_low()
    clock.t += 6 * 60
    f.presence.tick()  # off: nobody around
    monkeypatch.setattr(f, "screen_should_be_on", lambda now=None: False)  # night, by the schedule
    pin.drive_high()
    assert f.presence.tick() is None and not f.display.screen_on  # walking past at night: stays dark
    f.cfg.update({"presence_schedule_only": False})
    assert f.presence.tick() == "on"  # unless that option is off


def test_video_and_special_screens_keep_it_on(rig):
    f, pin, clock = rig
    pin.drive_low()
    clock.t += 10 * 60
    from pathlib import Path
    f.player.playing = Path("party.mp4")
    assert f.presence.tick() is None and f.display.screen_on  # a video is playing
    f.player.playing = None
    f.notice = "update"
    assert f.presence.tick() is None and f.display.screen_on  # the update screen stays visible
    f.notice = None
    assert f.presence.tick() == "off"


def test_pin_clash_and_settings(rig):
    f, pin, clock = rig
    f.cfg.update({"buttons_enabled": True, "presence_pin": 27})  # GPIO 27 is the Next button
    p = Presence(f, factory=MockFactory(), clock=clock)
    assert not p.start() and "same GPIO pin as a button" in p.error
    with pytest.raises(ConfigError):
        f.cfg.update({"presence_timeout_minutes": 0})


def test_web_status(rig):
    f, pin, clock = rig
    c = create_app(f).test_client()
    pin.drive_high()
    st = c.get("/api/presence").get_json()
    assert st["enabled"] and st["active"] and st["present"] is True
    pin.drive_low()
    clock.t += 90
    st = c.get("/api/presence").get_json()
    assert st["present"] is False and st["quiet_seconds"] == 90
    assert 'id="pres-status"' in c.get("/").get_data(as_text=True)
