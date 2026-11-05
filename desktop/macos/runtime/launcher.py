"""Standalone App supervisor. Stdout is exclusively the native JSON protocol."""
from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

# -I deliberately removes script-directory imports. Add only our bundle runtime.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import DesktopPaths, InstanceLock, private_write, reject_symlinks
from process_identity import postmaster_matches, process_exists

_EVENT_LOG: Path | None = None


class DesktopError(RuntimeError):
    pass


def select_bundle_index(resources: Path) -> Path:
    """Select one published primary index, including versioned release folders."""
    index_root = resources / 'backend' / 'data' / 'indexes'
    candidates = sorted([*index_root.rglob('*.db'), *index_root.rglob('*.trivium')])
    candidates = [candidate for candidate in candidates if candidate.is_file()]
    if not candidates:
        raise DesktopError('DESKTOP_INDEX_MISSING')
    if len(candidates) != 1:
        raise DesktopError('DESKTOP_INDEX_AMBIGUOUS')
    selected = candidates[0]
    if selected.is_symlink() or not selected.resolve().is_relative_to(index_root.resolve()):
        raise DesktopError('DESKTOP_INDEX_UNSAFE')
    return selected


def isolated_environment(paths: DesktopPaths, socket_dir: Path) -> dict[str, str]:
    """Allowlist OS essentials; never copy production API keys, PG or proxy env."""
    env = {
        'PATH': f'{paths.resources}/libredwg/bin:/usr/bin:/bin:/usr/sbin:/sbin',
        'HOME': str(Path.home()), 'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8',
        'TMPDIR': str(paths.root / 'temporary'),
        'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1',
        'DATABASE_URL': ('postgresql+psycopg://wateragent@/wateragent?host='
                         + quote(str(socket_dir), safe='')),
        'DATABASE_PASSWORD_FILE': str(paths.database_password),
        'PGHOST': str(socket_dir), 'PGPORT': '5432', 'PGUSER': 'wateragent',
        'PGDATABASE': 'wateragent', 'PGPASSFILE': str(paths.credentials / 'pgpass'),
        'WATER_AGENT_CREDENTIALS_DIR': str(paths.credentials / 'models'),
        'WATER_AGENT_UPLOAD_DIR': str(paths.uploads),
        'WATER_AGENT_EXPORT_DIR': str(paths.exports),
        'WATER_AGENT_TRACE_DIR': str(paths.root / 'traces'),
        'WATER_AGENT_MCP_WEB_LAYER': str(paths.root / 'config' / 'mcp_web.json'),
        'WEB_SECURE_COOKIE': 'false',
        'WATER_AGENT_SKILLS': 'on',
        'WATER_AGENT_SKILLS_DIR': str(paths.root / 'config' / 'skills'),
        'WATER_AGENT_SKILLS_INSTALLED_DIR': str(paths.root / 'config' / 'installed_skills'),
    }
    # Explicitly select the release index; never silently choose a developer DB.
    env['WATER_AGENT_INDEX_PATH'] = str(select_bundle_index(paths.resources))
    for binary, name in [('dwg2dxf', 'WATER_AGENT_DWG2DXF'),
                         ('dxf2dwg', 'WATER_AGENT_DXF2DWG')]:
        executable = paths.resources / 'libredwg' / 'bin' / binary
        if executable.is_file():
            env[name] = str(executable)
    return env


def parse_command(line: str) -> dict[str, Any]:
    if len(line) > 65536:
        raise ValueError('DESKTOP_INVALID_COMMAND')
    try:
        data = json.loads(line)
    except (ValueError, TypeError):
        raise ValueError('DESKTOP_INVALID_COMMAND') from None
    if not isinstance(data, dict) or data.get('command') not in {'setup', 'stop', 'backup'}:
        raise ValueError('DESKTOP_INVALID_COMMAND')
    if data['command'] == 'setup' and not isinstance(data.get('password'), str):
        raise ValueError('DESKTOP_INVALID_COMMAND')
    if data['command'] == 'backup' and not isinstance(data.get('destination'), str):
        raise ValueError('DESKTOP_INVALID_COMMAND')
    return data


def read_ready(path: Path, nonce: str) -> str | None:
    try:
        reject_symlinks(path)
        data = json.loads(path.read_text())
        url = urlsplit(data['url'])
        if (data['nonce'] == nonce and url.scheme == 'http' and url.hostname == '127.0.0.1'
                and url.port and 0 < url.port < 65536 and url.path == ''
                and not url.username and not url.password and not url.query and not url.fragment):
            return str(data['url'])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


class Postgres:
    """Only pg_ctl for this private cluster; no port search or global process kill."""
    def __init__(self, paths: DesktopPaths, *, run: Callable[..., Any] | None = None,
                 cancel_event: threading.Event | None = None):
        self.paths = paths
        self.socket_dir = Path(tempfile.mkdtemp(prefix='wa-', dir='/tmp'))
        self.socket_dir.chmod(0o700)
        self.env = isolated_environment(paths, self.socket_dir)
        self.started = False
        self.run = run or self._run
        self.cancel_event = cancel_event

    def _run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        # Only our Popen handle is terminated. Cancellation never searches by
        # executable/port, and stop commands remain runnable during cleanup.
        timeout = kwargs.pop('timeout', 45)
        cancellable = kwargs.pop('cancellable', True)
        deadline = time.monotonic() + timeout
        with subprocess.Popen(argv, env=self.env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              **kwargs) as process:
            while process.poll() is None:
                cancelled = cancellable and self.cancel_event is not None and self.cancel_event.is_set()
                if cancelled or time.monotonic() >= deadline:
                    Supervisor._stop_child(process)
                    raise DesktopError('DESKTOP_START_CANCELLED' if cancelled else 'DESKTOP_POSTGRES_TIMEOUT')
                time.sleep(0.05)
            if process.returncode:
                raise DesktopError('DESKTOP_POSTGRES_FAILED')
            return subprocess.CompletedProcess(argv, process.returncode)

    def command(self, binary: str, *args: str, **kwargs: Any) -> Any:
        return self.run([str(self.paths.resources / 'postgres' / 'bin' / binary), *args],
                        env=self.env, **kwargs) if self.run != self._run else self.run(
                            [str(self.paths.resources / 'postgres' / 'bin' / binary), *args],
                            **kwargs)

    def start(self) -> None:
        cluster = self.paths.cluster
        reject_symlinks(cluster)
        initialized = (cluster / 'PG_VERSION').is_file()
        if cluster.exists() and not initialized and any(cluster.iterdir()):
            raise DesktopError('DESKTOP_CLUSTER_INCOMPLETE')
        if initialized and not self.paths.database_password.is_file():
            raise DesktopError('DESKTOP_DATABASE_SECRET_MISSING')
        if not initialized:
            private_write(self.paths.database_password, secrets.token_urlsafe(48))
            self.command('initdb', '-D', str(cluster), '--username=wateragent',
                         '--auth-local=scram-sha-256', '--auth-host=scram-sha-256',
                         '--encoding=UTF8', '--locale=C',
                         '--pwfile=' + str(self.paths.database_password))
        cluster.chmod(0o700)
        password = self.paths.database_password.read_text().strip()
        private_write(self.paths.credentials / 'pgpass',
                      f'{self.socket_dir}:5432:*:wateragent:{password}\n')
        # Reapply a minimal closed policy rather than trusting copied config.
        private_write(cluster / 'pg_hba.conf', 'local all all scram-sha-256\n'
                      'host all all 0.0.0.0/0 reject\nhost all all ::0/0 reject\n')
        self._recover_owned_postmaster()
        private_write(self.paths.logs / 'postgres.log', '')
        self.command('pg_ctl', '-D', str(cluster), '-l', str(self.paths.logs / 'postgres.log'),
                     '-w', '-t', '25', '-o', "-c listen_addresses='' "
                     f"-c unix_socket_directories='{self.socket_dir}' "
                     '-c unix_socket_permissions=0700 -c password_encryption=scram-sha-256',
                     'start')
        self.started = True
        if not initialized:
            self.command('createdb', 'wateragent')

    def _recover_owned_postmaster(self) -> None:
        pidfile = self.paths.cluster / 'postmaster.pid'
        if not pidfile.exists():
            return
        lines = pidfile.read_text().splitlines()
        if len(lines) < 2 or not lines[0].isdigit() or Path(lines[1]).resolve() != self.paths.cluster:
            raise DesktopError('DESKTOP_CLUSTER_IDENTITY_FAILED')
        pid = int(lines[0])
        if not process_exists(pid):
            # PostgreSQL itself validates stale PID files when starting.
            return
        executable = self.paths.resources / 'postgres' / 'bin' / 'postgres'
        if not postmaster_matches(pid, executable, self.paths.cluster):
            raise DesktopError('DESKTOP_CLUSTER_IDENTITY_FAILED')
        self.command('pg_ctl', '-D', str(self.paths.cluster), '-w', '-t', '25',
                     '-m', 'fast', 'stop', cancellable=False)

    def stop(self) -> None:
        if self.started and (self.paths.cluster / 'postmaster.pid').exists():
            # Revalidate at shutdown as well: a crashed postmaster PID may have
            # been reused while this supervisor was still alive.
            self._recover_owned_postmaster()
            self.started = False
        if self.started:
            self.command('pg_ctl', '-D', str(self.paths.cluster), '-w', '-t', '25',
                         '-m', 'fast', 'stop', cancellable=False)
            self.started = False

    def close(self) -> None:
        if not self.started:
            self._recover_owned_postmaster()
        self.stop()
        shutil.rmtree(self.socket_dir, ignore_errors=True)


def emit(event: str, **fields: Any) -> None:
    payload = json.dumps({'event': event, **fields}, ensure_ascii=False)
    print(payload, flush=True)
    if _EVENT_LOG is not None:
        try:
            reject_symlinks(_EVENT_LOG)
            fd = os.open(_EVENT_LOG, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
            with os.fdopen(fd, 'a') as stream:
                stream.write(payload + '\n')
        except OSError:
            # A disk-full diagnostic must never turn successful cleanup into an error.
            pass


class Supervisor:
    def __init__(self, paths: DesktopPaths):
        self.paths = paths
        self.stopping = threading.Event()
        self.pg = Postgres(paths, cancel_event=self.stopping)
        self.child: subprocess.Popen[bytes] | None = None
        self.helper: subprocess.Popen[bytes] | None = None
        self.guardian: subprocess.Popen[bytes] | None = None
        self.commands: queue.Queue[dict[str, Any]] = queue.Queue()
        self.control_started = False
        self.ready_path = paths.root / 'temporary' / 'ready.json'

    def _input(self) -> None:
        while not self.stopping.is_set():
            line = sys.stdin.readline(65538)
            if not line:
                self.stopping.set()
                return
            try:
                command = parse_command(line)
            except ValueError:
                emit('error', code='DESKTOP_INVALID_COMMAND', message='控制命令无效')
                continue
            if command['command'] == 'stop':
                self.stopping.set()
                return
            self.commands.put(command)

    def _argv(self, command: str, *extra: str) -> list[str]:
        return [str(self.paths.resources / 'python' / 'bin' / 'python3.12'), '-I', '-B',
                str(self.paths.resources / 'runtime' / 'service.py'), command,
                '--resources', str(self.paths.resources), '--data-dir', str(self.paths.root),
                '--socket-dir', str(self.pg.socket_dir),
                '--parent-pid', str(os.getpid()), *extra]

    def helper_call(self, command: str, *extra: str) -> int:
        self.helper = subprocess.Popen(self._argv(command, *extra), env=self.pg.env,
                                       cwd=self.paths.root / 'work', stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 180
        while self.helper.poll() is None:
            if self.stopping.wait(0.1) or time.monotonic() > deadline:
                self._stop_child(self.helper)
                raise DesktopError('DESKTOP_START_CANCELLED')
        return self.helper.returncode

    @staticmethod
    def _stop_child(child: subprocess.Popen[bytes] | None) -> None:
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)

    def start_service(self) -> None:
        emit('stage', message='正在启动工作台')
        nonce = secrets.token_hex(24)
        self.ready_path.unlink(missing_ok=True)
        self.child = subprocess.Popen(
            self._argv('serve', '--ready-file', str(self.ready_path), '--nonce', nonce),
            env=self.pg.env, cwd=self.paths.root / 'work', stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 120
        while not self.stopping.is_set():
            if self.child.poll() is not None:
                raise DesktopError('DESKTOP_SERVICE_EXITED')
            url = read_ready(self.ready_path, nonce)
            if url:
                emit('ready', url=url)
                break
            if time.monotonic() > deadline:
                raise DesktopError('DESKTOP_READY_TIMEOUT')
            self.stopping.wait(0.1)

    def start_control(self) -> None:
        if self.control_started:
            return
        self.control_started = True
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, lambda *_: self.stopping.set())
        threading.Thread(target=self._input, daemon=True).start()
        self.guardian = subprocess.Popen(
            self._argv('guard'),
            env=self.pg.env, cwd=self.paths.root / 'work', stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def close(self) -> None:
        self.stopping.set()
        self._stop_child(self.helper)
        self._stop_child(self.child)
        self.ready_path.unlink(missing_ok=True)
        try:
            self.pg.close()
        finally:
            self._stop_child(self.guardian)

    def run(self) -> int:
        self.start_control()
        try:
            emit('stage', message='正在启动独立数据库')
            self.pg.start()
            if self.stopping.is_set():
                return 0
            emit('stage', message='正在检查和迁移数据库')
            if self.helper_call('migrate') != 0:
                raise DesktopError('DESKTOP_MIGRATION_FAILED')
            status = self.helper_call('admin-status')
            if status not in (0, 10):
                raise DesktopError('DESKTOP_ADMIN_CHECK_FAILED')
            if status == 10:
                emit('setup_required', username='admin', minimum_password_length=12)
                while not self.stopping.is_set():
                    try:
                        command = self.commands.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if command['command'] != 'setup':
                        continue
                    password = command.pop('password')
                    if len(password.rstrip('\n\r ')) < 12 or len(password) > 4096 or '\x00' in password:
                        emit('error', code='DESKTOP_PASSWORD_INVALID', message='密码至少需要 12 个字符')
                        continue
                    secret_path = self.paths.root / 'temporary' / 'initial-admin'
                    try:
                        private_write(secret_path, password)
                        del password
                        if self.helper_call('bootstrap', '--secret-file', str(secret_path)) != 0:
                            raise DesktopError('DESKTOP_BOOTSTRAP_FAILED')
                    finally:
                        secret_path.unlink(missing_ok=True)
                    break
            if self.stopping.is_set():
                return 0
            self.start_service()
            while not self.stopping.wait(0.2):
                if self.child is None or self.child.poll() is not None:
                    raise DesktopError('DESKTOP_SERVICE_EXITED')
                try:
                    command = self.commands.get_nowait()
                except queue.Empty:
                    continue
                if command['command'] == 'backup':
                    from backup import create_backup
                    try:
                        destination = Path(command['destination'])
                        emit('stage', message='正在备份，工作台将短暂停止')
                        self._stop_child(self.child)
                        try:
                            create_backup(self.paths, self.pg, destination)
                        finally:
                            if not self.stopping.is_set():
                                self.start_service()
                        emit('backup_complete', path=str(destination))
                    except Exception:  # noqa: BLE001 -- control boundary must never echo secrets
                        emit('error', code='DESKTOP_BACKUP_FAILED', message='备份失败；目标必须是新的绝对路径')
            return 0
        finally:
            self.close()


def main() -> int:
    global _EVENT_LOG
    parser = argparse.ArgumentParser(description='WaterAgent private desktop runtime')
    parser.add_argument('--resources', required=True, type=Path)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--restore', type=Path)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--restore-only', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    runtime: Supervisor | None = None
    try:
        paths = DesktopPaths.create(args.resources, args.data_dir)
        with InstanceLock(paths.root / 'instance.lock'):
            _EVENT_LOG = paths.logs / 'runtime-events.jsonl'
            if not _EVENT_LOG.exists():
                private_write(_EVENT_LOG, '')
            (paths.root / 'temporary' / 'initial-admin').unlink(missing_ok=True)
            from backup import recover_interrupted_restore, restore_backup
            runtime = Supervisor(paths)
            try:
                runtime.start_control()
                recover_interrupted_restore(paths)
                if args.restore:
                    emit('stage', message='正在验证和恢复备份')
                    restore_backup(paths, args.restore, force=args.force,
                                   cancel_event=runtime.stopping)
                    emit('stage', message='恢复完成，正在启动 App')
                    if args.restore_only:
                        return 0
                if runtime.stopping.is_set():
                    return 0
                return runtime.run()
            finally:
                runtime.close()
    except Exception as exc:  # noqa: BLE001 -- stable protocol error, no raw exceptions
        if (runtime is not None and runtime.stopping.is_set()
                and str(exc) == 'DESKTOP_START_CANCELLED'):
            return 0
        code = str(exc) if str(exc).startswith('DESKTOP_') else 'DESKTOP_START_FAILED'
        # Exception bodies may contain secrets: only our known stable codes cross protocol.
        if not code.replace('_', '').isalnum() or len(code) > 80:
            code = 'DESKTOP_START_FAILED'
        emit('error', code=code, message='本地工作台启动失败，请重试或查看本 App 日志')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
