#!/usr/bin/env bash
# Back up the Compose PostgreSQL database to a compressed, timestamped dump and prune old backups.
#
#   scripts/backup.sh                       # -> backups/devopsagents-<UTC timestamp>.dump
#   BACKUP_DIR=/mnt/usb KEEP=30 scripts/backup.sh
#
# Settings (environment): BACKUP_DIR (default: backups), KEEP (default: 14 newest kept),
# COMPOSE (default: "docker compose"), DB_USER / DB_NAME (default: devopsagents).
# A dump is written to a *.partial file first and only renamed when pg_dump succeeded and the file is
# non-empty, so a failed run can never leave something that looks like a good backup.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-backups}"
KEEP="${KEEP:-14}"
COMPOSE="${COMPOSE:-docker compose}"
DB_USER="${DB_USER:-devopsagents}"
DB_NAME="${DB_NAME:-devopsagents}"

case "$KEEP" in '' | *[!0-9]*) echo "KEEP must be a positive integer" >&2; exit 2 ;; esac
[ "$KEEP" -ge 1 ] || { echo "KEEP must be at least 1" >&2; exit 2; }

mkdir -p "$BACKUP_DIR"
umask 077 # dumps contain your data: owner-only
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$BACKUP_DIR/devopsagents-$stamp.dump"
tmp="$out.partial"
trap 'rm -f "$tmp"' EXIT

# shellcheck disable=SC2086  # COMPOSE is intentionally word-split ("docker compose")
$COMPOSE exec -T postgres pg_dump -U "$DB_USER" -d "$DB_NAME" --format=custom >"$tmp"
[ -s "$tmp" ] || { echo "pg_dump produced an empty file; nothing was saved" >&2; exit 1; }
mv "$tmp" "$out"

# Retention: keep the $KEEP newest dumps. File names carry a UTC timestamp, so glob (alphabetical) order is
# chronological order, and unlike modification times it survives copying the files around.
shopt -s nullglob
files=("$BACKUP_DIR"/devopsagents-*.dump)
excess=$((${#files[@]} - KEEP))
if [ "$excess" -gt 0 ]; then
  for f in "${files[@]:0:excess}"; do
    rm -f -- "$f"
  done
fi

echo "$out"
