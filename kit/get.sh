#!/bin/bash
# Pi Frame one-line installer, for a freshly written Raspberry Pi OS Lite (64-bit) card.
# Log in over SSH and run:
#
#     curl -fsSL https://github.com/Jimmy3D-xyz/pi-frame/releases/latest/download/install.sh | sudo bash
#
# Options (after "sudo bash -s --"):
#     --mode 2560x1440    set the screen resolution (otherwise the screen's own default)
#     --no-reboot         don't restart at the end
#
# It downloads the newest release's installer kit from GitHub, checks it
# against the release's SHA-256, and runs the kit's installer (the same one
# as copying the kit onto the card by hand). Everything is logged to
# /var/log/pi-frame-kit.log.
#
# The whole script is one function, called on the last line, so a download
# that stops half way doesn't run half a script.
set -euo pipefail

main() {
  local repo="${PF_REPO:-Jimmy3D-xyz/pi-frame}"
  local base="${PF_RELEASE_URL:-https://github.com/$repo/releases/latest/download}"
  local work="${PF_GET_WORK:-/var/tmp/pi-frame-get}"
  local mode="" reboot=1

  while [ $# -gt 0 ]; do
    case "$1" in
      --mode) mode="${2:-}"; shift 2 ;;
      --mode=*) mode="${1#--mode=}"; shift ;;
      --no-reboot) reboot=0; shift ;;
      -h|--help) sed -n '2,12p' "$0" 2>/dev/null || true; return 0 ;;
      *) echo "Unknown option: $1 (use --mode 2560x1440 or --no-reboot)" >&2; return 1 ;;
    esac
  done
  if [ -n "$mode" ] && ! [[ "$mode" =~ ^[0-9]{3,4}x[0-9]{3,4}$ ]]; then
    echo "--mode should look like 2560x1440 (got '$mode')" >&2; return 1
  fi

  if [ "$(id -u)" != 0 ]; then
    echo "Please run it with sudo:" >&2
    echo "  curl -fsSL $base/install.sh | sudo bash" >&2
    return 1
  fi
  if [ -z "${PF_GET_ANY_ARCH:-}" ] && [ "$(dpkg --print-architecture 2>/dev/null)" != arm64 ]; then
    echo "Pi Frame needs Raspberry Pi OS Lite (64-bit) on a Raspberry Pi 4." >&2
    echo "This system is $(dpkg --print-architecture 2>/dev/null || uname -m); write the card again with the 64-bit Lite version." >&2
    return 1
  fi

  echo "==> Downloading the newest Pi Frame from GitHub"
  rm -rf "$work"
  mkdir -p "$work"
  local tries
  for f in pi-frame-kit.zip pi-frame-kit.zip.sha256; do
    for tries in 1 2 3 4 5 6; do
      if curl -fsSL --retry 2 -o "$work/$f" "$base/$f"; then break; fi
      if [ "$tries" = 6 ]; then
        echo "!! Couldn't download $f. Is the Pi on the internet? (Check the Wi-Fi you chose in Raspberry Pi Imager.)" >&2
        return 1
      fi
      sleep 10
    done
  done

  echo "==> Checking the download"
  local want got
  want="$(awk '{print $1; exit}' "$work/pi-frame-kit.zip.sha256")"
  got="$(sha256sum "$work/pi-frame-kit.zip" | awk '{print $1}')"
  if [ -z "$want" ] || [ "$want" != "$got" ]; then
    echo "!! The download doesn't match the release's checksum, so it wasn't used. Try again in a minute." >&2
    return 1
  fi
  echo "    OK"

  # unpack with Python (always there; unzip may not be), refusing paths outside the folder
  python3 - "$work/pi-frame-kit.zip" "$work" <<'PY'
import sys, zipfile
from pathlib import Path
zpath, dest = sys.argv[1], Path(sys.argv[2]).resolve()
with zipfile.ZipFile(zpath) as z:
    for info in z.infolist():
        target = (dest / info.filename).resolve()
        if dest != target and dest not in target.parents:
            sys.exit(f"unsafe path in the kit: {info.filename}")
    z.extractall(dest)
PY
  local kit="$work/pi-frame-kit"
  [ -f "$kit/install-frame.sh" ] || { echo "!! The download isn't a Pi Frame kit" >&2; return 1; }
  if [ -n "$mode" ]; then
    sed -i "s/^RESOLUTION=.*/RESOLUTION=\"$mode\"/" "$kit/kit.conf"
  fi

  if [ "$reboot" = 0 ]; then export PF_KIT_NO_REBOOT=1; fi
  bash "$kit/install-frame.sh"
}

main "$@"
