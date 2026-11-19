#!/usr/bin/env bash
# Install or update Pi Frame (pi-photoframe) on Raspberry Pi OS Lite (Bookworm or Trixie).
#
#   sudo ./deploy/install.sh                  first install
#   sudo ./deploy/install.sh --mode 1920x1200 also force the HDMI resolution (32-bit colour)
#   sudo ./deploy/install.sh --update         pull latest code and restart
#   sudo ./deploy/install.sh --no-boot-splash keep the normal boot text (undo: deploy/bootsplash.py remove)
set -euo pipefail

APP_DIR=/opt/pi-photoframe
DATA_DIR=/var/lib/pi-photoframe
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MODE=""
UPDATE=0
BOOTSPLASH=1

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --update) UPDATE=1; shift ;;
    --no-boot-splash) BOOTSPLASH=0; shift ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }

if [ "$UPDATE" -eq 1 ]; then
  if [ -d "$APP_DIR/.git" ]; then git -C "$APP_DIR" pull --ff-only; fi
  systemctl restart pi-photoframe
  echo "Updated and restarted."
  exit 0
fi

echo "==> Checking free space"
avail_mb=$(df -Pm / | awk 'NR==2 {print $4}')
if [ "$avail_mb" -lt 1500 ]; then
  echo "Only ${avail_mb} MB free on the SD card; at least 1.5 GB is needed." >&2
  exit 1
fi

echo "==> Installing packages"
# Refresh package lists; carry on if offline (fine when everything's installed already).
apt-get update -q || echo "Warning: couldn't refresh package lists; using what's cached"
# --no-install-recommends keeps ffmpeg/mpv from pulling in ~500 MB of desktop
# extras (GTK, X11 tools, yt-dlp, Perl web libraries) a frame doesn't need.
apt-get install -y -q --no-install-recommends python3 python3-flask python3-pil python3-numpy \
  python3-requests python3-waitress python3-qrcode fonts-dejavu-core \
  avahi-daemon dnsmasq-base polkitd ffmpeg mpv libgl1-mesa-dri iw libheif-examples python3-gpiozero python3-lgpio ddcutil python3-kms++

echo "==> Installing rclone (for synced folders)"
# Debian's rclone (1.60) is too old: OneDrive downloads fail with
# "unauthenticated" since Microsoft's 2024 change (fixed in rclone 1.67).
# Install the current release from rclone.org, checking its SHA-256.
python3 - <<'PY' || echo "Warning: couldn't install rclone from rclone.org; synced folders may not work (try again when online)"
import hashlib, io, os, platform, re, shutil, subprocess, sys, urllib.error, urllib.request, zipfile
MIN = (1, 67, 0)
def have():
    exe = shutil.which("rclone")
    if not exe:
        return None
    out = subprocess.run([exe, "version"], capture_output=True, text=True).stdout
    m = re.search(r"v(\d+)\.(\d+)\.(\d+)", out)
    return tuple(map(int, m.groups())) if m else None
cur = None if os.environ.get("PF_RCLONE_FORCE") else have()
if cur and cur >= MIN:
    print("    rclone %s is new enough" % ".".join(map(str, cur)))
    sys.exit(0)
arch = {"aarch64": "arm64", "arm64": "arm64", "armv7l": "arm-v7", "armv6l": "arm", "x86_64": "amd64"}.get(platform.machine())
if not arch:
    sys.exit("unsupported CPU: " + platform.machine())
BASE = os.environ.get("PF_RCLONE_BASE", "https://downloads.rclone.org")  # overridable for testing
DEST = os.environ.get("PF_RCLONE_DEST", "/usr/local/bin")
UA = "Pi-Frame-installer (rclone download)"


def get(url):
    # Download with curl when it's there (rclone.org refuses Python's default
    # identity with HTTP 403), else urllib with a proper User-Agent.
    if shutil.which("curl"):
        r = subprocess.run(["curl", "-fsSL", "--retry", "3", "--max-time", "300", "-A", UA, url],
                           capture_output=True)
        if r.returncode == 0:
            return r.stdout
        sys.stderr.write("    curl couldn't download %s: %s\n" % (url, r.stderr.decode(errors="replace").strip()))
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60).read()
    except urllib.error.HTTPError as e:
        sys.exit("the server refused %s (HTTP %s)" % (url, e.code))
    except Exception as e:
        sys.exit("couldn't download %s (%s)" % (url, e))
ver = get(BASE + "/version.txt").decode().split()[-1]  # e.g. v1.71.1
name = "rclone-%s-linux-%s.zip" % (ver, arch)
blob = get("%s/%s/%s" % (BASE, ver, name))
sums = get("%s/%s/SHA256SUMS" % (BASE, ver)).decode()
want = next((l.split()[0] for l in sums.splitlines() if l.strip().endswith(name)), None)
if not want or hashlib.sha256(blob).hexdigest() != want:
    sys.exit("checksum mismatch for " + name)
with zipfile.ZipFile(io.BytesIO(blob)) as z:
    member = next(n for n in z.namelist() if n.endswith("/rclone"))
    data = z.read(member)
tmp = os.path.join(DEST, "rclone.new")
with open(tmp, "wb") as f:
    f.write(data)
os.chmod(tmp, 0o755)
os.replace(tmp, os.path.join(DEST, "rclone"))
print("    installed rclone %s to %s" % (ver, DEST))
PY

echo "==> Copying app to $APP_DIR"
if [ "$SRC_DIR" != "$APP_DIR" ]; then
  mkdir -p "$APP_DIR"
  cp -r "$SRC_DIR"/. "$APP_DIR"/
fi

echo "==> Creating service user and data directory"
id photoframe >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin photoframe
for g in video render audio systemd-journal gpio tty i2c; do
  getent group "$g" >/dev/null && usermod -aG "$g" photoframe
done
mkdir -p "$DATA_DIR"
chown -R photoframe:photoframe "$DATA_DIR"

echo "==> Installing systemd service and mDNS advertisement"
install -m 644 "$APP_DIR/deploy/pi-photoframe.service" /etc/systemd/system/pi-photoframe.service
install -m 644 "$APP_DIR/deploy/photoframe.avahi.service" /etc/avahi/services/photoframe.service

echo "==> Allowing the frame to run its Wi-Fi setup hotspot"
install -d /etc/polkit-1/rules.d /etc/NetworkManager/dnsmasq-shared.d
install -m 644 "$APP_DIR/deploy/50-pi-photoframe.rules" /etc/polkit-1/rules.d/50-pi-photoframe.rules
install -m 644 "$APP_DIR/deploy/captive-portal.conf" /etc/NetworkManager/dnsmasq-shared.d/pi-photoframe.conf
install -d /etc/NetworkManager/conf.d
install -m 644 "$APP_DIR/deploy/wifi-powersave-off.conf" /etc/NetworkManager/conf.d/pi-photoframe-wifi.conf

echo "==> Keeping the text console off the screen"
# The login prompt on tty1 would otherwise draw over the photos.
systemctl disable --now getty@tty1.service 2>/dev/null || true

CMDLINE=/boot/firmware/cmdline.txt
[ -f "$CMDLINE" ] || CMDLINE=/boot/cmdline.txt
if [ -f "$CMDLINE" ]; then
  cp -n "$CMDLINE" "$CMDLINE.pre-photoframe" || true
  line="$(tr -d '\n' < "$CMDLINE")"
  # Only the two options the frame needs: stop the kernel blanking the screen
  # after 10 minutes, and hide the text cursor. Everything else in the line
  # (including console=tty1) is left untouched, so boot messages still show.
  for opt in consoleblank=0 vt.global_cursor_default=0; do
    case " $line " in *" $opt "*) ;; *) line="$line $opt" ;; esac
  done
  if [ -n "$MODE" ]; then
    line="$(echo "$line" | sed -E 's/ ?video=HDMI-A-1:[^ ]*//')"
    line="$line video=HDMI-A-1:${MODE}-32@60"  # -32: full colour
  fi
  echo "$line" > "$CMDLINE"
  echo "    updated $CMDLINE (backup: $CMDLINE.pre-photoframe)"
fi

echo "==> Monitor control over HDMI (DDC/CI brightness and standby)"
echo i2c-dev > /etc/modules-load.d/pi-frame.conf
modprobe i2c-dev || true

echo "==> Installing the web updater (Maintenance tab → Software update)"
install -m 755 "$APP_DIR/deploy/update.sh" /usr/local/sbin/pi-photoframe-update
install -m 644 "$APP_DIR/deploy/pi-photoframe-update.path" /etc/systemd/system/pi-photoframe-update.path
install -m 644 "$APP_DIR/deploy/pi-photoframe-update.service" /etc/systemd/system/pi-photoframe-update.service
install -d -o photoframe -g photoframe -m 755 "$DATA_DIR/update"

echo "==> Installing the system helper (screen resolution from the Maintenance tab)"
install -m 644 "$APP_DIR/deploy/pi-frame-system.path" /etc/systemd/system/pi-frame-system.path
install -m 644 "$APP_DIR/deploy/pi-frame-system.service" /etc/systemd/system/pi-frame-system.service
install -m 644 "$APP_DIR/deploy/pi-frame-splash-refresh.service" /etc/systemd/system/pi-frame-splash-refresh.service
install -d -o photoframe -g photoframe -m 755 "$DATA_DIR/system"

echo "==> Keeping logs across reboots (for troubleshooting)"
# Raspberry Pi OS keeps the system log in memory only, so a failed boot leaves
# no record. Keep it on the card, capped small to spare the SD card.
install -d /etc/systemd/journald.conf.d /var/log/journal
cat > /etc/systemd/journald.conf.d/60-pi-frame.conf <<'JCONF'
# Pi Frame: keep logs across reboots (small cap to spare the SD card)
[Journal]
Storage=persistent
SystemMaxUse=48M
JCONF
systemd-tmpfiles --create --prefix /var/log/journal 2>/dev/null || true
systemctl restart systemd-journald

if [ "$BOOTSPLASH" = 1 ] && [ -f /boot/firmware/cmdline.txt -o -f /boot/cmdline.txt ]; then
  echo "==> Boot screen: Pi Frame logo instead of the rainbow and boot text"
  apt-get install -y -q --no-install-recommends rpi-splash-screen-support initramfs-tools \
    || echo "    Warning: rpi-splash-screen-support isn't available; boot text will be hidden but no logo shown"
  python3 "$APP_DIR/deploy/bootsplash.py" install || echo "    Warning: couldn't set up the boot screen (the frame itself is fine)"
fi

# the frame (tty group) keeps the console in graphics mode; restore the standard permissions now
# (the boot step does this at every start too)
if [ -c /dev/tty1 ]; then chown root:tty /dev/tty1 && chmod 620 /dev/tty1 || true; fi

systemctl daemon-reload
systemctl enable pi-photoframe.service
systemctl enable --now pi-photoframe-update.path
systemctl enable --now pi-frame-system.path
systemctl enable pi-frame-splash-refresh.service
systemctl restart avahi-daemon
systemctl restart pi-photoframe.service

echo
echo "Done. Open http://$(hostname).local/ from your phone."
echo "Reboot once so the console changes take effect: sudo reboot"
