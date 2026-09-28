#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${ENV_FILE:-/opt/abraham/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

DB_PATH="${ABRAHAM_DB_PATH:?ABRAHAM_DB_PATH is required}"
BACKUP_ROOT="${ABRAHAM_BACKUP_PATH:-/opt/abraham/backups}"
EXTERNAL="${ABRAHAM_EXTERNAL_BACKUP_PATH:-}"
STAMP="$(date '+%Y%m%d_%H%M%S')"
DEST="$BACKUP_ROOT/db/$STAMP"
mkdir -p "$DEST"

# SQLite-safe online backup.
sqlite3 "$DB_PATH" ".backup '$DEST/abraham.db'"

if [[ -n "$EXTERNAL" && -d "$EXTERNAL" ]]; then
  mkdir -p "$EXTERNAL/abraham-db"
  cp -f "$DEST/abraham.db" "$EXTERNAL/abraham-db/abraham_${STAMP}.db"
fi

# Keep the latest 14 DB snapshots locally.
find "$BACKUP_ROOT/db" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' 2>/dev/null \
  | sort -nr | tail -n +15 | cut -d' ' -f2- | xargs -r rm -rf

echo "Backup complete: $DEST/abraham.db"
