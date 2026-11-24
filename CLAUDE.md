# Notes for Claude

Context carried over from earlier chats, so a new session can pick up where the
last one left off. The guides in `docs/` (especially `docs/developers.md`) cover
the code layout, tests and releasing; this file covers the rest.

## The project

Pi Frame: a digital photo frame on a Raspberry Pi 4 (hostname `piframe`,
Raspberry Pi OS Trixie, Python 3.13) driving a 24" 2560 × 1440 panel over HDMI.
It speaks the esp32-photoframe HTTP API. Repository: `Jimmy3D-xyz/pi-frame`
(moved from `JimmySte/pi-frame` in 0.32.2).

## Working on it

- Plain, non-technical wording in the web page, docs and replies.
- The maintainer publishes releases: Actions → **Release** for every version (a
  minute or two). Actions → **Image** only when the first start changes, a new
  Raspberry Pi OS comes out, or now and then; with "Add it to this version's
  release" ticked it attaches the image to that version's release. Don't
  publish releases unless asked.
- Version numbers: last number for fixes (0.32.1), middle for features (0.33.0).
  Bump `photoframe/__init__.py`, run `python3 -m pytest tests -q -n 4 --dist loadfile`,
  commit, push.
- Keep the docs in step with changes (README, `docs/`).
- Security model (0.33.0): everyday page (optional page password) vs admin
  options (admin password, 15-minute unlock cookie); other host names and
  cross-site changes are refused. See `docs/developers.md#security`; new
  sensitive endpoints or settings go in `ADMIN_PREFIXES` / `ADMIN_KEYS`, and
  set-once settings in `SETUP_KEYS`.
- Page layout (0.34.0): Photos, Slideshow, Screen, Photo source, Maintenance.
  Set-once things live in admin cards in Maintenance (This frame, Frame
  hardware, Screen resolution); everyday tabs hold what the household changes.

- Security updates (0.35.0): Debian-Security only, nightly 03:40 by a root
  timer; the frame restarts the Pi for core updates only while the screen is
  off. Kernel/firmware from Raspberry Pi's archive are deliberately left out
  (display stack is fragile, see below).
- On-screen notice (0.35.0): `attention.py` watches the Maintenance summary
  (`WATCH` areas), shows after an hour; add new needs-a-person problems as
  summary lines in `health.summarise`.

## Things learned the hard way

- **Display hardware (vc4 KMS) must never shrink a picture on the top layer.**
  At 1440p, scaling down on the overlay plane over a full-screen primary
  overruns the hardware's memory bandwidth and freezes the Pi
  (`flip_done timed out`). Zooms run from a copy shrunk in advance
  (`kms.py: _zoom`, `_shrunk`); slides use whole pixels only (a fractional
  position scales the layer and the scaler reads past the picture: a white
  line). A step over 0.5 s turns moves off until restart.
- 3D turns are drawn ahead in background threads (`motion.Turn`) at up to
  1280 px wide, one frame per screen refresh, capped at a third of free memory.
- The Pi has no clock battery: the clock hides until timesyncd has synced
  (`overlay.clock_synced`).
- Away from home Wi-Fi the photos carry on with the hotspot up (`frame._go_away`).
