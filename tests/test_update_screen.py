import json
import time

import pytest
from PIL import Image

from photoframe import __version__
from photoframe.config import ConfigStore
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.network import WifiStatus
from photoframe.storage import Storage

_real_sleep = time.sleep


@pytest.fixture
def frame(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    monkeypatch.setattr("photoframe.frame.time.sleep", lambda s: _real_sleep(min(s, 0.02)))  # fast-forward waits
    cfg = ConfigStore(tmp_path)
    cfg.update({"transition": "none", "welcome_seconds": 0})
    f = Frame(cfg, Storage(tmp_path, (800, 480)), FileDisplay((800, 480), str(tmp_path / "f.png")), tmp_path)
    f.storage.add("Default", "a.jpg", Image.new("RGB", (800, 600), (200, 80, 80)))
    f.storage.add("Default", "b.jpg", Image.new("RGB", (800, 600), (80, 80, 200)))
    return f


def wait(cond, t=3.0):
    end = time.time() + t
    while time.time() < end:
        if cond():
            return True
        _real_sleep(0.02)
    return False


def write_status(frame, state, message="", age=0):
    d = frame.data_dir / "update"
    d.mkdir(exist_ok=True)
    (d / "status.json").write_text(json.dumps({"state": state, "message": message, "t": time.time() - age}))


def test_progress_follows_the_updater(frame, monkeypatch):
    frame.rotate()
    states = iter(["queued", "unpacking", "backing-up", "installing", "installing", "checking"])
    steps = []
    real = frame._show_update_step
    monkeypatch.setattr(frame, "_show_update_step", lambda step: steps.append(step) or real(step))
    frame.begin_update("0.99.0", lambda: {"state": next(states, "checking")})
    assert frame.notice == "update"
    assert wait(lambda: 4 in steps)
    assert steps[:5] == [0, 1, 2, 3, 4]  # each step drawn once, in order
    ok, msg = frame.rotate()
    assert not ok and "updating" in msg  # the slideshow and Next leave the screen alone
    frame.on_wifi_change(WifiStatus(mode="online", ssid="Home"))
    assert frame.notice == "update"  # a Wi-Fi reconnection doesn't clear it either
    frame._stop.set()


def test_failed_update_says_so_then_back_to_photos(frame):
    frame.rotate()
    seq = iter([{"state": "installing"}, {"state": "rolled-back", "message": "The installer reported an error."}])
    frame.begin_update("0.99.0", lambda: next(seq, {"state": "rolled-back", "message": "x"}))
    assert wait(lambda: frame.notice is None)  # result shown, then back to the photos
    assert frame.state["current"]


def test_after_restart_confirms_once(frame):
    write_status(frame, "done", "Updated to version 0.99.0")
    frame.startup()
    assert frame.notice == "update"  # "Updated to version ..."
    assert wait(lambda: frame.notice is None)  # then the usual start-up
    assert (frame.data_dir / "update" / "announced").exists()
    frame.startup()
    assert frame.notice is None  # not announced twice


def test_old_results_are_not_announced(frame):
    write_status(frame, "done", age=3600)  # an update from an hour ago
    frame.startup()
    assert frame.notice is None


def test_new_version_still_being_checked(frame, monkeypatch):
    write_status(frame, "checking", "Checking the frame starts up properly")
    steps = []
    real = frame._show_update_step
    monkeypatch.setattr(frame, "_show_update_step", lambda step: steps.append(step) or real(step))
    frame.startup()
    assert steps == [4] and frame.notice == "update"
    write_status(frame, "done", f"Updated to version {__version__}")
    assert wait(lambda: frame.notice is None, 5)  # confirmed, then the usual start-up
