import io

import pytest
from PIL import Image

from photoframe.config import ConfigStore
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.storage import Storage
from photoframe.web import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    cfg = ConfigStore(tmp_path)
    cfg.update({"transition": "none"})
    f = Frame(cfg, Storage(tmp_path, (2560, 1440)), FileDisplay((2560, 1440), str(tmp_path / "f.png")), tmp_path)
    f.storage.add("Default", "a.jpg", Image.new("RGB", (3000, 2000), (200, 60, 60)))
    f.storage.add("Default", "b.jpg", Image.new("RGB", (3000, 2000), (60, 60, 200)))
    return f, create_app(f).test_client()


def jpeg_size(data):
    return Image.open(io.BytesIO(data)).size


def test_larger_views_of_whats_on_screen(client):
    frame, c = client
    assert c.get("/api/current_image?size=large").status_code == 404  # nothing yet
    frame.rotate()
    small = c.get("/api/current_image")
    assert max(jpeg_size(small.data)) <= 400  # the existing preview is unchanged
    large = c.get("/api/current_image?size=large")
    assert large.mimetype == "image/jpeg" and jpeg_size(large.data) == (1920, 1080)  # capped at 1920
    full = c.get("/api/current_image?size=full")
    assert jpeg_size(full.data) == (2560, 1440)  # the screen's own resolution
    # includes the on-screen info (the clock panel darkens the bottom-left corner)
    img = Image.open(io.BytesIO(full.data)).convert("L")
    assert min(img.crop((90, 1200, 500, 1380)).getdata()) < 100


def test_encoded_once_per_change(client, monkeypatch):
    frame, c = client
    frame.rotate()
    a = c.get("/api/current_image?size=large").data
    assert c.get("/api/current_image?size=large").data is not None
    assert frame._full_cache[1] == a  # served from the cache
    v = frame.thumb_version
    frame.rotate()
    assert frame.thumb_version > v and c.get("/api/current_image?size=large").data != a  # new picture, new image


def test_live_page_and_popup(client):
    frame, c = client
    live = c.get("/live")
    assert live.status_code == 200 and b"Pi Frame" in live.data and b"/api/current_image?size=large" in live.data
    html = c.get("/").get_data(as_text=True)
    assert 'id="lightbox"' in html and 'href="/live"' in html


def test_maintenance_cards_fold(client):
    frame, c = client
    html = c.get("/").get_data(as_text=True)
    a, b = html.index('id="tab-maint"'), html.index('id="tab-source"')
    tab = html[a:b]
    for slug in ("net", "power", "storage", "screen", "library", "system", "activity", "resolution", "update", "password", "actions"):
        assert f'data-fold="{slug}"' in tab and f'id="sum-{slug}"' in tab
    assert tab.count("<section") == tab.count("</section>") and tab.count("<details") == tab.count("</details>")
    assert 'id="m-summary"' in tab  # the health summary stays outside, always visible
