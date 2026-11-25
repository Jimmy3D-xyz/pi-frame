import importlib.util
import json
import time
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from photoframe import collage, sysmodes
from photoframe.config import ConfigError, ConfigStore
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.storage import Storage
from photoframe.web import create_app

from adminhelp import unlock

ROOT = Path(__file__).resolve().parents[1]
PANEL = (1920, 1200)


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def frame(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    cfg = ConfigStore(tmp_path)
    cfg.update({"transition": "none"})
    f = Frame(cfg, Storage(tmp_path, PANEL), FileDisplay(PANEL, str(tmp_path / "f.png")), tmp_path)
    for i in range(6):
        size = (1600, 2400) if i % 3 == 0 else (2400, 1600)
        f.storage.add("Default", f"p{i}.jpg", Image.new("RGB", size, (40 * i, 100, 150)))
    return f


# ---------------- collages ----------------
def test_collage_puts_portrait_photos_in_tall_spaces():
    boxes, order = collage.plan([1.5, 0.66, 1.5], (1920, 1200))
    portrait_box = boxes[order.index(1)]
    w, h = portrait_box[2] - portrait_box[0], portrait_box[3] - portrait_box[1]
    assert h > w  # the portrait photo got a tall space
    for n in (3, 4, 5):
        imgs = [Image.new("RGB", (300, 200))] * n
        assert collage.compose(imgs, (1920, 1200)).size == (1920, 1200)
        assert collage.compose(imgs, (1200, 1920)).size == (1200, 1920)  # portrait screen
    with pytest.raises(ValueError):
        collage.compose([Image.new("RGB", (10, 10))] * 2, (100, 100))


# ---------------- slideshow styles ----------------
def test_story_style_order_and_timing(frame):
    frame.cfg.update({"slideshow_style": "story", "collage_count": 3, "collage_minutes": 5, "single_minutes": 10})
    shown = []
    for _ in range(5):
        ok, msg = frame.rotate()
        assert ok
        shown.append(frame.state["current"]["filepath"])
    assert shown[0] == "Collage of 3 photos"
    photos = [p.split("/")[-1] for p in shown[1:4]]
    assert len(set(photos)) == 3  # each photo of the collage, full screen, in turn
    assert shown[4] == "Collage of 3 photos"  # then a new collage
    frame.cfg.update({"slideshow_style": "spotlight"})
    frame.reset_story()
    seq = []
    for _ in range(5):
        frame.rotate()
        seq.append(frame.state["current"]["filepath"].startswith("Collage"))
    assert seq == [True, False, True, False, True]  # collage, photo, collage, photo, ...


def test_story_step_timers(frame):
    frame.cfg.update({"slideshow_style": "story", "collage_count": 4, "collage_minutes": 5, "single_minutes": 10})
    frame.rotate()
    assert 4.9 * 60 < frame._step_due - time.time() <= 5 * 60  # collage: 5 minutes
    frame.rotate()
    assert 9.9 * 60 < frame._step_due - time.time() <= 10 * 60  # photo: 10 minutes


def test_too_few_photos_falls_back_to_classic(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    cfg = ConfigStore(tmp_path)
    cfg.update({"transition": "none", "slideshow_style": "story"})
    f = Frame(cfg, Storage(tmp_path, PANEL), FileDisplay(PANEL, str(tmp_path / "f.png")), tmp_path)
    f.storage.add("Default", "a.jpg", Image.new("RGB", (800, 600)))
    f.storage.add("Default", "b.jpg", Image.new("RGB", (800, 600)))
    ok, _ = f.rotate()
    assert ok and not f.state["current"]["filepath"].startswith("Collage")


def test_style_settings_validate(frame):
    with pytest.raises(ConfigError):
        frame.cfg.update({"collage_count": 6})
    with pytest.raises(ConfigError):
        frame.cfg.update({"clip_every_minutes": 7})
    with pytest.raises(ConfigError):
        frame.cfg.update({"slideshow_style": "chaos"})


# ---------------- video moments ----------------
def test_video_moment_plays_a_random_part(frame, tmp_path, monkeypatch):
    assert frame.play_clip() == (False, "no videos")
    frame.rotate()  # a photo on screen to come back to (before the video exists, so it can't be picked)
    vid = frame.storage.root / "Default" / "holiday.mp4"
    vid.write_bytes(b"x")
    monkeypatch.setattr(frame.storage, "video_meta", lambda p: {"duration": 100.0})
    calls = []
    monkeypatch.setattr(frame.player, "play", lambda path, **kw: calls.append(kw) or "finished")
    frame.cfg.update({"clip_seconds": 20})
    ok, msg = frame.play_clip()
    assert ok and "holiday.mp4" in msg
    for _ in range(50):
        if calls:
            break
        time.sleep(0.02)
    assert calls[0]["max_seconds"] == 20 and 0 <= calls[0]["start"] <= 80


# ---------------- the Video button ----------------
def add_video(frame, name, size, poster=True):
    vid = frame.storage.root / "Default" / name
    vid.write_bytes(b"x")
    meta = frame.storage._meta_path(vid)
    meta.parent.mkdir(exist_ok=True)
    meta.write_text(json.dumps({"width": size[0], "height": size[1], "duration": 12.0}))
    if poster:
        Image.new("RGB", size, (200, 30, 30)).save(frame.storage.poster_path(vid))
    return vid


def wait_for(cond, secs=3.0):
    end = time.time() + secs
    while time.time() < end and not cond():
        time.sleep(0.02)
    return cond()


def test_video_button_with_no_videos(frame):
    frame.rotate()
    assert frame.video_now() == (False, "There are no videos on the frame yet")
    r = create_app(frame).test_client().post("/api/video/now")
    assert r.status_code == 409


def test_video_button_plays_in_a_space_of_the_collage(frame, monkeypatch):
    frame.cfg.update({"slideshow_style": "living", "collage_count": 3})
    frame.rotate()
    base, boxes, _ = frame._collage_shown
    add_video(frame, "phone.mp4", (1080, 1920))  # a portrait phone video
    calls = []

    def play(path, **kw):  # as the real player: busy until it returns
        calls.append((path, kw))
        frame.player.playing = None
        return "finished"

    monkeypatch.setattr(frame.player, "play", play)
    ok, msg = frame.video_now()
    assert ok and msg == "Playing phone.mp4 in the collage"
    assert wait_for(lambda: calls)
    path, kw = calls[0]
    assert path.name == "phone.mp4"
    # it went into the tallest space, with the rest of the collage left around it
    tall = min(boxes, key=lambda b: (b[2] - b[0]) / (b[3] - b[1]))
    m = kw["surround"]["margins"]
    W, H = PANEL
    got = (round(m["left"] * W), round(m["top"] * H), round(W - m["right"] * W), round(H - m["bottom"] * H))
    if kw["cover"]:  # close to the space's shape: it fills the space
        assert got == tall
    else:  # the whole video, centred in the space
        assert tall[0] <= got[0] and tall[1] <= got[1] and got[2] <= tall[2] and got[3] <= tall[3]
        assert abs((got[2] - got[0]) / (got[3] - got[1]) - 1080 / 1920) < 0.01
    # afterwards the collage is back as it was, and the button works again
    assert wait_for(lambda: frame._base is base)
    assert frame._collage_shown[0] is base and frame.player.playing is None
    assert not kw["surround"]["file"].exists()


def test_video_button_shapes(frame, monkeypatch):
    """A video close to its space's shape fills it; a wide video in a tall space is shown whole."""
    frame.cfg.update({"slideshow_style": "living", "collage_count": 4})
    frame.rotate()
    _, boxes, _ = frame._collage_shown
    b = boxes[0]
    calls = []

    def play(path, **kw):
        calls.append(kw)
        frame.player.playing = None
        return "finished"

    monkeypatch.setattr(frame.player, "play", play)
    add_video(frame, "same.mp4", (b[2] - b[0], b[3] - b[1]))  # exactly a space's shape
    assert frame.video_now()[0] and wait_for(lambda: calls and frame.player.playing is None)
    assert calls[0]["cover"] is True


def test_video_button_plays_full_screen_without_a_collage(frame, monkeypatch):
    frame.rotate()  # classic: a photo
    add_video(frame, "beach.mp4", (1920, 1080))
    started = []
    monkeypatch.setattr(frame, "_start_video", lambda p: started.append(p))
    ok, msg = frame.video_now()
    assert ok and started[0].name == "beach.mp4"


def test_video_button_waits_for_a_playing_video(frame):
    frame.rotate()
    add_video(frame, "beach.mp4", (1920, 1080))
    frame.player.playing = Path("x.mp4")
    assert frame.video_now() == (False, "A video is already playing")


# ---------------- jokes and facts ----------------
def test_fun_deck_no_repeats_and_own_lines(frame):
    fun = frame.overlay.fun
    frame.cfg.update({"overlay_fun": "jokes"})
    seen = set()
    for _ in range(len(fun.builtin["jokes"])):
        fun.next()
        assert fun.line[1] not in seen
        seen.add(fun.line[1])
    frame.cfg.update({"overlay_fun": "facts", "fun_custom": ["Happy anniversary, Sam!"]})
    texts = set()
    for _ in range(len(fun.builtin["facts"]) + 1):
        fun.next()
        texts.add(fun.line)
    assert ("own", "Happy anniversary, Sam!") in texts and all(k in ("fact", "own") for k, _ in texts)
    frame.cfg.update({"overlay_fun": "off"})
    fun.next()
    assert fun.line is None


def test_fun_panel_draws(frame):
    frame.cfg.update({"overlay_fun": "facts", "overlay_clock": False, "overlay_date": False, "overlay_photo_info": False})
    base = Image.new("RGB", PANEL, (230, 230, 230))
    out = frame.overlay.apply(base, {}, datetime(2026, 10, 2, 12, 0))
    assert min(out.crop((40, 40, 500, 200)).convert("L").getdata()) < 120  # glass panel top-left
    assert min(out.crop((40, 1000, 500, 1160)).convert("L").getdata()) > 200  # nothing bottom-left


# ---------------- screen resolution ----------------
def fake_sysfs(tmp_path, modes="1920x1200\n1920x1080\n1920x1080i\n2560x1440\n1920x1080\n"):  # noqa: E501
    conn = tmp_path / "drm" / "card1-HDMI-A-1"
    conn.mkdir(parents=True)
    (conn / "status").write_text("connected\n")
    (conn / "modes").write_text(modes)
    other = tmp_path / "drm" / "card1-HDMI-A-2"
    other.mkdir()
    (other / "status").write_text("disconnected\n")
    return str(tmp_path / "drm")


def test_screen_modes_and_cmdline(tmp_path):
    conn, modes = sysmodes.screen_modes(fake_sysfs(tmp_path))
    # no duplicates, no interlaced, nothing bigger than the native 1920x1200
    assert conn == "HDMI-A-1" and modes == ["1920x1200", "1920x1080"]
    line = "console=serial0,115200 console=tty3 root=PARTUUID=ad496537-02 video=HDMI-A-1:1920x1200-32@60 loglevel=5\n"
    out = sysmodes.set_mode_in_cmdline(line, "HDMI-A-1", "2560x1440").split()
    # the developer's new 24" (H24T7): native 2560x1440, also accepts 4K, which is left out
    h24 = ["2560x1440", "3840x2160", "3840x2160", "1920x1080", "2560x1080", "1280x720"]
    assert sysmodes.up_to_native(h24) == ["2560x1440", "1920x1080", "2560x1080", "1280x720"]  # ultrawide fits, kept
    assert "video=HDMI-A-1:2560x1440-32@60" in out and "video=HDMI-A-1:1920x1200-32@60" not in out
    assert "loglevel=5" in out and "console=tty3" in out
    assert sysmodes.current_mode_setting(line) == "1920x1200"


def test_helper_applies_only_offered_modes(tmp_path, monkeypatch):
    h = load("system_helper")
    data, boot = tmp_path / "data", tmp_path / "boot"
    (data / "system").mkdir(parents=True)
    boot.mkdir()
    line = "console=tty3 root=PARTUUID=x video=HDMI-A-1:1920x1200-32@60\n"
    (boot / "cmdline.txt").write_text(line)
    monkeypatch.setattr(h, "DATA", data)
    sysfs = fake_sysfs(tmp_path)
    (data / "system" / "request.json").write_text(json.dumps({"action": "set_mode", "mode": "2560x1440"}))
    assert h.handle_request(boot, sysfs, reboot=False) == 1  # bigger than this screen's native: refused
    assert (boot / "cmdline.txt").read_text() == line
    assert "doesn't offer" in json.loads((data / "system" / "status.json").read_text())["message"]
    remade = []
    monkeypatch.setattr(h, "remake_bootsplash", lambda mode=None: remade.append(mode) or 0)
    (data / "system" / "request.json").write_text(json.dumps({"action": "set_mode", "mode": "1920x1080"}))
    assert h.handle_request(boot, sysfs, reboot=False) == 0
    assert remade == ["1920x1080"]  # boot images made for the new size before restarting
    assert "video=HDMI-A-1:1920x1080-32@60" in (boot / "cmdline.txt").read_text()
    assert (boot / "cmdline.txt.pre-resolution").read_text() == line  # backed up first
    assert not (data / "system" / "request.json").exists()


def test_web_resolution_request(frame, tmp_path, monkeypatch):
    monkeypatch.setattr(sysmodes, "screen_modes", lambda sysfs="/sys/class/drm": ("HDMI-A-1", ["1920x1200", "2560x1440"]))
    monkeypatch.setattr(frame, "show_power_card", lambda restarting: None)
    c = create_app(frame).test_client()
    r = c.get("/api/display/modes").get_json()
    assert r["modes"] == ["1920x1200", "2560x1440"] and r["native"] == "1920x1200"
    assert c.post("/api/display/mode", json={"mode": "1920x1200"}).status_code == 403  # an admin option
    unlock(c, frame)
    assert c.post("/api/display/mode", json={"mode": "1024x768"}).status_code == 400
    assert c.post("/api/display/mode", json={"mode": "2560x1440"}).status_code == 200
    assert json.loads((frame.data_dir / "system" / "request.json").read_text())["mode"] == "2560x1440"


# ---------------- boot screen note ----------------
def test_boot_note_leaves_logo_untouched():
    bs = load("bootsplash")
    kernel, full = bs.make_images((1920, 1200))
    noted = bs.add_note(full, (1920, 1200))
    logo = (700, 300, 1220, 860)
    assert list(noted.crop(logo).getdata()) == list(full.crop(logo).getdata())  # logo pixels unchanged
    below = (600, 860, 1320, 1000)
    assert list(noted.crop(below).getdata()) != list(full.crop(below).getdata())  # the note is there



def test_recheck_the_screen(tmp_path, monkeypatch, frame):
    h = load("system_helper")
    data = tmp_path / "data"
    (data / "system").mkdir(parents=True)
    monkeypatch.setattr(h, "DATA", data)
    monkeypatch.setattr(h.time, "sleep", lambda s: None)
    sysfs = fake_sysfs(tmp_path)
    written = []
    real_detect = h.detect_screens

    def kernel_like(path):
        # writing "detect" is a command to the real kernel; afterwards the file
        # still reads "connected"/"disconnected". Record the writes, then restore.
        before = {p: Path(p).read_text() for p in Path(path).glob("card*-HDMI-A-*/status")}
        n = real_detect(path)
        for p, text in before.items():
            written.append(Path(p).read_text())
            Path(p).write_text(text)
        return n

    monkeypatch.setattr(h, "detect_screens", kernel_like)
    (data / "system" / "request.json").write_text(json.dumps({"action": "detect"}))
    assert h.handle_request(tmp_path, sysfs, reboot=False) == 0
    assert written == ["detect", "detect"]  # both HDMI ports asked to re-check
    assert "1920 × 1200 native" in json.loads((data / "system" / "status.json").read_text())["message"]
    c = create_app(frame).test_client()
    unlock(c, frame)
    assert c.post("/api/display/detect").status_code == 200
    assert json.loads((frame.data_dir / "system" / "request.json").read_text())["action"] == "detect"



def test_boot_images_out_of_date(tmp_path, monkeypatch):
    h = load("system_helper")
    png, meta, data = tmp_path / "splash.png", tmp_path / "splash.json", tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(h, "SPLASH_PNG", png)
    monkeypatch.setattr(h, "SPLASH_META", meta)
    monkeypatch.setattr(h, "DATA", data)
    fb = tmp_path / "virtual_size"
    fb.write_text("2560,1440\n")
    assert h.splash_out_of_date(str(fb)) is None  # boot screen not set up: nothing to do
    Image.new("RGB", (1920, 1200)).save(png)
    assert "2560x1440" in h.splash_out_of_date(str(fb))  # made by an older version, for the old screen
    meta.write_text(json.dumps({"size": [2560, 1440], "orientation": "landscape", "rotation": 0}))
    (data / "config.json").write_text(json.dumps({"config": {"display_orientation": "landscape"}}))
    assert h.splash_out_of_date(str(fb)) is None  # matches: left alone
    (data / "config.json").write_text(json.dumps({"config": {"display_orientation": "portrait"}}))
    assert "portrait" in h.splash_out_of_date(str(fb))  # turned: remade


def test_early_redraw_never_stretches(tmp_path, monkeypatch):
    import numpy as np
    from photoframe import console
    cs = load("console_splash")
    monkeypatch.setattr(console, "set_graphics", lambda on=True, tty="/dev/tty1": True)
    w, h = 256, 144  # a 16:9 screen
    fb = tmp_path / "fb0"
    fb.write_bytes(b"\0" * w * h * 4)
    splash = tmp_path / "splash.png"
    Image.new("RGB", (160, 100), (200, 80, 40)).save(splash)  # made for a 16:10 screen
    cs.main(str(fb), splash, geometry=(w, h, 32, w * 4, 16))
    px = np.frombuffer(fb.read_bytes(), np.uint8).reshape(h, w, 4)
    assert tuple(px[72, 3, :3]) == (0, 0, 0)  # black bars at the sides: shape kept, not stretched
    assert tuple(px[72, 128, :3]) == (40, 80, 200)  # the picture in the middle
