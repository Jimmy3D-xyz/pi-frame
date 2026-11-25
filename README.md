<p align="center"><img src="photoframe/static/logo.svg" width="112" alt="Pi Frame logo"></p>

<h1 align="center">Pi Frame</h1>

<p align="center"><b>Turn a Raspberry Pi 4 and any HDMI monitor into a digital photo frame, run from your phone.</b></p>

<p align="center">
  <a href="https://github.com/Jimmy3D-xyz/pi-frame/releases/latest"><img alt="Latest version" src="https://img.shields.io/github/v/release/Jimmy3D-xyz/pi-frame?label=version"></a>
  <a href="https://github.com/Jimmy3D-xyz/pi-frame/actions/workflows/tests.yml"><img alt="Tests" src="https://github.com/Jimmy3D-xyz/pi-frame/actions/workflows/tests.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="MIT licence" src="https://img.shields.io/github/license/Jimmy3D-xyz/pi-frame?label=licence"></a>
  <img alt="Raspberry Pi 4" src="https://img.shields.io/badge/Raspberry%20Pi-4-C51A4A?logo=raspberrypi&logoColor=white">
  <img alt="Raspberry Pi OS Trixie, 64-bit" src="https://img.shields.io/badge/Raspberry%20Pi%20OS-Trixie%2064--bit-C51A4A">
  <img alt="Python 3.13" src="https://img.shields.io/badge/python-3.13-3776AB?logo=python&logoColor=white">
</p>

<p align="center">
  <a href="docs/getting-started.md"><b>Getting started</b></a> ·
  <a href="#install">Install</a> ·
  <a href="docs/using.md">Using the frame</a> ·
  <a href="docs/troubleshooting.md">Troubleshooting</a> ·
  <a href="https://github.com/Jimmy3D-xyz/pi-frame/releases">Releases</a>
</p>

Photos and videos from the Pi itself, your OneDrive, Google Drive, Dropbox or
NAS, shown with smooth fades, zooms, collages and 3D turns. Everything is set
from a web page on your phone: no keyboard, no app. It looks after itself
too, with a screen schedule, safe automatic updates, nightly security
updates and backups.

**New here? Start with [Getting started](docs/getting-started.md)**: from a
Pi in a box to your photos on the wall in about half an hour.

Pi Frame also works as a member of the
[esp32-photoframe](https://github.com/aitjcize/esp32-photoframe) family: it
speaks the same HTTP API, so the
[image server](https://github.com/aitjcize/esp32-photoframe-server) and the
[Home Assistant integration](https://github.com/aitjcize/ha-esp32-photoframe)
work with it unchanged.

## Highlights

- **Photos and videos**: upload from your phone, or sync folders from OneDrive, Google Drive, Dropbox or a NAS
- **Smooth 60 fps fades, zooms and 3D turns** done by the Pi's display hardware, even at 1440p
- **Collages and slideshow styles** (including a living collage), photos around videos that don't fill the screen, short video moments, and a Video button that plays a video inside the collage
- **On-screen info**: clock, weather, when and where each photo was taken, jokes and fun facts
- **Looks after itself**: screen schedule, presence sensor, monitor brightness and standby over HDMI,
  automatic Wi-Fi repair with a setup hotspot, a health page, safe updates (automatically from GitHub, if you like),
  and a reset to start afresh
- **Backups**: download one, or back up automatically to your NAS, OneDrive, Dropbox or Google Drive,
  and restore from the website or onto a new card
- **Physical buttons** for previous, next, and screen / power
- **Live view** of what's on the frame, from any browser
- **Demo mode**, like a TV in a shop: public-domain paintings and NASA photos, with slides about the frame

## What you need

- A Raspberry Pi 4 and its power supply
- An HDMI monitor (a desktop monitor with DDC/CI lets the frame set its brightness and standby)
- A good-quality microSD card, 16 GB or more, from a known brand
- Optional: three push buttons, a 24 GHz mmWave presence sensor

## Install

- **Easiest:** write the ready-made image (from
  [Releases](https://github.com/Jimmy3D-xyz/pi-frame/releases)) to a card,
  switch on, and set it up from your phone.
- **Already running Raspberry Pi OS Lite (64-bit)?** Over SSH:

  ```sh
  curl -fsSL https://github.com/Jimmy3D-xyz/pi-frame/releases/latest/download/install.sh | sudo bash
  ```

Step by step: [Getting started](docs/getting-started.md). Every option,
including the installer kit, moving to a new card and updating:
[Installing and updating](docs/installing.md).

## Licence

MIT: see [LICENSE](LICENSE). Found a security problem? See [SECURITY.md](SECURITY.md).

## Guides

| | |
| --- | --- |
| [Getting started](docs/getting-started.md) | The short version: what to buy, installing, the first ten minutes, and setting it to look after itself |
| [Installing and updating](docs/installing.md) | The ready-made image and welcome wizard, the installer kit, backups and restoring, moving to a new card, updates |
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
