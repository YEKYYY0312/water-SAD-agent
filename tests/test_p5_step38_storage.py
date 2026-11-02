"""P5 Step 38 persistence contracts.

SQLite is used only as a deterministic SQLAlchemy substitute for repository
and transaction tests.  PostgreSQL-specific checkpoint coverage is skipped
when no reachable test database is configured; CI enables it with
``P5_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text

from water_agent.storage.database import (
    CHECKPOINT_MIGRATION_HEAD,
    MIGRATION_HEAD,
    Database,
    DatabasePasswordFileError,
    database_url_from_environment,
)
from water_agent.storage.models import Base
from water_agent.storage.repository import (
    ProjectRepository,
    ResourceNotFoundError,
    RunRepository,
    WebSessionRepository,
)
from water_agent.webapp.app import create_app


def _database(tmp_path: Path) -> Database:
    engine = create_engine(f"sqlite:///{tmp_path / 'step38.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return Database(engine=engine)


def _postgres_database() -> Database:
    url = os.getenv("P5_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "P5_TEST_DATABASE_URL is not configured; PostgreSQL integration is environment-dependent"
        )
    database = Database(url=url)
    database.require_ready()
    return database


def test_alembic_baseline_declares_business_and_checkpoint_tables() -> None:
    revision = Path("alembic/versions/0001_step38.py").read_text(encoding="utf-8")
    for table in (
        "users",
        "auth_sessions",
        "projects",
        "project_memberships",
        "web_sessions",
        "runs",
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
    ):
        assert f'"{table}"' in revision
    assert "INSERT INTO checkpoint_migrations (v) VALUES (9)" in revision
    assert "def downgrade()" in revision


def test_runtime_image_includes_alembic_migration_assets() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    excluded = Path(".dockerignore").read_text(encoding="utf-8")
    assert "COPY alembic.ini ./" in dockerfile
    assert "COPY alembic ./alembic" in dockerfile
    assert "COPY --from=python-build /app/alembic.ini /app/alembic.ini" in dockerfile
    assert "COPY --from=python-build /app/alembic /app/alembic" in dockerfile
    assert "!alembic.ini" in excluded
    assert "!alembic/**" in excluded


def test_database_settings_repr_does_not_expose_credentials() -> None:
    database = Database(url="postgresql+psycopg://user:secret-value@localhost/database")
    assert "secret-value" not in repr(database.settings)
    database.dispose()


def test_alembic_checkpoint_head_matches_installed_langgraph_saver() -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    assert CHECKPOINT_MIGRATION_HEAD == len(PostgresSaver.MIGRATIONS) - 1


def test_alembic_migration_executes_against_isolated_sqlite(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "migration.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    result = subprocess.run(
        [sys.executable, "-m", "water_agent.webapp.lifecycle", "migrate"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    engine = create_engine(f"sqlite:///{db_path}")
    tables = set(inspect(engine).get_table_names())
    assert {"alembic_version", "users", "web_sessions", "checkpoints"} <= tables
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == MIGRATION_HEAD
    assert Database(url=f"sqlite:///{db_path}").ready()


def test_migration_failure_is_nonzero_and_ready_is_not_ready(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://water_agent@127.0.0.1:1/water_agent")
    result = subprocess.run(
        [sys.executable, "-m", "water_agent.webapp.lifecycle", "migrate"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "password" not in result.stderr.lower()
    client = TestClient(create_app(static_dir=tmp_path), raise_server_exceptions=False)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["data"]["status"] == "not_ready"
    assert response.json()["data"]["checks"]["database"] == "unavailable"


def test_unusable_password_file_fails_loudly_instead_of_connecting_without_it(
    tmp_path: Path,
) -> None:
    # CI 2026-09-29: a 0600 runner-owned secret was unreadable for uid 10001;
    # the old reader returned None and migrate connected password-less.
    url = "postgresql+psycopg://water_agent@postgres:5432/water_agent"
    empty = tmp_path / "empty"
    empty.write_text("  \n", encoding="utf-8")
    unreadable = tmp_path / "a-directory"
    unreadable.mkdir()
    for path, reason in ((empty, "empty"), (unreadable, "not readable")):
        with pytest.raises(DatabasePasswordFileError, match=reason):
            database_url_from_environment(
                {"DATABASE_URL": url, "DATABASE_PASSWORD_FILE": str(path)}
            )
    # Unset keeps the URL-carries-its-own-credentials path for local development.
    assert database_url_from_environment({"DATABASE_URL": url}) == url
    good = tmp_path / "good"
    good.write_text("s3cret\n", encoding="utf-8")
    assert "s3cret" in database_url_from_environment(
        {"DATABASE_URL": url, "DATABASE_PASSWORD_FILE": str(good)}
    )


def test_migrate_names_the_unusable_password_file(tmp_path: Path, monkeypatch) -> None:
    empty = tmp_path / "postgres_password"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://water_agent@127.0.0.1:1/water_agent")
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(empty))
    result = subprocess.run(
        [sys.executable, "-m", "water_agent.webapp.lifecycle", "migrate"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "DATABASE_PASSWORD_FILE is empty" in result.stderr
    assert "Traceback" not in result.stderr


def test_bootstrap_database_failure_does_not_expose_connection_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from water_agent.webapp.auth import bootstrap

    def fail_configuration() -> None:
        raise RuntimeError("postgresql://user:secret-value@database/internal")

    monkeypatch.setattr(bootstrap, "configure_database_stores", fail_configuration)
    assert bootstrap.run_bootstrap() == 1
    captured = capsys.readouterr()
    assert captured.err == "bootstrap-admin: ERROR — database unavailable.\n"
    assert "secret-value" not in captured.err


def test_ready_rejects_missing_checkpoint_migration_state(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('0001_step38')")
        )
        connection.execute(text("DELETE FROM checkpoint_migrations"))
    client = TestClient(create_app(static_dir=tmp_path, database=database))
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["data"]["checks"]["migrations"] == "0001_step38"
    assert response.json()["data"]["checks"]["checkpoint_migrations"] == "not_applied"


def test_transaction_failure_rolls_back_all_business_records(tmp_path: Path) -> None:
    database = _database(tmp_path)
    user_id = str(uuid4())
    with pytest.raises(RuntimeError), database.transaction() as session:
        session.execute(
            text(
                "INSERT INTO users (user_id, username, password_hash, role, must_change_password, is_active, created_at, updated_at) "
                "VALUES (:id, :name, :hash, :role, :must_change, :active, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "id": user_id,
                "name": "rollback",
                "hash": "argon2id",
                "role": "member",
                "must_change": False,
                "active": True,
            },
        )
        raise RuntimeError("rollback")
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM users WHERE user_id=:id"), {"id": user_id}).scalar_one() == 0


def test_project_session_run_queries_are_owner_and_membership_scoped(tmp_path: Path) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    sessions = WebSessionRepository(database)
    runs = RunRepository(database)
    owner, member, outsider = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    with database.transaction() as db_session:
        for uid, name in ((owner, "owner"), (member, "member"), (outsider, "outsider")):
            db_session.execute(
                text(
                    "INSERT INTO users (user_id, username, password_hash, role, must_change_password, is_active, created_at, updated_at) "
                    "VALUES (:id, :name, 'hash', 'member', 0, 1, :now, :now)"
                ),
                {"id": str(uid), "name": name, "now": now},
            )
    project = projects.create(owner, "authorized")
    projects.add_member(project.project_id, owner, member)
    web_session = sessions.create(member, project_id=project.project_id, title="private")
    run = runs.create(web_session.thread_id, member)
    assert sessions.get_for_user(web_session.thread_id, owner).title == "private"
    assert runs.get_for_user(run.run_id, member).thread_id == web_session.thread_id
    assert runs.get_for_user(run.run_id, owner).thread_id == web_session.thread_id
    with pytest.raises(ResourceNotFoundError):
        sessions.get_for_user(web_session.thread_id, outsider)
    with pytest.raises(ResourceNotFoundError):
        runs.get_for_user(run.run_id, outsider)
    with database.transaction() as db_session:
        db_session.execute(
            text(
                "DELETE FROM project_memberships WHERE project_id=:project_id AND user_id=:user_id"
            ),
            {"project_id": project.project_id, "user_id": str(member)},
        )
    with pytest.raises(ResourceNotFoundError):
        sessions.get_for_user(web_session.thread_id, member)
    with pytest.raises(ResourceNotFoundError):
        runs.get_for_user(run.run_id, member)
    with pytest.raises(ResourceNotFoundError):
        sessions.delete_for_user(web_session.thread_id, member)


def test_deleting_project_cascades_instead_of_reclassifying_session_as_personal(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    owner = uuid4()
    now = datetime.now(UTC)
    with database.transaction() as session:
        session.execute(
            text(
                "INSERT INTO users (user_id, username, password_hash, role, must_change_password, is_active, created_at, updated_at) "
                "VALUES (:id, 'cascade-owner', 'hash', 'member', 0, 1, :now, :now)"
            ),
            {"id": str(owner), "now": now},
        )
    project = ProjectRepository(database).create(owner, "temporary")
    web_session = WebSessionRepository(database).create(
        owner, project_id=project.project_id, title="must not become personal"
    )
    with database.transaction() as session:
        session.execute(
            text("DELETE FROM projects WHERE project_id=:project_id"),
            {"project_id": project.project_id},
        )
    with pytest.raises(ResourceNotFoundError):
        WebSessionRepository(database).get_for_user(web_session.thread_id, owner)


def test_database_auth_and_session_records_survive_store_restart(tmp_path: Path) -> None:
    from water_agent.webapp.auth.db_store import PostgresSessionStore, PostgresUserStore
    from water_agent.webapp.auth.models import UserRole

    database = _database(tmp_path)
    first_users = PostgresUserStore(database)
    user = first_users.create_user("restart-user", "RestartPass2026!", UserRole.MEMBER)
    first_sessions = PostgresSessionStore(database)
    created = first_sessions.create(user.user_id)

    second_users = PostgresUserStore(database)
    second_sessions = PostgresSessionStore(database)
    assert second_users.get_by_username("restart-user").user_id == user.user_id
    assert second_sessions.get(created.session_id).user_id == user.user_id


def test_database_user_store_never_deactivates_the_last_active_admin(tmp_path: Path) -> None:
    """The guard and the write are one transaction (admin rows locked FOR UPDATE on PG)."""

    from water_agent.webapp.auth.db_store import PostgresUserStore
    from water_agent.webapp.auth.models import UserRole
    from water_agent.webapp.auth.user_store import LastActiveAdminError, UserNotFoundError

    database = _database(tmp_path)
    users = PostgresUserStore(database)
    first = users.create_user("guard-admin-1", "GuardAdminPass2026!", UserRole.ADMIN)
    second = users.create_user("guard-admin-2", "GuardAdminPass2026!", UserRole.ADMIN)
    member = users.create_user("guard-member", "GuardMemberPass2026!", UserRole.MEMBER)

    users.deactivate(member.user_id)
    users.deactivate(second.user_id)
    with pytest.raises(LastActiveAdminError):
        users.deactivate(first.user_id)

    assert users.get_by_id(first.user_id).is_active is True
    assert users.get_by_id(second.user_id).is_active is False
    assert users.get_by_id(member.user_id).is_active is False
    assert users.has_admin() is True
    # An already inactive account is a no-op, not a guard trip; unknown ids stay 404.
    users.deactivate(second.user_id)
    with pytest.raises(UserNotFoundError):
        users.deactivate(uuid4())


def test_sqlite_cli_legacy_checkpointer_remains_available(tmp_path: Path) -> None:
    from langgraph.checkpoint.sqlite import SqliteSaver

    with SqliteSaver.from_conn_string(str(tmp_path / "legacy.sqlite")) as saver:
        assert saver is not None


def test_postgres_checkpoint_factory_requires_ready_database(tmp_path: Path) -> None:
    from water_agent.storage.checkpoint import postgres_checkpointer
    from water_agent.storage.database import DatabaseNotReadyError

    database = _database(tmp_path)
    with pytest.raises(DatabaseNotReadyError), postgres_checkpointer(database):
        pass


def test_runtime_never_uses_checkpointer_setup_as_a_schema_path() -> None:
    source = Path("src/water_agent/storage/checkpoint.py").read_text(encoding="utf-8")
    assert ".setup(" not in source


def test_scoped_checkpointer_blocks_a_guessed_thread_before_delegate(tmp_path: Path) -> None:
    from langgraph.checkpoint.memory import MemorySaver

    from water_agent.storage.scoped_checkpoint import ScopedPostgresSaver

    database = _database(tmp_path)
    owner, outsider = uuid4(), uuid4()
    now = datetime.now(UTC)
    with database.transaction() as db_session:
        for uid, name in ((owner, "scope-owner"), (outsider, "scope-outsider")):
            db_session.execute(
                text(
                    "INSERT INTO users (user_id, username, password_hash, role, must_change_password, is_active, created_at, updated_at) "
                    "VALUES (:id, :name, 'hash', 'member', 0, 1, :now, :now)"
                ),
                {"id": str(uid), "name": name, "now": now},
            )
    record = WebSessionRepository(database).create(owner, title="hidden")
    scoped = ScopedPostgresSaver(MemorySaver(), WebSessionRepository(database), outsider)
    with pytest.raises(ResourceNotFoundError):
        scoped.get_tuple({"configurable": {"thread_id": record.thread_id}})
    with pytest.raises(ResourceNotFoundError):
        asyncio.run(scoped.aget_tuple({"configurable": {"thread_id": record.thread_id}}))


def test_postgres_checkpoint_cross_process() -> None:
    """Two Python processes prove checkpoint state survives process exit."""
    database = _postgres_database()
    url = database.settings.url
    database.dispose()
    thread_id = str(uuid4())
    program = """
import os, sys
from langchain_core.messages import HumanMessage
from water_agent.graphs.chat import build_chat_app
from water_agent.storage.checkpoint import postgres_checkpointer
from water_agent.storage.database import Database
database = Database(url=os.environ['P5_TEST_DATABASE_URL'])
with postgres_checkpointer(database) as saver:
    result = build_chat_app(checkpointer=saver).invoke(
        {'messages': [HumanMessage(content=sys.argv[2])], 'query': ''},
        config={'configurable': {'thread_id': sys.argv[1]}},
    )
contents = [getattr(item, 'content', '') for item in result['messages']]
raise SystemExit(0 if sys.argv[3] in contents else 9)
"""
    env = {**os.environ, "P5_TEST_DATABASE_URL": url}
    first = subprocess.run(
        [sys.executable, "-c", program, thread_id, "跨进程前", "跨进程前"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    second = subprocess.run(
        [sys.executable, "-c", program, thread_id, "跨进程后", "跨进程前"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert second.returncode == 0, second.stderr


def test_postgres_auth_and_web_session_survive_process_restart() -> None:
    database = _postgres_database()
    url = database.settings.url
    database.dispose()
    username = f"restart-{uuid4()}"
    program = """
import json, os, sys
from water_agent.storage.database import Database
from water_agent.storage.repository import WebSessionRepository
from water_agent.webapp.auth.db_store import PostgresSessionStore, PostgresUserStore
from water_agent.webapp.auth.models import UserRole
database = Database(url=os.environ['P5_TEST_DATABASE_URL'])
users = PostgresUserStore(database)
sessions = PostgresSessionStore(database)
web_sessions = WebSessionRepository(database)
if sys.argv[1] == 'create':
    user = users.create_user(sys.argv[2], 'RestartPass2026!', UserRole.MEMBER)
    sessions.create(user.user_id)
    web_session = web_sessions.create(user.user_id, title='restart-visible')
    print(json.dumps({'user_id': str(user.user_id), 'thread_id': web_session.thread_id}))
else:
    user = users.get_by_username(sys.argv[2])
    assert str(user.user_id) == sys.argv[3]
    assert sessions.list_for_user(user.user_id)
    assert web_sessions.get_for_user(sys.argv[4], user.user_id).title == 'restart-visible'
"""
    env = {**os.environ, "P5_TEST_DATABASE_URL": url}
    created = subprocess.run(
        [sys.executable, "-c", program, "create", username],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    identifiers = json.loads(created.stdout)
    restored = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            "restore",
            username,
            identifiers["user_id"],
            identifiers["thread_id"],
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert restored.returncode == 0, restored.stderr


def test_postgres_scoped_checkpoint_and_metadata_are_user_isolated() -> None:
    from langchain_core.messages import HumanMessage

    from water_agent.graphs.chat import build_chat_app
    from water_agent.storage.checkpoint import postgres_checkpointer
    from water_agent.storage.scoped_checkpoint import ScopedPostgresSaver
    from water_agent.webapp.auth.db_store import PostgresUserStore
    from water_agent.webapp.auth.models import UserRole

    database = _postgres_database()
    users = PostgresUserStore(database)
    owner = users.create_user(f"owner-{uuid4()}", "OwnerPass2026!", UserRole.MEMBER)
    outsider = users.create_user(f"outsider-{uuid4()}", "OutsiderPass2026!", UserRole.MEMBER)
    sessions = WebSessionRepository(database)
    web_session = sessions.create(owner.user_id, title="secret metadata")
    config = {"configurable": {"thread_id": web_session.thread_id}}

    with postgres_checkpointer(database) as saver:
        owner_saver = ScopedPostgresSaver(saver, sessions, owner.user_id)
        asyncio.run(
            build_chat_app(checkpointer=owner_saver).ainvoke(
                {"messages": [HumanMessage(content="owner checkpoint")], "query": ""},
                config=config,
            )
        )
        outsider_saver = ScopedPostgresSaver(saver, sessions, outsider.user_id)
        with pytest.raises(ResourceNotFoundError):
            outsider_saver.get_tuple(config)
        with pytest.raises(ResourceNotFoundError):
            sessions.get_for_user(web_session.thread_id, outsider.user_id)
    database.dispose()


def test_postgres_project_membership_scopes_session_and_run_metadata() -> None:
    from water_agent.webapp.auth.db_store import PostgresUserStore
    from water_agent.webapp.auth.models import UserRole

    database = _postgres_database()
    users = PostgresUserStore(database)
    owner = users.create_user(f"project-owner-{uuid4()}", "OwnerPass2026!", UserRole.MEMBER)
    member = users.create_user(f"project-member-{uuid4()}", "MemberPass2026!", UserRole.MEMBER)
    outsider = users.create_user(
        f"project-outsider-{uuid4()}", "OutsiderPass2026!", UserRole.MEMBER
    )
    projects = ProjectRepository(database)
    sessions = WebSessionRepository(database)
    runs = RunRepository(database)
    project = projects.create(owner.user_id, "isolated project")
    projects.add_member(project.project_id, owner.user_id, member.user_id)
    web_session = sessions.create(member.user_id, project_id=project.project_id, title="hidden")
    run = runs.create(web_session.thread_id, member.user_id)

    assert sessions.get_for_user(web_session.thread_id, owner.user_id).title == "hidden"
    assert runs.get_for_user(run.run_id, owner.user_id).thread_id == web_session.thread_id
    with pytest.raises(ResourceNotFoundError):
        projects.get_for_user(project.project_id, outsider.user_id)
    with pytest.raises(ResourceNotFoundError):
        sessions.get_for_user(web_session.thread_id, outsider.user_id)
    with pytest.raises(ResourceNotFoundError):
        runs.get_for_user(run.run_id, outsider.user_id)

    with database.transaction() as session:
        session.execute(
            text(
                "DELETE FROM project_memberships WHERE project_id=:project_id AND user_id=:user_id"
            ),
            {"project_id": project.project_id, "user_id": str(member.user_id)},
        )
    with pytest.raises(ResourceNotFoundError):
        sessions.get_for_user(web_session.thread_id, member.user_id)
    with pytest.raises(ResourceNotFoundError):
        runs.get_for_user(run.run_id, member.user_id)
    database.dispose()


def test_postgres_transaction_failure_leaves_no_partial_business_records() -> None:
    database = _postgres_database()
    user_id = str(uuid4())
    thread_id = str(uuid4())
    username = f"rollback-{uuid4()}"
    with pytest.raises(RuntimeError), database.transaction() as session:
        session.execute(
            text(
                "INSERT INTO users (user_id, username, password_hash, role, must_change_password, is_active, created_at, updated_at) "
                "VALUES (:user_id, :username, 'hash', 'member', false, true, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"user_id": user_id, "username": username},
        )
        session.execute(
            text(
                "INSERT INTO web_sessions (thread_id, owner_user_id, title, created_at, updated_at) "
                "VALUES (:thread_id, :user_id, 'partial', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"thread_id": thread_id, "user_id": user_id},
        )
        raise RuntimeError("force transaction rollback")

    with database.engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM users WHERE user_id=:user_id"), {"user_id": user_id}
        ).scalar_one() == 0
        assert connection.execute(
            text("SELECT count(*) FROM web_sessions WHERE thread_id=:thread_id"),
            {"thread_id": thread_id},
        ).scalar_one() == 0
    database.dispose()


def test_step15_migration_adds_and_removes_the_nullable_model_columns() -> None:
    """P7-S15-A06 迁移 0008 在 SQLite 上真跑：两张表各加一个可空 ``model`` 列，既有行读出为 ``None``
    （= 用全局模型）；降级后列消失、既有行仍在。版本链接在 0007 之后。"""

    import importlib.util

    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path("alembic/versions/0008_step15_model_choice.py")
    spec = importlib.util.spec_from_file_location("migration_0008", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0008_step15_model_choice"
    assert module.down_revision == "0007_step51_dwg_uploads"

    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE web_sessions (thread_id VARCHAR(36) PRIMARY KEY, title VARCHAR(200))"))
        connection.execute(sa.text("CREATE TABLE runs (run_id VARCHAR(36) PRIMARY KEY, status VARCHAR(32))"))
        connection.execute(sa.text("INSERT INTO web_sessions VALUES ('t1', 'old')"))
        connection.execute(sa.text("INSERT INTO runs VALUES ('r1', 'succeeded')"))

        def columns(table: str) -> list[str]:
            return [column["name"] for column in sa.inspect(connection).get_columns(table)]

        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            assert "model" in columns("web_sessions") and "model" in columns("runs")
            assert connection.execute(sa.text("SELECT model FROM web_sessions")).scalar() is None
            assert connection.execute(sa.text("SELECT model FROM runs")).scalar() is None
            module.downgrade()
        assert "model" not in columns("web_sessions") and "model" not in columns("runs")
        assert connection.execute(sa.text("SELECT title FROM web_sessions")).scalar() == "old"
