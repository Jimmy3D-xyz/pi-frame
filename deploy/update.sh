#!/usr/bin/env bash
# Pi Frame web updater. Runs as root, started by pi-photoframe-update.path
# when the frame (which runs unprivileged) drops a checked package at
#   /var/lib/pi-photoframe/update/pending.zip
# It backs up the current version, runs the new package's install.sh, checks
# the frame comes back healthy, and rolls back if anything goes wrong.
# Progress is written to update/status.json for the web page to show.
set -uo pipefail

APP_DIR="${PF_APP_DIR:-/opt/pi-photoframe}"
DATA_DIR="${PF_DATA_DIR:-/var/lib/pi-photoframe}"
HEALTH_URL="${PF_HEALTH_URL:-http://127.0.0.1/api/status}"
HEALTH_WAIT="${PF_HEALTH_WAIT:-90}"
RESTART_CMD="${PF_RESTART_CMD:-systemctl restart pi-photoframe}"
UPD="$DATA_DIR/update"
ZIP="$UPD/pending.zip"
WORK="$UPD/work"
LOG="$UPD/log.txt"
STATUS="$UPD/status.json"
BACKUP="${APP_DIR}.previous"

[ -f "$ZIP" ] || exit 0

status() {  # state, message
  python3 - "$STATUS" "$1" "$2" <<'PY'
import json, sys, time, os
path, state, msg = sys.argv[1:4]
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump({"state": state, "message": msg, "t": time.time()}, f)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PY
}

fail() {
  echo "!! $1" >>"$LOG"
  status "$2" "$1"
  rm -rf "$WORK"
  exit 1
}

mkdir -p "$UPD"
: >"$LOG"
chmod 644 "$LOG"
exec >>"$LOG" 2>&1
echo "== update started $(date)"
rm -rf "$WORK"
mkdir -p "$WORK"
mv "$ZIP" "$WORK/package.zip"

status "unpacking" "Unpacking the update"
# Safe extraction: regular files only, nothing outside the target folder.
python3 - "$WORK/package.zip" "$WORK/src" <<'PY' || fail "The package couldn't be unpacked safely" "failed"
import sys, zipfile, os, stat
from pathlib import Path
zpath, dest = sys.argv[1], Path(sys.argv[2]).resolve()
with zipfile.ZipFile(zpath) as z:
    for info in z.infolist():
        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            sys.exit(f"refusing symlink {info.filename}")
        target = (dest / info.filename).resolve()
        if dest not in target.parents and target != dest:
            sys.exit(f"refusing path outside package: {info.filename}")
    z.extractall(dest)
# keep the executable bit on shell scripts
for p in dest.rglob("*.sh"):
    p.chmod(0o755)
PY

NEW="$WORK/src/pi-photoframe"
[ -f "$NEW/deploy/install.sh" ] && [ -f "$NEW/photoframe/__init__.py" ] \
  || fail "That isn't a Pi Frame update (install.sh or the app is missing)" "failed"
NEW_VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$NEW/photoframe/__init__.py")
OLD_VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$APP_DIR/photoframe/__init__.py" 2>/dev/null || true)
echo "updating ${OLD_VERSION:-?} -> ${NEW_VERSION:-?}"

status "backing-up" "Keeping a copy of version ${OLD_VERSION:-current} in case it's needed"
rm -rf "$BACKUP"
if [ -d "$APP_DIR" ]; then
  cp -a "$APP_DIR" "$BACKUP" || fail "Couldn't back up the current version (is the SD card full?)" "failed"
fi

rollback() {
  echo "!! rolling back to ${OLD_VERSION:-previous}: $1"
  status "rolling-back" "Putting version ${OLD_VERSION:-previous} back: $1"
  if [ -d "$BACKUP" ]; then
    rm -rf "$APP_DIR"
    cp -a "$BACKUP" "$APP_DIR"
  fi
  $RESTART_CMD || true
  status "rolled-back" "The update didn't work, so version ${OLD_VERSION:-previous} was put back. $1"
  rm -rf "$WORK"
  exit 1
}

status "installing" "Installing version ${NEW_VERSION} (this takes a minute or two)"
# A clean copy: remove the old app so deleted files don't linger.
rm -rf "$APP_DIR"
if ! bash "$NEW/deploy/install.sh"; then
  rollback "The installer reported an error (details in the log below)."
fi

status "checking" "Checking the frame starts up properly"
ok=0
for _ in $(seq 1 "$HEALTH_WAIT"); do
  if python3 - "$HEALTH_URL" "$NEW_VERSION" <<'PY'
import json, sys, urllib.request
url, want = sys.argv[1:3]
try:
    with urllib.request.urlopen(url, timeout=3) as r:
        sys.exit(0 if json.load(r).get("version") == want else 1)
except Exception as e:
    # 401 means it's running but password-protected: that's healthy too
    sys.exit(0 if getattr(e, "code", None) == 401 else 1)
PY
  then ok=1; break; fi
  sleep 1
done
[ "$ok" = 1 ] || rollback "Version ${NEW_VERSION} didn't start within ${HEALTH_WAIT} seconds."

status "done" "Updated to version ${NEW_VERSION}"
echo "== update finished $(date)"
rm -rf "$WORK"
exit 0
