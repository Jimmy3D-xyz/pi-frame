import time
import types

import numpy as np
import pytest
from PIL import Image

from photoframe import display as dmod
from photoframe import kms


class FakeMode:
    def __init__(self, w, h, hz):
        self.hdisplay, self.vdisplay, self.vrefresh = w, h, hz


def fake_pykms(has_drop_master=True):
    """Behaves like python3-kms++ on the developer's Pi (same calls as kms_fade_test.py)."""
    m = types.SimpleNamespace()
    log = {"commits": [], "set_mode": [], "master": [], "cards": 0}
    m.log = log
    m.PixelFormat = types.SimpleNamespace(XRGB8888="XR24")

    class Card:
        def __init__(self):
            log["cards"] += 1
        if has_drop_master:
            def drop_master(self):
                log["master"].append("drop")

            def set_master(self):
                log["master"].append("set")

    class Conn:
        fullname = "HDMI-A-1"

        def get_modes(self):
            return [FakeMode(2560, 1440, 60), FakeMode(1920, 1200, 60), FakeMode(1920, 1200, 50)]

        def get_default_mode(self):
            return FakeMode(2560, 1440, 60)

    class Obj:
        def __init__(self, id_):
            self.id = id_

    class Crtc(Obj):
        def set_mode(self, conn, fb, mode):
            log["set_mode"].append((fb.id, mode.hdisplay, mode.vdisplay))

    class ResourceManager:
        def __init__(self, card):
            pass

        def reserve_connector(self):
            return Conn()

        def reserve_crtc(self, conn):
            return Crtc(50)

        def reserve_primary_plane(self, crtc, fmt):
            return Obj(91)

        def reserve_overlay_plane(self, crtc, fmt):
            return Obj(127)

    ids = iter(range(200, 10000))

    class DumbFramebuffer:
        def __init__(self, card, w, h, fmt):
            self.id = next(ids)
            self._w, self._h = w, h
            self.buf = bytearray(w * 4 * h)

        def map(self, plane):
            return self.buf

        def stride(self, plane):
            return self._w * 4

    class AtomicReq:
        def __init__(self, card):
            self.items = []

        def add(self, obj, props, value=None):
            self.items.append((obj.id, dict(props) if isinstance(props, dict) else {props: value}))

        def commit_sync(self, allow_modeset=False):
            log["commits"].append(self.items)
            time.sleep(0.002)  # a real commit waits for the next screen refresh

    m.Card, m.ResourceManager, m.DumbFramebuffer, m.AtomicReq = Card, ResourceManager, DumbFramebuffer, AtomicReq
    return m


@pytest.fixture
def disp(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.console.set_graphics", lambda on=True, tty="/dev/tty1": True)
    size = tmp_path / "virtual_size"
    size.write_text("1920,1200\n")  # the boot settings chose 1920x1200 (not the screen's best 2560x1440)
    pk = fake_pykms()
    return kms.KmsDisplay(pk, fb_size_file=str(size)), pk


def pixels(d, idx):
    return d.px[idx].reshape(d.size[1], d.size[0], 4)


def test_uses_the_resolution_the_boot_settings_chose(disp):
    d, pk = disp
    assert d.size == (1920, 1200) and pk.log["set_mode"][0][1:] == (1920, 1200)


def test_plain_change_flips_at_the_refresh(disp):
    d, pk = disp
    d.show(Image.new("RGB", d.size, (200, 40, 10)))
    last = pk.log["commits"][-1]
    assert last == [(91, {"FB_ID": d.fbs[d.front].id})]  # the bottom layer switched buffers
    assert tuple(pixels(d, d.front)[5, 5, :3]) == (10, 40, 200)  # BGRX


def test_hardware_fade(disp):
    d, pk = disp
    d.show(Image.new("RGB", d.size, (255, 0, 0)))
    pk.log["commits"].clear()
    d.show(Image.new("RGB", d.size, (0, 0, 255)), fade_ms=200)
    commits = pk.log["commits"]
    first = commits[0][0]
    assert first[0] == 127 and first[1]["alpha"] == 0 and first[1]["FB_ID"] == d.fbs[d.front].id  # new picture on top, invisible
    alphas = [c[0][1]["alpha"] for c in commits[1:-1]]
    assert len(alphas) > 5 and alphas == sorted(alphas) and alphas[-1] > 60000  # opacity rises smoothly
    assert commits[-1] == [(91, {"FB_ID": d.fbs[d.front].id}), (127, {"FB_ID": 0, "CRTC_ID": 0})]  # tidy up in one step
    assert d.last_fade["hardware"] and d.last_fade["frames"] == len(alphas)


def test_screen_power_through_the_hardware(disp):
    d, pk = disp
    d.set_power(False)
    assert pk.log["commits"][-1] == [(50, {"ACTIVE": 0})]
    d.set_power(True)
    assert (50, {"ACTIVE": 1}) in [i for c in pk.log["commits"] for i in c]


def test_handing_the_screen_to_mpv_and_back(disp):
    d, pk = disp
    d.show(Image.new("RGB", d.size, (10, 200, 10)))
    d.release()
    assert pk.log["master"] == ["drop"]
    n = len(pk.log["commits"])
    poster = Image.new("RGB", d.size, (1, 2, 3))
    d.show(poster)  # while mpv has the screen: remembered, not drawn
    assert len(pk.log["commits"]) == n
    d.reacquire()
    assert pk.log["master"] == ["drop", "set"]
    assert tuple(pixels(d, d.front)[0, 0, :3]) == (3, 2, 1)  # redrawn with the latest picture
    assert pk.log["set_mode"][-1][0] == d.fbs[d.front].id and pk.log["commits"][-1] == [(127, {"FB_ID": 0, "CRTC_ID": 0})]


def test_handover_without_drop_master(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.console.set_graphics", lambda on=True, tty="/dev/tty1": True)
    pk = fake_pykms(has_drop_master=False)
    d = kms.KmsDisplay(pk, fb_size_file=str(tmp_path / "missing"))
    assert d.size == (2560, 1440)  # no boot setting: the screen's best
    d.release()
    assert not hasattr(d, "card")  # closed, so mpv can open it
    d.reacquire()
    assert pk.log["cards"] == 2 and hasattr(d, "card")  # opened again


def test_falls_back_to_the_framebuffer(monkeypatch):
    made = []
    monkeypatch.setattr(dmod, "FramebufferDisplay", lambda dev: made.append(dev) or "fb")
    monkeypatch.setattr(kms, "available", lambda: (True, ""))

    def broken(*a, **k):
        raise RuntimeError("the display hardware has no free layers")

    monkeypatch.setattr(kms, "KmsDisplay", broken)
    assert dmod.make_display("fb") == "fb" and made == ["/dev/fb0"]
    made.clear()
    assert dmod.make_display("fb", hardware=False) == "fb"  # switched off in the settings


def test_frame_takes_the_screen_back_even_if_a_video_fails(tmp_path, monkeypatch):
    from photoframe.config import ConfigStore
    from photoframe.display import FileDisplay
    from photoframe.frame import Frame
    from photoframe.storage import Storage
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    cfg = ConfigStore(tmp_path)
    disp = FileDisplay((320, 200), str(tmp_path / "f.png"))
    calls = []
    disp.release = lambda: calls.append("release")
    disp.reacquire = lambda: calls.append("reacquire")
    f = Frame(cfg, Storage(tmp_path, (320, 200)), disp, tmp_path)
    f.storage.add("Default", "a.jpg", Image.new("RGB", (400, 300)))
    f.rotate()
    (f.storage.root / "Default" / "clip.mp4").write_bytes(b"x")
    monkeypatch.setattr(f.storage, "video_meta", lambda p: {"duration": 60.0})

    def crash(*a, **k):
        raise RuntimeError("mpv crashed")

    monkeypatch.setattr(f.player, "play", crash)
    f.play_clip()
    for _ in range(100):
        if "reacquire" in calls:
            break
        time.sleep(0.01)
    assert calls == ["release", "reacquire"]  # the screen came back despite the crash



def test_steps_aside_through_its_own_connection(tmp_path, monkeypatch):
    """the developer's kms++ has no drop_master() but exposes fd: hand over with the DRM ioctls."""
    monkeypatch.setattr("photoframe.console.set_graphics", lambda on=True, tty="/dev/tty1": True)
    pk = fake_pykms(has_drop_master=False)
    real_card = pk.Card

    class CardWithFd(real_card):
        fd = 42

    pk.Card = CardWithFd
    calls = []
    monkeypatch.setattr(kms.fcntl, "ioctl", lambda fd, req, *a: calls.append((fd, req)))
    d = kms.KmsDisplay(pk, fb_size_file=str(tmp_path / "missing"))
    d.release()
    assert calls == [(42, kms.DRM_IOCTL_DROP_MASTER)] and hasattr(d, "card")  # stepped aside, connection kept
    d.reacquire()
    assert calls[-1] == (42, kms.DRM_IOCTL_SET_MASTER) and pk.log["cards"] == 1  # took it back, no reopening
    assert pk.log["set_mode"][-1][0] == d.fbs[d.front].id  # and redrew the screen



def test_real_screens_really_play_videos(tmp_path, monkeypatch, disp):
    """The bug since 0.20.0: with the display hardware, videos were only pretended."""
    from photoframe.config import ConfigStore
    from photoframe.display import FileDisplay
    from photoframe.frame import Frame
    from photoframe.storage import Storage
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    d, pk = disp
    hw = Frame(ConfigStore(tmp_path), Storage(tmp_path, d.size), d, tmp_path)
    assert hw.player.simulate is False  # display hardware: mpv really runs
    (tmp_path / "t").mkdir()
    test = Frame(ConfigStore(tmp_path / "t"), Storage(tmp_path / "t", (320, 200)),
                 FileDisplay((320, 200), str(tmp_path / "f.png")), tmp_path / "t")
    assert test.player.simulate is True  # only the test display pretends
