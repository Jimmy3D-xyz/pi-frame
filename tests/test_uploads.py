import io
import subprocess
import time
from pathlib import Path

import pytest

from photoframe import video
from photoframe.config import ConfigStore
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.storage import Storage
from photoframe.uploads import UploadError, Uploads
from photoframe.web import create_app

PANEL = (1920, 1080)
_sleep = time.sleep


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    cfg = ConfigStore(tmp_path)
    storage = Storage(tmp_path, PANEL)
    frame = Frame(cfg, storage, FileDisplay(PANEL, str(tmp_path / "f.png")), tmp_path)
    if video.tools_available():
        frame.jobs = video.VideoJobs(storage, on_done=lambda p: None)
    return frame, create_app(frame).test_client(), tmp_path


def test_resume_and_retry_logic(tmp_path):
    storage = Storage(tmp_path, PANEL)
    up = Uploads(storage, tmp_path)
    data = bytes(range(256)) * 4096  # 1 MB
    s = up.start("Default", "Highlights.mp4", len(data), 111)
    assert s["received"] == 0
    assert up.append(s["id"], 0, io.BytesIO(data[:300_000]), 300_000) == 300_000
    # same file chosen again after a refresh: same session, resumes at 300 000
    s2 = up.start("Default", "Highlights.mp4", len(data), 111)
    assert s2["id"] == s["id"] and s2["received"] == 300_000
    # a retried chunk that had already arrived -> 409 with the real position
    with pytest.raises(UploadError) as e:
        up.append(s["id"], 0, io.BytesIO(data[:300_000]), 300_000)
    assert e.value.code == 409 and e.value.received == 300_000
    # connection drops mid-chunk: partial data is rolled back
    with pytest.raises(UploadError):
        up.append(s["id"], 300_000, io.BytesIO(data[300_000:400_000]), 200_000)
    assert up.start("Default", "Highlights.mp4", len(data), 111)["received"] == 300_000
    # can't finish early
    with pytest.raises(UploadError) as e:
        up.finish(s["id"])
    assert e.value.code == 409
    assert up.pending()[0]["received"] == 300_000
    up.append(s["id"], 300_000, io.BytesIO(data[300_000:]), len(data) - 300_000)
    album, dest = up.finish(s["id"])
    assert album == "Default" and dest.read_bytes() == data and dest.parent.name == ".incoming"
    assert up.pending() == [] and not list((tmp_path / "uploads").iterdir())
    # a different file (other size) is a different session
    assert up.start("Default", "Highlights.mp4", 5, 111)["id"] != s["id"]


def test_rejects_bad_input(tmp_path, monkeypatch):
    up = Uploads(Storage(tmp_path, PANEL), tmp_path)
    with pytest.raises(UploadError):
        up.start("Default", "notes.txt", 10, 1)
    with pytest.raises(UploadError):
        up.start("../etc", "a.mp4", 10, 1)
    with pytest.raises(UploadError) as e:
        up.append("../../x", 0, io.BytesIO(b"x"), 1)
    assert e.value.code == 404
    monkeypatch.setattr(up, "_free_bytes", lambda: 100 * 1024 * 1024)
    with pytest.raises(UploadError) as e:
        up.start("Default", "big.mp4", 2 * 1024**3, 1)
    assert e.value.code == 507 and "not enough space" in str(e.value)


@pytest.mark.skipif(not video.tools_available(), reason="ffmpeg not installed")
def test_chunked_upload_end_to_end(env, tmp_path):
    frame, client, data_dir = env
    clip = tmp_path / "Final.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                    "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)
    blob = clip.read_bytes()
    r = client.post("/api/upload/start", json={"album": "Default", "filename": "Final.mp4",
                                               "size": len(blob), "last_modified": 42})
    uid = r.get_json()["id"]
    step = 100_000
    off = 0
    while off < len(blob):
        chunk = blob[off:off + step]
        r = client.put(f"/api/upload/{uid}?offset={off}", data=chunk,
                       headers={"Content-Type": "application/octet-stream"})
        assert r.status_code == 200, r.get_json()
        off = r.get_json()["received"]
    r = client.post(f"/api/upload/{uid}/finish")
    assert r.status_code == 200 and r.get_json()["jobs"]
    for _ in range(100):
        jobs = client.get("/api/jobs").get_json()
        if jobs and jobs[0]["status"] in ("done", "failed"):
            break
        _sleep(0.1)
    assert jobs[0]["status"] == "done", jobs
    items = client.get("/api/images?album=Default").get_json()
    assert items[0]["filename"] == "Final.mp4" and items[0]["type"] == "video"
    # compatible H.264 was stored as-is: byte-identical, no conversion
    assert (data_dir / "albums/Default/Final.mp4").read_bytes() == blob
    assert client.get("/api/uploads/pending").get_json() == []
