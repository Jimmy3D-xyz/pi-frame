"""Local albums on the SD card: <data>/albums/<album>/<photo>.jpg plus a
.thumbs/ folder per album. Mirrors the firmware's album API."""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import threading
import time
from pathlib import Path

from PIL import Image

from . import render
from .video import VIDEO_EXTS, is_video

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif"}
_NAME_RE = re.compile(r"^[\w\- .()&']{1,64}$")


class StorageError(ValueError):
    pass


def _check_name(name: str, what: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name) or name.startswith(".") or ".." in name:
        raise StorageError(f"invalid {what} name")
    return name


def _safe_filename(name: str) -> str:
    stem = re.sub(r"[^\w\-]+", "_", Path(name).stem).strip("_")[:48] or "photo"
    return stem


class Storage:
    DEFAULT_ALBUM = "Default"
    DOWNLOADS_ALBUM = "Downloads"

    def __init__(self, data_dir: Path, panel: tuple[int, int]):
        self.root = data_dir / "albums"
        self.root.mkdir(parents=True, exist_ok=True)
        self.panel = panel
        self._state_path = data_dir / "albums.json"
        self._lock = threading.RLock()
        self._disabled: set[str] = set()
        self._seq_index = 0
        if self._state_path.exists():
            try:
                self._disabled = set(json.loads(self._state_path.read_text()).get("disabled", []))
            except (OSError, json.JSONDecodeError):
                pass
        (self.root / self.DEFAULT_ALBUM).mkdir(exist_ok=True)

    def _save_state(self) -> None:
        self._state_path.write_text(json.dumps({"disabled": sorted(self._disabled)}))

    # ---------- albums ----------
    def albums(self) -> list[dict]:
        with self._lock:
            names = sorted(p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith("."))
            return [{"name": n, "enabled": n not in self._disabled} for n in names]

    def create_album(self, name: str) -> None:
        name = _check_name(name, "album")
        (self.root / name).mkdir(exist_ok=True)

    def delete_album(self, name: str) -> None:
        name = _check_name(name, "album")
        path = self.root / name
        if not path.is_dir():
            raise StorageError("album not found")
        shutil.rmtree(path)
        with self._lock:
            self._disabled.discard(name)
            self._save_state()
        if name == self.DEFAULT_ALBUM:
            path.mkdir()

    def set_enabled(self, name: str, enabled: bool) -> None:
        name = _check_name(name, "album")
        if not (self.root / name).is_dir():
            raise StorageError("album not found")
        with self._lock:
            (self._disabled.discard if enabled else self._disabled.add)(name)
            self._save_state()

    # ---------- images ----------
    def _photos(self, album: str) -> list[Path]:
        d = self.root / album
        if not d.is_dir():
            return []
        return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)

    def _media(self, album: str) -> list[Path]:
        d = self.root / album
        if not d.is_dir():
            return []
        exts = IMAGE_EXTS | VIDEO_EXTS
        return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in exts)

    def images(self, album: str) -> list[dict]:
        """Photos and videos in an album (firmware name kept for compatibility)."""
        album = _check_name(album, "album")
        out = []
        for p in self._media(album):
            item = {"filename": p.name, "album": album, "thumbnail": f"{p.stem}.jpg", "type": "photo"}
            if is_video(p):
                meta = self.video_meta(p)
                from .video import describe_sound

                item.update(type="video", duration=round(meta.get("duration", 0), 1), sound=describe_sound(meta))
            out.append(item)
        return out

    # ---------- video helpers ----------
    def poster_path(self, video: Path) -> Path:
        d = video.parent / ".posters"
        d.mkdir(exist_ok=True)
        return d / f"{video.stem}.jpg"

    def _meta_path(self, video: Path) -> Path:
        return video.parent / ".thumbs" / f"{video.stem}.json"

    def write_video_meta(self, video: Path, probe) -> None:
        self.update_video_meta(video, {"duration": probe.duration, "width": probe.width, "height": probe.height,
                                       "has_audio": probe.has_audio})

    def update_video_meta(self, video: Path, meta: dict) -> None:
        path = self._meta_path(video)
        path.parent.mkdir(exist_ok=True)
        merged = {**self.video_meta(video), **meta}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged))
        os.replace(tmp, path)

    def video_meta(self, video: Path) -> dict:
        try:
            return json.loads(self._meta_path(video).read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def unique_stem(self, album: str, filename: str) -> str:
        """A file stem not used by any photo or video in the album (they share
        thumbnail names)."""
        base = _safe_filename(filename)
        used = {p.stem for p in self._media(album)}
        stem, n = base, 1
        while stem in used:
            stem = f"{base}_{n}"
            n += 1
        return stem

    def incoming_path(self, album: str, filename: str) -> Path:
        """Where a video upload is streamed to before its job processes it."""
        album = _check_name(album or self.DEFAULT_ALBUM, "album")
        d = self.root / album / ".incoming"
        d.mkdir(parents=True, exist_ok=True)
        ext = Path(filename).suffix.lower()
        if ext not in VIDEO_EXTS:
            raise StorageError("unsupported video type")
        return d / f"{time.time_ns():x}__{_safe_filename(filename)}{ext}"

    def resolve(self, filepath: str) -> Path:
        """'Album/photo.jpg' -> absolute path, rejecting traversal."""
        parts = (filepath or "").strip("/").split("/")
        if len(parts) != 2:
            raise StorageError("filepath must be 'album/filename'")
        album = _check_name(parts[0], "album")
        fname = parts[1]
        if "/" in fname or fname.startswith(".") or ".." in fname:
            raise StorageError("invalid filename")
        path = self.root / album / fname
        if not path.is_file():
            raise StorageError("image not found")
        return path

    def is_video(self, path: Path) -> bool:
        return is_video(path)

    def thumbnail_path(self, photo: Path) -> Path:
        return photo.parent / ".thumbs" / f"{photo.stem}.jpg"

    def ensure_thumbnail(self, photo: Path) -> Path:
        thumb = self.thumbnail_path(photo)
        if not thumb.exists():
            thumb.parent.mkdir(exist_ok=True)
            if is_video(photo):
                from .video import make_poster, probe

                p = probe(photo)
                self.write_video_meta(photo, p)
                make_poster(photo, self.poster_path(photo), thumb, p.duration, self.panel)
            else:
                thumb.write_bytes(render.thumbnail_jpeg(render.open_image(photo, (400, 400))))
        return thumb

    def add(self, album: str, filename: str, data: bytes | Image.Image, stem: str | None = None) -> Path:
        album = _check_name(album or self.DEFAULT_ALBUM, "album")
        (self.root / album).mkdir(exist_ok=True)
        img = data if isinstance(data, Image.Image) else render.open_image(data)
        exif = render.keep_exif(img)  # when/where it was taken, for the on-screen photo details
        img = render.store_copy(img, self.panel)
        path = self.root / album / f"{stem or self.unique_stem(album, filename)}.jpg"
        tmp = path.with_suffix(".tmp")
        img.save(tmp, "JPEG", quality=92, exif=exif)
        os.replace(tmp, path)
        thumb = self.thumbnail_path(path)
        thumb.parent.mkdir(exist_ok=True)
        thumb.write_bytes(render.thumbnail_jpeg(img))
        return path

    def delete_stem(self, album: str, stem: str) -> int:
        """Remove every photo/video in an album with this file stem, with its
        thumbnail, poster and details. Returns how many files were removed."""
        album = _check_name(album, "album")
        n = 0
        for p in self._media(album):
            if p.stem == stem:
                p.unlink(missing_ok=True)
                n += 1
        for extra in (self.root / album / ".thumbs" / f"{stem}.jpg", self.root / album / ".thumbs" / f"{stem}.json",
                      self.root / album / ".posters" / f"{stem}.jpg"):
            extra.unlink(missing_ok=True)
        return n

    def delete(self, filepath: str) -> None:
        path = self.resolve(filepath)
        path.unlink()
        self.thumbnail_path(path).unlink(missing_ok=True)
        if is_video(path):
            self._meta_path(path).unlink(missing_ok=True)
            (path.parent / ".posters" / f"{path.stem}.jpg").unlink(missing_ok=True)

    # ---------- rotation ----------
    def rotation_pool(self, videos: bool = False) -> list[Path]:
        pool: list[Path] = []
        for a in self.albums():
            if a["enabled"]:
                pool.extend(self._media(a["name"]) if videos else self._photos(a["name"]))
        return pool

    def pick(self, mode: str, exclude: set[Path] | None = None, videos: bool = False) -> Path | None:
        pool = self.rotation_pool(videos)
        if not pool:
            return None
        if mode == "sequential":
            with self._lock:
                self._seq_index %= len(pool)
                choice = pool[self._seq_index]
                self._seq_index += 1
            return choice
        candidates = [p for p in pool if not exclude or p not in exclude] or pool
        return random.choice(candidates)

    def usage(self) -> tuple[int, int]:
        du = shutil.disk_usage(self.root)
        return du.total, du.used

    def prune_downloads(self, keep: int = 200) -> None:
        photos = self._photos(self.DOWNLOADS_ALBUM)
        if len(photos) > keep:
            photos.sort(key=lambda p: p.stat().st_mtime)
            for p in photos[: len(photos) - keep]:
                p.unlink(missing_ok=True)
                self.thumbnail_path(p).unlink(missing_ok=True)


def timestamp_name() -> str:
    return time.strftime("download_%Y%m%d_%H%M%S")
