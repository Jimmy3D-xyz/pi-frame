import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image

from photoframe import render
from photoframe.config import ConfigError, ConfigStore
from photoframe.cron import CronError, CronRule, next_fire, parse_rules
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.storage import Storage
from photoframe.web import create_app

PANEL = (1920, 1080)


def jpeg(w, h, color=(200, 50, 50)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()


# ---------------- cron ----------------
def test_cron_parse_and_match():
    r = CronRule.parse("*/15 7-22 1-5")
    assert r.minutes == {0, 15, 30, 45}
    assert 7 in r.hours and 23 not in r.hours
    assert r.dows == {1, 2, 3, 4, 5}
    assert CronRule.parse("0 0 5-7").dows == {5, 6, 0}
    with pytest.raises(CronError):
        CronRule.parse("0 0")
    with pytest.raises(CronError):
        CronRule.parse("61 * *")
    with pytest.raises(CronError):
        parse_rules([])


def test_next_fire_is_on_matching_minute(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    rules = parse_rules(["30 9 *"])
    t0 = 1_700_000_000  # 2023-11-14 22:13:20 UTC
    nf = next_fire(rules, t0)
    tm = time.gmtime(nf)
    assert (tm.tm_hour, tm.tm_min, tm.tm_sec) == (9, 30, 0) and nf > t0


# ---------------- render ----------------
def test_render_orientation_and_collage():
    land = Image.new("RGB", (3000, 2000), (255, 0, 0))
    logical = render.logical_size(PANEL, "portrait")
    assert logical == (1080, 1920)
    assert render.is_mismatched(land, logical)
    frame = render.compose([land, land], logical, "fit", "black")
    assert frame.size == logical
    panel = render.to_panel(frame, PANEL, 0)
    assert panel.size == PANEL
    # stock-server native layout (landscape) is turned back upright for portrait
    native = Image.new("RGB", (1920, 1080))
    assert render.undo_native_layout(native, logical).size == (1080, 1920)
    assert render.store_copy(Image.new("RGB", (6000, 4000)), PANEL).size == (2880, 1920)


# ---------------- app fixture ----------------
@pytest.fixture
def app_env(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    cfg = ConfigStore(tmp_path)
    disp = FileDisplay(PANEL, str(tmp_path / "frame.png"))
    storage = Storage(tmp_path, disp.size)
    frame = Frame(cfg, storage, disp, tmp_path)
    app = create_app(frame)
    return app.test_client(), frame, tmp_path


def test_config_validation(app_env):
    client, frame, _ = app_env
    assert client.post("/api/config", json={"rotate_cron": ["bad"]}).status_code == 400
    assert client.post("/api/config", json={"display_rotation_deg": 45}).status_code == 400
    r = client.patch("/api/config", json={"rotate_cron": ["0 */2 *"], "device_name": "Wedding"})
    assert r.status_code == 200
    body = client.get("/api/config").get_json()
    assert body["rotate_cron"] == ["0 */2 *"] and body["device_name"] == "Wedding"
    assert "http_password" not in body
    assert client.post("/api/config", json={"rotate_interval": 3600}).status_code == 200
    assert client.get("/api/config").get_json()["rotate_cron"] == ["0 */1 *"]


def test_albums_upload_rotate_previous(app_env):
    client, frame, tmp = app_env
    assert client.post("/api/albums", json={"name": "Wedding"}).status_code == 200
    for i, size in enumerate([(4000, 3000), (3000, 4000), (4000, 2600)]):
        r = client.post(
            "/api/upload",
            data={"album": "Wedding", "image": (io.BytesIO(jpeg(*size)), f"IMG_{i}.jpg")},
            content_type="multipart/form-data",
        )
        assert r.status_code == 200, r.get_json()
    imgs = client.get("/api/images?album=Wedding").get_json()
    assert len(imgs) == 3
    assert client.get(f"/api/image?filepath=Wedding/{imgs[0]['filename']}").mimetype == "image/jpeg"
    assert client.get("/api/image?filepath=../../etc/passwd").status_code == 404

    client.post("/api/config", json={"display_orientation": "portrait", "transition": "none"})
    time.sleep(0.5)  # config change triggers a background rotate
    assert client.post("/api/rotate").status_code == 200
    assert Image.open(tmp / "frame.png").size == PANEL
    assert client.get("/api/current_image").mimetype == "image/jpeg"
    assert client.post("/api/rotate").status_code == 200
    assert client.post("/api/previous").status_code == 200

    fp = f"Wedding/{imgs[1]['filename']}"
    assert client.post("/api/display", json={"filepath": fp}).status_code == 200
    assert frame.state["current"]["filepath"] == fp
    assert client.post("/api/delete", json={"filepath": fp}).status_code == 200
    assert len(client.get("/api/images?album=Wedding").get_json()) == 2

    r = client.post("/api/display-image", data=jpeg(800, 600), content_type="image/jpeg")
    assert r.status_code == 200 and frame.state["current"]["source"] == "upload"


def test_http_password(app_env):
    client, frame, _ = app_env
    client.post("/api/config", json={"http_password": "s3cret"})
    assert client.get("/api/system-info").status_code == 401
    import base64

    ok = {"Authorization": "Basic " + base64.b64encode(b"photoframe:s3cret").decode()}
    info = client.get("/api/system-info", headers=ok).get_json()
    assert info["display_type"] == "rgb" and info["width"] == 1920


def test_screen_schedule(app_env, monkeypatch):
    _, frame, _ = app_env
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    frame.cfg.update({"screen_schedule_enabled": True, "screen_on_time": "07:00", "screen_off_time": "22:30"})
    day = time.mktime((2026, 9, 27, 12, 0, 0, 0, 0, 0))
    night = time.mktime((2026, 9, 27, 23, 0, 0, 0, 0, 0))
    assert frame.screen_should_be_on(day) and not frame.screen_should_be_on(night)
    frame.cfg.update({"screen_on_time": "22:00", "screen_off_time": "06:00"})  # crosses midnight
    assert frame.screen_should_be_on(night) and not frame.screen_should_be_on(day)


# ---------------- URL protocol against a fake image server ----------------
class FakeServer(BaseHTTPRequestHandler):
    seen: list = []
    logical_layout = False

    def log_message(self, *a):
        pass

    def do_GET(self):
        FakeServer.seen.append(dict(self.headers))
        if self.headers.get("If-None-Match") == '"v1"':
            self.send_response(304)
            self.end_headers()
            return
        body = jpeg(1080, 1920) if FakeServer.logical_layout else jpeg(1920, 1080, (0, 0, 255))
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("ETag", '"v1"')
        if FakeServer.logical_layout:
            self.send_header("X-Image-Layout", "logical")
        self.send_header("X-Config-Payload", json.dumps({"config": {"rotate_cron": ["0 8 *"]}}))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_url_rotation_protocol(app_env):
    client, frame, tmp = app_env
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeServer)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/image"
        frame.cfg.update(
            {"rotation_mode": "url", "image_url": url, "access_token": "tok", "display_orientation": "portrait"}
        )
        ok, msg = frame.rotate()
        assert ok, msg
        h = FakeServer.seen[-1]
        assert h["Authorization"] == "Bearer tok"
        assert h["X-Display-Width"] == "1920" and h["X-Display-Orientation"] == "portrait"
        assert h["X-Display-Type"] == "rgb" and h["X-Firmware-Version"].startswith("pi-")
        assert json.loads(h["X-Processing-Settings"])["scaleMode"] == "fit"
        assert frame.cfg.get("rotate_cron") == ["0 8 *"]  # payload applied
        assert frame.state["etag"] == '"v1"'
        ok, msg = frame.rotate()
        assert ok and msg == "image not modified"
        assert FakeServer.seen[-1]["If-None-Match"] == '"v1"'

        frame.cfg.update({"image_url": "http://127.0.0.1:1/nothing"})
        import photoframe.fetcher as f

        orig = f.time.sleep
        f.time.sleep = lambda s: None
        try:
            r = client.post("/api/rotate")
        finally:
            f.time.sleep = orig
        assert r.status_code == 502
        assert client.get("/api/config").get_json()["last_fetch_error"]
    finally:
        srv.shutdown()


def test_remote_payload_bumps_timestamp(tmp_path):
    cfg = ConfigStore(tmp_path)
    before = cfg.config_last_updated
    assert cfg.apply_remote_payload({"processing_settings": {"scaleMode": "cover"}})
    assert cfg.processing["scaleMode"] == "cover" and cfg.config_last_updated >= before
    with pytest.raises(ConfigError):
        cfg.update({"rotation_mode": "sdcard"})


def test_scheduler_fires_even_when_waking_late(app_env, monkeypatch):
    """Regression: the scheduler used to recompute the due time from 'now'
    after waking, which skipped the boundary it had been waiting for."""
    client, frame, tmp = app_env
    fake = {"t": 1_800_000_000.0 - 10}  # 10 s before a minute boundary
    calls = []
    import photoframe.frame as fr

    monkeypatch.setattr(fr.time, "time", lambda: fake["t"])
    frame.rotate = lambda: calls.append(fake["t"]) or (True, "ok")
    frame.cfg.update({"rotate_cron": ["* * *"]})

    def wait(timeout):  # simulate Event.wait oversleeping by 50 ms
        fake["t"] += timeout + 0.05
        if len(calls) >= 3:
            frame._stop.set()
        return False

    monkeypatch.setattr(frame._wake, "wait", wait)
    frame.run_scheduler()
    assert len(calls) == 3
    assert [round(c - calls[0]) for c in calls] == [0, 60, 120]


@pytest.mark.parametrize("bpp", [32, 16])
def test_framebuffer_fade_ends_on_new_image(tmp_path, bpp):
    import numpy as np
    from photoframe.display import FramebufferDisplay

    w, h = 320, 200
    stride = w * bpp // 8 + 64  # padded rows, like real hardware
    fb = tmp_path / "fb"
    fb.write_bytes(b"\0" * stride * h)
    d = FramebufferDisplay(str(fb), (w, h, bpp, stride, 16))
    a = Image.new("RGB", (w, h), (255, 0, 0))
    b = Image.new("RGB", (w, h), (0, 64, 255))
    d.show(a)
    d.show(b, 200)
    assert np.array_equal(np.asarray(d._view), d._native(b))
    raw = np.frombuffer(fb.read_bytes(), np.uint8).reshape(h, stride)
    if bpp == 32:
        assert tuple(raw[0, :3]) == (255, 64, 0)  # BGRX
    else:
        px = int(raw[0, 0]) | int(raw[0, 1]) << 8
        assert (px >> 11, (px >> 5) & 63, px & 31) == (0, 16, 31)


def test_fast_fade_matches_the_blend_formula(tmp_path, monkeypatch):
    """The banded, multi-core fade writes exactly old*(1-a) + new*a into the screen."""
    import numpy as np
    from photoframe import display as dmod
    w, h = 64, 40
    fb = tmp_path / "fb0"
    fb.write_bytes(b"\0" * w * h * 4)
    rng = np.random.default_rng(1)
    a = Image.fromarray(rng.integers(0, 256, (h, w, 3), dtype=np.uint8))
    b = Image.fromarray(rng.integers(0, 256, (h, w, 3), dtype=np.uint8))
    results = []
    for workers in (1, 4):
        disp = dmod.FramebufferDisplay(str(fb), (w, h, 32, w * 4, 16))
        disp._workers = workers
        if workers == 1:
            disp._pool = None
        disp._vsync = False
        disp.show(a)
        clock = iter([0.5, 0.5, 9.0])  # one frame exactly halfway, then done
        monkeypatch.setattr(dmod.time, "monotonic", lambda: next(clock, 9.0))
        assert disp._fade_32(a, b, 1000, 0.0) == 1
        monkeypatch.undo()
        got = np.frombuffer(fb.read_bytes(), np.uint8).reshape(h, w, 4)[..., :3][..., ::-1]  # BGRX -> RGB
        alpha = int(dmod._ease(0.5) * 128)
        want = ((np.asarray(a, np.int32) * (128 - alpha) + np.asarray(b, np.int32) * alpha) >> 7).astype(np.uint8)
        assert np.array_equal(got, want)
        results.append(got.copy())
    assert np.array_equal(results[0], results[1])  # four bands give exactly the same picture as one
