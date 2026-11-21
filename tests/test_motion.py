"""Moving transitions: geometry, 3D turns, the display hardware's moves, and the slideshow using them."""

import time

import numpy as np
import pytest
from PIL import Image, ImageDraw

from photoframe import collage, motion, render
from photoframe.config import ConfigStore
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.storage import Storage

from test_kms import disp  # noqa: F401  (the fake display hardware)

PANEL = (1920, 1080)
frames_for = motion.frames_for  # (the slideshow tests swap it for a quick one)


def photo(w, h, seed=0):
    """A photo with a pattern, so crops and zooms can be checked."""
    img = Image.new("RGB", (w, h), (30 + seed * 20, 90, 160))
    d = ImageDraw.Draw(img)
    for i in range(0, w, max(8, w // 12)):
        d.rectangle([i, 0, i + w // 30, h], fill=(220, 200 - seed * 10, 40))
    for j in range(0, h, max(8, h // 9)):
        d.rectangle([0, j, w, j + h // 40], fill=(250, 250, 250))
    return img


# ---------------- geometry ----------------
@pytest.mark.parametrize("scale", ["fit"])
@pytest.mark.parametrize("shape", [(1600, 1200), (1000, 1600), (2400, 900)])
def test_a_collage_space_matches_its_part_of_the_full_screen_photo(scale, shape):
    """A zoom runs between a collage space and part of the photo on its own: they must show the same."""
    logical = (1920, 1080)
    imgs = [photo(*shape, 1), photo(1500, 1000, 2), photo(900, 1400, 3)]
    canvas, boxes = collage.layout(imgs, logical)
    box = boxes[0]
    full = render.place(imgs[0], logical, scale, "black")
    x, y, w, h = motion.tile_source(imgs[0].size, box, logical, scale)
    from_full = np.asarray(full.crop((x, y, x + w, y + h)).resize((box[2] - box[0], box[3] - box[1])), dtype=float)
    tile = np.asarray(canvas.crop(box), dtype=float)
    assert np.abs(from_full - tile).mean() < 12  # the same picture (give or take resampling)


def test_cover_keeps_the_zoom_on_screen():
    """With fill (cover), part of a collage space can be cut off the full-screen photo: stay on screen."""
    for shape in [(1600, 1200), (1000, 1600), (2400, 900)]:
        x, y, w, h = motion.tile_source(shape, (1194, 12, 1908, 534), (1920, 1080), "cover")
        assert x >= 0 and y >= 0 and x + w <= 1920 and y + h <= 1080


def test_rect_to_panel_matches_to_panel():
    """A marked rectangle of the logical picture lands where render.to_panel puts it, at every rotation."""
    for logical, panel, extra in [((1920, 1080), (1920, 1080), 0), ((1920, 1080), (1920, 1080), 180),
                                  ((1080, 1920), (1920, 1080), 0), ((1080, 1920), (1920, 1080), 180),
                                  ((1080, 1920), (2560, 1440), 0)]:
        rect = (100, 300, 400, 200)
        img = Image.new("L", logical, 0)
        img.paste(255, (rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]))
        on_panel = render.to_panel(img, panel, extra)
        x, y, w, h = motion.rect_to_panel(rect, logical, panel, extra)
        bbox = on_panel.point(lambda v: 255 if v > 128 else 0).getbbox()
        assert abs(bbox[0] - x) <= 2 and abs(bbox[1] - y) <= 2, (logical, panel, extra)
        assert abs(bbox[2] - (x + w)) <= 2 and abs(bbox[3] - (y + h)) <= 2, (logical, panel, extra)


# ---------------- 3D turns ----------------
@pytest.mark.parametrize("kind", motion.KINDS_3D)
def test_3d_turns_start_at_the_old_picture_and_end_at_the_new(kind):
    old, new = photo(640, 360, 0), photo(640, 360, 5).transpose(Image.FLIP_LEFT_RIGHT)
    a = np.asarray(motion.frame_3d(kind, old, new, 0.0), dtype=float)
    z = np.asarray(motion.frame_3d(kind, old, new, 1.0), dtype=float)
    assert np.abs(a - np.asarray(old, dtype=float)).mean() < 6
    assert np.abs(z - np.asarray(new, dtype=float)).mean() < 6
    mid = np.asarray(motion.frame_3d(kind, old, new, 0.5), dtype=float)
    assert np.abs(mid - a).mean() > 10 and np.abs(mid - z).mean() > 10  # something's moving


def test_turns_are_drawn_in_the_background_for_the_panel():
    old, new = photo(1080, 1920, 0), photo(1080, 1920, 3)
    turn = motion.Turn("flip", old, lambda: new, (1080, 1920), (2560, 1440), 0, key="k", frames=6)
    assert turn.wait(30)
    assert turn.new_full is new
    assert turn.size == (1280, 720)  # half a 1440p panel, turned like the panel
    assert len(turn.frames) == 6 and all(len(f) == 1280 * 720 * 4 for f in turn.frames)


def test_a_failed_turn_is_just_not_ready():
    turn = motion.Turn("flip", photo(100, 60), lambda: 1 / 0, (100, 60), (100, 60), 0)
    assert not turn.wait(10) and turn.error


# ---------------- the display hardware ----------------
def test_zoom_in_grows_the_new_picture_from_its_space(disp):  # noqa: F811
    d, pk = disp
    d.show(Image.new("RGB", d.size, (255, 0, 0)))
    pk.log["commits"].clear()
    d.zoom_in(Image.new("RGB", d.size, (0, 0, 255)), (200, 100, 800, 450), (40, 60, 400, 225), 120)
    commits = pk.log["commits"]
    first = commits[0][0][1]
    assert commits[0][0][0] == 127 and abs(first["CRTC_X"] - 40) + abs(first["CRTC_Y"] - 60) < 4  # starts in the space
    assert abs(first["SRC_X"] / 65536 - 200) < 3 and first["FB_ID"] == d.fbs[d.front].id
    widths = [c[0][1]["CRTC_W"] for c in commits[:-1]]
    assert widths == sorted(widths) and widths[-1] > d.size[0] * 0.9  # and grows to fill the screen
    assert commits[-1] == [(91, d._plane_props(d.fbs[d.front])), (127, {"FB_ID": 0, "CRTC_ID": 0})]
    assert pixels_of(d)[0, 0, 0] == 255  # BGRX: blue
    assert d.last_fade["kind"] == "zoom in"


def test_zoom_out_shrinks_the_old_picture_into_the_collage(disp):  # noqa: F811
    d, pk = disp
    d.show(Image.new("RGB", d.size, (255, 0, 0)))
    old_fb = d.fbs[d.front].id
    pk.log["commits"].clear()
    d.zoom_out(Image.new("RGB", d.size, (0, 255, 0)), (200, 100, 800, 450), (40, 60, 400, 225), 120)
    commits = pk.log["commits"]
    assert commits[0][0][1]["FB_ID"] == old_fb and commits[0][0][1]["CRTC_W"] == d.size[0]  # old picture on top
    assert commits[0][1] == (91, d._plane_props(d.fbs[d.front]))  # new one underneath from the start
    last_move = commits[-2][0][1]
    assert last_move["CRTC_W"] < 500 and last_move["CRTC_X"] < 100  # ends in its space
    assert d._current.getpixel((0, 0)) == (0, 255, 0)


def test_slides_are_cut_off_at_the_screen_edge(disp):  # noqa: F811
    d, pk = disp
    W, H = d.size
    d.show(Image.new("RGB", d.size, (255, 0, 0)))
    pk.log["commits"].clear()
    d.slide_in(Image.new("RGB", d.size, (0, 0, 255)), (W - 500, 300, 400, 300), 150)
    moves = [c[0][1] for c in pk.log["commits"][:-1] if c[0][1].get("FB_ID")]
    assert moves
    for m in moves:  # never past the edge, and the picture's part matches what's on screen
        assert m["CRTC_X"] >= 0 and m["CRTC_X"] + m["CRTC_W"] <= W + 1
        assert abs((m["SRC_W"] >> 16) - m["CRTC_W"]) <= 2
    assert moves[-1]["CRTC_X"] < W - 450  # nearly in place by the end


def test_flip_book_plays_small_frames_scaled_up(disp):  # noqa: F811
    d, pk = disp
    d.show(Image.new("RGB", d.size, (255, 0, 0)))
    pk.log["commits"].clear()
    frames = [bytes([i * 20, 0, 0, 0]) * (320 * 200) for i in range(8)]
    d.play(frames, (320, 200), Image.new("RGB", d.size, (0, 0, 255)), 100, fade_ms=0)
    plays = [c[0][1] for c in pk.log["commits"] if c[0][0] == 91 and c[0][1].get("SRC_W") == 320 << 16]
    assert plays and all(p["CRTC_W"] == d.size[0] for p in plays)  # a small frame, filling the screen
    assert pk.log["commits"][-1][0] == (91, d._plane_props(d.fbs[d.front]))  # then the real picture
    assert d._current.getpixel((5, 5)) == (0, 0, 255)


def test_moves_need_a_picture_on_screen_and_the_screen_on(disp):  # noqa: F811
    d, pk = disp
    d.zoom_in(Image.new("RGB", d.size, (0, 0, 255)), (0, 0, 10, 10), (0, 0, 10, 10), 100)  # nothing before: plain
    assert not any(c[0][0] == 127 for c in pk.log["commits"])
    d.set_power(False)
    pk.log["commits"].clear()
    d.slide_in(Image.new("RGB", d.size, (0, 255, 0)), (0, 0, 100, 100), 100)
    assert not any(c[0][0] == 127 for c in pk.log["commits"])
    assert d._current.getpixel((0, 0)) == (0, 255, 0)


def pixels_of(d):
    return d.px[d.front].reshape(d.size[1], d.size[0], 4)


# ---------------- the slideshow ----------------
class MovingDisplay(FileDisplay):
    """A test screen that says it can move pictures, and notes what it was asked to do."""
    motions = True

    def __init__(self, *a):
        super().__init__(*a)
        self.calls = []

    def show(self, img, fade_ms=0):
        self.calls.append(("show", fade_ms))
        super().show(img, 0)

    def zoom_in(self, img, src, dst, ms, fade_ms=0):
        self.calls.append(("zoom-in", src, dst))
        super().show(img, 0)

    def zoom_out(self, img, src, dst, ms, fade_ms=0):
        self.calls.append(("zoom-out", src, dst))
        super().show(img, 0)

    def slide_in(self, img, rect, ms, fade_ms=0):
        self.calls.append(("slide", rect))
        super().show(img, 0)

    def play(self, frames, size, img, ms, fade_ms=0, name=""):
        self.calls.append(("3d", len(frames), size))
        super().show(img, 0)

    def kinds(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def mframe(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    monkeypatch.setattr(motion, "frames_for", lambda *a, **k: 4)
    cfg = ConfigStore(tmp_path)
    f = Frame(cfg, Storage(tmp_path, PANEL), MovingDisplay(PANEL, str(tmp_path / "f.png")), tmp_path)
    for i in range(8):
        size = (1200, 1800) if i % 3 == 0 else (1800, 1200)
        f.storage.add("Default", f"p{i}.jpg", photo(*size, i))
    return f


def wait_turn(f):
    if f._turn is not None:
        assert f._turn.wait(30)


def test_spotlight_zooms_in_and_out_of_the_collage(mframe):
    mframe.cfg.update({"slideshow_style": "spotlight", "collage_count": 3})
    for _ in range(5):
        mframe.rotate()
    assert mframe.display.kinds() == ["show", "zoom-in", "zoom-out", "zoom-in", "zoom-out"]
    _, src, dst = mframe.display.calls[1]
    assert dst[2] < PANEL[0] * 0.7 and src[2] > dst[2] * 0.5  # from a collage space


def test_story_turns_in_3d_from_photo_to_photo(mframe):
    mframe.cfg.update({"slideshow_style": "story", "collage_count": 4})
    mframe._turn_n = 0  # flip, fade, tip, fade…
    kinds = []
    for _ in range(5):
        mframe.rotate()
        wait_turn(mframe)
    kinds = mframe.display.kinds()
    assert kinds[:2] == ["show", "zoom-in"]
    assert kinds[2:5] == ["3d", "show", "3d"]  # 3D turns, mixed with fades


def test_classic_draws_the_next_photos_turn_ahead(mframe):
    mframe.cfg.update({"local_collage": False})  # (a pair of photos gets no turn)
    mframe._turn_n = 0
    for _ in range(4):
        mframe.rotate()
        wait_turn(mframe)
    assert mframe.display.kinds() == ["show", "3d", "show", "3d"]
    assert mframe.display.calls[1][1] == 4
    # something else shown in between: the turn no longer fits, so a fade
    mframe.show_upload(open(next(mframe.storage.root.rglob("p1.jpg")), "rb").read())
    wait_turn(mframe)
    mframe._turn_n = 0
    mframe.rotate()
    assert mframe.display.kinds()[-1] == "show"


def test_fades_only_or_instant_turns_motions_off(mframe):
    mframe.cfg.update({"slideshow_style": "spotlight", "collage_count": 3, "transition_effects": "fade"})
    for _ in range(3):
        mframe.rotate()
    assert set(mframe.display.kinds()) == {"show"}
    assert mframe._turn is None and mframe._queued is None
    mframe.cfg.update({"transition_effects": "moves"})
    mframe.rotate()
    assert mframe.display.kinds()[-1] in ("zoom-in", "zoom-out")
    mframe.cfg.update({"transition": "none"})
    mframe.rotate()
    assert mframe.display.calls[-1] == ("show", 0)


def test_dimmed_at_night_no_3d(mframe):
    mframe.monitor.software_level = 0.5
    for _ in range(3):
        mframe.rotate()
    assert set(mframe.display.kinds()) == {"show"}


def test_living_collage_swaps_one_photo_at_a_time(mframe):
    mframe.cfg.update({"slideshow_style": "living", "collage_count": 3, "living_minutes": 2})
    ok, msg = mframe.rotate()
    assert ok and "new layout" in msg
    before = list(mframe._living["photos"])
    t0 = time.time()
    ok, msg = mframe.rotate()
    assert ok and "swapped" in msg and 100 < mframe._step_due - t0 <= 121
    after = mframe._living["photos"]
    assert sum(a != b for a, b in zip(before, after)) == 1  # one space changed
    kind, rect = mframe.display.calls[-1]
    i = next(i for i, (a, b) in enumerate(zip(before, after)) if a != b)
    assert kind == "slide" and rect == motion.box_rect(mframe._living["boxes"][i])
    for _ in range(6):  # 3 spaces x 2 swaps, then a new layout
        ok, msg = mframe.rotate()
    assert "new layout" in msg


def test_demo_waits_for_its_3d_turn(mframe, monkeypatch):
    mframe.cfg.update({"demo_mode": True})
    seen = []
    for _ in range(12):
        ok, msg = mframe.rotate()
        seen.append(msg)
        wait_turn(mframe)
    joined = " ".join(seen)
    for how in ("3d", "zoom-out", "slide", "zoom-in"):
        assert f"({how})" in joined
    assert {"3d", "zoom-in", "zoom-out", "slide"} <= set(mframe.display.kinds())
    # not drawn yet when it's time: wait a little, rather than fade
    real_turn = motion.Turn

    class Slow(real_turn):
        def _run(self):
            time.sleep(0.5)
            super()._run()

    monkeypatch.setattr(motion, "Turn", Slow)
    while mframe.demo.peek()[3] != "3d":
        mframe.rotate()
    assert mframe._turn is not None and not mframe._turn.ready()
    ok, msg = mframe.rotate()
    assert "waiting" in msg and mframe._demo_due - time.time() < 2
    wait_turn(mframe)
    ok, msg = mframe.rotate()
    assert msg == "demo: photo (3d)" and mframe.display.kinds()[-1] == "3d"


def test_slides_only_count_refreshes_with_something_to_show(disp):  # noqa: F811
    d, pk = disp
    W, H = d.size
    d.show(Image.new("RGB", d.size, (255, 0, 0)))
    pk.log["commits"].clear()
    d.slide_in(Image.new("RGB", d.size, (0, 0, 255)), (W - 500, 300, 400, 300), 150)
    moves = [c[0][1] for c in pk.log["commits"][:-1]]
    assert moves and moves[0].get("FB_ID")  # no empty updates while the print is still off screen
    assert d.last_fade["frames"] == len(moves)


# ---------------- taking turns between styles ----------------
def test_ticking_several_styles(tmp_path):
    cfg = ConfigStore(tmp_path)
    assert cfg.get("slideshow_styles") == ["classic"]
    cfg.update({"slideshow_styles": ["living", "classic", "story"]})
    assert cfg.get("slideshow_styles") == ["classic", "story", "living"] and cfg.get("slideshow_style") == "classic"
    cfg.update({"slideshow_style": "spotlight"})  # the old single choice still works
    assert cfg.get("slideshow_styles") == ["spotlight"]
    for bad in ([], ["chaos"], "story"):
        with pytest.raises(Exception):
            cfg.update({"slideshow_styles": bad})
    # a frame set up before styles could be combined keeps its style
    import json
    raw = json.loads((tmp_path / "config.json").read_text())
    raw["config"].pop("slideshow_styles")
    raw["config"]["slideshow_style"] = "story"
    (tmp_path / "config.json").write_text(json.dumps(raw))
    assert ConfigStore(tmp_path).get("slideshow_styles") == ["story"]


def test_styles_take_turns_at_the_end_of_a_round(mframe):
    mframe.cfg.update({"slideshow_styles": ["classic", "story"], "collage_count": 3, "style_switch_minutes": 30,
                       "transition_effects": "fade"})
    mframe.rotate()
    assert mframe.active_style() == "classic"
    mframe._style_since -= 31 * 60  # half an hour later
    mframe.rotate()
    assert mframe.active_style() == "story" and mframe.state["current"]["filepath"].startswith("Collage")
    mframe._style_since -= 31 * 60
    mframe.rotate()  # mid-story: carries on with the story
    assert mframe.active_style() == "story" and not mframe.state["current"]["filepath"].startswith("Collage")
    for _ in range(2):
        mframe.rotate()
    assert mframe.active_style() == "story"
    mframe.rotate()  # the story's over
    assert mframe.active_style() == "classic"
    mframe.cfg.update({"slideshow_styles": ["living"]})
    mframe.reset_story()
    assert mframe.active_style() == "living"


def test_slower_turns_get_more_frames(mframe):
    """Every screen refresh gets a new frame, whatever the speed setting."""
    assert frames_for(1300, (1920, 1080), PANEL) == 78
    assert frames_for(2600, (1920, 1080), PANEL) == 156
    for speed, ms in ((1500, 1300), (4000, 2600), (800, 780)):
        mframe.cfg.update({"transition_ms": speed})
        assert mframe._motion_ms(1300) == ms


def test_frames_stay_within_memory(monkeypatch, tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 1000000 kB\nMemAvailable:  300000 kB\n")
    real_open = open
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: real_open(meminfo if p == "/proc/meminfo" else p, *a, **k))
    n = motion.frames_for(2600, (1920, 1080), PANEL)  # a third of 300 MB: about 27 frames of 1280x720
    assert 20 <= n <= 30
