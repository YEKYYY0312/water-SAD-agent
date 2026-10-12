"""P7 规格事实表（``docs/design/16-p7-plugin-mcp.md`` §2）里的数字由这里锁住。

文档不手抄数字：AGENTS.md 曾长期写着"工具 schema 4370/4500、余量 130"，实测早已是 4497、
余量 3。这里的断言全部读入仓的装配快照基线（基线与代码的一致性由
``tests/test_tool_assembly_snapshot.py`` 保证），所以数字一旦变化，这里与文档必须同步改。

F5/F6 曾是**已知缺陷的现状**。后续步骤有意修复时，在同一个 commit 里同步改这里、规格事实表
与快照基线（各步执行 Prompt 的范围块允许改本文件）。F7 已在 P7-S1 修复，F2（runner 与会话路径
的差异）与 F13（从未落盘的 trace 调用）已在 P7-S2 收尾，F6 在 P7-S3、F5 在 P7-S4 修复，对应测试
改为断言修复后的事实。F4 的全量数字（4497 / 余量 3）S4 之后仍然成立：S4 约束的是每轮实际发出
的 schema，不是注册集。
"""
from __future__ import annotations

from typing import Any

import pytest

from water_agent.harness import tool_assembly_snapshot as snap

_TRUNCATED = "…（受预算限制省略 {n} 行）"


@pytest.fixture(scope="module")
def profiles() -> dict[str, Any]:
    return snap.load_snapshot(snap.DEFAULT_BASELINE)["profiles"]


def _caps(view: dict[str, Any] | None) -> set[str]:
    return set((view or {}).get("capabilities", []))


def test_local_tool_schema_headroom_is_three_characters(profiles: dict[str, Any]) -> None:
    """P7-S0-A12 F4：5 个本地工具的 schema 共 4497 字符，离 4500 的上限只剩 3。"""

    surface = profiles["session_drawing_arg"]["surface"]
    assert surface["schema_chars_total"] == 4497
    assert surface["tools"]["read_drawing"]["schema_chars"] == 956
    assert 4500 - surface["schema_chars_total"] == 3


def test_exposed_tools_always_fit_the_tool_guide(profiles: dict[str, Any]) -> None:
    """P7-S4-A11 F5 已修（intent 策略）：每条暴露探针开放的工具都写进了指引、指引未截断、
    发出的 schema ≤ 4500；预算不够时 MCP 整组让位并如实记进 dropped。

    S0 时的现状（``full`` 回滚开关下仍然如此）：现行格式的指引 800 字预算截掉 read_drawing，
    两个 MCP 工具时截掉 2 行、schema 4724 字符。回退格式的 ``surface/tool_guide`` 保留原样，
    这里同时锁住它仍截断——证明回滚开关回到的确实是 S3 的行为。
    """

    checked = 0
    for name, profile in profiles.items():
        surface = profile["surface"]
        if surface is None:
            continue
        exposure = surface["exposure"]
        assert exposure["strategy"] == "intent", name
        for label, probe in exposure["probes"].items():
            assert probe["schema_chars"] <= 4500, (name, label)
            assert probe["guide"]["chars"] <= 800, (name, label)
            assert {"retrieve_clauses", "cite_clauses", "calc_velocity"} <= set(probe["exposed"])
            checked += 1
    assert checked == 48  # 12 个装配成功的 profile × 4 条探针（P7-S9 新增 session_skills_builtin，P7-S8 新增 session_http_mcp_fake）

    drawing = profiles["session_drawing_arg"]["surface"]
    both = drawing["exposure"]["probes"]["drawing_materials"]
    assert {"material_stats", "read_drawing"} <= set(both["exposed"])
    assert both["dropped"] == [] and both["schema_chars"] == 4497

    mcp = profiles["session_mcp_fake"]["surface"]
    assert mcp["exposure"]["probes"]["materials"]["dropped"] == [["mcp:snapfake", "schema_budget"]]
    assert "mcp_snapfake_echo_query" in mcp["exposure"]["probes"]["clause_qa"]["exposed"]

    # 回退格式（full 回滚开关 / 没有暴露决定时）仍是 S0 的现状
    assert drawing["tool_guide"].endswith(_TRUNCATED.format(n=1))
    assert "read_drawing" not in drawing["tool_guide"]
    assert mcp["tool_guide"].endswith(_TRUNCATED.format(n=2))
    assert mcp["schema_chars_total"] == 4724


def test_mcp_manifest_reaches_the_cache_service(profiles: dict[str, Any]) -> None:
    """P7-S3-A07 F6 已修：Web 装配暴露了 MCP 工具时，缓存服务的 mcp_server_manifest 非空。

    S0 时它恒为空串（``_build_cache_service`` 不传），MCP server 清单变了旧回答缓存照样命中。
    """

    web = profiles["session_web_member"]
    assert web["lifecycle"]["mcp"][0]["exposed"]
    assert web["lifecycle"]["mcp"][0]["manifest_hash_nonempty"] is True
    assert web["lifecycle"]["app"]["cache_service"]["mcp_server_manifest_nonempty"] is True


def test_confirmation_gate_is_enforced(profiles: dict[str, Any]) -> None:
    """P7-S1-A03 / P7-S7-A09 F7 的最终形态：配置不能声明"可确认"，确认门是否可用由入口决定。

    S0 时 ``session_mcp_ready_flag`` 的现状是"要求人工确认的工具直接暴露并可调用"；S1 起会话在加载
    配置时就以 MCP_CONFIG_INVALID 失败，不 spawn 任何 server、不授权、不构图。快照的会话 profile 走 CLI
    路径（没有确认门），``session_mcp_confirm_skip`` 仍整台跳过。

    S7：Web 入口（连接池租约声明 ``confirmation_ready``、装配借用租约）下，要求确认的工具暴露为
    ``return_direct`` 延迟工具；暴露不是授权——不经确认门直接执行仍被 ``tool.confirmation_required`` 拒绝。
    """

    ready = profiles["session_mcp_ready_flag"]
    assert ready["surface"] is None
    assert ready["lifecycle"]["captures"] == 0
    assert ready["lifecycle"]["exception"] == {"type": "MCPError", "code": "MCP_CONFIG_INVALID"}
    assert ready["lifecycle"]["mcp"] == []

    skipped = profiles["session_mcp_confirm_skip"]
    assert skipped["lifecycle"]["mcp"][0]["skipped_servers"] == [
        ["snapconfirm", "MCP_CONFIRMATION_UNAVAILABLE"]
    ]
    assert "mcp_invoke" not in _caps(skipped["surface"]["principal"])

    import sys
    from pathlib import Path

    from water_agent.mcp.config import MCP_CONFIG_VERSION, MCPGatewayConfig
    from water_agent.mcp.pool import McpConnectionPool
    from water_agent.plugins import AssemblyContext, McpGatewayPlugin, assemble_tools
    from water_agent.retrieval.trivium import TriviumRetriever
    from water_agent.runtime.governance.contracts import Principal
    from water_agent.runtime.governance.pipeline import GuardrailPipeline

    fake = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
    config = MCPGatewayConfig.model_validate(
        {
            "version": MCP_CONFIG_VERSION,
            "servers": [
                {
                    "server_id": "f7web",
                    "command": sys.executable,
                    "args": [str(fake), "--mode", "stateful"],
                    "allow": ["bump"],
                    "human_confirmation": True,
                }
            ],
        }
    )
    pool = McpConnectionPool()
    try:
        cli_like = pool.lease(config)
        assert [(s.server_id, s.code) for s in cli_like.assembly.skipped_servers] == [
            ("f7web", "MCP_CONFIRMATION_UNAVAILABLE")
        ]
        cli_like.release()

        lease = pool.lease(config, confirmation_ready=True)
        try:
            assembly = assemble_tools(
                AssemblyContext(retriever=TriviumRetriever(db_path=None, fallback_clauses=[])),
                base_principal=Principal.local("f7-web"),
                trace=None,
                plugins=[McpGatewayPlugin.borrowing(lease.assembly)],
            )
            assert assembly.confirmation_tools == frozenset({"mcp_f7web_bump"})
            tool = next(item for item in assembly.tools if item.name == "mcp_f7web_bump")
            assert tool.return_direct is True
            assert "mcp_invoke" in {cap.value for cap in assembly.principal.capabilities}
            outcome = GuardrailPipeline().validate_tool_call("mcp_f7web_bump", {}, assembly.principal)
            assert outcome.decision.rule_id == "tool.confirmation_required"
            assert assembly.registry.invoke("mcp_f7web_bump")["code"] == "GUARDRAIL_BLOCKED"
        finally:
            lease.release()
    finally:
        pool.close()


def test_no_assembly_trace_calls_that_never_land(profiles: dict[str, Any]) -> None:
    """P7-S2-A12 / P7-S3-A11 F13 已收尾：装配期的每一次 trace 调用都真正落盘。

    S0 时 runner 在 recorder 尚未 start 时写 ``mcp_assembly`` / ``mcp_assembly_error``，
    异常被吞，事件一条都没落盘；S2 删除了这两次调用；S3 改为先开 trace 再写装配摘要。
    有 MCP 活动的 runner profile 必须真的写了 ``mcp_assembly``。
    """

    for name, profile in profiles.items():
        for call in profile["lifecycle"]["trace_record_calls"]:
            assert call["landed"], (name, call)
    runner_fake = profiles["runner_mcp_fake"]["lifecycle"]["trace_record_calls"]
    assert {"node": "mcp", "event": "mcp_assembly", "landed": True} in runner_fake


def test_runner_path_matches_session_semantics(profiles: dict[str, Any]) -> None:
    """P7-S2-A07 F2 已统一：runner real-agentic 路径与会话路径同语义（规格 §3.5 第 1–7 条）。"""

    # 第 3 条：MCP 每条用例现开现关，作用域结束后不留子进程（与会话路径相同）
    for name in ("runner_mcp_fake", "session_mcp_fake"):
        assert profiles[name]["lifecycle"]["mcp_clients_alive_after_scope"] == 0, name
    # 第 4 条：注册故障响亮失败，两条路径同一种异常，不再降级为纯本地工具
    for name in ("runner_mcp_register_fault", "session_mcp_register_fault"):
        lifecycle = profiles[name]["lifecycle"]
        assert lifecycle["exception"] == {"type": "ValueError", "code": None}, name
        assert profiles[name]["surface"] is None, name
        assert lifecycle["mcp_clients_alive_after_scope"] == 0, name
    # 第 6 条：chat model 拿到的是授权后的主体，与交给图的主体一致
    runner_fake = profiles["runner_mcp_fake"]
    assert "mcp_invoke" in _caps(runner_fake["surface"]["principal"])
    assert _caps(runner_fake["lifecycle"]["app"]["chat_model_principal"]) == _caps(
        runner_fake["surface"]["principal"]
    )
    # 两条路径的模型可见面一致（同一份 fake MCP 配置，只是 server id 不同）
    session_fake = profiles["session_mcp_fake"]["surface"]
    assert [n.replace("snaprfake", "X") for n in runner_fake["surface"]["registration_order"]] == [
        n.replace("snapfake", "X") for n in session_fake["registration_order"]
    ]
    # 第 8 条（刻意保留）：runner 显式传 max_tool_turns、不接回答缓存
    assert profiles["runner_no_mcp"]["lifecycle"]["app"]["max_tool_turns"] == 10
    assert profiles["runner_no_mcp"]["lifecycle"]["app"]["cache_service"] is None
    assert profiles["session_web_member"]["lifecycle"]["app"]["cache_service"] is not None


def test_shared_mcp_servers_survive_across_runs(tmp_path) -> None:
    """P7-S5-A09 F8 已修（对声明 ``lifecycle: shared`` 的 server）：Web 连接池里跨 run 复用同一个
    进程；缺省 ``per_run`` 的 server 仍每个 run 重开（S0 时的现状，刻意保留为缺省）。

    直接断言池的行为，不读快照：快照的捕获点在 ``build_agentic_app``，看不到跨 run 的进程身份。
    """

    import sys
    from pathlib import Path

    from water_agent.mcp.config import MCP_CONFIG_VERSION, MCPGatewayConfig
    from water_agent.mcp.pool import McpConnectionPool

    fake = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"

    def server(server_id: str, lifecycle: str) -> dict[str, Any]:
        return {
            "server_id": server_id,
            "command": sys.executable,
            "args": [str(fake), "--mode", "stateful"],
            "allow": ["whoami"],
            "lifecycle": lifecycle,
        }

    config = MCPGatewayConfig.model_validate(
        {"version": MCP_CONFIG_VERSION, "servers": [server("f8shared", "shared"), server("f8run", "per_run")]}
    )

    def pids(lease: Any) -> dict[str, int]:
        return {
            session.config.server_id: session.client.call_tool("whoami", {})["structured_content"]["pid"]
            for session in lease.assembly.sessions
        }

    pool = McpConnectionPool()
    try:
        runs = []
        for _ in range(2):
            lease = pool.lease(config)
            runs.append(pids(lease))
            lease.release()
        assert runs[0]["f8shared"] == runs[1]["f8shared"]
        assert runs[0]["f8run"] != runs[1]["f8run"]
    finally:
        pool.close()
