"""P5 Step 45: backup/restore safety rules.

Two layers are tested:

* **Offline (always runs)** — the manifest verifier and the post-restore count
  comparison, exercised on synthetic backup directories.  These are the rules
  that decide whether a restore is allowed to touch a database, so they must be
  provable without Docker.
* **End-to-end (gated)** — the real ``scripts/backup.sh`` / ``scripts/restore.sh``
  against a live compose stack.  It mutates volumes, so it only runs when the
  operator opts in with ``WATER_AGENT_COMPOSE_TEST=1``; otherwise it SKIPs with
  an explicit reason.  The release-gate path for this is the ``p5-compose``
  GitHub Actions workflow, not the offline pytest gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.backup_manifest import (
    BackupVerificationError,
    compare_counts,
    parse_counts_text,
    read_counts,
    verify_backup,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

CLEAN_COUNTS = {
    "migrated": "true",
    "users": 3,
    "auth_sessions": 2,
    "web_sessions": 2,
    "runs": 1,
    "projects": 1,
    "active_memories": 4,
    "forgotten_memories": 2,
    "forgotten_with_body": 0,
    "memory_events": 11,
}


def _write_backup(
    root: Path,
    *,
    counts: dict[str, object] | None = None,
    database_bytes: int = 4096,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "database.dump").write_bytes(b"PGDMP" + b"\x00" * (database_bytes - 5))
    (root / "user_files.tar").write_bytes(b"tar" + b"\x00" * 1021)
    counts = counts if counts is not None else dict(CLEAN_COUNTS)
    (root / "row_counts.txt").write_text(
        "\n".join(f"{key}={value}" for key, value in counts.items()) + "\n", encoding="utf-8"
    )
    digests = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("database.dump", "user_files.tar")
    }
    (root / "sha256sums.txt").write_text(
        "\n".join(f"{digest}  {name}" for name, digest in digests.items()) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "generator": "scripts/backup.sh",
        "created_at": "2026-09-13T00:00:00Z",
        "compose_project": "water-agent",
        "database": {
            "file": "database.dump",
            "format": "pg_dump-custom",
            "bytes": (root / "database.dump").stat().st_size,
            "sha256": digests["database.dump"],
            "contains_row_content": True,
        },
        "user_files": {
            "status": "included",
            "file": "user_files.tar",
            "bytes": (root / "user_files.tar").stat().st_size,
            "sha256": digests["user_files.tar"],
            "entry_count": 1,
            "entries": [{"path": "./uploads/.keep", "bytes": 0}],
        },
        "row_counts": counts,
        "checksums_file": "sha256sums.txt",
        "notes": ["counts and digests only"],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return root


# ---------------------------------------------------------------------------
# Offline: manifest verification
# ---------------------------------------------------------------------------


def test_well_formed_backup_verifies(tmp_path: Path) -> None:
    root = _write_backup(tmp_path / "good")

    manifest = verify_backup(root)

    assert manifest["row_counts"]["users"] == 3
    assert manifest["database"]["format"] == "pg_dump-custom"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("tampered_dump", "checksum mismatch"),
        ("missing_tar", "missing"),
        ("wrong_size", "size"),
    ],
)
def test_corrupted_backup_is_refused(tmp_path: Path, mutation: str, message: str) -> None:
    root = _write_backup(tmp_path / mutation)
    if mutation == "tampered_dump":
        (root / "database.dump").write_bytes(b"PGDMP" + b"\x01" * 4091)
    elif mutation == "missing_tar":
        (root / "user_files.tar").unlink()
    else:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        manifest["database"]["bytes"] += 1
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupVerificationError, match=message):
        verify_backup(root)


def test_missing_manifest_or_wrong_schema_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BackupVerificationError, match="manifest.json is missing"):
        verify_backup(empty)

    root = _write_backup(tmp_path / "schema")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["schema_version"] = 99
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BackupVerificationError, match="unsupported manifest schema_version"):
        verify_backup(root)


def test_checksum_file_without_entries_is_refused(tmp_path: Path) -> None:
    root = _write_backup(tmp_path / "nocsums")
    (root / "sha256sums.txt").write_text("", encoding="utf-8")

    with pytest.raises(BackupVerificationError, match="lists no artefacts"):
        verify_backup(root)


def test_implausibly_small_dump_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "tiny"
    root.mkdir()
    # A backup whose dump is a few bytes cannot be a real pg_dump output; the
    # manifest is rebuilt around it so only the size rule can catch this.
    (root / "database.dump").write_bytes(b"PGDMP")
    (root / "sha256sums.txt").write_text(
        hashlib.sha256(b"PGDMP").hexdigest() + "  database.dump\n", encoding="utf-8"
    )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "checksums_file": "sha256sums.txt",
                "database": {"file": "database.dump", "bytes": 5},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BackupVerificationError, match="implausibly small"):
        verify_backup(root)


# ---------------------------------------------------------------------------
# Offline: post-restore comparison
# ---------------------------------------------------------------------------


def test_restored_state_matching_the_backup_passes(tmp_path: Path) -> None:
    manifest = verify_backup(_write_backup(tmp_path / "match"))

    assert compare_counts(manifest, dict(CLEAN_COUNTS)) == []


def test_row_count_drift_is_reported_per_table(tmp_path: Path) -> None:
    manifest = verify_backup(_write_backup(tmp_path / "drift"))
    restored = {**CLEAN_COUNTS, "users": 2, "active_memories": 0}

    failures = compare_counts(manifest, restored)

    assert any(item.startswith("users: backup=3 restored=2") for item in failures)
    assert any(item.startswith("active_memories: backup=4 restored=0") for item in failures)


def test_unmigrated_restore_target_is_reported() -> None:
    manifest = {"row_counts": dict(CLEAN_COUNTS)}

    failures = compare_counts(manifest, {"migrated": "false"})

    assert "restored database is not migrated" in failures


def test_forgotten_memory_never_comes_back_with_a_body(tmp_path: Path) -> None:
    manifest = verify_backup(_write_backup(tmp_path / "privacy"))
    restored = {**CLEAN_COUNTS, "forgotten_with_body": 1}

    failures = compare_counts(manifest, restored)

    assert any("forgotten_with_body" in item for item in failures)
    assert any("carry a body" in item for item in failures)


def test_backup_taken_from_a_bad_state_is_reported(tmp_path: Path) -> None:
    counts = {**CLEAN_COUNTS, "forgotten_with_body": 2}
    manifest = verify_backup(_write_backup(tmp_path / "badstate", counts=counts))

    failures = compare_counts(manifest, dict(CLEAN_COUNTS))

    assert any("backup recorded 2 bodies" in item for item in failures)


def test_counts_parsing_matches_the_shell_helper_format(tmp_path: Path) -> None:
    path = tmp_path / "row_counts.txt"
    path.write_text("migrated=true\nusers=0\n\nauth_sessions=0\n", encoding="utf-8")

    parsed = read_counts(path)

    assert parsed == {"migrated": "true", "users": 0, "auth_sessions": 0}
    assert parse_counts_text("users=4\nmigrated=true") == {"users": 4, "migrated": "true"}


# ---------------------------------------------------------------------------
# Script contract (offline, text-level)
# ---------------------------------------------------------------------------


def test_scripts_declare_the_required_safety_rules() -> None:
    backup = (REPO_ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")
    restore = (REPO_ROOT / "scripts" / "restore.sh").read_text(encoding="utf-8")

    assert "pg_dump" in backup and "--format=custom" in backup
    assert "user_files.tar" in backup and "sha256sums.txt" in backup
    assert "manifest.json" in backup
    assert "db_counts" in backup
    # restore: empty target or explicit --force, checksum verify, pre-restore backup
    assert "--force" in restore
    assert "already holds data" in restore
    assert "backup_manifest" in restore
    assert "scripts/backup.sh" in restore
    assert "pre-restore" in restore


def test_python_scripts_are_syntax_valid() -> None:
    import ast

    for name in ("backup_manifest.py", "compare_memory_p0.py", "compose_common.sh"):
        path = REPO_ROOT / "scripts" / name
        if path.suffix != ".py":
            continue
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _docker() -> str | None:
    found = shutil.which("docker")
    if found:
        return found
    desktop = Path("/Applications/Docker.app/Contents/Resources/bin/docker")
    return str(desktop) if desktop.is_file() else None


@pytest.mark.skipif(
    os.getenv("WATER_AGENT_COMPOSE_TEST") != "1",
    reason=(
        "compose backup/restore mutates user data volumes; set WATER_AGENT_COMPOSE_TEST=1 "
        "to run it against a live stack (the p5-compose workflow does this)"
    ),
)
def test_compose_backup_and_restore_roundtrip(tmp_path: Path) -> None:
    docker = _docker()
    if docker is None:
        pytest.skip("docker CLI is not available on this host")

    running = subprocess.run(
        [docker, "compose", "ps", "--format", "json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if running.returncode != 0 or not running.stdout.strip():
        pytest.skip("no running water-agent compose stack; backup/restore needs a live stack")

    out = tmp_path / "backup"
    backup = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "backup.sh"), "--out", str(out)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert backup.returncode == 0, backup.stdout + backup.stderr
    manifest = verify_backup(out)
    assert manifest["row_counts"]["migrated"] == "true"

    refused = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "restore.sh"), "--from", str(out)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode != 0
    assert "--force" in refused.stderr or "already holds data" in refused.stderr

    forced = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "restore.sh"), "--from", str(out), "--force"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert '"ok": true' in forced.stdout
