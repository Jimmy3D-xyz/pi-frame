"""Persistent configuration, mirroring esp32-photoframe's /api/config schema.

Field names match the firmware so the image server, the Home Assistant
integration and the X-Config-Payload sync protocol all work unchanged. A few
Pi-specific fields (screen schedule, transitions, local collage) are added.
"""

from __future__ import annotations

import copy
import json
import re
import os
import threading
import time
from pathlib import Path

from .cron import CronError, parse_rules

DEFAULT_CONFIG: dict = {
    "device_name": "Pi Frame",
    "timezone": "",  # "" = use the Pi's system timezone; else IANA name or POSIX TZ
    "ntp_server": "pool.ntp.org",  # informational; the Pi uses systemd-timesyncd
    "display_orientation": "landscape",  # landscape | portrait
    "display_rotation_deg": 0,  # 0/90/180/270, applied on top of orientation
    "auto_rotate": True,
    "rotate_cron": ["*/30 * *"],
    "rotation_mode": "storage",  # storage | url
    "sd_rotation_mode": "random",  # random | sequential
    "image_url": "",
    "access_token": "",
    "http_header_key": "",
    "http_header_value": "",
    "save_downloaded_images": False,
    "ha_url": "",
    "openai_api_key": "",
    "google_api_key": "",
    "deep_sleep_enabled": False,  # accepted for compatibility; an LCD frame never deep-sleeps
    "http_password": "",
    # --- Pi / LCD extras ---
    "screen_schedule_enabled": False,
    "screen_on_time": "07:00",
    "screen_off_time": "22:30",
    "transition": "fade",  # fade | none
    "transition_ms": 1500,
    "transition_effects": "all",  # fade | moves (zooms and slides too) | all (and 3D turns); with transition "fade"
    "local_collage": True,  # pair two mismatched-orientation photos in storage mode
    "welcome_seconds": 30,  # welcome/QR screen after starting; 0 = off
    # slideshow style: classic (one photo at a time), story (collage, then each of
    # its photos), spotlight (collage, a photo, collage, the next photo, ...)
    "slideshow_style": "classic",   # the first of slideshow_styles (kept for older pages and apps)
    "slideshow_styles": ["classic"],  # the styles the slideshow takes turns with
    "style_switch_minutes": 60,     # with more than one style: switch after this long (at the end of a round); 0 = every round
    "collage_count": 0,        # 3, 4 or 5 photos; 0 = mix it up
    "collage_minutes": 5,
    "single_minutes": 10,
    "living_minutes": 5,       # living collage: swap a photo this often
    "living_spotlight": True,  # living collage: now and then, one of its photos full screen, then back
    # video moments: a short clip from a random point of a random video
    "clip_every_minutes": 0,   # 0 = off
    "clip_seconds": 20,
    # jokes and facts panel
    "overlay_fun": "off",      # off | jokes | facts | both
    "fun_custom": [],          # the user's own lines
    # presence sensor (mmWave radar OT2 output) on a GPIO pin
    "presence_enabled": False,
    "presence_pin": 22,
    "presence_timeout_minutes": 5,
    "presence_schedule_only": True,  # only wake the screen within the screen schedule's on-hours
    # fades on the display hardware's second layer (60 fps); off = the framebuffer (slower)
    "display_hw_fade": True,
    # the monitor's own brightness over DDC/CI (-1 = leave it alone)
    "monitor_brightness": -1,
    "monitor_night_enabled": False,
    "monitor_night_brightness": 40,
    "monitor_night_from": "21:00",
    "monitor_night_to": "07:00",
    "monitor_standby": False,  # also put the monitor in standby when the screen turns off
    # on-screen info
    "overlay_clock": True,
    "overlay_24h": False,
    "overlay_date": True,
    "overlay_weather": False,  # switched on once a location is chosen
    "weather_location": {},    # {"name", "label", "lat", "lon"} from the place search
    "weather_units": "c",
    "overlay_photo_info": True,
    "overlay_memories": True,
    "overlay_corner": "bottom-left",
    "overlay_size": "medium",
    # physical buttons (BCM GPIO numbers; -1 = not fitted)
    "buttons_enabled": False,
    "button_previous_pin": 17,
    "button_next_pin": 27,
    "button_screen_pin": 3,  # GPIO 3 can also wake a shut-down Pi
    "button_hold_shutdown": True,
    "button_hold_seconds": 5,
    "video_surround": True,  # photos around videos that don't fill the screen
    "video_enabled": True,  # include videos in the slideshow
    "video_sound": False,
    "video_volume": 100,  # the player's scale is curved: 70 is only about a third as loud
    "video_normalize": True,  # even out loudness between videos (measured at import)
    "video_boost_db": 0,  # 0, 6 or 12: extra gain with a limiter, for quiet soundtracks
    "video_max_seconds": 0,  # 0 = play the whole video
    # automatic backups
    "backup_auto": False,
    "backup_where": "frame",   # "frame", or a synced-folder connection (onedrive, gdrive, dropbox, nas, nas2…)
    "backup_folder": "Pi Frame backups",
    "backup_kind": "full",     # full | settings
    "backup_every": "weekly",  # daily | weekly | monthly
    "backup_time": "03:00",
    "backup_keep": 4,
    # updates from GitHub (needs a read-only token, kept outside the settings)
    "update_auto": "notify",  # off | notify | install
    "update_time": "03:30",
    "update_repo": "Jimmy3D-xyz/pi-frame",
    # demo (shop) mode: sample photos and "about this frame" slides; also shown
    # automatically on a set-up frame with no photos yet
    "demo_mode": False,
}

# Firmware defaults (Spectra 6 measured palette). Irrelevant to an LCD but
# reported so the image server has something sane to read.
DEFAULT_PALETTE = {
    "black": {"r": 2, "g": 2, "b": 2},
    "white": {"r": 190, "g": 200, "b": 200},
    "yellow": {"r": 205, "g": 202, "b": 0},
    "red": {"r": 135, "g": 19, "b": 0},
    "blue": {"r": 5, "g": 64, "b": 158},
    "green": {"r": 39, "g": 102, "b": 60},
}

DEFAULT_PROCESSING = {
    "exposure": 1.0,
    "saturation": 1.0,
    "toneMode": "contrast",
    "contrast": 1.0,
    "strength": 0.5,
    "shadowBoost": 0.0,
    "highlightCompress": 0.0,
    "midpoint": 0.5,
    "colorMethod": "rgb",
    "ditherAlgorithm": "floyd-steinberg",
    "compressDynamicRange": False,
    # Used by this frame: how a photo is laid onto the screen.
    "scaleMode": "fit",  # fit | cover
    "backgroundColor": "blur",  # blur | black | white | any palette name
}

SECRET_KEYS = {"http_password"}  # never echoed back by GET /api/config

_ENUMS = {
    "display_orientation": {"landscape", "portrait"},
    "rotation_mode": {"storage", "url"},
    "sd_rotation_mode": {"random", "sequential"},
    "transition": {"fade", "none"},
    "transition_effects": {"fade", "moves", "all"},
    "overlay_corner": {"bottom-left", "bottom-right", "top-left", "top-right"},
    "slideshow_style": {"classic", "story", "spotlight", "living"},
    "overlay_fun": {"off", "jokes", "facts", "both"},
    "overlay_size": {"small", "medium", "large"},
    "weather_units": {"c", "f"},
    "backup_kind": {"full", "settings"},
    "backup_every": {"daily", "weekly", "monthly"},
    "update_auto": {"off", "notify", "install"},
}


class ConfigError(ValueError):
    pass


def apply_timezone(tz: str) -> None:
    """Set the process timezone. Accepts an IANA name (Australia/Melbourne)
    or a POSIX TZ string (AEST-10AEDT,M10.1.0,M4.1.0/3), as the firmware does."""
    if tz:
        os.environ["TZ"] = tz
    else:
        os.environ.pop("TZ", None)
    time.tzset()


def _valid_hhmm(v: str) -> bool:
    try:
        h, m = v.split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
    except (ValueError, AttributeError):
        return False


class ConfigStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "config.json"
        self._lock = threading.RLock()
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.processing = copy.deepcopy(DEFAULT_PROCESSING)
        self.palette = copy.deepcopy(DEFAULT_PALETTE)
        self.config_last_updated = 0
        self._load()
        apply_timezone(self.config["timezone"])

    # ---------- persistence ----------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        stored = raw.get("config", {})
        self.config.update({k: v for k, v in stored.items() if k in DEFAULT_CONFIG})
        if self.config.get("update_repo") == "JimmySte/pi-frame":  # the repository moved to the Jimmy3D-xyz organization
            self.config["update_repo"] = "Jimmy3D-xyz/pi-frame"
        if "slideshow_styles" not in stored:  # saved before styles could be combined
            self.config["slideshow_styles"] = [self.config.get("slideshow_style") or "classic"]
        if self.config.get("device_name") == "PhotoFrame":  # the old default name
            self.config["device_name"] = "Pi Frame"
        self.processing.update(raw.get("processing", {}))
        self.palette.update(raw.get("palette", {}))
        self.config_last_updated = int(raw.get("config_last_updated", 0))

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "config": self.config,
                    "processing": self.processing,
                    "palette": self.palette,
                    "config_last_updated": self.config_last_updated,
                },
                indent=2,
            )
        )
        os.replace(tmp, self.path)

    def touch(self) -> None:
        with self._lock:
            self.config_last_updated = int(time.time())
            self._save()

    # ---------- accessors ----------
    def get(self, key: str):
        with self._lock:
            return self.config[key]

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.config)

    def public(self) -> dict:
        with self._lock:
            return {k: v for k, v in self.config.items() if k not in SECRET_KEYS}

    # ---------- updates ----------
    def _validate(self, updates: dict) -> dict:
        clean: dict = {}
        if "rotate_interval" in updates and "rotate_cron" not in updates:
            # Legacy firmware field: seconds -> single cron rule.
            secs = int(updates["rotate_interval"])
            mins = max(1, secs // 60)
            if mins < 60:
                updates["rotate_cron"] = [f"*/{mins} * *"]
            else:
                updates["rotate_cron"] = [f"0 */{max(1, min(23, mins // 60))} *"]
        for key, val in updates.items():
            if key not in DEFAULT_CONFIG:
                continue  # ignore unknown / read-only firmware fields (device_id, wifi_ssid...)
            default = DEFAULT_CONFIG[key]
            if key == "slideshow_style" and "slideshow_styles" in updates:
                continue  # follows the list
            if key == "slideshow_styles":
                if not isinstance(val, list) or not val or any(v not in _ENUMS["slideshow_style"] for v in val):
                    raise ConfigError(f"slideshow_styles must be a list of {sorted(_ENUMS['slideshow_style'])}")
                order = ["classic", "story", "spotlight", "living"]
                clean[key] = sorted(set(val), key=order.index)
                clean["slideshow_style"] = clean[key][0]
                continue
            if key == "fun_custom":
                if not isinstance(val, list):
                    raise ConfigError("fun_custom must be a list of lines")
                clean[key] = [str(x).strip()[:200] for x in val if str(x).strip()][:200]
                continue
            if key == "weather_location":
                if not val:
                    clean[key] = {}
                    continue
                try:
                    loc = {"name": str(val.get("name", ""))[:80], "label": str(val.get("label", ""))[:160],
                           "lat": float(val["lat"]), "lon": float(val["lon"])}
                except (AttributeError, KeyError, TypeError, ValueError) as e:
                    raise ConfigError("weather_location needs a name, lat and lon") from e
                if not (-90 <= loc["lat"] <= 90 and -180 <= loc["lon"] <= 180):
                    raise ConfigError("weather_location is outside the map")
                clean[key] = loc
                continue
            if key == "rotate_cron":
                try:
                    parse_rules(val)
                except CronError as e:
                    raise ConfigError(str(e)) from e
                clean[key] = [str(v) for v in val]
                continue
            if isinstance(default, bool):
                val = bool(val)
            elif isinstance(default, int):
                try:
                    val = int(val)
                except (TypeError, ValueError) as e:
                    raise ConfigError(f"{key} must be an integer") from e
            elif isinstance(default, str):
                val = "" if val is None else str(val)
            if key in _ENUMS and val not in _ENUMS[key]:
                raise ConfigError(f"{key} must be one of {sorted(_ENUMS[key])}")
            if key == "display_rotation_deg" and val not in (0, 90, 180, 270):
                raise ConfigError("display_rotation_deg must be 0, 90, 180 or 270")
            if key in ("screen_on_time", "screen_off_time") and not _valid_hhmm(val):
                raise ConfigError(f"{key} must be HH:MM")
            if key.startswith("button_") and key.endswith("_pin") and not (val == -1 or 2 <= val <= 27):
                raise ConfigError(f"{key} must be a GPIO number from 2 to 27, or -1 for none")
            if key == "presence_pin" and not 2 <= val <= 27:
                raise ConfigError("presence_pin must be a GPIO number from 2 to 27")
            if key == "presence_timeout_minutes" and not 1 <= val <= 120:
                raise ConfigError("presence_timeout_minutes must be 1 to 120")
            if key == "button_hold_seconds" and not 2 <= val <= 15:
                raise ConfigError("button_hold_seconds must be 2 to 15")
            if key in ("monitor_brightness", "monitor_night_brightness") and not -1 <= val <= 100:
                raise ConfigError(f"{key} must be 0 to 100 (or -1 to leave the monitor alone)")
            if key in ("monitor_night_from", "monitor_night_to") and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(val)):
                raise ConfigError(f"{key} must be a time like 21:00")
            if key == "slideshow_style" and "slideshow_styles" not in updates:
                clean["slideshow_styles"] = [val]  # one style chosen the old way
            if key == "style_switch_minutes" and val not in (0, 30, 60, 120, 240, 1440):
                raise ConfigError("style_switch_minutes must be 0 (every round), 30, 60, 120, 240 or 1440")
            if key == "collage_count" and val not in (0, 3, 4, 5):
                raise ConfigError("collage_count must be 3, 4, 5 or 0 (mix)")
            if key in ("collage_minutes", "single_minutes", "living_minutes") and not 1 <= val <= 240:
                raise ConfigError(f"{key} must be 1 to 240")
            if key == "clip_every_minutes" and val not in (0, 15, 30, 60, 120, 240):
                raise ConfigError("clip_every_minutes must be 0, 15, 30, 60, 120 or 240")
            if key == "clip_seconds" and not 5 <= val <= 120:
                raise ConfigError("clip_seconds must be 5 to 120")
            if key == "transition_ms" and not 100 <= val <= 5000:
                raise ConfigError("transition_ms must be 100 to 5000")
            if key == "video_boost_db" and val not in (0, 6, 12):
                raise ConfigError("video_boost_db must be 0, 6 or 12")
            if key == "video_volume" and not 0 <= val <= 100:
                raise ConfigError("video_volume must be 0 to 100")
            if key == "video_max_seconds" and not 0 <= val <= 7200:
                raise ConfigError("video_max_seconds must be 0 to 7200")
            if key == "welcome_seconds" and not 0 <= val <= 600:
                raise ConfigError("welcome_seconds must be 0 to 600")
            if key == "timezone" and (len(val) > 63 or not val.isprintable()):
                raise ConfigError("timezone must be 0-63 printable characters")
            if key == "update_time" and not _valid_hhmm(val):
                raise ConfigError("update_time must be HH:MM")
            if key == "update_repo" and not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", val):
                raise ConfigError("update_repo must look like owner/repository")
            if key == "backup_time" and not _valid_hhmm(val):
                raise ConfigError("backup_time must be HH:MM")
            if key == "backup_keep" and not 1 <= val <= 30:
                raise ConfigError("backup_keep must be 1 to 30")
            if key == "backup_where" and not re.fullmatch(r"frame|[a-z][a-z0-9]{1,15}", val):
                raise ConfigError("backup_where must be 'frame' or a connected service")
            if key == "backup_folder":
                val = val.strip().strip("/")
                if not val or len(val) > 200 or ".." in val.split("/") or not val.isprintable():
                    raise ConfigError("choose a folder name for the backups")
            if key == "image_url" and len(val) > 1024:
                raise ConfigError("image_url too long")
            clean[key] = val
        return clean

    def update(self, updates: dict, touch: bool = True) -> dict:
        """Validate and apply a partial update. Returns the keys that changed."""
        with self._lock:
            clean = self._validate(dict(updates))
            changed = {k: v for k, v in clean.items() if self.config.get(k) != v}
            self.config.update(clean)
            if "timezone" in changed:
                apply_timezone(self.config["timezone"])
            if touch:
                self.config_last_updated = int(time.time())
            self._save()
            return changed

    def set_processing(self, values: dict | None, touch: bool = True) -> None:
        with self._lock:
            if values is None:
                self.processing = copy.deepcopy(DEFAULT_PROCESSING)
            else:
                self.processing.update(values)
            if touch:
                self.config_last_updated = int(time.time())
            self._save()

    def set_palette(self, values: dict | None, touch: bool = True) -> None:
        with self._lock:
            if values is None:
                self.palette = copy.deepcopy(DEFAULT_PALETTE)
            else:
                self.palette.update(values)
            if touch:
                self.config_last_updated = int(time.time())
            self._save()

    def apply_remote_payload(self, payload: dict) -> bool:
        """Apply an X-Config-Payload from the image server (firmware semantics:
        apply everything present, then touch the timestamp)."""
        applied = False
        with self._lock:
            if isinstance(payload.get("config"), dict):
                cfg = dict(payload["config"])
                try:
                    self.update(cfg, touch=False)
                except ConfigError:
                    # Apply what we can, field by field, rather than dropping it all.
                    for k, v in cfg.items():
                        try:
                            self.update({k: v}, touch=False)
                        except ConfigError:
                            pass
                applied = True
            if isinstance(payload.get("processing_settings"), dict):
                self.set_processing(payload["processing_settings"], touch=False)
                applied = True
            if isinstance(payload.get("color_palette"), dict):
                self.set_palette(payload["color_palette"], touch=False)
                applied = True
            if applied:
                self.touch()
        return applied

    def factory_reset(self) -> None:
        with self._lock:
            self.config = copy.deepcopy(DEFAULT_CONFIG)
            self.processing = copy.deepcopy(DEFAULT_PROCESSING)
            self.palette = copy.deepcopy(DEFAULT_PALETTE)
            self.config_last_updated = int(time.time())
            apply_timezone("")
            self._save()
