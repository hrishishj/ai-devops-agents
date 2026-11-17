#!/usr/bin/env bash
# Restore the Compose PostgreSQL database from a dump made by scripts/backup.sh.
#
#   scripts/restore.sh backups/devopsagents-20261117T120000Z.dump
#   FORCE=1 scripts/restore.sh <file>        # skip the confirmation prompt
#
# THIS REPLACES THE CURRENT DATABASE CONTENTS. The api and worker are stopped first so nothing writes
# during the restore, and started again afterwards.
set -euo pipefail

COMPOSE="${COMPOSE:-docker compose}"
DB_USER="${DB_USER:-devopsagents}"
DB_NAME="${DB_NAME:-devopsagents}"
file="${1:-}"

[ -n "$file" ] || { echo "usage: $0 <backup-file.dump>" >&2; exit 2; }
[ -f "$file" ] || { echo "no such file: $file" >&2; exit 2; }
[ -s "$file" ] || { echo "backup file is empty: $file" >&2; exit 2; }

if [ "${FORCE:-}" != "1" ]; then
  printf 'This will REPLACE the current database with %s. Type "restore" to continue: ' "$file"
  read -r answer
  answer="${answer%$'\r'}" # tolerate Windows line endings on piped input
  [ "$answer" = "restore" ] || { echo "aborted" >&2; exit 1; }
fi

trap 'echo "restore FAILED: api and worker remain stopped. Fix the problem and re-run, or start them with: $COMPOSE up -d api worker" >&2' ERR

# shellcheck disable=SC2086  # COMPOSE is intentionally word-split ("docker compose")
$COMPOSE stop api worker
# shellcheck disable=SC2086
$COMPOSE exec -T postgres pg_restore -U "$DB_USER" -d "$DB_NAME" --clean --if-exists --no-owner <"$file"
# shellcheck disable=SC2086
$COMPOSE up -d api worker
trap - ERR
echo "restored from $file"
