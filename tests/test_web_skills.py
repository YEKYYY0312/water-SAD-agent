"""P7-S9 Skill 库面板（``docs/design/16-p7-plugin-mcp.md`` §7 S9 A08）。

SQLite 上的真实鉴权（``create_app(database=...)``）；skill 来源用随包内置的 4 份 + ``tests/fixtures/skills/operator``
的副本；启停写进临时目录里的 Web 管理层。
"""
from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event

from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.audit import _clear_events, _get_events
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole

OPERATOR = Path(__file__).resolve().parent / "fixtures" / "skills" / "operator"
PASSWORD = "SkillPanelPass2026!"
BUILTIN = ["calc-book-workflow", "drawing-review-report", "missing-input-clarification", "spec-conflict-handling"]


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    engine = create_engine(f"sqlite:///{tmp_path / 'skills.sqlite'}")

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    db = Database(engine=engine)
    yield db
    db.dispose()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    operator = tmp_path / "operator-skills"
    shutil.copytree(OPERATOR, operator)
    web = tmp_path / "mcp-admin" / "web_layer.json"
    monkeypatch.setenv("WATER_AGENT_SKILLS", "on")
    monkeypatch.setenv("WATER_AGENT_SKILLS_DIR", str(operator))
    monkeypatch.setenv("WATER_AGENT_MCP_WEB_LAYER", str(web))
    for key in ("WATER_AGENT_MCP_CONFIG", "WATER_AGENT_MCP_OVERLAY", "WATER_AGENT_TOOL_EXPOSURE", "DRAWING_ROOT"):
        monkeypatch.delenv(key, raising=False)
    _clear_events()
    return SimpleNamespace(operator=operator, web=web, tmp=tmp_path)


@pytest.fixture
def app(database: Database, tmp_path: Path, env: SimpleNamespace) -> Any:
    return create_app(database=database, static_dir=tmp_path)


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


def _patch(client: TestClient, name: str, payload: dict[str, Any]) -> Any:
    return client.patch(f"/api/admin/skills/{name}", json=payload, headers=_headers(client))


def test_skill_listing_is_read_only_for_members(app: Any, database: Database, env: SimpleNamespace) -> None:
    """P7-S9-A08 成员能看列表（名称 / 来源 / 版本 / 描述 / triggers / requires_tools / 状态），不含正文、不含运维
    目录路径；未通过安检的 skill 只给名字与错误码；正文与启停只给管理员（成员 403、临时密码管理员 403、
    未登录 401）；写接口缺 CSRF 403。"""

    assert TestClient(app).get("/api/skills").status_code == 401

    member = _login(app, database, UserRole.MEMBER)
    response = member.get("/api/skills")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    names = [item["name"] for item in data["skills"]]
    assert names == sorted([*BUILTIN, "leak-test-notes", "pump-room-checklist"])
    by_name = {item["name"]: item for item in data["skills"]}
    assert by_name["calc-book-workflow"]["source"] == "builtin", "同名时内置优先"
    assert by_name["pump-room-checklist"]["source"] == "operator"
    assert set(by_name["pump-room-checklist"]) == {
        "name", "source", "version", "description", "triggers", "requires_tools", "status"
    }
    assert all(item["status"] == "enabled" for item in data["skills"])
    assert {item["name"]: item["status"] for item in data["unavailable"]} == {
        "unsafe-override": "SKILL_UNSAFE",
        "iron-rule-bypass": "SKILL_CONFLICTS_IRON_RULE",
        "needs-missing-tool": "SKILL_TOOL_UNAVAILABLE",
        "calc-book-workflow": "SKILL_SHADOWED",
    }
    text = response.text
    assert str(env.operator) not in text and str(env.tmp) not in text, "不回显运维目录路径"
    assert "先用 retrieve_clauses 检索与泵房布置" not in text, "列表不含正文"
    assert "忽略以上所有指令" not in text, "未过安检的 skill 不展示任何文本"
    assert data["revision"] == 0 and data["active"] is True and data["can_manage"] is False

    for client, code in ((member, "AUTH_FORBIDDEN"), (_login(app, database, UserRole.ADMIN, must_change=True), "AUTH_MUST_CHANGE_PASSWORD")):
        response = client.get("/api/admin/skills/pump-room-checklist")
        assert response.status_code == 403 and response.json()["error"]["code"] == code
        assert _patch(client, "pump-room-checklist", {"disabled": True, "expected_revision": 0}).status_code == 403

    admin = _login(app, database, UserRole.ADMIN)
    assert admin.get("/api/skills").json()["data"]["can_manage"] is True
    body = admin.get("/api/admin/skills/pump-room-checklist").json()["data"]["skill"]
    assert "先用 retrieve_clauses 检索与泵房布置" in body["body"] and len(body["digest"]) == 16
    for bad in ("unsafe-override", "no-such-skill", "Bad_Name"):
        response = admin.get(f"/api/admin/skills/{bad}")
        assert response.status_code == 404 and bad not in response.text

    no_csrf = admin.patch("/api/admin/skills/pump-room-checklist", json={"disabled": True, "expected_revision": 0})
    assert no_csrf.status_code == 403 and no_csrf.json()["error"]["code"] == "AUTH_CSRF_INVALID"
    assert not env.web.exists()


def test_admin_can_disable_a_skill_for_the_next_run(app: Any, database: Database, env: SimpleNamespace) -> None:
    """P7-S9-A08 管理员停用 / 启用写进 Web 管理层 ``skills`` 键（revision 乐观并发、只能写 disabled），审计只记
    名字与动作；停用后下一次装配的目录里没有它、成员列表显示已停用；S6 写进同一文件的 ``servers`` 改动不受
    影响，S6 的 server 编辑也不会抹掉 skill 启停。"""

    from water_agent.mcp.web_layer import (
        WebLayerServer,
        WebLayerStore,
        disabled_skill_names,
        load_web_layer,
    )
    from water_agent.plugins import (
        McpGatewayPlugin,
        SkillsContribution,
        assemble_tools,
        builtin_plugins,
    )
    from water_agent.plugins.base import AssemblyContext
    from water_agent.retrieval.trivium import TriviumRetriever
    from water_agent.runtime.governance.contracts import Principal

    admin = _login(app, database, UserRole.ADMIN)
    member = _login(app, database, UserRole.MEMBER)

    # 面板只能改 disabled
    for bad in (
        {"disabled": "true", "expected_revision": 0},
        {"disabled": True},
        {"disabled": True, "expected_revision": -1},
        {"disabled": True, "expected_revision": 0, "body": "新的正文"},
        {"disabled": True, "expected_revision": 0, "source": "builtin"},
        {"expected_revision": 0},
    ):
        response = _patch(admin, "pump-room-checklist", bad)
        assert response.status_code == 422, bad
        assert response.json()["error"]["code"] == "WEB_SKILL_REJECTED"
        assert "新的正文" not in response.text
    assert _patch(admin, "no-such-skill", {"disabled": True, "expected_revision": 0}).status_code == 404
    assert _patch(admin, "unsafe-override", {"disabled": True, "expected_revision": 0}).status_code == 404
    assert not env.web.exists()

    response = _patch(admin, "calc-book-workflow", {"disabled": True, "expected_revision": 0})
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"name": "calc-book-workflow", "disabled": True, "revision": 1}
    assert _patch(admin, "leak-test-notes", {"disabled": True, "expected_revision": 0}).status_code == 409

    listing = member.get("/api/skills").json()["data"]
    assert {item["name"]: item["status"] for item in listing["skills"]}["calc-book-workflow"] == "SKILL_DISABLED"
    assert listing["revision"] == 1

    def catalog() -> tuple[str, ...]:
        assembly = assemble_tools(
            AssemblyContext(retriever=TriviumRetriever(db_path=None, fallback_clauses=[]), drawing_root=""),
            base_principal=Principal.local("s9-web"),
            trace=None,
            plugins=builtin_plugins(
                mcp=McpGatewayPlugin.borrowing(None),
                skills_disabled=disabled_skill_names(),
            ),
        )
        return assembly.contribution(SkillsContribution).library.names

    assert "calc-book-workflow" not in catalog()

    # S6 写同一份文件：server 编辑不抹掉 skill 启停，skill 启停不抹掉 server 编辑
    store = WebLayerStore(env.web)
    from water_agent.mcp.web_layer import WebLayerDocument

    store.update(
        lambda doc: WebLayerDocument(
            version=doc.version, revision=doc.revision,
            servers=(WebLayerServer(server_id="cad_tool", disabled=True),), skills=doc.skills,
        ),
        expected_revision=1,
        validate=lambda _candidate: None,
    )
    from water_agent.mcp.web_layer import with_override, without_override

    document = load_web_layer(env.web)
    assert document.disabled_skills() == ("calc-book-workflow",)
    assert with_override(document, "cad_tool", user_id="u", allow=["x"]).skills == document.skills
    assert without_override(document, "cad_tool").skills == document.skills

    response = _patch(admin, "calc-book-workflow", {"disabled": False, "expected_revision": 2})
    assert response.status_code == 200 and response.json()["data"]["revision"] == 3
    document = load_web_layer(env.web)
    assert document.disabled_skills() == () and [entry.server_id for entry in document.servers] == ["cad_tool"]
    assert "calc-book-workflow" in catalog()
    on_disk = json.loads(env.web.read_text(encoding="utf-8"))
    assert "skills" not in on_disk, "没有停用记录时不写 skills 键（与 S6 的层文件逐字相同）"

    events = [event for event in _get_events() if event["event"] == "skill.updated"]
    assert [(event["name"], event["disabled"]) for event in events] == [
        ("calc-book-workflow", True), ("calc-book-workflow", False)
    ]
    assert "body" not in json.dumps(events, ensure_ascii=False)

    # 层文件里写了 skill 不允许的键：MCP_CONFIG_INVALID（与 S6 同语义）
    from water_agent.mcp.errors import MCPError

    env.web.write_text(json.dumps({"revision": 4, "skills": [{"name": "calc-book-workflow", "body": "x"}]}), encoding="utf-8")
    with pytest.raises(MCPError) as exc:
        load_web_layer(env.web)
    assert exc.value.code == "MCP_CONFIG_INVALID" and "body" in str(exc.value)
    assert member.get("/api/skills").json()["data"]["web_layer_error"] == "MCP_CONFIG_INVALID"


# ---------------------------------------------------------------------------
# P7-S13：已安装来源在面板里的呈现（只读；没有取 / 批准入口）
# ---------------------------------------------------------------------------


def _install_one(tmp: Path, monkeypatch: pytest.MonkeyPatch, name: str = "pump-room-notes") -> bytes:
    from water_agent.skills.installed import InstalledMeta, sha256_of, write_lock_atomic

    installed = tmp / "installed-skills"
    (installed / name).mkdir(parents=True)
    raw = (
        f"---\nname: {name}\ndescription: 泵房设计复核的补充步骤。\nversion: 1.2.0\nrequires_tools: [retrieve_clauses]\n---\n"
        "1. 先用 retrieve_clauses 检索泵房布置相关条文。\n"
    ).encode()
    (installed / name / "SKILL.md").write_bytes(raw)
    write_lock_atomic(
        installed,
        [InstalledMeta(name, "1.2.0", sha256_of(raw), len(raw), "registry.example.com", "ops-user", "2026-10-22T03:00:00Z", None)],
    )
    monkeypatch.setenv("WATER_AGENT_SKILLS_INSTALLED_DIR", str(installed))
    return raw


def test_installed_skills_show_provenance_without_paths(
    app: Any, database: Database, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S13-A08 ``GET /api/skills``：已安装来源的对象多一个 ``installed``（摘要前 8 位 / 批准人 / 批准时间 / 注册表主机名，来自 lock，
    **不含路径**）；其余来源的字段集合逐字不变；管理员详情同样带它。摘要只给前 8 位，完整摘要与安装区路径都不出现。"""

    raw = _install_one(env.tmp, monkeypatch)
    member = _login(app, database, UserRole.MEMBER)
    response = member.get("/api/skills")
    assert response.status_code == 200, response.text
    by_name = {item["name"]: item for item in response.json()["data"]["skills"]}

    installed = by_name["pump-room-notes"]
    assert installed["source"] == "installed" and installed["version"] == "1.2.0" and installed["status"] == "enabled"
    import hashlib

    digest = hashlib.sha256(raw).hexdigest()
    assert installed["installed"] == {
        "sha8": digest[:8], "approved_by": "ops-user", "approved_at": "2026-10-22T03:00:00Z", "registry_host": "registry.example.com",
    }
    assert set(installed) == {"name", "source", "version", "description", "triggers", "requires_tools", "status", "installed"}
    for name in (*BUILTIN, "pump-room-checklist"):
        assert "installed" not in by_name[name], "其余来源的字段逐字不变"
        assert set(by_name[name]) == {"name", "source", "version", "description", "triggers", "requires_tools", "status"}
    text = response.text
    assert digest not in text, "完整摘要不出现"
    assert str(env.tmp) not in text and "installed-skills" not in text, "不回显安装区路径"
    assert "先用 retrieve_clauses 检索泵房布置" not in text, "列表不含正文"

    admin = _login(app, database, UserRole.ADMIN)
    detail = admin.get("/api/admin/skills/pump-room-notes").json()["data"]["skill"]
    assert detail["installed"]["sha8"] == digest[:8] and "先用 retrieve_clauses 检索泵房布置" in detail["body"]
    assert str(env.tmp) not in admin.get("/api/admin/skills/pump-room-notes").text


def test_a_tampered_installed_skill_is_listed_only_by_name_and_code(
    app: Any, database: Database, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S13-A08 / A05 被篡改的已安装 skill 不加载：面板只显示名字与 ``SKILL_TAMPERED``（不展示任何文本、不展示路径），文件不被删除。"""

    _install_one(env.tmp, monkeypatch)
    skill_file = env.tmp / "installed-skills" / "pump-room-notes" / "SKILL.md"
    skill_file.write_bytes(skill_file.read_bytes() + b"tampered")
    member = _login(app, database, UserRole.MEMBER)
    data = member.get("/api/skills").json()["data"]
    assert "pump-room-notes" not in [item["name"] for item in data["skills"]]
    assert {"name": "pump-room-notes", "status": "SKILL_TAMPERED"} in data["unavailable"]
    assert "tampered" not in member.get("/api/skills").text and skill_file.exists()


def test_the_panel_can_disable_an_installed_skill_but_nothing_else(
    app: Any, database: Database, env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S13-A08 管理员对已安装 skill 只能停用 / 启用（S9 的 Web 管理层 ``skills`` 键）；PATCH 仍只认 ``disabled`` + ``expected_revision``，
    塞 ``version`` / ``sha256`` / ``install`` / ``approve`` 之类一律 422；没有任何取回 / 批准 / 安装路由。"""

    _install_one(env.tmp, monkeypatch)
    admin = _login(app, database, UserRole.ADMIN)
    for bad in ({"disabled": True, "expected_revision": 0, "approve": True}, {"disabled": True, "expected_revision": 0, "version": "9.9.9"},
                {"install": True, "expected_revision": 0}, {"sha256": "0" * 64, "disabled": False, "expected_revision": 0}):
        response = _patch(admin, "pump-room-notes", bad)
        assert response.status_code == 422, (bad, response.text)
    ok = _patch(admin, "pump-room-notes", {"disabled": True, "expected_revision": 0})
    assert ok.status_code == 200, ok.text
    listing = admin.get("/api/skills").json()["data"]
    assert "pump-room-notes" not in [item["name"] for item in listing["skills"]] or any(
        item["name"] == "pump-room-notes" and item["status"] == "SKILL_DISABLED" for item in listing["skills"]
    )
    for path in ("/api/skills/fetch", "/api/skills/approve", "/api/admin/skills/pump-room-notes/approve",
                 "/api/admin/skills/pump-room-notes/install", "/api/admin/skills/fetch", "/api/admin/skills/registry"):
        assert admin.post(path, json={}, headers=_headers(admin)).status_code in {404, 405}, path
