import base64
import io
import json
import os
import stat
import subprocess
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from photoframe import __version__, updater
from photoframe.config import ConfigStore
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.storage import Storage
from photoframe.web import create_app

from adminhelp import unlock

REPO = Path(__file__).resolve().parents[1]
AUTH = {"Authorization": "Basic " + base64.b64encode(b"alex:s3cret-pw").decode()}


def package(version="9.9.9", install="exit 0", extra=None, drop=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        files = {
            "pi-photoframe/photoframe/__init__.py": f'__version__ = "{version}"\n',
            "pi-photoframe/photoframe/__main__.py": "",
            "pi-photoframe/deploy/install.sh": f"#!/bin/bash\n{install}\n",
            **(extra or {}),
        }
        for name, body in files.items():
            if name not in drop:
                z.writestr(name, body)
    return buf.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    cfg = ConfigStore(tmp_path)
    frame = Frame(cfg, Storage(tmp_path, (800, 480)), FileDisplay((800, 480), str(tmp_path / "f.png")), tmp_path)
    return frame, create_app(frame).test_client(), tmp_path


def up(client, blob, headers=AUTH):
    return client.post("/api/maintenance/update/upload", data={"package": (io.BytesIO(blob), "pi-photoframe.zip")},
                       content_type="multipart/form-data", headers=headers)


def test_inspect_package(tmp_path):
    p = tmp_path / "p.zip"
    p.write_bytes(package("9.9.9"))
    info = updater.inspect_package(p)
    assert info == {"current": __version__, "new": "9.9.9", "direction": "upgrade"}
    p.write_bytes(package("0.0.1"))
    assert updater.inspect_package(p)["direction"] == "downgrade"
    bad_cases = {
        "not a zip": b"hello",
        "missing": package(drop=("pi-photoframe/deploy/install.sh",)),
        "traversal": package(extra={"pi-photoframe/../../etc/cron.d/x": "boom"}),
        "no version": package(extra={"pi-photoframe/photoframe/__init__.py": "x = 1\n"}),
    }
    for label, blob in bad_cases.items():
        p.write_bytes(blob)
        with pytest.raises(updater.UpdateError):
            updater.inspect_package(p)
    # symlinks are refused
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, b in [("pi-photoframe/photoframe/__init__.py", '__version__ = "1.0"\n'),
                     ("pi-photoframe/photoframe/__main__.py", ""), ("pi-photoframe/deploy/install.sh", "")]:
            z.writestr(n, b)
        info = zipfile.ZipInfo("pi-photoframe/evil")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "/etc/passwd")
    p.write_bytes(buf.getvalue())
    with pytest.raises(updater.UpdateError, match="symbolic link"):
        updater.inspect_package(p)


def test_updates_need_a_password(env):
    frame, client, data = env
    r = up(client, package(), headers={})
    assert r.status_code == 403 and "admin password" in r.get_json()["message"]
    assert client.get("/api/maintenance/update").get_json()["admin"] == "no-password"
    frame.cfg.update({"http_password": "s3cret-pw"})
    assert up(client, package(), headers={}).status_code == 401  # and the page itself is now protected
    frame.cfg.update({"admin_password": "admin-pass-1"})
    assert up(client, package()).status_code == 403  # the page password alone isn't enough
    unlock(client, frame, AUTH)
    r = up(client, package("9.9.9"))
    assert r.status_code == 200 and r.get_json()["new"] == "9.9.9"
    st = client.get("/api/maintenance/update", headers=AUTH).get_json()
    assert st["staged"]["new"] == "9.9.9" and st["password_set"] is True
    assert client.delete("/api/maintenance/update", headers=AUTH).status_code == 200
    assert client.get("/api/maintenance/update", headers=AUTH).get_json()["staged"] is None


def test_bad_upload_is_rejected_and_not_kept(env):
    frame, client, data = env
    frame.cfg.update({"http_password": "s3cret-pw"})
    unlock(client, frame, AUTH)
    r = up(client, package(extra={"pi-photoframe/../escape.sh": "x"}))
    assert r.status_code == 400 and "unsafe path" in r.get_json()["message"]
    assert not list((data / "update").glob("*.zip")) and not (data / "update" / "upload.tmp").exists()


def test_updater_not_installed_is_reported_and_package_kept(env, monkeypatch):
    frame, client, data = env
    frame.cfg.update({"http_password": "s3cret-pw"})
    unlock(client, frame, AUTH)
    up(client, package("9.9.9"))
    assert client.post("/api/maintenance/update/install", headers=AUTH).status_code == 200
    assert (data / "update" / "pending.zip").exists()
    t0 = updater.time.time()
    monkeypatch.setattr(updater.time, "time", lambda: t0 + 30)  # nobody picked it up
    st = client.get("/api/maintenance/update", headers=AUTH).get_json()
    assert st["state"] == "not-installed" and "install.sh" in st["message"]
    # taken back, so it can't install by surprise once the updater is set up later
    assert not (data / "update" / "pending.zip").exists() and st["staged"]["new"] == "9.9.9"


@pytest.mark.skipif(os.name != "posix", reason="needs bash")
def test_full_handoff_to_root_updater(env, tmp_path):
    """Web upload -> install -> the real update.sh (as the .path unit would run
    it) -> status shown back through the web API."""
    frame, client, data = env
    frame.cfg.update({"http_password": "s3cret-pw"})
    unlock(client, frame, AUTH)
    app_dir = tmp_path / "opt" / "pi-photoframe"
    (app_dir / "photoframe").mkdir(parents=True)
    (app_dir / "photoframe" / "__init__.py").write_text(f'__version__ = "{__version__}"\n')

    class H(BaseHTTPRequestHandler):  # stand-in for the restarted frame
        def log_message(self, *a):
            pass

        def do_GET(self):
            v = (app_dir / "photoframe" / "__init__.py").read_text().split('"')[1]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"version": v}).encode())

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    install = 'mkdir -p "$PF_APP_DIR" && cp -r "$(cd "$(dirname "$0")/.." && pwd)"/. "$PF_APP_DIR"/'
    assert up(client, package("9.9.9", install=install)).status_code == 200
    assert client.post("/api/maintenance/update/install", headers=AUTH).status_code == 200
    envv = dict(os.environ, PF_APP_DIR=str(app_dir), PF_DATA_DIR=str(data), PF_RESTART_CMD="true",
                PF_HEALTH_URL=f"http://127.0.0.1:{srv.server_port}/api/status", PF_HEALTH_WAIT="5")
    r = subprocess.run(["bash", str(REPO / "deploy" / "update.sh")], env=envv, capture_output=True, text=True, timeout=60)
    srv.shutdown()
    assert r.returncode == 0, r.stdout + r.stderr
    st = client.get("/api/maintenance/update", headers=AUTH).get_json()
    assert st["state"] == "done" and st["message"] == "Updated to version 9.9.9"
    assert "updating" in st["log"] and (app_dir / "photoframe" / "__init__.py").read_text().count("9.9.9")
    assert (tmp_path / "opt" / "pi-photoframe.previous").exists()  # backup kept for next time
