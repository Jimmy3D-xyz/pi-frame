# Installing and updating

[← Back to the README](../README.md)

- [Before you start](#before-you-start)
- [Install with the ready-made image](#install-with-the-ready-made-image) (easiest)
- [The welcome wizard](#the-welcome-wizard)
- [Building the image](#building-the-image)
- [Install with the kit](#install-with-the-kit)
- [Building the kit](#building-the-kit)
- [Installing by hand](#installing-by-hand)
- [What the installer changes](#what-the-installer-changes)
- [Backing up](#backing-up)
- [Moving to a new SD card](#moving-to-a-new-sd-card)
- [Updating](#updating), including [automatic updates from GitHub](#automatic-updates-from-github)
- [Reset to a fresh frame](#reset-to-a-fresh-frame)

## Before you start

- **Use a genuine, branded microSD card** (SanDisk, Samsung, Kingston…), 16 GB
  or more. Cheap unbranded cards fail in strange ways: a frame on one crashed
  at boot with `Caught <ILL>` because the card had silently damaged system files.
- **Write the card from Windows or Linux if a Mac gives trouble.** Raspberry
  Pi Imager on a Mac sometimes reports a verify error on a perfectly good
  card (the Mac's card reader, or Spotlight opening the new drive while it's
  being checked). The same card written on Windows worked first time.
- **Connect the monitor before powering on**, so the Pi picks up its resolution.

## Install with the ready-made image

The ready-made image is Raspberry Pi OS Lite (64-bit) with Pi Frame already
installed. Nothing needs typing on a computer: the frame sets itself up from
your phone.

1. **Download** `pi-frame-<version>.img.xz` from the repository's
   [Releases](https://github.com/JimmySte/pi-frame/releases) page.
2. **Write it** with Raspberry Pi Imager: *Raspberry Pi 4*, then Operating
   system → **Use custom** → the downloaded file, then your card. When Imager
   offers to apply OS customisation settings, choose **No**: the frame asks
   for everything itself. (If you do fill them in, the wizard skips what
   they covered.)
3. **Start the frame**: card in, monitor connected, power on. The first start
   takes a couple of minutes (it grows the system to fill the card and restarts once).
4. **Join the frame's Wi-Fi.** The screen shows two QR codes: the first joins
   the frame's own network (`PiFrame-XXXX`), the second opens the setup page.
   Most phones open it by themselves.
5. **Answer the [welcome wizard](#the-welcome-wizard)**. When you finish, the
   frame joins your Wi-Fi and shows a QR code for its page. Switch your phone
   back to your Wi-Fi and scan it.

With an Ethernet cable plugged in, there's no hotspot: the frame shows a QR
code for its page straight away, and the wizard opens there.

Moving from another frame? Restore its backup afterwards, under Maintenance →
Backups.

## The welcome wizard

It opens on a new frame's page until it's finished, and asks for:

- **Name**, e.g. "Living room" (shown on the page and in Home Assistant).
- **Country**: decides which Wi-Fi channels the frame may use. A new Pi
  keeps Wi-Fi switched off until it knows this, so the image switches the
  radio on just for the setup hotspot.
- **Time zone**, filled in from your phone.
- **Your Wi-Fi network** and its password (skipped on Ethernet).
- **A page password** (recommended: backups and updates need one).
- **An SSH login** (optional): a username and password for working on the Pi
  from a computer (`ssh name@piframe.local`). Without one, SSH stays switched
  off. The wizard can only create a login while the Pi has none, so the page
  can't be used to add one later.

If the frame can't join your Wi-Fi (a mistyped password, say), its setup
screen comes back: join its network again and choose the network once more.
Everything else is already saved.

The image itself has no Wi-Fi settings, no login and SSH switched off, and
the frame's name on the network is `piframe`.

## Building the image

GitHub builds a new image for feature versions (tags like `v0.26.0`) and
publishes a release with the image, the update package and the kit. It takes
about 10 minutes (on GitHub's Arm machines, so nothing needs emulating), so small fixes (`v0.26.1`…) get a release with just the
update package and the kit: a frame set up from the latest image updates
itself from the Maintenance tab. The image can also be built by hand from the
Actions tab (workflow "Image"), e.g. when Raspberry Pi releases a new version
of its OS; that publishes a test release called `pi-frame-test`.

To build one yourself, on a Linux machine with `qemu-user-static` (not
needed on Arm), `xz-utils`, `fdisk`, `e2fsprogs` and `zerofree`:

```sh
sudo deploy/build-image.sh       # writes dist/pi-frame-<version>.img.xz
```

It downloads the latest Raspberry Pi OS Lite (64-bit), checks its checksum,
installs Pi Frame inside it (`install.sh --image`), checks the result,
shrinks it and compresses it.

## Install with the kit

For a card you've written yourself with Raspberry Pi OS Lite, e.g. to keep
your own Imager settings. The kit is a folder, `pi-frame-kit`, holding the installer, a settings file
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

### Automatic updates from GitHub

The frame can check this repository's releases itself (Maintenance →
Software update → From GitHub). The repository is private, so it needs a
read-only GitHub token, made once:

1. On GitHub: your picture → **Settings** → **Developer settings** →
   **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
2. Name it (e.g. "Pi Frame updates") and choose an expiry (up to a year).
3. Repository access: **Only select repositories** → `JimmySte/pi-frame`.
4. Permissions → Repository permissions → **Contents: Read-only**. Nothing else.
5. **Generate token**, copy it, and paste it into the frame.

The frame checks it works, keeps it privately (readable by the frame only,
never shown again, left out of diagnostics; backups include it, like the
other sign-ins) and then looks for a new release once a day. Choose what
happens when there is one:

- **Tell me here** (the default): the Maintenance tab says "Version … is
  available", with what's new and an **Install now** button.
- **Install automatically**: at a time you choose (03:30 by default), with
  the same safety as any update: a copy of the current version is kept, and
  if the new one doesn't start properly the old one is put back. A version
  that failed this way isn't tried again by itself.

Only full releases are offered: the `pi-frame-test` builds are ignored. The
download is checked against the checksum GitHub publishes for it. When the
token expires, the health summary says so: make a new one and paste it in.

### Updating by hand

**From the website** (Maintenance → Software update → From a file): upload a new
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

## Reset to a fresh frame

Maintenance → **Reset to a fresh frame** erases photos and videos, albums,
settings, synced-folder sign-ins, saved Wi-Fi networks, backups kept on the
frame, the GitHub token and the page password, then restarts. The frame then
starts like a new card: straight to its setup hotspot and the welcome wizard.
The installed version stays as it is.

- It needs the page password, and typing RESET.
- Tick **Also remove the SSH logins** to delete the logins too and switch
  SSH off, exactly like a new card (the wizard offers to make a login again).
  Left unticked, SSH keeps working.
- Want anything back afterwards? Download an Everything backup first, and
  restore it after the wizard.

## A new frame's first start

A frame that has never been set up (the ready-made image, or after a reset)
doesn't wait for Wi-Fi: it shows "Welcome to Pi Frame" and starts its setup
hotspot within seconds. With an Ethernet cable plugged in, it shows a QR code
for its page instead. After the wizard, it shows the page's QR code for a
minute on your home Wi-Fi, then the photos. From then on it starts as usual.
