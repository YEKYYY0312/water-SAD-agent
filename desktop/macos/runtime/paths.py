"""Private desktop state, never a path into another deployment."""
from __future__ import annotations

import fcntl
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Self


def reject_symlinks(path: Path) -> None:
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('DESKTOP_UNSAFE_PATH')
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError('DESKTOP_UNSAFE_PATH')


def private_directory(path: Path) -> Path:
    reject_symlinks(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.stat().st_uid != os.geteuid():
        raise ValueError('DESKTOP_UNSAFE_PATH')
    path.chmod(0o700)
    return path


def private_write(path: Path, content: str | bytes) -> None:
    reject_symlinks(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(fd)
        if (metadata.st_uid != os.geteuid() or metadata.st_nlink != 1
                or not stat.S_ISREG(metadata.st_mode)):
            raise ValueError('DESKTOP_UNSAFE_PATH')
        os.ftruncate(fd, 0)
        os.fchmod(fd, 0o600)
        payload = content.encode('utf-8') if isinstance(content, str) else content
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class DesktopPaths:
    resources: Path
    root: Path

    @classmethod
    def create(cls, resources: Path, root: Path) -> DesktopPaths:
        reject_symlinks(root)
        resources = resources.resolve()
        if (root == Path.home() or len(root.parts) < 3
                or root.is_relative_to(resources) or resources.is_relative_to(root)):
            raise ValueError('DESKTOP_UNSAFE_PATH')
        private_directory(root)
        result = cls(resources, root)
        for name in ('credentials', 'logs', 'uploads', 'exports', 'config', 'traces',
                     'sessions', 'backups', 'temporary', 'work'):
            private_directory(root / name)
        for subdirectory in ('credentials/models', 'config/skills', 'config/installed_skills'):
            private_directory(root / subdirectory)
        return result

    @property
    def cluster(self) -> Path:
        return self.root / 'postgres'

    @property
    def credentials(self) -> Path:
        return self.root / 'credentials'

    @property
    def database_password(self) -> Path:
        return self.credentials / 'database_password'

    @property
    def logs(self) -> Path:
        return self.root / 'logs'

    @property
    def uploads(self) -> Path:
        return self.root / 'uploads'

    @property
    def exports(self) -> Path:
        return self.root / 'exports'


class InstanceLock:
    """Kernel lock is released on crash; never unlink a lock another process sees."""
    def __init__(self, path: Path):
        self.path = path
        self.stream: IO[str] | None = None

    def __enter__(self) -> Self:
        reject_symlinks(self.path)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.stream = os.fdopen(fd, 'r+')
        try:
            if os.fstat(fd).st_uid != os.geteuid():
                raise ValueError('DESKTOP_UNSAFE_PATH')
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ValueError):
            self.stream.close()
            self.stream = None
            raise RuntimeError('DESKTOP_ALREADY_RUNNING') from None
        self.stream.seek(0)
        self.stream.truncate()
        self.stream.write(str(os.getpid()))
        self.stream.flush()
        return self

    def __exit__(self, *args: object) -> None:
        if self.stream is not None:
            self.stream.close()
            self.stream = None
