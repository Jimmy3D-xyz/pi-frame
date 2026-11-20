# For developers

[← Back to the README](../README.md)

- [Code layout](#code-layout)
- [Running it on a computer](#running-it-on-a-computer)
- [Tests](#tests)
- [Settings for the service](#settings-for-the-service)
- [API](#api)
- [The image server patch](#the-image-server-patch)
- [Releasing](#releasing)

## Code layout

A Python (Flask) app, started with `python3 -m photoframe`.

| Path | What's there |
| --- | --- |
| `photoframe/__main__.py` | Start-up: settings, display, web server, background jobs |
| `photoframe/frame.py` | The slideshow: what to show next, schedules, screen power |
| `photoframe/web.py` | The website and HTTP API |
| `photoframe/config.py` | Settings and their defaults (`DEFAULT_CONFIG`) |
| `photoframe/display.py` | Screens: framebuffer, no-screen (headless), PNG file for testing |
| `photoframe/kms.py` | The display-hardware screen: double buffering and 60 fps fades |
| `photoframe/console.py` | Keeps the console in graphics mode so no text lands on photos |
| `photoframe/render.py`, `collage.py`, `surround.py` | Fitting photos to the screen, collages, photos around videos |
| `photoframe/overlay.py` | On-screen info: clock, weather, photo details, jokes |
| `photoframe/screens.py`, `brand.py` | Welcome, QR, setup, update and shutdown screens |
| `photoframe/storage.py`, `uploads.py` | Albums, imports, resumable uploads |
| `photoframe/video.py` | Video checks, conversion, loudness, playback with mpv |
| `photoframe/cloud.py` | Synced folders (rclone) |
| `photoframe/fetcher.py`, `cron.py` | Image-server URLs and the firmware's schedule rules |
| `photoframe/network.py` | Wi-Fi status, setup hotspot, link check |
| `photoframe/monitor.py` | Monitor brightness and standby (DDC/CI) |
| `photoframe/presence.py`, `buttons.py` | Presence sensor and physical buttons (gpiozero) |
| `photoframe/health.py` | The Maintenance tab's checks |
| `photoframe/updater.py` | Checking and handing over web updates |
| `photoframe/autoupdate.py` | Checking GitHub releases, downloading and installing them |
| `photoframe/firstrun.py` | A new frame's welcome wizard |
| `photoframe/backup.py` | Backups (also a command: `python3 -m photoframe.backup create\|inspect`), automatic backups, restoring |
| `photoframe/sysmodes.py` | Reading the screen's resolutions and the boot setting |
| `photoframe/static/` | The web pages (`index.html`, `setup.html`, `live.html`), icons, wiring diagram |
| `photoframe/data/fun.json` | The jokes and facts |
| `deploy/install.sh` | The installer |
| `deploy/update.sh` | The root updater: back up, install, health check, roll back |
| `deploy/system_helper.py` | Root helper for resolution changes and the boot screen |
| `deploy/bootsplash.py`, `console_splash.py` | The boot screen |
| `deploy/*.service`, `*.path`, `*.rules` | systemd units and the polkit rule |
| `kit/` | The installer kit, and `build-kit.sh` to package it |
| `server-patch/` | Full-colour patch for esp32-photoframe-server |
| `tests/` | pytest tests |

The frame runs as an unprivileged `photoframe` user. Anything that needs root
(installing updates, changing boot settings) is done by a helper that a
systemd `.path` unit starts when the frame drops a request file in
`/var/lib/pi-photoframe/update/` or `/var/lib/pi-photoframe/system/`. The
helpers check every request themselves.

## Running it on a computer

It runs on Linux or macOS, drawing frames to a PNG file instead of a screen:

```sh
pip install flask pillow numpy requests qrcode pytest
sudo apt install ffmpeg      # for video uploads; playback is simulated, so mpv isn't needed
python3 -m photoframe --data-dir /tmp/pf --display file:1920x1080:/tmp/frame.png --port 8080
```

Then open `http://localhost:8080/`. `--display` also takes `fb` (the default:
display hardware if available, else `/dev/fb0`), `fb:/dev/fb1` and `kms`.

## Tests

```sh
python3 -m pytest tests                    # everything, about 2 minutes
python3 -m pytest tests/test_kms.py -q     # one area
```

The tests use fake screens, fake GPIO and fake display hardware, so they run
anywhere. A few need extra tools and are skipped without them: ffmpeg (and mpv)
for video tests, rclone and a local SMB server for sync tests, gpiozero for
the button and sensor tests.

## Settings for the service

Set in `deploy/pi-photoframe.service` with `Environment=` lines:

| Variable | Default | Purpose |
| --- | --- | --- |
| `PHOTOFRAME_DATA` | `/var/lib/pi-photoframe` | Data folder |
| `PHOTOFRAME_DISPLAY` | `fb` | Screen (see above) |
| `PHOTOFRAME_PORT`, `PHOTOFRAME_HOST` | `80`, `0.0.0.0` | Web server |
| `PHOTOFRAME_WIFI_SETUP` | `1` | `0` turns off the setup hotspot |
| `PHOTOFRAME_WIFI_IFACE` | `wlan0` | Wi-Fi interface |
| `PHOTOFRAME_AUDIO_DEVICE` | | mpv audio device, e.g. `alsa/hdmi:CARD=vc4hdmi0,DEV=0` (list with `aplay -L`) |
| `PHOTOFRAME_AO` | `alsa` | mpv audio output |
| `PHOTOFRAME_MPV_ARGS` | | Extra mpv options |
| `PHOTOFRAME_SCREEN_OFF_CMD`, `PHOTOFRAME_SCREEN_ON_CMD` | | Extra commands to switch the monitor off and on (e.g. HDMI-CEC) |

After editing: `sudo systemctl daemon-reload && sudo systemctl restart pi-photoframe`.
A re-install replaces the file, so keep a note of any changes.

## API

All the firmware endpoints in esp32-photoframe's `docs/API.md` work (`/api/rotate`,
`/api/display-image`, `/api/config`, `/api/images`, `/api/upload`, …).
E-paper-only ones (palette calibration, OTA, battery) answer with friendly
errors or placeholder values. Pi Frame adds:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/status` | Current picture, next change, screen state, display details |
| `POST /api/previous` | Previous picture |
| `GET/POST /api/screen` | Read or set screen power: `{"on": true}` |
| `POST /api/sleep` | Screen off |
| `GET /api/current_image?size=large\|full` | The picture on screen (with on-screen info) |
| `GET /live` | Live view page |
| `PUT /api/albums/enabled?name=…` | Include or leave out an album |
| `POST /api/upload/start`, `PUT /api/upload/<id>`, `POST /api/upload/<id>/finish`, `DELETE /api/upload/<id>`, `GET /api/uploads/pending` | Resumable uploads in pieces |
| `GET /api/jobs` | Video conversion progress |
| `POST /api/video/stop` | Stop the playing video |
| `GET /api/weather`, `GET /api/weather/search?q=…` | Weather, and the place search |
| `GET /api/monitor`, `POST /api/monitor/probe` | Monitor brightness support (DDC/CI) |
| `GET /api/display/modes`, `POST /api/display/detect`, `POST /api/display/mode` | Screen resolutions |
| `GET /api/presence`, `GET /api/buttons` | Presence sensor and button state |
| `GET /api/cloud`, `POST /api/cloud/auth/start\|finish\|cancel`, `POST /api/cloud/disconnect`, `POST /api/cloud/nas` | Synced-folder accounts |
| `GET /api/cloud/folders`, `GET /api/cloud/count` | Browse a service's folders |
| `POST /api/cloud/links`, `PATCH/DELETE /api/cloud/links/<id>`, `POST /api/cloud/links/<id>/sync` | Synced folders |
| `GET /api/wifi/status`, `GET /api/wifi/networks`, `POST /api/wifi/connect`, `GET /setup` | Wi-Fi setup |
| `GET /api/maintenance`, `GET /api/maintenance/events` | Health and recent activity |
| `POST /api/maintenance/check-network\|restart\|reboot`, `GET /api/maintenance/diagnostics` | Maintenance actions |
| `GET/DELETE /api/maintenance/update`, `POST /api/maintenance/update/upload\|install` | Software updates |
| `GET /api/backup` | Backup settings, sizes, last and next backup, backups kept on the frame |
| `GET /api/backup/download?kind=settings\|full` | Download a new backup |
| `POST /api/backup/now` | Back up now, to the chosen place |
| `GET/DELETE /api/backup/file/<name>` | A backup kept on the frame |
| `POST /api/backup/restore/start`, `PUT /api/backup/restore/upload?offset=…`, `DELETE /api/backup/restore/upload` | Upload a backup to restore, in pieces |
| `GET /api/maintenance/update/github`, `POST …/github/check`, `POST …/github/install` | Updates from GitHub |
| `POST/DELETE /api/maintenance/update/github/token` | The read-only GitHub token |
| `GET/POST /api/maintenance/ssh` | SSH status; create a login (only when none exists), change its password, SSH on/off |
| `POST /api/maintenance/reset` | Reset to a fresh frame (`{"confirm": "RESET", "remove_logins": false}`) |
| `GET/POST /api/setup` | The welcome wizard (only on a frame that hasn't been set up) |
| `POST /api/backup/restore` | Restore it (`{"from": "upload"}` or a kept backup's name); the frame restarts |

When the page has a password, the API needs it too (HTTP basic auth). Extra
config fields beyond the firmware's are listed with their defaults in
`DEFAULT_CONFIG` in `photoframe/config.py`.

## The image server patch

The stock [esp32-photoframe-server](https://github.com/aitjcize/esp32-photoframe-server)
turns every photo into 6-colour dithered e-paper output, which looks poor on
a monitor. `server-patch/full-colour-frames.patch` adds a full-colour path:
frames that report `display_type: rgb` get a plain JPEG. Everything else
(sources, collages, overlays, history, settings sync) is unchanged, and
e-paper frames are unaffected.

```sh
git clone https://github.com/aitjcize/esp32-photoframe-server
cd esp32-photoframe-server
git apply /path/to/pi-frame/server-patch/full-colour-frames.patch
docker build -t esp32-photoframe-server:full-colour .
```

To run it as the Home Assistant add-on, copy the patched repository into
`/addons/` on the HA host (via the Samba or SSH add-on) and install it from
Settings → Add-ons → Add-on store → Local add-ons (upstream's `deploy-dev.sh`
automates this). The patch is `gofmt`-clean but hasn't been compiled yet, so
check `docker build` succeeds first.

## Releasing

1. Bump `__version__` in `photoframe/__init__.py`: the last number for a
   small fix (0.26.1), the middle one for new features (0.27.0).
2. Run the tests, commit and push.
3. Publish it, from GitHub's **Actions** tab (works from a phone too):
   - **feature versions** (`X.Y.0`): workflow **Image** → Run workflow → tick
     **Publish it as the release of this version**. Builds a new ready-made
     image, and releases it with the update package and the kit (about 10 minutes).
   - **small fixes**: workflow **Release** → Run workflow. The update package
     and the kit only (a minute or two).

   Pushing a tag (`git tag v0.27.0 && git push origin v0.27.0`) does the same.
   Either refuses a version that's already been released.

Frames with automatic updates install the latest full release (not
pre-releases such as `pi-frame-test`), so publishing a release is what ships a version to them.

Build a new image by hand (Actions → Image → Run workflow) when the first
start changes (the wizard, `install.sh`, Wi-Fi setup) in a small fix, or
when Raspberry Pi releases a new OS. `kit/build-kit.sh` makes the two zips
locally.

Every push to GitHub runs the tests automatically (on Python 3.13, as in
Raspberry Pi OS Trixie), then builds both zips. They're
on the run's page under the Actions tab, kept for 30 days.
