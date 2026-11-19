import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image

from photoframe import cloud as cloudmod, video
from photoframe.cloud import CloudError, OneDrive, managed_stem
from photoframe.config import ConfigStore
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.storage import Storage
from photoframe.web import create_app

pytestmark = pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not installed")
PANEL = (1920, 1080)


def photo(path: Path, color, size=(3000, 2000)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def heic(path: Path, color):
    jpg = photo(path.with_suffix(".src.jpg"), color)
    subprocess.run(["heif-enc", "-q", "60", "-o", str(path), str(jpg)], check=True, capture_output=True)
    jpg.unlink()
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    data = tmp_path / "data"
    data.mkdir()
    cfg = ConfigStore(data)
    storage = Storage(data, PANEL)
    frame = Frame(cfg, storage, FileDisplay(PANEL, str(tmp_path / "f.png")), data)
    if video.tools_available():
        frame.jobs = video.VideoJobs(storage)
    od = OneDrive(data, storage, frame)
    frame.cloud = od
    # a local folder stands in for OneDrive: same rclone commands, real behaviour
    od.conf.write_text("[onedrive]\ntype = local\n")
    root = tmp_path / "OneDrive"
    return frame, od, root, create_app(frame).test_client()


def wait_sync(od, link_id, t=60):
    end = time.time() + t
    while time.time() < end:
        link = od._link(link_id)
        if link.get("last_sync") and not od.progress.get(link_id):
            return link
        time.sleep(0.1)
    raise AssertionError("sync didn't finish")


def album_files(storage, album):
    return sorted(i["filename"] for i in storage.images(album))


def test_managed_stem_is_stable_and_unique():
    a = managed_stem("L1", "Ceremony/IMG_0001.jpg")
    assert a == managed_stem("L1", "Ceremony/IMG_0001.jpg") and a.startswith("IMG_0001_")
    assert a != managed_stem("L1", "Reception/IMG_0001.jpg")  # same name, other subfolder
    assert a != managed_stem("L2", "Ceremony/IMG_0001.jpg")


def test_sync_add_change_remove(env):
    frame, od, root, client = env
    wed = root / "Pictures" / "Wedding"
    photo(wed / "IMG_0001.jpg", (200, 50, 50))
    photo(wed / "Reception" / "IMG_0002.jpg", (50, 50, 200), (2000, 3000))
    heic(wed / "IMG_0003.HEIC", (50, 200, 50))
    (wed / "notes.txt").write_text("not a photo")

    link = od.add_link(str(wed), "Wedding")
    link = wait_sync(od, link["id"])
    assert link["last_ok"] is True and link["files"] == 3, link
    assert link["last_result"].startswith("Added 3")
    files = album_files(frame.storage, "Wedding")
    assert len(files) == 3 and all(f.endswith(".jpg") for f in files)  # HEIC became a normal photo
    heic_local = frame.storage.root / "Wedding" / f"{managed_stem(link['id'], 'IMG_0003.HEIC')}.jpg"
    assert Image.open(heic_local).getpixel((10, 10))[1] > 150  # really the green HEIC

    # a photo uploaded by hand into the same album must never be touched by the sync
    frame.storage.add("Wedding", "manual.jpg", Image.new("RGB", (800, 600)))

    # change in OneDrive: remove one, add one, replace one with a different photo
    (wed / "Reception" / "IMG_0002.jpg").unlink()
    photo(wed / "IMG_0004.jpg", (240, 240, 0))
    time.sleep(1.1)
    photo(wed / "IMG_0001.jpg", (0, 0, 0), (3200, 2000))
    r = od.sync(link["id"])
    assert (r["added"], r["updated"], r["removed"], r["failed"]) == (1, 1, 1, 0), r
    files = album_files(frame.storage, "Wedding")
    assert "manual.jpg" in files and len(files) == 4
    first = frame.storage.root / "Wedding" / f"{managed_stem(link['id'], 'IMG_0001.jpg')}.jpg"
    assert Image.open(first).getpixel((10, 10)) == (0, 0, 0)  # the replaced version

    # nothing changed: nothing downloaded
    r = od.sync(link["id"])
    assert (r["added"], r["updated"], r["removed"]) == (0, 0, 0)
    assert od._link(link["id"])["last_result"].startswith("No changes")


def test_subfolders_and_mirror_options(env):
    frame, od, root, client = env
    wed = root / "W"
    photo(wed / "top.jpg", (1, 2, 3))
    photo(wed / "sub" / "deep.jpg", (4, 5, 6))
    link = od.add_link(str(wed), "Frame", subfolders=False)
    wait_sync(od, link["id"])
    assert len(album_files(frame.storage, "Frame")) == 1  # subfolder ignored
    od.update_link(link["id"], subfolders=True)
    od.sync(link["id"])
    assert len(album_files(frame.storage, "Frame")) == 2
    od.update_link(link["id"], mirror=False)
    (wed / "top.jpg").unlink()
    od.sync(link["id"])
    assert len(album_files(frame.storage, "Frame")) == 2  # kept: mirroring deletions is off


def test_stops_when_card_is_nearly_full(env, monkeypatch):
    frame, od, root, client = env
    photo(root / "F" / "a.jpg", (9, 9, 9))
    real = shutil.disk_usage
    monkeypatch.setattr(cloudmod.shutil, "disk_usage", lambda p: real(p)._replace(free=100 * 1024 * 1024))
    with pytest.raises(CloudError):
        od.add_link("", "Bad/Name", True)  # invalid album name is refused up front
    link = od.add_link(str(root / "F"), "Full")
    link = wait_sync(od, link["id"])
    assert link["last_ok"] is False and "nearly full" in link["last_result"]


@pytest.mark.skipif(not video.tools_available(), reason="ffmpeg not installed")
def test_synced_video_gets_stable_name(env):
    frame, od, root, client = env
    clip = root / "V" / "clip.mp4"
    clip.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-t", "1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)
    link = od.add_link(str(root / "V"), "Videos")
    wait_sync(od, link["id"])
    stem = managed_stem(link["id"], "clip.mp4")
    for _ in range(100):
        if (frame.storage.root / "Videos" / f"{stem}.mp4").exists():
            break
        time.sleep(0.1)
    assert (frame.storage.root / "Videos" / f"{stem}.mp4").exists()
    clip.unlink()
    od.sync(link["id"])
    assert not (frame.storage.root / "Videos" / f"{stem}.mp4").exists()  # mirrored removal


def test_web_api_browse_and_link(env):
    frame, od, root, client = env
    photo(root / "Pictures" / "Wedding" / "a.jpg", (1, 1, 1))
    (root / "Pictures" / "Holidays").mkdir(parents=True)
    st = client.get("/api/cloud").get_json()
    assert st["connected"] and st["available"] and st["links"] == []
    folders = client.get(f"/api/cloud/folders?path={root / 'Pictures'}").get_json()
    assert [f["name"] for f in folders] == ["Holidays", "Wedding"]
    count = client.get(f"/api/cloud/count?path={root / 'Pictures' / 'Wedding'}").get_json()
    assert count["photos"] == 1 and count["videos"] == 0
    link = client.post("/api/cloud/links", json={"folder": str(root / "Pictures" / "Wedding"), "album": "Wedding"}).get_json()
    wait_sync(od, link["id"])
    assert client.post("/api/cloud/links", json={"folder": "x", "album": "Wedding"}).status_code == 400  # taken
    h = client.get("/api/maintenance").get_json()
    assert any(s["area"] == "Synced folders" and s["level"] == "ok" for s in h["summary"])
    assert client.delete(f"/api/cloud/links/{link['id']}?delete_album=1").status_code == 200
    assert "Wedding" not in [a["name"] for a in frame.storage.albums()]


# ---------------- sign-in hand-off ----------------
FAKE_RCLONE = r'''#!/usr/bin/env python3
# Stand-in for `rclone authorize onedrive`: same output, same local web server,
# same redirect to Microsoft and code hand-back. Anything else goes to real rclone.
import sys, os, http.server, urllib.parse, json
args = sys.argv[1:]
if "authorize" not in args:
    os.execv(REAL, [REAL] + args)
STATE = "st4te123"
open(os.environ.get("FAKE_ARGS_FILE", "/dev/null"), "a").write(json.dumps(args) + "\n")
print('NOTICE: Please go to the following link: http://127.0.0.1:53682/auth?state=' + STATE, flush=True)
print('NOTICE: Log in and authorize rclone for access', flush=True)
print('NOTICE: Waiting for code...', flush=True)
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if u.path == "/auth":
            self.send_response(302)
            self.send_header("Location", "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=x&redirect_uri=http%3A%2F%2Flocalhost%3A53682%2F&state=" + q["state"][0])
            self.end_headers(); return
        if q.get("state") != [STATE]:
            self.send_response(400); self.end_headers(); return
        self.send_response(200); self.end_headers(); self.wfile.write(b"Success!")
        tok = {"access_token": "AT-" + q["code"][0], "token_type": "Bearer", "refresh_token": "RT", "expiry": "2030-01-01T00:00:00Z"}
        print("Paste the following into your remote machine --->\n" + json.dumps(tok) + "\n<---End paste", flush=True)
        os._exit(0)
http.server.HTTPServer(("127.0.0.1", 53682), H).serve_forever()
'''


@pytest.fixture
def fake_rclone(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "rclone"
    exe.write_text(FAKE_RCLONE.replace("REAL", repr(shutil.which("rclone"))))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_ARGS_FILE", str(tmp_path / "args.jsonl"))
    seen = {"args_file": tmp_path / "args.jsonl"}

    def fake_drive(token):
        seen["token"] = token
        return {"id": "b!DRIVE", "driveType": "personal", "owner": {"user": {"displayName": "Alex Smith"}}}

    monkeypatch.setattr(cloudmod, "get_drive", fake_drive)
    monkeypatch.setattr(cloudmod, "google_user", lambda t: seen.update(g=t) or {"displayName": "Alex S", "emailAddress": "j@gmail.com"})
    monkeypatch.setattr(cloudmod, "dropbox_user", lambda t: seen.update(d=t) or {"name": {"display_name": "Alex Dropbox"}, "email": "j@x.com"})
    return seen


def test_sign_in_handoff(tmp_path, fake_rclone):
    od = OneDrive(tmp_path, Storage(tmp_path, PANEL))
    url = od.start_auth()
    assert url.startswith("https://login.microsoftonline.com/") and "state=st4te123" in url
    with pytest.raises(CloudError, match="doesn't look like"):
        od.finish_auth("hello")
    with pytest.raises(CloudError, match="access_denied|denied"):
        od.finish_auth("http://localhost:53682/?error=access_denied&error_description=The+user+denied")
    # what the browser lands on after signing in (it won't load on the PC; the user copies it)
    account = od.finish_auth("http://localhost:53682/?code=M.C123&state=st4te123")
    assert account["name"] == "Alex Smith" and fake_rclone["token"] == "AT-M.C123"
    conf = od.conf.read_text()
    assert "type = onedrive" in conf and "drive_id = b!DRIVE" in conf and '"refresh_token":"RT"' in conf
    assert oct(od.conf.stat().st_mode & 0o777) == "0o600"  # token file private
    assert od.connected and od.status()["account"]["name"] == "Alex Smith"
    od.disconnect()
    assert not od.connected


def test_token_pasted_from_pc(tmp_path, fake_rclone):
    od = OneDrive(tmp_path, Storage(tmp_path, PANEL))
    pasted = ('Paste the following into your remote machine --->\n'
              '{"access_token":"AT-pc","token_type":"Bearer","refresh_token":"R","expiry":"2030-01-01T00:00:00Z"}\n<---End paste')
    assert od.finish_auth(pasted)["name"] == "Alex Smith" and fake_rclone["token"] == "AT-pc"



def test_google_drive_and_dropbox_sign_in(tmp_path, fake_rclone):
    import base64
    od = OneDrive(tmp_path, Storage(tmp_path, PANEL))
    od.finish_auth("http://localhost:53682/?code=ONE&state=st4te123") if False else None
    url = od.start_auth("gdrive")
    assert "state=st4te123" in url
    acct = od.finish_auth("http://127.0.0.1:53682/?state=st4te123&code=GOOG")  # Google lands on 127.0.0.1
    assert acct["name"] == "Alex S" and fake_rclone["g"] == "AT-GOOG"
    args = [json.loads(l) for l in fake_rclone["args_file"].read_text().splitlines()]
    blob = args[-1][args[-1].index("drive") + 1]
    assert json.loads(base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))) == {"scope": "drive.readonly,drive.file"}
    od.start_auth("dropbox")
    assert od.finish_auth("http://localhost:53682/?code=DBX&state=st4te123")["name"] == "Alex Dropbox"
    conf = od._read_conf()
    assert conf.get("gdrive", "type") == "drive" and conf.get("gdrive", "scope") == "drive.readonly,drive.file"
    assert conf.get("dropbox", "type") == "dropbox" and "AT-DBX" in conf.get("dropbox", "token")
    st = od.status()
    assert {a["label"] for a in st["accounts"]} == {"Google Drive", "Dropbox"} and all(a["connected"] for a in st["accounts"])
    od.disconnect("gdrive")
    assert od.remotes() == ["dropbox"]
    with pytest.raises(CloudError):
        od.start_auth("smb")  # NAS doesn't use web sign-in


def test_own_google_key_is_used(tmp_path, fake_rclone):
    import base64
    od = OneDrive(tmp_path, Storage(tmp_path, PANEL))
    od.start_auth("gdrive", "123.apps.googleusercontent.com", "sekret")
    od.finish_auth("http://127.0.0.1:53682/?state=st4te123&code=K")
    args = [json.loads(l) for l in fake_rclone["args_file"].read_text().splitlines()][-1]
    blob = args[args.index("drive") + 1]
    assert json.loads(base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)))["client_id"] == "123.apps.googleusercontent.com"
    conf = od._read_conf()
    assert conf.get("gdrive", "client_id") == "123.apps.googleusercontent.com" and conf.get("gdrive", "client_secret") == "sekret"


def test_upgrade_from_single_onedrive(tmp_path):
    d = tmp_path / "cloud"
    d.mkdir()
    (d / "rclone.conf").write_text("[onedrive]\ntype = local\n")
    (d / "state.json").write_text(json.dumps({"account": {"name": "Alex Smith", "drive_type": "personal"},
                                              "links": [{"id": "abc", "folder": "Pictures/Wedding", "album": "Wedding",
                                                         "subfolders": True, "mirror": True, "every_hours": 1}]}))
    od = OneDrive(tmp_path, Storage(tmp_path, PANEL))
    st = od.status()
    assert st["accounts"][0]["name"] == "Alex Smith" and st["accounts"][0]["label"] == "OneDrive"
    assert st["links"][0]["remote"] == "onedrive" and st["links"][0]["source"] == "OneDrive"


# ---------------- NAS (real SMB server, if one is running locally) ----------------
def _smb_up():
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 445), timeout=1):
            return Path("/srv/photos").is_dir()
    except OSError:
        return False


@pytest.mark.skipif(not _smb_up(), reason="no local SMB server")
def test_nas_connect_browse_sync(env):
    frame, od, root, client = env
    with pytest.raises(CloudError, match="username or password"):
        od.add_nas("127.0.0.1", "frame", "wrong-password")
    assert "nas" not in od.remotes()  # a failed connection isn't kept
    # by name, not IP: the frame looks the name up itself and gives rclone the address
    r = client.post("/api/cloud/nas", json={"host": "smb://localhost/", "user": "frame", "password": "nas-pass-123",
                                            "label": "Synology"})
    assert r.status_code == 200 and r.get_json()["account"]["name"] == "Synology"
    conf = od.conf.read_text()
    assert "nas-pass-123" not in conf  # stored obscured, never in plain text
    assert "host = 127.0.0.1" in conf and od._account("nas")["host"] == "localhost"
    shares = client.get("/api/cloud/folders?remote=nas&path=").get_json()
    assert [s["name"] for s in shares] == ["Photos"]
    inside = client.get("/api/cloud/folders?remote=nas&path=Photos").get_json()
    assert {"Holidays", "Wedding"} <= {f["name"] for f in inside}
    link = client.post("/api/cloud/links", json={"remote": "nas", "folder": "Photos/Wedding", "album": "From NAS"}).get_json()
    link = wait_sync(od, link["id"])
    assert link["last_ok"] is True and link["files"] >= 1, link
    before = len(album_files(frame.storage, "From NAS"))
    extra = Path("/srv/photos/Wedding/NAS_extra.jpg")
    Image.new("RGB", (1600, 1200), (250, 100, 0)).save(extra)
    try:
        r = od.sync(link["id"])
        assert r["added"] == 1 and len(album_files(frame.storage, "From NAS")) == before + 1
    finally:
        extra.unlink()
    st = client.get("/api/cloud").get_json()
    assert any(a["provider"] == "smb" and a["connected"] for a in st["accounts"])
    assert [l["source"] for l in st["links"]] == ["Synology"]  # the name given to the NAS



def test_old_rclone_download_error_is_explained(monkeypatch):
    raw = ("2026/09/30 ERROR : IMG_0001.jpg: Failed to copy: failed to open source object: unauthenticated: Unauthenticated\n"
           "Failed to copy with 1 errors: last error was: failed to open source object: unauthenticated: Unauthenticated")
    monkeypatch.setattr(cloudmod, "rclone_version", lambda: (1, 60, 1))
    msg = cloudmod._friendly(raw)
    assert "too old" in msg and "1.60.1" in msg and "expired" not in msg
    monkeypatch.setattr(cloudmod, "rclone_version", lambda: (1, 71, 1))
    assert "refused" in cloudmod._friendly(raw)
    # a genuinely expired sign-in still says so
    assert "expired" in cloudmod._friendly('couldn\'t fetch token: invalid_grant: AADSTS70000: The provided grant has expired')


def test_errors_name_the_right_service():
    f = cloudmod._friendly
    # the bug you hit: a NAS problem was reported as OneDrive
    nas_dns = 'couldn\'t connect SMB: dial tcp: lookup mynas.local on 192.168.1.1:53: no such host'
    msg = f(nas_dns, "NAS", True, "mynas.local")
    assert "OneDrive" not in msg and "Couldn't find the NAS at mynas.local" in msg and "IP address" in msg
    assert "refused the connection" in f("couldn't connect SMB: dial tcp 192.168.1.20:445: connect: connection refused", "NAS", True)
    assert "switched on" in f("couldn't connect SMB: dial tcp 192.168.1.20:445: i/o timeout", "NAS", True)
    assert "shared folder doesn't exist" in f("response error: STATUS_BAD_NETWORK_NAME", "NAS", True)
    assert f("dial tcp: lookup www.googleapis.com: no such host", "Google Drive") == "Couldn't reach Google Drive. Is the frame online?"
    assert "Dropbox sign-in has expired" in f("couldn't fetch token: invalid_grant", "Dropbox")
    assert "That Google Drive folder can't be found" in f("error in ListJSON: directory not found", "Google Drive")


def test_nas_name_lookup(tmp_path, monkeypatch):
    assert cloudmod.resolve_host("192.168.1.20") == "192.168.1.20"
    assert cloudmod.resolve_host("localhost") == "127.0.0.1"
    assert cloudmod.resolve_host("no-such-nas.invalid") is None
    od = OneDrive(tmp_path, Storage(tmp_path, PANEL))
    with pytest.raises(CloudError, match="Couldn't find a NAS called 'no-such-nas.invalid'"):
        od.add_nas("no-such-nas.invalid", "frame", "x")
    assert od.remotes() == []


def test_status_reports_rclone_version(tmp_path, monkeypatch):
    monkeypatch.setattr(cloudmod, "rclone_version", lambda: (1, 60, 1))
    st = OneDrive(tmp_path, Storage(tmp_path, PANEL)).status()
    assert st["rclone_version"] == "1.60.1" and st["rclone_old_for_onedrive"] is True


# ---------------------------------------------------------------- saving backups
def test_backups_saved_through_rclone(tmp_path):
    od = OneDrive(tmp_path, Storage(tmp_path, PANEL))
    od._set_remote("dest", {"type": "local"})
    assert od.can_back_up("dest")[0]
    folder = str(tmp_path / "remote" / "Pi Frame backups")
    src = tmp_path / "b.tgz"
    src.write_bytes(os.urandom(300_000))
    with open(src, "rb") as f:
        assert od.backup_upload("dest", folder, "piframe-backup-2026-01-01-0300-full.tgz", f) == 300_000
    saved = Path(folder) / "piframe-backup-2026-01-01-0300-full.tgz"
    assert saved.read_bytes() == src.read_bytes()
    assert [x["Name"] for x in od.backup_list("dest", folder)] == [saved.name]
    od.backup_delete("dest", folder, saved.name)
    assert not saved.exists() and od.backup_list("dest", str(tmp_path / "nowhere")) == []


def test_google_drive_connected_read_only_cant_take_backups(tmp_path):
    od = OneDrive(tmp_path, Storage(tmp_path, PANEL))
    od._set_remote("gdrive", {"type": "drive", "scope": "drive.readonly", "token": "{}"})
    ok, why = od.can_back_up("gdrive")
    assert not ok and "sign in to Google Drive again" in why
    od._set_remote("gdrive", {"type": "drive", "scope": "drive.readonly,drive.file", "token": "{}"})
    assert od.can_back_up("gdrive")[0]


def test_after_a_restore_missing_synced_photos_download_again(tmp_path):
    st = Storage(tmp_path, PANEL)
    od = OneDrive(tmp_path, st)
    st.create_album("Wedding")
    od.state["links"] = [{"id": "l1", "remote": "onedrive", "folder": "W", "album": "Wedding", "every_hours": 1,
                          "last_sync": time.time()}]
    od._save_manifest("l1", {"a.jpg": {"size": 1, "mtime": "x"}, "b.jpg": {"size": 1, "mtime": "x"}})
    photo(tmp_path / "albums" / "Wedding" / f"{managed_stem('l1', 'a.jpg')}.jpg", (1, 2, 3), (40, 30))
    od.reconcile()
    assert list(od._manifest("l1")) == ["a.jpg"] and od.state["links"][0]["last_sync"] is None
