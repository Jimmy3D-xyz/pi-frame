# Getting started

[← Back to the README](../README.md)

From a Raspberry Pi in a box to your photos on the wall, in about half an
hour. Each step links to the full guide if you want more detail.

- [1. What you need](#1-what-you-need)
- [2. Install Pi Frame](#2-install-pi-frame)
- [3. The first ten minutes](#3-the-first-ten-minutes)
- [4. Set it and forget it](#4-set-it-and-forget-it)
- [5. Where next](#5-where-next)

## 1. What you need

- **A Raspberry Pi 4** and **its official power supply**. Weak phone chargers cause odd problems; the frame warns you if
  the power is too low.
- **An HDMI monitor**, plus a micro-HDMI to HDMI cable (the Pi 4 has the
  small sockets). A desktop monitor with DDC/CI lets the frame dim it at
  night and put it in standby.
- **A microSD card from a known brand** (SanDisk, Samsung, Kingston…),
  16 GB or more. Cheap unbranded cards fail in strange ways.
- **A phone** to set it up and look after it.
- Optional, for later: three push buttons (previous, next, screen) and a
  24 GHz presence sensor. See [Screen and hardware](screen-and-hardware.md).

## 2. Install Pi Frame

Choose one.

### The easy way: the ready-made image

Nothing to type: you set the frame up from your phone.

1. From the newest [release](https://github.com/Jimmy3D-xyz/pi-frame/releases),
   download `pi-frame-<version>.img.xz` (the release notes link to it if it
   isn't attached to that release).
2. Write it to the card with [Raspberry Pi Imager](https://www.raspberrypi.com/software/):
   *Raspberry Pi 4* → Operating system → **Use custom** → the file you
   downloaded → your card. When Imager offers OS customisation, choose **No**.
3. Put the card in the Pi, connect the monitor, then the power. The first
   start takes a couple of minutes.
4. The screen shows two QR codes. Scan the first to join the frame's own
   Wi-Fi, then the second to open its setup page.
5. The welcome wizard asks for a name, your country, time zone and Wi-Fi,
   and an **admin password** (keep it somewhere safe: it unlocks updates,
   backups and the other important settings).
6. When it's done, switch your phone back to your Wi-Fi and scan the QR code
   on the screen: that's the frame's page. Bookmark it; it's also at
   `http://piframe.local/`.

More: [the ready-made image and the wizard](installing.md#install-with-the-ready-made-image).

### If you're happy with Raspberry Pi Imager and SSH: one command

1. Write **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. In its
   settings, set the hostname to `piframe`, a username and password, your
   Wi-Fi, and switch on SSH.
2. Start the Pi with the monitor connected and give it 2–3 minutes.
3. From Terminal (Mac) or PowerShell (Windows):

   ```sh
   ssh yourname@piframe.local
   curl -fsSL https://github.com/Jimmy3D-xyz/pi-frame/releases/latest/download/install.sh | sudo bash
   ```

   It takes 10–15 minutes and restarts at the end.
4. Open `http://piframe.local/` on your phone, go to **Maintenance**, and set
   an admin password at the top.

More: [install with one command](installing.md#install-with-one-command),
including setting the screen resolution.

## 3. The first ten minutes

Everything happens on the frame's page (`http://piframe.local/`).

**Add your photos.** Either:

- **Upload them**: on the **Photos** tab, tap the box and choose photos and
  videos from your phone (iPhone photos work as they are). They go into
  the Default album.
- **Or sync a folder** from OneDrive, Google Drive, Dropbox or a NAS: on
  **Maintenance**, unlock the admin options with your admin password, then
  go to **Photo source → Synced folders**, sign in, and pick a folder. It
  becomes its own album and stays up to date by itself. See
  [Synced folders](synced-folders.md).

Each album has an **Include this album in the slideshow** tick box: untick
any you'd rather leave out.

**Choose how it looks.** On the **Slideshow** tab:

- **Slideshow styles**: Classic (one photo at a time), Story, Spotlight or
  a Living collage. Tick several to take turns.
- **Changing photos**: how often (every 30 minutes, say) and in what order.
- **How photos look and change**: fit the whole photo or fill the screen,
  and the transition between photos.

**Choose when it's on.** On the **Screen** tab, set **Screen on and off**
(e.g. on at 07:00, off at 22:30), and if you like, the clock, date and
weather under **On-screen info**.

Remember to press each section's **Save** button.

## 4. Set it and forget it

Three things worth doing once, all on **Maintenance** with the admin
options unlocked:

- **Backups → Automatic backups**: a daily or weekly backup to your NAS,
  OneDrive, Dropbox or Google Drive, so a worn-out SD card is quick to
  recover from: install on a new card and restore.
  See [Backing up](installing.md#backing-up).
- **Software update → New versions**: leave it on **Tell me here**, or
  choose **Install automatically** to have new versions installed overnight
  (with the old one kept in case anything goes wrong).
- **Passwords**: optionally add a **page password** too, if people you
  don't want changing the photos use your Wi-Fi. Everyday use of the page
  doesn't need the admin password.

After that the frame looks after itself: it installs security updates each
night, fixes its Wi-Fi when it can, and keeps synced folders up to date. If
something needs you, a small **"Pi Frame needs attention"** panel appears
on the screen; open Maintenance to see why
([more](using.md#when-the-frame-needs-attention)).

## 5. Where next

| | |
| --- | --- |
| [Using the frame](using.md) | Slideshow styles in detail, videos, on-screen info, demo mode and the showcase, the live view, away from home |
| [Synced folders](synced-folders.md) | OneDrive, Google Drive, Dropbox and NAS |
| [Screen and hardware](screen-and-hardware.md) | Resolution, brightness, buttons, the presence sensor, portrait mounting |
| [Installing and updating](installing.md) | Every way to install, backups, moving to a new card, updates |
| [Troubleshooting](troubleshooting.md) | When something isn't right |
