"""Updates from GitHub, against a stand-in for GitHub's API."""
import datetime as dt
import hashlib
import http.server
import json
import threading
import time

import pytest

from photoframe import __version__, autoupdate
from photoframe.autoupdate import AutoUpdate, AutoUpdateError
from photoframe.config import ConfigStore
from photoframe.updater import Updates

from adminhelp import unlock

from test_updater import package

REPO = "Jimmy3D-xyz/pi-frame"


class FakeGitHub:
    def __init__(self):
        self.releases = []  # newest first; GitHub's /latest skips pre-releases
        self.assets = {}
        self.public = True
        self.requests = []
        gh = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                gh.requests.append((self.path, self.headers.get("Authorization"), self.headers.get("Accept")))
                if not gh.public:  # GitHub answers "not found" for a repository it can't show
                    return self.send(404, {"message": "Not Found"})
                if self.path == f"/repos/{REPO}":
                    return self.send(200, {"full_name": REPO, "private": not gh.public})
                if self.path == f"/repos/{REPO}/releases/latest":
                    rel = next((r for r in gh.releases if not r.get("prerelease")), None)
                    return self.send(200, rel) if rel else self.send(404, {"message": "Not Found"})
                if self.path.startswith(f"/repos/{REPO}/releases/assets/"):
                    blob = gh.assets.get(int(self.path.rsplit("/", 1)[1]))
                    if blob is None or self.headers.get("Accept") != "application/octet-stream":
                        return self.send(404, {"message": "Not Found"})
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(blob)))
                    self.end_headers()
                    self.wfile.write(blob)
                    return None
                return self.send(404, {"message": "Not Found"})

            def send(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def release(self, version, blob=None, prerelease=False, digest=True, asset_id=None):
        asset_id = asset_id or len(self.assets) + 1
        blob = blob if blob is not None else package(version)
        self.assets[asset_id] = blob
        self.releases.insert(0, {
            "tag_name": f"v{version}", "name": f"Pi Frame {version}", "body": f"What's new in {version}",
            "html_url": f"https://github.com/{REPO}/releases/tag/v{version}", "prerelease": prerelease,
            "published_at": "2026-11-20T00:00:00Z",
            "assets": [{"id": asset_id, "name": "pi-photoframe.zip", "size": len(blob),
                        **({"digest": "sha256:" + hashlib.sha256(blob).hexdigest()} if digest else {})},
                       {"id": 999, "name": "pi-frame-x.img.xz", "size": 1}]})


class FakeFrame:
    def __init__(self):
        self.began = []

    def begin_update(self, version, status_fn=None):
        self.began.append(version)


@pytest.fixture
def gh(tmp_path):
    g = FakeGitHub()
    yield g
    g.server.shutdown()


@pytest.fixture
def au(tmp_path, gh):
    cfg = ConfigStore(tmp_path)
    frame = FakeFrame()
    a = AutoUpdate(tmp_path, cfg, Updates(tmp_path), frame, api=gh.url)
    a.frame_ = frame
    return a


def newer(step=1):
    parts = [int(x) for x in __version__.split(".")]
    parts[-1] += step
    return ".".join(map(str, parts))


def test_no_token_is_sent_and_an_old_one_is_deleted(gh, tmp_path):
    (tmp_path / "github-token").write_text("github_pat_" + "A1b2" * 10)
    a = AutoUpdate(tmp_path, ConfigStore(tmp_path), Updates(tmp_path), FakeFrame(), api=gh.url)
    assert not (tmp_path / "github-token").exists()  # saved when the repository was private
    gh.release(newer())
    assert a.check()["available"]["version"] == newer()
    assert all(auth is None for _, auth, _ in gh.requests)


def test_no_releases_yet(au, gh):
    st = au.check()
    assert st["available"] is None and st["latest"] is None and st["error"] is None


def test_a_missing_repository_is_reported(au, gh):
    gh.public = False
    with pytest.raises(AutoUpdateError, match="Couldn't find"):
        au.check()
    assert au.health()["level"] == "warn" and au.status()["error"]


def test_finds_a_newer_release_and_ignores_test_builds(au, gh):
    gh.release(__version__)
    assert au.check()["available"] is None and au.status()["latest"] == __version__
    gh.release(newer(2), prerelease=True)  # a test build: not offered
    assert au.check()["available"] is None
    gh.release(newer(1))
    st = au.check()
    assert st["available"]["version"] == newer(1) and "What's new" in st["available"]["notes"]
    assert au.health()["text"].startswith(f"Version {newer(1)} is available")


def test_install_downloads_checks_and_hands_over(au, gh, tmp_path):
    gh.release(newer())
    au.check()
    info = au.install()
    assert info["new"] == newer() and (tmp_path / "update" / "pending.zip").exists()
    assert au.frame_.began == [newer()]
    assert not list((tmp_path / "update").glob("github-*.zip"))  # the download is tidied away
    path, auth, accept = gh.requests[-1]
    assert path.endswith("/releases/assets/1") and accept == "application/octet-stream"


def test_a_download_that_doesnt_match_the_checksum_is_refused(au, gh, tmp_path):
    gh.release(newer())
    au.check()
    gh.assets[1] = package(newer()) + b"tampered"
    with pytest.raises(AutoUpdateError, match="checksum"):
        au.install()
    assert not (tmp_path / "update" / "pending.zip").exists() and not (tmp_path / "update" / "staged.zip").exists()


def test_a_release_without_a_proper_package_is_refused(au, gh, tmp_path):
    gh.release(newer(), blob=b"not a zip", digest=False)
    au.check()
    with pytest.raises(AutoUpdateError, match="isn't right"):
        au.install()
    assert not (tmp_path / "update" / "pending.zip").exists()


def test_automatic_install_window_and_no_retry_after_a_failure(au, gh, tmp_path):
    gh.release(newer())
    au.check()
    night = dt.datetime(2026, 11, 21, 3, 45)
    assert not au.due_to_install(night)  # "tell me" by default
    au.cfg.update({"update_auto": "install", "update_time": "03:30"})
    assert au.due_to_install(night) and not au.due_to_install(dt.datetime(2026, 11, 21, 14, 0))
    au.install(auto=True)
    # the frame restarted, still on the old version: the update didn't take
    again = AutoUpdate(tmp_path, au.cfg, Updates(tmp_path), None, api=gh.url)
    assert newer() in again.state["failed"] and not again.due_to_install(night)
    assert again.status()["failed"]


def test_web_endpoints(tmp_path, gh, monkeypatch):
    import base64

    from photoframe.display import FileDisplay
    from photoframe.frame import Frame
    from photoframe.storage import Storage
    from photoframe.web import create_app

    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    monkeypatch.setattr(autoupdate, "API", gh.url)
    cfg = ConfigStore(tmp_path)
    frame = Frame(cfg, Storage(tmp_path, (800, 480)), FileDisplay((800, 480), str(tmp_path / "f.png")), tmp_path)
    client = create_app(frame).test_client()
    assert client.post("/api/maintenance/update/github/check").status_code == 403  # an admin option
    cfg.update({"http_password": "pagepass1"})
    auth = {"Authorization": "Basic " + base64.b64encode(b"x:pagepass1").decode()}
    unlock(client, frame, auth)
    gh.release(newer())
    r = client.post("/api/maintenance/update/github/check", headers=auth)
    assert r.status_code == 200 and r.get_json()["available"]["version"] == newer()
    m = client.get("/api/maintenance", headers=auth).get_json()
    assert any(x["area"] == "Updates" for x in m["summary"])
    r = client.post("/api/maintenance/update/github/install", headers=auth)
    assert r.status_code == 200 and (tmp_path / "update" / "pending.zip").exists()
    assert client.post("/api/maintenance/update/github/token", json={"token": "x"}, headers=auth).status_code in (404, 405)


def test_once_installed_it_isnt_offered_again(au, gh, tmp_path, monkeypatch):
    gh.release(newer())
    assert au.check()["available"]["version"] == newer()
    # the frame restarts on the new version, with the note still saved
    monkeypatch.setattr(autoupdate, "__version__", newer())
    again = AutoUpdate(tmp_path, au.cfg, Updates(tmp_path), None, api=gh.url)
    st = again.status()
    assert st["available"] is None and st["latest"] == newer() and again.health() is None


def test_frames_follow_the_repository_to_its_new_home(tmp_path):
    import json
    from photoframe.config import ConfigStore
    cfg = ConfigStore(tmp_path)
    cfg.update({"update_repo": "JimmySte/pi-frame"})
    raw = json.loads((tmp_path / "config.json").read_text())
    assert raw["config"]["update_repo"] == "JimmySte/pi-frame"
    assert ConfigStore(tmp_path).get("update_repo") == "Jimmy3D-xyz/pi-frame"
