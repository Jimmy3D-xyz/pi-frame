#!/bin/bash
# Pi Frame installer kit: sets up a freshly flashed Raspberry Pi OS Lite card.
#
# Copy the pi-frame-kit folder onto the card's "bootfs" drive, boot the Pi,
# log in over SSH, and run:
#
#     sudo bash /boot/firmware/pi-frame-kit/install-frame.sh
#
# It installs Pi Frame, sets the screen resolution from kit.conf, restores
# frame-backup.tgz if you put one in the folder, then restarts the Pi.
# Everything it does is logged to /var/log/pi-frame-kit.log.
set -euo pipefail

KIT="$(cd "$(dirname "$0")" && pwd)"
# (overridable for testing)
WORK="${PF_KIT_WORK:-/var/tmp/pi-frame-kit}"
DATA_PARENT="${PF_KIT_DATA_PARENT:-/var/lib}"
LOG="${PF_KIT_LOG:-/var/log/pi-frame-kit.log}"
NET_URL="${PF_KIT_NET_URL:-http://deb.debian.org/}"
NO_REBOOT="${PF_KIT_NO_REBOOT:-0}"

if [ "$(id -u)" != 0 ]; then
  echo "Please run it with sudo:  sudo bash $0" >&2
  exit 1
fi
exec > >(tee -a "$LOG") 2>&1
echo "=== Pi Frame kit started $(date)"

# ---- settings
RESOLUTION=""
[ -f "$KIT/kit.conf" ] && . "$KIT/kit.conf"
if [ -n "$RESOLUTION" ] && ! [[ "$RESOLUTION" =~ ^[0-9]{3,4}x[0-9]{3,4}$ ]]; then
  echo "!! kit.conf: RESOLUTION should look like 2560x1440 (got '$RESOLUTION')"; exit 1
fi
[ -f "$KIT/pi-photoframe.zip" ] || { echo "!! pi-photoframe.zip is missing from $KIT"; exit 1; }

# ---- internet (the installer downloads packages)
echo "==> Checking the internet connection"
ok=0
for _ in $(seq 1 60); do
  if python3 -c "import urllib.request,sys; urllib.request.urlopen(sys.argv[1], timeout=5)" "$NET_URL" 2>/dev/null; then
    ok=1; break
  fi
  sleep 2
done
[ "$ok" = 1 ] || { echo "!! No internet connection. Check the Wi-Fi settings you chose in Raspberry Pi Imager, then run this again."; exit 1; }
echo "    online"

# ---- unpack (python3 is always there; unzip may not be)
echo "==> Unpacking Pi Frame"
rm -rf "$WORK"
mkdir -p "$WORK"
python3 - "$KIT/pi-photoframe.zip" "$WORK" <<'PY'
import sys, zipfile, os
from pathlib import Path
zpath, dest = sys.argv[1], Path(sys.argv[2]).resolve()
with zipfile.ZipFile(zpath) as z:
    for info in z.infolist():
        target = (dest / info.filename).resolve()
        if dest != target and dest not in target.parents:
            sys.exit(f"unsafe path in the zip: {info.filename}")
    z.extractall(dest)
for p in dest.rglob("*.sh"):
    p.chmod(0o755)
PY
SRC="$WORK/pi-photoframe"
[ -f "$SRC/deploy/install.sh" ] || { echo "!! pi-photoframe.zip doesn't look like Pi Frame"; exit 1; }
echo "    version $(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/photoframe/__init__.py")"

# ---- install
echo "==> Installing Pi Frame (this takes several minutes)"
args=()
[ -n "$RESOLUTION" ] && args+=(--mode "$RESOLUTION")
bash "$SRC/deploy/install.sh" "${args[@]}"

# ---- restore the backup
if [ -f "$KIT/frame-backup.tgz" ]; then
  echo "==> Restoring photos and settings from frame-backup.tgz"
  systemctl stop pi-photoframe 2>/dev/null || true
  tar xzf "$KIT/frame-backup.tgz" -C "$DATA_PARENT"
  chown -R photoframe:photoframe "$DATA_PARENT/pi-photoframe" 2>/dev/null || true
  # the boot logo is remade for this screen by the installer and at start-up
  echo "    restored"
else
  echo "==> No frame-backup.tgz in the kit: starting with an empty frame"
fi

touch "$KIT/.installed" 2>/dev/null || true
echo "=== Pi Frame kit finished $(date)"
if [ "$NO_REBOOT" = 1 ]; then
  echo "(not restarting: PF_KIT_NO_REBOOT=1)"
  exit 0
fi
echo "Restarting in 10 seconds. Pi Frame will be up in about a minute."
sleep 10
systemctl reboot
