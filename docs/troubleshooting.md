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
Tick "Use physical buttons" under Maintenance → Frame hardware → Physical buttons. The page shows
the last button pressed: if nothing appears, check the wiring against the
[diagram](screen-and-hardware.md#physical-buttons) (each button between its
pin and a ground pin).

**Monitor brightness doesn't change.**
Switch on DDC/CI in the monitor's own menu, then press **Check again** under
Screen → Monitor brightness.

**Videos stutter.**
Look in Recent activity for "decoded in hardware". Videos bigger than 1080p
are re-made at 1080p in the background, after which they play smoothly.

**Fades are slow (around 14 fps).**
The display hardware isn't being used. Recent activity says why; check
Maintenance → Frame hardware → Fades is on, and that `python3-kms++` is installed. Run
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

**I've forgotten the SSH password, or there's no SSH login.**
The admin password and the SSH password are separate. Under Maintenance →
**SSH login** you can set a new password for the login, create a login if the
Pi has none (e.g. it was skipped in the welcome wizard), and switch SSH on or
off. Unlock the admin options first.

**I've forgotten the admin password.**
With an SSH login, clear it from a computer, then set a new one at the top
of Maintenance:

```sh
ssh name@piframe.local
sudo systemctl stop pi-photoframe
sudo python3 -c "import json; p='/var/lib/pi-photoframe/config.json'; d=json.load(open(p)); d['config']['admin_password']=''; json.dump(d, open(p, 'w'), indent=2)"
sudo systemctl start pi-photoframe
```

Without an SSH login there's no way in from the page (that's the point of
it): write the image to the card again and run the welcome wizard. Restore
an Everything backup afterwards if you have one.

**The page says "This frame only answers to its own address".**
Open it as `http://piframe.local/` or by its IP address (Maintenance shows
it). Other names are turned away so websites can't trick your browser into
reaching the frame. A name of your own can be added: see
[Away from home](using.md#away-from-home).

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

<a id="security-updates"></a>**Security updates aren't installing, or I want to run them now.**
Maintenance shows the last result. To run them now and see what happens,
over SSH:

```sh
sudo systemctl start pi-frame-security
cat /var/lib/pi-photoframe/system/security-log.txt
```

"Couldn't reach the update servers" means the Pi had no internet at 03:40;
it tries again the next night (and soon after starting, if it was off).

**The screen says "Pi Frame needs attention".**
Open the frame's page → Maintenance: the yellow note at the top says why.
See [When the frame needs attention](using.md#when-the-frame-needs-attention).

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
