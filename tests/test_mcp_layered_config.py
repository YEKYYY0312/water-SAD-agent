"""P7-S3：MCP 配置分层、按实例启停与工作目录隔离（``docs/design/16-p7-plugin-mcp.md`` §7 S3）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from water_agent.mcp.config import MCPGatewayConfig, MCPServerConfig, load_gateway_config
from water_agent.mcp.errors import MCPError

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_SERVER = REPO_ROOT / "tests" / "fixtures" / "fake_mcp_server.py"


@pytest.fixture(autouse=True)
def _reset_bindings():
    from water_agent.runtime.governance.inventory import clear_mcp_tool_bindings

    clear_mcp_tool_bindings()
    yield
    clear_mcp_tool_bindings()


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _server(server_id: str = "layered", **overrides: object) -> dict:
    return {
        "server_id": server_id,
        "command": sys.executable,
        "args": [str(FAKE_SERVER), "--mode", "ok"],
        "allow": ["echo_query"],
        **overrides,
    }


# ---------------------------------------------------------------------------
# Step 1：配置模型 + 加载期校验
# ---------------------------------------------------------------------------


def test_server_config_validates_cwd_and_command(tmp_path: Path) -> None:
    """P7-S3-A01 新增 disabled / cwd；cwd 必须是绝对路径；command 只能是绝对路径或裸命令名。"""

    config = MCPServerConfig(server_id="srv", command="/bin/echo")
    assert config.disabled is False and config.cwd is None

    assert MCPServerConfig(server_id="srv", command="/bin/echo", cwd=str(tmp_path)).cwd == str(tmp_path)
    for relative in ("tools", "./tools", "../tools"):
        with pytest.raises(ValueError, match="cwd"):
            MCPServerConfig(server_id="srv", command="/bin/echo", cwd=relative)

    # 裸命令名按 PATH 查找，照常接受；含 / 的相对路径在子进程换工作目录后含义会变，拒绝
    assert MCPServerConfig(server_id="srv", command="python3").command == "python3"
    for relative_command in ("bin/server", "./server", "../venv/bin/python"):
        with pytest.raises(ValueError, match="command"):
            MCPServerConfig(server_id="srv", command=relative_command)

    assert MCPServerConfig(server_id="srv", command="/bin/echo", disabled=True).disabled is True


def test_env_file_and_cwd_resolved_and_checked_at_load(tmp_path: Path) -> None:
    """P7-S3-A02 F9：env_file 相对路径按配置文件所在目录解析；env_file / cwd 不存在在加载期即失败。"""

    from water_agent.mcp.config import child_env

    config_dir = tmp_path / "conf"
    (config_dir / "secrets").mkdir(parents=True)
    (config_dir / "secrets" / "server.env").write_text("TOKEN=from-file\n", encoding="utf-8")
    config_path = _write(
        config_dir / "mcp_servers.json",
        {"servers": [_server(env_file="secrets/server.env", cwd=str(tmp_path))]},
    )

    loaded = load_gateway_config(config_path)
    assert loaded is not None
    server = loaded.servers[0]
    # 解析成绝对路径后写进模型：之后无论在哪个工作目录连接，读到的都是同一个文件
    assert server.env_file == str((config_dir / "secrets" / "server.env").resolve())
    assert child_env(server)["TOKEN"] == "from-file"

    _write(config_path, {"servers": [_server(env_file="secrets/missing.env")]})
    with pytest.raises(MCPError) as missing_env:
        load_gateway_config(config_path)
    assert missing_env.value.code == "MCP_CONFIG_INVALID"
    assert "env_file" in str(missing_env.value)

    _write(config_path, {"servers": [_server(cwd=str(tmp_path / "no-such-dir"))]})
    with pytest.raises(MCPError) as missing_cwd:
        load_gateway_config(config_path)
    assert missing_cwd.value.code == "MCP_CONFIG_INVALID"
    assert "cwd" in str(missing_cwd.value)


def test_model_level_validation_does_not_touch_the_filesystem() -> None:
    """P7-S3-A01 模型层只做形状校验：示例文件里的占位路径在任何机器上都能通过模型校验。"""

    config = MCPGatewayConfig.model_validate(
        {
            "servers": [
                {
                    "server_id": "example",
                    "command": "/path/to/server/.venv/bin/python",
                    "cwd": "/path/to/server",
                    "env_file": "server.env",
                    "allow": ["read_dwg"],
                }
            ]
        }
    )
    assert config.servers[0].cwd == "/path/to/server"


# ---------------------------------------------------------------------------
# Step 2：配置分层
# ---------------------------------------------------------------------------


@pytest.fixture
def layers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """默认层与覆盖层都放在临时目录；返回 (默认层路径, 覆盖层路径, 加载函数)。"""

    from water_agent.mcp.config import load_layered_gateway_config

    base = tmp_path / "repo" / "mcp_servers.json"
    overlay = tmp_path / "deploy" / "mcp_overlay.json"
    monkeypatch.setenv("WATER_AGENT_MCP_CONFIG", str(base))
    monkeypatch.setenv("WATER_AGENT_MCP_OVERLAY", str(overlay))
    return base, overlay, load_layered_gateway_config


def test_overlay_merges_by_server_id(layers, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S3-A03 覆盖层按 server_id 合并：新增 server、逐字段覆盖、allow / args / env 整体替换。"""

    base, overlay, load = layers
    _write(base, {"servers": [_server("alpha", allow=["echo_query", "lookup_project"],
                                      env={"A": "1", "B": "2"})]})
    _write(overlay, {"servers": [
        {"server_id": "alpha", "allow": ["echo_query"], "env": {"C": "3"},
         "call_timeout_seconds": 5.0},
        _server("beta"),
    ]})

    config = load()
    assert config is not None
    alpha, beta = config.servers
    assert alpha.server_id == "alpha" and beta.server_id == "beta"
    assert alpha.allow == ("echo_query",), "allow 整体替换，不与默认层合并"
    assert alpha.env == {"C": "3"}, "env 整体替换，不与默认层合并"
    assert alpha.call_timeout_seconds == 5.0
    assert alpha.args == tuple(_server()["args"]), "覆盖层没写的字段沿用默认层"

    # 只有覆盖层、没有默认层：默认层按空骨架处理（镜像里本来就不带默认层文件）
    base.unlink()
    _write(overlay, {"servers": [_server("beta")]})
    only_overlay = load()
    assert only_overlay is not None
    assert [server.server_id for server in only_overlay.servers] == ["beta"]
    assert only_overlay.confirmation_handler_ready is False

    # 这时覆盖层里只写部分字段的 server 就成了"新增"——缺 command 必须响亮失败，
    # 不能悄悄拿一台不完整的 server 去装配
    _write(overlay, {"servers": [{"server_id": "alpha", "allow": ["echo_query"]}]})
    with pytest.raises(MCPError) as partial:
        load()
    assert partial.value.code == "MCP_CONFIG_INVALID"

    # 两层都没有（覆盖层环境变量未设、默认层文件不存在）：功能关闭。
    # 注意不是"设了覆盖层但文件被删"——那是配置错误，见 test_overlay_cannot_widen_the_gate
    overlay.unlink()
    monkeypatch.delenv("WATER_AGENT_MCP_OVERLAY")
    assert load() is None

    # 没设覆盖层环境变量：退化为单层加载
    _write(base, {"servers": [_server("alpha")]})
    single = load()
    assert single is not None and [s.server_id for s in single.servers] == ["alpha"]


def test_overlay_cannot_widen_the_gate(layers) -> None:
    """P7-S3-A04 覆盖层不能写 confirmation_handler_ready 或未知键；覆盖层路径给了却不存在 → 配置错误。"""

    base, overlay, load = layers
    _write(base, {"servers": [_server("alpha")]})

    for bad in (
        {"confirmation_handler_ready": True, "servers": []},
        {"confirmation_handler_ready": False, "servers": []},
        {"servers": [], "unknown_key": 1},
        {"servers": [{"allow": ["echo_query"]}]},
        {"servers": [{"server_id": "alpha", "no_such_field": 1}]},
    ):
        _write(overlay, bad)
        with pytest.raises(MCPError) as exc:
            load()
        assert exc.value.code == "MCP_CONFIG_INVALID", bad

    overlay.unlink()
    import os

    os.environ["WATER_AGENT_MCP_OVERLAY"] = str(overlay)
    with pytest.raises(MCPError) as missing:
        load()
    assert missing.value.code == "MCP_CONFIG_INVALID"
    assert "覆盖层" in str(missing.value)


def test_overlay_env_file_resolves_against_the_overlay_directory(layers, tmp_path: Path) -> None:
    """P7-S3-A02 覆盖层里写的相对 env_file 按**覆盖层文件**所在目录解析，不按默认层目录。"""

    base, overlay, load = layers
    _write(base, {"servers": [_server("alpha")]})
    overlay.parent.mkdir(parents=True, exist_ok=True)
    (overlay.parent / "alpha.env").write_text("TOKEN=overlay\n", encoding="utf-8")
    _write(overlay, {"servers": [{"server_id": "alpha", "env_file": "alpha.env"}]})

    config = load()
    assert config is not None
    assert config.servers[0].env_file == str((overlay.parent / "alpha.env").resolve())


def test_load_mcp_gateway_uses_the_layered_loader(layers) -> None:
    """P7-S3-A03 会话 / runner / Web 能力端点共用的 ``load_mcp_gateway`` 读的是合并后的配置。"""

    from water_agent.mcp.wiring import load_mcp_gateway

    base, overlay, _load = layers
    _write(base, {"servers": [_server("alpha")]})
    _write(overlay, {"servers": [_server("beta")]})
    config = load_mcp_gateway()
    assert config is not None
    assert [server.server_id for server in config.servers] == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# Step 3：停用 + 子进程工作目录隔离
# ---------------------------------------------------------------------------


def test_disabled_server_is_skipped_and_excluded_from_manifest(tmp_path: Path) -> None:
    """P7-S3-A05 停用的 server 不 spawn、不暴露、记 MCP_SERVER_DISABLED、不进 manifest。"""

    from water_agent.mcp.wiring import compute_manifest_hash, open_gateway_sessions

    marker = tmp_path / "spawned"
    spawn_probe = {
        "server_id": "probe",
        "command": sys.executable,
        "args": ["-c", f"open({str(marker)!r}, 'w').close()"],
        "allow": ["anything"],
    }
    enabled = MCPGatewayConfig.model_validate({"servers": [_server("alpha")]})
    with_disabled = MCPGatewayConfig.model_validate(
        {"servers": [_server("alpha"), {**spawn_probe, "disabled": True}]}
    )
    assert compute_manifest_hash(with_disabled) == compute_manifest_hash(enabled), (
        "停用的 server 不得进 manifest：停用一台 server 应让缓存键回到没有它的状态"
    )
    all_disabled = MCPGatewayConfig.model_validate({"servers": [{**spawn_probe, "disabled": True}]})
    assert compute_manifest_hash(all_disabled) == ""

    # 确认门判定之后才轮到停用会报错误的码；停用先判，不报 MCP_CONFIRMATION_UNAVAILABLE
    confirm_and_disabled = MCPGatewayConfig.model_validate(
        {"servers": [{**spawn_probe, "disabled": True, "human_confirmation": True}]}
    )
    for config in (all_disabled, confirm_and_disabled):
        assembly = open_gateway_sessions(config)
        try:
            assert assembly.exposed_tool_names == ()
            assert [(s.server_id, s.code) for s in assembly.skipped_servers] == [
                ("probe", "MCP_SERVER_DISABLED")
            ]
        finally:
            assembly.close()
    assert not marker.exists(), "停用的 server 不得被 spawn"


def _cwd_config(server_id: str, cwd: str | None = None) -> MCPGatewayConfig:
    server = {
        "server_id": server_id,
        "command": sys.executable,
        "args": [str(FAKE_SERVER), "--mode", "cwd"],
        "allow": ["read_relative"],
    }
    if cwd is not None:
        server["cwd"] = cwd
    return MCPGatewayConfig.model_validate({"servers": [server]})


def _read_relative(assembly, server_id: str, path: str) -> dict:
    from water_agent.runtime.governance.contracts import (
        DEFAULT_LOCAL_CAPABILITIES,
        Capability,
        Principal,
    )
    from water_agent.tools.registry import ToolRegistry

    registry = ToolRegistry(
        principal=Principal.local(
            "cwd-test", capabilities=DEFAULT_LOCAL_CAPABILITIES | {Capability.MCP_INVOKE}
        )
    )
    for session in assembly.sessions:
        for tool in session.result.tools:
            registry.register(tool)
    result = registry.invoke(f"mcp_{server_id}_read_relative", path=path)
    assert result.get("ok") is True, result
    return dict(result["structured_content"])


def test_child_runs_in_isolated_or_configured_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S3-A06 F15 / F16：未配 cwd 时子进程在新建的空临时目录里运行，相对路径读不到 agent
    的文件，连接关闭后目录被删；配了 cwd 就在那里运行。

    测试进程的工作目录钉在仓库根，那里有 ``pyproject.toml``：子进程若继承了工作目录（旧行为），
    相对路径 ``pyproject.toml`` 就会存在。
    """

    from water_agent.mcp.wiring import open_gateway_sessions

    monkeypatch.chdir(REPO_ROOT)
    assert (REPO_ROOT / "pyproject.toml").is_file()

    isolated = open_gateway_sessions(_cwd_config("cwdiso"))
    try:
        report = _read_relative(isolated, "cwdiso", "pyproject.toml")
        child_cwd = Path(report["cwd"])
        assert report["exists"] is False, "子进程能按相对路径读到 agent 工作目录里的文件（F15）"
        assert child_cwd.resolve() != REPO_ROOT.resolve()
        assert child_cwd.name.startswith("wa-mcp-")
        assert child_cwd.is_dir() and not any(child_cwd.iterdir())
    finally:
        isolated.close()
    assert not child_cwd.exists(), "连接关闭后临时工作目录应被删除"

    configured = open_gateway_sessions(_cwd_config("cwdcfg", cwd=str(REPO_ROOT)))
    try:
        report = _read_relative(configured, "cwdcfg", "pyproject.toml")
        assert Path(report["cwd"]).resolve() == REPO_ROOT.resolve()
        assert report["exists"] is True
    finally:
        configured.close()
    assert REPO_ROOT.is_dir(), "配置的 cwd 不得在关闭时被删除"


def test_temporary_cwd_is_removed_when_the_server_fails_to_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P7-S3-A06 启动失败（命令不存在）时，已经建好的临时工作目录同样被删除。"""

    import tempfile

    from water_agent.mcp.wiring import open_gateway_sessions

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    config = MCPGatewayConfig.model_validate(
        {"servers": [{"server_id": "broken", "command": str(tmp_path / "no-such-binary"),
                      "allow": ["x"]}]}
    )
    assembly = open_gateway_sessions(config)
    try:
        assert [(s.server_id, s.code) for s in assembly.skipped_servers] == [
            ("broken", "MCP_CONNECT_FAILED")
        ]
    finally:
        assembly.close()
    assert not any(scratch.iterdir()), "启动失败后不得残留临时工作目录"


# ---------------------------------------------------------------------------
# Step 4：装配摘要写进 trace
# ---------------------------------------------------------------------------


def _fake_config_file(tmp_path: Path, server_id: str) -> Path:
    return _write(tmp_path / "mcp_servers.json", {"servers": [_server(server_id)]})


def _session_args(tmp_path: Path):
    import argparse

    return argparse.Namespace(
        db_path=None, dimensions=None, rerank="local", rerank_model=None, rerank_base_url=None,
        rerank_api_key=None, top_k=5, recall_top_k=20, hybrid_alpha=0.7, model="deepseek-chat",
        base_url="https://api.deepseek.com", api_key_env="DEEPSEEK_API_KEY", api_key=None,
        mock_generator=True, session_dir=str(tmp_path / "s"), trace_dir=str(tmp_path / "t"),
        stream=False, drawing_root="",
    )


def _mcp_events(document: dict) -> list[dict]:
    return [event for event in document["events"] if event["node_name"] == "mcp"]


def test_assembly_summary_lands_in_trace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S3-A08 会话的 trace 里确有 mcp_assembly 事件，内容不含 detail 文本；没有 MCP 配置时不写。

    同步 ``ask()`` 路径此前不调 ``start_trace``，trace 由图的首个节点开启——装配摘要必须在
    trace 开始之后补记，否则就是 F13 的重演（写入时 recorder 尚未 start，事件被吞）。
    """

    from contextlib import ExitStack

    from langgraph.checkpoint.memory import MemorySaver

    from water_agent.runtime.agentic import AgenticSession

    monkeypatch.delenv("WATER_AGENT_MCP_OVERLAY", raising=False)
    monkeypatch.setenv("WATER_AGENT_MCP_CONFIG", str(_fake_config_file(tmp_path, "tracesum")))
    with ExitStack() as stack:
        session = AgenticSession(_session_args(tmp_path), stack=stack, thread_id="trace-summary",
                                 checkpointer=MemorySaver())
        session.ask("给水管道设计秒流量")
        document = session.recorder.snapshot()
    assert document is not None
    events = _mcp_events(document)
    assert [event["event_type"] for event in events] == ["mcp_assembly"]
    data = events[0]["data"]
    assert data["mcp_servers"] == [
        {"server_id": "tracesum", "exposed": ["mcp_tracesum_echo_query"],
         # 保持 server tools/list 的原始顺序（确定的），不排序
         "skipped": [["lookup_project", "MCP_NOT_IN_ALLOWLIST"],
                     ["dangerous_write", "MCP_NOT_IN_ALLOWLIST"],
                     ["nested_schema_tool", "MCP_NOT_IN_ALLOWLIST"],
                     ["unlisted_tool", "MCP_NOT_IN_ALLOWLIST"]],
         # P7-S5：会话路径（CLI / runner）始终现开现关
         "connection": "per_run"}
    ]
    assert data["mcp_manifest_nonempty"] is True
    assert "detail" not in json.dumps(data, ensure_ascii=False)

    # 没有任何 MCP 配置：不写这条事件（不给每个 run 加噪声）
    monkeypatch.setenv("WATER_AGENT_MCP_CONFIG", str(tmp_path / "absent.json"))
    with ExitStack() as stack:
        quiet = AgenticSession(_session_args(tmp_path), stack=stack, thread_id="trace-quiet",
                               checkpointer=MemorySaver())
        quiet.ask("给水管道设计秒流量")
        quiet_document = quiet.recorder.snapshot()
    assert quiet_document is not None
    assert _mcp_events(quiet_document) == []


def test_runner_trace_records_assembly_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S3-A08 runner real-agentic 路径同样把装配摘要写进本用例的 trace。"""

    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from water_agent.domain.models import EvalCase
    from water_agent.harness.runner import HarnessRunner
    from water_agent.retrieval.loader import load_clauses_jsonl
    from water_agent.retrieval.trivium import TriviumRetriever

    fixture = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"
    monkeypatch.delenv("WATER_AGENT_MCP_OVERLAY", raising=False)
    monkeypatch.setenv("WATER_AGENT_MCP_CONFIG", str(_fake_config_file(tmp_path, "runsum")))
    eval_path = tmp_path / "unused.jsonl"
    eval_path.write_text("", encoding="utf-8")
    runner = HarnessRunner(eval_path=eval_path, report_dir=tmp_path / "r",
                           trace_dir=tmp_path / "traces", fixture_path=fixture)
    runner._real_retriever = TriviumRetriever(db_path=None, fallback_clauses=load_clauses_jsonl(fixture))
    runner._real_chat_model = FakeMessagesListChatModel(responses=[AIMessage(content="无需检索的寒暄。")])

    runner._run_real_agentic_case(EvalCase(id="runsum", query="你好", variant="real"))

    traces = list((tmp_path / "traces").glob("*.json"))
    assert len(traces) == 1
    document = json.loads(traces[0].read_text(encoding="utf-8"))
    events = _mcp_events(document)
    assert [event["event_type"] for event in events] == ["mcp_assembly"]
    assert events[0]["data"]["mcp_servers"][0]["server_id"] == "runsum"
