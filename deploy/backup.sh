#!/usr/bin/env bash
# Daily backup of the JS Entertainment SQLite database.
# Uses SQLite's online backup API (WAL-safe, consistent even while the app is
# running), writes a timestamped copy to /opt/jsent-desk/backups, and keeps only
# the 7 most recent. Run by the jsent-desk-backup.timer systemd unit.
set -euo pipefail

APP=/opt/jsent-desk
DIR="$APP/backups"
mkdir -p "$DIR"
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$DIR/jsent-$STAMP.db"

"$APP/.venv/bin/python3" - "$OUT" <<'PY'
import sqlite3, sys
src = sqlite3.connect("/opt/jsent-desk/jsent.db")
dst = sqlite3.connect(sys.argv[1])
src.backup(dst)           # consistent snapshot, handles WAL
dst.close(); src.close()
PY

# Keep the 7 most recent backups; delete the rest.
ls -1t "$DIR"/jsent-*.db | tail -n +8 | xargs -r rm --

echo "backup complete: $OUT ($(du -h "$OUT" | cut -f1))"
