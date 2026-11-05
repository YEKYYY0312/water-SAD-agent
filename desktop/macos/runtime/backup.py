"""Explicit portable backups: logical PostgreSQL dump, user files and hashes.

The supervisor drains/stops its backend before calling create_backup. PostgreSQL
remains running for pg_dump. Never copy a running physical cluster.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import DesktopPaths, private_directory, private_write, reject_symlinks

MAX_FILES = 100000
MAX_TOTAL_BYTES = 20 * 1024**3
MAX_FILE_BYTES = 10 * 1024**3


def _allowed(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or str(path) != name or '\\' in name:
        return False
    return name == 'database.dump' or (
        len(path.parts) >= 2 and path.parts[0] in {'uploads', 'exports'}
    ) or name == 'config/mcp_web.json'


def _check_cancelled(cancel_check: Callable[[], bool]) -> None:
    if cancel_check():
        from launcher import DesktopError
        raise DesktopError('DESKTOP_START_CANCELLED')


def _hash(stream: Any, cancel_check: Callable[[], bool] = lambda: False) -> str:
    digest = hashlib.sha256()
    while block := stream.read(1024 * 1024):
        _check_cancelled(cancel_check)
        digest.update(block)
    return digest.hexdigest()


def _safe_config(path: Path) -> None:
    """Configuration has no secret storage contract; reject inline credential keys."""
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if nested and key.lower() in {'password', 'api_key', 'secret', 'token', 'access_token',
                                   'refresh_token', 'authorization', 'headers', 'env'}:
                    raise ValueError('DESKTOP_BACKUP_INLINE_SECRET')
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)
    walk(json.loads(path.read_text()))


def create_backup(paths: DesktopPaths, pg: Any, destination: Path) -> None:
    reject_symlinks(destination)
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError('DESKTOP_BACKUP_TARGET_EXISTS')
    if destination.is_relative_to(paths.resources):
        raise ValueError('DESKTOP_UNSAFE_PATH')
    with tempfile.TemporaryDirectory(prefix='backup-', dir=paths.root / 'temporary') as name:
        staging = Path(name)
        dump = staging / 'database.dump'
        pg.command('pg_dump', '--format=custom', '-Z0', '--no-owner', '--no-acl',
                   '--file', str(dump), 'wateragent', timeout=180)
        dump.chmod(0o600)
        entries: dict[str, Path] = {'database.dump': dump}
        for category in ('uploads', 'exports', 'config'):
            base = paths.root / category
            for item in sorted(base.rglob('*')):
                if item.is_symlink():
                    raise ValueError('DESKTOP_UNSAFE_PATH')
                if item.is_file():
                    relative = item.relative_to(paths.root).as_posix()
                    if _allowed(relative):
                        if category == 'config':
                            _safe_config(item)
                        entries[relative] = item
        manifest: dict[str, Any] = {'version': 1, 'created_at': int(time.time()), 'files': {}}
        total = 0
        for relative, item in entries.items():
            size = item.stat().st_size
            total += size
            if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(entries) > MAX_FILES:
                raise ValueError('DESKTOP_BACKUP_TOO_LARGE')
            with item.open('rb') as source:
                digest = _hash(source)
            manifest['files'][relative] = {'size': size, 'sha256': digest}
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as output, tarfile.open(fileobj=output, mode='w:gz') as archive:
                payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
                info = tarfile.TarInfo('manifest.json')
                info.size = len(payload)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(payload))
                for relative, item in entries.items():
                    archive.add(item, arcname=relative, recursive=False)
            # Re-open verification also detects files changed while archiving.
            validate_archive(destination)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def validate_archive(archive_path: Path, *, extract_to: Path | None = None,
                     cancel_check: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    reject_symlinks(archive_path)
    try:
        with tarfile.open(archive_path, mode='r:gz') as archive:
            members: dict[str, tarfile.TarInfo] = {}
            total = 0
            for member in archive:
                _check_cancelled(cancel_check)
                if (not member.isfile() or member.name in members or member.size < 0
                        or member.size > MAX_FILE_BYTES or len(members) >= MAX_FILES + 1):
                    raise ValueError('DESKTOP_BACKUP_INVALID')
                if member.name == 'config/mcp_web.json' and member.size > 1024**2:
                    raise ValueError('DESKTOP_BACKUP_INVALID')
                if member.name != 'manifest.json' and not _allowed(member.name):
                    raise ValueError('DESKTOP_BACKUP_INVALID')
                total += member.size
                if total > MAX_TOTAL_BYTES:
                    raise ValueError('DESKTOP_BACKUP_TOO_LARGE')
                members[member.name] = member
            manifest_info = members.pop('manifest.json', None)
            if manifest_info is None or manifest_info.size > 16 * 1024**2:
                raise ValueError('DESKTOP_BACKUP_INVALID')
            stream = archive.extractfile(manifest_info)
            if stream is None:
                raise ValueError('DESKTOP_BACKUP_INVALID')
            manifest = json.load(stream)
            if (not isinstance(manifest, dict) or manifest.get('version') != 1
                    or not isinstance(manifest.get('files'), dict)
                    or set(manifest['files']) != set(members) or 'database.dump' not in members):
                raise ValueError('DESKTOP_BACKUP_INVALID')
            for name, member in members.items():
                metadata = manifest['files'][name]
                source = archive.extractfile(member)
                if (not isinstance(metadata, dict) or metadata.get('size') != member.size
                        or source is None or _hash(source, cancel_check) != metadata.get('sha256')):
                    raise ValueError('DESKTOP_BACKUP_HASH_MISMATCH')
            # Every member is validated before any path is written. extractall
            # is intentionally unused: no links/devices/PAX path interpretation.
            if extract_to is not None:
                for name, member in members.items():
                    target = extract_to / name
                    private_directory(target.parent)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError('DESKTOP_BACKUP_INVALID')
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, 'wb') as output:
                        while chunk := source.read(1024 * 1024):
                            _check_cancelled(cancel_check)
                            output.write(chunk)
                config = extract_to / 'config' / 'mcp_web.json'
                if config.exists():
                    _safe_config(config)
            return manifest
    except (tarfile.TarError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('DESKTOP_BACKUP_INVALID') from exc


_RESTORE_TARGETS = ('postgres', 'uploads', 'exports', 'config', 'credentials/database_password')


def _remove(path: Path) -> None:
    reject_symlinks(path)
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _journal_write(paths: DesktopPaths, journal: dict[str, Any]) -> None:
    target = paths.root / 'temporary' / 'restore-journal.json'
    temporary = target.with_suffix('.new')
    private_write(temporary, json.dumps(journal))
    temporary.replace(target)


def recover_interrupted_restore(paths: DesktopPaths) -> None:
    """Called under instance lock, including by the orphan guardian after SIGKILL."""
    target = paths.root / 'temporary' / 'restore-journal.json'
    if not target.exists():
        return
    reject_symlinks(target)
    journal = json.loads(target.read_text())
    rollback_name = journal.get('rollback')
    if (not isinstance(rollback_name, str) or '/' in rollback_name
            or not rollback_name.startswith('before-restore-')
            or journal.get('targets') != list(_RESTORE_TARGETS)
            or not isinstance(journal.get('originals'), dict)
            or set(journal['originals']) != set(_RESTORE_TARGETS)
            or not all(type(value) is bool for value in journal['originals'].values())
            or journal.get('phase') not in {'prepared', 'committed'}):
        raise ValueError('DESKTOP_RESTORE_JOURNAL_INVALID')
    rollback = paths.root / 'backups' / rollback_name
    reject_symlinks(rollback)
    if journal['phase'] == 'prepared':
        for category in reversed(_RESTORE_TARGETS):
            current = paths.root / category
            previous = rollback / category
            if previous.exists():
                _remove(current)
                previous.rename(current)
            elif not journal['originals'][category]:
                _remove(current)
    target.unlink()


def commit_restore(paths: DesktopPaths, stage_paths: DesktopPaths, *,
                   cancel_check: Callable[[], bool] = lambda: False) -> None:
    from launcher import DesktopError

    rollback = private_directory(paths.root / 'backups' / ('before-restore-' + str(time.time_ns())))
    private_directory(rollback / 'credentials')
    journal = {'version': 1, 'phase': 'prepared', 'rollback': rollback.name,
               'targets': list(_RESTORE_TARGETS),
               'originals': {category: (paths.root / category).exists() for category in _RESTORE_TARGETS}}
    _journal_write(paths, journal)
    try:
        for category in _RESTORE_TARGETS:
            if cancel_check():
                raise DesktopError('DESKTOP_START_CANCELLED')
            current = paths.root / category
            if current.exists():
                current.rename(rollback / category)
            (stage_paths.root / category).rename(current)
        if cancel_check():
            raise DesktopError('DESKTOP_START_CANCELLED')
        journal['phase'] = 'committed'
        _journal_write(paths, journal)
    except BaseException:
        recover_interrupted_restore(paths)
        raise
    recover_interrupted_restore(paths)


def restore_backup(paths: DesktopPaths, archive_path: Path, *, force: bool = False,
                   cancel_event: threading.Event | None = None) -> None:
    """Restore into a new cluster, then journal the reversible directory switch.

    Caller holds InstanceLock with stdin/signal cancellation already installed.
    API credentials stay local and are never loaded from the backup.
    """
    from launcher import DesktopError, Postgres, Supervisor

    def check_cancel() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def require_running() -> None:
        if check_cancel():
            raise DesktopError('DESKTOP_START_CANCELLED')

    occupied = paths.cluster.exists() or any(
        any(p.is_file() for p in (paths.root / name).rglob('*')) for name in ('uploads', 'exports', 'config'))
    if occupied and not force:
        raise ValueError('DESKTOP_RESTORE_NEEDS_FORCE')
    if (paths.cluster / 'postmaster.pid').exists():
        raise ValueError('DESKTOP_RESTORE_DATABASE_RUNNING')
    require_running()
    validate_archive(archive_path, cancel_check=check_cancel)
    require_running()
    with tempfile.TemporaryDirectory(prefix='.wateragent-restore-', dir=paths.root.parent) as name:
        staging = Path(name)
        payload = private_directory(staging / 'payload')
        validate_archive(archive_path, extract_to=payload, cancel_check=check_cancel)
        require_running()
        stage_paths = DesktopPaths.create(paths.resources, staging / 'data')
        pg = Postgres(stage_paths, cancel_event=cancel_event)
        guardian = subprocess.Popen(
            [str(paths.resources / 'python/bin/python3.12'), '-I', '-B',
             str(paths.resources / 'runtime/service.py'), 'guard',
             '--resources', str(paths.resources), '--data-dir', str(stage_paths.root),
             '--socket-dir', str(pg.socket_dir), '--parent-pid', str(os.getpid())],
            env=pg.env, cwd=stage_paths.root / 'work', stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            pg.start()
            require_running()
            pg.command('pg_restore', '--no-owner', '--no-acl', '--exit-on-error',
                       '--dbname=wateragent', str(payload / 'database.dump'), timeout=180)
        finally:
            try:
                pg.close()
            finally:
                Supervisor._stop_child(guardian)
        require_running()
        for category in ('uploads', 'exports', 'config'):
            source = payload / category
            if source.exists():
                shutil.rmtree(stage_paths.root / category)
                # All payload links were rejected before extraction. Bound
                # cancellation latency while copying even a multi-GB upload.
                def copy_file(source_file: str, target_file: str) -> str:
                    with open(source_file, 'rb') as incoming, open(target_file, 'wb') as outgoing:
                        while block := incoming.read(1024 * 1024):
                            require_running()
                            outgoing.write(block)
                    return target_file
                shutil.copytree(source, stage_paths.root / category, copy_function=copy_file)
        commit_restore(paths, stage_paths, cancel_check=check_cancel)
