"""Desktop process boundaries; no developer database or network is used."""
from __future__ import annotations

from pathlib import Path

import pytest

from desktop.macos.runtime.launcher import Postgres, isolated_environment, parse_command, read_ready
from desktop.macos.runtime.paths import DesktopPaths, InstanceLock, private_write


def complete_paths(tmp_path):
    resources = tmp_path / 'Resources'
    index = resources / 'backend/data/indexes/release/clauses.db'
    index.parent.mkdir(parents=True)
    index.write_bytes(b'fake primary index')
    return DesktopPaths.create(resources, tmp_path / 'Data')


def test_private_paths_reject_relative_symlinks_and_resource_overlap(tmp_path):
    resources = tmp_path / 'Resources'
    resources.mkdir()
    with pytest.raises(ValueError):
        DesktopPaths.create(resources, Path('relative'))
    alias = tmp_path / 'alias'
    alias.symlink_to(resources, target_is_directory=True)
    with pytest.raises(ValueError):
        DesktopPaths.create(resources, alias / 'data')
    with pytest.raises(ValueError):
        DesktopPaths.create(resources, resources / 'data')


def test_private_permissions_and_single_instance(tmp_path):
    paths = DesktopPaths.create(tmp_path / 'Resources', tmp_path / 'App Data')
    for directory in (paths.root, paths.credentials, paths.logs, paths.uploads):
        assert directory.stat().st_mode & 0o777 == 0o700
    private_write(paths.credentials / 'test', 'secret')
    assert (paths.credentials / 'test').stat().st_mode & 0o777 == 0o600
    with (InstanceLock(paths.root / 'instance.lock'),
          pytest.raises(RuntimeError, match='DESKTOP_ALREADY_RUNNING'),
          InstanceLock(paths.root / 'instance.lock')):
        pass
    with InstanceLock(paths.root / 'instance.lock'):
        pass


def test_environment_does_not_inherit_production_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'production')
    monkeypatch.setenv('OPENAI_API_KEY', 'secret')
    monkeypatch.setenv('PYTHONPATH', '/developer/source')
    monkeypatch.setenv('WATER_AGENT_MCP_ENABLED', 'true')
    paths = complete_paths(tmp_path)
    env = isolated_environment(paths, tmp_path / 'socket')
    assert 'OPENAI_API_KEY' not in env
    assert 'PYTHONPATH' not in env
    assert env['DATABASE_URL'].startswith('postgresql+psycopg://wateragent@/wateragent?host=')
    assert env['WATER_AGENT_CREDENTIALS_DIR'] == str(paths.credentials / 'models')
    assert env.get('WATER_AGENT_MCP_ENABLED') != 'true'
    assert env['PATH'].endswith('/usr/bin:/bin:/usr/sbin:/sbin')


def test_desktop_embedding_adapter_uses_production_endpoint_resolution(tmp_path, monkeypatch):
    import json

    from water_agent.generation.endpoints import resolve_chat_endpoint, resolve_embedding_endpoint

    paths = complete_paths(tmp_path)
    private_write(paths.root / 'config/embedding.json', json.dumps({
        'base_url': 'https://llm-proxy.learnings.ai/v1',
        'model': 'text-embedding-3-large', 'retrieval_mode': 'hybrid',
    }))
    monkeypatch.setenv('WATER_AGENT_LLM_BASE_URL', 'https://host.invalid/v1')
    monkeypatch.setenv('WATER_AGENT_EMBEDDING_BASE_URL', 'https://host.invalid/v1')
    monkeypatch.setenv('WEB_MOCK_GENERATOR', 'true')
    env = isolated_environment(paths, tmp_path / 'socket')
    monkeypatch.setenv('WATER_AGENT_CREDENTIALS_DIR', env['WATER_AGENT_CREDENTIALS_DIR'])
    private_write(paths.credentials / 'models/llm_endpoint.json', json.dumps({
        'base_url': 'https://llm-proxy.learnings.ai/v1', 'model': 'deepseek-v4.1-flash',
    }))
    embedding = resolve_embedding_endpoint(getenv=env.get)
    chat = resolve_chat_endpoint(getenv=env.get)
    assert embedding.base_url == chat.base_url == 'https://llm-proxy.learnings.ai/v1'
    assert embedding.model == 'text-embedding-3-large'
    assert chat.source == 'panel'
    assert chat.model == 'deepseek-v4.1-flash'
    assert env['WATER_AGENT_RETRIEVAL_MODE'] == 'hybrid'
    assert 'WATER_AGENT_LLM_BASE_URL' not in env
    assert 'WEB_MOCK_GENERATOR' not in env
    assert 'WATER_AGENT_EMBEDDING_API_KEY' not in env


@pytest.mark.parametrize('content', [
    '[]', '{bad', '{}',
    '{"base_url":"https://example.com","model":"x","api_key":"secret"}',
    '{"base_url":4,"model":"x","retrieval_mode":"hybrid"}',
    '{"base_url":"https://example.com","model":"x","retrieval_mode":"fixture"}',
])
def test_invalid_desktop_embedding_config_fails_without_echo(tmp_path, content):
    paths = complete_paths(tmp_path)
    private_write(paths.root / 'config/embedding.json', content)
    with pytest.raises(RuntimeError, match='^DESKTOP_EMBEDDING_CONFIG_INVALID$'):
        isolated_environment(paths, tmp_path / 'socket')


def test_desktop_embedding_config_rejects_symlink(tmp_path):
    paths = complete_paths(tmp_path)
    source = tmp_path / 'external.json'
    source.write_text('{}')
    (paths.root / 'config/embedding.json').symlink_to(source)
    with pytest.raises(ValueError, match='DESKTOP_UNSAFE_PATH'):
        isolated_environment(paths, tmp_path / 'socket')


def test_control_and_ready_reject_wrong_origin_stale_nonce_and_secret_echo(tmp_path):
    assert parse_command('{"command":"stop"}') == {'command': 'stop'}
    for invalid in ('[]', '{"command":"shell"}', '{"command":"setup","password":5}'):
        with pytest.raises(ValueError):
            parse_command(invalid)
    target = tmp_path / 'ready.json'
    private_write(target, '{"nonce":"old","url":"http://127.0.0.1:1234"}')
    assert read_ready(target, 'new') is None
    private_write(target, '{"nonce":"new","url":"http://evil:1234"}')
    assert read_ready(target, 'new') is None
    private_write(target, '{"nonce":"new","url":"http://127.0.0.1:1234"}')
    assert read_ready(target, 'new') == 'http://127.0.0.1:1234'


def test_postgres_uses_scram_private_socket_and_no_password_arguments(tmp_path):
    paths = complete_paths(tmp_path)
    commands = []

    def run(argv, **kwargs):
        commands.append((argv, kwargs))
        if argv[0].endswith('/initdb'):
            paths.cluster.mkdir()
            (paths.cluster / 'PG_VERSION').write_text('18')

    pg = Postgres(paths, run=run)
    try:
        pg.start()
        hba = (paths.cluster / 'pg_hba.conf').read_text()
        assert 'scram-sha-256' in hba
        assert 'trust' not in hba
        args = ' '.join(' '.join(argv) for argv, _ in commands)
        password = paths.database_password.read_text().strip()
        assert password not in args
        assert "listen_addresses=''" in args
        assert '-D' in args
        assert pg.socket_dir.stat().st_mode & 0o777 == 0o700
        pg.stop()
        assert commands[-1][0][0].endswith('/pg_ctl')
        assert 'stop' in commands[-1][0]
        assert all('pkill' not in argv and 'killall' not in argv for argv, _ in commands)
    finally:
        pg.close()


def test_never_reinitializes_existing_or_partial_cluster(tmp_path):
    paths = complete_paths(tmp_path)
    paths.cluster.mkdir()
    (paths.cluster / 'partial').write_text('do not erase')
    calls = []
    pg = Postgres(paths, run=lambda *a, **kw: calls.append(a))
    try:
        with pytest.raises(RuntimeError, match='DESKTOP_CLUSTER_INCOMPLETE'):
            pg.start()
        assert not calls
        assert (paths.cluster / 'partial').read_text() == 'do not erase'
    finally:
        pg.close()


def test_private_write_refuses_hardlinked_existing_files_without_truncation(tmp_path):
    original = tmp_path / 'original'
    original.write_text('preserve')
    linked = tmp_path / 'linked'
    linked.hardlink_to(original)
    with pytest.raises(ValueError, match='DESKTOP_UNSAFE_PATH'):
        private_write(linked, 'overwrite')
    assert original.read_text() == 'preserve'


def test_database_creation_recovers_interruption_and_preserves_existing_database(monkeypatch):
    import psycopg

    from desktop.macos.runtime import service

    statements = []
    connect_options = []

    class Connection:
        exists = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, statement, params=None):
            statements.append((statement, params))
            if statement.startswith('CREATE'):
                self.exists = True
            return self

        def fetchone(self):
            return (1,) if self.exists else None

    connection = Connection()

    def connect(**kwargs):
        connect_options.append(kwargs)
        return connection

    monkeypatch.setattr(psycopg, 'connect', connect)
    service.ensure_database()
    service.ensure_database()
    assert [s for s, _ in statements].count('CREATE DATABASE wateragent') == 1
    assert statements[0][1] == ('wateragent',)
    assert all(options == {'dbname': 'postgres', 'autocommit': True} for options in connect_options)


def test_guardian_does_not_stop_cluster_owned_by_a_new_supervisor(tmp_path, monkeypatch):
    from desktop.macos.runtime import service

    paths = complete_paths(tmp_path)
    monkeypatch.setattr(service.os, 'getppid', lambda: 1)
    monkeypatch.setattr(service, '_guard_cleanup', lambda _: pytest.fail('new owner must be preserved'))
    with InstanceLock(paths.root / 'instance.lock'):
        assert service.guard(paths, 999999) == 0


def test_nested_release_index_is_explicitly_selected(tmp_path):
    from desktop.macos.runtime.launcher import select_bundle_index

    resources = tmp_path / 'Resources'
    index = resources / 'backend/data/indexes/release-name/clauses.db'
    index.parent.mkdir(parents=True)
    index.write_bytes(b'fake primary index')
    assert select_bundle_index(resources) == index
    paths = DesktopPaths.create(resources, tmp_path / 'Data')
    assert isolated_environment(paths, tmp_path / 'socket')['WATER_AGENT_INDEX_PATH'] == str(index)


@pytest.mark.parametrize('count,code', [(0, 'DESKTOP_INDEX_MISSING'),
                                       (2, 'DESKTOP_INDEX_AMBIGUOUS')])
def test_index_selection_rejects_missing_or_ambiguous_release(tmp_path, count, code):
    from desktop.macos.runtime.launcher import select_bundle_index

    resources = tmp_path / 'Resources'
    for number in range(count):
        index = resources / f'backend/data/indexes/release-{number}/clauses.db'
        index.parent.mkdir(parents=True)
        index.write_bytes(b'fake primary index')
    with pytest.raises(RuntimeError, match=code):
        select_bundle_index(resources)


def test_cancelled_postgres_command_terminates_only_its_child(tmp_path):
    import os
    import sys
    import threading

    paths = complete_paths(tmp_path)
    cancel = threading.Event()
    pg = Postgres(paths, cancel_event=cancel)
    pidfile = tmp_path / 'owned-command.pid'
    script = 'import os,time;from pathlib import Path;Path(__import__("sys").argv[1]).write_text(str(os.getpid()));time.sleep(60)'
    timer = threading.Timer(0.2, cancel.set)
    timer.start()
    try:
        with pytest.raises(RuntimeError, match='DESKTOP_START_CANCELLED'):
            pg._run([sys.executable, '-B', '-c', script, str(pidfile)])
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
    finally:
        timer.cancel()
        pg.close()


def test_process_identity_requires_exact_cluster_with_unicode_and_spaces(tmp_path, monkeypatch):
    from desktop.macos.runtime import process_identity

    executable = tmp_path / 'Resources/给排水 App/postgres/bin/postgres'
    cluster = tmp_path / '用户 数据/postgres'
    other = tmp_path / '其他 数据/postgres'
    monkeypatch.setattr(process_identity, 'read_process_identity', lambda _: process_identity.ProcessIdentity(
        executable, (str(executable), '-D', str(other))))
    assert not process_identity.postmaster_matches(12345, executable, cluster)
    monkeypatch.setattr(process_identity, 'read_process_identity', lambda _: process_identity.ProcessIdentity(
        executable, (str(executable), '-D', str(cluster))))
    assert process_identity.postmaster_matches(12345, executable, cluster)
    monkeypatch.setattr(process_identity, 'read_process_identity', lambda _: None)
    assert not process_identity.postmaster_matches(12345, executable, cluster)


def test_recovery_does_not_stop_same_executable_for_another_cluster(tmp_path, monkeypatch):
    from desktop.macos.runtime import launcher

    paths = complete_paths(tmp_path)
    paths.cluster.mkdir()
    (paths.cluster / 'postmaster.pid').write_text(f'12345\n{paths.cluster}\n')
    calls = []
    pg = Postgres(paths, run=lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(launcher, 'postmaster_matches', lambda *_: False)
    monkeypatch.setattr(launcher, 'process_exists', lambda _: True)
    with pytest.raises(RuntimeError, match='DESKTOP_CLUSTER_IDENTITY_FAILED'):
        pg._recover_owned_postmaster()
    assert not calls
    (paths.cluster / 'postmaster.pid').unlink()
    pg.close()


def test_adopted_service_returns_before_importing_application(tmp_path, monkeypatch):
    import asyncio

    from desktop.macos.runtime import service

    paths = complete_paths(tmp_path)
    monkeypatch.setattr(service.os, 'getppid', lambda: 1)
    ready = tmp_path / 'ready.json'
    assert asyncio.run(service.serve(paths, ready, 'nonce', expected_parent_pid=999999)) == 1
    assert not ready.exists()


def test_stale_pid_does_not_target_an_unrelated_process(tmp_path, monkeypatch):
    from desktop.macos.runtime import launcher

    paths = complete_paths(tmp_path)
    paths.cluster.mkdir()
    (paths.cluster / 'postmaster.pid').write_text(f'12345\n{paths.cluster}\n')
    calls = []
    pg = Postgres(paths, run=lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(launcher, 'process_exists', lambda _: False)
    pg._recover_owned_postmaster()
    assert not calls
    (paths.cluster / 'postmaster.pid').unlink()
    pg.close()


def test_guardian_rejects_same_postgres_binary_for_another_cluster(tmp_path, monkeypatch):
    from desktop.macos.runtime import service

    paths = complete_paths(tmp_path)
    paths.cluster.mkdir()
    (paths.cluster / 'postmaster.pid').write_text(f'12345\n{paths.cluster}\n')
    monkeypatch.setattr(service, 'postmaster_matches', lambda *_: False)
    monkeypatch.setattr(service.subprocess, 'run', lambda *args, **kwargs: pytest.fail('foreign PG'))
    assert service._guard_cleanup(paths) == 0


def test_macos_argument_decoder_keeps_unicode_whitespace_boundaries(tmp_path):
    import sys

    from desktop.macos.runtime.process_identity import _decode_macos_procargs

    executable = str(tmp_path / '给排水 App/bin/postgres')
    cluster = str(tmp_path / '用户 数据/postgres')
    arguments = (executable, '-D', cluster, '-c', "listen_addresses=")
    payload = len(arguments).to_bytes(4, sys.byteorder, signed=True)
    payload += executable.encode() + b'\0\0\0'
    payload += b'\0'.join(value.encode() for value in arguments) + b'\0'
    identity = _decode_macos_procargs(payload)
    assert identity is not None
    assert identity.executable == Path(executable)
    assert identity.argv == arguments


def test_service_detects_parent_adoption_during_database_startup(tmp_path, monkeypatch):
    import asyncio
    import sys
    from types import SimpleNamespace

    from desktop.macos.runtime import service
    from water_agent.storage.database import Database

    paths = complete_paths(tmp_path)
    state = {'parent': 999999, 'disposed': False}

    class FakeDatabase:
        def require_ready(self):
            state['parent'] = 1

        def dispose(self):
            state['disposed'] = True

    monkeypatch.setattr(service.os, 'getppid', lambda: state['parent'])
    monkeypatch.setattr(Database, 'from_environment', lambda: FakeDatabase())
    monkeypatch.setitem(sys.modules, 'water_agent.webapp.app', SimpleNamespace(
        create_app=lambda **kwargs: pytest.fail('adopted service must not construct HTTP app')))
    ready = tmp_path / 'ready.json'
    assert asyncio.run(service.serve(paths, ready, 'nonce', expected_parent_pid=999999)) == 1
    assert state['disposed']
    assert not ready.exists()


def test_all_service_helpers_receive_explicit_original_supervisor_pid(tmp_path):
    import os

    from desktop.macos.runtime.launcher import Supervisor

    runtime = Supervisor(complete_paths(tmp_path))
    try:
        for command in ('migrate', 'admin-status', 'bootstrap', 'serve', 'guard'):
            argv = runtime._argv(command)
            assert argv.count('--parent-pid') == 1
            assert argv[argv.index('--parent-pid') + 1] == str(os.getpid())
    finally:
        runtime.close()
