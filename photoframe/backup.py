"""Backups: settings only, or settings plus the photos and videos added by hand.

A backup is a .tgz whose top folder is `pi-photoframe/`, laid out like the
frame's data folder (/var/lib/pi-photoframe), so the installer kit (or anyone,
by hand) can restore one with `tar xzf backup.tgz -C /var/lib`.

- Settings: settings, album names, synced-folder sign-ins and links, place
  names, and the frame's identity (for Home Assistant and the image server).
- Full: the same, plus the photos and videos in the albums. Photos from synced
  folders are left out: they download again by themselves.

Archives are made by a separate, low-priority process (`python -m
photoframe.backup create`), so a big backup doesn't hold up the slideshow.
Restoring reads only the files it knows (never links, never anything outside
the data folder), puts photos back first and settings last, and then the frame
restarts to load them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import io
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import zlib
from pathlib import Path

from . import __version__
from .storage import IMAGE_EXTS, StorageError, _check_name
from .video import VIDEO_EXTS

log = logging.getLogger(__name__)

PREFIX = "pi-photoframe"  # the top folder in every backup (the data folder's name on a Pi)
INFO = ".backup-info.json"
FORMAT = 1
KINDS = ("settings", "full")
SETTINGS = ("config.json", "albums.json", "device_id", "places.json", "cloud/rclone.conf", "cloud/state.json")
PRIVATE = {"cloud/rclone.conf"}  # sign-in tokens: readable by the frame only
MANIFEST_RE = re.compile(r"^cloud/manifest-[0-9a-f]{1,40}\.json$")
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS
DOWNLOADS_ALBUM = "Downloads"  # the image server's cache: not worth keeping
MAX_SETTINGS_FILE = 8 * 1024 * 1024
PERIODS = {"daily": 86400, "weekly": 7 * 86400, "monthly": 30 * 86400}
RETRY_AFTER = 6 * 3600  # after a failed automatic backup
SPARE_BYTES = 500 * 1024 * 1024


class BackupError(RuntimeError):
    pass


# ------------------------------------------------------------------ what goes in
def _synced_stems(data_dir: Path) -> dict[str, set[str]]:
    """album -> file stems that came from a synced folder (they download again)."""
    from .cloud import managed_stem

    out: dict[str, set[str]] = {}
    try:
        links = json.loads((data_dir / "cloud" / "state.json").read_text()).get("links", [])
    except (OSError, ValueError):
        return out
    for link in links:
        try:
            manifest = json.loads((data_dir / "cloud" / f"manifest-{link['id']}.json").read_text())
        except (OSError, ValueError, KeyError):
            manifest = {}
        out.setdefault(link.get("album", ""), set()).update(managed_stem(link["id"], p) for p in manifest)
    return out


def _albums(data_dir: Path) -> list[Path]:
    root = data_dir / "albums"
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        try:
            if d.is_dir() and _check_name(d.name, "album") == d.name:
                out.append(d)
        except StorageError:
            pass
    return out


def _sidecars(album: Path, stem: str) -> list[Path]:
    return [album / ".thumbs" / f"{stem}.jpg", album / ".thumbs" / f"{stem}.json", album / ".posters" / f"{stem}.jpg"]


def device_name(data_dir: Path) -> str:
    try:
        raw = json.loads((data_dir / "config.json").read_text())
    except (OSError, ValueError):
        return "Pi Frame"
    cfg = raw.get("config", raw) if isinstance(raw, dict) else {}
    return str(cfg.get("device_name") or "Pi Frame")


def plan(data_dir: Path, kind: str) -> tuple[list[tuple[Path | None, str]], dict]:
    """What a backup of this kind holds: [(file, or None for a folder; path inside the data folder)], info."""
    if kind not in KINDS:
        raise BackupError("unknown kind of backup")
    items: list[tuple[Path | None, str]] = []
    for rel in SETTINGS:
        if (data_dir / rel).is_file():
            items.append((data_dir / rel, rel))
    for p in sorted((data_dir / "cloud").glob("manifest-*.json")):
        rel = f"cloud/{p.name}"
        if MANIFEST_RE.match(rel):
            items.append((p, rel))
    photos = videos = 0
    synced = _synced_stems(data_dir) if kind == "full" else {}
    for album in _albums(data_dir):
        items.append((None, f"albums/{album.name}"))
        if kind != "full" or album.name == DOWNLOADS_ALBUM:
            continue
        skip = synced.get(album.name, set())
        for f in sorted(album.iterdir()):
            if (not f.is_file() or f.name.startswith(".") or f.suffix.lower() not in MEDIA_EXTS
                    or f.stem in skip):
                continue
            items.append((f, f"albums/{album.name}/{f.name}"))
            if f.suffix.lower() in VIDEO_EXTS:
                videos += 1
            else:
                photos += 1
            for side in _sidecars(album, f.stem):
                if side.is_file():
                    items.append((side, side.relative_to(data_dir).as_posix()))
    size = 0
    for src, _ in items:
        if src is not None:
            try:
                size += src.stat().st_size
            except OSError:
                pass
    info = {"format": FORMAT, "kind": kind, "created": time.time(), "version": __version__,
            "hostname": socket.gethostname(), "device_name": device_name(data_dir),
            "photos": photos, "videos": videos, "bytes": size}
    return items, info


def _neutral(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def write_archive(data_dir: Path, kind: str, out) -> dict:
    """Write a backup to the binary stream `out` (never needs to seek)."""
    items, info = plan(data_dir, kind)
    gz = gzip.GzipFile(fileobj=out, mode="wb", compresslevel=6 if kind == "settings" else 1)
    with tarfile.open(fileobj=gz, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        def folder(rel: str, mode: int = 0o755) -> None:
            ti = tarfile.TarInfo(f"{PREFIX}/{rel}".rstrip("/"))
            ti.type, ti.mode, ti.mtime = tarfile.DIRTYPE, mode, int(time.time())
            tar.addfile(_neutral(ti))

        blob = json.dumps(info, indent=1).encode()
        ti = tarfile.TarInfo(f"{PREFIX}/{INFO}")
        ti.size, ti.mode, ti.mtime = len(blob), 0o644, int(info["created"])
        folder("")
        tar.addfile(_neutral(ti), io.BytesIO(blob))
        if any(rel.startswith("cloud/") for _, rel in items):
            folder("cloud", 0o700)
        folder("albums")
        for src, rel in items:
            if src is None:
                folder(rel)
                continue
            try:
                # Open first and describe what was opened: a photo replaced
                # meanwhile can't leave a size that doesn't match its data.
                with open(src, "rb") as f:
                    st = os.fstat(f.fileno())
                    ti = tarfile.TarInfo(f"{PREFIX}/{rel}")
                    ti.size, ti.mtime = st.st_size, int(st.st_mtime)
                    ti.mode = 0o600 if rel in PRIVATE else 0o644
                    tar.addfile(_neutral(ti), f)
            except FileNotFoundError:
                continue  # deleted while the backup was being made
    gz.close()
    return info


# ------------------------------------------------------------------ reading one back
def _classify(rel: str) -> str | None:
    """What a path inside a backup is, or None for anything a restore leaves alone."""
    if rel in SETTINGS or MANIFEST_RE.match(rel):
        return "settings"
    parts = rel.split("/")
    if parts[0] != "albums" or len(parts) < 2:
        return None
    try:
        if _check_name(parts[1], "album") != parts[1]:
            return None
    except StorageError:
        return None
    if len(parts) == 2:
        return "album"
    name = parts[-1]
    if len(parts) == 3:
        if not name.startswith(".") and "/" not in name and Path(name).suffix.lower() in MEDIA_EXTS:
            return "media"
        return None
    if len(parts) == 4 and parts[2] in (".thumbs", ".posters") and not name.startswith("."):
        if Path(name).suffix.lower() in ({".jpg", ".json"} if parts[2] == ".thumbs" else {".jpg"}):
            return "sidecar"
    return None


def _members(tar: tarfile.TarFile):
    """(member, path inside the data folder) for each entry, refusing anything unsafe."""
    for m in tar:
        name = m.name.rstrip("/")
        if name.startswith("./"):
            name = name[2:]
        if name == PREFIX:
            continue
        if not name.startswith(PREFIX + "/"):
            raise BackupError("that file isn't a Pi Frame backup (everything in one should be in a "
                              f"'{PREFIX}' folder)")
        rel = name[len(PREFIX) + 1:]
        if not rel or ".." in rel.split("/") or rel.startswith("/") or "\\" in rel:
            raise BackupError("that backup has a file with an unsafe name in it, so it wasn't used")
        if not (m.isfile() or m.isdir()):
            raise BackupError("that backup has links or special files in it, so it wasn't used")
        yield m, rel


def inspect(path: Path) -> dict:
    """Check a backup file through to the end, and say what's in it."""
    info = None
    photos = videos = 0
    settings = False
    try:
        with tarfile.open(path, "r:*") as tar:
            for m, rel in _members(tar):
                if rel == INFO and m.isfile() and m.size < 65536:
                    try:
                        info = json.loads(tar.extractfile(m).read())
                    except ValueError:
                        info = None
                    continue
                kind = _classify(rel)
                if kind == "settings" and m.isfile():
                    settings = True
                elif kind == "media" and m.isfile():
                    if Path(rel).suffix.lower() in VIDEO_EXTS:
                        videos += 1
                    else:
                        photos += 1
                if m.isfile():
                    # read every file, so a damaged backup is caught now rather than half-way through restoring
                    f = tar.extractfile(m)
                    while f is not None and f.read(1024 * 1024):
                        pass
    except BackupError:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
        raise BackupError("that file isn't a Pi Frame backup, or it's damaged (" + str(e)[:120] + ")") from e
    if not settings and not photos and not videos:
        raise BackupError("there's nothing in that backup that the frame can restore")
    info = info if isinstance(info, dict) else {}
    return {"kind": info.get("kind") if info.get("kind") in KINDS else ("full" if photos or videos else "settings"),
            "made_by_hand": not info, "created": info.get("created"), "version": info.get("version"),
            "hostname": info.get("hostname"), "device_name": info.get("device_name"),
            "settings": settings, "photos": photos, "videos": videos, "bytes": path.stat().st_size}


def _write_atomic(dest: Path, src, mtime: float | None = None, mode: int = 0o644) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.restoring")
    with open(tmp, "wb") as out:
        shutil.copyfileobj(src, out, 1024 * 1024)
    os.chmod(tmp, mode)
    if mtime:
        os.utime(tmp, (mtime, mtime))
    os.replace(tmp, dest)


def restore(data_dir: Path, path: Path, progress=None) -> dict:
    """Put a backup's photos back (keeping any others on the frame), then its
    settings. The frame must restart afterwards to load the settings."""
    settings: dict[str, bytes] = {}
    photos = videos = 0
    try:
        with tarfile.open(path, "r:*") as tar:
            for m, rel in _members(tar):
                kind = _classify(rel)
                if kind is None:
                    continue
                if m.isdir():
                    if kind == "album":
                        (data_dir / rel).mkdir(parents=True, exist_ok=True)
                    continue
                if kind == "settings":
                    if m.size > MAX_SETTINGS_FILE:
                        raise BackupError(f"{rel} in that backup is far too big for a settings file")
                    settings[rel] = tar.extractfile(m).read()
                    continue
                _write_atomic(data_dir / rel, tar.extractfile(m), m.mtime)
                if kind == "media":
                    if Path(rel).suffix.lower() in VIDEO_EXTS:
                        videos += 1
                    else:
                        photos += 1
                    if progress:
                        progress(photos + videos)
    except BackupError:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
        raise BackupError(f"restoring stopped part-way ({e}); your settings weren't changed") from e
    # settings last, all together: the frame restarts straight after
    if any(rel.startswith("cloud/") for rel in settings):
        (data_dir / "cloud").mkdir(exist_ok=True)
        os.chmod(data_dir / "cloud", 0o700)
    for rel, blob in settings.items():
        _write_atomic(data_dir / rel, io.BytesIO(blob), mode=0o600 if rel in PRIVATE else 0o644)
    return {"photos": photos, "videos": videos, "settings": bool(settings)}


# ------------------------------------------------------------------ names
def hostname() -> str:
    return re.sub(r"[^A-Za-z0-9-]", "", socket.gethostname().split(".")[0]) or "piframe"


def backup_name(kind: str, when: float | None = None, label: str = "backup") -> str:
    stamp = dt.datetime.fromtimestamp(when or time.time()).strftime("%Y-%m-%d-%H%M")
    return f"{hostname()}-{label}-{stamp}-{kind}.tgz"


NAME_RE = re.compile(r"^[A-Za-z0-9-]+-(backup|before-restore)-\d{4}-\d{2}-\d{2}-\d{4}-(settings|full)\.tgz$")


def _ours(names: list[str], kind: str, label: str = "backup") -> list[str]:
    """This frame's backups of one kind, oldest first (the names sort by date)."""
    prefix = f"{hostname()}-{label}-"
    return sorted(n for n in names if n.startswith(prefix) and n.endswith(f"-{kind}.tgz") and NAME_RE.match(n))


# ------------------------------------------------------------------ the frame's side
def _creator(data_dir: Path, kind: str) -> list[str]:
    cmd = [sys.executable, "-m", "photoframe.backup", "create", "--data-dir", str(data_dir), "--kind", kind]
    if shutil.which("ionice"):
        cmd = ["ionice", "-c", "3"] + cmd  # read the card only when nothing else wants it
    if shutil.which("nice"):
        cmd = ["nice", "-n", "15"] + cmd
    return cmd


class Backups:
    """Downloads, automatic backups, and restoring, for the web page."""

    def __init__(self, data_dir: Path, cfg, cloud=None, frame=None, restart=None):
        self.data_dir = data_dir
        self.cfg = cfg
        self.cloud = cloud
        self.frame = frame
        self.restart = restart or (lambda: os._exit(0))  # systemd starts the frame again
        self.dir = data_dir / "backups"
        self.dir.mkdir(exist_ok=True)
        os.chmod(self.dir, 0o700)  # backups hold sign-ins
        self.state_path = self.dir / "state.json"
        self.part = self.dir / "restore-upload.part"
        self.part_meta = self.dir / "restore-upload.json"
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._stop = threading.Event()
        self.running: dict | None = None
        self.state: dict = {"last": None, "last_ok_t": None}
        try:
            self.state.update(json.loads(self.state_path.read_text()))
        except (OSError, ValueError):
            pass
        self._est: tuple[float, dict] | None = None
        self.just_restored = self._announce_restored()

    # ---------- bookkeeping
    def _save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        os.replace(tmp, self.state_path)

    def _announce_restored(self) -> bool:
        """Whether a backup was restored just before this start (from the web
        page or the installer kit); says so in the log once."""
        restored = False
        info_file = self.data_dir / INFO
        if info_file.exists():  # left by the installer kit's `tar xzf`
            restored = True
            try:
                info = json.loads(info_file.read_text())
                when = dt.datetime.fromtimestamp(info["created"]).strftime("%-d %B %Y")
                log.info("restored from a %s backup made %s", info.get("kind", ""), when)
            except (OSError, ValueError, KeyError, TypeError):
                pass
            info_file.unlink(missing_ok=True)
        st = self.restore_state()
        if st and st.get("state") == "done" and st.get("fresh"):
            restored = True
            self._restore_status("done", st.get("message", ""), t=st.get("t"))
        return restored

    def restore_state(self) -> dict | None:
        try:
            st = json.loads((self.dir / "restore.json").read_text())
        except (OSError, ValueError):
            return None
        if st.get("state") == "restoring" and time.time() - st.get("t", 0) > 6 * 3600:
            st = {**st, "state": "failed", "message": "The restore was interrupted (the frame restarted part-way)."}
        return st

    def _restore_status(self, state: str, message: str, **extra) -> None:
        tmp = self.dir / "restore.json.tmp"
        tmp.write_text(json.dumps({"state": state, "message": message, "t": extra.pop("t", None) or time.time(),
                                   **extra}))
        os.replace(tmp, self.dir / "restore.json")

    # ---------- sizes, for the web page
    def estimate(self) -> dict:
        if self._est and time.time() - self._est[0] < 60:
            return self._est[1]
        out = {}
        for kind in KINDS:
            _, info = plan(self.data_dir, kind)
            out[kind] = {k: info[k] for k in ("bytes", "photos", "videos")}
        self._est = (time.time(), out)
        return out

    # ---------- making one
    def _spawn(self, kind: str) -> subprocess.Popen:
        return subprocess.Popen(_creator(self.data_dir, kind), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=str(Path(__file__).resolve().parents[1]))

    def stream(self, kind: str):
        """The bytes of a new backup, for a download."""
        if kind not in KINDS:
            raise BackupError("unknown kind of backup")
        proc = self._spawn(kind)

        def gen():
            ok = False
            try:
                while True:
                    buf = proc.stdout.read(256 * 1024)
                    if not buf:
                        break
                    yield buf
                ok = proc.wait() == 0
            finally:
                if proc.poll() is None:
                    proc.kill()  # the browser stopped the download
                    proc.wait()
                if ok:
                    log.info("%s backup downloaded", kind)
                else:
                    err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
                    if err:
                        log.warning("backup download failed: %s", err[-400:])
        return gen()

    def _make_file(self, kind: str, dest: Path) -> None:
        tmp = dest.with_name(f".{dest.name}.part")
        try:
            with open(tmp, "wb") as out:
                proc = subprocess.Popen(_creator(self.data_dir, kind), stdout=out, stderr=subprocess.PIPE,
                                        cwd=str(Path(__file__).resolve().parents[1]))
                _, err = proc.communicate()
            if proc.returncode != 0:
                raise BackupError("couldn't make the backup: " + err.decode(errors="replace").strip()[-300:])
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)

    # ---------- backups kept on the frame
    def local_files(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.tgz"), reverse=True):
            if NAME_RE.match(p.name):
                st = p.stat()
                out.append({"name": p.name, "bytes": st.st_size, "t": st.st_mtime,
                            "kind": "full" if p.name.endswith("-full.tgz") else "settings",
                            "before_restore": "-before-restore-" in p.name})
        return out

    def local_path(self, name: str) -> Path:
        if not NAME_RE.match(name or "") or not (self.dir / name).is_file():
            raise BackupError("backup not found")
        return self.dir / name

    def delete_local(self, name: str) -> None:
        self.local_path(name).unlink()

    def _prune_local(self, kind: str, keep: int, label: str = "backup") -> None:
        names = _ours([p.name for p in self.dir.glob("*.tgz")], kind, label)
        for n in names[: max(0, len(names) - keep)]:
            (self.dir / n).unlink(missing_ok=True)

    # ---------- automatic backups
    def where_label(self, where: str | None = None) -> str:
        where = where or self.cfg.get("backup_where") or "frame"
        if where == "frame":
            return "this frame's SD card"
        acct = self._account(where)
        if not acct:
            return where
        if acct.get("provider") == "smb":
            return acct.get("name") or "your NAS"
        from .cloud import PROVIDERS

        return PROVIDERS.get(acct.get("provider"), {}).get("label", where)

    def _account(self, remote: str) -> dict | None:
        if self.cloud is None:
            return None
        return next((a for a in self.cloud.state.get("accounts", []) if a.get("remote") == remote), None)

    def destinations(self) -> list[dict]:
        out = [{"where": "frame", "label": "This frame's SD card", "ok": True,
                "note": "Protects against mistakes, not against a failing card."}]
        if self.cloud is not None:
            for a in self.cloud.state.get("accounts", []):
                if not self.cloud.remote_connected(a["remote"]):
                    continue
                ok, why = self.cloud.can_back_up(a["remote"])
                out.append({"where": a["remote"], "label": self.where_label(a["remote"]),
                            "account": a.get("name"), "provider": a.get("provider"), "ok": ok, "note": why})
        return out

    def next_due(self, now: float | None = None) -> float | None:
        """When the next automatic backup will run (None when they're off)."""
        c = self.cfg
        if not c.get("backup_auto"):
            return None
        now = now or time.time()
        period = PERIODS.get(c.get("backup_every"), PERIODS["weekly"])
        last = self.state.get("last") or {}
        last_ok = self.state.get("last_ok_t")
        sched = 0.0  # never backed up: don't wait for the night
        if last_ok:
            # the backup time, on the first day a period after the last good one
            # (an hour's slack keeps it from drifting a day later each time)
            hh, mm = (int(x) for x in str(c.get("backup_time") or "03:00").split(":"))
            earliest = last_ok + period - 3600
            at = dt.datetime.fromtimestamp(earliest).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if at.timestamp() < earliest:
                at += dt.timedelta(days=1)
            sched = at.timestamp()
        if last.get("t") and not last.get("ok") and last.get("auto"):
            sched = max(sched, last["t"] + RETRY_AFTER)  # after a failure, try again in a few hours
        return max(sched, 0.0) or now

    def due(self, now: float | None = None) -> bool:
        nxt = self.next_due(now)
        return nxt is not None and (now or time.time()) >= nxt

    def back_up(self, auto: bool = False) -> dict:
        """Make a backup and keep it where the settings say. Returns the result."""
        if not self._run_lock.acquire(blocking=False):
            raise BackupError("a backup is already running")
        c = self.cfg
        kind = c.get("backup_kind") if c.get("backup_kind") in KINDS else "full"
        where = c.get("backup_where") or "frame"
        keep = max(1, int(c.get("backup_keep") or 4))
        name = backup_name(kind)
        started = time.time()
        self.running = {"started": started, "kind": kind, "where": self.where_label(where)}
        try:
            if where == "frame":
                need = plan(self.data_dir, kind)[1]["bytes"] + SPARE_BYTES
                if shutil.disk_usage(self.dir).free < need:
                    raise BackupError("There isn't enough free space on the SD card for this backup. "
                                      "Keep backups on your NAS or a cloud drive instead.")
                self._make_file(kind, self.dir / name)
                size = (self.dir / name).stat().st_size
                self._prune_local(kind, keep)
            else:
                if self.cloud is None or not self.cloud.remote_connected(where):
                    raise BackupError(f"{self.where_label(where)} isn't connected any more (Photo source → "
                                      "Synced folders), so the backup couldn't be saved there.")
                folder = str(c.get("backup_folder") or "Pi Frame backups").strip("/")
                proc = subprocess.Popen(_creator(self.data_dir, kind), stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, cwd=str(Path(__file__).resolve().parents[1]))
                try:
                    size = self.cloud.backup_upload(where, folder, name, proc.stdout)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    rc = proc.wait()
                if rc != 0:
                    err = proc.stderr.read().decode(errors="replace").strip()[-300:]
                    try:
                        self.cloud.backup_delete(where, folder, name)  # don't leave half a backup
                    except Exception:  # noqa: BLE001
                        pass
                    raise BackupError("couldn't make the backup: " + err)
                names = [f["Name"] for f in self.cloud.backup_list(where, folder)]
                for old in _ours(names, kind)[: max(0, len(_ours(names, kind)) - keep)]:
                    self.cloud.backup_delete(where, folder, old)
            secs = time.time() - started
            result = {"t": time.time(), "ok": True, "auto": auto, "kind": kind, "where": self.where_label(where),
                      "name": name, "bytes": size, "secs": round(secs),
                      "message": f"Saved {name} to {self.where_label(where)}"}
            self.state["last_ok_t"] = result["t"]
            log.info("backup: %s (%d MB in %ds)", result["message"], size // 1048576, secs)
        except Exception as e:  # noqa: BLE001
            msg = str(e) if isinstance(e, BackupError) else f"the backup failed ({e})"
            if not isinstance(e, BackupError) and type(e).__name__ == "CloudError":
                msg = str(e)
            result = {"t": time.time(), "ok": False, "auto": auto, "kind": kind, "where": self.where_label(where),
                      "message": msg}
            log.warning("backup to %s failed: %s", self.where_label(where), msg)
        finally:
            self.running = None
            self._run_lock.release()
        self.state["last"] = result
        self._save()
        return result

    def run(self) -> None:
        """Background thread: automatic backups when they're due."""
        while not self._stop.wait(60):
            try:
                if not self.due():
                    continue
                where = self.cfg.get("backup_where") or "frame"
                wifi = getattr(self.frame, "wifi", None)
                if where != "frame" and wifi is not None and getattr(wifi.status, "mode", "online") != "online":
                    continue
                self.back_up(auto=True)
            except BackupError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("automatic backup")

    def stop(self) -> None:
        self._stop.set()

    # ---------- restoring: the upload, in pieces like big videos
    def restore_start(self, name: str, size: int, last_modified: int) -> dict:
        if not 0 < size <= 64 * 1024**3:
            raise BackupError("that file is empty, or far too big")
        key = {"name": Path(name or "").name, "size": int(size), "last_modified": int(last_modified)}
        with self._lock:
            try:
                meta = json.loads(self.part_meta.read_text())
            except (OSError, ValueError):
                meta = {}
            if {k: meta.get(k) for k in key} != key:  # a different file: start again
                self.part.unlink(missing_ok=True)
                meta = {**key, "info": None}
                self.part_meta.write_text(json.dumps(meta))
                self.part.touch()
            received = self.part.stat().st_size if self.part.exists() else 0
            if size - received > shutil.disk_usage(self.dir).free - SPARE_BYTES:
                raise BackupError(f"not enough space on the frame for that backup ({size / 1e9:.1f} GB)")
        out = {"received": received, "size": size, "chunk": 8 * 1024 * 1024}
        if received == size:  # already uploaded (e.g. chosen again after a refresh)
            out["staged"] = self.check_staged()
        return out

    def restore_append(self, offset: int, stream, length: int) -> dict:
        with self._lock:
            try:
                meta = json.loads(self.part_meta.read_text())
            except (OSError, ValueError):
                raise BackupError("choose the backup file again") from None
            received = self.part.stat().st_size if self.part.exists() else 0
            if offset != received:
                return {"received": received, "mismatch": True}
            if length <= 0 or length > 16 * 1024 * 1024 or received + length > meta["size"]:
                raise BackupError("that piece of the upload doesn't fit")
            written = 0
            with open(self.part, "ab") as f:
                while written < length:
                    buf = stream.read(min(1024 * 1024, length - written))
                    if not buf:
                        break
                    f.write(buf)
                    written += len(buf)
            if written != length:
                with open(self.part, "r+b") as f:
                    f.truncate(received)
                raise BackupError("the connection dropped; trying again")
            received += written
        out = {"received": received}
        if received == meta["size"]:
            out["staged"] = self.check_staged()
        return out

    def check_staged(self) -> dict:
        try:
            meta = json.loads(self.part_meta.read_text())
        except (OSError, ValueError):
            raise BackupError("no backup has been uploaded") from None
        if not self.part.exists() or self.part.stat().st_size != meta.get("size"):
            raise BackupError("the backup hasn't finished uploading")
        if not meta.get("info"):
            try:
                meta["info"] = inspect(self.part)
            except BackupError:
                self.restore_discard()
                raise
            self.part_meta.write_text(json.dumps(meta))
        return {**meta["info"], "name": meta["name"]}

    def staged(self) -> dict | None:
        try:
            meta = json.loads(self.part_meta.read_text())
        except (OSError, ValueError):
            return None
        if meta.get("info"):
            return {**meta["info"], "name": meta["name"]}
        received = self.part.stat().st_size if self.part.exists() else 0
        return {"partial": True, "name": meta.get("name"), "size": meta.get("size"), "received": received}

    def restore_discard(self) -> None:
        with self._lock:
            self.part.unlink(missing_ok=True)
            self.part_meta.unlink(missing_ok=True)

    def restore_begin(self, source: Path | None = None) -> dict:
        """Restore the uploaded backup (or one kept on the frame), then restart."""
        if source is None:
            info = self.check_staged()
            source = self.part
        else:
            info = inspect(source)
        st = self.restore_state()
        if st and st.get("state") == "restoring":
            raise BackupError("a restore is already running")
        self._restore_status("restoring", "Restoring: keeping a copy of the current settings first")

        def work():
            try:
                # a way back: today's settings, kept on the frame
                self._make_file("settings", self.dir / backup_name("settings", label="before-restore"))
                self._prune_local("settings", 3, label="before-restore")

                def progress(n):
                    if n % 25 == 0:
                        self._restore_status("restoring", f"Restoring: {n} photos and videos put back so far")

                self._restore_status("restoring", "Restoring photos, videos and settings")
                done = restore(self.data_dir, source, progress)
                bits = []
                if done["photos"]:
                    bits.append(f"{done['photos']} photo{'s' if done['photos'] != 1 else ''}")
                if done["videos"]:
                    bits.append(f"{done['videos']} video{'s' if done['videos'] != 1 else ''}")
                when = (dt.datetime.fromtimestamp(info["created"]).strftime("%-d %B %Y")
                        if info.get("created") else "")
                msg = "Restored the settings" + (" and " + " and ".join(bits) if bits else "") + \
                      (f" from the backup made {when}" if when else "") + "."
                if source == self.part:
                    self.restore_discard()
                self._restore_status("done", msg, fresh=True)
                log.warning("restore: %s Restarting to load them.", msg)
                time.sleep(1)
                self.restart()
            except Exception as e:  # noqa: BLE001
                msg = str(e) if isinstance(e, BackupError) else f"the restore failed ({e})"
                self._restore_status("failed", msg[0].upper() + msg[1:])
                log.error("restore failed: %s", msg)

        threading.Thread(target=work, daemon=True, name="restore").start()
        return info

    # ---------- for the web page
    def status(self) -> dict:
        c = self.cfg
        rs = self.restore_state()
        if rs and rs.get("state") == "done" and time.time() - rs.get("t", 0) > 900:
            rs = None  # said it; no need to keep saying it
        return {
            "auto": bool(c.get("backup_auto")), "where": c.get("backup_where") or "frame",
            "where_label": self.where_label(), "folder": c.get("backup_folder") or "Pi Frame backups",
            "kind": c.get("backup_kind") or "full", "every": c.get("backup_every") or "weekly",
            "time": c.get("backup_time") or "03:00", "keep": int(c.get("backup_keep") or 4),
            "next": self.next_due(), "last": self.state.get("last"), "last_ok_t": self.state.get("last_ok_t"),
            "running": self.running, "local": self.local_files(), "staged": self.staged(), "restore": rs,
            "destinations": self.destinations(),
        }

    def health(self) -> dict | None:
        """A line for the Maintenance summary when automatic backups need attention."""
        if not self.cfg.get("backup_auto"):
            return None
        last = self.state.get("last") or {}
        period = PERIODS.get(self.cfg.get("backup_every"), PERIODS["weekly"])
        if last and not last.get("ok"):
            return {"level": "warn", "text": "The last backup didn't work: " + last.get("message", "")}
        ok_t = self.state.get("last_ok_t")
        if ok_t and time.time() - ok_t > 2 * period + 86400:
            return {"level": "warn", "text": "No backup for " + _ago(time.time() - ok_t)}
        if ok_t:
            return {"level": "ok", "text": "Last backup " + _ago(time.time() - ok_t) + " ago"}
        return None


def _ago(secs: float) -> str:
    if secs < 3600:
        return f"{max(1, int(secs // 60))} min"
    if secs < 2 * 86400:
        return f"{int(secs // 3600)} hours"
    return f"{int(secs // 86400)} days"


# ------------------------------------------------------------------ command line
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m photoframe.backup", description="Pi Frame backups")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="write a backup to standard output (or --out)")
    c.add_argument("--data-dir", default=os.environ.get("PHOTOFRAME_DATA", "/var/lib/pi-photoframe"))
    c.add_argument("--kind", choices=KINDS, default="full")
    c.add_argument("--out")
    i = sub.add_parser("inspect", help="check a backup file and say what's in it")
    i.add_argument("file")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "create":
            if args.out:
                with open(args.out, "wb") as f:
                    write_archive(Path(args.data_dir), args.kind, f)
            else:
                write_archive(Path(args.data_dir), args.kind, sys.stdout.buffer)
                sys.stdout.buffer.flush()
        else:
            print(json.dumps(inspect(Path(args.file)), indent=1))
    except BackupError as e:
        print(f"backup: {e}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 1  # the download was stopped
    return 0


if __name__ == "__main__":
    sys.exit(main())
