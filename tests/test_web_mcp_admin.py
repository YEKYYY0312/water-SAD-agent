"""P7-S6 Web 扩展面板的管理 API（``docs/design/16-p7-plugin-mcp.md`` §7 S6 A03–A07）。

SQLite 上的真实鉴权（``create_app(database=...)``）+ 真实 ``WebAgenticSessionFactory``（PostgreSQL
checkpointer 换成内存实现，与 ``tests/test_web_mcp_pool.py`` 同法）；MCP 用
``tests/fixtures/fake_mcp_server.py --mode stateful``。配置三层都在临时目录。
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event

from water_agent.mcp.config import MCP_CONFIG_VERSION
from water_agent.runtime.governance.binding import local_request_context
from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.webapp import run_manager as rm
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.audit import _clear_events, _get_events
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole

FAKE_SERVER = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
PASSWORD = "McpAdminPass2026!"

#: 这些值无论如何都不能出现在任何响应、审计事件或层文件里。
ARGS_SECRET = "args-secret-value-0001"
INLINE_SECRET = "inline-secret-value-0002"
FILE_SECRET = "file-secret-value-0003"
SLOT_SECRET = "slot-secret-value-0004"
SLOT_SECRET_ROTATED = "slot-secret-value-0005"
SECRET_COMMAND_DIR = "/opt/operator-only-dir"


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp_admin.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    db = Database(engine=engine)
    yield db
    db.dispose()


def _write(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _stateful(server_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "server_id": server_id,
        "command": sys.executable,
        "args": [str(FAKE_SERVER), "--mode", "stateful"],
        "allow": ["whoami"],
        "call_timeout_seconds": 3.0,
        "lifecycle": "shared",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def layers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """仓库默认层（空骨架）+ 部署覆盖层（两台 server）+ Web 管理层（尚不存在）。"""

    from water_agent.config.credentials import forget_injected_credentials
    from water_agent.runtime.governance.inventory import clear_mcp_tool_bindings

    env_file = tmp_path / "deploy" / "secret.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(f"FILE_TOKEN={FILE_SECRET}\n", encoding="utf-8")
    base = _write(tmp_path / "repo" / "mcp_servers.json", {"version": MCP_CONFIG_VERSION, "servers": []})
    overlay = _write(
        tmp_path / "deploy" / "overlay.json",
        {
            "servers": [
                _stateful("adm_cad", credential_env=["CAD_API_TOKEN"]),
                {
                    "server_id": "adm_secret",
                    "command": f"{SECRET_COMMAND_DIR}/bin/library-server",
                    "args": ["--token", ARGS_SECRET],
                    "env": {"INLINE_TOKEN": INLINE_SECRET},
                    "env_file": str(env_file),
                    "credential_env": ["SLOT_TOKEN"],
                    "human_confirmation": True,
                    "disabled": True,
                },
            ]
        },
    )
    web = tmp_path / "mcp-admin" / "web_layer.json"
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    monkeypatch.setenv("WATER_AGENT_MCP_CONFIG", str(base))
    monkeypatch.setenv("WATER_AGENT_MCP_OVERLAY", str(overlay))
    monkeypatch.setenv("WATER_AGENT_MCP_WEB_LAYER", str(web))
    for key in ("WATER_AGENT_TOOL_EXPOSURE", "DRAWING_ROOT", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    forget_injected_credentials()
    clear_mcp_tool_bindings()
    _clear_events()
    yield SimpleNamespace(base=base, overlay=overlay, web=web, env_file=env_file, tmp=tmp_path)
    clear_mcp_tool_bindings()
    forget_injected_credentials()


@pytest.fixture
def factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[rm.WebAgenticSessionFactory]:
    from langgraph.checkpoint.memory import InMemorySaver

    from water_agent.session import default_checkpoint_serde

    @contextmanager
    def memory_checkpointer(_database: Any) -> Iterator[Any]:
        yield InMemorySaver(serde=default_checkpoint_serde())

    monkeypatch.setattr(rm, "postgres_checkpointer", memory_checkpointer)
    monkeypatch.setattr(rm, "ScopedPostgresSaver", lambda saver, _sessions, _user: saver)
    instance = rm.WebAgenticSessionFactory(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        trace_dir=tmp_path / "traces",
        session_dir=tmp_path / "sessions",
        mock_generator=True,
    )
    yield instance
    instance.close()


@pytest.fixture
def app(database: Database, tmp_path: Path, factory: rm.WebAgenticSessionFactory, layers: SimpleNamespace):
    return create_app(database=database, static_dir=tmp_path, session_factory=factory)


def _login(app: Any, database: Database, role: UserRole, *, must_change: bool = False) -> TestClient:
    username = f"{role.value}-{uuid4().hex[:8]}"
    PostgresUserStore(database).create_user(username, PASSWORD, role, must_change_password=must_change)
    client = TestClient(app, raise_server_exceptions=True)
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def _headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token
    return {"X-CSRF-Token": token}


def _servers(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/admin/mcp/servers")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _patch(client: TestClient, server_id: str, payload: dict[str, Any]) -> Any:
    return client.patch(f"/api/admin/mcp/servers/{server_id}", json=payload, headers=_headers(client))


def _create_session(factory: rm.WebAgenticSessionFactory, stack: ExitStack) -> Any:
    thread_id = uuid4().hex
    return factory.create(
        run_id=uuid4().hex,
        thread_id=thread_id,
        user_id=uuid4(),
        principal=local_request_context(thread_id=thread_id).principal,
        project_id=None,
        stack=stack,
    )


def _pid(session: Any, server_id: str) -> int:
    mcp_session = next(s for s in session.mcp_assembly.sessions if s.config.server_id == server_id)
    return int(mcp_session.client.call_tool("whoami", {})["structured_content"]["pid"])


def _gone(pid: int) -> bool:
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _no_secrets(text: str) -> None:
    for secret in (ARGS_SECRET, INLINE_SECRET, FILE_SECRET, SLOT_SECRET, SLOT_SECRET_ROTATED):
        assert secret not in text, secret
    assert SECRET_COMMAND_DIR not in text
    assert sys.executable not in text, "完整命令路径不返回"


# ---------------------------------------------------------------------------
# A04：权限与读接口
# ---------------------------------------------------------------------------


def test_admin_api_authorization(app: Any, database: Database, layers: SimpleNamespace) -> None:
    """P7-S6-A04 全部接口 require_admin：未登录 401、成员 403、临时密码管理员 403；写接口缺 CSRF 403。
    读接口不含 args、env 的值、env_file 内容、凭据值与完整命令路径。"""

    anonymous = TestClient(app)
    assert anonymous.get("/api/admin/mcp/servers").status_code == 401

    member = _login(app, database, UserRole.MEMBER)
    response = member.get("/api/admin/mcp/servers")
    assert response.status_code == 403 and response.json()["error"]["code"] == "AUTH_FORBIDDEN"
    assert _patch(member, "adm_cad", {"disabled": True, "expected_revision": 0}).status_code == 403

    temporary = _login(app, database, UserRole.ADMIN, must_change=True)
    response = temporary.get("/api/admin/mcp/servers")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_MUST_CHANGE_PASSWORD"

    admin = _login(app, database, UserRole.ADMIN)
    writes = [
        ("patch", "/api/admin/mcp/servers/adm_cad", {"disabled": True, "expected_revision": 0}),
        ("delete", "/api/admin/mcp/servers/adm_cad/overrides?expected_revision=0", None),
        ("delete", "/api/admin/mcp/web-layer", None),
        ("put", "/api/admin/mcp/servers/adm_cad/credentials/CAD_API_TOKEN", {"value": SLOT_SECRET}),
        ("delete", "/api/admin/mcp/servers/adm_cad/credentials/CAD_API_TOKEN", None),
        ("post", "/api/admin/mcp/servers/adm_cad/probe", None),
        ("post", "/api/admin/mcp/import-preview", {"text": '{"mcpServers": {}}'}),
    ]
    for method, url, payload in writes:
        kwargs = {"json": payload} if payload is not None else {}
        response = getattr(admin, method)(url, **kwargs)
        assert response.status_code == 403, (method, url, response.text)
        assert response.json()["error"]["code"] == "AUTH_CSRF_INVALID"
    assert not layers.web.exists(), "缺 CSRF 的写入一条都没落盘"

    # 槽位填上值之后，读接口照样只报状态
    response = admin.put(
        "/api/admin/mcp/servers/adm_secret/credentials/SLOT_TOKEN",
        json={"value": SLOT_SECRET},
        headers=_headers(admin),
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["credential"]["configured"] is True

    data = _servers(admin)
    text = json.dumps(data, ensure_ascii=False)
    _no_secrets(text)
    assert "args" not in {key for server in data["servers"] for key in server}
    assert "env" not in {key for server in data["servers"] for key in server}
    by_id = {server["server_id"]: server for server in data["servers"]}
    assert by_id["adm_secret"]["command_name"] == "library-server"
    assert by_id["adm_secret"]["credentials"] == [
        {"name": "SLOT_TOKEN", "configured": True, "updated_at": by_id["adm_secret"]["credentials"][0]["updated_at"]}
    ]
    assert by_id["adm_secret"]["human_confirmation"] is True
    assert by_id["adm_cad"]["lifecycle"] == "shared"
    assert by_id["adm_cad"]["sources"]["command"] == "overlay"
    assert data["notice"].startswith("改动从下一个 run 生效")
    assert data["web_layer"] == {"configured": True, "writable": True, "revision": 0}
    assert data["config_error"] is None


def test_capabilities_report_counts_only(app: Any, database: Database, layers: SimpleNamespace) -> None:
    """P7-S6-A04 普通成员的能力端点只给计数（启用 / 停用 / 需确认 / 共享），不给 server 清单；
    S7 之后已过期的"危险确认门仍按配置 fail closed"文案去掉。"""

    member = _login(app, database, UserRole.MEMBER)
    mcp = member.get("/api/system/capabilities").json()["data"]["mcp"]
    assert mcp["configured_server_count"] == 2
    assert mcp["enabled_server_count"] == 1
    assert mcp["disabled_server_count"] == 1
    assert mcp["confirmation_server_count"] == 0, "停用的 server 不计入"
    assert mcp["shared_server_count"] == 1
    assert "fail closed" not in mcp["message"]
    text = json.dumps(mcp, ensure_ascii=False)
    assert "adm_cad" not in text and "adm_secret" not in text


# ---------------------------------------------------------------------------
# A04：PATCH 乐观并发与整份校验；恢复部署配置
# ---------------------------------------------------------------------------


def test_server_update_is_validated_and_versioned(
    app: Any, database: Database, layers: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S6-A04 PATCH：未知 server 404（不回显名字）、expected_revision 过期 409、写 command 等字段 /
    非法值 422（不回显请求值、不落盘）、合并后不合法（启用一台 cwd 不存在的 server）422 且层文件不变、
    成功后 revision + 1；没设 WATER_AGENT_MCP_WEB_LAYER 时 503。"""

    admin = _login(app, database, UserRole.ADMIN)

    response = _patch(admin, "no_such_server", {"disabled": True, "expected_revision": 0})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WEB_MCP_SERVER_UNKNOWN"
    assert "no_such_server" not in response.text

    # 面板写不了运维的字段：command / args / env / cwd / human_confirmation / credential_env……
    for forbidden in (
        {"command": "/bin/sh"},
        {"args": ["-c", "id"]},
        {"env": {"LD_PRELOAD": "/tmp/evil.so"}},
        {"cwd": "/"},
        {"lifecycle": "shared"},
        {"human_confirmation": False},
        {"credential_env": ["NODE_OPTIONS"]},
        {"server_id": "other"},
    ):
        response = _patch(admin, "adm_cad", {**forbidden, "disabled": False, "expected_revision": 0})
        assert response.status_code == 422, forbidden
        assert response.json()["error"]["code"] == "WEB_MCP_CONFIG_REJECTED"
        for value in ("/bin/sh", "/tmp/evil.so", "NODE_OPTIONS"):
            assert value not in response.text
    assert not layers.web.exists(), "被拒的写入一条都没落盘"

    for bad in (
        {"disabled": "true", "expected_revision": 0},
        {"allow": ["has space"], "expected_revision": 0},
        {"allow": ["dup", "dup"], "expected_revision": 0},
        {"allow": "whoami", "expected_revision": 0},
        {"expected_revision": 0},
        {"disabled": True},
        {"disabled": True, "expected_revision": -1},
        {"disabled": True, "expected_revision": True},
    ):
        response = _patch(admin, "adm_cad", bad)
        assert response.status_code == 422, bad
        assert "has space" not in response.text
    assert not layers.web.exists()

    response = _patch(admin, "adm_cad", {"allow": ["whoami", "bump"], "expected_revision": 0})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["revision"] == 1
    server = response.json()["data"]["server"]
    assert server["allow"] == ["whoami", "bump"] and server["sources"]["allow"] == "web"
    on_disk = json.loads(layers.web.read_text(encoding="utf-8"))
    assert on_disk["revision"] == 1 and on_disk["servers"][0]["allow"] == ["whoami", "bump"]

    # 过期的 revision
    response = _patch(admin, "adm_cad", {"disabled": True, "expected_revision": 0})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WEB_MCP_REVISION_CONFLICT"

    # 合并后加载不了：adm_secret 是停用的，启用它会让一个不存在的 env_file 生效
    before = layers.web.read_text(encoding="utf-8")
    layers.env_file.unlink()
    response = _patch(admin, "adm_secret", {"disabled": False, "expected_revision": 1})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "WEB_MCP_CONFIG_REJECTED"
    assert "env_file" in response.json()["error"]["message"]
    assert str(layers.env_file) not in response.text
    assert layers.web.read_text(encoding="utf-8") == before

    # 面板可以启用运维停用的 server（路径恢复后）
    layers.env_file.write_text(f"FILE_TOKEN={FILE_SECRET}\n", encoding="utf-8")
    response = _patch(admin, "adm_secret", {"disabled": False, "expected_revision": 1})
    assert response.status_code == 200 and response.json()["data"]["revision"] == 2
    assert response.json()["data"]["server"]["disabled"] is False
    _no_secrets(response.text)

    monkeypatch.delenv("WATER_AGENT_MCP_WEB_LAYER")
    response = _patch(admin, "adm_cad", {"disabled": True, "expected_revision": 2})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "WEB_MCP_ADMIN_STORE_UNAVAILABLE"
    assert _servers(admin)["web_layer"]["configured"] is False


def test_reset_restores_deployment_config(app: Any, database: Database, layers: SimpleNamespace) -> None:
    """P7-S6-A04 恢复部署配置：单台（DELETE .../overrides，孤立条目也用它清理）与整层（DELETE
    /web-layer）；层文件损坏时读接口照常返回错误码，整层恢复可用。"""

    admin = _login(app, database, UserRole.ADMIN)
    assert _patch(admin, "adm_cad", {"disabled": True, "expected_revision": 0}).status_code == 200

    # 本来就没有改动的 server：幂等
    response = admin.delete("/api/admin/mcp/servers/adm_secret/overrides?expected_revision=1", headers=_headers(admin))
    assert response.status_code == 200 and response.json()["data"]["revision"] == 1

    response = admin.delete("/api/admin/mcp/servers/adm_cad/overrides?expected_revision=0", headers=_headers(admin))
    assert response.status_code == 409
    response = admin.delete("/api/admin/mcp/servers/adm_cad/overrides?expected_revision=1", headers=_headers(admin))
    assert response.status_code == 200 and response.json()["data"]["revision"] == 2
    by_id = {server["server_id"]: server for server in _servers(admin)["servers"]}
    assert by_id["adm_cad"]["disabled"] is False and by_id["adm_cad"]["web_override"] is None

    # 孤立条目：运维把 server 从覆盖层删掉之后，面板如实列出并能清理
    _write(layers.web, {"revision": 2, "servers": [{"server_id": "adm_gone", "disabled": False}]})
    data = _servers(admin)
    assert [entry["server_id"] for entry in data["orphans"]] == ["adm_gone"]
    assert "adm_gone" not in {server["server_id"] for server in data["servers"]}
    response = admin.delete("/api/admin/mcp/servers/adm_gone/overrides?expected_revision=2", headers=_headers(admin))
    assert response.status_code == 200
    assert _servers(admin)["orphans"] == []
    response = admin.delete("/api/admin/mcp/servers/adm_never/overrides?expected_revision=3", headers=_headers(admin))
    assert response.status_code == 404

    # 层文件坏了：读接口照常返回并报告是哪一层；PATCH 拒绝；整层恢复可用
    layers.web.write_text("{ 这不是 JSON", encoding="utf-8")
    data = _servers(admin)
    assert data["config_error"]["code"] == "MCP_CONFIG_INVALID"
    assert data["config_error"]["layer"] == "web"
    assert {server["server_id"] for server in data["servers"]} == {"adm_cad", "adm_secret"}
    assert _patch(admin, "adm_cad", {"disabled": True, "expected_revision": 3}).status_code == 422
    response = admin.delete("/api/admin/mcp/web-layer", headers=_headers(admin))
    assert response.status_code == 200 and response.json()["data"]["existed"] is True
    data = _servers(admin)
    assert data["config_error"] is None and data["web_layer"]["revision"] == 0
    assert not layers.web.exists()

    # 运维的覆盖层坏了：面板如实报告，改不了它
    layers.overlay.write_text("{ 坏", encoding="utf-8")
    data = _servers(admin)
    assert data["config_error"]["layer"] == "operator" and data["servers"] == []
    assert _patch(admin, "adm_cad", {"disabled": True, "expected_revision": 0}).status_code == 422


# ---------------------------------------------------------------------------
# A05：下一个 run 生效
# ---------------------------------------------------------------------------


def test_changes_take_effect_on_the_next_run(
    app: Any, database: Database, layers: SimpleNamespace, factory: rm.WebAgenticSessionFactory
) -> None:
    """P7-S6-A05 停用 / 改 allow / 改凭据后，下一次会话工厂装配随之变化：allow 与凭据进 shared server
    的指纹，下一个 run 换新进程，旧进程在借用它的 run 结束后关闭；正在跑的 run 不受影响。"""

    admin = _login(app, database, UserRole.ADMIN)

    # 槽位没填值：整台跳过
    with ExitStack() as stack:
        session = _create_session(factory, stack)
        assert [(s.server_id, s.code) for s in session.mcp_assembly.skipped_servers] == [
            ("adm_secret", "MCP_SERVER_DISABLED"),
            ("adm_cad", "MCP_CREDENTIAL_MISSING"),
        ]

    response = admin.put(
        "/api/admin/mcp/servers/adm_cad/credentials/CAD_API_TOKEN", json={"value": SLOT_SECRET}, headers=_headers(admin)
    )
    assert response.status_code == 200

    with ExitStack() as running:
        first = _create_session(factory, running)
        assert "mcp_adm_cad_whoami" in first.mcp_tool_names
        assert "mcp_adm_cad_env_present" not in first.mcp_tool_names
        first_pid = _pid(first, "adm_cad")

        # run 还在跑的时候改 allow：这个 run 照常可用，下一个 run 拿到新清单与新进程
        assert _patch(admin, "adm_cad", {"allow": ["whoami", "env_present"], "expected_revision": 0}).status_code == 200
        with ExitStack() as stack:
            second = _create_session(factory, stack)
            assert "mcp_adm_cad_env_present" in second.mcp_tool_names
            assert second.mcp_assembly.connections == {"adm_cad": "shared_new"}
            second_pid = _pid(second, "adm_cad")
            assert second_pid != first_pid
            present = next(s for s in second.mcp_assembly.sessions if s.config.server_id == "adm_cad")
            assert present.client.call_tool("env_present", {"name": "CAD_API_TOKEN"})["structured_content"][
                "present"
            ] is True
        assert _pid(first, "adm_cad") == first_pid, "正在跑的 run 不受影响"
    assert _gone(first_pid), "旧进程在借用它的 run 结束后关闭"

    # 改凭据：指纹变化，下一个 run 换新进程
    response = admin.put(
        "/api/admin/mcp/servers/adm_cad/credentials/CAD_API_TOKEN",
        json={"value": SLOT_SECRET_ROTATED},
        headers=_headers(admin),
    )
    assert response.status_code == 200
    with ExitStack() as stack:
        third = _create_session(factory, stack)
        assert third.mcp_assembly.connections == {"adm_cad": "shared_new"}
        assert _pid(third, "adm_cad") != second_pid

    # 停用：下一个 run 不再有这台 server 的工具
    assert _patch(admin, "adm_cad", {"disabled": True, "expected_revision": 1}).status_code == 200
    with ExitStack() as stack:
        fourth = _create_session(factory, stack)
        assert not [name for name in fourth.mcp_tool_names if name.startswith("mcp_adm_cad_")]
        assert ("adm_cad", "MCP_SERVER_DISABLED") in [
            (s.server_id, s.code) for s in fourth.mcp_assembly.skipped_servers
        ]
    assert "adm_cad" not in factory.mcp_pool.snapshot()["active"]


# ---------------------------------------------------------------------------
# A06：探测
# ---------------------------------------------------------------------------


def test_probe_reports_tools_without_remote_text(
    app: Any, database: Database, layers: SimpleNamespace, factory: rm.WebAgenticSessionFactory
) -> None:
    """P7-S6-A06 探测：只对启用的 server、经 Web 会话工厂的连接池借一次租约（confirmation_ready=True）；
    返回工具名与跳过码（allow 全拒时也列得出工具名）、不含远端描述原文；不让池里其他在用的 shared
    连接退役；停用 409、并发 409、未知 404。"""

    from water_agent.webapp.mcp_admin_routes import McpAdminState

    admin = _login(app, database, UserRole.ADMIN)
    admin.put("/api/admin/mcp/servers/adm_cad/credentials/CAD_API_TOKEN", json={"value": SLOT_SECRET}, headers=_headers(admin))
    # 第二台 shared server 先被一个 run 打开：探测 adm_cad 不能把它退役
    _write(
        layers.overlay,
        {
            "servers": [
                _stateful("adm_cad", credential_env=["CAD_API_TOKEN"], allow=None),
                _stateful("adm_other"),
                {"server_id": "adm_off", "command": "/bin/false", "disabled": True},
            ]
        },
    )
    with ExitStack() as stack:
        session = _create_session(factory, stack)
        other_pid = _pid(session, "adm_other")

    response = admin.post("/api/admin/mcp/servers/adm_cad/probe", headers=_headers(admin))
    assert response.status_code == 200, response.text
    probe = response.json()["data"]["probe"]
    assert probe["status"] == "ok" and probe["server_code"] is None
    assert [tool["name"] for tool in probe["tools"]] == ["whoami", "bump", "peek", "env_present", "slow_echo"]
    assert {tool["code"] for tool in probe["tools"]} == {"MCP_NOT_IN_ALLOWLIST"}, "allow 全拒时照样列出工具名"
    assert "合成 fixture" not in response.text and "pid" not in json.dumps(probe), "远端描述原文不返回"
    assert set(probe) == {"server_id", "probed_at", "status", "server_code", "tools", "truncated"}

    # 放行一个之后再探测：exposed 如实
    assert _patch(admin, "adm_cad", {"allow": ["whoami"], "expected_revision": 0}).status_code == 200
    probe = admin.post("/api/admin/mcp/servers/adm_cad/probe", headers=_headers(admin)).json()["data"]["probe"]
    assert [(tool["name"], tool["exposed"]) for tool in probe["tools"]][:1] == [("whoami", True)]
    assert {tool["name"] for tool in probe["tools"] if not tool["exposed"]} == {
        "bump", "peek", "env_present", "slow_echo"
    }
    # 列表接口带最近一次探测
    by_id = {server["server_id"]: server for server in _servers(admin)["servers"]}
    assert by_id["adm_cad"]["last_probe"]["tools"] == probe["tools"]

    # 经的是会话工厂的池：其他在用的 shared 连接原样留着
    snapshot = factory.mcp_pool.snapshot()
    assert "adm_other" in snapshot["active"] and snapshot["leases"] == 0
    with ExitStack() as stack:
        assert _pid(_create_session(factory, stack), "adm_other") == other_pid

    response = admin.post("/api/admin/mcp/servers/adm_off/probe", headers=_headers(admin))
    assert response.status_code == 409 and response.json()["error"]["code"] == "WEB_MCP_SERVER_DISABLED"
    response = admin.post("/api/admin/mcp/servers/adm_nope/probe", headers=_headers(admin))
    assert response.status_code == 404

    state = app.state.mcp_admin
    assert isinstance(state, McpAdminState)
    assert state.begin_probe("adm_cad")
    try:
        response = admin.post("/api/admin/mcp/servers/adm_cad/probe", headers=_headers(admin))
        assert response.status_code == 409 and response.json()["error"]["code"] == "WEB_MCP_PROBE_BUSY"
    finally:
        state.end_probe("adm_cad", None)

    # 连接失败：只给跳过码，不给 stderr / 路径
    _write(layers.overlay, {"servers": [{"server_id": "adm_crash", "command": sys.executable,
                                         "args": [str(FAKE_SERVER), "--mode", "crash"], "allow": ["x"]}]})
    probe = admin.post("/api/admin/mcp/servers/adm_crash/probe", headers=_headers(admin)).json()["data"]["probe"]
    assert probe["status"] == "failed" and probe["server_code"] == "MCP_CONNECT_FAILED"
    assert probe["tools"] == []


# ---------------------------------------------------------------------------
# A03（经 API）：导入预览无状态
# ---------------------------------------------------------------------------


def test_import_preview_endpoint_is_stateless(app: Any, database: Database, layers: SimpleNamespace) -> None:
    """P7-S6-A03 导入预览经 API：提案里没有 env 值、每台 disabled: true、与现有 id 冲突改名；不落盘、
    不改任何配置。"""

    admin = _login(app, database, UserRole.ADMIN)
    overlay_before = layers.overlay.read_text(encoding="utf-8")
    text = json.dumps({"mcpServers": {"adm-cad": {"command": "uvx", "args": ["cad"],
                                                  "env": {"CAD_TOKEN": "import-secret-value-77"}}}})
    response = admin.post("/api/admin/mcp/import-preview", json={"text": text}, headers=_headers(admin))
    assert response.status_code == 200, response.text
    preview = response.json()["data"]["preview"]
    assert "import-secret-value-77" not in response.text
    proposal = json.loads(preview["proposal_text"])
    assert proposal["servers"][0]["server_id"] == "adm_cad_2"
    assert proposal["servers"][0]["disabled"] is True and "allow" not in proposal["servers"][0]
    assert proposal["servers"][0]["credential_env"] == ["CAD_TOKEN"]
    assert layers.overlay.read_text(encoding="utf-8") == overlay_before
    assert not layers.web.exists()

    response = admin.post("/api/admin/mcp/import-preview", json={"text": "not json or toml ["}, headers=_headers(admin))
    assert response.status_code == 422 and response.json()["error"]["code"] == "WEB_MCP_CONFIG_REJECTED"

    # HTTP 层字符上限（请求体上限要给最大的合法请求体留 4 倍余量，见 MAX_IMPORT_CHARACTERS）
    from water_agent.webapp.mcp_admin_routes import MAX_IMPORT_CHARACTERS

    response = admin.post(
        "/api/admin/mcp/import-preview", json={"text": "x" * (MAX_IMPORT_CHARACTERS + 1)}, headers=_headers(admin)
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# A07：审计
# ---------------------------------------------------------------------------


def test_admin_changes_are_audited_without_secrets(
    app: Any, database: Database, layers: SimpleNamespace
) -> None:
    """P7-S6-A07 每次变更一条结构化审计事件（actor、server、字段名、allow 增减工具名）；凭据只记槽位名
    与动作、不记值；层文件条目带 updated_at / updated_by。"""

    import logging

    class _Collect(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.DEBUG)
            self.lines: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.lines.append(record.getMessage())

    collector = _Collect()
    watched = [logging.getLogger(), logging.getLogger("water_agent"), logging.getLogger("water_agent.auth.audit"),
               logging.getLogger("uvicorn"), logging.getLogger("httpx")]
    previous_levels = [logger.level for logger in watched]
    for logger in watched:
        logger.addHandler(collector)
        logger.setLevel(logging.DEBUG)

    admin = _login(app, database, UserRole.ADMIN)
    me = admin.get("/api/auth/me").json()["data"]["user"]["user_id"]
    _clear_events()

    assert _patch(admin, "adm_cad", {"allow": ["whoami", "bump"], "expected_revision": 0}).status_code == 200
    assert _patch(admin, "adm_cad", {"allow": ["bump", "peek"], "disabled": True, "expected_revision": 1}).status_code == 200
    put = admin.put("/api/admin/mcp/servers/adm_cad/credentials/CAD_API_TOKEN", json={"value": SLOT_SECRET},
                    headers=_headers(admin))
    assert put.status_code == 200
    assert admin.delete("/api/admin/mcp/servers/adm_cad/credentials/CAD_API_TOKEN", headers=_headers(admin)).status_code == 200
    # 未声明的槽位与不存在的 server 同答 404，不回显名字，也不记审计
    for url in ("/api/admin/mcp/servers/adm_cad/credentials/OTHER_TOKEN",
                "/api/admin/mcp/servers/adm_nope/credentials/CAD_API_TOKEN"):
        response = admin.put(url, json={"value": SLOT_SECRET}, headers=_headers(admin))
        assert response.status_code == 404 and response.json()["error"]["code"] == "WEB_MCP_SERVER_UNKNOWN"
        assert "OTHER_TOKEN" not in response.text and "adm_nope" not in response.text
    bad = admin.put("/api/admin/mcp/servers/adm_cad/credentials/CAD_API_TOKEN", json={"value": "has space x"},
                    headers=_headers(admin))
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "WEB_CREDENTIAL_INVALID"
    assert "has space x" not in bad.text

    on_disk = json.loads(layers.web.read_text(encoding="utf-8"))
    entry = on_disk["servers"][0]
    assert entry["updated_by"] == me and entry["updated_at"]
    # 层文件随备份流转：里面没有任何凭据值
    _no_secrets(layers.web.read_text(encoding="utf-8"))
    assert admin.delete("/api/admin/mcp/servers/adm_cad/overrides?expected_revision=2", headers=_headers(admin)).status_code == 200
    assert admin.delete("/api/admin/mcp/web-layer", headers=_headers(admin)).status_code == 200

    events = [event for event in _get_events() if event["event"].startswith("mcp.")]
    assert [event["event"] for event in events] == [
        "mcp.server_updated",
        "mcp.server_updated",
        "mcp.credential_changed",
        "mcp.credential_changed",
        "mcp.override_removed",
        "mcp.web_layer_reset",
    ]
    first, second = events[0], events[1]
    assert first["actor_user_id"] == me and first["server_id"] == "adm_cad"
    assert first["fields"] == ["allow"]
    assert first["allow_added"] == ["bump"] and first["allow_removed"] == []
    assert second["fields"] == ["allow", "disabled"]
    assert second["allow_added"] == ["peek"] and second["allow_removed"] == ["whoami"]
    assert events[2]["slot"] == "CAD_API_TOKEN" and events[2]["action"] == "set"
    assert events[3]["action"] == "cleared"
    assert events[4]["orphan"] is False
    text = json.dumps(events, ensure_ascii=False)
    _no_secrets(text)
    _no_secrets(json.dumps(_get_events(), ensure_ascii=False))

    # 日志里也没有：结构化审计逐行落 stdout，其余 logger 一并收集
    for logger, level in zip(watched, previous_levels, strict=True):
        logger.removeHandler(collector)
        logger.setLevel(level)
    assert any("mcp.credential_changed" in line for line in collector.lines), "审计事件确实进了日志"
    _no_secrets("\n".join(collector.lines))
    # 清除之后，凭据卷里也不留这个值
    slot_files = [path for path in (layers.tmp / "credentials").rglob("*") if path.is_file()]
    assert all(SLOT_SECRET not in path.read_text(encoding="utf-8") for path in slot_files), "清除后槽位文件已删"
