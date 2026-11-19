#!/usr/bin/env bash
# Build pi-frame-kit.zip from this repository: the kit files plus
# pi-photoframe.zip (the app package, also what the website's updater takes).
#   kit/build-kit.sh [output-folder]      (default: dist/)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$(mkdir -p "${1:-$REPO/dist}" && cd "${1:-$REPO/dist}" && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
VER="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO/photoframe/__init__.py")"

mkdir -p "$TMP/pi-photoframe"
git -C "$REPO" archive HEAD photoframe deploy requirements.txt README.md | tar -x -C "$TMP/pi-photoframe"
(cd "$TMP" && zip -qr "$OUT/pi-photoframe.zip" pi-photoframe -x '*/__pycache__/*')

mkdir -p "$TMP/pi-frame-kit"
cp "$REPO/kit/install-frame.sh" "$REPO/kit/kit.conf" "$REPO/kit/README.txt" "$OUT/pi-photoframe.zip" "$TMP/pi-frame-kit/"
rm -f "$OUT/pi-frame-kit.zip"
(cd "$TMP" && zip -qr "$OUT/pi-frame-kit.zip" pi-frame-kit)
echo "Pi Frame $VER: $OUT/pi-photoframe.zip and $OUT/pi-frame-kit.zip"
