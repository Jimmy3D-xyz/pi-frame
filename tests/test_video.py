import io
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image

from photoframe import video
from photoframe.config import ConfigStore
from photoframe.display import FileDisplay
from photoframe.frame import Frame
from photoframe.storage import Storage
from photoframe.web import create_app

pytestmark = pytest.mark.skipif(not video.tools_available(), reason="ffmpeg not installed")
PANEL = (1920, 1080)
_sleep = time.sleep


def make_clip(path: Path, size="1280x720", rate=30, secs=2, hdr=False, rotate=0):
    if hdr:
        args = ["-c:v", "libx265", "-pix_fmt", "yuv420p10le",
                "-x265-params", "log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
                "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc"]
    else:
        args = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    tmp = path.with_suffix(".tmp" + path.suffix) if rotate else path
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(secs), "-shortest", *args,
                    "-c:a", "aac", str(tmp)], check=True)
    if rotate:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", str(rotate), "-i", str(tmp),
                        "-c", "copy", str(path)], check=True)
        tmp.unlink()
    return path


def jpeg(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (90, 30, 30)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("photoframe.frame.Frame._notify_ha", lambda *a, **k: None)
    cfg = ConfigStore(tmp_path)
    cfg.update({"transition": "none"})
    disp = FileDisplay(PANEL, str(tmp_path / "frame.png"))
    storage = Storage(tmp_path, PANEL)
    frame = Frame(cfg, storage, disp, tmp_path)
    frame.jobs = video.VideoJobs(storage, on_done=lambda p: frame.photos_added())
    return frame, create_app(frame).test_client(), tmp_path


def wait_jobs(client, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        jobs = client.get("/api/jobs").get_json()
        if jobs and all(j["status"] in ("done", "failed") for j in jobs):
            return jobs
        _sleep(0.2)
    raise AssertionError("jobs did not finish")


def upload(client, path: Path, album="Default"):
    with open(path, "rb") as f:
        return client.post("/api/upload", data={"album": album, "video": (f, path.name)},
                           content_type="multipart/form-data")


def test_plan_decisions(tmp_path):
    ok = video.probe(make_clip(tmp_path / "ok.mp4"))
    assert video.plan(ok, PANEL) == "copy" and ok.has_audio
    hdr = video.probe(make_clip(tmp_path / "hdr.mov", size="1280x720", hdr=True))
    assert hdr.hdr and video.plan(hdr, PANEL) == "convert"
    rot = video.probe(make_clip(tmp_path / "rot.mp4", size="1920x1080", rotate=90))
    assert (rot.width, rot.height) == (1080, 1920) and video.plan(rot, PANEL) == "copy"
    big = video.probe(make_clip(tmp_path / "big.mp4", size="2560x1440"))
    assert video.plan(big, PANEL) == "convert"


def test_upload_copy_and_convert(env, tmp_path):
    frame, client, data = env
    r = upload(client, make_clip(tmp_path / "Highlights.mp4", secs=3))
    assert r.status_code == 200 and r.get_json()["jobs"]
    r = upload(client, make_clip(tmp_path / "IMG_0042.MOV", size="1280x720", rate=60, hdr=True))
    assert r.status_code == 200
    jobs = wait_jobs(client)
    assert [j["status"] for j in jobs] == ["done", "done"], jobs
    items = {i["filename"]: i for i in client.get("/api/images?album=Default").get_json()}
    assert items["Highlights.mp4"]["type"] == "video" and items["Highlights.mp4"]["duration"] == pytest.approx(3, abs=0.2)
    out = video.probe(data / "albums/Default/IMG_0042.mp4")  # converted
    assert out.codec == "h264" and out.pix_fmt == "yuv420p" and not out.hdr and out.fps <= 31
    assert client.get("/api/image?filepath=Default/IMG_0042.mp4").mimetype == "image/jpeg"
    assert not list((data / "albums/Default/.incoming").iterdir())
    # delete cleans up poster + meta
    assert client.post("/api/delete", json={"filepath": "Default/IMG_0042.mp4"}).status_code == 200
    assert not (data / "albums/Default/.posters/IMG_0042.jpg").exists()


def test_bad_video_fails_cleanly(env, tmp_path):
    frame, client, data = env
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not really a video")
    assert upload(client, bad).status_code == 200
    jobs = wait_jobs(client)
    assert jobs[0]["status"] == "failed" and jobs[0]["message"]
    assert client.get("/api/images?album=Default").get_json() == []


def test_play_then_next_photo(env, tmp_path):
    frame, client, data = env
    client.post("/api/upload", data={"album": "Default", "image": (io.BytesIO(jpeg(1600, 1000)), "p.jpg")},
                content_type="multipart/form-data")
    upload(client, make_clip(tmp_path / "clip.mp4", secs=1))
    wait_jobs(client)
    r = client.post("/api/display", json={"filepath": "Default/clip.mp4"})
    assert r.status_code == 200
    st = client.get("/api/status").get_json()
    assert st["playing"]["filepath"] == "Default/clip.mp4"
    assert frame.state["current"]["source"] == "video"  # poster is up
    # the scheduler never interrupts a video
    assert frame.player.playing is not None
    # after it ends (simulated: ~1 s), the next photo appears
    for _ in range(60):
        if frame.player.playing is None and frame.state["current"].get("filepath") == "Default/p.jpg":
            break
        _sleep(0.1)
    assert frame.player.playing is None
    assert frame.state["current"]["filepath"] == "Default/p.jpg"


def test_next_stops_a_long_video(env, tmp_path):
    frame, client, data = env
    upload(client, make_clip(tmp_path / "long.mp4", secs=8))
    wait_jobs(client)
    client.post("/api/display", json={"filepath": "Default/long.mp4"})
    _sleep(0.3)
    assert frame.player.playing is not None
    t = time.time()
    r = client.post("/api/rotate")
    assert r.get_json()["message"] == "Stopped the video"
    for _ in range(30):
        if frame.player.playing is None:
            break
        _sleep(0.1)
    assert frame.player.playing is None and time.time() - t < 2


def test_videos_can_be_left_out_of_slideshow(env, tmp_path):
    frame, client, data = env
    upload(client, make_clip(tmp_path / "only.mp4", secs=1))
    wait_jobs(client)
    frame.cfg.update({"video_enabled": False})
    ok, msg = frame.rotate()
    assert not ok and frame.notice == "empty"
    frame.cfg.update({"video_enabled": True})
    ok, msg = frame.rotate()
    assert ok and msg == "Playing video"


def test_mpv_command_line(monkeypatch, tmp_path):
    """Check the arguments handed to mpv (sound, rotation, cover, length)."""
    captured = {}

    class FakeProc:
        returncode = 0

        def __init__(self, args, **kw):
            captured["args"] = args

        def communicate(self):
            return "", ""

        def poll(self):
            return 0

    monkeypatch.setattr(video.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(video.subprocess, "Popen", FakeProc)
    p = video.VideoPlayer(simulate=False)
    assert p.play(tmp_path / "v.mp4", rotate=90, cover=True, sound=False, volume=40, max_seconds=60, duration=90) == "finished"
    a = captured["args"]
    assert "--video-rotate=90" in a and "--panscan=1.0" in a and "--length=60" in a
    assert "--mute=yes" in a and "--ao=null" in a and "--hwdec=v4l2m2m,drm,v4l2m2m-copy" in a
    p.play(tmp_path / "v.mp4", rotate=0, cover=False, sound=True, volume=40, max_seconds=0, duration=90)
    a = captured["args"]
    assert "--mute=no" in a and "--volume=40" in a and not any(x.startswith("--length") for x in a)


def test_restart_mid_conversion_does_not_requeue_partial_output(tmp_path):
    storage = Storage(tmp_path, PANEL)
    inc = storage.root / "Default" / ".incoming"
    inc.mkdir(parents=True)
    src = make_clip(inc / "1a2b__IMG_1.mov", secs=1)
    (inc / "IMG_1.converting.mp4").write_bytes(b"half written")
    jobs = video.VideoJobs(storage)
    names = [j["name"] for j in jobs.list()]
    assert names == ["IMG_1.mov"] and not (inc / "IMG_1.converting.mp4").exists()


def test_hdmi_audio_device_detection(tmp_path):
    pi4 = tmp_path / "pi4"
    pi4.write_text(
        " 0 [Headphones     ]: bcm2835_headpho - bcm2835 Headphones\n"
        "                      bcm2835 Headphones\n"
        " 1 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0\n"
        "                      vc4-hdmi-0\n"
        " 2 [vc4hdmi1       ]: vc4-hdmi - vc4-hdmi-1\n"
        "                      vc4-hdmi-1\n")
    assert video.hdmi_audio_device(str(pi4)) == "alsa/hdmi:CARD=vc4hdmi0,DEV=0"
    zero = tmp_path / "zero"
    zero.write_text(" 0 [vc4hdmi        ]: vc4-hdmi - vc4-hdmi\n                      vc4-hdmi\n")
    assert video.hdmi_audio_device(str(zero)) == "alsa/hdmi:CARD=vc4hdmi,DEV=0"
    none = tmp_path / "none"
    none.write_text(" 0 [Headphones     ]: bcm2835_headpho - bcm2835 Headphones\n")
    assert video.hdmi_audio_device(str(none)) is None
    assert video.hdmi_audio_device(str(tmp_path / "missing")) is None


def test_mpv_gets_hdmi_audio_device(monkeypatch, tmp_path):
    captured = {}

    class FakeProc:
        returncode = 0

        def __init__(self, args, **kw):
            captured["args"] = args

        def communicate(self):
            return "", ""

        def poll(self):
            return 0

    monkeypatch.setattr(video.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(video.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(video, "hdmi_audio_device", lambda: "alsa/hdmi:CARD=vc4hdmi0,DEV=0")
    monkeypatch.delenv("PHOTOFRAME_AUDIO_DEVICE", raising=False)
    p = video.VideoPlayer(simulate=False)
    p.play(tmp_path / "v.mp4", rotate=0, cover=False, sound=True, volume=70, max_seconds=0, duration=5)
    assert "--audio-device=alsa/hdmi:CARD=vc4hdmi0,DEV=0" in captured["args"]
    monkeypatch.setenv("PHOTOFRAME_AUDIO_DEVICE", "alsa/sysdefault")
    p.play(tmp_path / "v.mp4", rotate=0, cover=False, sound=True, volume=70, max_seconds=0, duration=5)
    assert "--audio-device=alsa/sysdefault" in captured["args"]


def test_volume_boost_reaches_mpv(monkeypatch, tmp_path):
    captured = {}

    class FakeProc:
        returncode = 0

        def __init__(self, args, **kw):
            captured["args"] = args

        def communicate(self):
            return "", ""

        def poll(self):
            return 0

    monkeypatch.setattr(video.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(video.subprocess, "Popen", FakeProc)
    p = video.VideoPlayer(simulate=False)
    p.play(tmp_path / "v.mp4", rotate=0, cover=False, sound=True, volume=100, max_seconds=0, duration=5, boost_db=12)
    assert "--af=lavfi=[volume=12dB,alimiter=limit=0.89:level=disabled]" in captured["args"]
    p.play(tmp_path / "v.mp4", rotate=0, cover=False, sound=True, volume=100, max_seconds=0, duration=5, boost_db=0)
    assert not any(a.startswith("--af=") for a in captured["args"])
    # muted: no filter at all
    p.play(tmp_path / "v.mp4", rotate=0, cover=False, sound=False, volume=100, max_seconds=0, duration=5, boost_db=12)
    assert not any(a.startswith("--af=") for a in captured["args"])


@pytest.mark.skipif(shutil.which("mpv") is None, reason="mpv not installed")
def test_real_mpv_accepts_boost_filter(tmp_path):
    """Run the real player with the exact filter string, recording its audio."""
    clip = make_clip(tmp_path / "c.mp4", size="320x240", secs=1)
    out = tmp_path / "o.wav"
    r = subprocess.run(["mpv", "--no-config", "--no-terminal", "--vo=null", "--ao=pcm", f"--ao-pcm-file={out}",
                        "--af=lavfi=[volume=12dB,alimiter=limit=0.89:level=disabled]", str(clip)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert out.stat().st_size > 10_000


def test_boost_setting_validation(tmp_path):
    cfg = ConfigStore(tmp_path)
    assert cfg.get("video_volume") == 100 and cfg.get("video_boost_db") == 0
    cfg.update({"video_boost_db": 6})
    with pytest.raises(Exception):
        cfg.update({"video_boost_db": 30})


# ---------------- sound levelling ----------------
def make_sound_clip(path: Path, level_db: float, secs=4):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10",
                    "-f", "lavfi", "-i", f"anoisesrc=d={secs}:a=0.5", "-t", str(secs),
                    "-af", f"volume={level_db}dB", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-b:a", "192k", str(path)], check=True)
    return path


def test_measure_and_gain(tmp_path):
    quiet = video.measure_loudness(make_sound_clip(tmp_path / "q.mp4", -30))
    loud = video.measure_loudness(make_sound_clip(tmp_path / "l.mp4", 0))
    assert quiet["lufs"] < -30 < loud["lufs"] and "peak_db" in quiet
    assert video.normalise_gain(quiet) == pytest.approx(min(20, -16 - quiet["lufs"]), abs=0.1)
    assert video.normalise_gain(loud) < 0  # loud clips are turned down
    assert video.normalise_gain({"lufs": -70.0}) == 0.0  # silence isn't amplified
    silent = make_clip(tmp_path / "s.mp4", secs=1)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(silent), "-an", "-c", "copy", str(tmp_path / "na.mp4")], check=True)
    assert video.measure_loudness(tmp_path / "na.mp4") == {}


def test_import_reports_sound_level(env, tmp_path):
    frame, client, data = env
    upload(client, make_sound_clip(tmp_path / "Quiet.mp4", -28))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10", "-t", "1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp_path / "Mute.mp4")], check=True)
    upload(client, tmp_path / "Mute.mp4")
    jobs = {j["name"]: j for j in wait_jobs(client)}
    assert jobs["Quiet.mp4"]["status"] == "done" and jobs["Quiet.mp4"]["message"].startswith("Sound evened out (+")
    assert jobs["Mute.mp4"]["message"] == "No soundtrack"
    items = {i["filename"]: i for i in client.get("/api/images?album=Default").get_json()}
    assert items["Quiet.mp4"]["sound"].startswith("Sound evened out") and items["Mute.mp4"]["sound"] == "No soundtrack"
    meta = frame.storage.video_meta(data / "albums/Default/Quiet.mp4")
    assert meta["gain_db"] > 5 and meta["duration"] > 0  # levelling didn't wipe the other details


def test_existing_videos_are_measured_in_background(tmp_path):
    storage = Storage(tmp_path, PANEL)
    old = make_sound_clip(storage.root / "Default" / "Highlights.mp4", -26)
    storage.update_video_meta(old, {"duration": 4.0})  # imported before levelling existed
    video.VideoJobs(storage)
    for _ in range(100):
        if "lufs" in storage.video_meta(old):
            break
        _sleep(0.1)
    meta = storage.video_meta(old)
    assert meta["gain_db"] > 5 and meta["duration"] == 4.0


def test_level_and_boost_combine_in_mpv_args(monkeypatch, tmp_path):
    captured = {}

    class FakeProc:
        returncode = 0

        def __init__(self, args, **kw):
            captured["args"] = args

        def communicate(self):
            return "", ""

        def poll(self):
            return 0

    monkeypatch.setattr(video.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(video.subprocess, "Popen", FakeProc)
    p = video.VideoPlayer(simulate=False)
    kw = dict(rotate=0, cover=False, sound=True, volume=100, max_seconds=0, duration=5)
    p.play(tmp_path / "v.mp4", gain_db=8.5, boost_db=6, **kw)
    assert "--af=lavfi=[volume=14.5dB,alimiter=limit=0.89:level=disabled]" in captured["args"]
    p.play(tmp_path / "v.mp4", gain_db=-5.1, boost_db=0, **kw)
    assert "--af=lavfi=[volume=-5.1dB]" in captured["args"]  # turning down needs no limiter
    p.play(tmp_path / "v.mp4", gain_db=0.0, boost_db=0, **kw)
    assert not any(a.startswith("--af=") for a in captured["args"])


@pytest.mark.skipif(shutil.which("mpv") is None, reason="mpv not installed")
def test_quiet_video_plays_near_target_loudness(tmp_path):
    """End to end with the real player: a quiet clip, levelled, should come
    out close to the target and never clip."""
    clip = make_sound_clip(tmp_path / "quiet.mp4", -24, secs=6)
    gain = video.normalise_gain(video.measure_loudness(clip))
    out = tmp_path / "o.wav"
    subprocess.run(["mpv", "--no-config", "--no-terminal", "--vo=null", "--ao=pcm", f"--ao-pcm-file={out}",
                    f"--af=lavfi=[volume={gain}dB,alimiter=limit=0.89:level=disabled]", str(clip)],
                   check=True, capture_output=True, timeout=60)
    result = video.measure_loudness(out)
    assert abs(result["lufs"] - video.TARGET_LUFS) < 2.5, result
    assert result["peak_db"] < 0, result


def test_videos_capped_at_what_the_hardware_decodes():
    from photoframe import video

    def clip(w, h):
        return video.Probe(duration=10.0, width=w, height=h, codec="h264", pix_fmt="yuv420p", profile="High",
                           transfer="bt709", fps=30.0, rotation=0, has_audio=True)

    # on the developer's 1440p screen, 1440p H.264 used to be kept as it was: the Pi 4 can't decode that in hardware
    assert video.plan(clip(2560, 1440), (2560, 1440)) == "convert"
    assert video.plan(clip(1920, 1080), (2560, 1440)) == "copy"
    assert video.plan(clip(1080, 1920), (1440, 2560)) == "copy"  # portrait phone video
    assert video.too_big_for_hw(2560, 1440) and video.too_big_for_hw(1440, 2560)
    assert not video.too_big_for_hw(1920, 1080) and not video.too_big_for_hw(1080, 1920)


@pytest.mark.skipif(not video.tools_available(), reason="ffmpeg not installed")
def test_oversized_stored_video_is_redone_at_1080p(tmp_path, monkeypatch):
    import subprocess
    import time as _t
    storage = Storage(tmp_path, (2560, 1440))
    storage.create_album("Default")
    big = storage.root / "Default" / "party.mp4"  # stored by an earlier version at the screen's 1440p
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=2560x1440:rate=25", "-t", "1",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(big)], check=True)
    monkeypatch.setattr(video.VideoJobs, "REDO_DELAY", 0)  # don't wait for start-up
    jobs = video.VideoJobs(storage)
    assert jobs.oversized() == [big]
    jobs._redo_oversized()  # a second check while the first is queued: not queued twice
    for _ in range(600):
        done = [j for j in jobs.list() if j["status"] in ("done", "failed")]
        if done:
            break
        _t.sleep(0.1)
    assert done and done[0]["status"] == "done", jobs.list()
    assert len(jobs.list()) == 1  # queued once
    p = video.probe(storage.root / "Default" / "party.mp4")  # same name, so links and order are kept
    assert (p.width, p.height) == (1920, 1080)
    assert jobs.oversized() == []  # nothing left to redo


def test_mpv_messages_summarised():
    from pathlib import Path as _P
    # real mpv output from a machine without the Pi's decoder (it falls back to software)
    text = (_P(__file__).parent / "data" / "mpv_software.txt").read_text()
    decoder, warnings = video.summarise_mpv(text)
    assert decoder == "software" and warnings == []  # the probing noise is filtered out
    # what the Pi prints when its decoder works, plus the CUDA line the developer saw, plus a real warning
    pi = ("Cannot load libcuda.so.1\n[vd] Looking at hwdec h264_v4l2m2m-v4l2m2m...\n"
          "[vd] Using hardware decoding (v4l2m2m).\nAudio device underrun detected.\n"
          # the two harmless lines the developer saw on his Pi
          "[vo/gpu/drm] Can't open TTY for VT control: No such device or address\n"
          "[vo/gpu/drm] Failed to set up VT switcher. Terminal switching will be unavailable.\n")
    decoder, warnings = video.summarise_mpv(pi)
    assert decoder == "hardware (v4l2m2m)" and warnings == ["Audio device underrun detected."]


def test_screen_messages_are_kept_apart_and_handover_noise_dropped():
    text = """[vo/gpu/drm] Connector name: HDMI-A-1
[vo/gpu/drm] Mode: 2560x1440@59.95 (setting 0)
[vo/gpu] Using DRM plane 91 for drawing
[ipc_0] Write error (Broken pipe)
client removed during hook handling
[vd] Using hardware decoding (v4l2m2m).
"""
    decoder, warnings = video.summarise_mpv(text)
    assert decoder == "hardware (v4l2m2m)" and warnings == []
    assert video.screen_lines(text) == ["[vo/gpu/drm] Connector name: HDMI-A-1",
                                        "[vo/gpu/drm] Mode: 2560x1440@59.95 (setting 0)",
                                        "[vo/gpu] Using DRM plane 91 for drawing"]
