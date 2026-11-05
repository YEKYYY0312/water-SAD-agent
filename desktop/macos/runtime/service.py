"""Bundle-only entry points into the unchanged production application."""
from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from launcher import isolated_environment
from paths import DesktopPaths, InstanceLock, private_write
from process_identity import postmaster_matches


def admin_status() -> int:
    from water_agent.webapp.auth.storage import configure_database_stores
    from water_agent.webapp.auth.user_store import UserNotFoundError, get_user_store

    database = configure_database_stores()
    if database is None:
        return 1
    try:
        store = get_user_store()
        if store.has_admin():
            return 0
        try:
            store.get_by_username('admin')
        except UserNotFoundError:
            return 10
        # Existing inactive admin or name collision: never reset/bootstrap it.
        return 0
    finally:
        database.dispose()


def ensure_database() -> None:
    """Repair an interrupted first createdb; never alter an existing database."""
    import psycopg

    with psycopg.connect(dbname='postgres', autocommit=True) as connection:
        exists = connection.execute(
            'SELECT 1 FROM pg_database WHERE datname = %s', ('wateragent',)).fetchone()
        if exists is None:
            connection.execute('CREATE DATABASE wateragent')


def migrate(resources: Path) -> int:
    from alembic.config import Config

    from alembic import command
    from water_agent.storage.database import Database

    ensure_database()
    config = Config(str(resources / 'backend' / 'alembic.ini'))
    config.set_main_option('script_location', str(resources / 'backend' / 'alembic'))
    config.set_main_option('prepend_sys_path', str(resources / 'backend' / 'src'))
    command.upgrade(config, 'head')
    database = Database.from_environment()
    try:
        if not database.ready():
            return 1
    finally:
        database.dispose()
    return 0


def _probe(port: int) -> bool:
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        connection.request('GET', '/health/ready')
        response = connection.getresponse()
        body = json.loads(response.read())
        return response.status == 200 and body['data']['status'] == 'ready'
    except (OSError, ValueError, KeyError):
        return False
    finally:
        connection.close()


async def serve(paths: DesktopPaths, ready_file: Path, nonce: str, *, expected_parent_pid: int) -> int:
    if expected_parent_pid <= 1 or os.getppid() != expected_parent_pid:
        return 1
    import uvicorn

    from water_agent.storage.database import Database
    from water_agent.webapp.app import create_app

    database = Database.from_environment()
    database.require_ready()
    if os.getppid() != expected_parent_pid:
        database.dispose()
        return 1
    app = create_app(database=database,
                     static_dir=paths.resources / 'backend' / 'web' / 'app' / 'dist',
                     trace_dir=paths.root / 'traces', session_dir=paths.root / 'sessions',
                     upload_dir=paths.uploads, export_dir=paths.exports)
    if os.getppid() != expected_parent_pid:
        database.dispose()
        return 1
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                          log_config=None, access_log=False,
                                          timeout_graceful_shutdown=15))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        deadline = time.monotonic() + 45
        while not server.started:
            if os.getppid() != expected_parent_pid:
                return 1
            if task.done():
                await task
                return 1
            if time.monotonic() > deadline:
                return 1
            await asyncio.sleep(0.05)
        if (os.getppid() != expected_parent_pid
                or not await asyncio.to_thread(_probe, port)
                or os.getppid() != expected_parent_pid):
            return 1
        private_write(ready_file, json.dumps({'nonce': nonce, 'url': f'http://127.0.0.1:{port}'}))
        while not task.done():
            if os.getppid() != expected_parent_pid:
                server.should_exit = True
            await asyncio.sleep(0.2)
        await task
        return 0
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=20)
        except TimeoutError:
            task.cancel()
        listener.close()
        database.dispose()
        ready_file.unlink(missing_ok=True)


def guard(paths: DesktopPaths, parent_pid: int) -> int:
    """On supervisor SIGKILL stop only a verified postmaster in its private cluster."""
    while os.getppid() == parent_pid:
        time.sleep(0.2)
    try:
        with InstanceLock(paths.root / 'instance.lock'):
            from backup import recover_interrupted_restore
            recover_interrupted_restore(paths)
            (paths.root / 'temporary' / 'initial-admin').unlink(missing_ok=True)
            return _guard_cleanup(paths)
    except RuntimeError:
        # A newer supervisor acquired the lock and owns recovery now.
        return 0


def _guard_cleanup(paths: DesktopPaths) -> int:
    # initdb/pg_ctl may be finishing concurrently when their parent disappears.
    for _ in range(100):
        pidfile = paths.cluster / 'postmaster.pid'
        if pidfile.is_file():
            lines = pidfile.read_text().splitlines()
            if len(lines) >= 2 and lines[0].isdigit() and Path(lines[1]).resolve() == paths.cluster:
                executable = paths.resources / 'postgres' / 'bin' / 'postgres'
                if postmaster_matches(int(lines[0]), executable, paths.cluster):
                    subprocess.run([str(paths.resources / 'postgres' / 'bin' / 'pg_ctl'),
                                    '-D', str(paths.cluster), '-w', '-t', '20', '-m', 'fast', 'stop'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25, check=False)
            return 0
        time.sleep(0.2)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['migrate', 'admin-status', 'bootstrap', 'serve', 'guard'])
    for flag in ('resources', 'data-dir', 'socket-dir'):
        parser.add_argument('--' + flag, required=True, type=Path)
    parser.add_argument('--secret-file', type=Path)
    parser.add_argument('--ready-file', type=Path)
    parser.add_argument('--nonce')
    parser.add_argument('--parent-pid', type=int, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command != 'guard' and (args.parent_pid <= 1 or os.getppid() != args.parent_pid):
        return 1
    paths = DesktopPaths.create(args.resources, args.data_dir)
    os.environ.clear()
    os.environ.update(isolated_environment(paths, args.socket_dir))
    sys.path.insert(0, str(paths.resources / 'backend' / 'src'))
    # Do not let third-party loggers print raw request/driver exceptions. This
    # child only reports an exit code; supervisor logs a fixed stage/result.
    import logging
    logging.disable(logging.CRITICAL)
    try:
        if args.command == 'guard':
            return guard(paths, args.parent_pid)
        if args.command == 'migrate':
            return migrate(paths.resources)
        if args.command == 'admin-status':
            return admin_status()
        if args.command == 'bootstrap':
            from water_agent.webapp.auth.bootstrap import run_bootstrap
            return int(run_bootstrap(secret_path=args.secret_file))
        if args.ready_file is None or args.nonce is None:
            return 1
        return asyncio.run(serve(paths, args.ready_file, args.nonce,
                                 expected_parent_pid=args.parent_pid))
    except Exception as exc:  # noqa: BLE001 -- never expose driver or submitted secret values
        private_write(paths.logs / 'service-error.log',
                      f'command={args.command} error_type={type(exc).__name__}\n')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
