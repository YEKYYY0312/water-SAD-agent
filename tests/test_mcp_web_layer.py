"""P7-S6：MCP 配置的第三层 Web 管理层（``docs/design/16-p7-plugin-mcp.md`` §7 S6 A01）。"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

from water_agent.mcp.config import (
    MCPGatewayConfig,
    load_layered_gateway_config,
    merge_web_layer,
    read_operator_layers,
)
from water_agent.mcp.errors import MCPError
from water_agent.mcp.web_layer import (
    WebLayerDocument,
    WebLayerRejected,
    WebLayerRevisionConflict,
    WebLayerStore,
    WebLayerStoreUnavailable,
    check_candidate,
    field_sources,
    load_web_layer,
    orphan_entries,
    with_override,
    without_override,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_SERVER = REPO_ROOT / "tests" / "fixtures" / "fake_mcp_server.py"


def _write(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _server(server_id: str, **overrides: object) -> dict:
    return {
        "server_id": server_id,
        "command": sys.executable,
        "args": [str(FAKE_SERVER), "--mode", "ok"],
        "allow": ["echo_query"],
        **overrides,
    }


@pytest.fixture
def layers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """三层都放在临时目录；返回 (默认层, 覆盖层, Web 管理层) 路径。"""

    base = tmp_path / "repo" / "mcp_servers.json"
    overlay = tmp_path / "deploy" / "mcp_overlay.json"
    web = tmp_path / "mcp-admin" / "web_layer.json"
    monkeypatch.setenv("WATER_AGENT_MCP_CONFIG", str(base))
    monkeypatch.setenv("WATER_AGENT_MCP_OVERLAY", str(overlay))
    monkeypatch.setenv("WATER_AGENT_MCP_WEB_LAYER", str(web))
    monkeypatch.delenv("WATER_AGENT_TOOL_EXPOSURE", raising=False)
    _write(base, {"servers": [_server("alpha", allow=["echo_query", "lookup_project"])]})
    _write(overlay, {"servers": [_server("beta")]})
    return base, overlay, web


def test_web_layer_can_only_toggle_and_allow(layers) -> None:
    """P7-S6-A01 Web 管理层每台 server 只能写 server_id / disabled / allow（外加 updated_at / updated_by
    元数据）；能 spawn 什么、在哪里跑、要不要确认一律写不了，顶层也只认 version / revision / servers。"""

    _base, _overlay, web = layers

    _write(web, {"servers": [{"server_id": "alpha", "disabled": True, "allow": ["lookup_project"],
                              "updated_at": "2026-10-07T09:00:00+00:00", "updated_by": "u-1"}]})
    config = load_layered_gateway_config()
    assert config is not None
    alpha = next(server for server in config.servers if server.server_id == "alpha")
    assert alpha.disabled is True and alpha.allow == ("lookup_project",)
    assert alpha.command == sys.executable, "命令只来自运维的两层"

    forbidden_fields = {
        "command": "/bin/sh",
        "args": ["-c", "id"],
        "env": {"LD_PRELOAD": "/tmp/x.so"},
        "env_file": "/etc/passwd",
        "cwd": "/",
        "lifecycle": "shared",
        "human_confirmation": False,
        "transport": "stdio",
        "startup_timeout_seconds": 1.0,
        "call_timeout_seconds": 1.0,
        "max_response_bytes": 1,
        "credential_env": ["CAD_API_TOKEN"],
    }
    for field, value in forbidden_fields.items():
        _write(web, {"servers": [{"server_id": "alpha", field: value}]})
        with pytest.raises(MCPError) as exc:
            load_layered_gateway_config()
        assert exc.value.code == "MCP_CONFIG_INVALID", field
        assert field in str(exc.value), field
        # 只列键名，不回显写进去的值
        for leaked in ("/bin/sh", "/tmp/x.so", "/etc/passwd", "CAD_API_TOKEN", "id"):
            assert f"'{leaked}'" not in str(exc.value), (field, leaked)

    for bad_top in (
        {"confirmation_handler_ready": True, "servers": []},
        {"confirmation_handler_ready": False, "servers": []},
        {"servers": [], "unknown_key": 1},
    ):
        _write(web, bad_top)
        with pytest.raises(MCPError) as exc:
            load_layered_gateway_config()
        assert exc.value.code == "MCP_CONFIG_INVALID", bad_top

    # 不能新增 server：下层没有的 id 即使写全了字段也只是被拒绝的写法，写成合法形状也只是孤立条目
    _write(web, {"servers": [{"server_id": "gamma", "command": "/bin/sh"}]})
    with pytest.raises(MCPError):
        load_layered_gateway_config()
    _write(web, {"servers": [{"server_id": "gamma", "disabled": False, "allow": ["x"]}]})
    config = load_layered_gateway_config()
    assert config is not None
    assert [server.server_id for server in config.servers] == ["alpha", "beta"]

    # disabled 只认 JSON 布尔：字符串 "false" 不是"启用"
    _write(web, {"servers": [{"server_id": "alpha", "disabled": "false"}]})
    with pytest.raises(MCPError) as exc:
        load_layered_gateway_config()
    assert exc.value.code == "MCP_CONFIG_INVALID"

    # 合并原语本身也只收这三个键
    with pytest.raises(ValueError, match="command"):
        merge_web_layer({"servers": [_server("alpha")]}, [{"server_id": "alpha", "command": "/bin/sh"}])


def test_web_layer_merges_last_and_fails_loudly(layers, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S6-A01 合并顺序 默认层 → 覆盖层 → Web 管理层（最后），allow 整体替换；未设变量 = 没有这一层，
    文件不存在 = 空层；坏 JSON / 坏形状 → MCP_CONFIG_INVALID；路径存在性在最终配置上检查。"""

    from water_agent.mcp.wiring import load_mcp_gateway

    _base, overlay, web = layers
    _write(overlay, {"servers": [{"server_id": "alpha", "allow": ["echo_query"], "disabled": True},
                                 _server("beta")]})

    # 文件不存在 = 空层：结果与只有两层时相同
    assert not web.exists()
    two_layers = load_layered_gateway_config()
    monkeypatch.delenv("WATER_AGENT_MCP_WEB_LAYER")
    assert load_layered_gateway_config() == two_layers, "未设变量 = 没有这一层"
    monkeypatch.setenv("WATER_AGENT_MCP_WEB_LAYER", str(web))
    assert two_layers is not None and two_layers.servers[0].disabled is True

    _write(web, {"servers": [{"server_id": "alpha", "disabled": False, "allow": ["lookup_project"]}]})
    config = load_mcp_gateway()
    assert config is not None
    alpha = config.servers[0]
    assert alpha.disabled is False, "Web 管理层在覆盖层之后合并"
    assert alpha.allow == ("lookup_project",), "allow 整体替换：不与覆盖层的 echo_query 合并"
    assert alpha.args == tuple(_server("alpha")["args"]), "没写的字段沿用下层"

    # allow: null 与覆盖层同规则：全拒
    _write(web, {"servers": [{"server_id": "alpha", "allow": None}]})
    config = load_layered_gateway_config()
    assert config is not None and config.servers[0].allow is None

    for corrupt in ("{ 这不是 JSON", "[]", json.dumps({"revision": -1, "servers": []}),
                    json.dumps({"revision": "3", "servers": []}),
                    json.dumps({"servers": [{"server_id": "alpha"}, {"server_id": "alpha"}]}),
                    json.dumps({"servers": [{"server_id": "Bad-Id"}]}),
                    json.dumps({"servers": [{"server_id": "alpha", "allow": ["a b"]}]})):
        web.write_text(corrupt, encoding="utf-8")
        with pytest.raises(MCPError) as exc:
            load_layered_gateway_config()
        assert exc.value.code == "MCP_CONFIG_INVALID", corrupt

    # 层文件路径是目录：同样响亮失败
    web.unlink()
    web.mkdir()
    with pytest.raises(MCPError) as exc:
        load_layered_gateway_config()
    assert exc.value.code == "MCP_CONFIG_INVALID"
    web.rmdir()

    # 路径存在性在最终配置上检查：Web 管理层启用一台 cwd 不存在的 server → 失败；
    # 停用一台 cwd 不存在的 server → 其余照常加载（面板能把坏掉的 server 关掉）
    missing_cwd = str(tmp_path / "gone")
    _write(overlay, {"servers": [_server("beta", cwd=missing_cwd, disabled=True)]})
    assert load_layered_gateway_config() is not None
    _write(web, {"servers": [{"server_id": "beta", "disabled": False}]})
    with pytest.raises(MCPError) as exc:
        load_layered_gateway_config()
    assert exc.value.code == "MCP_CONFIG_INVALID" and "cwd" in str(exc.value)

    _write(overlay, {"servers": [_server("beta", cwd=missing_cwd)]})
    monkeypatch.delenv("WATER_AGENT_MCP_WEB_LAYER")
    with pytest.raises(MCPError):
        load_layered_gateway_config()
    monkeypatch.setenv("WATER_AGENT_MCP_WEB_LAYER", str(web))
    _write(web, {"servers": [{"server_id": "beta", "disabled": True}]})
    rescued = load_layered_gateway_config()
    assert rescued is not None
    assert {server.server_id: server.disabled for server in rescued.servers} == {
        "alpha": False,
        "beta": True,
    }

    # 运维两层自己坏了：报错指向运维的文件，与 Web 管理层无关
    overlay.write_text("{ 坏", encoding="utf-8")
    with pytest.raises(MCPError) as exc:
        load_layered_gateway_config()
    assert str(overlay) in str(exc.value)


def test_orphan_entries_are_ignored_and_listed(layers, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S6-A01 下层已删掉的 server 在 Web 管理层里的条目加载时忽略（不报错、不复活），并能列出来清理；
    下面两层都没有时 Web 管理层不能凭空造出 server。"""

    base, overlay, web = layers
    _write(web, {"servers": [{"server_id": "ghost", "disabled": False, "allow": ["anything"]},
                             {"server_id": "beta", "disabled": True}]})

    config = load_layered_gateway_config()
    assert config is not None
    assert [server.server_id for server in config.servers] == ["alpha", "beta"]
    assert config.servers[1].disabled is True

    document = load_web_layer(web)
    orphans = orphan_entries(read_operator_layers(), document)
    assert [entry.server_id for entry in orphans] == ["ghost"]

    cleaned = without_override(document, "ghost")
    assert [entry.server_id for entry in cleaned.servers] == ["beta"]

    # 两层都没有：功能关闭，Web 管理层的条目全是孤立条目
    base.unlink()
    overlay.unlink()
    monkeypatch.delenv("WATER_AGENT_MCP_OVERLAY")
    assert load_layered_gateway_config() is None
    assert [entry.server_id for entry in orphan_entries(read_operator_layers(), document)] == [
        "ghost",
        "beta",
    ]
    # 但 Web 管理层坏了照样响亮失败
    web.write_text("{ 坏", encoding="utf-8")
    with pytest.raises(MCPError):
        load_layered_gateway_config()


def test_writes_are_atomic_and_revisioned(layers, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S6-A01 写入：revision 乐观并发（过期即拒）、合并后整份校验（不通过不落盘）、
    同目录临时文件 + os.replace（写到一半失败不留半个文件）、文件锁串行化；整层恢复删除文件。"""

    _base, _overlay, web = layers
    store = WebLayerStore(web)
    operator = read_operator_layers()

    def validate(candidate: WebLayerDocument) -> None:
        check_candidate(operator, candidate, web_path=web)

    assert store.read() == WebLayerDocument() and store.read().revision == 0

    first = store.update(
        lambda doc: with_override(doc, "alpha", user_id="u-1", disabled=True),
        expected_revision=0,
        validate=validate,
    )
    assert first.revision == 1
    on_disk = json.loads(web.read_text(encoding="utf-8"))
    assert on_disk["revision"] == 1
    assert on_disk["servers"][0]["server_id"] == "alpha"
    assert on_disk["servers"][0]["disabled"] is True
    assert "allow" not in on_disk["servers"][0], "没改的字段不写进层文件（继续沿用下层）"
    assert on_disk["servers"][0]["updated_by"] == "u-1" and on_disk["servers"][0]["updated_at"]
    assert field_sources(operator, store.read(), "alpha")["disabled"] == "web"
    assert field_sources(operator, store.read(), "alpha")["allow"] == "repo"
    assert field_sources(operator, store.read(), "beta")["command"] == "overlay"

    # 再改 allow：disabled 保留
    second = store.update(
        lambda doc: with_override(doc, "alpha", user_id="u-2", allow=["lookup_project"]),
        expected_revision=1,
        validate=validate,
    )
    assert second.entry("alpha").overrides() == {
        "server_id": "alpha",
        "disabled": True,
        "allow": ("lookup_project",),
    }

    before = web.read_text(encoding="utf-8")

    # revision 过期
    with pytest.raises(WebLayerRevisionConflict) as conflict:
        store.update(lambda doc: with_override(doc, "alpha", user_id="u-3", disabled=False),
                     expected_revision=1, validate=validate)
    assert conflict.value.current_revision == 2
    assert web.read_text(encoding="utf-8") == before

    # 合并后不合法（启用一台 cwd 不存在的 server）：拒绝且不落盘
    _write(layers[1], {"servers": [_server("beta", cwd=str(tmp_path / "gone"), disabled=True)]})
    operator = read_operator_layers()
    with pytest.raises(WebLayerRejected) as rejected:
        store.update(lambda doc: with_override(doc, "beta", user_id="u-3", disabled=False),
                     expected_revision=2, validate=validate)
    assert "cwd" in rejected.value.reason and str(tmp_path) not in rejected.value.reason
    assert web.read_text(encoding="utf-8") == before

    # 写到一半失败：原文件原样、不留临时文件
    def broken_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(WebLayerStoreUnavailable):
        store.update(lambda doc: with_override(doc, "alpha", user_id="u-4", disabled=False),
                     expected_revision=2, validate=validate)
    monkeypatch.undo()
    assert web.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in web.parent.iterdir()) == ["web_layer.json", "web_layer.json.lock"]

    # 文件锁串行化：并发写同一个 expected_revision，恰好一个成功
    outcomes: list[str] = []
    barrier = threading.Barrier(4)

    def attempt(index: int) -> None:
        barrier.wait()
        try:
            store.update(lambda doc: with_override(doc, "alpha", user_id=f"t-{index}", disabled=False),
                         expected_revision=2, validate=lambda _doc: None)
            outcomes.append("ok")
        except WebLayerRevisionConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["conflict", "conflict", "conflict", "ok"]
    assert store.read().revision == 3

    # 整层恢复：文件损坏时也可用；之后是空层
    web.write_text("{ 坏", encoding="utf-8")
    assert store.reset() is True
    assert not web.exists() and store.read().revision == 0
    assert store.reset() is False


def test_store_reports_an_unwritable_directory(tmp_path: Path) -> None:
    """P7-S6-A01 层文件目录不可写（未挂卷 / 只读根文件系统）：WebLayerStoreUnavailable，不留半个文件。

    用"父路径是个普通文件"模拟建不了目录——与 root 下目录权限不生效无关，任何用户都确定失败。
    """

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    store = WebLayerStore(blocker / "mcp-admin" / "web_layer.json")
    with pytest.raises(WebLayerStoreUnavailable):
        store.update(lambda doc: doc, expected_revision=0, validate=lambda _doc: None)
    with pytest.raises(WebLayerStoreUnavailable):
        store.reset()
    assert blocker.read_text(encoding="utf-8") == "x"


def test_default_skeleton_and_model_layer_are_unchanged_by_the_third_layer() -> None:
    """P7-S6-A01 第三层只叠在合并结果上：MCPGatewayConfig 本身仍不接受 revision / updated_by 这类键。"""

    with pytest.raises(ValueError):
        MCPGatewayConfig.model_validate({"revision": 1, "servers": []})


def test_skill_toggles_share_the_layer_without_touching_servers(layers) -> None:
    """P7-S9-A08 层文件的 ``skills`` 键只能写 name / disabled（+ 元数据），名字须 kebab-case、不得重复；
    启用即删掉这条、没有停用记录时不写 ``skills`` 键；skill 启停不改变 MCP 合并结果，server 编辑与
    整层恢复对 skills 的语义与 servers 相同（编辑保留、恢复清空）。"""

    from water_agent.mcp.web_layer import disabled_skill_names, with_skill_toggle

    _base, _overlay, web = layers
    for bad in (
        {"skills": {"calc-book-workflow": True}},
        {"skills": ["calc-book-workflow"]},
        {"skills": [{"name": "calc-book-workflow", "disabled": True, "body": "改写正文"}]},
        {"skills": [{"name": "calc-book-workflow", "source": "operator"}]},
        {"skills": [{"name": "Calc_Book", "disabled": True}]},
        {"skills": [{"name": "calc-book-workflow", "disabled": "yes"}]},
        {"skills": [{"name": "a-b", "disabled": True}, {"name": "a-b", "disabled": False}]},
    ):
        _write(web, {"revision": 1, **bad})
        with pytest.raises(MCPError) as exc:
            load_web_layer(web)
        assert exc.value.code == "MCP_CONFIG_INVALID", bad
        with pytest.raises(MCPError):
            disabled_skill_names()
    web.unlink()
    assert disabled_skill_names() == ()

    store = WebLayerStore(web)
    operator = read_operator_layers()

    def validate(candidate: WebLayerDocument) -> object:
        return check_candidate(operator, candidate, web_path=web)

    before = load_layered_gateway_config()
    document = store.update(
        lambda current: with_skill_toggle(current, "spec-conflict-handling", user_id="u1", disabled=True),
        expected_revision=0,
        validate=validate,
    )
    document = store.update(
        lambda current: with_skill_toggle(current, "calc-book-workflow", user_id="u1", disabled=True),
        expected_revision=1,
        validate=validate,
    )
    assert document.disabled_skills() == ("calc-book-workflow", "spec-conflict-handling"), "按名字排序"
    assert disabled_skill_names() == ("calc-book-workflow", "spec-conflict-handling")
    assert load_layered_gateway_config() == before, "skill 启停不改变 MCP 有效配置"
    on_disk = json.loads(web.read_text(encoding="utf-8"))
    assert on_disk["skills"][0]["updated_by"] == "u1" and on_disk["servers"] == []

    document = store.update(
        lambda current: with_override(current, "beta", user_id="u2", disabled=True),
        expected_revision=2,
        validate=validate,
    )
    assert document.disabled_skills() == ("calc-book-workflow", "spec-conflict-handling")
    document = store.update(
        lambda current: without_override(current, "beta"), expected_revision=3, validate=validate
    )
    assert document.disabled_skills() == ("calc-book-workflow", "spec-conflict-handling")

    for name in ("calc-book-workflow", "spec-conflict-handling"):
        document = store.update(
            lambda current, name=name: with_skill_toggle(current, name, user_id="u1", disabled=False),
            expected_revision=document.revision,
            validate=validate,
        )
    assert document.skills == ()
    assert "skills" not in json.loads(web.read_text(encoding="utf-8"))

    store.update(
        lambda current: with_skill_toggle(current, "calc-book-workflow", user_id="u1", disabled=True),
        expected_revision=document.revision,
        validate=validate,
    )
    assert store.reset() is True
    assert disabled_skill_names() == ()
