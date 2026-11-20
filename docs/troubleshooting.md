# Troubleshooting

[← Back to the README](../README.md)

- [The Maintenance tab](#the-maintenance-tab)
- [Logs over SSH](#logs-over-ssh)
- [Problems and fixes](#problems-and-fixes)
- [Checking an SD card](#checking-an-sd-card)

## The Maintenance tab

Start here. It shows a green, amber or red summary, then:

- **Wi-Fi**: band, router radio, signal, speed, link-check results and every automatic re-join
- **Power**: CPU temperature, under-voltage and throttling since the last start
- **SD card**: free space, whether it has gone read-only, and any card errors the kernel has logged
- **Screen and slideshow**: resolution, colour depth, last fade's frame rate
- **Photos and videos**: conversions in progress, unfinished uploads
- **Recent activity**: the frame's own log, with an "only problems" filter

Actions: check the network now, restart the frame software, reboot the Pi,
and **download diagnostics**, a text file with the status and recent logs
(passwords, tokens and keys removed) that's useful to share when asking for help.

## Logs over SSH

```sh
journalctl -u pi-photoframe -f                    # the frame, live
journalctl -u pi-photoframe -b                    # since the last start
journalctl -u pi-photoframe | grep "link check"   # Wi-Fi link checks
journalctl -u pi-photoframe | grep Wi-Fi          # how long Wi-Fi took to connect
sudo systemctl restart pi-photoframe              # restart the frame
cat /var/lib/pi-photoframe/update/log.txt         # the last software update
cat /var/log/pi-frame-kit.log                     # the installer kit
```

## Problems and fixes

**The website doesn't load, but the Pi answers ping.**
Before 0.23.0, the frame stopped if no monitor was connected. It now runs
without one; update, or connect the monitor and restart.

**"Folder sync needs rclone on the frame", or OneDrive says sign-in has expired straight after signing in.**
rclone is missing or too old. Re-run the installer, or install it by hand:
see [Synced folders → rclone](synced-folders.md#rclone).

**A NAS can't be reached.**
Try its IP address instead of its name, and check the username has access
to the shared folder.

**Physical buttons do nothing.**
Tick "Use physical buttons" under Display → Physical buttons. The page shows
the last button pressed: if nothing appears, check the wiring against the
[diagram](screen-and-hardware.md#physical-buttons) (each button between its
pin and a ground pin).

**Monitor brightness doesn't change.**
Switch on DDC/CI in the monitor's own menu, then press **Check again** under
Display → Monitor brightness.

**Videos stutter.**
Look in Recent activity for "decoded in hardware". Videos bigger than 1080p
are re-made at 1080p in the background, after which they play smoothly.

**Fades are slow (around 14 fps).**
The display hardware isn't being used. Recent activity says why; check
Display → Fades is on, and that `python3-kms++` is installed. Run
`deploy/kms_fade_test.py` to test the Pi step by step.

**Text appears on the screen during boot or over the photos.**
Re-run the installer, which puts the boot settings back. See
[Boot screen](screen-and-hardware.md#boot-screen).

**The screen stays blank after changing resolution.**
Change it back from Maintenance → Screen resolution on your phone. Or put
the card in a computer and copy `cmdline.txt.pre-resolution` over
`cmdline.txt` on the `bootfs` drive.

**The Pi stops at the logo, or crashes at boot with `Caught <ILL>` or other odd errors.**
On our first frame this was a failing no-name SD card: it had silently
damaged system files. Boot may still work after leaving it a while, which
makes it look random. Check the card (below). If files are damaged, back up
and move to a new, branded card:
[Moving to a new SD card](installing.md#moving-to-a-new-sd-card).

**The SD card shows as read-only.**
A card that switches itself to read-only is usually failing. Back up while
you still can and replace it.

**I've forgotten the SSH password.**
The page password and the SSH password are separate. If you can still sign
in to the website, a one-off update package can set a temporary SSH
password (ask for one, and delete it afterwards). Otherwise, put the card
in a computer, or reflash it with the kit and your backup.

**An automatic backup to the NAS fails with "didn't let the frame save there".**
The NAS account can read but not write. Give it write permission on the
backup folder (or choose a different folder or place), then press Back up now.

**Google Drive isn't offered for backups ("connected read-only").**
Google Drive connections made before version 0.24 can only read. Disconnect
it under Photo source → Synced folders and sign in again; synced albums are kept.

**"Couldn't check for updates: GitHub no longer accepts the token".**
The token has expired (or was deleted). Make a new one, as in
[Automatic updates](installing.md#automatic-updates-from-github), and paste it
under Maintenance → Software update → From GitHub.

**SSH says the host key has changed.**
Expected after reflashing a card: `ssh-keygen -R piframe.local`.

## Checking an SD card

On the Pi:

```sh
sudo dpkg --verify                  # system files that don't match what was installed
dmesg | grep -i mmc                 # card errors from the kernel
vcgencmd get_throttled              # power problems: 0x0 means none
findmnt -no OPTIONS /               # should start with "rw"
```

`dpkg --verify` takes a few minutes and should print nothing (or only
`/usr/lib/modprobe.d/g_ether.conf`, which Raspberry Pi OS changes on purpose).
Any other line starting with `??5` means a file's contents have changed:
the card is damaging data.

On a computer, H2testw (Windows) or F3 (Mac/Linux) checks a card's real
capacity and whether it keeps what's written to it. Fake cards claim more
space than they have.
