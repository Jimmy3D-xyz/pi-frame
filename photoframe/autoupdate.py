"""Updates from GitHub: the frame checks the repository's releases itself.

The repository is public, so no sign-in is needed. (Frames from before
0.36.1 may have a GitHub token saved from when it was private: it's deleted
when the frame starts.)

Once a day the frame looks at the latest release (test builds, marked
pre-release, are ignored). If it's newer:
- "notify": the Maintenance tab says so, with an Install button;
- "install": it installs at the chosen time of night.
Installing downloads the release's pi-photoframe.zip, checks it against the
checksum GitHub publishes, and hands it to the usual updater (backup,
install, health check, roll back if it fails). A version that failed to
install automatically isn't tried again by itself.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

import requests

from . import __version__
from .updater import UpdateError, _version_key, is_beta, is_version

log = logging.getLogger(__name__)

API = "https://api.github.com"
ASSET = "pi-photoframe.zip"
CHECK_EVERY = 24 * 3600
FIRST_CHECK_AFTER = 10 * 60  # after starting up
MODES = ("off", "notify", "install")
BUSY_STATES = ("queued", "unpacking", "backing-up", "installing", "checking", "rolling-back")


class AutoUpdateError(RuntimeError):
    pass


class AutoUpdate:
    def __init__(self, data_dir: Path, cfg, updates, frame=None, api: str | None = None):
        self.cfg = cfg
        self.updates = updates
        self.frame = frame
        self.api = api or API
        (data_dir / "github-token").unlink(missing_ok=True)  # from when the repository was private
        self.state_path = data_dir / "update" / "github.json"
        self.state: dict = {}
        try:
            self.state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            pass
        self.busy: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._started = time.time()
        self._note_failed_attempt()
        self._forget_installed()

    def _forget_installed(self) -> None:
        """A version noted as available that's now installed (or older) isn't available any more."""
        avail = self.state.get("available")
        if avail and _version_key(avail["version"]) <= _version_key(__version__):
            self.state.pop("available", None)
            self.state["latest"] = self.state.get("latest") or avail["version"]
            self._save()

    # ---------- bookkeeping
    def _save(self) -> None:
        self.state_path.parent.mkdir(exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        os.replace(tmp, self.state_path)

    def _note_failed_attempt(self) -> None:
        """After an automatic install: did it take? If not, don't try that version again by itself."""
        tried = self.state.get("auto_attempt")
        if tried and _version_key(__version__) < _version_key(tried):
            self.state.setdefault("failed", [])
            if tried not in self.state["failed"]:
                self.state["failed"].append(tried)
            log.warning("automatic update to %s didn't take (still %s); it won't be retried automatically",
                        tried, __version__)
        if tried:
            self.state.pop("auto_attempt", None)
            self._save()

    @property
    def repo(self) -> str:
        return str(self.cfg.get("update_repo") or "Jimmy3D-xyz/pi-frame")

    @property
    def mode(self) -> str:
        m = self.cfg.get("update_auto")
        return m if m in MODES else "notify"

    @property
    def channel(self) -> str:
        """stable: full releases only. test: pre-releases too (the maintainer's test frames)."""
        return "test" if self.cfg.get("update_channel") == "test" else "stable"

    # ---------- GitHub
    def _get(self, path: str, **kw) -> requests.Response:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": f"Pi-Frame/{__version__}"}
        headers.update(kw.pop("headers", {}))
        try:
            return requests.get(self.api + path, headers=headers, timeout=kw.pop("timeout", 20), **kw)
        except requests.RequestException as e:
            raise AutoUpdateError(f"couldn't reach GitHub ({type(e).__name__})") from e

    def check(self) -> dict:
        """Look at the latest release. Returns the status for the web page."""
        with self._lock:
            self.busy = "checking"
            try:
                if self.channel == "test":
                    r = self._get(f"/repos/{self.repo}/releases", params={"per_page": 30})
                else:
                    r = self._get(f"/repos/{self.repo}/releases/latest")
                self.state["last_check"] = time.time()
                if r.status_code == 404 and not self._repo_found():
                    raise AutoUpdateError(f"Couldn't find {self.repo} on GitHub.")
                if r.status_code == 404:  # the repository has no releases yet
                    self.state.pop("available", None)
                    self.state["latest"] = None
                    self.state.pop("error", None)
                    return self.status()
                if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
                    raise AutoUpdateError("GitHub is limiting how often it can be asked; trying again later")
                if r.status_code != 200:
                    raise AutoUpdateError(f"GitHub answered {r.status_code}")
                rel = r.json()
                if self.channel == "test":
                    # the newest version of all, test releases included
                    rels = [x for x in rel if not x.get("draft") and is_version(str(x.get("tag_name", "")).lstrip("v"))
                            and any(a.get("name") == ASSET for a in x.get("assets", []))]
                    if not rels:
                        self.state.pop("available", None)
                        self.state["latest"] = None
                        self.state.pop("error", None)
                        return self.status()
                    rel = max(rels, key=lambda x: _version_key(str(x["tag_name"]).lstrip("v")))
                version = str(rel.get("tag_name", "")).lstrip("v")
                asset = next((a for a in rel.get("assets", []) if a.get("name") == ASSET), None)
                self.state["latest"] = version
                self.state.pop("error", None)
                if not is_version(version) or asset is None or (is_beta(version) and self.channel != "test"):
                    self.state.pop("available", None)
                elif _version_key(version) > _version_key(__version__):
                    self.state["available"] = {
                        "version": version, "name": rel.get("name") or f"Pi Frame {version}",
                        "test": bool(rel.get("prerelease")) or is_beta(version),
                        "notes": (rel.get("body") or "")[:4000], "url": rel.get("html_url"),
                        "published": rel.get("published_at"), "asset_id": asset["id"],
                        "size": asset.get("size"), "digest": asset.get("digest"),
                    }
                    log.info("update available: %s (installed %s)", version, __version__)
                else:
                    self.state.pop("available", None)
            except AutoUpdateError as e:
                self.state["error"] = str(e)
                self.state["last_check"] = time.time()
                log.warning("update check: %s", e)
                raise
            finally:
                self.busy = None
                self._save()
        return self.status()

    def _repo_found(self) -> bool:
        try:
            return self._get(f"/repos/{self.repo}").status_code == 200
        except AutoUpdateError:
            return False

    def _download(self, avail: dict) -> Path:
        r = self._get(f"/repos/{self.repo}/releases/assets/{avail['asset_id']}",
                      headers={"Accept": "application/octet-stream"}, stream=True, timeout=120)
        if r.status_code != 200:
            raise AutoUpdateError(f"couldn't download the update (GitHub answered {r.status_code})")
        fd, name = tempfile.mkstemp(prefix="github-", suffix=".zip", dir=self.updates.dir)
        h = hashlib.sha256()
        with os.fdopen(fd, "wb") as f:
            for chunk in r.iter_content(256 * 1024):
                f.write(chunk)
                h.update(chunk)
        want = str(avail.get("digest") or "")
        if want.startswith("sha256:") and want.split(":", 1)[1] != h.hexdigest():
            Path(name).unlink(missing_ok=True)
            raise AutoUpdateError("the downloaded update didn't match GitHub's checksum, so it wasn't used")
        return Path(name)

    def install(self, auto: bool = False) -> dict:
        """Download the available release and hand it to the updater."""
        avail = self.state.get("available")
        if not avail:
            raise AutoUpdateError("There's no newer version to install.")
        self.busy = "downloading"
        try:
            path = self._download(avail)
            try:
                with open(path, "rb") as f:
                    info = self.updates.stage(f)
            except UpdateError as e:
                raise AutoUpdateError(f"the release's package isn't right: {e}") from e
            finally:
                path.unlink(missing_ok=True)
            if auto:
                self.state["auto_attempt"] = info["new"]
                self._save()
            info = self.updates.install()
        finally:
            self.busy = None
        log.warning("installing %s from GitHub%s", info["new"], " (automatic)" if auto else "")
        if self.frame is not None:
            self.frame.begin_update(info["new"], self.updates.status)
        return info

    # ---------- the background check
    def due_to_install(self, now: dt.datetime | None = None) -> bool:
        avail = self.state.get("available")
        if self.mode != "install" or not avail or avail["version"] in self.state.get("failed", []):
            return False
        now = now or dt.datetime.now()
        hh, mm = (int(x) for x in str(self.cfg.get("update_time") or "03:30").split(":"))
        start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return start <= now < start + dt.timedelta(hours=2)  # a window, in case the frame was busy

    def run(self) -> None:
        while not self._stop.wait(60):
            try:
                if self.mode == "off":
                    continue
                last = self.state.get("last_check") or 0
                if time.time() - self._started >= FIRST_CHECK_AFTER and time.time() - last >= CHECK_EVERY:
                    try:
                        self.check()
                    except AutoUpdateError:
                        pass
                busy = self.updates.status().get("state") in BUSY_STATES
                if not busy and self.due_to_install():
                    self.install(auto=True)
            except AutoUpdateError as e:
                self.state["error"] = str(e)
                self._save()
            except Exception:  # noqa: BLE001
                log.exception("automatic update")

    def stop(self) -> None:
        self._stop.set()

    # ---------- for the web page
    def status(self) -> dict:
        self._forget_installed()
        avail = self.state.get("available")
        return {
            "mode": self.mode, "time": self.cfg.get("update_time") or "03:30", "repo": self.repo,
            "channel": self.channel,
            "last_check": self.state.get("last_check"),
            "latest": self.state.get("latest"), "available": avail, "error": self.state.get("error"),
            "busy": self.busy, "failed": avail is not None and avail["version"] in self.state.get("failed", []),
        }

    def health(self) -> dict | None:
        if self.mode == "off":
            return None
        if self.state.get("error"):
            return {"level": "warn", "text": "Couldn't check for updates: " + self.state["error"]}
        avail = self.state.get("available")
        if avail:
            how = " (installs tonight)" if self.mode == "install" and avail["version"] not in self.state.get("failed", []) else ""
            return {"level": "ok", "text": f"Version {avail['version']} is available{how}"}
        return None
