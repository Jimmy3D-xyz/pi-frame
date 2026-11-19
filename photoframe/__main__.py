"""python3 -m photoframe [--data-dir DIR] [--display fb|file:WxH:path] [--port 80]"""

from __future__ import annotations

import argparse
import logging
import json
import os
import signal
import sys
import tempfile
import threading
from pathlib import Path

from . import __version__
from .config import ConfigStore
from .display import make_display
from .frame import Frame
from .network import WifiManager
from .storage import Storage
from .video import VideoJobs, tools_available
from .cloud import OneDrive
from .web import create_app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="photoframe")
    ap.add_argument("--data-dir", default=os.environ.get("PHOTOFRAME_DATA", "/var/lib/pi-photoframe"))
    ap.add_argument("--display", default=os.environ.get("PHOTOFRAME_DISPLAY", "fb"))
    ap.add_argument("--host", default=os.environ.get("PHOTOFRAME_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PHOTOFRAME_PORT", "80")))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("photoframe")
    from .health import install_event_log

    install_event_log()
    log.info("Pi Frame %s starting", __version__)

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    # Big uploads (videos) are spooled to disk by the web server: keep them on
    # the SD card rather than /tmp, which is in RAM on recent Raspberry Pi OS.
    tmp = data_dir / "tmp"
    tmp.mkdir(exist_ok=True)
    for old in tmp.iterdir():
        old.unlink(missing_ok=True)
    os.environ["TMPDIR"] = str(tmp)
    tempfile.tempdir = str(tmp)

    cfg = ConfigStore(data_dir)
    # the last real screen's size: used when starting without one, so photos stay sharp
    screen_file = data_dir / "screen.json"
    try:
        last = tuple(json.loads(screen_file.read_text())["size"])
    except (OSError, ValueError, KeyError):
        last = (1920, 1080)
    display = make_display(args.display, hardware=cfg.get("display_hw_fade"), headless_size=last)
    if display.info().get("kind") not in ("none", "file"):
        try:
            screen_file.write_text(json.dumps({"size": list(display.size)}))
        except OSError:
            pass
    storage = Storage(data_dir, display.size)
    frame = Frame(cfg, storage, display, data_dir)

    iface = os.environ.get("PHOTOFRAME_WIFI_IFACE", "wlan0")
    if os.environ.get("PHOTOFRAME_WIFI_SETUP", "1") != "0" and WifiManager.available(iface):
        frame.wifi = WifiManager(iface, on_change=frame.on_wifi_change)
        log.info("Wi-Fi setup fallback enabled on %s", iface)
    else:
        log.info("Wi-Fi setup fallback disabled (no NetworkManager/%s, or turned off)", iface)

    if tools_available():
        frame.jobs = VideoJobs(storage, on_done=lambda path: frame.photos_added())
    else:
        log.info("ffmpeg not found: video uploads disabled")

    frame.cloud = OneDrive(data_dir, storage, frame)
    threading.Thread(target=frame.cloud.run, name="onedrive", daemon=True).start()

    from .backup import Backups

    frame.backups = Backups(data_dir, cfg, frame.cloud, frame)
    if frame.backups.just_restored:
        frame.cloud.reconcile()  # synced photos aren't in backups: fetch them again
    threading.Thread(target=frame.backups.run, name="backups", daemon=True).start()

    from .buttons import Buttons

    frame.buttons = Buttons(frame)
    frame.buttons.start()

    frame.startup()
    threading.Thread(target=frame.run_scheduler, name="scheduler", daemon=True).start()
    threading.Thread(target=frame.run_overlay_clock, name="overlay-clock", daemon=True).start()
    threading.Thread(target=frame.monitor.run, args=(frame._stop,), name="monitor", daemon=True).start()
    threading.Thread(target=frame.watch_screen, name="screen-watch", daemon=True).start()
    frame.presence.start()
    threading.Thread(target=frame.presence.run, args=(frame._stop,), name="presence", daemon=True).start()
    threading.Thread(target=frame.overlay.weather.run, args=(frame._stop,), name="weather", daemon=True).start()
    if frame.wifi is not None:
        threading.Thread(target=frame.wifi.run, name="wifi", daemon=True).start()
    frame.notify_online()

    def _shutdown(*_):
        # If the whole Pi is going down (reboot/poweroff from anywhere: SSH, Home
        # Assistant, the Maintenance tab), say so on screen. A plain service
        # restart (e.g. an update) leaves the photo up.
        try:
            import subprocess

            state = subprocess.run(["systemctl", "is-system-running"], capture_output=True, text=True,
                                   timeout=3).stdout.strip()
            if state == "stopping":
                jobs = subprocess.run(["systemctl", "list-jobs", "--no-legend"], capture_output=True, text=True,
                                      timeout=3).stdout
                frame.show_power_card(restarting="reboot.target" in jobs)
        except Exception:  # noqa: BLE001
            pass
        frame.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)

    app = create_app(frame)
    try:
        from waitress import serve  # apt: python3-waitress
    except ImportError:
        log.warning("waitress not installed; using Flask's development server")
        app.run(host=args.host, port=args.port, threaded=True)
    else:
        serve(
            app, host=args.host, port=args.port, threads=4,
            max_request_body_size=4 * 1024 * 1024 * 1024,
            # keep each 8 MB upload chunk in memory rather than a temp file,
            # so big uploads are written to the SD card only once
            inbuf_overflow=12 * 1024 * 1024,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
