"""The installer kit: finds a backup, checks it, and restores it."""
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from photoframe import backup
from photoframe.config import ConfigStore
from photoframe.storage import Storage

REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0, reason="the kit runs as root")


def kit(tmp_path) -> Path:
    k = tmp_path / "pi-frame-kit"
    k.mkdir()
    for f in ("install-frame.sh", "kit.conf"):
        (k / f).write_bytes((REPO / "kit" / f).read_bytes())
    with zipfile.ZipFile(k / "pi-photoframe.zip", "w") as z:  # a stand-in package whose installer does nothing
        z.writestr("pi-photoframe/deploy/install.sh", "#!/bin/bash\necho fake install \"$@\"\n")
        z.writestr("pi-photoframe/photoframe/__init__.py", '__version__ = "9.9.9"\n')
    return k


def run(k: Path, tmp_path: Path):
    env = {**os.environ, "PF_KIT_WORK": str(tmp_path / "work"), "PF_KIT_DATA_PARENT": str(tmp_path / "lib"),
           "PF_KIT_LOG": str(tmp_path / "kit.log"), "PF_KIT_NET_URL": (REPO / "README.md").as_uri(),
           "PF_KIT_NO_REBOOT": "1"}
    (tmp_path / "lib").mkdir(exist_ok=True)
    return subprocess.run(["bash", str(k / "install-frame.sh")], env=env, capture_output=True, text=True, timeout=120)


def test_kit_restores_a_downloaded_backup(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    ConfigStore(src).update({"device_name": "Kitchen"})
    Storage(src, (800, 480)).create_album("Holidays")
    k = kit(tmp_path)
    with open(k / "piframe-backup-2026-11-20-0300-settings.tgz", "wb") as f:
        backup.write_archive(src, "settings", f)
    r = run(k, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Restoring photos and settings from piframe-backup-2026-11-20-0300-settings.tgz" in r.stdout
    data = tmp_path / "lib" / "pi-photoframe"
    assert ConfigStore(data).get("device_name") == "Kitchen" and (data / "albums" / "Holidays").is_dir()
    assert (data / backup.INFO).exists()  # so the frame notices the restore when it starts


def test_kit_refuses_something_that_isnt_a_backup(tmp_path):
    k = kit(tmp_path)
    with tarfile.open(k / "frame-backup.tgz", "w:gz") as t:
        (tmp_path / "x").write_text("x")
        t.add(tmp_path / "x", arcname="etc/cron.d/x")
    r = run(k, tmp_path)
    assert r.returncode != 0 and "isn't a Pi Frame backup" in r.stdout
    assert not (tmp_path / "lib" / "etc").exists()


def test_kit_without_a_backup(tmp_path):
    r = run(kit(tmp_path), tmp_path)
    assert r.returncode == 0 and "No backup in the kit" in r.stdout
