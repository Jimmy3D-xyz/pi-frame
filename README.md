<p align="center"><img src="photoframe/static/logo.svg" width="112" alt="Pi Frame logo"></p>

# Pi Frame

[![Tests](https://github.com/JimmySte/pi-frame/actions/workflows/tests.yml/badge.svg)](https://github.com/JimmySte/pi-frame/actions/workflows/tests.yml)

A digital photo frame for a Raspberry Pi 4 and an HDMI monitor. Photos and
videos from the Pi itself, your OneDrive, Google Drive, Dropbox or NAS, run
from a web page on your phone.

Pi Frame also works as a member of the
[esp32-photoframe](https://github.com/aitjcize/esp32-photoframe) family: it
speaks the same HTTP API, so the
[image server](https://github.com/aitjcize/esp32-photoframe-server) and the
[Home Assistant integration](https://github.com/aitjcize/ha-esp32-photoframe)
work with it unchanged.

## Highlights

- **Photos and videos**: upload from your phone, or sync folders from OneDrive, Google Drive, Dropbox or a NAS
- **Smooth 60 fps fades** done by the Pi's display hardware, even at 1440p
- **Collages and slideshow styles**, photos around videos that don't fill the screen, and short video moments
- **On-screen info**: clock, weather, when and where each photo was taken, jokes and fun facts
- **Looks after itself**: screen schedule, presence sensor, monitor brightness and standby over HDMI,
  automatic Wi-Fi repair with a setup hotspot, a health page, and safe updates from the website
- **Physical buttons** for previous, next, and screen / power
- **Live view** of what's on the frame, from any browser

## What you need

- A Raspberry Pi 4 and its power supply
- An HDMI monitor (a desktop monitor with DDC/CI lets the frame set its brightness and standby)
- A good-quality microSD card, 16 GB or more, from a known brand
- Optional: three push buttons, a 24 GHz mmWave presence sensor

## Install

1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. In its
   settings choose the hostname `piframe`, your Wi-Fi, and switch on SSH.
2. Copy the **installer kit** (`pi-frame-kit` folder) onto the card's `bootfs` drive.
3. Boot the Pi with the monitor connected, then:

   ```sh
   ssh yourname@piframe.local
   sudo bash /boot/firmware/pi-frame-kit/install-frame.sh
   ```

4. After it restarts, open `http://piframe.local/` on your phone.

The full guide, including building the kit, moving to a new card and
updating, is in [Installing and updating](docs/installing.md).

## Guides

| | |
| --- | --- |
| [Installing and updating](docs/installing.md) | The installer kit, backups, moving to a new card, software updates |
| [Using the frame](docs/using.md) | Photos and videos, slideshow styles, on-screen info, live view, Wi-Fi setup |
| [Synced folders](docs/synced-folders.md) | OneDrive, Google Drive, Dropbox and NAS |
| [Screen and hardware](docs/screen-and-hardware.md) | Resolution, fades, boot screen, brightness, presence sensor, buttons |
| [Troubleshooting](docs/troubleshooting.md) | The Maintenance tab, logs, and fixes for problems we've hit |
| [For developers](docs/developers.md) | Code layout, running it on a PC, tests, the API, the image server |

## Status

Running daily on a Raspberry Pi 4 with a 24" 1440p monitor. The image-server
patch in `server-patch/` hasn't been compiled yet.

Internally the project is still called `pi-photoframe` (the service, the
`/opt/pi-photoframe` and `/var/lib/pi-photoframe` folders, the update
package), so existing frames keep updating.
