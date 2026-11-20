"""The frame controller: decides what to show and when.

Equivalent of the firmware's display_flow / periodic_tasks / power_manager,
minus deep sleep. Instead, an LCD frame gets a screen on/off schedule.
"""

from __future__ import annotations

import json
import os
import logging
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
from . import collage, demo
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
        self.cloud = None  # OneDrive folder sync, set by __main__ (or the web app)
        self.backups = None  # backups and restoring, set by __main__ (or the web app)
        self.notice: str | None = None  # None | "empty" | "wifi" while a card is on screen
        self.wifi = None  # WifiManager, when running on a Pi with NetworkManager

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
                 caption: str | None = None) -> None:
        proc = self.cfg.processing
        frame = render.compose(
            images, self.logical, proc.get("scaleMode", "fit"), proc.get("backgroundColor", "blur")
        )
        # when/where the photo was taken (single stored photos only, not collages or server images)
        details = {}
        if remember is not None and len(images) == 1 and Path(remember).parent != self.cache:
            details = photo_details(Path(remember))
        self._base, self._base_details = frame, details
        self.overlay.fun.next()  # a new joke/fact with each new picture
        shown = self.overlay.apply(frame, details)
        if caption:  # demo mode's samples: "Sample photo" and the credit
            shown = demo.draw_caption(shown, caption, self.cfg.get("overlay_corner"))
        panel_img = render.to_panel(shown, self.panel, self.cfg.get("display_rotation_deg"))
        fade = self.cfg.get("transition_ms") if self.cfg.get("transition") == "fade" else 0
        self.display.show(self._dim(panel_img), fade)
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
            if self.notice == "wifi":
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
        kind, item, secs = self.demo.next()
        if kind == "photo":
            img = render.open_image(item.path, self.logical)
            self._present([img], {"source": "demo", "filepath": f"Sample: {item.title}"}, None, caption=item.caption)
        elif kind == "collage":
            img = collage.compose([render.open_image(s.path, self.logical) for s in item], self.logical)
            self._present([img], {"source": "demo", "filepath": f"Sample collage of {len(item)}"}, None,
                          caption="Collage style · " + ", ".join(s.title.split(",")[0] for s in item[:2]) + "…")
        else:
            title, text, with_qr = item
            backdrop = Image.open(random.choice(self.demo.samples).path)
            self._show_card(demo.info_card(self.logical, title, text, frame_urls()[0] if with_qr else None, backdrop),
                            "demo")
            self.state["current"] = {"source": "demo", "filepath": f"About: {title}", "shown_at": int(time.time())}
        self._demo_due = time.time() + secs
        return True, f"demo: {kind}"

    # ---------- slideshow styles ----------
    def story_style(self) -> str | None:
        style = self.cfg.get("slideshow_style")
        if style in ("story", "spotlight") and self.cfg.get("rotation_mode") == "storage":
            return style
        return None

    def reset_story(self) -> None:
        self._story = None
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
        if kind == "collage":
            imgs = [render.open_image(p, self.logical) for p in st["photos"]]
            img = collage.compose(imgs, self.logical)
            remember = self._cache_image(img)
            self._present([img], {"source": "storage", "filepath": f"Collage of {len(imgs)} photos"}, remember)
            minutes = self.cfg.get("collage_minutes")
        else:
            img = render.open_image(path, self.logical)
            self._present([img], {"source": "storage", "filepath": f"{path.parent.name}/{path.name}"}, path)
            minutes = self.cfg.get("single_minutes")
        self._step_due = time.time() + minutes * 60
        return True, f"{kind} {st['i'] + 1} of {len(st['steps'])}"

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
        if self.story_style():
            return self._story_step()
        return self._rotate_classic()

    def _rotate_classic(self) -> tuple[bool, str]:
        mode = self.cfg.get("sd_rotation_mode")
        first = self.storage.pick(mode, set(self._recent), videos=self.cfg.get("video_enabled"))
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
        if len(images) == 1:
            remember = first
        else:  # remember the composed collage so "previous" can re-show it
            proc = self.cfg.processing
            collage = render.compose(images, self.logical, proc.get("scaleMode"), proc.get("backgroundColor"))
            remember = self._cache_image(collage)
            images = [collage]
        self._present(images, {"source": "storage", "filepath": label}, remember)
        return True, "Image rotation triggered"

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
            shown = self.overlay.apply(self._base, self._base_details)
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
    def _show_card(self, card, kind: str) -> None:
        """Show a QR/info card. Doesn't touch history, so the photo comes back after."""
        panel_img = render.to_panel(card, self.panel, self.cfg.get("display_rotation_deg"))
        self.display.show(self._dim(panel_img), 0)
        self.notice = kind
        self.current_thumb = render.thumbnail_jpeg(card)
        self.current_full = card
        self.thumb_version += 1
        self.last_rotation = time.time()

    def on_wifi_change(self, status) -> None:
        """Called by WifiManager whenever its mode changes."""
        with self._rotate_lock:
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
            elif status.mode == "connecting":
                self._show_card(
                    screens.message_card(self.logical, status.message, "This takes up to a minute."), "wifi"
                )
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
