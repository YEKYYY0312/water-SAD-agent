from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from desktop.macos.runtime.backup import create_backup, restore_backup, validate_archive
from desktop.macos.runtime.paths import DesktopPaths


class DumpPostgres:
    def command(self, binary, *args, **kwargs):
        assert binary == 'pg_dump'
        destination = Path(args[args.index('--file') + 1])
        destination.write_bytes(b'PGDMP sample')


def test_backup_only_allowlisted_files_with_hashes_and_private_output(tmp_path):
    paths = DesktopPaths.create(tmp_path / 'Resources', tmp_path / 'Data')
    (paths.uploads / '中文.docx').write_bytes(b'document')
    (paths.credentials / 'API_KEY').write_text('private-key')
    (paths.root / 'traces' / 'trace').write_text('private-trace')
    (paths.root / 'config' / 'mcp_web.json').write_text('{"version":1}')
    destination = tmp_path / 'backup.tar.gz'
    create_backup(paths, DumpPostgres(), destination)
    assert destination.stat().st_mode & 0o777 == 0o600
    manifest = validate_archive(destination)
    assert set(manifest['files']) == {'database.dump', 'uploads/中文.docx', 'config/mcp_web.json'}
    assert manifest['files']['uploads/中文.docx']['sha256'] == hashlib.sha256(b'document').hexdigest()
    with pytest.raises((ValueError, FileExistsError)):
        create_backup(paths, DumpPostgres(), destination)


def malicious_archive(path, name, *, symlink=False, digest='bad'):
    with tarfile.open(path, 'w:gz') as archive:
        payload = b'evil'
        manifest = json.dumps({'version': 1, 'files': {name: {'size': 4, 'sha256': digest}}}).encode()
        info = tarfile.TarInfo('manifest.json')
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        if symlink:
            info.type = tarfile.SYMTYPE
            info.linkname = '/outside'
        archive.addfile(info, io.BytesIO(payload))


@pytest.mark.parametrize('name,symlink', [('../outside', False), ('/outside', False),
                                         ('uploads/link', True), ('credentials/API_KEY', False),
                                         ('uploads/valid', False)])
def test_restore_rejects_traversal_links_credentials_and_corruption(tmp_path, name, symlink):
    archive = tmp_path / 'bad.tar.gz'
    malicious_archive(archive, name, symlink=symlink)
    with pytest.raises(ValueError):
        validate_archive(archive)


def test_restore_requires_explicit_force_before_touching_existing_data(tmp_path):
    paths = DesktopPaths.create(tmp_path / 'Resources', tmp_path / 'Data')
    (paths.uploads / 'keep').write_text('keep')
    with pytest.raises(ValueError, match='DESKTOP_RESTORE_NEEDS_FORCE'):
        restore_backup(paths, tmp_path / 'missing.tar.gz')
    assert (paths.uploads / 'keep').read_text() == 'keep'


def test_backup_rejects_symlinked_user_files(tmp_path):
    paths = DesktopPaths.create(tmp_path / 'Resources', tmp_path / 'Data')
    secret = tmp_path / 'private'
    secret.write_text('secret')
    (paths.uploads / 'escape').symlink_to(secret)
    with pytest.raises(ValueError):
        create_backup(paths, DumpPostgres(), tmp_path / 'output.tar.gz')
    assert not (tmp_path / 'output.tar.gz').exists()


def test_restore_hardkill_journal_recovers_previous_data(tmp_path):
    import subprocess
    import sys

    from desktop.macos.runtime.backup import recover_interrupted_restore

    paths = DesktopPaths.create(tmp_path / 'Resources', tmp_path / 'Data')
    paths.cluster.mkdir()
    (paths.cluster / 'keep').write_text('previous database')
    (paths.uploads / 'keep').write_text('previous upload')
    paths.database_password.write_text('previous database credential')
    stage = DesktopPaths.create(tmp_path / 'Resources', tmp_path / 'Stage')
    stage.cluster.mkdir()
    (stage.cluster / 'keep').write_text('incoming database')
    stage.database_password.write_text('incoming credential')
    runtime = Path(__file__).resolve().parents[1] / 'desktop/macos/runtime'
    script = '''
import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from paths import DesktopPaths
from backup import commit_restore
paths = DesktopPaths(Path(sys.argv[2]), Path(sys.argv[3]))
stage = DesktopPaths(Path(sys.argv[2]), Path(sys.argv[4]))
calls = 0
def cancel():
    global calls
    calls += 1
    if calls == 2:
        os.kill(os.getpid(), signal.SIGKILL)
    return False
commit_restore(paths, stage, cancel_check=cancel)
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, str(runtime),
                             str(paths.resources), str(paths.root), str(stage.root)], check=False)
    assert result.returncode < 0
    assert (paths.cluster / 'keep').read_text() == 'incoming database'
    recover_interrupted_restore(paths)
    assert (paths.cluster / 'keep').read_text() == 'previous database'
    assert (paths.uploads / 'keep').read_text() == 'previous upload'
    assert paths.database_password.read_text() == 'previous database credential'
    assert not (paths.root / 'temporary/restore-journal.json').exists()


def test_soft_cancel_during_restore_switch_rolls_back_existing_data(tmp_path):
    from desktop.macos.runtime.backup import commit_restore

    paths = DesktopPaths.create(tmp_path / 'Resources', tmp_path / 'Data')
    stage = DesktopPaths.create(tmp_path / 'Resources', tmp_path / 'Stage')
    for item, text in [(paths, 'old'), (stage, 'incoming')]:
        item.cluster.mkdir()
        (item.cluster / 'identity').write_text(text)
        (item.uploads / 'identity').write_text(text)
        item.database_password.write_text(text)
    checks = []

    def cancel():
        checks.append(True)
        return len(checks) == 2

    with pytest.raises(RuntimeError, match='DESKTOP_START_CANCELLED'):
        commit_restore(paths, stage, cancel_check=cancel)
    assert (paths.cluster / 'identity').read_text() == 'old'
    assert (paths.uploads / 'identity').read_text() == 'old'
    assert paths.database_password.read_text() == 'old'
    assert not (paths.root / 'temporary/restore-journal.json').exists()


def test_archive_validation_obeys_restore_cancellation_before_writes(tmp_path):
    paths = DesktopPaths.create(tmp_path / 'Resources', tmp_path / 'Data')
    archive = tmp_path / 'backup.tar.gz'
    create_backup(paths, DumpPostgres(), archive)
    with pytest.raises(RuntimeError, match='DESKTOP_START_CANCELLED'):
        validate_archive(archive, extract_to=tmp_path / 'Payload', cancel_check=lambda: True)
    assert not (tmp_path / 'Payload').exists()
