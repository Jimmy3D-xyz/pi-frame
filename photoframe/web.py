"""HTTP API, endpoint-compatible with esp32-photoframe (docs/API.md), so the
image server and the ha-esp32-photoframe integration can drive this frame.
Pi-only extras: /api/previous and /api/screen."""

from __future__ import annotations

import json

import hmac
import subprocess
import shutil
import os
import logging
import threading
import time
from pathlib import Path

import socket

from flask import Flask, Response, jsonify, redirect, request, send_file

from . import __version__, health
from .config import ConfigError
from .fetcher import FIRMWARE_VERSION
from .frame import Frame
from .network import HOTSPOT_IP
from .storage import StorageError
from .uploads import UploadError, Uploads
from .updater import UpdateError, Updates
from .cloud import CloudError, OneDrive
from .backup import BackupError, Backups, backup_name
from . import firstrun
from .video import is_video

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
STARTED = time.time()


def _ok(message: str | None = None, **extra):
    body = {"status": "success", **extra}
    if message:
        body["message"] = message
    return jsonify(body)


def _err(message: str, code: int = 400):
    return jsonify({"status": "error", "message": message}), code


def _board_model() -> str:
    try:
        return Path("/proc/device-tree/model").read_text().strip("\x00\n ")
    except OSError:
        return "Linux"


def create_app(frame: Frame) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # videos
    cfg, storage = frame.cfg, frame.storage
    uploads = Uploads(storage, frame.data_dir)
    health.install_event_log()
    updates = Updates(frame.data_dir)
    from .autoupdate import AutoUpdate, AutoUpdateError

    frame.autoupdate = AutoUpdate(frame.data_dir, cfg, updates, frame)
    autoupd = frame.autoupdate
    if getattr(frame, "cloud", None) is None:
        frame.cloud = OneDrive(frame.data_dir, storage, frame)
    cloud = frame.cloud
    if getattr(frame, "backups", None) is None:
        frame.backups = Backups(frame.data_dir, cfg, cloud, frame)
    backups = frame.backups

    def in_setup_mode() -> bool:
        return frame.wifi is not None and frame.wifi.status.mode in ("setup", "connecting")

    def first_run() -> bool:
        return firstrun.is_first_run(frame.data_dir)

    def is_setup_path() -> bool:
        return (request.path in ("/setup", "/favicon.ico") or request.path.startswith("/api/wifi/")
                or request.path.startswith("/static/")
                or (first_run() and request.path.startswith("/api/setup")))

    # ---------- captive portal while the setup hotspot is up ----------
    @app.before_request
    def _captive():
        if not in_setup_mode():
            return None
        frame.wifi.last_portal_hit = time.time()
        host = (request.host or "").split(":")[0]
        if is_setup_path():
            return None
        if host != HOTSPOT_IP:
            # Phones probe sites like connectivitycheck.gstatic.com; sending
            # them here makes the "sign in to network" page pop up.
            return redirect(f"http://{HOTSPOT_IP}/setup", 302)
        return None

    # ---------- optional HTTP password (firmware: username ignored) ----------
    @app.before_request
    def _auth():
        if in_setup_mode() and is_setup_path():
            return None  # joining the hotspot needs the password shown on the screen
        pw = cfg.get("http_password")
        if not pw:
            return None
        auth = request.authorization
        if auth and auth.password and hmac.compare_digest(auth.password, pw):
            return None
        return Response(
            "Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Pi Frame"'}
        )

    # ---------- UI ----------
    @app.get("/")
    def index():
        if first_run():
            return send_file(STATIC / "welcome.html")
        return send_file(STATIC / "index.html")

    @app.get("/static/<path:name>")
    def static_file(name):
        # logo, icons and the web app manifest
        if "/" in name or name.startswith(".") or name.endswith(".html"):
            return _err("not found", 404)
        path = STATIC / name
        if not path.is_file():
            return _err("not found", 404)
        mime = {"webmanifest": "application/manifest+json", "svg": "image/svg+xml", "ico": "image/x-icon"}.get(
            name.rsplit(".", 1)[-1])
        return send_file(path, mimetype=mime, max_age=86400)

    @app.get("/favicon.ico")
    def favicon():
        return send_file(STATIC / "favicon.ico", mimetype="image/x-icon", max_age=86400)

    @app.get("/setup")
    def setup_page():
        return send_file(STATIC / ("welcome.html" if first_run() else "setup.html"))

    # ---------- first run: the welcome wizard ----------
    @app.get("/api/setup")
    def setup_status():
        st = frame.wifi.status if frame.wifi is not None else None
        return jsonify({
            "first_run": first_run(), "hostname": socket.gethostname(),
            "url": f"http://{socket.gethostname()}.local/",
            "name": cfg.get("device_name"), "has_login": bool(firstrun.login_users()),
            "wifi_mode": st.mode if st else "disabled", "ssid": st.ssid if st else "",
            "wifi_saved": bool(frame.wifi.saved_wifi()) if frame.wifi is not None and hasattr(frame.wifi, "saved_wifi") else False,
            "password_set": bool(cfg.get("http_password")),
        })

    @app.post("/api/setup")
    def setup_finish():
        if not first_run():
            return _err("This frame is already set up.", 409)
        try:
            a = firstrun.check(request.get_json(silent=True) or {})
            req = {"action": "first_run", "country": a["country"], "timezone": a["timezone"]}
            if a.get("ssh"):
                req["ssh"] = {"username": a["ssh"]["username"], "hash": firstrun.hash_password(a["ssh"]["password"])}
        except firstrun.SetupError as e:
            return _err(str(e))
        if a.get("wifi") and frame.wifi is None:
            return _err("Wi-Fi setup isn't available on this device")
        helper = Path("/etc/systemd/system/pi-frame-system.path").exists() or app.config.get("SETUP_HELPER")
        if helper:
            res = (app.config.get("SETUP_HELPER") or firstrun.ask_helper)(frame.data_dir, req)
            if res.get("state") != "done":
                return _err(res.get("message") or "Couldn't apply the settings")
        # the frame's own settings last: the page password takes effect with them
        cfg.update({"device_name": a["name"], "timezone": a["timezone"], "http_password": a["password"]})
        firstrun.finish(frame.data_dir)
        log.info("first-run setup done: %s, %s, %s%s", a["name"], a["country"], a["timezone"],
                 f", SSH login {a['ssh']['username']}" if a.get("ssh") else "")
        if a.get("wifi"):
            w = a["wifi"]
            frame.wifi.connect(w["ssid"], w["password"], w["hidden"])
            frame.after_setup()
            return _ok(f"Joining {w['ssid']}", joining=w["ssid"])
        frame.refresh_overlay()
        frame.after_setup()
        return _ok("All set")

    # ---------- Wi-Fi ----------
    @app.get("/api/wifi/status")
    def wifi_status():
        base = {"hostname": socket.gethostname(), "url": f"http://{socket.gethostname()}.local/"}
        if frame.wifi is None:
            return jsonify({**base, "mode": "disabled", "available": False})
        st = frame.wifi.status
        return jsonify(
            {
                **base,
                "available": True,
                "mode": st.mode,
                "ssid": st.ssid,
                "hotspot_ssid": st.hotspot_ssid,
                "message": st.message,
            }
        )

    @app.get("/api/wifi/networks")
    def wifi_networks():
        if frame.wifi is None:
            return _err("Wi-Fi setup isn't available on this device", 404)
        nets = frame.wifi.status.networks if in_setup_mode() else frame.wifi.scan()
        return jsonify([{"ssid": n.ssid, "signal": n.signal, "secure": n.secure} for n in nets])

    @app.post("/api/wifi/connect")
    def wifi_connect():
        if frame.wifi is None:
            return _err("Wi-Fi setup isn't available on this device", 404)
        data = request.get_json(silent=True) or {}
        ssid = str(data.get("ssid", "")).strip()
        password = str(data.get("password", ""))
        if not ssid or len(ssid) > 32:
            return _err("choose a network name (up to 32 characters)")
        if password and not 8 <= len(password) <= 63:
            return _err("Wi-Fi passwords are 8 to 63 characters")
        frame.wifi.connect(ssid, password, bool(data.get("hidden")))
        return _ok(f"Connecting to {ssid}")

    # ---------- system ----------
    @app.get("/api/system-info")
    def system_info():
        total, used = storage.usage()
        return jsonify(
            {
                "device_name": cfg.get("device_name"),
                "device_id": frame.device_id,
                "board_name": "raspberrypi_lcd",
                "board_model": _board_model(),
                "display_type": "rgb",
                "version": FIRMWARE_VERSION,
                "project_name": "pi-photoframe",
                "compile_time": "",
                "compile_date": "",
                "idf_version": "n/a",
                "width": frame.panel[0],
                "height": frame.panel[1],
                "has_sdcard": True,
                "sdcard_inserted": True,
                "has_flash_storage": False,
                "storage_total": total,
                "storage_used": used,
                "uptime": int(time.time() - STARTED),
            }
        )

    @app.get("/api/battery")
    def battery():
        return _err("no battery on this frame", 404)

    @app.get("/api/sensor")
    def sensor():
        return _err("no environmental sensor on this frame", 404)

    @app.get("/api/time")
    def get_time():
        now = time.time()
        return jsonify({"time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)), "timestamp": int(now)})

    @app.post("/api/time/sync")
    def time_sync():
        return _ok("time is kept by systemd-timesyncd")

    @app.post("/api/keep_alive")
    def keep_alive():
        return _ok()

    @app.post("/api/sleep")
    def sleep():
        frame.set_screen(False)
        return _ok("Screen turned off")

    # ---------- config ----------
    @app.get("/api/config")
    def get_config():
        body = cfg.public()
        body.update(
            {
                "device_id": frame.device_id,
                "wifi_ssid": "",
                "ca_cert_set": False,
                "last_fetch_error": frame.state.get("last_fetch_error", ""),
                "http_password_set": bool(cfg.get("http_password")),
                "config_last_updated": cfg.config_last_updated,
            }
        )
        return jsonify(body)

    @app.route("/api/config", methods=["POST", "PATCH"])
    def set_config():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _err("expected a JSON object")
        try:
            changed = cfg.update(data)
        except ConfigError as e:
            return _err(str(e))
        frame.poke()
        if {"display_orientation", "display_rotation_deg"} & changed.keys() and \
                Path("/usr/share/pi-photoframe/splash.png").exists():
            # the boot logo should turn too (the root helper remakes it in the background)
            d = frame.data_dir / "system"
            d.mkdir(exist_ok=True)
            if not (d / "request.json").exists():
                (d / "request.tmp").write_text(json.dumps({"action": "splash", "t": time.time()}))
                os.replace(d / "request.tmp", d / "request.json")
        if {"slideshow_style", "collage_count"} & changed.keys():
            frame.reset_story()
        if "display_hw_fade" in changed:
            # the display is chosen at start-up: restart to switch (systemd starts the frame again)
            log.warning("hardware fades turned %s; restarting the frame", "on" if changed["display_hw_fade"] else "off")
            threading.Thread(target=lambda: (time.sleep(1.5), os._exit(0)), daemon=True).start()
        if any(k.startswith("monitor_") for k in changed):
            def apply_monitor():
                if frame.monitor.apply() and not frame.monitor.available:
                    frame.refresh_overlay()  # software dimming changed
            threading.Thread(target=apply_monitor, daemon=True).start()
        if any(k.startswith("presence_") for k in changed) or (
                "buttons_enabled" in changed or any(k.startswith("button_") and k.endswith("_pin") for k in changed)):
            frame.presence.start()
        if any(k.startswith("button") for k in changed) and frame.buttons is not None:
            frame.buttons.start()
        if {"overlay_weather", "weather_location", "weather_units"} & changed.keys():
            threading.Thread(target=lambda: (frame.overlay.weather.refresh(), frame.refresh_overlay()), daemon=True).start()
        elif any(k.startswith("overlay_") for k in changed):
            threading.Thread(target=frame.refresh_overlay, daemon=True).start()
        if {"display_orientation", "display_rotation_deg"} & changed.keys() or (
            "rotation_mode" in changed or "image_url" in changed
        ):
            threading.Thread(target=frame.rotate, daemon=True).start()
        return _ok()

    @app.route("/api/settings/processing", methods=["GET", "POST", "DELETE"])
    def processing():
        if request.method == "GET":
            return jsonify(cfg.processing)
        if request.method == "DELETE":
            cfg.set_processing(None)
        else:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return _err("expected a JSON object")
            cfg.set_processing(data)
        return _ok()

    @app.route("/api/settings/palette", methods=["GET", "POST", "DELETE"])
    def palette():
        if request.method == "GET":
            return jsonify(cfg.palette)
        if request.method == "DELETE":
            cfg.set_palette(None)
        else:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return _err("expected a JSON object")
            cfg.set_palette(data)
        return _ok()

    # ---------- display ----------
    @app.post("/api/rotate")
    def rotate():
        ok, msg = frame.rotate()
        if ok:
            return _ok(msg)
        return _err(msg, 502 if cfg.get("rotation_mode") == "url" else 404)

    @app.post("/api/previous")
    def previous():
        ok, msg = frame.previous()
        return _ok(msg) if ok else _err(msg, 404)

    @app.post("/api/display")
    def display():
        data = request.get_json(silent=True) or {}
        try:
            path = storage.resolve(data.get("filepath", ""))
        except StorageError as e:
            return _err(str(e), 404)
        if is_video(path):
            frame.play_video(path)
            return _ok("Playing video")
        frame.show_path(path, data.get("filepath"))
        return _ok("Image displayed")

    @app.post("/api/display-image")
    def display_image():
        if request.files:
            f = request.files.get("image") or next(iter(request.files.values()))
            data = f.read()
        else:
            data = request.get_data()
        if not data:
            return _err("no image data")
        try:
            frame.show_upload(data)
        except Exception as e:  # noqa: BLE001
            return _err(f"could not display image: {e}")
        return _ok("Image displayed")

    @app.get("/api/current_image")
    def current_image():
        """What's on screen: a small preview by default; ?size=large (up to
        1920 px) or ?size=full (the screen's own resolution) for the pop-up and
        the live view. Includes the clock and other on-screen info."""
        size = request.args.get("size")
        if size in ("large", "full"):
            data = frame.current_jpeg(size)
            if data is None:
                return _err("no image displayed", 404)
            return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})
        if not frame.current_thumb:
            return _err("no image displayed", 404)
        return Response(frame.current_thumb, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/live")
    def live_page():
        return send_file(STATIC / "live.html", mimetype="text/html", max_age=0)

    @app.get("/api/status")
    def status():
        """Pi extra: everything the UI's header needs in one call."""
        return jsonify(
            {
                "current": frame.state.get("current"),
                "screen_on": frame.display.screen_on,
                "next_rotation": frame.next_rotation,
                "last_rotation": frame.last_rotation,
                "thumb_version": frame.thumb_version,
                "last_fetch_error": frame.state.get("last_fetch_error", ""),
                "can_go_back": len(frame.state.get("history", [])) >= 2,
                "panel": frame.panel,
                "notice": frame.notice,
                "playing": (
                    {
                        "filepath": f"{frame.player.playing.parent.name}/{frame.player.playing.name}",
                        "started_at": frame.player.started_at,
                    }
                    if frame.player.playing
                    else None
                ),
                "videos_supported": frame.jobs is not None,
                "wifi": frame.wifi.status.mode if frame.wifi else "disabled",
                "version": __version__,
            }
        )

    @app.route("/api/screen", methods=["GET", "POST"])
    def screen():
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            frame.set_screen(bool(data.get("on", True)))
        return jsonify({"on": frame.display.screen_on})

    @app.post("/api/calibration/display")
    def calibration():
        return _err("colour calibration is only needed for e-paper panels", 400)

    # ---------- images & albums ----------
    @app.get("/api/images")
    def images():
        try:
            return jsonify(storage.images(request.args.get("album", storage.DEFAULT_ALBUM)))
        except StorageError as e:
            return _err(str(e), 404)

    @app.get("/api/image")
    def image():
        try:
            path = storage.resolve(request.args.get("filepath", ""))
        except StorageError as e:
            return _err(str(e), 404)
        return send_file(storage.ensure_thumbnail(path), mimetype="image/jpeg", max_age=86400)

    @app.post("/api/upload")
    def upload():
        album = request.form.get("album") or storage.DEFAULT_ALBUM
        files = request.files.getlist("image") + request.files.getlist("video")
        if not files:
            return _err("no image field in upload")
        saved, jobs = [], []
        try:
            for f in files:
                name = f.filename or "photo"
                if is_video(name) or (f.mimetype or "").startswith("video/"):
                    if frame.jobs is None:
                        return _err("videos need ffmpeg installed on the frame (re-run install.sh)")
                    dest = storage.incoming_path(album, name if is_video(name) else name + ".mp4")
                    f.save(dest)
                    jobs.append(frame.jobs.add(album, dest).public())
                else:
                    saved.append(storage.add(album, name, f.read()).name)
        except StorageError as e:
            return _err(str(e))
        except Exception as e:  # noqa: BLE001
            return _err(f"could not read file: {e}")
        if saved:
            frame.photos_added()
        msg = f"Uploaded {len(saved)} photo(s)" if not jobs else f"Uploaded {len(saved) + len(jobs)} file(s)"
        return _ok(msg, files=saved, jobs=jobs)

    # ---------- chunked, resumable uploads (videos) ----------
    def _upload_err(e: UploadError):
        body = {"status": "error", "message": str(e)}
        if e.received is not None:
            body["received"] = e.received
        return jsonify(body), e.code

    @app.post("/api/upload/start")
    def upload_start():
        if frame.jobs is None:
            return _err("videos need ffmpeg installed on the frame (re-run install.sh)")
        d = request.get_json(silent=True) or {}
        try:
            return jsonify(uploads.start(str(d.get("album", "")), str(d.get("filename", "")),
                                         int(d.get("size", 0)), int(d.get("last_modified", 0))))
        except (TypeError, ValueError) as e:
            if isinstance(e, UploadError):
                return _upload_err(e)
            return _err("size and last_modified must be numbers")

    @app.put("/api/upload/<uid>")
    def upload_chunk(uid):
        try:
            offset = int(request.args.get("offset", "-1"))
            length = int(request.headers.get("Content-Length", "0"))
            received = uploads.append(uid, offset, request.stream, length)
        except UploadError as e:
            return _upload_err(e)
        except ValueError:
            return _err("offset must be a number")
        return jsonify({"received": received})

    @app.post("/api/upload/<uid>/finish")
    def upload_finish(uid):
        try:
            album, dest = uploads.finish(uid)
        except UploadError as e:
            return _upload_err(e)
        job = frame.jobs.add(album, dest).public()
        return _ok("Upload complete", jobs=[job])

    @app.delete("/api/upload/<uid>")
    def upload_cancel(uid):
        try:
            uploads.cancel(uid)
        except UploadError as e:
            return _upload_err(e)
        return _ok("Upload discarded")

    @app.get("/api/uploads/pending")
    def uploads_pending():
        return jsonify(uploads.pending())

    # ---------- weather for the on-screen info ----------
    @app.get("/api/weather/search")
    def weather_search():
        from .overlay import search_places

        q = request.args.get("q", "").strip()
        if len(q) < 2:
            return jsonify([])
        try:
            return jsonify(search_places(q))
        except Exception as e:  # noqa: BLE001
            return _err(f"couldn't search for places ({e}); is the frame online?", 502)

    @app.get("/api/weather")
    def weather_now():
        w = frame.overlay.weather
        return jsonify({"now": w.current(), "error": w.error, "location": cfg.get("weather_location")})

    # ---------- the monitor's own brightness (DDC/CI) ----------
    @app.get("/api/monitor")
    def monitor_status():
        return jsonify(frame.monitor.status())

    @app.post("/api/monitor/probe")
    def monitor_probe():
        frame.monitor.probe()
        return jsonify(frame.monitor.status())

    # ---------- screen resolution ----------
    @app.get("/api/display/modes")
    def display_modes():
        from .sysmodes import current_mode_setting, screen_modes

        conn, modes = screen_modes()
        try:
            cmd = Path("/proc/cmdline").read_text()
        except OSError:
            cmd = ""
        try:
            st = json.loads((frame.data_dir / "system" / "status.json").read_text())
        except (OSError, ValueError):
            st = None
        helper = Path("/etc/systemd/system/pi-frame-system.path").exists()
        return jsonify({"connector": conn, "modes": modes, "native": modes[0] if modes else None,
                        "setting": current_mode_setting(cmd), "screen": list(frame.panel),
                        "helper": helper, "status": st})

    @app.post("/api/display/detect")
    def display_detect():
        """Ask the root helper to re-read the screen's details (after swapping monitors)."""
        d = frame.data_dir / "system"
        d.mkdir(exist_ok=True)
        tmp = d / "request.tmp"
        tmp.write_text(json.dumps({"action": "detect", "t": time.time()}))
        os.replace(tmp, d / "request.json")
        # re-checking can make the screen briefly re-sync: put the picture back afterwards
        threading.Thread(target=lambda: (time.sleep(4), frame.refresh_overlay()), daemon=True).start()
        return _ok("Checking the screen")

    @app.post("/api/display/mode")
    def display_set_mode():
        from .sysmodes import screen_modes

        mode = str((request.get_json(silent=True) or {}).get("mode", ""))
        conn, modes = screen_modes()
        if not conn:
            return _err("no HDMI screen is connected")
        if mode not in modes:
            return _err(f"the screen doesn't offer {mode}")
        d = frame.data_dir / "system"
        d.mkdir(exist_ok=True)
        tmp = d / "request.tmp"
        tmp.write_text(json.dumps({"action": "set_mode", "mode": mode, "t": time.time()}))
        os.replace(tmp, d / "request.json")  # the root helper reacts to this
        log.warning("screen resolution change to %s requested; the Pi will restart", mode)
        threading.Thread(target=lambda: (time.sleep(1.5), frame.show_power_card(restarting=True)), daemon=True).start()
        return _ok(f"Changing to {mode}; the Pi restarts in a moment")

    # ---------- presence sensor ----------
    @app.get("/api/presence")
    def presence_status():
        return jsonify(frame.presence.status())

    # ---------- physical buttons ----------
    @app.get("/api/buttons")
    def buttons_status():
        if frame.buttons is None:
            from .buttons import available

            ok, why = available()
            return jsonify({"available": ok, "why": why or "buttons aren't running", "enabled": cfg.get("buttons_enabled"),
                            "active": False, "error": "", "last": None})
        return jsonify(frame.buttons.status())

    @app.get("/api/jobs")
    def jobs():
        return jsonify(frame.jobs.list() if frame.jobs else [])

    @app.post("/api/video/stop")
    def video_stop():
        frame.stop_video()
        return _ok("Video stopped")

    @app.post("/api/delete")
    def delete():
        data = request.get_json(silent=True) or {}
        try:
            storage.delete(data.get("filepath", ""))
        except StorageError as e:
            return _err(str(e), 404)
        return _ok("Image deleted")

    @app.route("/api/albums", methods=["GET", "POST", "DELETE"])
    def albums():
        try:
            if request.method == "GET":
                return jsonify(storage.albums())
            if request.method == "POST":
                storage.create_album((request.get_json(silent=True) or {}).get("name", ""))
                return _ok("Album created")
            storage.delete_album(request.args.get("name", ""))
            return _ok("Album deleted")
        except StorageError as e:
            return _err(str(e))

    @app.put("/api/albums/enabled")
    def album_enabled():
        data = request.get_json(silent=True) or {}
        try:
            storage.set_enabled(request.args.get("name", ""), bool(data.get("enabled", True)))
        except StorageError as e:
            return _err(str(e))
        frame.photos_added()
        return _ok()

    # ---------- maintenance ----------
    # ---------- maintenance ----------
    def _maintenance() -> dict:
        net: dict = {"mode": "disabled"}
        if frame.wifi is not None:
            w = frame.wifi
            try:
                details = w.details() if w.status.mode == "online" else {}
            except Exception:  # noqa: BLE001
                details = {}
            from .network import probe_method

            net = {"mode": w.status.mode, "connection": w.status.ssid, **details,
                   "online_since": w.online_since, "link": dict(w.link, method=probe_method()),
                   "rejoins": list(w.rejoins)[::-1], "hotspot": w.status.hotspot_ssid if w.status.mode == "setup" else None}
        albums = storage.albums()
        photos = videos = 0
        for a in albums:
            for i in storage.images(a["name"]):
                if i["type"] == "video":
                    videos += 1
                else:
                    photos += 1
        h = {
            "version": __version__,
            "time": time.time(),
            "system": health.system_info(),
            "power": health.power_info(),
            "storage": health.storage_info(frame.data_dir),
            "network": net,
            "display": frame.display.info(),
            "slideshow": {
                "current": frame.state.get("current"),
                "last_change": frame.last_rotation or None,
                "next_change": frame.next_rotation,
                "rotation_mode": cfg.get("rotation_mode"),
                "notice": frame.notice,
                "playing": str(frame.player.playing.name) if frame.player.playing else None,
            },
            "library": {
                "albums": len(albums), "photos": photos, "videos": videos,
                "pending_uploads": uploads.pending(),
                "jobs": frame.jobs.list() if frame.jobs else [],
                "video_support": frame.jobs is not None,
            },
        }
        cs = cloud.status()
        h["onedrive"] = {"connected": cs["connected"],
                         "accounts": [{"source": a["label"], "name": a["name"], "connected": a["connected"]} for a in cs["accounts"]],
                         "links": [{k: l.get(k) for k in ("source", "folder", "album", "last_sync", "last_ok", "last_result",
                                                             "files", "connected")} for l in cs["links"]]}
        h["backup"] = backups.health()
        h["github_update"] = autoupd.health()
        h["summary"] = health.summarise(h)
        return h

    @app.get("/api/maintenance")
    def maintenance():
        return jsonify(_maintenance())

    @app.get("/api/maintenance/events")
    def maintenance_events():
        return jsonify(health.EVENTS.recent(limit=int(request.args.get("limit", 200)),
                                            warnings_only=request.args.get("warnings") == "1"))

    @app.post("/api/maintenance/check-network")
    def maintenance_check_network():
        if frame.wifi is None:
            return _err("network checks need NetworkManager (they run on the Pi, not here)", 404)
        results = frame.wifi.check_now()
        log.info("manual network check: %s", ", ".join(f"{r['target']} {'OK' if r['ok'] else 'no answer'}" for r in results))
        return jsonify({"results": results})

    @app.post("/api/maintenance/restart")
    def maintenance_restart():
        log.warning("restart requested from the Maintenance tab")

        def later():
            time.sleep(1.0)
            os._exit(0)  # systemd (Restart=always) starts the frame again

        threading.Thread(target=later, daemon=True).start()
        return _ok("Restarting the frame software")

    @app.post("/api/maintenance/reboot")
    def maintenance_reboot():
        if not shutil.which("systemctl"):
            return _err("reboot isn't available here", 404)
        log.warning("reboot requested from the Maintenance tab")
        r = subprocess.run(["systemctl", "reboot"], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return _err(f"couldn't reboot: {(r.stderr or r.stdout).strip()} (re-run install.sh to allow it)", 500)
        return _ok("Rebooting")

    # ---------- SSH ----------
    def _ssh_state() -> dict:
        def sysctl(*args) -> str:
            try:
                return subprocess.run(["systemctl", *args, "ssh.service"], capture_output=True, text=True,
                                      timeout=10).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                return "unknown"
        host = socket.gethostname()
        active = sysctl("is-active") == "active"
        if not active:  # some systems start SSH on demand, from a socket
            try:
                active = subprocess.run(["systemctl", "is-active", "ssh.socket"], capture_output=True, text=True,
                                        timeout=10).stdout.strip() == "active"
            except (OSError, subprocess.TimeoutExpired):
                pass
        return {"active": active, "enabled": sysctl("is-enabled") == "enabled",
                "logins": firstrun.login_users(), "hostname": host,
                "helper": Path("/etc/systemd/system/pi-frame-system.path").exists() or bool(app.config.get("SETUP_HELPER"))}

    @app.get("/api/maintenance/ssh")
    def ssh_status():
        return jsonify(_ssh_state())

    @app.post("/api/maintenance/ssh")
    def ssh_change():
        if not cfg.get("http_password"):
            return _err("Set a page password first (Maintenance → Page password): an SSH login can do anything "
                        "on the Pi, so it's only managed here when the page is protected.", 403)
        d = request.get_json(silent=True) or {}
        what = str(d.get("what", ""))
        req = {"action": "ssh", "what": what}
        try:
            if what in ("create", "password"):
                user = str(d.get("username", "")).strip()
                pw = str(d.get("password", ""))
                if what == "create":
                    if firstrun.login_users():
                        return _err("This Pi already has a login, so another can't be added here.")
                    if not firstrun.USERNAME_RE.match(user) or user in firstrun.RESERVED:
                        return _err("Use lower-case letters and numbers for the username, starting with a letter.")
                elif user not in firstrun.login_users():
                    return _err(f"{user} isn't a login on this Pi")
                if len(pw) < 8:
                    return _err("The password needs at least 8 characters.")
                req.update(username=user, hash=firstrun.hash_password(pw))
            elif what not in ("on", "off"):
                return _err("unknown request")
        except firstrun.SetupError as e:
            return _err(str(e))
        res = (app.config.get("SETUP_HELPER") or firstrun.ask_helper)(frame.data_dir, req)
        if res.get("state") != "done":
            return _err(res.get("message") or "That didn't work")
        log.info("SSH: %s", res.get("message"))
        return _ok(res.get("message", "Done"), **_ssh_state())

    @app.post("/api/maintenance/reset")
    def maintenance_reset():
        """Back to a fresh frame: the root helper erases photos, settings,
        sign-ins and saved Wi-Fi (and optionally the SSH logins), then restarts."""
        if not cfg.get("http_password"):
            return _err("Set a page password first (Maintenance → Page password): resetting is only allowed "
                        "when the page is protected.", 403)
        d = request.get_json(silent=True) or {}
        if str(d.get("confirm", "")).strip().upper() != "RESET":
            return _err("Type RESET to confirm")
        if not Path("/etc/systemd/system/pi-frame-system.path").exists() and not app.config.get("SETUP_HELPER"):
            return _err("The system helper isn't installed. Re-run install.sh over SSH first.", 500)
        remove_logins = bool(d.get("remove_logins"))
        log.warning("reset to a fresh frame requested from the Maintenance tab%s",
                    " (removing SSH logins)" if remove_logins else "")
        frame.show_resetting()

        def go():
            time.sleep(2)  # let the answer reach the page first: the frame stops during the reset
            req = {"action": "factory_reset", "remove_logins": remove_logins}
            if app.config.get("SETUP_HELPER"):
                app.config["SETUP_HELPER"](frame.data_dir, req)
                return
            d2 = frame.data_dir / "system"
            d2.mkdir(exist_ok=True)
            tmp = d2 / "request.tmp"
            tmp.write_text(json.dumps({**req, "t": time.time()}))
            os.replace(tmp, d2 / "request.json")

        threading.Thread(target=go, name="reset", daemon=True).start()
        name = getattr(frame.wifi, "hotspot_ssid", "") if frame.wifi is not None else ""
        return _ok("Resetting. The frame restarts as new in about a minute.", hotspot=name)

    @app.get("/api/maintenance/diagnostics")
    def maintenance_diagnostics():
        import json as _json

        h = _maintenance()
        parts = [f"Pi Frame diagnostics (pi-photoframe {__version__}), {time.strftime('%Y-%m-%d %H:%M:%S %Z')}", "",
                 "== summary"] + [f"[{x['level']}] {x['area']}: {x['text']}" for x in h["summary"]]
        safe_cfg = {k: v for k, v in cfg.public().items() if "key" not in k and "token" not in k}
        parts += ["", "== status", _json.dumps(h, indent=2, default=str), "", "== settings (secrets removed)",
                  _json.dumps(safe_cfg, indent=2), "", "== recent activity"]
        parts += [f"{time.strftime('%H:%M:%S', time.localtime(e['t']))} {e['level']:7s} {e['area']}: {e['msg']}"
                  for e in health.EVENTS.recent(limit=400)[::-1]]
        for title, cmd in [("frame log (this boot)", ["journalctl", "-u", "pi-photoframe", "-b", "--no-pager", "-n", "400"]),
                           ("network log (this boot)", ["journalctl", "-u", "NetworkManager", "-b", "--no-pager", "-n", "200"]),
                           ("wifi log (this boot)", ["journalctl", "-u", "wpa_supplicant", "-b", "--no-pager", "-n", "100"]),
                           ("kernel warnings (this boot)", ["journalctl", "-k", "-b", "-p", "warning", "--no-pager", "-n", "200"])]:
            out = health._run(cmd, timeout=15)
            parts += ["", f"== {title}", out if out is not None else "(not available)"]
        name = f"pi-frame-diagnostics-{time.strftime('%Y%m%d-%H%M')}.txt"
        return Response("\n".join(parts), mimetype="text/plain",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ---------- OneDrive folder sync ----------
    def _cloud_err(e: Exception):
        return _err(str(e), 400)

    @app.get("/api/cloud")
    def cloud_status():
        return jsonify(cloud.status())

    @app.post("/api/cloud/auth/start")
    def cloud_auth_start():
        try:
            d = request.get_json(silent=True) or {}
            return jsonify({"url": cloud.start_auth(str(d.get("provider", "onedrive")), str(d.get("client_id", "")),
                                                    str(d.get("client_secret", "")))})
        except CloudError as e:
            return _cloud_err(e)

    @app.post("/api/cloud/auth/finish")
    def cloud_auth_finish():
        try:
            d = request.get_json(silent=True) or {}
            account = cloud.finish_auth(d.get("pasted", ""), d.get("provider") or None)
        except CloudError as e:
            return _cloud_err(e)
        return jsonify({"account": account})

    @app.post("/api/cloud/auth/cancel")
    def cloud_auth_cancel():
        cloud.cancel_auth()
        return _ok()

    @app.post("/api/cloud/disconnect")
    def cloud_disconnect():
        cloud.disconnect((request.get_json(silent=True) or {}).get("remote", "onedrive"))
        return _ok("Disconnected")

    @app.post("/api/cloud/nas")
    def cloud_nas():
        d = request.get_json(silent=True) or {}
        try:
            acct = cloud.add_nas(str(d.get("host", "")), str(d.get("user", "")), str(d.get("password", "")),
                                 str(d.get("domain", "")), str(d.get("label", "")))
        except CloudError as e:
            return _cloud_err(e)
        return jsonify({"account": acct})

    @app.get("/api/cloud/folders")
    def cloud_folders():
        try:
            return jsonify(cloud.list_folders(request.args.get("path", ""), request.args.get("remote", "onedrive")))
        except CloudError as e:
            return _cloud_err(e)

    @app.get("/api/cloud/count")
    def cloud_count():
        try:
            return jsonify(cloud.count_media(request.args.get("path", ""), request.args.get("subfolders", "1") == "1",
                                             request.args.get("remote", "onedrive")))
        except CloudError as e:
            return _cloud_err(e)

    @app.post("/api/cloud/links")
    def cloud_link_add():
        d = request.get_json(silent=True) or {}
        try:
            link = cloud.add_link(str(d.get("folder", "")), str(d.get("album", "")), d.get("subfolders", True),
                                  d.get("mirror", True), int(d.get("every_hours", 1)), str(d.get("remote", "onedrive")))
        except (CloudError, ValueError) as e:
            return _cloud_err(e)
        return jsonify(link)

    @app.patch("/api/cloud/links/<link_id>")
    def cloud_link_update(link_id):
        try:
            return jsonify(cloud.update_link(link_id, **(request.get_json(silent=True) or {})))
        except (CloudError, ValueError) as e:
            return _cloud_err(e)

    @app.delete("/api/cloud/links/<link_id>")
    def cloud_link_remove(link_id):
        try:
            cloud.remove_link(link_id, delete_album=request.args.get("delete_album") == "1")
        except CloudError as e:
            return _cloud_err(e)
        return _ok("Folder unlinked")

    @app.post("/api/cloud/links/<link_id>/sync")
    def cloud_link_sync(link_id):
        try:
            cloud._link(link_id)
        except CloudError as e:
            return _cloud_err(e)
        threading.Thread(target=cloud.sync, args=(link_id,), daemon=True).start()
        return _ok("Syncing")

    # ---------- backups ----------
    def _backup_password():
        if not cfg.get("http_password"):
            return _err("Set a password for this page first (Maintenance → Page password). Backups hold your "
                        "sign-ins, so they're only made and restored when the page is protected.", 403)
        return None

    @app.get("/api/backup")
    def backup_status():
        try:
            est = backups.estimate()
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't size up a backup: %s", e)
            est = None
        return jsonify({**backups.status(), "estimate": est, "password_set": bool(cfg.get("http_password"))})

    @app.get("/api/backup/download")
    def backup_download():
        if (resp := _backup_password()) is not None:
            return resp
        kind = request.args.get("kind", "settings")
        try:
            body = backups.stream(kind)
        except BackupError as e:
            return _err(str(e))
        return Response(body, mimetype="application/gzip", headers={
            "Content-Disposition": f'attachment; filename="{backup_name(kind)}"', "Cache-Control": "no-store"})

    @app.post("/api/backup/now")
    def backup_now():
        if (resp := _backup_password()) is not None:
            return resp
        if backups.running:
            return _err("A backup is already running", 409)
        threading.Thread(target=lambda: backups.back_up(auto=False), name="backup", daemon=True).start()
        return _ok(f"Backing up to {backups.where_label()}")

    @app.get("/api/backup/file/<name>")
    def backup_file(name):
        if (resp := _backup_password()) is not None:
            return resp
        try:
            path = backups.local_path(name)
        except BackupError as e:
            return _err(str(e), 404)
        return send_file(path, mimetype="application/gzip", as_attachment=True, download_name=path.name)

    @app.delete("/api/backup/file/<name>")
    def backup_file_delete(name):
        try:
            backups.delete_local(name)
        except BackupError as e:
            return _err(str(e), 404)
        return _ok("Backup deleted")

    @app.post("/api/backup/restore/start")
    def backup_restore_start():
        if (resp := _backup_password()) is not None:
            return resp
        d = request.get_json(silent=True) or {}
        try:
            return jsonify(backups.restore_start(str(d.get("name", "")), int(d.get("size", 0)),
                                                 int(d.get("last_modified", 0))))
        except (TypeError, ValueError) as e:
            return _err(str(e) if isinstance(e, BackupError) else "size must be a number")

    @app.put("/api/backup/restore/upload")
    def backup_restore_upload():
        if (resp := _backup_password()) is not None:
            return resp
        try:
            offset = int(request.args.get("offset", "-1"))
            length = int(request.headers.get("Content-Length", "0"))
            r = backups.restore_append(offset, request.stream, length)
        except BackupError as e:
            return _err(str(e))
        except ValueError:
            return _err("offset must be a number")
        return jsonify(r), (409 if r.get("mismatch") else 200)

    @app.delete("/api/backup/restore/upload")
    def backup_restore_discard():
        backups.restore_discard()
        return _ok("Discarded")

    @app.post("/api/backup/restore")
    def backup_restore():
        if (resp := _backup_password()) is not None:
            return resp
        src = str((request.get_json(silent=True) or {}).get("from", "upload"))
        try:
            info = backups.restore_begin(None if src == "upload" else backups.local_path(src))
        except BackupError as e:
            return _err(str(e))
        return _ok("Restoring. The frame restarts when it's done.", backup=info)

    # ---------- software update from the Maintenance tab ----------
    def _need_password():
        if not cfg.get("http_password"):
            return _err("Set a password for this page first (Maintenance → Page password). "
                        "Updates can install any code, so they're only allowed when the page is protected.", 403)
        return None

    @app.get("/api/maintenance/update")
    def update_status():
        return jsonify({**updates.status(), "password_set": bool(cfg.get("http_password"))})

    @app.post("/api/maintenance/update/upload")
    def update_upload():
        if (resp := _need_password()) is not None:
            return resp
        f = request.files.get("package")
        if f is None:
            return _err("choose the pi-photoframe zip file")
        try:
            info = updates.stage(f.stream)
        except UpdateError as e:
            return _err(str(e))
        log.info("update package uploaded: %s -> %s", info["current"], info["new"])
        return jsonify(info)

    # ---------- updates from GitHub ----------
    @app.get("/api/maintenance/update/github")
    def github_status():
        return jsonify(autoupd.status())

    @app.post("/api/maintenance/update/github/token")
    def github_token():
        if (resp := _need_password()) is not None:
            return resp
        try:
            return jsonify(autoupd.set_token(str((request.get_json(silent=True) or {}).get("token", ""))))
        except AutoUpdateError as e:
            return _err(str(e))

    @app.delete("/api/maintenance/update/github/token")
    def github_token_forget():
        autoupd.forget_token()
        return _ok("Token removed")

    @app.post("/api/maintenance/update/github/check")
    def github_check():
        try:
            return jsonify(autoupd.check())
        except AutoUpdateError as e:
            return _err(str(e))

    @app.post("/api/maintenance/update/github/install")
    def github_install():
        if (resp := _need_password()) is not None:
            return resp
        if autoupd.busy:
            return _err("Already working on it", 409)
        try:
            info = autoupd.install()
        except AutoUpdateError as e:
            return _err(str(e))
        return _ok(f"Installing {info['new']}", **info)

    @app.post("/api/maintenance/update/install")
    def update_install():
        if (resp := _need_password()) is not None:
            return resp
        try:
            info = updates.install()
        except UpdateError as e:
            return _err(str(e))
        log.warning("installing update %s -> %s from the Maintenance tab", info["current"], info["new"])
        frame.begin_update(info["new"], updates.status)  # the update screen, with progress
        return _ok(f"Installing version {info['new']}")

    @app.delete("/api/maintenance/update")
    def update_discard():
        updates.discard()
        return _ok("Discarded")

    @app.get("/api/ota/status")
    def ota_status():
        return jsonify(
            {
                "status": "idle",
                "state": "idle",
                "current_version": FIRMWARE_VERSION,
                "latest_version": FIRMWARE_VERSION,
                "update_available": False,
            }
        )

    @app.post("/api/ota/check")
    def ota_check():
        return _ok("update with: sudo /opt/pi-photoframe/deploy/install.sh --update")

    @app.post("/api/ota/update")
    def ota_update():
        return _err("over-the-air updates are not supported; use git pull + install.sh", 400)

    @app.post("/api/factory-reset")
    def factory_reset():
        cfg.factory_reset()
        frame.poke()
        return _ok("Settings reset to defaults")

    @app.post("/api/format-storage")
    def format_storage():
        return _err("not supported on the Pi; delete albums instead", 400)

    return app
