"""Desktop SSO issues ordinary sessions through private IPC, never an HTTP bypass."""
import json

import pytest
from sqlalchemy import create_engine

from desktop.macos.runtime.local_session import issue_local_session
from desktop.macos.runtime.paths import DesktopPaths
from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.webapp.auth import session_store, storage, user_store
from water_agent.webapp.auth.db_store import PostgresSessionStore, PostgresUserStore
from water_agent.webapp.auth.models import UserRole


@pytest.fixture
def owner(tmp_path, monkeypatch):
    db = Database(engine=create_engine(f"sqlite:///{tmp_path / 'owner.sqlite'}"))
    Base.metadata.create_all(db.engine)
    users = PostgresUserStore(db)
    sessions = PostgresSessionStore(db)
    user = users.create_user('admin', 'FixtureOnly2026!', UserRole.ADMIN)
    monkeypatch.setattr(storage, 'configure_database_stores', lambda: db)
    monkeypatch.setattr(user_store, 'get_user_store', lambda: users)
    monkeypatch.setattr(session_store, 'get_session_store', lambda: sessions)
    return db, users, sessions, user


def test_private_session_preserves_existing_owner_and_password(tmp_path, owner, capsys):
    _, users, sessions, user = owner
    paths = DesktopPaths.create(tmp_path/'Resources', tmp_path/'Private')
    original = user.password_hash
    output = issue_local_session(paths)
    assert output == paths.credentials/'desktop-session.json'
    assert output.stat().st_mode & 0o777 == 0o600
    data = json.loads(output.read_text())
    assert sessions.get(data['session_id']).user_id == user.user_id
    assert data['csrf_token'] and data['expires_at'] > 0
    assert users.get_by_id(user.user_id).password_hash == original
    assert data['session_id'] not in capsys.readouterr().out


def test_local_session_does_not_create_or_elevate_an_unknown_account(tmp_path, owner):
    _, users, _, _ = owner
    paths = DesktopPaths.create(tmp_path/'Resources', tmp_path/'Private')
    users.get_by_username = lambda _: (_ for _ in ()).throw(user_store.UserNotFoundError('missing'))
    with pytest.raises(ValueError, match='OWNER_UNAVAILABLE'):
        issue_local_session(paths)
    assert not (paths.credentials/'desktop-session.json').exists()


def test_forced_password_change_not_silently_overridden(tmp_path, owner):
    _, users, _, user = owner
    users.set_must_change_password(user.user_id, True)
    paths = DesktopPaths.create(tmp_path/'Resources', tmp_path/'Private')
    with pytest.raises(ValueError, match='OWNER_UNAVAILABLE'):
        issue_local_session(paths)

@pytest.mark.parametrize('active,role', [(False, UserRole.ADMIN), (True, UserRole.MEMBER)])
def test_inactive_or_nonadmin_local_owner_is_rejected(tmp_path, owner, active, role):
    from types import SimpleNamespace
    _, users, _, _ = owner
    users.get_by_username = lambda _: SimpleNamespace(is_active=active, role=role, must_change_password=False)
    paths = DesktopPaths.create(tmp_path/'Resources', tmp_path/'Private')
    with pytest.raises(ValueError, match='OWNER_UNAVAILABLE'):
        issue_local_session(paths)
