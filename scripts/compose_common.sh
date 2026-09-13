#!/usr/bin/env bash
# Shared helpers for the P5 self-hosted compose operations (Step 45).
#
# Sourced by scripts/backup.sh and scripts/restore.sh.  Nothing here prints a
# secret value: database access always goes through the container's own
# environment (DATABASE_PASSWORD_FILE), never through a command-line argument.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

#: Compose files used by every operation.  The production overlay is optional:
#: set WATER_AGENT_PRODUCTION=1 to include compose.production.yml.
compose_files() {
    printf -- '-f %s' "$REPO_ROOT/compose.yml"
    if [ "${WATER_AGENT_PRODUCTION:-0}" = "1" ]; then
        printf -- ' -f %s' "$REPO_ROOT/compose.production.yml"
    fi
}

#: Resolve the docker CLI.  Docker Desktop on macOS is often not on PATH, which
#: is exactly the case on the maintainer machine.
resolve_docker() {
    if [ -n "${DOCKER:-}" ]; then
        printf '%s' "$DOCKER"
        return 0
    fi
    local found
    found="$(command -v docker || true)"
    if [ -z "$found" ] && [ -x /Applications/Docker.app/Contents/Resources/bin/docker ]; then
        found=/Applications/Docker.app/Contents/Resources/bin/docker
    fi
    printf '%s' "$found"
}

require_docker() {
    local docker
    docker="$(resolve_docker)"
    if [ -z "$docker" ]; then
        echo "ERROR: docker CLI not found (checked PATH and Docker Desktop)." >&2
        return 1
    fi
    if ! "$docker" info >/dev/null 2>&1; then
        echo "ERROR: docker daemon is unreachable." >&2
        return 1
    fi
    printf '%s' "$docker"
}

compose() {
    local docker
    docker="$(require_docker)"
    # shellcheck disable=SC2046
    "$docker" compose $(compose_files) "$@"
}

#: True when a service container exists (running or exited).
service_container() {
    compose ps -aq "$1" 2>/dev/null | head -1
}

#: Run a SQL statement with tab-separated output, using the container's own
#: credentials.  Fails loudly when postgres is not available.
db_query() {
    local sql="$1"
    compose exec -T postgres sh -ec \
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$1"' _ "$sql"
}

#: Emit `key=value` rows for the tables the restore acceptance compares.
#: Missing tables (unmigrated volume) are reported as `migrated=false`.
db_counts() {
    local raw
    if ! raw="$(db_query "SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM auth_sessions), (SELECT count(*) FROM web_sessions), (SELECT count(*) FROM runs), (SELECT count(*) FROM projects), (SELECT count(*) FROM memory_records WHERE status = 'active' AND tombstone = false), (SELECT count(*) FROM memory_records WHERE status = 'forgotten'), (SELECT count(*) FROM memory_records WHERE status = 'forgotten' AND content IS NOT NULL), (SELECT count(*) FROM memory_events)" 2>/dev/null)"; then
        echo "migrated=false"
        return 0
    fi
    raw="$(printf '%s' "$raw" | tr -d '[:space:]')"
    local IFS='|'
    # shellcheck disable=SC2086
    set -- $raw
    echo "migrated=true"
    echo "users=${1:-0}"
    echo "auth_sessions=${2:-0}"
    echo "web_sessions=${3:-0}"
    echo "runs=${4:-0}"
    echo "projects=${5:-0}"
    echo "active_memories=${6:-0}"
    echo "forgotten_memories=${7:-0}"
    echo "forgotten_with_body=${8:-0}"
    echo "memory_events=${9:-0}"
}

#: Portable sha256 of one file (macOS ships shasum, Linux ships sha256sum).
file_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

#: Prefer the repository virtualenv, then the platform python3.
resolve_python() {
    if [ -x "$REPO_ROOT/_venv/bin/python" ]; then
        printf '%s' "$REPO_ROOT/_venv/bin/python"
        return 0
    fi
    command -v python3 || command -v python || true
}

file_size() {
    wc -c <"$1" | tr -d ' '
}
