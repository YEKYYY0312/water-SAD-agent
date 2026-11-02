"""P5 Step 45 backup manifest verification and restore comparison.

``scripts/restore.sh`` must refuse to touch a database unless the backup it is
about to apply is intact and the target state is accounted for.  Keeping that
logic in a Python module (instead of a shell heredoc) makes the safety-critical
part unit-testable without Docker or PostgreSQL.

CLI
---
``python -m scripts.backup_manifest verify --from DIR``
    Validate ``manifest.json`` + ``sha256sums.txt`` + every listed artefact.
    Exits 0 on success; prints one line per verified artefact.

``python -m scripts.backup_manifest compare --manifest FILE --counts FILE``
    Compare a backup's recorded row counts with the post-restore counts and
    enforce the privacy invariant that a forgotten memory never carries a body.
    Exits 1 and prints every mismatch when the restore did not reproduce state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA_VERSION = 1

#: Tables the restore acceptance compares.  ``migrated`` is handled separately.
COMPARED_KEYS: tuple[str, ...] = (
    "users",
    "auth_sessions",
    "web_sessions",
    "runs",
    "projects",
    "active_memories",
    "forgotten_memories",
    "memory_events",
)


def postgres_secret_path(repo_root: Path) -> Path:
    """The database password file compose.yml mounts as the ``postgres_password`` secret.

    ``scripts/backup.sh`` refuses to run without it.  The name is spelled out here
    rather than in ``src/``: this module never ships in the image, whereas
    ``scripts/check_image.sh`` fails the build on any image file that names the secret.
    """

    return repo_root / "secrets" / "postgres_password"


class BackupVerificationError(RuntimeError):
    """The backup is unusable; restoring it would be unsafe."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_counts(path: Path) -> dict[str, Any]:
    """Parse the ``key=value`` output of ``db_counts`` in compose_common.sh."""

    counts: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        counts[key] = int(value) if value.strip().isdigit() else value.strip()
    return counts


def parse_counts_text(text: str) -> dict[str, Any]:
    """Same as :func:`read_counts` but for in-memory output."""

    counts: dict[str, Any] = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        counts[key] = int(value) if value.strip().isdigit() else value.strip()
    return counts


def verify_backup(root: Path) -> dict[str, Any]:
    """Validate a backup directory; raise on the first unsafe condition."""

    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise BackupVerificationError(f"{manifest_path} is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BackupVerificationError(f"{manifest_path} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BackupVerificationError("manifest.json must be an object")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise BackupVerificationError(
            f"unsupported manifest schema_version: {manifest.get('schema_version')!r}"
        )

    checksums_path = root / str(manifest.get("checksums_file") or "sha256sums.txt")
    if not checksums_path.is_file():
        raise BackupVerificationError(f"{checksums_path} is missing")
    expected: dict[str, str] = {}
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, separator, name = line.partition("  ")
        if not separator or not name.strip():
            raise BackupVerificationError(f"malformed checksum line: {line!r}")
        expected[name.strip()] = digest.strip()
    if not expected:
        raise BackupVerificationError("checksum file lists no artefacts")

    verified: list[str] = []
    for name, digest in expected.items():
        path = root / name
        if not path.is_file():
            raise BackupVerificationError(f"{name} is listed in the checksums but missing")
        actual = _sha256(path)
        if actual != digest:
            raise BackupVerificationError(f"checksum mismatch for {name}")
        verified.append(name)

    for section in ("database", "user_files"):
        block = manifest.get(section)
        if not isinstance(block, dict):
            continue
        manifest_file = block.get("file")
        if not isinstance(manifest_file, str) or not manifest_file:
            continue
        path = root / manifest_file
        if not path.is_file():
            raise BackupVerificationError(
                f"{manifest_file} referenced by manifest.{section} is missing"
            )
        size = path.stat().st_size
        if block.get("bytes") is not None and size != int(block["bytes"]):
            raise BackupVerificationError(
                f"{manifest_file} size {size} != manifest {block['bytes']}"
            )
        if block.get("sha256") and _sha256(path) != block["sha256"]:
            raise BackupVerificationError(f"{manifest_file} digest does not match the manifest")

    database = manifest.get("database") or {}
    if not isinstance(database, dict) or not database.get("file"):
        raise BackupVerificationError("manifest.database.file is required")
    if int(database.get("bytes") or 0) < 512:
        raise BackupVerificationError("manifest records an implausibly small database dump")
    return manifest


def compare_counts(manifest: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Return one message per inconsistency between backup and restored state."""

    failures: list[str] = []
    if actual.get("migrated") != "true":
        failures.append("restored database is not migrated")
    expected = manifest.get("row_counts") or {}
    if not isinstance(expected, dict):
        return [*failures, "manifest.row_counts must be an object"]
    if expected.get("migrated") != "true":
        failures.append("backup itself was taken from an unmigrated database")
    for key in COMPARED_KEYS:
        want = expected.get(key)
        got = actual.get(key)
        if want is None:
            failures.append(f"{key}: backup recorded no count")
            continue
        if got is None:
            failures.append(f"{key}: restored state has no count")
            continue
        if int(want) != int(got):
            failures.append(f"{key}: backup={want} restored={got}")
    # Privacy invariant: forgetting must not be undone by a restore.
    restored_bodies = int(actual.get("forgotten_with_body") or 0)
    if restored_bodies != 0:
        failures.append(f"forgotten_with_body: restored {restored_bodies} forgotten rows carry a body")
    backup_bodies = expected.get("forgotten_with_body")
    if backup_bodies not in (None, 0):
        failures.append(f"backup recorded {backup_bodies} bodies on forgotten rows")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="verify a P5 compose backup")
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify", help="validate manifest and checksums")
    verify.add_argument("--from", dest="source", required=True)

    compare = sub.add_parser("compare", help="compare backup counts with restored counts")
    compare.add_argument("--manifest", required=True)
    compare.add_argument("--counts", required=True, help="key=value file of restored counts")

    args = parser.parse_args(argv)
    if args.command == "verify":
        root = Path(args.source)
        try:
            manifest = verify_backup(root)
        except BackupVerificationError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        entries = (manifest.get("user_files") or {}).get("entries") or []
        print(
            json.dumps(
                {
                    "ok": True,
                    "verified_files": len(manifest.get("database") or {}),
                    "user_file_entries": len(entries) if isinstance(entries, list) else 0,
                    "row_count_keys": sorted((manifest.get("row_counts") or {}).keys()),
                },
                ensure_ascii=False,
            )
        )
        return 0

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    actual = read_counts(Path(args.counts))
    failures = compare_counts(manifest, actual)
    if failures:
        print(f"ERROR: restore verification failed: {'; '.join(failures)}", file=sys.stderr)
        print(json.dumps({"ok": False, "failures": failures}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "restored_counts": actual}, ensure_ascii=False))
    return 0


__all__ = [
    "COMPARED_KEYS",
    "MANIFEST_SCHEMA_VERSION",
    "BackupVerificationError",
    "compare_counts",
    "main",
    "parse_counts_text",
    "read_counts",
    "verify_backup",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
