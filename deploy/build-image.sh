#!/usr/bin/env bash
# Build the ready-made Pi Frame image: Raspberry Pi OS Lite (64-bit) with Pi
# Frame installed, booting into the first-run setup (setup hotspot + welcome
# wizard). No Wi-Fi settings, no login, SSH off.
#
#   sudo deploy/build-image.sh [output-folder]        (default: dist/)
#
# Runs on an x86 or Arm Linux machine (GitHub Actions does it: see
# .github/workflows/image.yml). Needs: curl, xz, sfdisk, e2fsprogs, zerofree, losetup,
# and qemu-user-static with binfmt support when not on Arm.
#
#   PF_BASE_URL   the Raspberry Pi OS image to start from (default: the latest Lite 64-bit)
#   PF_WORK       working folder (default: /tmp/pi-frame-image; needs ~6 GB)
#   PF_XZ_LEVEL   compression, 0-9 (default 3: much quicker than 6, a slightly bigger file)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(mkdir -p "${1:-$REPO/dist}" && cd "${1:-$REPO/dist}" && pwd)"
WORK="${PF_WORK:-/tmp/pi-frame-image}"
BASE_URL="${PF_BASE_URL:-https://downloads.raspberrypi.com/raspios_lite_arm64_latest}"
GROW_MB=2500
XZ_LEVEL="${PF_XZ_LEVEL:-3}"
VER="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO/photoframe/__init__.py")"
NAME="pi-frame-$VER"
IMG="$WORK/$NAME.img"
ROOT="$WORK/root"
LOOP=""

[ "$(id -u)" = 0 ] || { echo "Run with sudo." >&2; exit 1; }
step() { echo; echo "==> $*"; }

cleanup() {
  set +e
  for m in boot/firmware dev/pts dev proc sys run; do
    mountpoint -q "$ROOT/$m" && umount -l "$ROOT/$m"
  done
  mountpoint -q "$ROOT" && umount -l "$ROOT"
  [ -n "$LOOP" ] && losetup -d "$LOOP" 2>/dev/null
}
trap cleanup EXIT

mkdir -p "$WORK" "$ROOT"

step "Downloading Raspberry Pi OS Lite (64-bit)"
url="$(curl -fsSL -o /dev/null -w '%{url_effective}' "$BASE_URL")"
base="$(basename "$url")"
echo "    $url"
if [ ! -f "$WORK/$base" ]; then
  curl -fL --retry 3 -o "$WORK/$base.part" "$url"
  mv "$WORK/$base.part" "$WORK/$base"
fi
want="$(curl -fsSL "$url.sha256" | awk '{print $1}')"
have="$(sha256sum "$WORK/$base" | awk '{print $1}')"
[ -n "$want" ] && [ "$want" = "$have" ] || { echo "!! checksum doesn't match ($have, expected $want)"; exit 1; }
echo "    checksum OK"

step "Unpacking and making room for Pi Frame"
xz -dc "$WORK/$base" > "$IMG"
truncate -s "+${GROW_MB}M" "$IMG"
echo ", +" | sfdisk --no-reread --quiet -N 2 "$IMG"
LOOP="$(losetup -fP --show "$IMG")"
e2fsck -fy "${LOOP}p2" >/dev/null || true
resize2fs "${LOOP}p2" >/dev/null
mount "${LOOP}p2" "$ROOT"
mount "${LOOP}p1" "$ROOT/boot/firmware"
mount --bind /dev "$ROOT/dev"
mount --bind /dev/pts "$ROOT/dev/pts"
mount -t proc proc "$ROOT/proc"
mount -t sysfs sysfs "$ROOT/sys"
mount -t tmpfs tmpfs "$ROOT/run"

step "What the base image has"
cat "$ROOT/etc/os-release" | grep -E '^(PRETTY_NAME|VERSION_CODENAME)='
echo "    cmdline: $(cat "$ROOT/boot/firmware/cmdline.txt")"
echo "    users:   $(awk -F: '$3>=1000 && $3<60000 {print $1}' "$ROOT/etc/passwd" | tr '\n' ' ')"
for u in userconfig.service cloud-init.service cloud-init-main.service ssh.service regenerate_ssh_host_keys.service; do
  printf '    %-34s %s\n' "$u" "$(chroot "$ROOT" systemctl is-enabled "$u" 2>/dev/null || echo absent)"
done
echo "    boot files: $(ls "$ROOT/boot/firmware" | grep -vE '\.(dtb|dtbo)$' | grep -v overlays | tr '\n' ' ')"
for f in "$ROOT"/etc/cloud/cloud.cfg.d/*.cfg; do
  [ -f "$f" ] && { echo "    --- ${f#$ROOT}"; sed 's/^/      /' "$f"; }
done
for f in user-data meta-data network-config; do
  [ -f "$ROOT/boot/firmware/$f" ] && { echo "    --- bootfs/$f"; sed 's/^/      /' "$ROOT/boot/firmware/$f" | head -40; }
done

step "Installing Pi Frame $VER inside the image"
# name lookups inside the image while building; services mustn't start here
resolv_backup=""
if [ -e "$ROOT/etc/resolv.conf" ] || [ -L "$ROOT/etc/resolv.conf" ]; then
  resolv_backup="$ROOT/etc/resolv.conf.pi-frame-build"
  mv "$ROOT/etc/resolv.conf" "$resolv_backup"
fi
cp /etc/resolv.conf "$ROOT/etc/resolv.conf" 2>/dev/null || echo "nameserver 1.1.1.1" > "$ROOT/etc/resolv.conf"
printf '#!/bin/sh\nexit 101\n' > "$ROOT/usr/sbin/policy-rc.d"
chmod 755 "$ROOT/usr/sbin/policy-rc.d"
rm -rf "$ROOT/tmp/pi-frame-src"
mkdir -p "$ROOT/tmp/pi-frame-src"
git -C "$REPO" archive HEAD photoframe deploy requirements.txt README.md | tar -x -C "$ROOT/tmp/pi-frame-src" \
  || cp -r "$REPO/photoframe" "$REPO/deploy" "$REPO/requirements.txt" "$ROOT/tmp/pi-frame-src/"
chroot "$ROOT" /usr/bin/env DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8 \
  bash /tmp/pi-frame-src/deploy/install.sh --image

step "Tidying up"
chroot "$ROOT" apt-get clean
rm -rf "$ROOT/tmp/pi-frame-src" "$ROOT/var/lib/apt/lists/"* "$ROOT/var/cache/apt/"*.bin
rm -f "$ROOT/usr/sbin/policy-rc.d"
rm -f "$ROOT/etc/resolv.conf"
[ -n "$resolv_backup" ] && mv "$resolv_backup" "$ROOT/etc/resolv.conf"
rm -rf "$ROOT/var/log/journal/"* "$ROOT/root/.cache" "$ROOT/var/lib/pi-photoframe/__pycache__"
find "$ROOT/opt/pi-photoframe" -name __pycache__ -type d -prune -exec rm -rf {} +
: > "$ROOT/etc/machine-id"   # each frame makes its own at first boot
rm -f "$ROOT/var/lib/dbus/machine-id"
cat > "$ROOT/etc/pi-frame-image" <<EOF
Pi Frame $VER
built $(date -u +%Y-%m-%dT%H:%MZ) from $base
EOF

step "Checking the result"
fail=0
check() { if eval "$2"; then echo "    ok: $1"; else echo "    !! $1"; fail=1; fi; }
check "app installed"            "[ -f '$ROOT/opt/pi-photoframe/photoframe/__init__.py' ]"
check "frame starts at boot"     "chroot '$ROOT' systemctl is-enabled pi-photoframe.service >/dev/null"
check "first-run setup waiting"  "[ -f '$ROOT/var/lib/pi-photoframe/.first-run' ]"
check "Wi-Fi radio switched on"  "chroot '$ROOT' systemctl is-enabled pi-frame-wifi-on.service >/dev/null"
check "no first-boot user prompt" "! chroot '$ROOT' systemctl is-enabled userconfig.service >/dev/null 2>&1"
check "SSH off"                  "! chroot '$ROOT' systemctl is-enabled ssh.service >/dev/null 2>&1"
check "hostname piframe"         "[ \"\$(cat '$ROOT/etc/hostname')\" = piframe ]"
check "rclone installed"         "chroot '$ROOT' rclone version >/dev/null 2>&1"
check "mpv and ffmpeg installed" "chroot '$ROOT' sh -c 'command -v mpv && command -v ffmpeg' >/dev/null"
check "python modules import"    "chroot '$ROOT' sh -c 'cd /opt/pi-photoframe && python3 -c \"import photoframe.web, photoframe.kms, pykms\"' >/dev/null 2>&1"
check "no login on the image"    "[ -z \"\$(awk -F: '\$3>=1000 && \$3<60000' '$ROOT/etc/passwd')\" ]"
echo "    cmdline: $(cat "$ROOT/boot/firmware/cmdline.txt")"
echo "    root file system: $(df -h --output=used "$ROOT" | tail -1 | tr -d ' ') used"
[ "$fail" = 0 ] || { echo "!! the image isn't right (see above)"; exit 1; }

step "Shrinking the image"
umount "$ROOT/boot/firmware" "$ROOT/dev/pts" "$ROOT/dev" "$ROOT/proc" "$ROOT/sys" "$ROOT/run"
fstrim "$ROOT" 2>/dev/null || true
umount "$ROOT"
e2fsck -fy "${LOOP}p2" >/dev/null || true
resize2fs -M "${LOOP}p2" >/dev/null
blocks="$(dumpe2fs -h "${LOOP}p2" 2>/dev/null | awk -F: '/^Block count/ {gsub(/ /,"",$2); print $2}')"
bsize="$(dumpe2fs -h "${LOOP}p2" 2>/dev/null | awk -F: '/^Block size/ {gsub(/ /,"",$2); print $2}')"
# a little room, in case the first boot can't grow the partition straight away
spare=$((300 * 1024 * 1024))
fs_bytes=$((blocks * bsize + spare))
resize2fs "${LOOP}p2" "$((fs_bytes / 1024))K" >/dev/null
# blank the free space (deleted package downloads, etc.), so it compresses to
# nothing; fstrim does this on some machines but not all
if command -v zerofree >/dev/null; then
  zerofree "${LOOP}p2"
else
  echo "    (zerofree isn't installed: the image will be bigger than it needs to be)"
fi
start="$(partx -g -o START -n 2 "$IMG" | tr -d ' ')"  # in 512-byte sectors
losetup -d "$LOOP"; LOOP=""
sectors=$(((fs_bytes + 511) / 512))
echo ", $sectors" | sfdisk --no-reread --quiet -N 2 "$IMG"
truncate -s $(((start + sectors) * 512)) "$IMG"
echo "    $((($(stat -c %s "$IMG")) / 1048576)) MB"

step "Compressing (xz level $XZ_LEVEL)"
rm -f "$OUT/$NAME.img.xz"
xz -T0 "-$XZ_LEVEL" -c "$IMG" > "$OUT/$NAME.img.xz"
(cd "$OUT" && sha256sum "$NAME.img.xz" > "$NAME.img.xz.sha256")
rm -f "$IMG"
echo
echo "Done: $OUT/$NAME.img.xz ($(( $(stat -c %s "$OUT/$NAME.img.xz") / 1048576 )) MB)"
