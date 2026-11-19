"""On-screen information drawn over the photos: the time and date, the
weather (from Open-Meteo, free, no account), and details of the photo on show
(when and where it was taken, and "N years ago today" on its anniversary).

The panels are "frosted glass": the photo behind is blurred and darkened so
text stays readable over bright or busy photos. They're drawn at the logical
(upright) size, before the frame is rotated onto the panel.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from . import __version__
from .screens import _font

log = logging.getLogger(__name__)

WEATHER_EVERY = 15 * 60
WEATHER_STALE = 3 * 3600  # hide weather older than this
USER_AGENT = f"PiFrame/{__version__} (personal digital photo frame; https://github.com/aitjcize/esp32-photoframe)"

INK = (250, 248, 242)
SOFT = (222, 218, 208)
SUN = (240, 186, 82)

# WMO weather codes (used by Open-Meteo) -> words and an icon name
WMO = {
    0: ("Clear", "clear"), 1: ("Mostly clear", "clear"), 2: ("Partly cloudy", "partly"), 3: ("Cloudy", "cloud"),
    45: ("Fog", "fog"), 48: ("Fog", "fog"),
    51: ("Light drizzle", "drizzle"), 53: ("Drizzle", "drizzle"), 55: ("Heavy drizzle", "drizzle"),
    56: ("Freezing drizzle", "drizzle"), 57: ("Freezing drizzle", "drizzle"),
    61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"), 67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"), 77: ("Snow grains", "snow"),
    80: ("Showers", "rain"), 81: ("Showers", "rain"), 82: ("Heavy showers", "rain"),
    85: ("Snow showers", "snow"), 86: ("Snow showers", "snow"),
    95: ("Thunderstorm", "storm"), 96: ("Thunderstorm, hail", "storm"), 99: ("Thunderstorm, hail", "storm"),
}


def _get_json(url: str, timeout: float = 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---------------------------------------------------------------- weather
def search_places(query: str) -> list[dict]:
    """Town/suburb search for the weather location (Open-Meteo geocoding)."""
    q = urllib.parse.urlencode({"name": query.strip(), "count": 8, "language": "en", "format": "json"})
    data = _get_json(f"https://geocoding-api.open-meteo.com/v1/search?{q}")
    out = []
    for r in data.get("results", []) or []:
        label = ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country")) if x)
        out.append({"name": r.get("name"), "label": label, "lat": r["latitude"], "lon": r["longitude"]})
    return out


def fetch_weather(lat: float, lon: float, units: str = "c") -> dict:
    q = urllib.parse.urlencode({
        "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}",
        "current": "temperature_2m,weather_code,is_day",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code",
        "timezone": "auto", "forecast_days": 1,
        "temperature_unit": "fahrenheit" if units == "f" else "celsius",
    })
    d = _get_json(f"https://api.open-meteo.com/v1/forecast?{q}")
    cur, day = d["current"], d["daily"]
    code = int(cur.get("weather_code", 0))
    words, icon = WMO.get(code, ("", "cloud"))
    return {
        "temp": round(cur["temperature_2m"]), "code": code, "words": words, "icon": icon,
        "day": bool(cur.get("is_day", 1)),
        "high": round(day["temperature_2m_max"][0]), "low": round(day["temperature_2m_min"][0]),
        "units": units, "fetched": time.time(),
    }


class Weather:
    """Keeps the current weather fresh in the background."""

    def __init__(self, cfg, data_dir: Path):
        self.cfg = cfg
        self.cache = data_dir / "weather.json"
        self.now: dict | None = None
        self.error = ""
        try:
            self.now = json.loads(self.cache.read_text())
        except (OSError, json.JSONDecodeError):
            pass
        self._wake = threading.Event()

    def refresh(self) -> dict | None:
        c = self.cfg.snapshot()
        loc = c.get("weather_location") or {}
        if not c.get("overlay_weather") or not loc.get("lat"):
            return None
        try:
            self.now = fetch_weather(loc["lat"], loc["lon"], c.get("weather_units", "c"))
            self.now["place"] = loc.get("name")
            self.error = ""
            self.cache.write_text(json.dumps(self.now))
        except Exception as e:  # noqa: BLE001
            self.error = f"couldn't get the weather ({e})"
            log.warning("weather: %s", self.error)
        return self.now

    def current(self) -> dict | None:
        if not self.now or time.time() - self.now.get("fetched", 0) > WEATHER_STALE:
            return None
        loc = self.cfg.get("weather_location") or {}
        if self.now.get("units") != self.cfg.get("weather_units") or self.now.get("place") != loc.get("name"):
            return None  # settings changed; a fresh reading is on its way
        return self.now

    def poke(self) -> None:
        self._wake.set()

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.refresh()
            self._wake.wait(WEATHER_EVERY)
            self._wake.clear()


# ---------------------------------------------------------------- photo details
def _dms(v) -> float:
    d, m, s = (float(x) for x in v)
    return d + m / 60 + s / 3600


def photo_details(path: Path) -> dict:
    """When and where a stored photo was taken, from its EXIF details."""
    out: dict = {}
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            taken = exif.get_ifd(0x8769).get(0x9003) or exif.get(0x0132)
            if taken:
                out["taken"] = datetime.strptime(str(taken).strip()[:19], "%Y:%m:%d %H:%M:%S")
            gps = exif.get_ifd(0x8825)
            if gps.get(2) and gps.get(4):
                lat, lon = _dms(gps[2]), _dms(gps[4])
                if str(gps.get(1, "N")).upper().startswith("S"):
                    lat = -lat
                if str(gps.get(3, "E")).upper().startswith("W"):
                    lon = -lon
                if abs(lat) > 0.01 or abs(lon) > 0.01:  # 0,0 means "no fix" on some cameras
                    out["lat"], out["lon"] = lat, lon
    except Exception:  # noqa: BLE001
        pass
    return out


class Places:
    """Place names for photo GPS positions (OpenStreetMap Nominatim), looked up
    once in the background and remembered. At most one lookup a second, per
    OpenStreetMap's usage policy."""

    def __init__(self, data_dir: Path, on_found=None):
        self.path = data_dir / "places.json"
        self.on_found = on_found or (lambda: None)
        self._lock = threading.Lock()
        self._pending: set[str] = set()
        self._last = 0.0
        try:
            self.cache: dict = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            self.cache = {}

    @staticmethod
    def _key(lat: float, lon: float) -> str:
        return f"{lat:.3f},{lon:.3f}"  # ~100 m: photos from one spot share a lookup

    def name(self, lat: float, lon: float) -> str | None:
        key = self._key(lat, lon)
        if key in self.cache:
            return self.cache[key] or None
        with self._lock:
            if key not in self._pending:
                self._pending.add(key)
                threading.Thread(target=self._lookup, args=(key, lat, lon), daemon=True).start()
        return None

    def _lookup(self, key: str, lat: float, lon: float) -> None:
        with self._lock:
            wait = 1.1 - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
        name = ""
        try:
            q = urllib.parse.urlencode({"format": "jsonv2", "lat": f"{lat:.5f}", "lon": f"{lon:.5f}",
                                        "zoom": 12, "accept-language": "en"})
            a = _get_json(f"https://nominatim.openstreetmap.org/reverse?{q}").get("address", {})
            local = next((a[k] for k in ("village", "town", "suburb", "city", "hamlet", "municipality", "county")
                          if a.get(k)), "")
            region = a.get("state") if a.get("country_code") in ("au", "us", "ca") else a.get("country")
            name = ", ".join(x for x in (local, region) if x)
        except Exception as e:  # noqa: BLE001
            log.info("place lookup failed (%s); will try again later", e)
            with self._lock:
                self._pending.discard(key)
            return
        with self._lock:
            self.cache[key] = name
            self._pending.discard(key)
            try:
                self.path.write_text(json.dumps(self.cache))
            except OSError:
                pass
        if name:
            self.on_found()


def years_ago_today(taken: datetime, today: datetime | None = None) -> int | None:
    today = today or datetime.now()
    if (taken.month, taken.day) == (today.month, today.day) and taken.year < today.year:
        return today.year - taken.year
    return None


# ---------------------------------------------------------------- icons
def draw_icon(d: ImageDraw.ImageDraw, kind: str, x: int, y: int, s: int, day: bool = True) -> None:
    """Simple weather icons in a box of side s."""
    def cloud(cx, cy, w, col=(236, 238, 240)):
        r = w * 0.22
        d.ellipse([cx - w * 0.42, cy - r * 0.2, cx - w * 0.42 + r * 2, cy + r * 1.8], fill=col)
        d.ellipse([cx - w * 0.2, cy - r * 1.2, cx - w * 0.2 + r * 2.6, cy + r * 1.4], fill=col)
        d.ellipse([cx + w * 0.08, cy - r * 0.5, cx + w * 0.08 + r * 2, cy + r * 1.5], fill=col)
        d.rounded_rectangle([cx - w * 0.42 + r * 0.5, cy + r * 0.4, cx + w * 0.08 + r * 1.5, cy + r * 1.8], radius=r * 0.7, fill=col)

    def sun(cx, cy, r):
        if day:
            for i in range(8):
                a = i * math.pi / 4
                d.line([cx + math.cos(a) * r * 1.35, cy + math.sin(a) * r * 1.35,
                        cx + math.cos(a) * r * 1.8, cy + math.sin(a) * r * 1.8], fill=SUN, width=max(2, int(r * 0.22)))
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=SUN)
        else:  # crescent moon: the part of one circle not covered by an equal, offset circle
            ox, oy = cx + r * 0.62, cy - r * 0.38
            dist = math.hypot(ox - cx, oy - cy)
            phi = math.atan2(oy - cy, ox - cx)          # direction of the covering circle
            alpha = math.acos(dist / (2 * r))           # half-angle to where the circles cross
            outer = [(cx + r * math.cos(t), cy + r * math.sin(t))
                     for t in (phi + alpha + i * (2 * math.pi - 2 * alpha) / 40 for i in range(41))]
            inner = [(ox + r * math.cos(t), oy + r * math.sin(t))
                     for t in (phi + math.pi + alpha - i * (2 * alpha) / 20 for i in range(21))]
            d.polygon(outer + inner, fill=(236, 230, 205))

    cx, cy = x + s / 2, y + s / 2
    if kind == "clear":
        sun(cx, cy, s * 0.24)
    elif kind == "partly":
        sun(x + s * 0.64, y + s * 0.36, s * 0.17)
        cloud(cx - s * 0.02, cy + s * 0.02, s * 0.9)
    elif kind == "fog":
        for i in range(3):
            yy = y + s * (0.35 + i * 0.16)
            d.rounded_rectangle([x + s * (0.12 + 0.06 * (i % 2)), yy, x + s * (0.88 - 0.06 * (i % 2)), yy + s * 0.07],
                                radius=s * 0.035, fill=(214, 218, 220))
    else:
        cloud(cx, cy - s * 0.1, s * 0.9, (214, 219, 224) if kind in ("rain", "storm", "drizzle") else (236, 238, 240))
        if kind in ("rain", "drizzle"):
            n = 3 if kind == "rain" else 2
            for i in range(n):
                xx = x + s * (0.3 + i * 0.2)
                d.line([xx, y + s * 0.7, xx - s * 0.06, y + s * 0.86], fill=(120, 180, 230), width=max(2, int(s * 0.06)))
        elif kind == "snow":
            for i in range(3):
                xx, yy = x + s * (0.3 + i * 0.2), y + s * 0.78
                d.ellipse([xx - s * 0.04, yy - s * 0.04, xx + s * 0.04, yy + s * 0.04], fill=(250, 250, 255))
        elif kind == "storm":
            d.polygon([(x + s * 0.52, y + s * 0.58), (x + s * 0.4, y + s * 0.8), (x + s * 0.5, y + s * 0.8),
                       (x + s * 0.44, y + s * 0.98), (x + s * 0.64, y + s * 0.72), (x + s * 0.54, y + s * 0.72)], fill=SUN)


# ---------------------------------------------------------------- drawing
SIZES = {"small": 0.052, "medium": 0.068, "large": 0.09}


def _glass(img: Image.Image, box: tuple[int, int, int, int], radius: int) -> None:
    """Frosted-glass panel: blur and darken the photo inside the box."""
    x0, y0, x1, y1 = [int(v) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(img.width, x1), min(img.height, y1)
    if x1 <= x0 or y1 <= y0:
        return
    region = img.crop((x0, y0, x1, y1))
    blurred = region.filter(ImageFilter.GaussianBlur(max(6, radius)))
    # darken, and squash highlights harder, so a bright sun or lamp behind the
    # glass can't glow through the text
    dark = blurred.point(lambda v: int(v * 0.5 if v < 120 else 60 + (v - 120) * 0.16))
    dark = Image.blend(dark, Image.new("RGB", region.size, (14, 16, 16)), 0.25)
    mask = Image.new("L", region.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, region.width - 1, region.height - 1], radius=radius, fill=255)
    img.paste(dark, (x0, y0), mask)
    # a faint light edge, like glass catching the light
    ImageDraw.Draw(img, "RGBA").rounded_rectangle([x0, y0, x1 - 1, y1 - 1], radius=radius, outline=(255, 255, 255, 40), width=1)


def _fmt_time(now: datetime, h24: bool) -> tuple[str, str]:
    if h24:
        return now.strftime("%H:%M"), ""
    return f"{now.hour % 12 or 12}:{now.minute:02d}", ("am" if now.hour < 12 else "pm")


def _nice_date(dt: datetime) -> str:
    return f"{dt.day} {dt.strftime('%B %Y')}"


FUN_FILE = Path(__file__).with_name("data") / "fun.json"


class Fun:
    """Jokes and facts, dealt like cards so nothing repeats until all have shown."""

    def __init__(self, cfg):
        self.cfg = cfg
        try:
            self.builtin = json.loads(FUN_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            self.builtin = {"jokes": [], "facts": []}
        self._deck: list[tuple[str, str]] = []
        self._key = None
        self.line: tuple[str, str] | None = None  # (kind, text)

    def _pool(self) -> list[tuple[str, str]]:
        c = self.cfg.snapshot()
        kind = c.get("overlay_fun", "off")
        pool = []
        if kind in ("jokes", "both"):
            pool += [("joke", t) for t in self.builtin.get("jokes", [])]
        if kind in ("facts", "both"):
            pool += [("fact", t) for t in self.builtin.get("facts", [])]
        if kind != "off":
            pool += [("own", t) for t in c.get("fun_custom", [])]
        return pool

    def next(self) -> None:
        pool = self._pool()
        key = (self.cfg.get("overlay_fun"), tuple(self.cfg.get("fun_custom")))
        if key != self._key:  # settings changed: shuffle a fresh deck
            self._key, self._deck = key, []
        if not pool:
            self.line = None
            return
        if not self._deck:
            import random
            self._deck = pool[:]
            random.shuffle(self._deck)
        self.line = self._deck.pop()


class Overlay:
    def __init__(self, cfg, data_dir: Path, on_change=None):
        self.cfg = cfg
        self.weather = Weather(cfg, data_dir)
        self.places = Places(data_dir, on_found=on_change)
        self.fun = Fun(cfg)

    def enabled(self) -> bool:
        c = self.cfg.snapshot()
        return (any(c.get(k) for k in ("overlay_clock", "overlay_date", "overlay_weather", "overlay_photo_info"))
                or c.get("overlay_fun", "off") != "off")

    def caption(self, details: dict, now: datetime | None = None) -> list[str]:
        c = self.cfg.snapshot()
        lines: list[str] = []
        if not c.get("overlay_photo_info") or not details:
            return lines
        taken = details.get("taken")
        place = self.places.name(details["lat"], details["lon"]) if "lat" in details else None
        first = " · ".join(x for x in (_nice_date(taken) if taken else "", place or "") if x)
        if first:
            lines.append(first)
        if taken and c.get("overlay_memories", True):
            n = years_ago_today(taken, now)
            if n:
                lines.append(f"{n} year{'s' if n != 1 else ''} ago today")
        return lines

    def apply(self, img: Image.Image, details: dict | None = None, now: datetime | None = None) -> Image.Image:
        main_box = None
        """Draw the enabled panels onto a copy of the (logical, upright) frame."""
        c = self.cfg.snapshot()
        if not self.enabled():
            return img
        now = now or datetime.now()
        img = img.copy()
        w, h = img.size
        unit = min(w, h)
        big = int(unit * SIZES.get(c.get("overlay_size", "medium"), SIZES["medium"]))
        small = max(12, int(big * 0.36))
        margin = int(unit * 0.035)
        # drift a few pixels each hour so nothing sits on the same pixels for ever
        drift = int(unit * 0.004)
        dx = ((now.hour % 3) - 1) * drift
        dy = (((now.hour // 3) % 3) - 1) * drift
        corner = c.get("overlay_corner", "bottom-left")
        right = corner.endswith("right")
        bottom = corner.startswith("bottom")
        pad = int(big * 0.34)

        # ---- main panel: time, date, weather
        rows: list[tuple] = []
        if c.get("overlay_clock"):
            t, ampm = _fmt_time(now, c.get("overlay_24h", False))
            rows.append(("clock", t, ampm))
        if c.get("overlay_date"):
            rows.append(("text", now.strftime("%A ") + f"{now.day} " + now.strftime("%B"), SOFT, small))
        wx = self.weather.current() if c.get("overlay_weather") else None
        if wx:
            rows.append(("weather", wx))
        if rows:
            d = ImageDraw.Draw(img)
            clock_f = _font("sans", big)
            ampm_f = _font("sans", int(big * 0.36))
            text_f = _font("sans", small)
            temp_f = _font("sans", int(big * 0.62))
            # measure
            widths, heights = [], []
            for r in rows:
                if r[0] == "clock":
                    tw = d.textlength(r[1], font=clock_f) + (d.textlength(" " + r[2], font=ampm_f) if r[2] else 0)
                    widths.append(tw)
                    heights.append(int(big * 1.02))
                elif r[0] == "text":
                    widths.append(d.textlength(r[1], font=text_f))
                    heights.append(int(small * 1.45))
                else:
                    wx = r[1]
                    icon = int(big * 0.7)
                    sub = f"{wx['words']} · H {wx['high']}° L {wx['low']}°"
                    widths.append(icon + int(big * 0.18) + max(d.textlength(f"{wx['temp']}°", font=temp_f), d.textlength(sub, font=text_f)))
                    heights.append(int(big * 0.72 + small * 1.4))
            pw = int(max(widths) + 2 * pad)
            ph = int(sum(heights) + 2 * pad - small * 0.2)
            x0 = (w - margin - pw if right else margin) + dx
            y0 = (h - margin - ph if bottom else margin) + dy
            _glass(img, (x0, y0, x0 + pw, y0 + ph), int(pad * 0.9))
            main_box = (x0, y0, x0 + pw, y0 + ph)
            d = ImageDraw.Draw(img, "RGBA")
            y = y0 + pad - int(big * 0.12)
            for r, rh in zip(rows, heights):
                tx = x0 + pad
                if r[0] == "clock":
                    d.text((tx, y), r[1], font=clock_f, fill=INK)
                    if r[2]:
                        d.text((tx + d.textlength(r[1] + " ", font=clock_f) - big * 0.12, y + big * 0.56), r[2], font=ampm_f, fill=SOFT)
                elif r[0] == "text":
                    d.text((tx, y), r[1], font=text_f, fill=SOFT)
                else:
                    wx = r[1]
                    icon = int(big * 0.7)
                    draw_icon(d, wx["icon"], tx, int(y + big * 0.06), icon, wx.get("day", True))
                    ix = tx + icon + int(big * 0.18)
                    d.text((ix, y), f"{wx['temp']}°", font=temp_f, fill=INK)
                    d.text((ix, y + int(big * 0.72)), f"{wx['words']} · H {wx['high']}° L {wx['low']}°", font=text_f, fill=SOFT)
                y += rh

        # ---- joke / fact: across from the main panel, on the other edge
        if c.get("overlay_fun", "off") != "off":
            if self.fun.line is None or self.fun._key != (c.get("overlay_fun"), tuple(c.get("fun_custom", []))):
                self.fun.next()
            if self.fun.line:
                kind, text = self.fun.line
                d = ImageDraw.Draw(img)
                head_f = _font("sans", max(11, int(small * 0.8)))
                body_f = _font("sans", int(small * 1.1))
                fpad = int(small * 0.9)
                max_w = int(w * (0.42 if w > h else 0.8))
                words, lines_, cur = text.split(), [], ""
                for word in words:
                    trial = (cur + " " + word).strip()
                    if d.textlength(trial, font=body_f) <= max_w - 2 * fpad or not cur:
                        cur = trial
                    else:
                        lines_.append(cur)
                        cur = word
                if cur:
                    lines_.append(cur)
                lines_ = lines_[:5]
                heading = {"joke": "HERE'S ONE", "fact": "DID YOU KNOW?", "own": ""}[kind]
                lh = int(body_f.size * 1.35)
                fw = int(max([d.textlength(l, font=body_f) for l in lines_] + [d.textlength(heading, font=head_f)]) + 2 * fpad)
                fh = int(2 * fpad + len(lines_) * lh + (head_f.size * 1.6 if heading else 0) - body_f.size * 0.25)
                fx0 = (w - margin - fw if right else margin) + dx
                fy0 = (margin if bottom else h - margin - fh) - dy
                _glass(img, (fx0, fy0, fx0 + fw, fy0 + fh), int(fpad * 0.9))
                d = ImageDraw.Draw(img)
                ty = fy0 + fpad - int(small * 0.1)
                if heading:
                    d.text((fx0 + fpad, ty), heading, font=head_f, fill=SUN)
                    ty += int(head_f.size * 1.6)
                for line in lines_:
                    d.text((fx0 + fpad, ty), line, font=body_f, fill=INK)
                    ty += lh

        # ---- photo details, in the opposite corner along the same edge
        lines = self.caption(details or {}, now)
        if lines:
            d = ImageDraw.Draw(img)
            cap_f = _font("sans", small)
            cpad = int(small * 0.8)
            cw = int(max(d.textlength(l, font=cap_f) for l in lines) + 2 * cpad)
            ch = int(len(lines) * small * 1.45 + 2 * cpad - small * 0.3)
            cx0 = (margin if right else w - margin - cw) - dx
            cy0 = (h - margin - ch if bottom else margin) + dy
            if main_box and (cx0 < main_box[2] + margin and cx0 + cw > main_box[0] - margin):
                # not enough width for both side by side (e.g. portrait): sit just
                # above (or below) the main panel, on the same side
                cx0 = (w - margin - cw if right else margin) + dx
                gap = int(margin * 0.5)
                cy0 = main_box[1] - gap - ch if bottom else main_box[3] + gap
            _glass(img, (cx0, cy0, cx0 + cw, cy0 + ch), int(cpad * 0.9))
            d = ImageDraw.Draw(img)
            for i, line in enumerate(lines):
                d.text((cx0 + cpad, cy0 + cpad - small * 0.1 + i * small * 1.45), line,
                       font=cap_f, fill=SUN if i == 1 else INK)
        return img
