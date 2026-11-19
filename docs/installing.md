# Installing and updating

[← Back to the README](../README.md)

- [Before you start](#before-you-start)
- [Install with the kit](#install-with-the-kit)
- [Building the kit](#building-the-kit)
- [Installing by hand](#installing-by-hand)
- [What the installer changes](#what-the-installer-changes)
- [Backing up](#backing-up)
- [Moving to a new SD card](#moving-to-a-new-sd-card)
- [Updating](#updating)

## Before you start

- **Use a genuine, branded microSD card** (SanDisk, Samsung, Kingston…), 16 GB
  or more. Cheap unbranded cards fail in strange ways: a frame on one crashed
  at boot with `Caught <ILL>` because the card had silently damaged system files.
- **Write the card from Windows or Linux if a Mac gives trouble.** Raspberry
  Pi Imager on a Mac sometimes reports a verify error on a perfectly good
  card (the Mac's card reader, or Spotlight opening the new drive while it's
  being checked). The same card written on Windows worked first time.
- **Connect the monitor before powering on**, so the Pi picks up its resolution.

## Install with the kit

The kit is a folder, `pi-frame-kit`, holding the installer, a settings file
and the app package. It turns a freshly flashed card into a working frame
with one command.

1. **Flash the card.** In Raspberry Pi Imager choose *Raspberry Pi 4* and
   *Raspberry Pi OS Lite (64-bit)*. In the settings, set the hostname to
   `piframe`, a username and password you'll remember (this is the SSH
   password, separate from the frame's page password), your Wi-Fi, and switch
   on SSH with password login.
2. **Copy the kit.** Take the card out and put it back in so the `bootfs`
   drive appears, then copy the whole `pi-frame-kit` folder onto it.
   - To set the resolution, edit `kit.conf`: `RESOLUTION="2560x1440"`. Leave it
     empty to use the screen's own default; you can change it later from the website.
   - To bring back an old frame's photos and settings, put a backup of it
     inside the `pi-frame-kit` folder (see [Backing up](#backing-up)). If
     there are several, the newest is used.
3. **Boot and run the installer.** Put the card in the Pi and power on. The
   first boot sets itself up and may restart, so give it 2–3 minutes. Then,
   from Terminal (Mac) or PowerShell (Windows):

   ```sh
   ssh yourname@piframe.local
   sudo bash /boot/firmware/pi-frame-kit/install-frame.sh
   ```

   It waits for the internet, installs Pi Frame, sets the resolution, restores
   the backup if there is one, and restarts. It takes 10–15 minutes.
4. Open `http://piframe.local/` on your phone.

Everything the kit does is logged to `/var/log/pi-frame-kit.log`. If SSH
complains that the host key has changed (it will after reflashing a card),
run `ssh-keygen -R piframe.local` and connect again.

## Building the kit

From a copy of this repository:

```sh
kit/build-kit.sh            # writes dist/pi-photoframe.zip and dist/pi-frame-kit.zip
```

`pi-photoframe.zip` is the app package on its own: it's also what the
website's software update takes. `pi-frame-kit.zip` unzips to the
`pi-frame-kit` folder, with the package inside.

## Installing by hand

On a Pi with Raspberry Pi OS Lite, with a copy of this repository (or an
unzipped `pi-photoframe.zip`):

```sh
sudo ./deploy/install.sh                  # install, or re-install over the top
sudo ./deploy/install.sh --mode 2560x1440 # ...and set the screen resolution
sudo ./deploy/install.sh --no-boot-splash # ...and keep the normal boot text
sudo reboot
```

Running it again is safe: it keeps the photos and settings and only puts
right what's missing. It's also the fix for "Folder sync needs rclone" and
similar messages.

## What the installer changes

- **Packages**: Python (Flask, Pillow, NumPy, requests, waitress, qrcode),
  ffmpeg and mpv for videos, python3-kms++ for hardware fades, gpiozero and
  lgpio for buttons and the presence sensor, ddcutil for monitor brightness,
  avahi for `piframe.local`, dnsmasq for the setup hotspot, and
  libheif-examples for iPhone photos.
- **rclone** for synced folders, downloaded from rclone.org (checksum
  checked) into `/usr/local/bin`, because Debian's own version is too old
  for OneDrive.
- **The app** in `/opt/pi-photoframe`, run by the `pi-photoframe` service as
  a `photoframe` user, with its data in `/var/lib/pi-photoframe`.
- **Permissions**: that user may manage Wi-Fi, reboot and shut down (a polkit
  rule), and is in the video, render, audio, gpio, i2c and systemd-journal
  groups. It doesn't run as root.
- **Two small root helpers**, started only when the website asks:
  `pi-photoframe-update.path` installs software updates, and
  `pi-frame-system.path` changes the screen resolution and remakes the boot
  screen.
- **Boot settings** in `/boot/firmware`: the screen mode with 32-bit colour
  (with `--mode`), the Pi Frame boot screen, and the console moved off the
  screen. Each file is backed up first (`cmdline.txt.pre-photoframe`,
  `.pre-bootsplash`, `.pre-resolution`, `config.txt.pre-bootsplash`). See
  [Boot screen](screen-and-hardware.md#boot-screen).
- **System**: the login prompt on the screen is turned off, Wi-Fi power
  saving is turned off (it made the Pi slow to answer), a captive-portal rule
  is added for the setup hotspot, and logs are kept across restarts (up to 48 MB).

## Backing up

Maintenance → Backups. Backups are only made and restored once the page has
a password, because they hold your sign-ins.

**Two kinds of backup:**

- **Settings**: settings, albums, synced-folder sign-ins and links, and the
  frame's identity (so Home Assistant and the image server still know it).
  A few kilobytes.
- **Everything**: the same, plus the photos and videos you added. Photos from
  synced folders are left out, because they download again by themselves.

**Download one** with the Settings or Everything button. The file is named
like `piframe-backup-2026-11-20-0300-full.tgz`.

**Automatic backups** run every day, week or month at a time you choose,
and keep the last few (4 by default). They can go to:

- **Your NAS**: the NAS account needs permission to write to the backup
  folder. If you gave the frame a read-only account for syncing, give it
  write access to one folder (e.g. `Backups/Pi Frame`).
- **OneDrive, Dropbox or Google Drive**: the connection you use for synced
  folders. Google Drive connections made before version 0.24 are read-only:
  disconnect and sign in again to allow backups. The frame can then only
  change files it made itself.
- **This frame's SD card**: handy before trying something risky, but no help
  if the card fails.

The Maintenance tab shows the last backup and warns if one fails. A failed
automatic backup is tried again a few hours later.

**Restore** from Maintenance → Backups → Choose a backup file, or from a
backup kept on the frame. The frame first keeps a copy of its current
settings (listed under "Backups on this frame"), then puts back the backup's
photos, settings and sign-ins, and restarts. Photos already on the frame stay.
Synced folders download their photos again afterwards.

**Over SSH**, a backup can be made with:

```sh
cd /opt/pi-photoframe
sudo -u photoframe python3 -m photoframe.backup create --kind full --out /tmp/piframe-backup.tgz
python3 -m photoframe.backup inspect /tmp/piframe-backup.tgz     # check one, and see what's in it
```

Backups made by hand with
`tar czf frame-backup.tgz -C /var/lib pi-photoframe` (as these docs used to
say) still restore, from the website or the kit.

Keep backups somewhere private: they hold sign-in tokens and the page password.

## Moving to a new SD card

Build a fresh card rather than cloning the old one. A clone copies any
damage along with everything else, and a fresh install takes about the same
time:

1. Download an **Everything** backup from the old frame (Maintenance →
   Backups), or use its latest automatic backup. If the old frame doesn't
   start, put its card in a Linux PC or another Pi and copy its
   `/var/lib/pi-photoframe` folder instead.
2. Follow [Install with the kit](#install-with-the-kit), with the backup in
   the kit folder. (Or install with an empty kit and restore the backup from
   the website afterwards.)

The boot screen and screen settings are remade for whatever monitor the new
card starts with.

## Updating

**From the website** (Maintenance → Software update): upload a new
`pi-photoframe.zip`. The frame checks it's a genuine package and shows the
version change before you confirm. The updater keeps a copy of the current
version, runs the new package's installer, and checks the frame comes back
up on the new version within 90 seconds; if anything goes wrong, the
previous version is put back by itself. The screen shows an "Updating Pi
Frame" screen with the steps while it works, then "Updated to version …".

Because an update can install any code, it only works once the page has a
password (Maintenance → Page password).

**Over SSH**: copy the zip to the Pi, unzip it, and run its installer:

```sh
unzip -o pi-photoframe.zip && cd pi-photoframe && sudo ./deploy/install.sh
```
