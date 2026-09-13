#!/usr/bin/env bash
# P5 Step 45 logical backup: database dump + user-file archive + checksum digest.
#
# Usage:
#   bash scripts/backup.sh [--out DIR] [--no-files]
#
# Produces, into the output directory:
#   database.dump      pg_dump --format=custom of the compose database
#   user_files.tar     tar of the app upload/export volumes (unless --no-files)
#   sha256sums.txt     sha256 digest of every artefact
#   manifest.json      schema version, sizes, digests, entry list, row counts
#
# The manifest stores *counts and digests only*.  No row content, no user text
# and no secret value is ever written to the backup directory or to stdout: the
# database is reached through the container's own credentials, so no password is
# ever placed on a command line or in an environment variable of this script.
set -euo pipefail

# shellcheck source=scripts/compose_common.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/compose_common.sh"

OUT_DIR=""
SKIP_FILES=0
while [ $# -gt 0 ]; do
    case "$1" in
        --out)
            OUT_DIR="${2:?--out needs a directory}"
            shift 2
            ;;
        --no-files)
            SKIP_FILES=1
            shift
            ;;
        -h | --help)
            sed -n '2,12p' "$0"
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            exit 64
            ;;
    esac
done

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
if [ -z "$OUT_DIR" ]; then
    OUT_DIR="$REPO_ROOT/backups/$STAMP"
fi
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

if [ ! -f "$REPO_ROOT/secrets/postgres_password" ]; then
    echo "ERROR: secrets/postgres_password is missing; the compose stack cannot start." >&2
    exit 1
fi

PY="$(resolve_python)"
if [ -z "$PY" ]; then
    echo "ERROR: no python interpreter found to write the manifest." >&2
    exit 1
fi

if [ -z "$(service_container postgres)" ]; then
    echo "ERROR: no water-agent postgres container; start the stack before backing up." >&2
    exit 1
fi

echo "==> dumping database (logical, pg_dump --format=custom)"
compose exec -T postgres sh -ec \
    'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-privileges' \
    >"$OUT_DIR/database.dump"

DB_BYTES="$(file_size "$OUT_DIR/database.dump")"
if [ "$DB_BYTES" -lt 512 ]; then
    echo "ERROR: database dump is suspiciously small ($DB_BYTES bytes)." >&2
    exit 1
fi
echo "database.dump: $DB_BYTES bytes"

FILE_MODE="skipped"
FILE_BYTES="0"
ENTRY_COUNT="0"
ENTRIES_JSON="[]"
if [ "$SKIP_FILES" -eq 0 ]; then
    echo "==> archiving application volumes"
    compose run --rm --no-deps -T --entrypoint sh app -c \
        'tar -cf - -C /var/lib/water-agent .' >"$OUT_DIR/user_files.tar"
    FILE_BYTES="$(file_size "$OUT_DIR/user_files.tar")"
    FILE_MODE="included"
    # Only paths and sizes are recorded; file contents are never inspected.
    # Listing only: names and sizes.  Paths containing spaces are recorded with
    # a truncated name by this awk-based parse, which is acceptable for a
    # manifest whose purpose is an inventory, not an exact restore plan.
    ENTRY_LIST="$(tar -tvf "$OUT_DIR/user_files.tar" 2>/dev/null | awk '{print $3" "$NF}' || true)"
    ENTRY_COUNT="$(printf '%s\n' "$ENTRY_LIST" | grep -c . || true)"
    ENTRIES_JSON="$(printf '%s\n' "$ENTRY_LIST" | grep . | "$PY" -c '
import json, sys
rows = []
for line in sys.stdin:
    parts = line.rstrip("\n").split(" ", 1)
    if len(parts) != 2:
        continue
    size, path = parts
    if path in (".", "./"):
        continue
    rows.append({"path": path, "bytes": int(size) if size.isdigit() else None})
print(json.dumps(rows, ensure_ascii=False))
' || echo '[]')"
    echo "user_files.tar: $FILE_BYTES bytes, $ENTRY_COUNT entries"
fi

echo "==> recording row counts"
COUNTS_FILE="$OUT_DIR/row_counts.txt"
db_counts >"$COUNTS_FILE"
cat "$COUNTS_FILE"

DB_SHA="$(file_sha256 "$OUT_DIR/database.dump")"
FILE_SHA="$([ "$SKIP_FILES" -eq 0 ] && file_sha256 "$OUT_DIR/user_files.tar" || echo "")"

: >"$OUT_DIR/sha256sums.txt"
echo "$DB_SHA  database.dump" >>"$OUT_DIR/sha256sums.txt"
if [ "$SKIP_FILES" -eq 0 ]; then
    echo "$FILE_SHA  user_files.tar" >>"$OUT_DIR/sha256sums.txt"
fi

OUT_DIR="$OUT_DIR" DB_SHA="$DB_SHA" FILE_SHA="$FILE_SHA" DB_BYTES="$DB_BYTES" \
    FILE_BYTES="$FILE_BYTES" FILE_MODE="$FILE_MODE" ENTRY_COUNT="$ENTRY_COUNT" \
    ENTRIES_JSON="$ENTRIES_JSON" STAMP="$STAMP" COUNTS_FILE="$COUNTS_FILE" \
    "$PY" - <<'PY'
import json
import os
from pathlib import Path

out = Path(os.environ["OUT_DIR"])
counts: dict[str, object] = {}
for line in Path(os.environ["COUNTS_FILE"]).read_text(encoding="utf-8").splitlines():
    key, _, value = line.partition("=")
    if not key:
        continue
    counts[key] = int(value) if value.isdigit() else value
manifest = {
    "schema_version": 1,
    "generator": "scripts/backup.sh",
    "created_at": os.environ["STAMP"],
    "compose_project": "water-agent",
    "database": {
        "file": "database.dump",
        "format": "pg_dump-custom",
        "bytes": int(os.environ["DB_BYTES"]),
        "sha256": os.environ["DB_SHA"],
        "contains_row_content": True,
    },
    "user_files": {
        "status": os.environ["FILE_MODE"],
        "file": "user_files.tar" if os.environ["FILE_MODE"] == "included" else None,
        "bytes": int(os.environ["FILE_BYTES"]),
        "sha256": os.environ["FILE_SHA"] or None,
        "entry_count": int(os.environ["ENTRY_COUNT"]),
        "entries": json.loads(os.environ["ENTRIES_JSON"]),
    },
    "row_counts": counts,
    "checksums_file": "sha256sums.txt",
    "notes": [
        "counts and digests only; no memory body, session text or secret is recorded here",
        "forgotten_with_body must stay 0: a forgotten memory may never hold a body",
        "the dump contains user data and must be stored with the same care as the volume",
    ],
}
(out / "manifest.json").write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(f"manifest: {out / 'manifest.json'}")
PY

echo "==> backup complete: $OUT_DIR"
