"""Chunked, resumable uploads for big files (videos).

The browser sends the file in pieces (8 MB by default). Each piece is appended
to a single .part file, so every byte is written to the SD card once, with no
temporary copies. Sessions are identified by album + name + size + the file's
modified time, so choosing the same file again after a refresh or a dropped
connection resumes where it stopped.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from .storage import Storage, StorageError, _check_name
from .video import VIDEO_EXTS

MAX_CHUNK = 16 * 1024 * 1024
STALE_AFTER = 7 * 86400  # forget unfinished uploads after a week


class UploadError(ValueError):
    def __init__(self, message: str, code: int = 400, received: int | None = None):
        super().__init__(message)
        self.code = code
        self.received = received


class Uploads:
    def __init__(self, storage: Storage, data_dir: Path):
        self.storage = storage
        self.dir = data_dir / "uploads"
        self.dir.mkdir(exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.cleanup()

    # ---------- helpers ----------
    def _lock(self, uid: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(uid, threading.Lock())

    def _paths(self, uid: str) -> tuple[Path, Path]:
        if not uid.isalnum() or len(uid) != 20:
            raise UploadError("unknown upload", 404)
        return self.dir / f"{uid}.part", self.dir / f"{uid}.json"

    def _meta(self, uid: str) -> dict:
        _, meta = self._paths(uid)
        try:
            return json.loads(meta.read_text())
        except (OSError, json.JSONDecodeError):
            raise UploadError("unknown upload (it may have expired); choose the file again", 404)

    def cleanup(self) -> None:
        now = time.time()
        for meta in self.dir.glob("*.json"):
            part = meta.with_suffix(".part")
            newest = max(meta.stat().st_mtime, part.stat().st_mtime if part.exists() else 0)
            if now - newest > STALE_AFTER:
                part.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)

    # ---------- API ----------
    def start(self, album: str, filename: str, size: int, last_modified: int) -> dict:
        try:
            album = _check_name(album or self.storage.DEFAULT_ALBUM, "album")
        except StorageError as e:
            raise UploadError(str(e)) from e
        if not (self.storage.root / album).is_dir():
            raise UploadError("album not found", 404)
        name = Path(filename or "").name
        if Path(name).suffix.lower() not in VIDEO_EXTS:
            raise UploadError("only videos use resumable upload")
        if not 0 < size <= 8 * 1024**3:
            raise UploadError("file size must be between 1 byte and 8 GB")
        free = self._free_bytes()
        key = f"{album}\0{name}\0{size}\0{last_modified}"
        uid = hashlib.sha1(key.encode()).hexdigest()[:20]
        part, meta = self._paths(uid)
        with self._lock(uid):
            received = part.stat().st_size if part.exists() else 0
            if size - received > free - 200 * 1024 * 1024:  # keep 200 MB spare
                raise UploadError(
                    f"not enough space on the frame: needs {(size - received) / 1e9:.1f} GB, "
                    f"{max(0, free) / 1e9:.1f} GB free", 507)
            if not meta.exists():
                meta.write_text(json.dumps({"album": album, "filename": name, "size": size,
                                            "last_modified": last_modified, "started": time.time()}))
                part.touch()
        return {"id": uid, "received": received, "size": size, "chunk": 8 * 1024 * 1024}

    def append(self, uid: str, offset: int, stream, length: int) -> int:
        meta = self._meta(uid)
        part, _ = self._paths(uid)
        if length <= 0 or length > MAX_CHUNK:
            raise UploadError(f"chunks must be 1 byte to {MAX_CHUNK // 1048576} MB")
        with self._lock(uid):
            received = part.stat().st_size if part.exists() else 0
            if offset != received:
                # e.g. the browser retried a chunk that had in fact arrived
                raise UploadError("offset mismatch", 409, received)
            if received + length > meta["size"]:
                raise UploadError("more data than the file size", 400, received)
            written = 0
            with open(part, "ab") as f:
                while written < length:
                    buf = stream.read(min(1024 * 1024, length - written))
                    if not buf:
                        break
                    f.write(buf)
                    written += len(buf)
            if written != length:
                # connection dropped mid-chunk: roll back so the next try lines up
                with open(part, "r+b") as f:
                    f.truncate(received)
                raise UploadError("chunk incomplete", 400, received)
            return received + written

    def finish(self, uid: str) -> tuple[str, Path]:
        """Move the finished file to the album's incoming folder. Returns
        (album, path) for the video job queue."""
        meta = self._meta(uid)
        part, meta_path = self._paths(uid)
        with self._lock(uid):
            received = part.stat().st_size if part.exists() else 0
            if received != meta["size"]:
                raise UploadError("upload isn't complete yet", 409, received)
            try:
                dest = self.storage.incoming_path(meta["album"], meta["filename"])
            except StorageError as e:
                raise UploadError(str(e)) from e
            os.replace(part, dest)  # same file system: a rename, no copy
            meta_path.unlink(missing_ok=True)
        return meta["album"], dest

    def cancel(self, uid: str) -> None:
        part, meta = self._paths(uid)
        with self._lock(uid):
            part.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)

    def pending(self) -> list[dict]:
        out = []
        for meta_path in sorted(self.dir.glob("*.json")):
            try:
                m = json.loads(meta_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            part = meta_path.with_suffix(".part")
            out.append({"id": meta_path.stem, "album": m["album"], "filename": m["filename"], "size": m["size"],
                        "received": part.stat().st_size if part.exists() else 0,
                        "updated": int(part.stat().st_mtime) if part.exists() else int(m.get("started", 0))})
        return out

    def _free_bytes(self) -> int:
        st = os.statvfs(self.dir)
        return st.f_bavail * st.f_frsize
