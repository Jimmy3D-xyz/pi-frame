"""Updates from GitHub: the frame checks the repository's releases itself.

The repository is private, so the frame needs a read-only GitHub token
(fine-grained, this repository only, "Contents: read"), pasted once on the
Maintenance tab. It's kept in the data folder, readable by the frame only, and
never shown again or included in diagnostics.

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
import re
import tempfile
import threading
import time
from pathlib import Path

import requests

from . import __version__
from .updater import UpdateError, _version_key

log = logging.getLogger(__name__)

API = "https://api.github.com"
ASSET = "pi-photoframe.zip"
CHECK_EVERY = 24 * 3600
FIRST_CHECK_AFTER = 10 * 60  # after starting up
MODES = ("off", "notify", "install")
BUSY_STATES = ("queued", "unpacking", "backing-up", "installing", "checking", "rolling-back")
TOKEN_RE = re.compile(r"^(github_pat_[A-Za-z0-9_]{20,255}|gh[pousr]_[A-Za-z0-9]{20,255})$")


class AutoUpdateError(RuntimeError):
    pass


class AutoUpdate:
    def __init__(self, data_dir: Path, cfg, updates, frame=None, api: str | None = None):
        self.cfg = cfg
        self.updates = updates
        self.frame = frame
        self.api = api or API
        self.token_path = data_dir / "github-token"
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
        return str(self.cfg.get("update_repo") or "JimmySte/pi-frame")

    @property
    def mode(self) -> str:
        m = self.cfg.get("update_auto")
        return m if m in MODES else "notify"

    # ---------- the token
    def token(self) -> str | None:
        try:
            t = self.token_path.read_text().strip()
        except OSError:
            return None
        return t or None

    def set_token(self, token: str) -> dict:
        token = (token or "").strip()
        if not TOKEN_RE.match(token):
            raise AutoUpdateError("That doesn't look like a GitHub token (they start with github_pat_ or ghp_).")
        r = self._get(f"/repos/{self.repo}", token=token)
        if r.status_code in (401, 403):
            raise AutoUpdateError("GitHub didn't accept that token. Check it hasn't expired, and that it can "
                                  f"read {self.repo}.")
        if r.status_code == 404:
            raise AutoUpdateError(f"That token can't see {self.repo}. When creating it, choose this repository "
                                  "under Repository access.")
        if r.status_code != 200:
            raise AutoUpdateError(f"GitHub answered {r.status_code}; try again in a minute.")
        tmp = self.token_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
        os.replace(tmp, self.token_path)
        self.state.pop("error", None)
        self._save()
        log.info("GitHub token saved for %s", self.repo)
        return self.check()

    def forget_token(self) -> None:
        self.token_path.unlink(missing_ok=True)
        self.state.pop("available", None)
        self.state.pop("error", None)
        self._save()

    # ---------- GitHub
    def _get(self, path: str, token: str | None = None, **kw) -> requests.Response:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": f"Pi-Frame/{__version__}"}
        headers.update(kw.pop("headers", {}))
        tok = token or self.token()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        try:
            return requests.get(self.api + path, headers=headers, timeout=kw.pop("timeout", 20), **kw)
        except requests.RequestException as e:
            raise AutoUpdateError(f"couldn't reach GitHub ({type(e).__name__})") from e

    def check(self) -> dict:
        """Look at the latest release. Returns the status for the web page."""
        if not self.token():
            raise AutoUpdateError("Add a GitHub token first.")
        with self._lock:
            self.busy = "checking"
            try:
                r = self._get(f"/repos/{self.repo}/releases/latest")
                self.state["last_check"] = time.time()
                if r.status_code == 404:
                    self.state.pop("available", None)
                    self.state["latest"] = None
                    self.state.pop("error", None)
                    return self.status()
                if r.status_code in (401, 403):
                    raise AutoUpdateError("GitHub no longer accepts the token (it may have expired). "
                                          "Add a new one.")
                if r.status_code != 200:
                    raise AutoUpdateError(f"GitHub answered {r.status_code}")
                rel = r.json()
                version = str(rel.get("tag_name", "")).lstrip("v")
                asset = next((a for a in rel.get("assets", []) if a.get("name") == ASSET), None)
                self.state["latest"] = version
                self.state.pop("error", None)
                if not re.fullmatch(r"\d+(\.\d+){1,3}", version) or asset is None:
                    self.state.pop("available", None)
                elif _version_key(version) > _version_key(__version__):
                    self.state["available"] = {
                        "version": version, "name": rel.get("name") or f"Pi Frame {version}",
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
                if self.mode == "off" or not self.token():
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
        avail = self.state.get("available")
        return {
            "mode": self.mode, "time": self.cfg.get("update_time") or "03:30", "repo": self.repo,
            "token_set": bool(self.token()), "last_check": self.state.get("last_check"),
            "latest": self.state.get("latest"), "available": avail, "error": self.state.get("error"),
            "busy": self.busy, "failed": avail is not None and avail["version"] in self.state.get("failed", []),
        }

    def health(self) -> dict | None:
        if self.mode == "off" or not self.token():
            return None
        if self.state.get("error"):
            return {"level": "warn", "text": "Couldn't check for updates: " + self.state["error"]}
        avail = self.state.get("available")
        if avail:
            how = " (installs tonight)" if self.mode == "install" and avail["version"] not in self.state.get("failed", []) else ""
            return {"level": "ok", "text": f"Version {avail['version']} is available{how}"}
        return None
