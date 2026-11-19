PI FRAME INSTALLER KIT
======================

1. Flash the card with Raspberry Pi Imager
   - Device: Raspberry Pi 4.  OS: Raspberry Pi OS Lite (64-bit)
   - Settings: hostname piframe, a username and a password you'll remember,
     your Wi-Fi, and SSH switched on (password login).

2. Copy this kit onto the card
   - Take the card out and put it back in, so "bootfs" appears.
   - Copy the whole pi-frame-kit folder onto bootfs.
   - Optional: set RESOLUTION in kit.conf (e.g. "2560x1440").
   - Optional: put your frame-backup.tgz inside the pi-frame-kit folder on
     bootfs, to bring back your photos, videos and settings.
   - Eject the card.

3. Boot and run one command
   - Put the card in the Pi, with the monitor connected, and power on.
     Wait 2-3 minutes (the first boot sets itself up and may restart).
   - From Terminal (Mac) or PowerShell (Windows):
         ssh yourname@piframe.local
         sudo bash /boot/firmware/pi-frame-kit/install-frame.sh
   - It installs Pi Frame, sets the resolution (kit.conf), restores the
     backup if there is one, then restarts. About 10-15 minutes.

If something goes wrong, the full log is in /var/log/pi-frame-kit.log.
If ssh complains the host key changed:  ssh-keygen -R piframe.local
