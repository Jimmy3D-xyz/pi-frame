import json
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

from photoframe import brand, screens
from photoframe.config import ConfigStore
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.storage import Storage
from photoframe.web import create_app

STATIC = Path(__file__).resolve().parents[1] / "photoframe" / "static"


def test_logo_svg_is_valid_and_matches_the_files():
    for variant in ("full", "compact"):
        ET.fromstring(brand.svg(variant=variant))  # well-formed
    assert (STATIC / "logo.svg").read_text() == brand.svg()
    assert (STATIC / "favicon.svg").read_text() == brand.svg(variant="compact")


def test_logo_raster_sizes_and_transparency():
    for size in (16, 32, 48, 64, 180, 512):
        m = brand.mark(size)
        assert m.size == (size, size) and m.mode == "RGBA"
        assert m.getpixel((0, 0))[3] < 20  # rounded corner is (nearly) transparent after smoothing
        assert m.getpixel((size // 2, size // 2))[3] == 255
    assert json.loads((STATIC / "manifest.webmanifest").read_text())["name"] == "Pi Frame"
    for f in ("apple-touch-icon.png", "icon-192.png", "icon-512.png", "favicon.ico"):
        assert Image.open(STATIC / f).size[0] >= 16


def test_screens_carry_the_brand():
    img = screens.welcome_card((1920, 1080), "Pi Frame", ["http://piframe.local/"], "")
    # the large logo sits in the right half: its cream mat is much brighter than the background
    right = img.crop((1150, 380, 1500, 700)).convert("L")
    assert max(right.getdata()) > 200
    for size in ((1920, 1080), (1080, 1920), (800, 480)):
        assert screens.wifi_setup_card(size, "PiFrame-AB12", "12345678", "http://10.42.0.1/setup", "x").size == size


def test_rename_and_old_default_name(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"config": {"device_name": "PhotoFrame"}}))
    assert ConfigStore(tmp_path).get("device_name") == "Pi Frame"  # old default migrated
    (tmp_path / "config.json").write_text(json.dumps({"config": {"device_name": "Lounge room"}}))
    assert ConfigStore(tmp_path).get("device_name") == "Lounge room"  # a chosen name is kept
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    cfg = ConfigStore(tmp_path)
    frame = Frame(cfg, Storage(tmp_path, (800, 480)), FileDisplay((800, 480), str(tmp_path / "f.png")), tmp_path)
    c = create_app(frame).test_client()
    html = c.get("/").get_data(as_text=True)
    assert "<title>Pi Frame</title>" in html and "Photo frame" not in html
    assert c.get("/static/logo.svg").mimetype == "image/svg+xml"
    assert c.get("/favicon.ico").status_code == 200
    assert c.get("/static/../config.py").status_code == 404 and c.get("/static/index.html").status_code == 404
    cfg.update({"http_password": "pw"})
    r = c.get("/")
    assert r.status_code == 401 and 'realm="Pi Frame"' in r.headers["WWW-Authenticate"]
