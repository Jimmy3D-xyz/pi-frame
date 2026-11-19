import time

import pytest

gpiozero = pytest.importorskip("gpiozero")
from gpiozero.pins.mock import MockFactory  # noqa: E402

from photoframe.buttons import Buttons  # noqa: E402
from photoframe.config import ConfigError, ConfigStore  # noqa: E402
from photoframe.display import FileDisplay  # noqa: E402
from photoframe.frame import Frame  # noqa: E402
from photoframe.storage import Storage  # noqa: E402
from photoframe.web import create_app  # noqa: E402


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    cfg = ConfigStore(tmp_path)
    frame = Frame(cfg, Storage(tmp_path, (800, 480)), FileDisplay((800, 480), str(tmp_path / "f.png")), tmp_path)
    calls = []
    monkeypatch.setattr(frame, "rotate", lambda: calls.append("rotate") or (True, ""))
    monkeypatch.setattr(frame, "previous", lambda: calls.append("previous") or (True, ""))
    monkeypatch.setattr(frame, "set_screen", lambda on: calls.append(f"screen {'on' if on else 'off'}"))
    factory = MockFactory()
    frame.buttons = Buttons(frame, factory=factory)
    yield frame, factory, calls
    frame.buttons.close()


def tap(factory, gpio, hold=0.12):
    pin = factory.pin(gpio)
    pin.drive_low()
    time.sleep(hold)
    pin.drive_high()
    time.sleep(0.15)


def settle(calls, n, t=2.0):
    end = time.time() + t
    while len(calls) < n and time.time() < end:
        time.sleep(0.02)


def test_off_by_default(rig):
    frame, factory, calls = rig
    assert frame.buttons.start() is False and frame.buttons.status()["active"] is False


def test_each_button_does_its_job(rig):
    frame, factory, calls = rig
    frame.cfg.update({"buttons_enabled": True})
    assert frame.buttons.start() is True
    tap(factory, 27)
    tap(factory, 17)
    tap(factory, 3)
    settle(calls, 3)
    assert calls == ["rotate", "previous", "screen off"]
    assert frame.buttons.last["button"] == "screen"


def test_any_button_wakes_a_sleeping_screen(rig):
    frame, factory, calls = rig
    frame.cfg.update({"buttons_enabled": True})
    frame.buttons.start()
    frame.display.screen_on = False
    tap(factory, 27)  # Next while the screen is off...
    settle(calls, 1)
    assert calls == ["screen on"]  # ...wakes it, rather than skipping a photo nobody saw
    assert frame.buttons.last["action"] == "screen on"


def test_hold_screen_button_shuts_down(rig, monkeypatch):
    frame, factory, calls = rig
    frame.cfg.update({"buttons_enabled": True, "button_hold_seconds": 2})
    frame.buttons.start()
    monkeypatch.setattr(frame.buttons, "shutdown", lambda: calls.append("shutdown"))
    tap(factory, 3, hold=2.4)
    settle(calls, 1)
    time.sleep(0.2)
    assert calls == ["shutdown"]  # and the release afterwards doesn't also toggle the screen
    frame.cfg.update({"button_hold_shutdown": False})
    frame.buttons.start()
    tap(factory, 3, hold=2.4)
    settle(calls, 2)
    assert calls == ["shutdown", "screen off"]  # hold-to-shut-down switched off: just a press


def test_pins_can_change_and_be_left_out(rig):
    frame, factory, calls = rig
    frame.cfg.update({"buttons_enabled": True, "button_next_pin": 22, "button_previous_pin": -1})
    assert frame.buttons.start()
    tap(factory, 22)
    tap(factory, 17)  # no longer a button
    settle(calls, 1)
    time.sleep(0.2)
    assert calls == ["rotate"]
    frame.cfg.update({"button_next_pin": 3})  # same pin as the screen button
    assert frame.buttons.start() is False and "same GPIO pin" in frame.buttons.error
    with pytest.raises(ConfigError):
        frame.cfg.update({"button_next_pin": 40})


def test_web_status_and_live_restart(rig):
    frame, factory, calls = rig
    c = create_app(frame).test_client()
    assert c.get("/api/buttons").get_json()["enabled"] is False
    assert c.post("/api/config", json={"buttons_enabled": True}).status_code == 200  # starts them
    st = c.get("/api/buttons").get_json()
    assert st["active"] and st["pins"] == {"previous": 17, "next": 27, "screen": 3}
    assert st["physical"] == {"previous": 11, "next": 13, "screen": 5}
    tap(factory, 27)
    settle(calls, 1)
    assert c.get("/api/buttons").get_json()["last"]["action"] == "next photo"
    html = c.get("/").get_data(as_text=True)
    assert 'data-btn="buttons_enabled"' in html and "/static/wiring.png" in html
    assert c.get("/static/wiring.png").mimetype == "image/png"


# ---------------- choosing the pin library (the real-Pi start-up path) ----------------
def _fake_lgpio(monkeypatch, fail_chips=()):
    import sys
    import types
    made = []

    class FakeLGPIOFactory:
        def __init__(self, chip=0):
            if chip in fail_chips:
                raise OSError("can not open gpiochip")
            made.append(chip)
            self.chip = chip

    lg = types.ModuleType("lgpio")
    mod = types.ModuleType("gpiozero.pins.lgpio")
    mod.LGPIOFactory = FakeLGPIOFactory
    monkeypatch.setitem(sys.modules, "lgpio", lg)
    monkeypatch.setitem(sys.modules, "gpiozero.pins.lgpio", mod)
    return made


def test_lgpio_gets_a_writable_work_dir_and_right_chip(tmp_path, monkeypatch):
    from photoframe import buttons as bmod
    monkeypatch.delenv("LG_WD", raising=False)
    made = _fake_lgpio(monkeypatch)
    f = bmod.lgpio_factory(str(tmp_path / "gpio"))
    assert f.chip == 0 and made == [0]
    import os
    assert os.environ["LG_WD"] == str(tmp_path / "gpio") and (tmp_path / "gpio").is_dir()
    made = _fake_lgpio(monkeypatch, fail_chips=(0,))  # an older Pi 5 kernel: header on gpiochip4
    assert bmod.lgpio_factory(str(tmp_path / "gpio")).chip == 4


def test_lgpio_missing_is_reported_plainly(tmp_path, monkeypatch):
    import sys
    from photoframe import buttons as bmod
    monkeypatch.setitem(sys.modules, "lgpio", None)  # import fails
    with pytest.raises(RuntimeError, match="lgpio library isn't installed"):
        bmod.lgpio_factory(str(tmp_path / "gpio"))


def test_real_start_path_uses_lgpio_without_fallback(rig, monkeypatch):
    frame, factory, calls = rig
    from photoframe import buttons as bmod
    used = []
    monkeypatch.setattr(bmod, "_shared", None)  # a fresh shared GPIO connection for this test
    monkeypatch.setattr(bmod, "lgpio_factory", lambda wd: used.append(wd) or factory)
    frame.buttons = bmod.Buttons(frame)  # no test factory: the path a real Pi takes
    frame.cfg.update({"buttons_enabled": True})
    assert frame.buttons.start() is True and used == [str(frame.data_dir / "gpio")]
    tap(factory, 27)
    settle(calls, 1)
    assert calls == ["rotate"]
    frame.buttons.close()
    monkeypatch.setattr(bmod, "_shared", None)  # forget the working connection, so this one fails
    monkeypatch.setattr(bmod, "lgpio_factory", lambda wd: (_ for _ in ()).throw(RuntimeError("lgpio couldn't open the GPIO pins (x)")))
    frame.buttons = bmod.Buttons(frame)
    assert frame.buttons.start() is False
    assert "lgpio couldn't open" in frame.buttons.error or "No GPIO pins" in frame.buttons.error


def test_page_says_when_buttons_are_off(rig):
    frame, factory, calls = rig
    html = create_app(frame).test_client().get("/").get_data(as_text=True)
    assert 'id="btn-off-note"' in html and "Buttons are switched off" in html


def test_power_cards(rig):
    frame, factory, calls = rig
    frame.show_power_card(restarting=True)
    assert frame.notice == "power"
    frame.notice = None
    frame.show_power_card(restarting=False)
    assert frame.notice == "power" and frame.refresh_overlay() is False  # nothing draws over it
