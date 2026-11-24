import io
import json
import os
import tarfile
import time
from pathlib import Path

import pytest
from PIL import Image

from photoframe import backup
from photoframe.backup import BackupError, Backups
from photoframe.cloud import managed_stem
from photoframe.config import ConfigStore
from photoframe.storage import Storage

from adminhelp import unlock

PANEL = (800, 480)


def jpeg(color=(200, 50, 50), size=(640, 480)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "JPEG")
    return b.getvalue()


@pytest.fixture
def frame_data(tmp_path):
    """A frame's data folder: settings, two albums (one synced), a sign-in."""
    d = tmp_path / "src"
    d.mkdir()
    cfg = ConfigStore(d)
    cfg.update({"device_name": "Living room", "http_password": "pw123456"})
    st = Storage(d, PANEL)
    st.create_album("Holidays")
    st.add("Holidays", "beach.jpg", jpeg())
    st.add("Default", "cat.jpg", jpeg((10, 200, 10)))
    (d / "albums" / "Holidays" / "clip.mp4").write_bytes(b"\0" * 1000)  # a video, as far as backups care
    st.create_album("Wedding")
    link_id = "abc123def0"
    synced = managed_stem(link_id, "Ceremony/IMG_1.jpg")
    st.add("Wedding", "IMG_1.jpg", jpeg((0, 0, 200)), stem=synced)
    st.add("Wedding", "mine.jpg", jpeg((90, 90, 90)))  # added by hand to a synced album
    (d / "cloud").mkdir()
    (d / "cloud" / "rclone.conf").write_text("[onedrive]\ntype = onedrive\ntoken = {\"t\":1}\n")
    os.chmod(d / "cloud" / "rclone.conf", 0o600)
    (d / "cloud" / "state.json").write_text(json.dumps({"accounts": [], "links": [
        {"id": link_id, "remote": "onedrive", "folder": "Wedding", "album": "Wedding", "every_hours": 1,
         "last_sync": time.time()}]}))
    (d / "cloud" / f"manifest-{link_id}.json").write_text(json.dumps(
        {"Ceremony/IMG_1.jpg": {"size": 1, "mtime": "x"}}))
    (d / "device_id").write_text("frame-id-1")
    (d / "screen.json").write_text("{}")  # machine-specific: not backed up
    (d / "update").mkdir()
    (d / "update" / "log.txt").write_text("old")
    return d, synced


def make(d: Path, kind: str, tmp_path: Path) -> Path:
    out = tmp_path / f"{kind}.tgz"
    with open(out, "wb") as f:
        backup.write_archive(d, kind, f)
    return out


def names(path: Path) -> set[str]:
    with tarfile.open(path) as t:
        return {m.name for m in t.getmembers()}


def test_settings_backup_holds_settings_and_album_names_only(frame_data, tmp_path):
    d, _ = frame_data
    n = names(make(d, "settings", tmp_path))
    for rel in ("config.json", "device_id", "cloud/rclone.conf", "cloud/state.json", "albums/Holidays",
                "albums/Wedding", backup.INFO):
        assert f"pi-photoframe/{rel}" in n, rel
    assert not any(x.endswith(".jpg") or x.endswith(".mp4") for x in n)
    assert "pi-photoframe/screen.json" not in n and not any("/update" in x for x in n)


def test_full_backup_leaves_out_synced_photos(frame_data, tmp_path):
    d, synced = frame_data
    path = make(d, "full", tmp_path)
    n = names(path)
    assert "pi-photoframe/albums/Holidays/beach.jpg" in n and "pi-photoframe/albums/Default/cat.jpg" in n
    assert "pi-photoframe/albums/Holidays/clip.mp4" in n
    assert "pi-photoframe/albums/Wedding/mine.jpg" in n  # added by hand: kept
    assert f"pi-photoframe/albums/Wedding/{synced}.jpg" not in n  # downloads again by itself
    assert "pi-photoframe/albums/Holidays/.thumbs/beach.jpg" in n
    info = backup.inspect(path)
    assert info["kind"] == "full" and info["photos"] == 3 and info["videos"] == 1 and not info["made_by_hand"]
    with tarfile.open(path) as t:
        conf = t.getmember("pi-photoframe/cloud/rclone.conf")
        assert conf.mode == 0o600 and conf.uid == 0 and conf.uname == ""


def test_restore_onto_a_new_frame(frame_data, tmp_path):
    d, synced = frame_data
    path = make(d, "full", tmp_path)
    new = tmp_path / "new"
    new.mkdir()
    Storage(new, PANEL)  # a fresh frame
    done = backup.restore(new, path)
    assert done == {"photos": 3, "videos": 1, "settings": True}
    assert ConfigStore(new).get("device_name") == "Living room"
    assert (new / "albums" / "Holidays" / "beach.jpg").read_bytes() == (d / "albums" / "Holidays" / "beach.jpg").read_bytes()
    assert (new / "albums" / "Wedding").is_dir() and not (new / "albums" / "Wedding" / f"{synced}.jpg").exists()
    assert oct((new / "cloud" / "rclone.conf").stat().st_mode & 0o777) == "0o600"
    assert (new / "device_id").read_text() == "frame-id-1"
    assert not list(new.rglob("*.restoring"))


def test_settings_restore_keeps_photos_already_there(frame_data, tmp_path):
    d, _ = frame_data
    path = make(d, "settings", tmp_path)
    ConfigStore(d).update({"device_name": "Changed"})
    (d / "albums" / "Default" / "later.jpg").write_bytes(jpeg())
    backup.restore(d, path)
    assert ConfigStore(d).get("device_name") == "Living room"
    assert (d / "albums" / "Default" / "later.jpg").exists() and (d / "albums" / "Holidays" / "beach.jpg").exists()


def test_old_hand_made_backup_restores_what_it_knows(frame_data, tmp_path):
    """`tar czf frame-backup.tgz -C /var/lib pi-photoframe`, as the docs used to say."""
    d, _ = frame_data
    src = tmp_path / "lib" / "pi-photoframe"
    src.parent.mkdir()
    os.rename(d, src)
    old = tmp_path / "frame-backup.tgz"
    with tarfile.open(old, "w:gz") as t:
        t.add(src, arcname="pi-photoframe")
    info = backup.inspect(old)
    assert info["made_by_hand"] and info["kind"] == "full" and info["settings"]
    new = tmp_path / "new"
    new.mkdir()
    backup.restore(new, old)
    assert (new / "config.json").exists() and (new / "albums" / "Holidays" / "beach.jpg").exists()
    assert not (new / "update").exists() and not (new / "screen.json").exists()


def evil(tmp_path, build) -> Path:
    p = tmp_path / "evil.tgz"
    with tarfile.open(p, "w:gz") as t:
        build(t)
    return p


def add_bytes(t, name, data=b"x", **kw):
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    for k, v in kw.items():
        setattr(ti, k, v)
    t.addfile(ti, io.BytesIO(data))


@pytest.mark.parametrize("build,match", [
    (lambda t: add_bytes(t, "pi-photoframe/../../etc/passwd"), "unsafe"),
    (lambda t: add_bytes(t, "etc/cron.d/x"), "isn't a Pi Frame backup"),
    (lambda t: add_bytes(t, "pi-photoframe/config.json", type=tarfile.SYMTYPE, linkname="/etc/shadow"), "links"),
    (lambda t: add_bytes(t, "pi-photoframe/notes.txt"), "nothing in that backup"),
])
def test_unsafe_or_foreign_archives_are_refused(tmp_path, build, match):
    p = evil(tmp_path, build)
    with pytest.raises(BackupError, match=match):
        backup.inspect(p)
    if match != "nothing in that backup":
        with pytest.raises(BackupError, match=match):
            backup.restore(tmp_path / "x", p)
        assert not (tmp_path / "x").exists() or not any((tmp_path / "x").rglob("*"))


def test_damaged_backup_is_caught_before_restoring(frame_data, tmp_path):
    d, _ = frame_data
    path = make(d, "full", tmp_path)
    blob = path.read_bytes()
    path.write_bytes(blob[: len(blob) // 2])
    with pytest.raises(BackupError, match="damaged"):
        backup.inspect(path)
    (tmp_path / "junk.tgz").write_text("hello")
    with pytest.raises(BackupError, match="damaged"):
        backup.inspect(tmp_path / "junk.tgz")


def test_only_known_files_are_restored(tmp_path):
    p = evil(tmp_path, lambda t: (add_bytes(t, "pi-photoframe/config.json", b'{"config": {}}'),
                                  add_bytes(t, "pi-photoframe/albums/A/x.jpg", jpeg()),
                                  add_bytes(t, "pi-photoframe/albums/A/run.sh", b"#!/bin/sh"),
                                  add_bytes(t, "pi-photoframe/update/pending.zip", b"zip")))
    new = tmp_path / "new"
    new.mkdir()
    backup.restore(new, p)
    assert (new / "albums" / "A" / "x.jpg").exists() and (new / "config.json").exists()
    assert not (new / "albums" / "A" / "run.sh").exists() and not (new / "update").exists()


def test_cli_streams_a_backup(frame_data, tmp_path):
    import subprocess
    import sys

    d, _ = frame_data
    out = tmp_path / "cli.tgz"
    r = subprocess.run([sys.executable, "-m", "photoframe.backup", "create", "--data-dir", str(d), "--kind",
                        "settings"], stdout=open(out, "wb"), stderr=subprocess.PIPE,
                       cwd=str(Path(backup.__file__).resolve().parents[1]))
    assert r.returncode == 0, r.stderr
    assert backup.inspect(out)["kind"] == "settings"


# ---------------------------------------------------------------- the manager
class FakeCloud:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.state = {"accounts": [{"remote": "nas", "provider": "smb", "name": "NAS (diskstation)"}]}
        self.fail = None

    def remote_connected(self, r):
        return r == "nas"

    def can_back_up(self, r):
        return True, ""

    def backup_upload(self, remote, folder, name, src):
        if self.fail:
            raise BackupError(self.fail)
        self.files[f"{folder}/{name}"] = src.read()
        return len(self.files[f"{folder}/{name}"])

    def backup_list(self, remote, folder):
        return [{"Name": k.split("/", 1)[1], "Size": len(v)} for k, v in self.files.items() if k.startswith(folder + "/")]

    def backup_delete(self, remote, folder, name):
        self.files.pop(f"{folder}/{name}", None)


def manager(d, **cfg_changes):
    cfg = ConfigStore(d)
    cfg.update(cfg_changes)
    restarted = []
    cloud = FakeCloud()
    b = Backups(d, cfg, cloud, None, restart=lambda: restarted.append(True))
    return b, cfg, cloud, restarted


def test_automatic_backups_to_the_nas_keep_the_newest(frame_data, monkeypatch):
    d, _ = frame_data
    b, cfg, cloud, _ = manager(d, backup_auto=True, backup_where="nas", backup_kind="settings", backup_keep=2)
    stamps = iter(["2026-01-0%d-0300" % i for i in range(1, 9)])
    monkeypatch.setattr(backup, "backup_name", lambda kind, when=None, label="backup":
                        f"{backup.hostname()}-{label}-{next(stamps)}-{kind}.tgz")
    for _ in range(3):
        r = b.back_up(auto=True)
        assert r["ok"], r
    assert sorted(cloud.files) == [f"Pi Frame backups/{backup.hostname()}-backup-2026-01-02-0300-settings.tgz",
                                   f"Pi Frame backups/{backup.hostname()}-backup-2026-01-03-0300-settings.tgz"]
    cloud.files["Pi Frame backups/someone-elses-file.tgz"] = b"keep me"
    b.back_up()
    assert "Pi Frame backups/someone-elses-file.tgz" in cloud.files
    assert b.status()["last"]["where"] == "NAS (diskstation)"


def test_failed_backup_is_reported_and_retried_later(frame_data):
    d, _ = frame_data
    b, cfg, cloud, _ = manager(d, backup_auto=True, backup_where="nas")
    cloud.fail = "The NAS didn't let the frame save there."
    r = b.back_up(auto=True)
    assert not r["ok"] and "didn't let" in r["message"]
    assert b.health()["level"] == "warn"
    assert not b.due(time.time() + 3600)  # waits before trying again
    assert b.due(time.time() + backup.RETRY_AFTER + 2 * 86400)


def test_disconnected_destination_says_so(frame_data):
    d, _ = frame_data
    b, *_ = manager(d, backup_auto=True, backup_where="onedrive")
    r = b.back_up()
    assert not r["ok"] and "isn't connected" in r["message"]


def test_backups_on_the_frame(frame_data):
    d, _ = frame_data
    b, *_ = manager(d, backup_auto=True, backup_where="frame", backup_kind="full")
    r = b.back_up()
    assert r["ok"], r
    files = b.local_files()
    assert len(files) == 1 and files[0]["kind"] == "full"
    assert backup.inspect(b.local_path(files[0]["name"]))["photos"] == 3
    with pytest.raises(BackupError):
        b.local_path("../config.json")


def test_schedule(frame_data):
    import datetime as dt

    d, _ = frame_data
    b, cfg, *_ = manager(d)
    assert b.next_due() is None  # off
    cfg.update({"backup_auto": True, "backup_every": "weekly", "backup_time": "03:00"})
    assert b.due()  # never backed up: straight away
    day = dt.datetime(2026, 3, 2, 3, 0).timestamp()  # a Monday, 03:00
    b.state["last_ok_t"] = day
    b.state["last"] = {"t": day, "ok": True}
    assert not b.due(day + 3 * 86400)
    nxt = dt.datetime.fromtimestamp(b.next_due(day + 3 * 86400))
    assert (nxt.month, nxt.day, nxt.hour, nxt.minute) == (3, 9, 3, 0)
    assert b.due(dt.datetime(2026, 3, 9, 3, 1).timestamp())
    assert b.health()["level"] in ("ok", "warn")


def test_restore_flow_keeps_a_copy_and_restarts(frame_data, tmp_path):
    d, _ = frame_data
    path = make(d, "full", tmp_path)
    blob = path.read_bytes()
    new = tmp_path / "new"
    new.mkdir()
    Storage(new, PANEL)
    ConfigStore(new).update({"device_name": "Fresh"})
    b, cfg, cloud, restarted = manager(new)
    st = b.restore_start("piframe-backup.tgz", len(blob), 123)
    assert st["received"] == 0
    half = len(blob) // 2
    assert b.restore_append(0, io.BytesIO(blob[:half]), half)["received"] == half
    # the page was refreshed: choosing the same file carries on
    assert b.restore_start("piframe-backup.tgz", len(blob), 123)["received"] == half
    r = b.restore_append(half, io.BytesIO(blob[half:]), len(blob) - half)
    assert r["staged"]["photos"] == 3
    b.restore_begin()
    for _ in range(100):
        if restarted:
            break
        time.sleep(0.05)
    assert restarted
    assert ConfigStore(new).get("device_name") == "Living room"
    assert b.restore_state()["state"] == "done" and "3 photos and 1 video" in b.restore_state()["message"]
    before = [f for f in b.local_files() if f["before_restore"]]
    assert len(before) == 1 and backup.inspect(b.local_path(before[0]["name"]))["kind"] == "settings"
    assert b.staged() is None
    # after the restart: noticed once, so synced folders fetch their photos again
    again = Backups(new, ConfigStore(new), cloud, None)
    assert again.just_restored and not Backups(new, ConfigStore(new), cloud, None).just_restored


def test_bad_upload_is_refused_and_removed(frame_data):
    d, _ = frame_data
    b, *_ = manager(d)
    b.restore_start("x.tgz", 5, 1)
    with pytest.raises(BackupError, match="damaged"):
        b.restore_append(0, io.BytesIO(b"hello"), 5)
    assert b.staged() is None


def test_kit_restore_is_noticed(frame_data):
    d, _ = frame_data
    (d / backup.INFO).write_text(json.dumps({"created": time.time(), "kind": "full"}))
    b, *_ = manager(d)
    assert b.just_restored and not (d / backup.INFO).exists()


# ---------------------------------------------------------------- the web page
import base64  # noqa: E402

from photoframe.display import FileDisplay  # noqa: E402
from photoframe.frame import Frame  # noqa: E402
from photoframe.web import create_app  # noqa: E402

AUTH = {"Authorization": "Basic " + base64.b64encode(b"alex:pw123456").decode()}


@pytest.fixture
def web(frame_data, monkeypatch):
    d, _ = frame_data
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    cfg = ConfigStore(d)
    frame = Frame(cfg, Storage(d, PANEL), FileDisplay(PANEL, str(d.parent / "f.png")), d)
    client = create_app(frame).test_client()
    restarted = []
    frame.backups.restart = lambda: restarted.append(True)
    return frame, client, d, restarted


def test_backups_need_the_admin_login(web):
    frame, client, d, _ = web
    r = client.get("/api/backup/download?kind=settings", headers=AUTH)
    assert r.status_code == 403 and r.get_json()["admin"] == "no-password"
    frame.cfg.update({"admin_password": "admin-pass-1"})
    # the page password alone doesn't open them
    r = client.post("/api/backup/restore/start", headers=AUTH, json={"name": "x", "size": 5})
    assert r.status_code == 403 and r.get_json()["admin"] == "locked"
    unlock(client, frame, AUTH)
    st = client.get("/api/backup", headers=AUTH).get_json()
    assert st["password_set"] is True


def test_web_download_status_and_restore(web, tmp_path):
    frame, client, d, restarted = web
    unlock(client, frame, AUTH)
    st = client.get("/api/backup", headers=AUTH).get_json()
    assert st["password_set"] and st["estimate"]["full"]["photos"] == 3 and st["destinations"][0]["where"] == "frame"
    r = client.get("/api/backup/download?kind=full", headers=AUTH)
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"] and "-full.tgz" in r.headers["Content-Disposition"]
    blob = r.get_data()
    (tmp_path / "dl.tgz").write_bytes(blob)
    assert backup.inspect(tmp_path / "dl.tgz")["photos"] == 3
    # change something, then restore the download
    frame.cfg.update({"device_name": "Changed"})
    s = client.post("/api/backup/restore/start", headers=AUTH,
                    json={"name": "dl.tgz", "size": len(blob), "last_modified": 1}).get_json()
    r = client.put(f"/api/backup/restore/upload?offset={s['received']}", headers=AUTH, data=blob)
    assert r.status_code == 200 and r.get_json()["staged"]["kind"] == "full"
    assert client.put("/api/backup/restore/upload?offset=0", headers=AUTH, data=b"x").status_code in (400, 409)
    assert client.post("/api/backup/restore", headers=AUTH, json={"from": "upload"}).status_code == 200
    for _ in range(100):
        if restarted:
            break
        time.sleep(0.05)
    assert restarted and ConfigStore(d).get("device_name") == "Living room"


def test_web_backup_now_and_health(web):
    frame, client, d, _ = web
    unlock(client, frame, AUTH)
    frame.cfg.update({"backup_auto": True, "backup_where": "frame", "backup_kind": "settings"})
    assert client.post("/api/backup/now", headers=AUTH).status_code == 200
    for _ in range(100):
        if frame.backups.state.get("last"):
            break
        time.sleep(0.05)
    st = client.get("/api/backup", headers=AUTH).get_json()
    assert st["last"]["ok"] and len(st["local"]) == 1
    name = st["local"][0]["name"]
    assert client.get(f"/api/backup/file/{name}", headers=AUTH).status_code == 200
    m = client.get("/api/maintenance", headers=AUTH).get_json()
    assert any(x["area"] == "Backups" and x["level"] == "ok" for x in m["summary"])
    assert client.delete(f"/api/backup/file/{name}", headers=AUTH).status_code == 200
    assert client.get("/api/backup/file/..%2Fconfig.json", headers=AUTH).status_code == 404


def test_config_checks_backup_settings(web):
    frame, client, d, _ = web
    unlock(client, frame, AUTH)
    for bad in ({"backup_time": "25:00"}, {"backup_keep": 0}, {"backup_where": "../x"}, {"backup_folder": "a/../b"},
                {"backup_every": "hourly"}):
        assert client.post("/api/config", headers=AUTH, json=bad).status_code == 400, bad
    assert client.post("/api/config", headers=AUTH, json={"backup_folder": "/Backups/Frame/"}).status_code == 200
    assert frame.cfg.get("backup_folder") == "Backups/Frame"
