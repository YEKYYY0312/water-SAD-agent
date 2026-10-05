"""P7 规格事实表（``docs/design/16-p7-plugin-mcp.md`` §2）里的数字由这里锁住。

文档不手抄数字：AGENTS.md 曾长期写着"工具 schema 4370/4500、余量 130"，实测早已是 4497、
余量 3。这里的断言全部读入仓的装配快照基线（基线与代码的一致性由
``tests/test_tool_assembly_snapshot.py`` 保证），所以数字一旦变化，这里与文档必须同步改。

其中 F5/F6 是**已知缺陷的现状**，不是期望行为。后续步骤有意修复它们时，在同一个
commit 里同步改这里、规格事实表与快照基线（各步执行 Prompt 的范围块允许改本文件）。
F7 已在 P7-S1 修复，F2（runner 与会话路径的差异）与 F13（从未落盘的 trace 调用）已在
P7-S2 收尾，对应测试改为断言修复后的事实。
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


def test_tool_guide_already_truncates_tools(profiles: dict[str, Any]) -> None:
    """P7-S0-A13 F5：工具指引 800 字预算已截掉 read_drawing；两个 MCP 工具时截掉 2 行。

    MCP 工具排在本地工具之后注册，所以总是先被截掉；它们的 schema 也不计入
    ``tests/test_answer_cache.py`` 的 4500 预算（那里只装配本地工具）。
    """

    drawing = profiles["session_drawing_arg"]["surface"]
    assert drawing["tool_guide"].endswith(_TRUNCATED.format(n=1))
    assert "read_drawing" not in drawing["tool_guide"]

    mcp = profiles["session_mcp_fake"]["surface"]
    assert mcp["tool_guide"].endswith(_TRUNCATED.format(n=2))
    assert not any(line.startswith("- mcp_") for line in mcp["tool_guide"].splitlines())
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
    """P7-S1-A03 F7 已修：confirmation_handler_ready=true 在加载期即被拒绝，装配不再发生。

    S0 时这个 profile 的现状是"要求人工确认的工具直接暴露并可调用"；现在会话在加载配置时
    就以 MCP_CONFIG_INVALID 失败，不 spawn 任何 server、不授权、不构图。
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
