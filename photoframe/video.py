"""Video support: probe uploads, convert the ones the Pi can't play well,
make poster frames, and play videos full screen with mpv.

Playback: mpv draws straight to the HDMI output through DRM/KMS and the Pi 4's
hardware H.264 decoder. While it runs it owns the screen; when it exits the
kernel restores the framebuffer, so the frame carries on where it left off.

Conversion rules (see plan()):
- H.264, 8-bit 4:2:0, SDR, no larger than the screen needs -> stored as is
- anything else (iPhone HEVC, HDR/Dolby Vision, 4K, 10-bit...) -> converted
  in the background to 1080p-class H.264 + AAC, with HDR tone-mapped to SDR.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from . import render

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".3gp"}
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
COPY_PIX_FMTS = {"yuv420p", "yuvj420p"}


def is_video(path: Path | str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTS


def tools_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# ---------------------------------------------------------------- probing
@dataclass
class Probe:
    duration: float
    width: int  # as displayed (rotation applied)
    height: int
    codec: str
    pix_fmt: str
    profile: str
    transfer: str
    fps: float
    rotation: int
    has_audio: bool

    @property
    def hdr(self) -> bool:
        return self.transfer in HDR_TRANSFERS


def probe(path: Path) -> Probe:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if r.returncode != 0:
        raise ValueError(f"not a video ffmpeg can read: {r.stderr.strip()[:200]}")
    info = json.loads(r.stdout or "{}")
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    if v is None:
        raise ValueError("file has no video stream")
    rotation = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    if not rotation and "rotate" in (v.get("tags") or {}):
        rotation = -int(v["tags"]["rotate"])
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    if abs(rotation) % 180 == 90:
        w, h = h, w
    try:
        num, den = (v.get("avg_frame_rate") or "0/1").split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    duration = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    return Probe(
        duration=duration, width=w, height=h,
        codec=v.get("codec_name", ""), pix_fmt=v.get("pix_fmt", ""), profile=v.get("profile", ""),
        transfer=v.get("color_transfer", ""), fps=fps, rotation=rotation,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


# The Pi 4's hardware H.264 decoder handles up to 1920x1080 (1088 coded).
# Bigger videos fall back to the CPU, which can't keep up, so videos are kept
# at most this size whatever the screen; the GPU scales them up to fill it.
HW_LONG, HW_SHORT = 1920, 1088


def too_big_for_hw(width: int, height: int) -> bool:
    return max(width, height) > HW_LONG or min(width, height) > HW_SHORT


def plan(p: Probe, panel: tuple[int, int]) -> str:
    """'copy' if the Pi can play the file well as it is, else 'convert'."""
    long_edge, short_edge = HW_LONG, HW_SHORT
    if (
        p.codec == "h264"
        and p.pix_fmt in COPY_PIX_FMTS
        and not p.hdr
        and "10" not in p.profile
        and max(p.width, p.height) <= long_edge
        and min(p.width, p.height) <= short_edge
        and p.fps <= 61
    ):
        return "copy"
    return "convert"


# ---------------------------------------------------------------- loudness
TARGET_LUFS = -16.0  # typical phone/streaming level
MAX_GAIN_DB = 20.0
MIN_GAIN_DB = -15.0
SILENT_LUFS = -60.0


def measure_loudness(path: Path) -> dict:
    """EBU R128 loudness of the soundtrack. Reads audio only, so it's quick.
    Returns {} when there's no soundtrack."""
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-i", str(path), "-vn",
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        capture_output=True, text=True, timeout=1800, check=False,
    )
    text = r.stderr.split("Summary:")[-1] if "Summary:" in r.stderr else ""
    out: dict = {}
    for line in text.splitlines():
        parts = line.split()
        try:
            if parts[:1] == ["I:"]:
                out["lufs"] = float(parts[1])
            elif parts[:1] == ["Peak:"]:
                out["peak_db"] = float(parts[1])
        except (IndexError, ValueError):
            pass
    return out


def normalise_gain(loudness: dict) -> float:
    """Gain (dB) that brings a soundtrack to TARGET_LUFS, within sane limits."""
    lufs = loudness.get("lufs")
    if lufs is None or lufs <= SILENT_LUFS:
        return 0.0
    return round(max(MIN_GAIN_DB, min(MAX_GAIN_DB, TARGET_LUFS - lufs)), 1)


def describe_sound(meta: dict) -> str:
    if meta.get("has_audio") is False:
        return "No soundtrack"
    lufs = meta.get("lufs")
    if lufs is None:
        return ""
    if lufs <= SILENT_LUFS:
        return "Soundtrack is silent"
    gain = meta.get("gain_db", 0.0)
    if abs(gain) < 1.5:
        return "Sound level already good"
    return f"Sound evened out ({gain:+.0f} dB)"


def analyse_sound(path: Path, storage) -> dict:
    """Measure a video's loudness and store it with the video's details."""
    meta = storage.video_meta(path)
    try:
        p = probe(path)
    except ValueError:
        return meta
    if not p.has_audio:
        meta.update(has_audio=False, lufs=None, gain_db=0.0)
    else:
        loud = measure_loudness(path)
        meta.update(has_audio=True, lufs=loud.get("lufs", SILENT_LUFS), peak_db=loud.get("peak_db"),
                    gain_db=normalise_gain(loud or {"lufs": SILENT_LUFS}))
    storage.update_video_meta(path, meta)
    return meta


def _has_filter(name: str) -> bool:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, check=False)
    return f" {name} " in r.stdout


def convert_args(src: Path, dst: Path, p: Probe, panel: tuple[int, int]) -> list[str]:
    long_edge = HW_LONG  # the hardware decoder's limit, not the screen's size
    # Scale the long edge down to fit; never up. -2 keeps the height/width even.
    scale = (
        f"scale=w='if(gte(iw,ih),min({long_edge},iw),-2)':h='if(gte(iw,ih),-2,min({long_edge},ih))'"
        ":flags=lanczos"
    )
    # Cheapest order: drop frames and shrink first, then tone-map the smaller
    # frames (tone mapping in float is the slowest step by far).
    filters = []
    if p.fps > 31:
        filters.append("fps=30")  # halves the work for 60 fps phone clips
    filters.append(scale)
    if p.hdr and _has_filter("zscale") and _has_filter("tonemap"):
        filters += [
            "zscale=t=linear:npl=100",
            "format=gbrpf32le",
            "zscale=p=bt709",
            "tonemap=tonemap=hable:desat=0",
            "zscale=t=bt709:m=bt709:r=tv",
        ]
    filters.append("format=yuv420p")
    args = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", ",".join(filters),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high", "-level", "4.1",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-c:a", "aac", "-b:a", "160k", "-ac", "2",
        "-movflags", "+faststart", "-map_metadata", "-1",
        "-progress", "pipe:1", "-nostats",
        str(dst),
    ]
    return args


def make_poster(video: Path, poster: Path, thumb: Path, duration: float, panel: tuple[int, int]) -> None:
    at = min(1.0, duration / 3) if duration else 0
    tmp = poster.with_name(poster.stem + ".tmp.jpg")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-ss", f"{at:.2f}", "-i", str(video),
         "-frames:v", "1", "-q:v", "3", str(tmp)],
        capture_output=True, timeout=120, check=False,
    )
    if not tmp.exists():
        img = Image.new("RGB", (640, 360), (27, 31, 30))
    else:
        img = render.open_image(tmp)
        tmp.unlink(missing_ok=True)
    render.store_copy(img, panel).save(poster, "JPEG", quality=88)
    thumb.write_bytes(render.thumbnail_jpeg(img))


# ---------------------------------------------------------------- jobs
@dataclass
class Job:
    id: str
    album: str
    name: str
    src: Path
    status: str = "queued"  # queued | checking | converting | done | failed
    stem: str | None = None  # fixed output name (synced files); None = from the file name
    progress: float = 0.0
    message: str = ""
    started: float = field(default_factory=time.time)

    def public(self) -> dict:
        return {
            "id": self.id, "album": self.album, "name": self.name, "status": self.status,
            "progress": round(self.progress, 3), "message": self.message,
        }


class VideoJobs:
    """One background worker, so conversions don't compete with each other or
    starve the slideshow. Runs ffmpeg at the lowest CPU priority."""

    def __init__(self, storage, on_done=None):
        self.storage = storage
        self.on_done = on_done or (lambda path: None)
        self.jobs: dict[str, Job] = {}
        self._q: queue.Queue[Job] = queue.Queue()
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, name="video-jobs", daemon=True).start()
        self._requeue_incoming()
        threading.Thread(target=self._backfill_sound, name="video-sound", daemon=True).start()
        threading.Thread(target=self._redo_oversized, name="video-resize", daemon=True).start()

    def oversized(self) -> list[Path]:
        """Stored videos too big for the hardware decoder (from before the cap)."""
        found = []
        for album_dir in sorted(self.storage.root.iterdir()):
            if not album_dir.is_dir() or album_dir.name.startswith("."):
                continue
            for v in sorted(album_dir.iterdir()):
                if not (is_video(v) and v.is_file()):
                    continue
                meta = self.storage.video_meta(v)
                w, h = meta.get("width"), meta.get("height")
                if not (w and h):
                    try:
                        pr = probe(v)
                        w, h = pr.width, pr.height
                    except Exception:  # noqa: BLE001
                        continue
                if too_big_for_hw(int(w), int(h)):
                    found.append(v)
        return found

    REDO_DELAY = 30  # seconds after start-up, so the frame starts up first

    def _redo_oversized(self) -> None:
        """Convert those down to 1080p in the background; the old file stays
        playable until its replacement is ready."""
        time.sleep(self.REDO_DELAY)
        for v in self.oversized():
            with self._lock:
                if any(j.stem == v.stem and j.album == v.parent.name and j.status not in ("done", "failed")
                       for j in self.jobs.values()):
                    continue  # already being redone
            try:
                copy = self.storage.incoming_path(v.parent.name, v.name)
                shutil.copy2(v, copy)
                self.add(v.parent.name, copy, stem=v.stem)
                log.info("%s is bigger than the Pi can decode smoothly; converting it to 1080p", v.name)
            except Exception as e:  # noqa: BLE001
                log.warning("couldn't queue %s for resizing: %s", v.name, e)

    def _backfill_sound(self) -> None:
        """Measure videos imported before sound levelling existed."""
        try:
            os.nice(10)
        except OSError:
            pass
        for album_dir in sorted(self.storage.root.iterdir()):
            if not album_dir.is_dir() or album_dir.name.startswith("."):
                continue
            for v in sorted(album_dir.iterdir()):
                if is_video(v) and v.is_file() and "lufs" not in self.storage.video_meta(v):
                    try:
                        meta = analyse_sound(v, self.storage)
                        log.info("sound level for %s: %s", v.name, describe_sound(meta) or "unknown")
                    except Exception as e:  # noqa: BLE001
                        log.warning("couldn't measure sound of %s: %s", v.name, e)

    def _requeue_incoming(self) -> None:
        for inc in self.storage.root.glob("*/.incoming/*"):
            if not inc.is_file():
                continue
            if ".converting." in inc.name or inc.name.endswith(".part"):
                # half-written output of a conversion interrupted by a restart;
                # its source is still queued alongside it, so just start again
                inc.unlink(missing_ok=True)
                continue
            self.add(inc.parent.parent.name, inc)

    def add(self, album: str, src: Path, stem: str | None = None) -> Job:
        job = Job(id=f"{time.time_ns():x}", album=album, name=src.name.split("__", 1)[-1], src=src, stem=stem)
        with self._lock:
            self.jobs[job.id] = job
        self._q.put(job)
        return job

    def list(self) -> list[dict]:
        with self._lock:
            # forget finished jobs after 10 minutes
            for jid in [j.id for j in self.jobs.values() if j.status in ("done", "failed") and time.time() - j.started > 600]:
                self.jobs.pop(jid, None)
            return [j.public() for j in sorted(self.jobs.values(), key=lambda j: j.started)]

    def _worker(self) -> None:
        while True:
            job = self._q.get()
            try:
                self._run(job)
            except Exception as e:  # noqa: BLE001
                log.exception("video job failed")
                job.status, job.message = "failed", str(e)[:200]
                job.src.unlink(missing_ok=True)

    def _run(self, job: Job) -> None:
        job.status = "checking"
        panel = self.storage.panel
        p = probe(job.src)
        album_dir = self.storage.root / job.album
        album_dir.mkdir(exist_ok=True)
        stem = job.stem or self.storage.unique_stem(job.album, job.name)
        how = plan(p, panel)
        if how == "copy":
            dst = album_dir / f"{stem}{job.src.suffix.lower()}"
            if job.stem:  # replacing an existing video: only now, so it stays playable until here
                self.storage.delete_stem(job.album, job.stem)
            shutil.move(str(job.src), dst)
            job.progress = 1.0
        else:
            dst = album_dir / f"{stem}.mp4"
            part = album_dir / ".incoming" / f"{stem}.converting.mp4"
            job.status = "converting"
            job.message = "Converting HDR video" if p.hdr else "Converting video"
            args = convert_args(job.src, part, p, panel)
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                preexec_fn=lambda: os.nice(19),
            )
            for line in proc.stdout:  # -progress key=value lines
                if line.startswith("out_time_us=") and p.duration:
                    try:
                        job.progress = min(0.99, int(line.split("=", 1)[1]) / 1e6 / p.duration)
                    except ValueError:
                        pass
            err = proc.stderr.read()
            if proc.wait() != 0 or not part.exists():
                part.unlink(missing_ok=True)
                raise RuntimeError("conversion failed: " + (err.strip().splitlines() or ["unknown error"])[-1])
            if job.stem:  # replacing an existing video: only now that the new one is ready
                self.storage.delete_stem(job.album, job.stem)
            os.replace(part, dst)
            job.src.unlink(missing_ok=True)
            p = probe(dst)
        self.storage.write_video_meta(dst, p)
        make_poster(dst, self.storage.poster_path(dst), self.storage.thumbnail_path(dst), p.duration, panel)
        job.status, job.message = "checking", "Checking the sound level"
        meta = analyse_sound(dst, self.storage)
        job.status, job.progress, job.message = "done", 1.0, describe_sound(meta)
        log.info("video ready: %s (%s)", dst, how)
        self.on_done(dst)


# ---------------------------------------------------------------- playback
def hdmi_audio_device(cards_file: str = "/proc/asound/cards") -> str | None:
    """ALSA device for the first HDMI port's sound. On a Pi 4 the ports are
    cards 'vc4hdmi0'/'vc4hdmi1'; a Zero 2 W has one called 'vc4hdmi'. HDMI on
    the Pi needs ALSA's 'hdmi:' device (plain hw/plughw is rejected)."""
    try:
        text = Path(cards_file).read_text()
    except OSError:
        return None
    ids = []
    for line in text.splitlines():
        # " 1 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0"
        if "[" in line and "]" in line:
            ids.append(line.split("[", 1)[1].split("]", 1)[0].strip())
    for want in ("vc4hdmi0", "vc4hdmi"):
        if want in ids:
            return f"alsa/hdmi:CARD={want},DEV=0"
    hdmi = [i for i in ids if "hdmi" in i.lower()]
    return f"alsa/hdmi:CARD={hdmi[0]},DEV=0" if hdmi else None


def _drm_target(connector: str = "HDMI-A-1") -> tuple[str, str] | None:
    """Find the /dev/dri card that owns the HDMI connector (card1 on a Pi 4)."""
    for c in sorted(Path("/sys/class/drm").glob(f"card*-{connector}")):
        card = c.name.split("-", 1)[0]
        return f"/dev/dri/{card}", connector
    return None


# The Pi 4's hardware decoders, best first: v4l2m2m hands decoded frames straight
# to the graphics chip (no copying); drm does the same for HEVC; v4l2m2m-copy copies
# every frame through main memory (~90 MB/s at 1080p: choppy at 1440p). mpv uses
# the first that works and falls back to software if none do.
HWDEC = "v4l2m2m,drm,v4l2m2m-copy"
# harmless lines from mpv trying decoders this machine doesn't have
MPV_NOISE = ("libcuda", "cuvid", "nvcuvid", "vdpau", "vaapi", "unsupported hwdec", "could not find a valid device",
             "can't configure decoder", "could not open codec",
             # it can't offer console switching, which a frame doesn't need
             "vt switcher", "tty for vt control",
             # the frame closing its control connection once the video is handed over
             "write error (broken pipe)", "client removed during hook handling")


def screen_lines(text: str) -> list[str]:
    """What mpv says about the screen (its [vo...] messages): the connector, the screen
    setting it picked and the layers it uses, to see why the screen might blink."""
    keep = ("mode", "connector", "crtc", "plane", "drm", "format", "vsync", "resolution")
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[vo") and any(k in line.lower() for k in keep):
            out.append(line)
    return out[:20]


def summarise_mpv(text: str) -> tuple[str | None, list[str]]:
    """(how the video was decoded, real warnings) from mpv's messages."""
    decoder, warnings = None, []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[vo"):  # screen details, logged separately
            continue
        m = re.search(r"Using hardware decoding \(([^)]+)\)", line)
        if m:
            decoder = f"hardware ({m.group(1)})"
            continue
        if "Using software decoding" in line:
            decoder = decoder or "software"
            continue
        if not line or line.startswith("[vd]") or any(n in line.lower() for n in MPV_NOISE):
            continue
        warnings.append(line)
    return decoder, warnings[-8:]


def _mpv_commands(sock_path: str, commands: list, wait: float = 5.0) -> list[str] | None:
    """Once mpv's control socket appears, send it commands in turn; their replies,
    or None if the socket never opened."""
    import socket as _socket

    deadline = time.time() + wait
    while time.time() < deadline:
        if os.path.exists(sock_path):
            try:
                with _socket.socket(_socket.AF_UNIX) as s:
                    s.settimeout(3)
                    s.connect(sock_path)
                    replies = []
                    f = s.makefile("rb")
                    for cmd in commands:
                        s.sendall((json.dumps({"command": cmd}) + "\n").encode())
                        while True:  # skip mpv's event messages until the reply
                            line = f.readline().decode(errors="replace")
                            if not line or '"error"' in line:
                                break
                        replies.append(line.strip())
                    return replies
            except OSError:
                pass
        time.sleep(0.05)
    return None


def _overlay_cmd(surround: dict) -> list:
    w, h = surround["size"]
    return ["overlay-add", 0, 0, 0, str(surround["file"]), 0, "bgra", w, h, w * 4]


def send_overlay(surround: dict, wait: float = 5.0) -> bool:
    """Add the surround picture as an overlay (a premultiplied BGRA file covering the
    screen, transparent where the video is)."""
    replies = _mpv_commands(surround["socket"], [_overlay_cmd(surround)], wait)
    if replies is None:
        log.warning("couldn't put photos around the video (mpv didn't open its control socket)")
        return False
    ok = '"success"' in replies[0]
    if not ok:
        log.warning("couldn't put photos around the video (%s)", replies[0][:200])
    return ok


def hand_over(surround: dict, path: Path, wait: float = 5.0) -> bool:
    """mpv starts idle with nothing on screen: give it the pictures around the video
    first, then the video, so its very first frame already has them (no black flash).
    True once the video is loaded."""
    replies = _mpv_commands(surround["socket"], [_overlay_cmd(surround), ["loadfile", str(path)]], wait)
    if replies is None:
        log.warning("couldn't start the video (mpv didn't open its control socket)")
        return False
    if '"success"' not in replies[0]:
        log.warning("couldn't put photos around the video (%s)", replies[0][:200])
    ok = len(replies) > 1 and '"success"' in replies[1]
    if not ok:
        log.warning("couldn't start the video (%s)", (replies[1] if len(replies) > 1 else "no reply")[:200])
    return ok


class VideoPlayer:
    """Plays one video at a time with mpv. play() blocks until the video ends
    or stop() is called."""

    def __init__(self, simulate: bool = False):
        self.simulate = simulate  # dev machines: show the poster instead of running mpv
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.playing: Path | None = None
        self.started_at = 0.0
        self.drm_mode: str | None = None  # the screen setting the frame uses, so mpv doesn't switch it
        self.drm_mode_refused = False  # mpv wouldn't take it: don't pass it again

    @staticmethod
    def available() -> bool:
        return shutil.which("mpv") is not None

    def play(self, path: Path, *, rotate: int, cover: bool, sound: bool, volume: int,
             max_seconds: int, duration: float, show_poster=None, boost_db: int = 0,
             gain_db: float = 0.0, start: float = 0.0, surround: dict | None = None) -> str:
        self._stop.clear()
        self.playing = path
        self.started_at = time.time()
        try:
            if self.simulate or not self.available():
                log.info("not really playing %s (%s); showing its first frame instead", path.name,
                         "test display" if self.simulate else "mpv isn't installed")
                if show_poster:
                    show_poster()
                limit = min(duration or 5, 5, max_seconds or 5)
                self._stop.wait(limit)
                return "stopped" if self._stop.is_set() else "finished"
            args = [
                # messages on (warnings, plus which decoder it uses), status line off
                "mpv", "--msg-level=all=warn,vd=v,vo=v", "--term-status-msg=", "--no-input-terminal",
                "--no-config", "--osc=no", "--osd-level=0",
                "--no-input-default-bindings", "--input-vo-keyboard=no", "--cursor-autohide=always",
                # only the Pi's own H.264 decoder (every stored video is H.264); "auto"
                # would also probe NVIDIA/Intel/AMD ones and log noise about them
                f"--hwdec={HWDEC}", "--vo=gpu", "--gpu-context=drm",
                "--profile=fast", "--interpolation=no",
                f"--video-rotate={rotate % 360}",
                "--keep-open=no", "--loop-file=no",
                # with pictures around the video, mpv waits (idle, nothing on screen) until
                # they're in place, then plays the video and quits; otherwise it plays straight away
                "--idle=once" if surround else "--idle=no",
                f"--volume={max(0, min(100, volume))}",
                "--mute=no" if sound else "--mute=yes",
            ]
            if not sound:
                args.append("--ao=null")
            else:
                args.append("--ao=" + os.environ.get("PHOTOFRAME_AO", "alsa"))
                # Explicit setting wins; otherwise send sound to the HDMI port
                # the screen is on, not the Pi's default (often the headphone jack).
                dev = os.environ.get("PHOTOFRAME_AUDIO_DEVICE") or hdmi_audio_device()
                # Levelling (measured at import) plus any boost. When turning
                # up, a limiter holds peaks just under full scale so loud
                # moments don't distort.
                total = round(max(-30.0, min(30.0, float(gain_db) + float(boost_db))), 1)
                if total > 0.05:
                    args.append(f"--af=lavfi=[volume={total:g}dB,alimiter=limit=0.89:level=disabled]")
                elif total < -0.05:
                    args.append(f"--af=lavfi=[volume={total:g}dB]")
                if dev:
                    args.append(f"--audio-device={dev}")
                    log.info("video sound -> %s (volume %d%%, level %+.1f dB, boost +%d dB)", dev, volume, gain_db, boost_db)
                else:
                    log.warning("no HDMI sound device found; using the system default")
            if cover:
                args.append("--panscan=1.0")
            if surround:  # photos around the video: the video in its window, the photos on the overlay layer
                m = surround["margins"]
                args += [f"--video-margin-ratio-{side}={m[side]:.5f}" for side in ("left", "right", "top", "bottom")]
                args.append(f"--input-ipc-server={surround['socket']}")
            if start and start > 0:
                args.append(f"--start={start:.1f}")
            if max_seconds and max_seconds > 0:
                args.append(f"--length={max_seconds}")
            target = _drm_target()
            if target:
                args += [f"--drm-device={target[0]}", f"--drm-connector={target[1]}"]
            if self.drm_mode:  # the frame's own screen setting: switching it would blank the screen
                args.append(f"--drm-mode={self.drm_mode}")
            args += os.environ.get("PHOTOFRAME_MPV_ARGS", "").split()
            if not surround:
                args.append(str(path))
            with self._lock:
                if self._stop.is_set():
                    return "stopped"
                # mpv prints its messages to stdout when it isn't in a terminal
                self._proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if surround:
                proc = self._proc

                def start():
                    if not hand_over(surround, path) and proc.poll() is None:
                        proc.terminate()  # it would wait for ever otherwise

                threading.Thread(target=start, name="video-surround", daemon=True).start()
            err, _ = self._proc.communicate()
            decoder, warnings = summarise_mpv(err or "")
            screen = screen_lines(err or "")
            if screen:
                log.info("video player and the screen (asked for setting %s): %s",
                         self.drm_mode or "none", " | ".join(screen))
            rc = self._proc.returncode
            ran = time.time() - self.started_at
            if decoder is None and ran < 15 and not self._stop.is_set():
                # it never got as far as decoding: log everything it said, unfiltered
                raw = [l.strip() for l in (err or "").splitlines() if l.strip()]
                log.warning("mpv stopped after %.1fs (exit code %s) without playing %s. Its last messages: %s",
                            ran, rc, path.name, " | ".join(raw[-15:]) or "(none)")
                if self.drm_mode and "mode" in (err or "").lower():
                    log.warning("mpv didn't take the screen setting %s; videos will let it choose", self.drm_mode)
                    self.drm_mode_refused = True
            if self._stop.is_set():
                return "stopped"
            if rc not in (0, 4):  # 4 = quit by signal/user
                log.warning("mpv exited with %s: %s", rc, " | ".join(warnings)[-400:] or "no details")
                return "error"
            if decoder and decoder.startswith("hardware"):
                log.info("video %s: decoded in %s", path.name, decoder)
            elif decoder:
                log.warning("video %s: decoded in software, which can be choppy for big videos", path.name)
            for line in warnings:
                log.warning("mpv: %s", line)
            return "finished"
        finally:
            with self._lock:
                self._proc = None
            self.playing = None

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.send_signal(signal.SIGTERM)
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
