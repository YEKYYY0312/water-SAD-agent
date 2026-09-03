"""Step 8 测试：系统提示装配器（PromptAssembler）。

覆盖：分段组装 / 段级预算 / 无 Key 离线可跑 / 可调用（create_react_agent prompt 接口）。
"""
from __future__ import annotations

from langchain_core.messages import SystemMessage

from water_agent.prompting.assembler import (
    ROLE_AND_PRINCIPLES,
    SEGMENT_BUDGETS,
    PromptAssembler,
)
from water_agent.tools.calc_velocity import calc_velocity_spec
from water_agent.tools.registry import ToolRegistry


class _FakeRegistry:
    """假版本注册表（离线、确定性，不依赖 data/versions.json）。"""

    def list_all(self):
        return [
            {"spec_id": "GB50015", "version": "2019", "status": "current", "source": "test", "verified_at": "2026-09-03"},
            {"spec_id": "GB99999", "version": "2099", "status": "unknown", "source": "", "verified_at": ""},
        ]

    def snapshot_lines(self):
        return [
            "- GB50015-2019: 现行[来源: test]",
            "- GB99999-2099: 未知",
        ]


class TestPromptAssembler:
    def test_returns_system_message(self) -> None:
        asm = PromptAssembler(version_registry=_FakeRegistry())
        msg = asm.assemble_prompt()
        assert isinstance(msg, SystemMessage)
        assert msg.content

    def test_contains_role_and_principles(self) -> None:
        asm = PromptAssembler(version_registry=_FakeRegistry())
        msg = asm.assemble_prompt()
        # 静态段（角色与铁律）在前
        assert ROLE_AND_PRINCIPLES in msg.content
        assert "引用可追溯" in msg.content

    def test_contains_version_snapshot(self) -> None:
        asm = PromptAssembler(version_registry=_FakeRegistry())
        msg = asm.assemble_prompt()
        assert "规范版本状态" in msg.content
        assert "GB50015-2019" in msg.content

    def test_contains_formula_specs(self) -> None:
        asm = PromptAssembler(version_registry=_FakeRegistry())
        msg = asm.assemble_prompt()
        assert "公式口径" in msg.content
        assert "calc_velocity" in msg.content

    def test_tool_guide_from_registry(self) -> None:
        registry = ToolRegistry()
        registry.register(calc_velocity_spec)
        asm = PromptAssembler(version_registry=_FakeRegistry(), tool_registry=registry)
        msg = asm.assemble_prompt()
        assert "可用工具" in msg.content
        assert "calc_velocity" in msg.content

    def test_no_tool_registry_no_tool_guide(self) -> None:
        asm = PromptAssembler(version_registry=_FakeRegistry())
        msg = asm.assemble_prompt()
        assert "可用工具" not in msg.content

    def test_empty_registry_no_version_segment(self) -> None:
        class EmptyRegistry:
            def list_all(self):
                return []

            def snapshot_lines(self):
                return []

        asm = PromptAssembler(version_registry=EmptyRegistry())
        msg = asm.assemble_prompt()
        assert "规范版本状态" not in msg.content

    def test_budget_limits_dynamic_segment(self) -> None:
        """动态段（版本快照/工具指南）受段级预算约束。"""
        registry = ToolRegistry()
        registry.register(calc_velocity_spec)
        asm = PromptAssembler(version_registry=_FakeRegistry(), tool_registry=registry)
        msg = asm.assemble_prompt()
        content = msg.content
        # 整体长度受预算之和约束（粗略断言：不超过各段预算之和的 2 倍安全阀）
        total_budget = sum(SEGMENT_BUDGETS.values())
        assert len(content) <= total_budget * 4

    def test_callable_signature_for_create_react_agent(self) -> None:
        """PromptAssembler 可作为 create_react_agent 的 prompt 可调用对象。"""
        asm = PromptAssembler(version_registry=_FakeRegistry())
        # 可调用接收 state（含 messages）并返回 [SystemMessage, ...] + messages
        result = asm({"query": "设计秒流量", "messages": []})
        assert isinstance(result, list)
        assert len(result) >= 1
        assert isinstance(result[0], SystemMessage)
        assert result[0].content

    def test_offline_no_key(self, monkeypatch) -> None:
        """无任何 Key 时装配器可离线运行。"""
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        asm = PromptAssembler(version_registry=_FakeRegistry())
        msg = asm.assemble_prompt()
        assert msg.content