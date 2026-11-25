import json
import os
import socket
import threading
from pathlib import Path

import pytest
from PIL import Image

from photoframe import surround, video
from photoframe.config import ConfigStore
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.storage import Storage


def test_plan():
    # the developer's 1440x2560 portrait screen, a 1080p landscape video: bands above and below
    window, spaces = surround.plan((1440, 2560), (1920, 1080))
    assert window == (0, 875, 1440, 1685) and spaces == [(0, 0, 1440, 875), (0, 1685, 1440, 2560)]
    # a portrait phone video on a landscape screen: spaces at the sides
    window, spaces = surround.plan((2560, 1440), (1080, 1920))
    assert spaces[0][2] == window[0] and spaces[1][0] == window[2] and spaces[0][3] == 1440
    # fills (or nearly fills) the screen: nothing to do
    assert surround.plan((2560, 1440), (1920, 1080))[1] == []
    assert surround.plan((1920, 1200), (1920, 1080))[1] == []  # thin bands aren't worth filling


def test_pick_suits_the_spaces():
    wide, tall = (0, 0, 1440, 875), (0, 0, 700, 1440)
    chosen = surround.pick([wide, tall], [("portrait", 0.66), ("landscape", 1.5), ("square", 1.0)])
    assert chosen == ["landscape", "portrait"]


def test_compose_has_a_transparent_window():
    window, spaces = surround.plan((360, 640), (640, 360))
    img = surround.compose((360, 640), window, spaces, [Image.new("RGB", (900, 600), (200, 0, 0))] * 2)
    assert img.getpixel((180, 320))[3] == 0  # the video shows through
    assert img.getpixel((180, 100)) == (200, 0, 0, 255)  # a photo above
    assert img.getpixel((1, 1))[3] == 255  # the mat


@pytest.fixture
def frame_for(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)

    count = [0]

    def make(panel, orientation="landscape", photos=3):
        count[0] += 1
        d = tmp_path / f"frame{count[0]}"
        d.mkdir()
        cfg = ConfigStore(d)
        cfg.update({"transition": "none", "display_orientation": orientation})
        f = Frame(cfg, Storage(d, panel), FileDisplay(panel, str(d / "f.png")), d)
        for i in range(photos):
            f.storage.add("Default", f"p{i}.jpg", Image.new("RGB", (900, 600), (60 * i, 100, 150)))
        f.storage.video_meta = lambda p: {"width": 640, "height": 360}
        return f
    return make


def test_rotated_portrait_screen(frame_for):
    """A landscape monitor turned on its side: above/below in the upright view is
    left/right in the monitor's own coordinates, which is what mpv uses."""
    f = frame_for((640, 360), "portrait")
    sur = f._surround_for(Path("v.mp4"), Image.new("RGB", (360, 640), (9, 9, 9)))
    m = sur["margins"]
    assert m["top"] == 0 and m["bottom"] == 0 and m["left"] == pytest.approx(0.342, abs=0.005) == pytest.approx(m["right"], abs=0.005)
    assert sur["size"] == (640, 360) and Path(sur["file"]).stat().st_size == 640 * 360 * 4
    assert sur["preview"].getpixel((180, 320)) == (9, 9, 9)  # the opening frame shows in the window


def test_native_portrait_screen(frame_for):
    f = frame_for((360, 640), "portrait")
    m = f._surround_for(Path("v.mp4"), None)["margins"]
    assert m["left"] == 0 and m["top"] == pytest.approx(0.342, abs=0.005)


def test_when_not_to(frame_for):
    assert frame_for((640, 360))._surround_for(Path("v.mp4"), None) is None  # landscape video fills a landscape screen
    f = frame_for((360, 640), "portrait")
    f.cfg.update({"video_surround": False})
    assert f._surround_for(Path("v.mp4"), None) is None  # switched off
    f = frame_for((360, 640), "portrait", photos=1)
    assert f._surround_for(Path("v.mp4"), None) is None  # not enough photos


def test_hand_over_sends_the_pictures_before_the_video(tmp_path):
    sock_path = str(tmp_path / "mpv2.sock")
    got = []
    server = socket.socket(socket.AF_UNIX)
    server.bind(sock_path)
    server.listen(1)

    def fake_mpv():
        conn, _ = server.accept()
        f = conn.makefile("rb")
        for _ in range(2):
            got.append(json.loads(f.readline().decode())["command"])
            conn.sendall(b'{"event":"idle"}\n{"data":null,"request_id":0,"error":"success"}\n')
        conn.close()

    t = threading.Thread(target=fake_mpv, daemon=True)
    t.start()
    sur = {"file": tmp_path / "s.bgra", "size": (100, 50), "socket": sock_path, "margins": {}}
    assert video.hand_over(sur, tmp_path / "v.mp4") is True
    t.join(2)
    assert got[0][0] == "overlay-add" and got[1] == ["loadfile", str(tmp_path / "v.mp4")]


def test_player_settings_and_overlay(tmp_path, monkeypatch):
    sock_path = str(tmp_path / "mpv.sock")
    got = {}
    server = socket.socket(socket.AF_UNIX)
    server.bind(sock_path)
    server.listen(1)

    def fake_mpv():  # answers like mpv's control socket
        conn, _ = server.accept()
        got["cmd"] = json.loads(conn.recv(65536).decode())
        conn.sendall(b'{"data":null,"request_id":0,"error":"success"}\n')
        conn.close()

    t = threading.Thread(target=fake_mpv, daemon=True)
    t.start()
    sur = {"file": tmp_path / "s.bgra", "size": (1440, 2560), "socket": sock_path,
           "margins": {"left": 0, "right": 0, "top": 0.342, "bottom": 0.342}}
    assert video.send_overlay(sur) is True
    t.join(2)
    assert got["cmd"]["command"] == ["overlay-add", 0, 0, 0, str(tmp_path / "s.bgra"), 0, "bgra", 1440, 2560, 1440 * 4]
    captured = {}

    class FakeProc:
        returncode = 0

        def __init__(self, args, **kw):
            captured["args"] = args

        def communicate(self):
            return "", ""

        def poll(self):
            return 0

    monkeypatch.setattr(video.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(video.subprocess, "Popen", FakeProc)
    handed = []
    monkeypatch.setattr(video, "hand_over", lambda s, p: handed.append(p) or True)
    player = video.VideoPlayer(simulate=False)
    player.drm_mode = "3"
    player.play(tmp_path / "v.mp4", rotate=0, cover=False, sound=False, volume=50,
                max_seconds=0, duration=10, surround=sur)
    a = captured["args"]
    # mpv waits for the pictures around the video, then gets the video (no black flash first)
    assert "--idle=once" in a and str(tmp_path / "v.mp4") not in a
    assert handed == [tmp_path / "v.mp4"]
    assert "--drm-mode=3" in a  # keeps the frame's screen setting
    assert "--video-margin-ratio-top=0.34200" in a and "--video-margin-ratio-bottom=0.34200" in a
    assert f"--input-ipc-server={sock_path}" in a
