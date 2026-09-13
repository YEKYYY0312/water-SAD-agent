#!/usr/bin/env bash
# P5 Step 45 restore: verify a backup, back up the current state first, then
# restore into the compose stack and prove account/session/memory consistency.
#
# Usage:
#   bash scripts/restore.sh --from DIR [--force] [--no-pre-backup]
#
# Safety rules (each one is a hard refusal, not a warning):
#   1. The target must be empty, or --force must be given explicitly.  "Empty"
#      means the stack is not running, the database is unmigrated, or every
#      compared table holds zero rows.
#   2. Every artefact is verified against manifest.json + sha256sums.txt before
#      a single row is touched.
#   3. The current state is backed up first (scripts/backup.sh) unless
#      --no-pre-backup is passed; that pre-restore backup is an unverified
#      safety net, and its path is printed.
#   4. After restoring, the row counts and the "forgotten memory never carries a
#      body" invariant are re-measured and compared with the manifest.
#
# No secret value is printed: the database is reached through the container's
# own credentials and the admin password is never read by this script.
set -euo pipefail

# shellcheck source=scripts/compose_common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/compose_common.sh"

PY="$(resolve_python)"
FROM_DIR=""
FORCE=0
PRE_BACKUP=1
while [ $# -gt 0 ]; do
    case "$1" in
        --from)
            FROM_DIR="${2:?--from needs a directory}"
            shift 2
            ;;
        --force)
            FORCE=1
            shift
            ;;
        --no-pre-backup)
            PRE_BACKUP=0
            shift
            ;;
        -h | --help)
            sed -n '2,20p' "$0"
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            exit 64
            ;;
    esac
done

if [ -z "$FROM_DIR" ]; then
    echo "ERROR: --from DIR is required." >&2
    exit 64
fi
FROM_DIR="$(cd "$FROM_DIR" && pwd)"
MANIFEST="$FROM_DIR/manifest.json"

echo "==> verifying backup at $FROM_DIR"
"$PY" -m scripts.backup_manifest verify --from "$FROM_DIR"

echo "==> checking restore target"
COUNTS_BEFORE="$(db_counts || echo 'migrated=false')"
printf '%s\n' "$COUNTS_BEFORE"
TARGET_IN_USE=0
while IFS='=' read -r key value; do
    case "$key" in
        migrated)
            [ "$value" = "true" ] || continue
            ;;
        users | web_sessions | active_memories | memory_events)
            if [ "${value:-0}" != "0" ]; then TARGET_IN_USE=1; fi
            ;;
        *) ;;
    esac
done <<<"$COUNTS_BEFORE"

if [ "$TARGET_IN_USE" -eq 1 ] && [ "$FORCE" -ne 1 ]; then
    echo "ERROR: target database already holds data; pass --force to overwrite it." >&2
    exit 1
fi
if [ "$TARGET_IN_USE" -eq 1 ]; then
    echo "WARNING: --force given; the current data will be replaced."
fi

PRE_DIR=""
if [ "$PRE_BACKUP" -eq 1 ] && [ -n "$(service_container postgres)" ]; then
    PRE_DIR="$REPO_ROOT/backups/pre-restore-$(date -u +%Y%m%dT%H%M%SZ)"
    echo "==> pre-restore backup of the current state -> $PRE_DIR"
    bash "$REPO_ROOT/scripts/backup.sh" --out "$PRE_DIR" --no-files
fi

echo "==> stopping the app to freeze writes"
compose stop app >/dev/null 2>&1 || true

echo "==> restoring database"
compose exec -T postgres sh -ec \
    'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --no-privileges --exit-on-error' \
    <"$FROM_DIR/database.dump"

USER_TAR="$FROM_DIR/user_files.tar"
if [ -f "$USER_TAR" ]; then
    echo "==> restoring application volumes"
    # -m (do not restore mtimes) is required: /var/lib/water-agent itself is on
    # the read-only root filesystem, so tar's attempt to utime "." fails with
    # "Cannot utime: Read-only file system".  Only the uploads/ and exports/
    # mounts are writable, and -m keeps tar off directory metadata.
    compose run --rm --no-deps -T --entrypoint tar app -xmf - -C /var/lib/water-agent \
        <"$USER_TAR"
else
    echo "==> no user_files.tar in this backup; volumes left untouched"
fi

echo "==> starting the app"
compose up -d app >/dev/null

echo "==> verifying restored state against the manifest"
COUNTS_AFTER="$(db_counts)"
printf '%s\n' "$COUNTS_AFTER"
COUNTS_FILE="$(mktemp)"
printf '%s\n' "$COUNTS_AFTER" >"$COUNTS_FILE"
"$PY" -m scripts.backup_manifest compare --manifest "$MANIFEST" --counts "$COUNTS_FILE"
rm -f "$COUNTS_FILE"

echo "==> restore complete"
if [ -n "$PRE_DIR" ]; then
    echo "pre-restore backup kept at: $PRE_DIR"
else
    echo "no pre-restore backup was taken (--no-pre-backup, or the stack was not running)"
fi
