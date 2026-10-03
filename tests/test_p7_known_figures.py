"""P7 规格事实表（``docs/design/16-p7-plugin-mcp.md`` §2）里的数字由这里锁住。

文档不手抄数字：AGENTS.md 曾长期写着"工具 schema 4370/4500、余量 130"，实测早已是 4497、
余量 3。这里的断言全部读入仓的装配快照基线（基线与代码的一致性由
``tests/test_tool_assembly_snapshot.py`` 保证），所以数字一旦变化，这里与文档必须同步改。

其中 F5/F6/F7/F13 与 runner 差异是**已知缺陷的现状**，不是期望行为。后续步骤有意修复
它们时，在同一个 commit 里同步改这里、规格事实表与快照基线（各步执行 Prompt 的范围块
允许改本文件）。
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


def test_mcp_manifest_never_reaches_the_cache_service(profiles: dict[str, Any]) -> None:
    """P7-S0-A14 F6：Web 装配暴露了 MCP 工具，缓存服务的 mcp_server_manifest 仍是空串。"""

    web = profiles["session_web_member"]
    assert web["lifecycle"]["mcp"][0]["exposed"]
    assert web["lifecycle"]["mcp"][0]["manifest_hash_nonempty"] is True
    assert web["lifecycle"]["app"]["cache_service"]["mcp_server_manifest"] == ""


def test_confirmation_flag_is_not_enforced_at_call_time(profiles: dict[str, Any]) -> None:
    """P7-S0-A15 F7：confirmation_handler_ready=true 时，要求人工确认的工具直接暴露并可调用。"""

    ready = profiles["session_mcp_ready_flag"]
    assert ready["lifecycle"]["mcp"][0]["exposed"]
    assert "mcp_invoke" in _caps(ready["surface"]["registry_principal"])
    echo = ready["lifecycle"]["app"]["probes"]["mcp_echo"]["mcp_snapready_echo_query"]
    assert echo["error"] is False

    skipped = profiles["session_mcp_confirm_skip"]
    assert skipped["lifecycle"]["mcp"][0]["skipped_servers"] == [
        ["snapconfirm", "MCP_CONFIRMATION_UNAVAILABLE"]
    ]
    assert "mcp_invoke" not in _caps(skipped["surface"]["principal"])


def test_runner_mcp_trace_events_never_land(profiles: dict[str, Any]) -> None:
    """P7-S0-A16 F13：runner 的 mcp_assembly / mcp_assembly_error 事件尝试写入，但从未落盘。"""

    calls = profiles["runner_mcp_register_fault"]["lifecycle"]["trace_record_calls"]
    assert [(call["event"], call["landed"]) for call in calls] == [
        ("mcp_assembly", False),
        ("mcp_assembly_error", False),
    ]
    assert all(
        not call["landed"]
        for profile in profiles.values()
        for call in profile["lifecycle"]["trace_record_calls"]
    )


def test_runner_path_differs_from_session_path(profiles: dict[str, Any]) -> None:
    """P7-S0-A17 F2：runner real-agentic 路径与会话路径的可观察差异（S2 统一前的现状）。"""

    # 模块级缓存：用例结束后 MCP 子进程仍存活；会话路径随 ExitStack 关闭
    assert profiles["runner_mcp_fake"]["lifecycle"]["mcp_clients_alive_after_scope"] == 1
    assert profiles["session_mcp_fake"]["lifecycle"]["mcp_clients_alive_after_scope"] == 0
    # 注册故障：runner 降级为纯本地工具且不授权；会话路径直接抛错
    fault = profiles["runner_mcp_register_fault"]
    assert fault["lifecycle"]["exception"] is None
    assert fault["surface"]["registration_order"] == [
        "retrieve_clauses", "cite_clauses", "calc_velocity", "material_stats"
    ]
    assert "mcp_invoke" not in _caps(fault["surface"]["principal"])
    assert profiles["session_mcp_register_fault"]["lifecycle"]["exception"] == {
        "type": "ValueError", "code": None
    }
    # chat model 拿到的是授权前的主体，交给图的是授权后的主体
    runner_fake = profiles["runner_mcp_fake"]
    assert "mcp_invoke" in _caps(runner_fake["surface"]["principal"])
    assert "mcp_invoke" not in _caps(runner_fake["lifecycle"]["app"]["chat_model_principal"])
    # 会话路径不传 max_tool_turns、接缓存服务（真实模型时）；runner 传 10、不接缓存
    assert profiles["runner_no_mcp"]["lifecycle"]["app"]["max_tool_turns"] == 10
    assert profiles["runner_no_mcp"]["lifecycle"]["app"]["cache_service"] is None
    assert profiles["session_web_member"]["lifecycle"]["app"]["cache_service"] is not None
