"""The frame controller: decides what to show and when.

Equivalent of the firmware's display_flow / periodic_tasks / power_manager,
minus deep sleep. Instead, an LCD frame gets a screen on/off schedule.
"""

from __future__ import annotations

import json
import os
import logging
import math
import random
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import requests
from PIL import Image

from . import fetcher, render, screens
from .network import HOTSPOT_IP, frame_urls
from .config import ConfigStore
from .cron import CronError, next_fire, parse_rules
from .display import BaseDisplay, FileDisplay, HeadlessDisplay
from .storage import Storage, timestamp_name
from .video import VideoPlayer, is_video
from .overlay import Overlay, photo_details
from . import collage, demo, motion
from .monitor import Monitor

log = logging.getLogger(__name__)

HISTORY = 20


def device_id(data_dir: Path) -> str:
    for iface in ("wlan0", "eth0"):
        try:
            mac = Path(f"/sys/class/net/{iface}/address").read_text().strip()
            if mac and mac != "00:00:00:00:00:00":
                return mac.replace(":", "").lower()
        except OSError:
            continue
    f = data_dir / "device_id"
    if not f.exists():
        f.write_text(uuid.uuid4().hex[:12])
    return f.read_text().strip()


def _upright_size(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as im:
            w, h = im.size
            if im.getexif().get(274) in (5, 6, 7, 8):  # EXIF says rotate 90/270
                w, h = h, w
            return w, h
    except Exception:  # noqa: BLE001
        return None


class _Shape:
    """Just a width and height, for the shape checks."""

    def __init__(self, width: int, height: int):
        self.width, self.height = width, height


class Frame:
    def __init__(self, cfg: ConfigStore, storage: Storage, display: BaseDisplay, data_dir: Path):
        self.cfg = cfg
        self.storage = storage
        self.display = display
        self.data_dir = data_dir
        self.cache = data_dir / "cache"
        self.cache.mkdir(exist_ok=True)
        self.device_id = device_id(data_dir)
        self._state_path = data_dir / "state.json"
        self.state = {"etag": "", "last_fetch_error": "", "current": None, "history": []}
        self._load_state()
        self._rotate_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._recent: deque[Path] = deque(maxlen=50)
        self._sizes: dict[Path, tuple[int, int] | None] = {}
        self.current_thumb: bytes = b""
        self.next_rotation: float | None = None
        self.last_rotation: float = 0.0
        self._reschedule = False
        # pretend to play videos only on the test display (a PNG file); every real
        # screen (framebuffer or display hardware) runs mpv
        # (and with no screen connected there's nothing to play on)
        self.player = VideoPlayer(simulate=isinstance(display, (FileDisplay, HeadlessDisplay)))
        self.jobs = None  # VideoJobs, set by __main__
        self.overlay = Overlay(cfg, data_dir, on_change=self.refresh_overlay)
        self.buttons = None  # physical buttons, set by __main__ (needs the GPIO pins)
        self.monitor = Monitor(cfg, on_change=self.refresh_overlay)
        from .attention import Attention
        from .secupdates import SecurityUpdates

        # the on-screen notice when something needs a person (see attention.py)
        self.attention = Attention(cfg, data_dir, on_change=lambda: threading.Thread(
            target=self.refresh_overlay, name="attention-redraw", daemon=True).start())
        self.overlay.attention = self.attention
        self.security = SecurityUpdates(data_dir)  # Raspberry Pi OS security updates (secupdates.py)
        from .presence import Presence

        self.presence = Presence(self)
        self._base = None  # the current frame without on-screen info, for per-minute redraws
        self.thumb_version = 0  # bumps whenever the preview image changes
        self.current_full = None  # the whole picture on screen (with on-screen info), for the web page
        self._full_cache: tuple | None = None
        self._story: dict | None = None  # current collage + its photos (story/spotlight styles)
        self.demo = demo.Demo()  # samples for demo (shop) mode
        self._demo_due: float | None = None
        self._step_due: float | None = None
        self._next_clip: float | None = None
        self._base_details: dict = {}
        self._base_caption: str | None = None  # demo mode's "Sample photo" credit, redrawn with the clock
        self.cloud = None  # OneDrive folder sync, set by __main__ (or the web app)
        self.backups = None  # backups and restoring, set by __main__ (or the web app)
        self.notice: str | None = None  # None | "empty" | "wifi" while a card is on screen
        self.wifi = None  # WifiManager, when running on a Pi with NetworkManager
        self.away = False  # away from its Wi-Fi, carrying on with the photos (see _go_away)
        # moving transitions (motion.py)
        self._shown = 0  # counts pictures put on screen, so a 3D turn drawn ahead knows it's still current
        self._turn: motion.Turn | None = None  # a 3D turn being drawn for the next picture
        self._turn_n = 0
        self._queued: Path | None = None  # classic style: the next photo, chosen early for its 3D turn
        self._living: dict | None = None  # living collage style
        self._style: str | None = None  # the slideshow style showing now (of those ticked)
        self._style_since = 0.0
        self._style_steps = 0  # steps shown in the current style
        self._force_switch = False  # "New set" asked for the next style
        self._turn_now: str | None = None  # the 3D button: this change is a 3D turn of this kind
        self._turn_now_n = 0
        self._last_edge: str | None = None  # the side the last new set slid in from
        self._demo_view: dict | None = None  # what demo mode has on screen (for its zooms and slides)
        self._demo_wait: float | None = None

    # ---------- state ----------
    def _load_state(self) -> None:
        try:
            self.state.update(json.loads(self._state_path.read_text()))
        except (OSError, json.JSONDecodeError):
            pass

    def _save_state(self) -> None:
        self._state_path.write_text(json.dumps(self.state))

    @property
    def panel(self) -> tuple[int, int]:
        return self.display.size

    @property
    def logical(self) -> tuple[int, int]:
        return render.logical_size(self.panel, self.cfg.get("display_orientation"))

    # ---------- drawing ----------
    def _present(self, images: list[Image.Image], source: dict, remember: Path | None,
                 caption: str | None = None, move: dict | None = None, composed: Image.Image | None = None) -> None:
        """Put a picture on screen. move: how to get there, other than a fade (see _transition)."""
        proc = self.cfg.processing
        frame = composed if composed is not None and composed.size == self.logical else render.compose(
            images, self.logical, proc.get("scaleMode", "fit"), proc.get("backgroundColor", "blur")
        )
        # when/where the photo was taken (single stored photos only, not collages or server images)
        details = {}
        if remember is not None and len(images) == 1 and Path(remember).parent != self.cache:
            details = photo_details(Path(remember))
        self._base, self._base_details, self._base_caption = frame, details, caption
        self.overlay.fun.next()  # a new joke/fact with each new picture
        shown = self._decorate(self.overlay.apply(frame, details), caption)
        panel_img = render.to_panel(shown, self.panel, self.cfg.get("display_rotation_deg"))
        if self._turn_now:  # the 3D button: draw a turn to this picture now, then play it
            kind, self._turn_now = self._turn_now, None
            move = self._draw_turn_now(kind, shown) or move
        self._transition(self._dim(panel_img), move)
        self._shown += 1
        self.notice = None
        self.current_thumb = render.thumbnail_jpeg(shown)
        self.current_full = shown
        self.thumb_version += 1
        self.last_rotation = time.time()
        source = dict(source, shown_at=int(self.last_rotation))
        self.state["current"] = source
        if remember is not None:
            hist = [h for h in self.state["history"] if h.get("path") != str(remember)]
            hist.append({**source, "path": str(remember)})
            self.state["history"] = hist[-HISTORY:]
        self._save_state()
        self._prune_cache()
        self._notify_ha("update")

    # ---------- moving transitions ----------
    def effects(self, demo_mode: bool = False) -> str:
        """none (instant), fade, moves (zooms and slides too) or all (and 3D turns)."""
        if self.cfg.get("transition") != "fade":
            return "none"
        if not getattr(self.display, "motions", False):
            return "fade"
        return "all" if demo_mode else self.cfg.get("transition_effects")

    def _can(self, what: str) -> bool:
        """Can a zoom/slide ("moves") or a 3D turn ("3d") be used now?"""
        eff = self.effects(self.demo_active())
        if what == "3d":
            # 3D frames are drawn ahead, undimmed: not while the picture is darkened for the night
            return eff == "all" and (self.monitor is None or self.monitor.software_level >= 0.999)
        return eff in ("moves", "all")

    def _motion_ms(self, base: int) -> int:
        """Motion lengths follow the fade length setting (normal 1.5 s)."""
        k = min(2.0, max(0.6, self.cfg.get("transition_ms") / 1500))
        return int(base * k)

    def _transition(self, img: Image.Image, move: dict | None) -> None:
        fade = self.cfg.get("transition_ms") if self.cfg.get("transition") == "fade" else 0
        kind = (move or {}).get("kind")
        if fade and kind:
            rot = self.cfg.get("display_rotation_deg")

            def panel(rect):
                return motion.rect_to_panel(rect, self.logical, self.panel, rot)

            try:
                if kind == "zoom-in" and self._can("moves"):
                    return self.display.zoom_in(img, panel(move["src"]), panel(move["dst"]), self._motion_ms(1600), fade)
                if kind == "zoom-out" and self._can("moves"):
                    return self.display.zoom_out(img, panel(move["src"]), panel(move["dst"]), self._motion_ms(1600),
                                                 fade)
                turn = move.get("turn")
                if kind == "part3d":
                    if turn is not None and turn.ready() and self._can("3d"):
                        return self.display.play_part(turn.frames, turn.size, panel(move["rect"]), img, turn.ms, fade,
                                                      name=motion.NAMES.get(turn.kind, "3D turn"))
                    if turn is not None and not turn.ready():
                        log.info("%s not drawn yet, so it slides in instead", motion.NAMES.get(turn.kind, "3D turn"))
                        turn.cancel()
                    move = move["fallback"]
                    kind = move["kind"]
                if kind == "slide" and self._can("moves"):
                    edge = move.get("edge")
                    if edge:  # which panel edge the logical edge is on
                        edge = motion.edge_to_panel(edge, self.logical, self.panel, rot)
                    return self.display.slide_in(img, panel(move["rect"]), self._motion_ms(1100 if not edge else 1500),
                                                 fade, edge=edge)
                if kind == "3d" and turn is not None and not turn.ready():
                    log.info("%s not drawn yet, so a fade instead", motion.NAMES.get(turn.kind, "3D turn"))
                    turn.cancel()  # stop drawing it: it's no use now
                if kind == "3d" and turn is not None and turn.ready() and self._can("3d"):
                    return self.display.play(turn.frames, turn.size, img, turn.ms, 0 if move.get("info_drawn") else 400,
                                             name=motion.NAMES.get(turn.kind, "3D turn"))
            except Exception:  # noqa: BLE001
                log.exception("moving transition")
        self.display.show(img, fade)

    def _new_set_move(self) -> dict | None:
        """How a new set arrives: half the time it slides in, otherwise a fade."""
        if random.random() < 0.5:
            return None
        return {"kind": "slide", "rect": (0, 0, *self.logical), "edge": self._set_edge()}

    def _set_edge(self) -> str:
        """Which side a new set slides in from: any side, but not the same one twice running."""
        edge = random.choice([e for e in ("left", "right", "top", "bottom") if e != self._last_edge])
        self._last_edge = edge
        return edge

    def turn_now(self, kind: str | None = None) -> tuple[bool, str]:
        """The 3D button: a new set (as the New set button) with a 3D turn, drawn there and then
        (a few seconds). With Classic, just the next photo."""
        if not getattr(self.display, "motions", False) or self.cfg.get("transition") != "fade":
            return False, "3D turns need the display hardware and changes that aren't instant"
        if kind not in motion.KINDS_3D:
            kind = motion.KINDS_3D[self._turn_now_n % len(motion.KINDS_3D)]
            self._turn_now_n += 1
        self._turn_now = kind
        try:
            return self.next_set()
        finally:
            self._turn_now = None

    def _draw_turn_now(self, kind: str, shown: Image.Image) -> dict | None:
        if self.current_full is None or (self.monitor is not None and self.monitor.software_level < 0.999):
            return None
        self._drop_turn()
        ms = self._motion_ms(1300)
        log.info("%s: drawing it now", motion.NAMES[kind])
        turn = motion.Turn(kind, self.current_full, shown, self.logical, self.panel,
                           self.cfg.get("display_rotation_deg"), ms=ms,
                           frames=motion.frames_for(ms, self.logical, self.panel))
        if not turn.wait(90):
            return None
        return {"kind": "3d", "turn": turn, "info_drawn": True}

    def _next_turn_kind(self, mix: bool) -> str | None:
        """Which 3D turn next; with mix, every other change is a plain fade."""
        cycle = [k for k in motion.KINDS_3D for k in (k, None)] if mix else list(motion.KINDS_3D)
        kind = cycle[self._turn_n % len(cycle)]
        self._turn_n += 1
        return kind

    def _prepare_turn(self, key, new, mix: bool = True) -> None:
        """Start drawing a 3D turn from what's on screen to the next picture (new:
        the logical-size picture, or a function making it)."""
        self._drop_turn()
        if not self._can("3d") or self.current_full is None:
            return
        kind = self._next_turn_kind(mix)
        if kind is None:
            return
        ms = self._motion_ms(1300)  # one frame for every screen refresh, however slow it's set
        self._turn = motion.Turn(kind, self.current_full, new, self.logical, self.panel,
                                 self.cfg.get("display_rotation_deg"), key=(key, self._shown), ms=ms,
                                 frames=motion.frames_for(ms, self.logical, self.panel))

    def _take_turn(self, key) -> motion.Turn | None:
        """The 3D turn drawn for this picture, if it was drawn from what's on screen now."""
        turn, self._turn = self._turn, None
        if turn is not None and turn.key == (key, self._shown):
            return turn
        if turn is not None:
            turn.cancel()
        return None

    def _drop_turn(self) -> None:
        if self._turn is not None:
            self._turn.cancel()
            self._turn = None

    def _decorate(self, shown: Image.Image, caption: str | None) -> Image.Image:
        """Extras over the on-screen info: demo mode's sample credit, and (away from its Wi-Fi)
        how to reach the frame through its own."""
        corner = self.cfg.get("overlay_corner")
        if caption:
            shown = demo.draw_caption(shown, caption, corner)
        if self.away and self.wifi is not None and self.wifi.status.hotspot_ssid:
            st = self.wifi.status
            shown = screens.away_badge(shown, st.hotspot_ssid, st.hotspot_password, corner)
        return shown

    # ---------- away from its Wi-Fi ----------
    AWAY_AFTER = 60  # seconds of the Wi-Fi setup screen before the photos carry on

    def can_go_away(self) -> bool:
        """A set-up frame with something to show can carry on without Wi-Fi."""
        if (Path(self.data_dir) / ".first-run").exists():
            return False
        return self.demo_active() or any(self.storage.images(a["name"]) for a in self.storage.albums())

    def _go_away(self) -> bool:
        """Leave the Wi-Fi setup screen and carry on with the photos, keeping the frame's own
        Wi-Fi up so a phone can still reach it (with a reminder of how in a corner)."""
        if self.wifi is None or self.wifi.status.mode != "setup" or self.notice != "wifi" or not self.can_go_away():
            return False
        self.away = self.wifi.away = True
        self.overlay.offline = True
        self.notice = None
        log.info("away from home Wi-Fi: carrying on with the photos; the frame's own Wi-Fi %s stays on",
                 self.wifi.status.hotspot_ssid)
        return True

    def _away_soon(self) -> None:
        def later():
            if self._stop.wait(self.AWAY_AFTER):
                return
            with self._rotate_lock:
                went = self._go_away()
            if went:
                self._resume()

        threading.Thread(target=later, name="away", daemon=True).start()

    def _cache_image(self, img: Image.Image) -> Path:
        path = self.cache / f"{time.time_ns()}.jpg"
        img.save(path, "JPEG", quality=92)
        return path

    def _prune_cache(self) -> None:
        keep = {h.get("path") for h in self.state["history"]}
        cur = (self.state.get("current") or {}).get("path")
        for p in self.cache.glob("*.jpg"):
            if str(p) not in keep and str(p) != cur:
                p.unlink(missing_ok=True)

    # ---------- public actions ----------
    def show_path(self, path: Path, label: str | None = None) -> None:
        with self._rotate_lock:
            img = render.open_image(path, self.logical)
            self._present([img], {"source": "storage", "filepath": label or path.name, "path": str(path)}, path)

    def show_upload(self, data: bytes) -> None:
        """POST /api/display-image: show immediately without adding to an album."""
        with self._rotate_lock:
            img = render.open_image(data, self.logical)
            cached = self._cache_image(render.store_copy(img, self.panel))
            self._present([img], {"source": "upload", "path": str(cached)}, cached)

    def rotate(self) -> tuple[bool, str]:
        """Show the next image according to rotation_mode. Returns (ok, message)."""
        with self._rotate_lock:
            if self.notice == "wifi" and not self._go_away():  # (Next leaves the setup screen when it can)
                return False, "the frame is in Wi-Fi setup mode"
            if self.notice in ("update", "power"):
                return False, "the frame is updating" if self.notice == "update" else "the frame is shutting down"
            if self.player.playing:
                self.player.stop()  # the video's thread then moves on to the next photo
                return True, "Stopped the video"
            if self.demo_active():
                return self._demo_step()
            if self.cfg.get("rotation_mode") == "url":
                return self._rotate_url()
            return self._rotate_storage()

    def previous(self) -> tuple[bool, str]:
        with self._rotate_lock:
            hist = self.state["history"]
            if len(hist) < 2:
                return False, "no earlier photo to go back to"
            hist.pop()  # current
            prev = hist.pop()
            path = Path(prev["path"])
            if not path.exists():
                self._save_state()
                return False, "previous photo no longer exists"
            img = render.open_image(path, self.logical)
            self._present([img], prev, path)
            return True, "showing previous photo"

    # ---------- demo (shop) mode ----------
    def demo_active(self) -> bool:
        """Switched on, or a set-up frame with no photos at all yet."""
        if not self.demo.available():
            return False
        if self.cfg.get("demo_mode"):
            return True
        if self.cfg.get("rotation_mode") != "storage" or (Path(self.data_dir) / ".first-run").exists():
            return False
        return not any(self.storage.images(a["name"]) for a in self.storage.albums())

    def _demo_step(self) -> tuple[bool, str]:
        kind, item, secs, how = self.demo.peek()
        turn = self._turn if how == "3d" and kind == "photo" and self._turn is not None else None
        if turn is not None and turn.key == (item.path, self._shown) and not turn.ready() and turn.error is None:
            # the 3D turn is still being drawn: the demo waits for it (up to half a minute)
            if self._demo_wait is None:
                self._demo_wait = time.time()
            if time.time() - self._demo_wait < 30:
                self._demo_due = time.time() + 1
                return True, "demo: waiting for the 3D turn"
        self._demo_wait = None
        kind, item, secs, how = self.demo.next()
        view, logical = self._demo_view or {}, self.logical
        scale = self.cfg.processing.get("scaleMode", "fit")
        if kind == "photo":
            img = render.open_image(item.path, logical)
            move, composed = None, None
            if how == "3d":
                turn = self._take_turn(item.path)
                if turn is not None:
                    move, composed = {"kind": "3d", "turn": turn}, turn.new_full
            elif how == "zoom-in" and view.get("kind") == "collage" and item.path in view["boxes"]:
                box = view["boxes"][item.path]
                move = {"kind": "zoom-in", "src": motion.tile_source(img.size, box, logical, scale),
                        "dst": motion.box_rect(box)}
            self._present([img], {"source": "demo", "filepath": f"Sample: {item.title}"}, None, caption=item.caption,
                          move=move, composed=composed)
            self._demo_view = {"kind": "photo", "path": item.path, "size": img.size}
        elif kind in ("collage", "swap"):
            if kind == "collage":
                imgs = [render.open_image(s.path, logical) for s in item]
                base, boxes = collage.layout(imgs, logical)
                move = None
                if how == "zoom-out" and view.get("kind") == "photo" and view["path"] in [s.path for s in item]:
                    box = boxes[[s.path for s in item].index(view["path"])]
                    move = {"kind": "zoom-out", "src": motion.tile_source(view["size"], box, logical, scale),
                            "dst": motion.box_rect(box)}
                view = {"kind": "collage", "base": base, "boxes": {s.path: b for s, b in zip(item, boxes)},
                        "order": [s.path for s in item], "list": boxes}
            else:
                space, sample = item
                if view.get("kind") != "collage":
                    return self._demo_step()
                box = view["list"][space]
                base = view["base"].copy()
                collage.place_tile(base, render.open_image(sample.path, logical), box)
                old = view["order"][space]
                view["boxes"].pop(old, None)
                view["boxes"][sample.path], view["order"][space], view["base"] = box, sample.path, base
                move = {"kind": "slide", "rect": motion.box_rect(box)}
            names = [s.title.split(",")[0] for s in self.demo.collage[:2]]
            self._present([base], {"source": "demo", "filepath": f"Sample collage of {len(self.demo.collage)}"}, None,
                          caption="Collage style · " + ", ".join(names) + "…", move=move, composed=base)
            self._demo_view = view
        else:
            title, text, with_qr = item
            backdrop = Image.open(random.choice(self.demo.samples).path)
            self._show_card(demo.info_card(logical, title, text, frame_urls()[0] if with_qr else None, backdrop),
                            "demo", fade=self.cfg.get("transition_ms") if self.cfg.get("transition") == "fade" else 0)
            self.state["current"] = {"source": "demo", "filepath": f"About: {title}", "shown_at": int(time.time())}
            self._demo_view = None
        self._demo_due = time.time() + secs
        # the next step is a 3D turn: start drawing it now
        nk, nitem, _secs, nhow = self.demo.peek()
        if nk == "photo" and nhow == "3d":
            proc = dict(self.cfg.processing)
            self._prepare_turn(nitem.path, lambda p=nitem.path: render.compose(
                [render.open_image(p, logical)], logical, proc.get("scaleMode", "fit"), proc.get("backgroundColor", "blur")),
                mix=False)
        return True, f"demo: {kind}" + (f" ({how})" if how != "fade" else "")

    # ---------- slideshow styles ----------
    def styles(self) -> list[str]:
        return list(self.cfg.get("slideshow_styles") or [self.cfg.get("slideshow_style") or "classic"])

    def active_style(self) -> str:
        """The style showing now: with several ticked, they take turns."""
        styles = self.styles()
        if self._style not in styles:
            self._style, self._style_since, self._style_steps = styles[0], time.time(), 0
        return self._style

    def story_style(self) -> str | None:
        style = self.active_style()
        if style in ("story", "spotlight", "living") and self.cfg.get("rotation_mode") == "storage":
            return style
        return None

    CLASSIC_ROUND = 5  # photos in a round of Classic, when styles change every round

    def _round_over(self, every_round: bool = False) -> bool:
        """At a good place to change style: never mid-story or mid-way through a living collage."""
        if every_round and self._style_steps == 0:
            return False  # the style hasn't shown anything yet
        if self._style in ("story", "spotlight"):
            st = self._story
            return st is None or st["i"] >= len(st["steps"]) - 1
        if self._style == "living":
            lv = self._living
            return lv is None or (lv["swaps"] >= 2 * len(lv["photos"]) and lv.get("spot") is None)
        return not every_round or self._style_steps >= self.CLASSIC_ROUND

    def next_set(self) -> tuple[bool, str]:
        """Skip the rest of this round: a new collage and its photos, a new living collage
        layout, or (with styles taking turns) the next style."""
        with self._rotate_lock:
            if self.demo_active() or self.cfg.get("rotation_mode") != "storage" or self.player.playing \
                    or self.notice in ("wifi", "update", "power"):
                pass
            else:
                self._story = self._living = None
                self._force_switch = True
        ok, msg = self.rotate()
        return ok, msg

    def _maybe_switch_style(self) -> None:
        styles = self.styles()
        self.active_style()
        forced, self._force_switch = self._force_switch, False
        if len(styles) < 2:
            return
        every = self.cfg.get("style_switch_minutes")
        if not forced and every and time.time() - self._style_since < every * 60:
            return
        if not forced and not self._round_over(every_round=not every):
            return
        self._style = styles[(styles.index(self._style) + 1) % len(styles)]
        self._style_since = time.time()
        self._style_steps = 0
        self._story = self._living = None
        self._reschedule = True  # classic goes back to the schedule rules
        log.info("slideshow style: %s now", self._style)

    def reset_story(self) -> None:
        self._story = None
        self._living = None
        self._style = None  # start again from the first ticked style
        self._step_due = None
        self.poke()

    def _new_story(self) -> bool:
        n = self.cfg.get("collage_count") or random.choice((3, 4, 5))
        mode = self.cfg.get("sd_rotation_mode")
        chosen: list[Path] = []
        for _ in range(n * 3):
            p = self.storage.pick(mode, set(self._recent) | set(chosen), videos=False)
            if p is None or p in chosen:
                continue
            chosen.append(p)
            if len(chosen) == n:
                break
        if len(chosen) < 3:
            return False  # too few photos for a collage: classic it is
        self._recent.extend(chosen)
        if self.story_style() == "spotlight":
            steps = []
            for p in chosen:
                steps += [("collage", None), ("photo", p)]
        else:
            steps = [("collage", None)] + [("photo", p) for p in chosen]
        self._story = {"photos": chosen, "steps": steps, "i": -1}
        return True

    def _story_step(self) -> tuple[bool, str]:
        st = self._story
        if st is None or st["i"] >= len(st["steps"]) - 1 or not all(p.exists() for p in st["photos"]):
            if not self._new_story():
                return self._rotate_classic()
            st = self._story
        st["i"] += 1
        kind, path = st["steps"][st["i"]]
        prev = st["steps"][st["i"] - 1] if st["i"] > 0 else (None, None)
        scale = self.cfg.processing.get("scaleMode", "fit")
        if kind == "collage":
            imgs = [render.open_image(p, self.logical) for p in st["photos"]]
            img, boxes = collage.layout(imgs, self.logical)
            st["boxes"] = {p: (b, im.size) for p, b, im in zip(st["photos"], boxes, imgs)}
            move = self._new_set_move() if st["i"] == 0 else None
            if prev[0] == "photo" and prev[1] in st["boxes"]:  # spotlight: the photo shrinks back into its space
                box, size = st["boxes"][prev[1]]
                move = {"kind": "zoom-out", "src": motion.tile_source(size, box, self.logical, scale),
                        "dst": motion.box_rect(box)}
            remember = self._cache_image(img)
            self._present([img], {"source": "storage", "filepath": f"Collage of {len(imgs)} photos"}, remember,
                          move=move)
            minutes = self.cfg.get("collage_minutes")
        else:
            img = render.open_image(path, self.logical)
            move, composed = None, None
            if prev[0] == "collage" and path in st.get("boxes", {}):  # the photo grows out of the collage
                box, size = st["boxes"][path]
                move = {"kind": "zoom-in", "src": motion.tile_source(img.size, box, self.logical, scale),
                        "dst": motion.box_rect(box)}
            else:
                turn = self._take_turn(path)
                if turn is not None:
                    move, composed = {"kind": "3d", "turn": turn}, turn.new_full
            self._present([img], {"source": "storage", "filepath": f"{path.parent.name}/{path.name}"}, path,
                          move=move, composed=composed)
            minutes = self.cfg.get("single_minutes")
        self._step_due = time.time() + minutes * 60
        # story: photo after photo, so draw the 3D turn to the next one now
        nxt = st["steps"][st["i"] + 1] if st["i"] + 1 < len(st["steps"]) else (None, None)
        if kind == "photo" and nxt[0] == "photo":
            logical, proc = self.logical, dict(self.cfg.processing)
            self._prepare_turn(nxt[1], lambda p=nxt[1]: render.compose(
                [render.open_image(p, logical)], logical, proc.get("scaleMode", "fit"), proc.get("backgroundColor", "blur")))
        return True, f"{kind} {st['i'] + 1} of {len(st['steps'])}"

    # ---------- living collage ----------
    def _living_step(self) -> tuple[bool, str]:
        """A collage where one photo at a time is replaced, sliding in like a print laid
        on top. After every space has changed twice, a new layout."""
        lv = self._living
        if lv is None or not all(p.exists() for p in lv["photos"]):
            return self._living_new()
        scale = self.cfg.processing.get("scaleMode", "fit")
        if lv.get("spot") is not None:  # a photo's been in the spotlight: back into its space
            i, size = lv.pop("spot")
            box = lv["boxes"][i]
            self._present([lv["base"]], {"source": "storage", "filepath": f"Living collage of {len(lv['photos'])} photos"},
                          self._cache_image(lv["base"]), composed=lv["base"],
                          move={"kind": "zoom-out", "src": motion.tile_source(size, box, self.logical, scale),
                                "dst": motion.box_rect(box)})
            self._step_due = time.time() + self.cfg.get("living_minutes") * 60
            self._living_ahead()
            return True, "living collage: back to the collage"
        if lv["swaps"] >= 2 * len(lv["photos"]):
            return self._living_new()
        if self.cfg.get("living_spotlight") and lv["since_spot"] >= 3:
            # now and then, one of its photos grows to fill the screen for a while
            i = lv["last"] if lv.get("last") is not None and random.random() < 0.5 else random.randrange(len(lv["photos"]))
            p, box = lv["photos"][i], lv["boxes"][i]
            img = render.open_image(p, self.logical)
            lv["spot"], lv["since_spot"] = (i, img.size), 0
            self._present([img], {"source": "storage", "filepath": f"{p.parent.name}/{p.name}"}, p,
                          move={"kind": "zoom-in", "src": motion.tile_source(img.size, box, self.logical, scale),
                                "dst": motion.box_rect(box)})
            self._step_due = time.time() + self.cfg.get("single_minutes") * 60
            return True, f"living collage: spotlight on {p.name}"
        queued, lv["queued"] = lv.get("queued"), None
        if queued and queued[1].exists() and queued[1] not in lv["photos"]:
            i, p = queued
        else:
            pick = self._living_choose(lv)
            if pick is None:
                return self._living_new()
            i, p = pick
        box = lv["boxes"][i]
        self._recent.append(p)
        img = render.open_image(p, self.logical)
        base = lv["base"].copy()
        collage.place_tile(base, img, box)
        lv["base"], lv["photos"][i] = base, p
        lv["swaps"] += 1
        lv["since_spot"] += 1
        lv["last"] = i
        move = {"kind": "slide", "rect": motion.box_rect(box)}
        turn = self._take_turn(("tile", i, p))
        if turn is not None:  # this one turns over in 3D, just in its own space
            move = {"kind": "part3d", "turn": turn, "rect": turn.rect, "fallback": move}
        self._present([base], {"source": "storage", "filepath": f"Living collage: {p.parent.name}/{p.name}"},
                      self._cache_image(base), move=move, composed=base)
        self._step_due = time.time() + self.cfg.get("living_minutes") * 60
        self._living_ahead()
        return True, f"living collage: swapped in {p.name}" + (" (3D)" if turn is not None else "")

    def _living_choose(self, lv: dict):
        """The next space to change, and a photo whose shape suits it."""
        i = lv["order"][lv["swaps"] % len(lv["photos"])]
        box = lv["boxes"][i]
        want = (box[2] - box[0]) / max(1, box[3] - box[1])
        mode = self.cfg.get("sd_rotation_mode")
        best = None
        for _ in range(8):
            p = self.storage.pick(mode, set(self._recent) | set(lv["photos"]), videos=False)
            if p is None or p in lv["photos"]:
                continue
            size = self._sizes.get(p) or _upright_size(p)
            self._sizes[p] = size
            cost = abs(math.log(want / (size[0] / size[1]))) if size else 9
            if best is None or cost < best[0]:
                best = (cost, p)
            if cost < 0.2 or mode == "sequential":
                break
        return (i, best[1]) if best else None

    def _living_ahead(self) -> None:
        """If the next step is a swap, choose it now and (every other time) draw it turning
        over in 3D, in its own space, in the background."""
        lv = self._living
        if lv is None or lv.get("spot") is not None or lv["swaps"] >= 2 * len(lv["photos"]):
            return
        if self.cfg.get("living_spotlight") and lv["since_spot"] >= 3:
            return  # a spotlight comes next
        if not self._can("3d"):
            return
        pick = self._living_choose(lv)
        if pick is None:
            return
        lv["queued"] = pick
        i, p = pick
        kind = self._next_turn_kind(mix=True)
        if kind is None:
            return
        box = lv["boxes"][i]
        W, H = self.logical
        m = int(min(W, H) * 0.06)  # a little room around the space, for the turning photo's near edge
        part = (max(0, box[0] - m), max(0, box[1] - m), min(W, box[2] + m), min(H, box[3] + m))
        under = lv["base"].crop(part)
        under.paste(collage.BACKGROUND, (box[0] - part[0], box[1] - part[1], box[2] - part[0], box[3] - part[1]))
        tile = (box[2] - box[0], box[3] - box[1])
        logical = self.logical

        def new_tile(path=p):
            from PIL import ImageOps
            return ImageOps.fit(render.open_image(path, logical), tile, Image.LANCZOS, centering=(0.5, 0.4))

        self._drop_turn()
        ms = self._motion_ms(1300)
        self._turn = motion.Turn(kind, lv["base"].crop(box), new_tile, logical, self.panel,
                                 self.cfg.get("display_rotation_deg"), key=(("tile", i, p), self._shown), ms=ms,
                                 frames=motion.frames_for(ms, logical, self.panel), under=under,
                                 at=(box[0] - part[0], box[1] - part[1]))
        self._turn.rect = (part[0], part[1], part[2] - part[0], part[3] - part[1])

    def _living_new(self) -> tuple[bool, str]:
        n = self.cfg.get("collage_count") or random.choice((3, 4, 5))
        mode = self.cfg.get("sd_rotation_mode")
        chosen: list[Path] = []
        for _ in range(n * 3):
            p = self.storage.pick(mode, set(self._recent) | set(chosen), videos=False)
            if p is not None and p not in chosen:
                chosen.append(p)
            if len(chosen) == n:
                break
        if len(chosen) < 3:
            self._living = None
            return self._rotate_classic()
        self._recent.extend(chosen)
        imgs = [render.open_image(p, self.logical) for p in chosen]
        base, boxes = collage.layout(imgs, self.logical)
        order = list(range(len(chosen)))
        random.shuffle(order)
        self._living = {"photos": chosen, "boxes": boxes, "base": base, "order": order, "swaps": 0,
                        "since_spot": 0}
        self._present([base], {"source": "storage", "filepath": f"Living collage of {len(chosen)} photos"},
                      self._cache_image(base), composed=base,
                      move=self._new_set_move())
        self._step_due = time.time() + self.cfg.get("living_minutes") * 60
        self._living_ahead()
        return True, "living collage: new layout"

    # ---------- video moments ----------
    def play_clip(self) -> tuple[bool, str]:
        """A short clip from a random point of a random video, then back to
        whatever was on screen."""
        if self.player.playing or self.notice is not None or not self.display.screen_on:
            return False, "not now"
        videos = [p for p in self.storage.rotation_pool(videos=True) if is_video(p)]
        if not videos:
            return False, "no videos"
        path = random.choice(videos)
        length = self.cfg.get("clip_seconds")
        duration = float(self.storage.video_meta(path).get("duration") or 0)
        start = random.uniform(0, max(0.0, duration - length)) if duration > length + 1 else 0.0
        self.player.playing = path
        c = self.cfg.snapshot()

        def run():
            gain = float(self.storage.video_meta(path).get("gain_db", 0.0)) if c["video_normalize"] else 0.0
            self._release_display()  # mpv needs the display hardware
            try:
                result = self.player.play(path, rotate=self._video_rotation(),
                                          cover=self.cfg.processing.get("scaleMode") == "cover",
                                          sound=c["video_sound"], volume=c["video_volume"], max_seconds=length,
                                          duration=duration, boost_db=c["video_boost_db"], gain_db=gain, start=start)
            finally:
                self._reacquire_display()  # take the screen back, however the video ended
            log.info("video moment %s from %.0fs: %s", path.name, start, result)
            # back to what was showing (the screen's contents survive mpv)
            self.refresh_overlay()

        threading.Thread(target=run, name="clip", daemon=True).start()
        return True, f"playing {length}s of {path.name}"

    def _rotate_storage(self) -> tuple[bool, str]:
        self._maybe_switch_style()
        self._style_steps += 1
        style = self.story_style()
        if style == "living":
            return self._living_step()
        if style:
            return self._story_step()
        return self._rotate_classic()

    def _rotate_classic(self) -> tuple[bool, str]:
        mode = self.cfg.get("sd_rotation_mode")
        videos = self.cfg.get("video_enabled")
        first, self._queued = self._queued, None
        if first is not None and (not first.exists() or first not in self.storage.rotation_pool(videos)):
            first = None  # deleted, or its album switched off, since it was chosen
        if first is None:
            first = self.storage.pick(mode, set(self._recent), videos=videos)
        if first is None:
            has_any = any(self.storage.images(a["name"]) for a in self.storage.albums())
            reason = (
                "None of the albums with photos are switched on for the slideshow."
                if has_any
                else "There are no photos on this frame yet."
            )
            self._show_card(screens.empty_card(self.logical, frame_urls(), reason), "empty")
            return False, "no photos in enabled albums"
        self._recent.append(first)
        if is_video(first):
            self._start_video(first)
            return True, "Playing video"
        images = [render.open_image(first, self.logical)]
        label = f"{first.parent.name}/{first.name}"
        if self.cfg.get("local_collage") and render.is_mismatched(images[0], self.logical):
            partner = self._find_partner(first)
            if partner is not None:
                self._recent.append(partner)
                images.append(render.open_image(partner, self.logical))
                label += f" + {partner.parent.name}/{partner.name}"
        move, composed = None, None
        turn = self._take_turn(first)
        if len(images) == 1:
            remember = first
            if turn is not None:
                move, composed = {"kind": "3d", "turn": turn}, turn.new_full
        else:  # remember the composed collage so "previous" can re-show it
            proc = self.cfg.processing
            collage = render.compose(images, self.logical, proc.get("scaleMode"), proc.get("backgroundColor"))
            remember = self._cache_image(collage)
            images = [collage]
        self._present(images, {"source": "storage", "filepath": label}, remember, move=move, composed=composed)
        self._queue_next()
        return True, "Image rotation triggered"

    def _queue_next(self) -> None:
        """With 3D turns on: choose the next photo now, and draw the turn to it in the background."""
        if not self._can("3d"):
            return
        nxt = self.storage.pick(self.cfg.get("sd_rotation_mode"), set(self._recent),
                                videos=self.cfg.get("video_enabled"))
        if nxt is None:
            return
        self._queued = nxt
        if is_video(nxt):
            return
        size = self._sizes.get(nxt) or _upright_size(nxt)
        self._sizes[nxt] = size
        if self.cfg.get("local_collage") and size and render.is_mismatched(_Shape(*size), self.logical):
            return  # it'll be shown with a partner: no turn for that

        def make(path=nxt, logical=self.logical, proc=dict(self.cfg.processing)):
            return render.compose([render.open_image(path, logical)], logical, proc.get("scaleMode", "fit"),
                                  proc.get("backgroundColor", "blur"))

        self._prepare_turn(nxt, make)

    def _find_partner(self, first: Path) -> Path | None:
        screen_portrait = self.logical[1] > self.logical[0]
        pool = [p for p in self.storage.rotation_pool() if p != first and p not in self._recent]
        random.shuffle(pool)
        for p in pool[:60]:
            if p not in self._sizes:
                self._sizes[p] = _upright_size(p)
            size = self._sizes[p]
            if not size:
                continue
            w, h = size
            if (screen_portrait and w / h >= 1.15) or (not screen_portrait and h / w >= 1.15):
                return p
        return None

    def _rotate_url(self) -> tuple[bool, str]:
        try:
            result, etag = fetcher.fetch(self.cfg, self.panel, self.state.get("etag", ""))
        except fetcher.FetchError as e:
            self.state["last_fetch_error"] = str(e)
            self._save_state()
            log.warning("image fetch failed: %s", e)
            return False, str(e)
        self.state["etag"] = etag
        self.state["last_fetch_error"] = ""
        if result.not_modified:
            self._save_state()
            return True, "image not modified"
        img = result.image
        remember = self._cache_image(img)
        if self.cfg.get("save_downloaded_images"):
            try:
                self.storage.add(self.storage.DOWNLOADS_ALBUM, timestamp_name(), img)
                self.storage.prune_downloads()
            except Exception as e:  # noqa: BLE001
                log.warning("could not save downloaded image: %s", e)
        self._present([img], {"source": "url"}, remember)
        return True, "Image rotation triggered"

    # ---------- software updates on screen ----------
    UPDATE_BUSY = {"queued": 0, "unpacking": 1, "backing-up": 2, "installing": 3, "checking": 4}

    def _update_status(self) -> dict:
        from .updater import Updates

        return Updates(self.data_dir).status()

    def begin_update(self, version: str, status_fn=None) -> None:
        """Show the update screen and follow the updater's progress until the
        frame restarts (or the update stops early)."""
        self._update_version = version
        self._show_update_step(0)
        threading.Thread(target=self._watch_update, args=(status_fn or self._update_status, False, None, 0),
                         name="update-screen", daemon=True).start()

    def _show_update_step(self, step: int) -> None:
        v = getattr(self, "_update_version", "") or "the new version"
        sub = (f"Checking version {v} starts properly." if step >= 4 else
               f"Installing version {v}. This takes a few minutes; the frame restarts by itself.")
        card = screens.update_card(self.logical, "Updating Pi Frame", sub, step=step)
        with self._rotate_lock:
            self.player.stop()
            self._power(True)
            self._show_card(card, "update")

    def _watch_update(self, status_fn, finishing: bool = False, then=None, shown: int | None = None) -> None:
        last, deadline = shown, time.time() + 25 * 60  # shown: the step already on screen
        while time.time() < deadline and not self._stop.is_set():
            st = status_fn() or {}
            state = st.get("state")
            if state in self.UPDATE_BUSY:
                step = self.UPDATE_BUSY[state]
                if step != last:
                    self._show_update_step(step)
                    last = step
            elif state in ("rolled-back", "failed", "not-installed") or (state == "done" and finishing):
                self._show_update_result(st, then)
                return
            time.sleep(2)
        if self.notice == "update":  # gave up waiting: back to the photos
            self.notice = None
            (then or self.restore)()

    def _show_update_result(self, st: dict, then=None) -> None:
        from . import __version__

        ok = st.get("state") == "done"
        self._mark_update_announced(st)
        if ok:
            card = screens.update_card(self.logical, f"Updated to version {__version__}",
                                       "Back to your photos in a moment.", step=None, note="")
        else:
            card = screens.update_card(self.logical, "The update didn't work",
                                       f"{st.get('message', '')} Your photos and settings are fine.", step=None,
                                       note="Details are on the Maintenance tab.", note_colour=screens.SOFT)
        with self._rotate_lock:
            self._show_card(card, "update")

        def back():
            time.sleep(8 if ok else 20)
            if self.notice == "update":
                self.notice = None
                (then or self.restore)()

        threading.Thread(target=back, name="update-done", daemon=True).start()

    def _mark_update_announced(self, st: dict) -> None:
        try:
            (self.data_dir / "update" / "announced").write_text(str(st.get("t")))
        except OSError:
            pass

    def _update_to_announce(self) -> tuple[str, dict] | None:
        """At start-up: an update that just finished (or is still being checked)."""
        try:
            st = self._update_status()
        except Exception:  # noqa: BLE001
            return None
        t = st.get("t")
        if not t or time.time() - t > 20 * 60:
            return None
        try:
            if (self.data_dir / "update" / "announced").read_text().strip() == str(t):
                return None
        except OSError:
            pass
        if st.get("state") in ("installing", "checking"):
            return "busy", st
        if st.get("state") in ("done", "rolled-back", "failed"):
            return "result", st
        return None

    def startup(self) -> None:
        """At boot: say if an update just finished, show the welcome screen
        for a while, then the photos."""
        ann = self._update_to_announce()
        if ann:
            kind, st = ann
            if kind == "busy":  # this is the new version, being checked by the updater
                from . import __version__

                self._update_version = __version__
                self._show_update_step(4)
                threading.Thread(target=self._watch_update, args=(self._update_status, True, self._normal_startup, 4),
                                 name="update-screen", daemon=True).start()
            else:
                self._show_update_result(st, self._normal_startup)
            return
        self._normal_startup()

    def _normal_startup(self) -> None:
        if (Path(self.data_dir) / ".first-run").exists():
            # a new frame: no welcome countdown; the setup hotspot (or, on a cable,
            # the welcome QR for the page) follows within seconds and stays
            # until the wizard is done
            with self._rotate_lock:
                self._show_card(screens.message_card(self.logical, "Welcome to Pi Frame",
                                                     "Getting ready to set up…"), "welcome")
            return
        secs = self.cfg.get("welcome_seconds")
        if secs <= 0:
            self.restore()
            return
        with self._rotate_lock:
            self._show_welcome()

        def finish():
            time.sleep(secs)
            if self.notice == "welcome":  # not replaced by Wi-Fi setup or a manual Next
                self.restore()

        threading.Thread(target=finish, name="welcome", daemon=True).start()

    def show_resetting(self) -> None:
        with self._rotate_lock:
            self.player.stop()
            self._power(True)
            self._show_card(screens.message_card(self.logical, "Resetting Pi Frame",
                                                 "Erasing photos and settings. It starts again as new in a minute."),
                            "reset")

    def after_setup(self) -> None:
        """The welcome wizard is done: once online, show the page's QR code for
        a minute (so the phone can find the frame on the home Wi-Fi), then the photos."""

        def work():
            end = time.time() + 180
            while self.wifi is not None and self.wifi.status.mode != "online" and time.time() < end:
                time.sleep(2)
            with self._rotate_lock:
                if self.notice in ("welcome", "wifi"):
                    self._show_welcome()
            time.sleep(max(60, self.cfg.get("welcome_seconds")))
            if self.notice == "welcome":
                self.restore()

        threading.Thread(target=work, name="after-setup", daemon=True).start()

    def _show_welcome(self) -> None:
        wifi = self.wifi.status if self.wifi else None
        if wifi is None or wifi.mode == "disabled":
            footer = ""
        elif wifi.mode == "online":
            footer = f"Connected to {wifi.ssid}."
        else:
            footer = "Connecting to Wi-Fi…"
        card = screens.welcome_card(self.logical, self.cfg.get("device_name"), frame_urls(), footer)
        self._show_card(card, "welcome")

    def restore(self) -> None:
        """At boot, put the last photo back up straight away."""
        cur = self.state.get("current") or {}
        path = cur.get("path")
        hist = self.state.get("history") or []
        if not path and hist:
            path = hist[-1].get("path")
        if path and Path(path).exists():
            try:
                with self._rotate_lock:
                    img = render.open_image(Path(path), self.logical)
                    self.state["history"] = hist[:-1] if hist and hist[-1].get("path") == path else hist
                    self._present([img], cur or {"source": "storage"}, Path(path))
                return
            except Exception as e:  # noqa: BLE001
                log.warning("could not restore last image: %s", e)
        threading.Thread(target=self.rotate, daemon=True).start()

    # ---------- video ----------
    def _video_rotation(self) -> int:
        rot = self.cfg.get("display_rotation_deg")
        lw, lh = self.logical
        if (lw > lh) != (self.panel[0] > self.panel[1]) and lw != lh:
            rot += 90
        return rot % 360

    def play_video(self, path: Path) -> None:
        """Play a video now (web page 'Play on frame')."""
        if self.player.playing:
            self.player.stop()
            deadline = time.time() + 6
            while self.player.playing and time.time() < deadline:
                time.sleep(0.1)
        with self._rotate_lock:
            self._start_video(path)

    def _start_video(self, path: Path) -> None:
        """Show the poster, play the video in the background, then move on to
        the next photo. Call with _rotate_lock held."""
        label = f"{path.parent.name}/{path.name}"
        meta = self.storage.video_meta(path)
        poster = self.storage.poster_path(path)
        if not poster.exists():
            try:
                self.storage.ensure_thumbnail(path)
            except Exception as e:  # noqa: BLE001
                log.warning("no poster for %s: %s", path, e)
        poster_img = render.open_image(poster, self.logical) if poster.exists() else None
        sur = None
        try:
            sur = self._surround_for(path, poster_img)
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't arrange photos around %s: %s", path.name, e)
        if sur is not None:
            self._present([sur["preview"]], {"source": "video", "filepath": label}, None)
        elif poster_img is not None:
            self._present([poster_img], {"source": "video", "filepath": label}, None)
        self.player.playing = path  # mark busy before the thread starts
        c = self.cfg.snapshot()

        def run():
            gain = 0.0
            if c["video_normalize"] and c["video_sound"]:
                m = self.storage.video_meta(path)
                if "lufs" not in m:  # e.g. copied in by hand: measure now (takes seconds)
                    try:
                        from .video import analyse_sound

                        m = analyse_sound(path, self.storage)
                    except Exception as e:  # noqa: BLE001
                        log.warning("couldn't measure sound of %s: %s", path.name, e)
                gain = float(m.get("gain_db", 0.0))
            self._release_display()  # mpv needs the display hardware
            try:
                result = self.player.play(
                    path,
                    rotate=self._video_rotation(),
                    cover=self.cfg.processing.get("scaleMode") == "cover",
                    sound=c["video_sound"],
                    volume=c["video_volume"],
                    max_seconds=c["video_max_seconds"],
                    duration=meta.get("duration", 0),
                    boost_db=c["video_boost_db"],
                    gain_db=gain,
                    surround=sur,
                )
            finally:
                self._reacquire_display()  # take the screen back, however the video ended
                if sur:
                    Path(sur["file"]).unlink(missing_ok=True)
            log.info("video %s: %s", label, result)
            if self.display.screen_on and self.notice is None:
                self._after_video()

        threading.Thread(target=run, name="video", daemon=True).start()

    def _surround_for(self, path: Path, poster: Image.Image | None) -> dict | None:
        """Photos for the empty space around this video, if it leaves enough."""
        from . import surround

        c = self.cfg.snapshot()
        if not c.get("video_surround") or self.cfg.processing.get("scaleMode") == "cover":
            return None
        meta = self.storage.video_meta(path)
        w, h = meta.get("width"), meta.get("height")
        if not (w and h):
            return None
        window, spaces = surround.plan(self.logical, (int(w), int(h)))
        if not spaces:
            return None
        pool = [p for p in self.storage.rotation_pool(videos=False) if not is_video(p)]
        random.shuffle(pool)
        candidates = []
        for p in pool[:16]:
            try:
                with Image.open(p) as im:
                    candidates.append((p, im.width / im.height))
            except Exception:  # noqa: BLE001
                continue
        chosen = surround.pick(spaces, candidates)
        if len(chosen) < len(spaces):
            return None  # not enough photos
        photos = [render.open_image(p, self.logical) for p in chosen]
        rgba = surround.compose(self.logical, window, spaces, photos)
        rot = self.cfg.get("display_rotation_deg")
        panel_rgba = render.to_panel(rgba, self.panel, rot)
        # where the window lands on the screen as mpv sees it (it may be turned on its side)
        mask = Image.new("L", self.logical, 0)
        mask.paste(255, window)
        window_panel = render.to_panel(mask, self.panel, rot).getbbox()
        tmp = self.data_dir / "tmp"
        tmp.mkdir(exist_ok=True)
        raw = tmp / "surround.bgra"
        raw.write_bytes(panel_rgba.tobytes("raw", "BGRa"))  # premultiplied BGRA, as mpv's overlays want
        preview = rgba.copy()
        if poster is not None:  # the opening frame in the window, for a seamless start
            preview.paste(poster.resize((window[2] - window[0], window[3] - window[1]), Image.LANCZOS), window[:2])
        log.info("photos around %s: %s", path.name, ", ".join(p.name for p in chosen))
        return {"file": raw, "size": panel_rgba.size, "margins": surround.margins(window_panel, self.panel),
                "socket": str(tmp / "mpv.sock"), "preview": preview.convert("RGB")}

    def _after_video(self) -> None:
        with self._rotate_lock:
            if self.player.playing:
                return
            first = self.storage.pick(self.cfg.get("sd_rotation_mode"), set(self._recent), videos=False)
            if first is None:
                return  # only videos: leave the poster up
            self._recent.append(first)
            try:
                img = render.open_image(first, self.logical)
                self._present([img], {"source": "storage", "filepath": f"{first.parent.name}/{first.name}"}, first)
            except Exception as e:  # noqa: BLE001
                log.warning("could not show photo after video: %s", e)

    def stop_video(self) -> None:
        self.player.stop()

    def current_jpeg(self, size: str = "large") -> bytes | None:
        """What's on screen as a JPEG: 'large' (up to 1920 px) or 'full'.
        Encoded once per change, however many web pages are watching."""
        img = self.current_full
        if img is None:
            return None
        key = (self.thumb_version, size)
        if self._full_cache and self._full_cache[0] == key:
            return self._full_cache[1]
        import io

        if size != "full" and max(img.size) > 1920:
            img = img.copy()
            img.thumbnail((1920, 1920), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88)
        self._full_cache = (key, buf.getvalue())
        return self._full_cache[1]

    def _dim(self, panel_img):
        """Darken the picture when the night level is on and the monitor can't
        dim its own backlight (no DDC/CI)."""
        level = self.monitor.software_level if self.monitor else 1.0
        if level >= 0.999:
            return panel_img
        return panel_img.point(lambda v, k=level: int(v * k))

    def _release_display(self) -> None:
        rel = getattr(self.display, "release", None)
        if rel:
            rel()

    def _reacquire_display(self) -> None:
        re_ = getattr(self.display, "reacquire", None)
        if re_:
            re_()

    # ---------- screen plugged in / unplugged ----------
    def screen_watch_step(self, connected: bool | None) -> bool:
        """True when the frame should restart to use a screen that just appeared."""
        prev = getattr(self, "_screen_was", None)
        self._screen_was = connected
        if connected is None:
            return False
        if connected and isinstance(self.display, HeadlessDisplay):
            log.warning("a screen was connected; restarting the frame to use it")
            return True
        if prev is False and connected:
            log.warning("the screen was reconnected; restarting the frame to set it up again")
            return True
        if prev and connected is False:
            log.warning("the screen was disconnected")
        return False

    def watch_screen(self) -> None:
        from .display import screen_connected

        while not self._stop.wait(5):
            if self.screen_watch_step(screen_connected()):
                time.sleep(0.5)
                os._exit(3)  # systemd starts the frame again, now with the screen

    # ---------- power ----------
    def show_power_card(self, restarting: bool) -> None:
        """'Shutting down…' / 'Restarting…' while the Pi goes down. With the boot
        screen set up, no console text covers it."""
        try:
            from . import screens

            self.player.stop()
            self._power(True)
            if restarting:
                card = screens.message_card(self.logical, "Restarting…", "Pi Frame will be back in about a minute.")
            else:
                card = screens.message_card(self.logical, "Shutting down…",
                                            "Wait for the Pi's green light to stop flashing before unplugging.")
            with self._rotate_lock:
                self._show_card(card, "power")
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't show the %s screen: %s", "restart" if restarting else "shutdown", e)

    # ---------- on-screen info ----------
    def refresh_overlay(self) -> bool:
        """Redraw the clock/weather/photo details over the current photo, with
        no fade. Skipped while a card or video is on screen or the screen is off."""
        if self._base is None or self.notice is not None or self.player.playing or not self.display.screen_on:
            return False
        if not self._rotate_lock.acquire(timeout=2):
            return False  # a photo change is in progress; it draws the overlay itself
        try:
            if self.notice is not None or self.player.playing:
                return False
            shown = self._decorate(self.overlay.apply(self._base, self._base_details), self._base_caption)
            self.display.show(self._dim(render.to_panel(shown, self.panel, self.cfg.get("display_rotation_deg"))), 0)
            self.current_thumb = render.thumbnail_jpeg(shown)
            self.current_full = shown
            self.thumb_version += 1
            return True
        finally:
            self._rotate_lock.release()

    def run_overlay_clock(self) -> None:
        """Redraw on each new minute while the time/date is shown, and whenever
        new weather arrives."""
        last_weather = None
        while not self._stop.is_set():
            now = time.time()
            self._stop.wait(60 - (now % 60) + 0.2)  # just after the minute changes
            c = self.cfg.snapshot()
            wx = self.overlay.weather.current()
            new_weather = bool(wx) and wx.get("fetched") != last_weather
            if c["overlay_clock"] or c["overlay_date"] or new_weather:
                if self.refresh_overlay() and wx:
                    last_weather = wx.get("fetched")

    # ---------- information cards ----------
    def _show_card(self, card, kind: str, fade: int = 0) -> None:
        """Show a QR/info card. Doesn't touch history, so the photo comes back after."""
        panel_img = render.to_panel(card, self.panel, self.cfg.get("display_rotation_deg"))
        self.display.show(self._dim(panel_img), fade)
        self._shown += 1
        self.notice = kind
        self.current_thumb = render.thumbnail_jpeg(card)
        self.current_full = card
        self.thumb_version += 1
        self.last_rotation = time.time()

    def on_wifi_change(self, status) -> None:
        """Called by WifiManager whenever its mode changes."""
        with self._rotate_lock:
            if status.mode == "setup" and self.away:
                # still away (the hotspot restarted after a try of the saved Wi-Fi): keep the photos up
                threading.Thread(target=self.refresh_overlay, daemon=True).start()
                return
            if status.mode == "connecting":
                self.away = self.wifi.away = False
                self.overlay.offline = False
            if status.mode == "setup":
                self.player.stop()
                self._power(True)  # the QR codes must be visible, even at night
                self._show_card(
                    screens.wifi_setup_card(
                        self.logical, status.hotspot_ssid, status.hotspot_password,
                        f"http://{HOTSPOT_IP}/setup", status.message,
                        new_frame=(Path(self.data_dir) / ".first-run").exists(),
                    ),
                    "wifi",
                )
                if self.can_go_away():
                    self._away_soon()
            elif status.mode == "connecting":
                self._show_card(
                    screens.message_card(self.logical, status.message, "This takes up to a minute."), "wifi"
                )
            elif status.mode == "online" and self.away:  # back on Wi-Fi: the reminder goes
                self.away = self.wifi.away = False
                self.overlay.offline = False
                log.info("back on Wi-Fi (%s)", status.ssid)
                threading.Thread(target=self.refresh_overlay, daemon=True).start()
            elif status.mode == "online" and self.notice == "welcome":
                self._show_welcome()  # now it can show the IP address and network name
            elif status.mode == "online" and self.notice is not None and self.notice not in ("update", "power"):
                self.notice = None
                threading.Thread(target=self._resume, daemon=True).start()

    def _resume(self) -> None:
        """Back online: put the last photo up again (or the no-photos card)."""
        cur = (self.state.get("current") or {}).get("path")
        if cur and Path(cur).exists():
            try:
                self.show_path(Path(cur), (self.state.get("current") or {}).get("filepath"))
                return
            except Exception as e:  # noqa: BLE001
                log.warning("could not re-show photo: %s", e)
        self.rotate()

    def photos_added(self) -> None:
        showing_demo = (self.state.get("current") or {}).get("source") == "demo"
        if self.notice == "empty" or (showing_demo and not self.demo_active()):
            threading.Thread(target=self.rotate, daemon=True).start()

    # ---------- screen power ----------
    def screen_should_be_on(self, now: float | None = None) -> bool:
        c = self.cfg.snapshot()
        if not c["screen_schedule_enabled"]:
            return True
        tm = time.localtime(now or time.time())
        minute = tm.tm_hour * 60 + tm.tm_min
        on = int(c["screen_on_time"][:2]) * 60 + int(c["screen_on_time"][3:])
        off = int(c["screen_off_time"][:2]) * 60 + int(c["screen_off_time"][3:])
        if on == off:
            return True
        if on < off:
            return on <= minute < off
        return minute >= on or minute < off  # window crosses midnight

    def _power(self, on: bool) -> None:
        """Screen on/off, plus the monitor's own standby when that's enabled.
        The picture signal is switched either way, so the screen still turns
        off if the monitor doesn't answer."""
        was = self.display.screen_on
        if on and not was:
            self.monitor.standby(True)  # wake the monitor first
        self.display.set_power(on)
        if not on and was:
            threading.Thread(target=self.monitor.standby, args=(False,), daemon=True).start()

    def set_screen(self, on: bool) -> None:
        # A manual toggle holds until the schedule next changes state, because
        # the scheduler only acts on schedule transitions.
        if not on:
            self.player.stop()
        self._power(on)
        self.presence.manual(on)  # a hand-made choice: the sensor respects it
        self._notify_ha("update")

    # ---------- scheduler ----------
    def poke(self) -> None:
        """Config changed: recompute schedule now."""
        self._reschedule = True
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run_scheduler(self) -> None:
        last_scheduled = self.screen_should_be_on()
        self._power(last_scheduled)
        while not self._stop.is_set():
            now = time.time()
            # Screen schedule: act only on transitions so manual toggles stick.
            want = self.screen_should_be_on(now)
            if want != last_scheduled and self.notice != "wifi":
                last_scheduled = want
                log.info("screen schedule: turning screen %s", "on" if want else "off")
                if not want:
                    self.player.stop()
                self._power(want)
                if want and self.cfg.get("auto_rotate"):
                    threading.Thread(target=self.rotate, daemon=True).start()

            # Video moments: their own timer, never while a video plays
            every = self.cfg.get("clip_every_minutes")
            if not every:
                self._next_clip = None
            elif self._next_clip is None:
                self._next_clip = now + every * 60
            elif now >= self._next_clip:
                ok, msg = self.play_clip()
                self._next_clip = now + (every * 60 if ok else 120)
                log.info("video moment: %s", msg)

            # Demo mode: its own quicker timer
            if self.demo_active() and self.notice not in ("wifi", "update", "power", "reset", "welcome"):
                if self._demo_due is None:
                    self._demo_due = now + 5
                self.next_rotation = self._demo_due
                if now >= self._demo_due - 0.5:
                    # due again shortly whatever happens; a step that runs sets its own time
                    self._demo_due = now + (30 if self.player.playing or not self.display.screen_on else demo.PHOTO_SECONDS)
                    if not (self.player.playing or not self.display.screen_on):
                        try:
                            self.rotate()
                        except Exception:  # noqa: BLE001
                            log.exception("demo step")
                    continue
                self._wake.wait(min(30.0, max(0.2, self._demo_due - now)))
                self._wake.clear()
                continue
            self._demo_due = None

            # Story / spotlight styles: each step sets its own timer
            if self.story_style() and self.cfg.get("auto_rotate"):
                if self._step_due is None:
                    self._step_due = now + 30  # start the first collage shortly after start-up
                self.next_rotation = self._step_due
                if now >= self._step_due - 0.5:
                    if self.player.playing or not self.display.screen_on:
                        self._step_due = now + 30
                    else:
                        ok, msg = self.rotate()
                        log.info("slideshow step: %s", msg)
                        if not ok:
                            self._step_due = now + 60
                    continue
                timeout = min(30.0, max(0.2, self._step_due - now))
                if self._next_clip:
                    timeout = min(timeout, max(0.2, self._next_clip - now))
                self._wake.wait(timeout)
                self._wake.clear()
                continue

            # Rotation schedule. The due time is kept until it has fired: it must
            # not be recomputed from "now" on each pass, or waking up a moment
            # after the minute boundary would skip straight to the next one.
            if self._reschedule:
                self._reschedule = False
                self.next_rotation = None
            if not self.cfg.get("auto_rotate"):
                self.next_rotation = None
            elif self.next_rotation is None or self.next_rotation > now + 8 * 86400:
                try:
                    self.next_rotation = next_fire(parse_rules(self.cfg.get("rotate_cron")), now)
                except CronError as e:
                    log.error("bad rotate_cron: %s", e)
            elif now - self.next_rotation > 3600:
                self.next_rotation = None  # clock jumped (e.g. NTP sync at boot): start afresh
                continue

            if self.next_rotation is not None and now >= self.next_rotation - 0.5:
                due = self.next_rotation
                if self.player.playing:
                    log.info("scheduled rotation skipped: a video is playing")
                elif self.display.screen_on:
                    ok, msg = self.rotate()
                    log.info("scheduled rotation: %s", msg)
                self.next_rotation = None
                self._reschedule = False
                # Schedule from the boundary that just fired, so a slow rotation
                # can't make us miss or repeat one.
                try:
                    self.next_rotation = next_fire(parse_rules(self.cfg.get("rotate_cron")), max(due, time.time() - 1))
                except CronError:
                    pass
                continue

            timeout = 30.0
            if self.next_rotation is not None:
                timeout = min(timeout, max(0.2, self.next_rotation - now))
            if self._next_clip:
                timeout = min(timeout, max(0.2, self._next_clip - now))
            self._wake.wait(timeout)
            self._wake.clear()

    # ---------- Home Assistant ----------
    def _notify_ha(self, state: str) -> None:
        ha = self.cfg.get("ha_url").rstrip("/")
        if not ha:
            return
        body = {"device_name": self.cfg.get("device_name"), "device_id": self.device_id, "state": state}

        def send():
            try:
                requests.post(f"{ha}/api/esp32_photoframe/notify", json=body, timeout=5)
            except requests.RequestException as e:
                log.debug("HA notify failed: %s", e)

        threading.Thread(target=send, daemon=True).start()

    def notify_online(self) -> None:
        self._notify_ha("online")
