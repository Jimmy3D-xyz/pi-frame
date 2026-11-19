"""Software updates from the Maintenance tab.

The frame (unprivileged) only checks the uploaded package and places it at
update/pending.zip. The root updater (deploy/update.sh, started by the
pi-photoframe-update.path unit) installs it, health-checks the frame and rolls
back on failure, writing progress to update/status.json.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
import zipfile
from pathlib import Path

from . import __version__

MAX_PACKAGE = 50 * 1024 * 1024
_VERSION_RE = re.compile(r'^__version__\s*=\s*"([0-9A-Za-z.\-+]{1,32})"', re.M)


class UpdateError(ValueError):
    pass


def _version_key(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"[.\-+]", v))


def inspect_package(path: Path) -> dict:
    """Check an uploaded zip is a Pi Frame package; return its version."""
    if path.stat().st_size > MAX_PACKAGE:
        raise UpdateError("that file is too big to be a Pi Frame update")
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise UpdateError("that isn't a zip file") from e
    with z:
        names = set()
        for info in z.infolist():
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts:
                raise UpdateError(f"the package contains an unsafe path ({name})")
            if stat.S_ISLNK((info.external_attr >> 16) & 0o177777):
                raise UpdateError("the package contains a symbolic link")
            names.add(name)
        needed = {"pi-photoframe/photoframe/__init__.py", "pi-photoframe/deploy/install.sh",
                  "pi-photoframe/photoframe/__main__.py"}
        missing = needed - names
        if missing:
            raise UpdateError("that zip isn't a Pi Frame update (it's missing "
                              + ", ".join(sorted(m.split("/", 1)[1] for m in missing)) + ")")
        m = _VERSION_RE.search(z.read("pi-photoframe/photoframe/__init__.py").decode("utf-8", "replace"))
        if not m:
            raise UpdateError("couldn't find a version number in the package")
    new = m.group(1)
    try:
        direction = "upgrade" if _version_key(new) > _version_key(__version__) else \
            "same" if new == __version__ else "downgrade"
    except TypeError:
        direction = "unknown"
    return {"current": __version__, "new": new, "direction": direction}


class Updates:
    def __init__(self, data_dir: Path):
        self.dir = data_dir / "update"
        self.dir.mkdir(exist_ok=True)

    @property
    def staged(self) -> Path:
        return self.dir / "staged.zip"

    def stage(self, fileobj) -> dict:
        tmp = self.dir / "upload.tmp"
        size = 0
        with open(tmp, "wb") as f:
            while chunk := fileobj.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_PACKAGE:
                    f.close()
                    tmp.unlink(missing_ok=True)
                    raise UpdateError("that file is too big to be a Pi Frame update")
                f.write(chunk)
        try:
            info = inspect_package(tmp)
        except UpdateError:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self.staged)
        return info

    def install(self) -> dict:
        """Hand the staged package to the root updater."""
        if not self.staged.exists():
            raise UpdateError("no update has been uploaded")
        info = inspect_package(self.staged)
        self._write_status("queued", f"Waiting for the updater to start (version {info['new']})")
        os.replace(self.staged, self.dir / "pending.zip")  # the .path unit reacts to this
        return info

    def discard(self) -> None:
        self.staged.unlink(missing_ok=True)

    def _write_status(self, state: str, message: str) -> None:
        tmp = self.dir / "status.json.tmp"
        tmp.write_text(json.dumps({"state": state, "message": message, "t": time.time()}))
        os.replace(tmp, self.dir / "status.json")

    def status(self) -> dict:
        try:
            st = json.loads((self.dir / "status.json").read_text())
        except (OSError, json.JSONDecodeError):
            st = {"state": "idle", "message": "", "t": None}
        # queued but nobody picked it up: the updater isn't installed yet
        if st["state"] == "queued" and (self.dir / "pending.zip").exists() and time.time() - (st.get("t") or 0) > 15:
            # Take it back, so it doesn't install by surprise the moment the
            # updater is set up later; it stays uploaded, ready to try again.
            try:
                os.replace(self.dir / "pending.zip", self.staged)
            except OSError:
                pass
            self._write_status("not-installed", "")
            st = {"state": "not-installed", "t": st.get("t"),
                  "message": "The updater isn't set up on this Pi yet. Run install.sh once over SSH "
                             "(sudo ./deploy/install.sh), then web updates will work."}
        try:
            log = (self.dir / "log.txt").read_text()[-4000:]
        except OSError:
            log = ""
        staged = None
        if self.staged.exists():
            try:
                staged = inspect_package(self.staged)
            except UpdateError:
                staged = None
        return {**st, "version": __version__, "log": log, "staged": staged}
