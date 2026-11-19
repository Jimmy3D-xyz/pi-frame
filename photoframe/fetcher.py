"""URL-mode image fetch, speaking the esp32-photoframe protocol
(docs/API.md -> "URL Rotation Fetch") so esp32-photoframe-server, HA Puppet
or any compatible URL works unchanged."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import requests
from PIL import Image

from . import __version__, render
from .config import ConfigStore

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
# Deliberately not a semver >= 2.6.1: the image server then sends PNG rather
# than the e-paper-only EPDGZ format.
FIRMWARE_VERSION = f"pi-{__version__}"


class FetchError(RuntimeError):
    pass


@dataclass
class FetchResult:
    image: Image.Image | None  # upright, logical orientation; None on 304
    not_modified: bool = False
    raw: bytes = b""


def build_headers(cfg: ConfigStore, panel: tuple[int, int], etag: str) -> dict:
    c = cfg.snapshot()
    h = {
        "User-Agent": USER_AGENT,
        "X-Display-Width": str(panel[0]),
        "X-Display-Height": str(panel[1]),
        "X-Display-Orientation": c["display_orientation"],
        "X-Display-Type": "rgb",  # lets a patched server skip e-paper dithering
        "X-Firmware-Version": FIRMWARE_VERSION,
        "X-Config-Last-Updated": str(cfg.config_last_updated),
        "X-Processing-Settings": json.dumps(cfg.processing, separators=(",", ":")),
        "X-Color-Palette": json.dumps(cfg.palette, separators=(",", ":")),
    }
    if c["access_token"]:
        h["Authorization"] = f"Bearer {c['access_token']}"
    key, val = c["http_header_key"], c["http_header_value"]
    if key and val and not (key.lower() == "authorization" and c["access_token"]):
        h[key] = val
    if etag:
        h["If-None-Match"] = etag
    return h


def fetch(cfg: ConfigStore, panel: tuple[int, int], etag: str) -> tuple[FetchResult, str]:
    """Returns (result, new_etag). Raises FetchError after 3 failed attempts."""
    url = cfg.get("image_url")
    if not url:
        raise FetchError("no image URL configured")
    headers = build_headers(cfg, panel, etag)
    last_err = "unknown error"
    for attempt in range(3):
        if attempt:
            time.sleep(3)
        try:
            resp = requests.get(url, headers=headers, timeout=120, allow_redirects=True)
        except requests.RequestException as e:
            last_err = f"request failed: {e}"
            continue
        if resp.status_code == 304 and etag:
            _sync_config(cfg, resp)
            return FetchResult(None, not_modified=True), etag
        if resp.status_code != 200 or not resp.content:
            detail = ""
            try:
                detail = resp.json().get("message", "")
            except ValueError:
                pass
            last_err = f"HTTP {resp.status_code}" + (f": {detail}" if detail else "")
            continue
        body = resp.content
        if body[:2] == b"\x1f\x8b":
            raise FetchError(
                "server sent EPDGZ (e-paper) data; it should fall back to PNG for this frame's firmware version"
            )
        try:
            img = render.open_image(body)
        except Exception as e:  # noqa: BLE001
            last_err = f"could not decode image: {e}"
            continue
        _sync_config(cfg, resp)
        logical = render.logical_size(panel, cfg.get("display_orientation"))
        if resp.headers.get("X-Image-Layout", "").lower() != "logical":
            img = render.undo_native_layout(img, logical)
        new_etag = resp.headers.get("ETag", "")
        return FetchResult(img, raw=body), new_etag
    raise FetchError(last_err)


def _sync_config(cfg: ConfigStore, resp: requests.Response) -> None:
    payload = resp.headers.get("X-Config-Payload")
    if not payload:
        return
    try:
        if cfg.apply_remote_payload(json.loads(payload)):
            log.info("applied config payload from image server")
    except (json.JSONDecodeError, AttributeError) as e:
        log.warning("ignoring malformed X-Config-Payload: %s", e)
