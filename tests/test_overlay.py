import io
import json
import time
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from photoframe import overlay as ov
from photoframe.config import ConfigError, ConfigStore
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.storage import Storage
from photoframe.web import create_app

PANEL = (1920, 1080)

# Real Open-Meteo response shapes (trimmed)
FORECAST = {"latitude": -37.8, "longitude": 144.96, "timezone": "Australia/Melbourne",
            "current": {"time": "2026-09-30T16:30", "interval": 900, "temperature_2m": 20.6, "weather_code": 61, "is_day": 1},
            "daily": {"time": ["2026-09-30"], "temperature_2m_max": [23.8], "temperature_2m_min": [11.6], "weather_code": [61]}}
GEOCODE = {"results": [
    {"id": 2158177, "name": "Melbourne", "latitude": -37.814, "longitude": 144.96332, "country": "Australia", "admin1": "Victoria"},
    {"id": 4163971, "name": "Melbourne", "latitude": 28.08363, "longitude": -80.60811, "country": "United States", "admin1": "Florida"}]}
NOMINATIM = {"address": {"town": "Yarra Glen", "state": "Victoria", "country": "Australia", "country_code": "au"}}


def exif_photo(path: Path, taken="2024:09:30 15:04:05", lat=(37.0, 39.0, 36.0), lon=(145.0, 22.0, 30.0)):
    img = Image.new("RGB", (3000, 2000), (160, 200, 230))
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = taken
    if lat:
        exif.get_ifd(0x8825).update({1: "S", 2: lat, 3: "E", 4: lon})
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes())
    return buf.getvalue()


@pytest.fixture
def frame(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    monkeypatch.setattr(ov, "_get_json", lambda url, timeout=20: FORECAST if "forecast" in url
                        else GEOCODE if "geocoding" in url else NOMINATIM)
    cfg = ConfigStore(tmp_path)
    cfg.update({"transition": "none"})
    f = Frame(cfg, Storage(tmp_path, PANEL), FileDisplay(PANEL, str(tmp_path / "f.png")), tmp_path)
    return f


def test_years_ago_today():
    assert ov.years_ago_today(datetime(2024, 9, 30), datetime(2026, 9, 30, 8)) == 2
    assert ov.years_ago_today(datetime(2025, 9, 30), datetime(2026, 9, 30)) == 1
    assert ov.years_ago_today(datetime(2026, 9, 30), datetime(2026, 9, 30)) is None  # taken today
    assert ov.years_ago_today(datetime(2024, 9, 29), datetime(2026, 9, 30)) is None


def test_weather_and_place_search(frame):
    places = ov.search_places("Melbourne")
    assert places[0]["label"] == "Melbourne, Victoria, Australia" and places[0]["lat"] == -37.814
    assert places[1]["label"].endswith("Florida, United States")
    wx = ov.fetch_weather(-37.8, 144.96)
    assert (wx["temp"], wx["high"], wx["low"], wx["words"], wx["icon"]) == (21, 24, 12, "Light rain", "rain")


def test_photo_details_and_place_names(frame, tmp_path):
    p = frame.storage.add("Default", "IMG_1.jpg", exif_photo(tmp_path))
    d = ov.photo_details(p)
    assert d["taken"] == datetime(2024, 9, 30, 15, 4, 5)
    assert d["lat"] == pytest.approx(-37.66) and d["lon"] == pytest.approx(145.375)  # south and east signs
    found = []
    places = ov.Places(tmp_path, on_found=lambda: found.append(1))
    assert places.name(d["lat"], d["lon"]) is None  # looked up in the background
    for _ in range(50):
        if found:
            break
        time.sleep(0.05)
    assert places.name(d["lat"], d["lon"]) == "Yarra Glen, Victoria"
    assert json.loads((tmp_path / "places.json").read_text())  # remembered for next time
    none = frame.storage.add("Default", "nodata.jpg", Image.new("RGB", (800, 600)))
    assert ov.photo_details(none) == {}


def test_overlay_draws_in_the_chosen_corner(frame):
    o = frame.overlay
    base = Image.new("RGB", PANEL, (230, 230, 230))
    now = datetime(2026, 9, 30, 16, 42)
    out = o.apply(base, {}, now)
    corner = out.crop((40, 800, 400, 1040)).convert("L")
    assert min(corner.getdata()) < 120  # the dark glass panel is there, bottom left
    assert min(out.crop((1500, 40, 1880, 300)).convert("L").getdata()) > 200  # top right untouched
    frame.cfg.update({"overlay_corner": "top-right"})
    out = o.apply(base, {}, now)
    assert min(out.crop((1500, 40, 1880, 300)).convert("L").getdata()) < 120
    frame.cfg.update({"overlay_clock": False, "overlay_date": False, "overlay_weather": False, "overlay_photo_info": False})
    assert list(o.apply(base, {}, now).getdata()) == list(base.getdata())  # everything off: untouched


def test_caption_and_memories(frame):
    o = frame.overlay
    o.places.cache["-37.660,145.375"] = "Yarra Glen, Victoria"
    details = {"taken": datetime(2024, 9, 30, 15, 4), "lat": -37.66, "lon": 145.375}
    assert o.caption(details, datetime(2026, 9, 30)) == ["30 September 2024 · Yarra Glen, Victoria", "2 years ago today"]
    assert o.caption(details, datetime(2026, 10, 1)) == ["30 September 2024 · Yarra Glen, Victoria"]
    frame.cfg.update({"overlay_memories": False})
    assert len(o.caption(details, datetime(2026, 9, 30))) == 1
    frame.cfg.update({"overlay_photo_info": False})
    assert o.caption(details, datetime(2026, 9, 30)) == []


def test_weather_follows_settings(frame):
    w = frame.overlay.weather
    assert w.refresh() is None  # off by default
    frame.cfg.update({"overlay_weather": True, "weather_location": {"name": "Melbourne", "lat": -37.81, "lon": 144.96}})
    assert w.refresh()["temp"] == 21 and w.current()["temp"] == 21
    frame.cfg.update({"weather_units": "f"})
    assert w.current() is None  # stale units: hidden until the next reading
    with pytest.raises(ConfigError):
        frame.cfg.update({"weather_location": {"name": "x", "lat": 200, "lon": 0}})
    with pytest.raises(ConfigError):
        frame.cfg.update({"overlay_corner": "middle"})


def test_frame_redraws_overlay_but_not_over_cards_or_videos(frame, tmp_path):
    frame.storage.add("Default", "IMG_1.jpg", exif_photo(tmp_path))
    ok, _ = frame.rotate()
    assert ok and frame._base_details.get("taken") == datetime(2024, 9, 30, 15, 4, 5)
    v = frame.thumb_version
    assert frame.refresh_overlay() is True and frame.thumb_version >= v + 1  # (the place name arriving redraws too)
    frame.notice = "wifi"
    assert frame.refresh_overlay() is False  # the Wi-Fi setup card stays untouched
    frame.notice = None
    frame.player.playing = Path("x.mp4")
    assert frame.refresh_overlay() is False  # the video owns the screen
    frame.player.playing = None
    frame.display.screen_on = False
    assert frame.refresh_overlay() is False


def test_web_settings_and_search(frame):
    c = create_app(frame).test_client()
    assert c.get("/api/weather/search?q=Melb").get_json()[0]["name"] == "Melbourne"
    r = c.post("/api/config", json={"overlay_weather": True, "weather_location": {"name": "Melbourne", "label": "Melbourne, Victoria",
                                                                                  "lat": -37.81, "lon": 144.96}})
    assert r.status_code == 200
    for _ in range(40):
        now = c.get("/api/weather").get_json()["now"]
        if now:
            break
        time.sleep(0.05)
    assert now["temp"] == 21
    html = c.get("/").get_data(as_text=True)
    assert 'data-ovl="overlay_clock"' in html and 'id="wx-search"' in html
