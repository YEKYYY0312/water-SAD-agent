"""Step 8 测试：系统提示装配器（PromptAssembler）。

覆盖：分段组装 / 段级预算 / 无 Key 离线可跑 / 可调用（create_react_agent prompt 接口）。
"""
from __future__ import annotations

import pytest
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

    def test_budget_truncates_dynamic_segment_with_marker(self) -> None:
        """动态段（版本快照）超预算 → 按行截断到预算内，且**显式标注省略行数**。"""

        class HugeRegistry:
            def list_all(self):
                return []

            def snapshot_lines(self):
                return [f"- GB{i:05d}-2019: 现行[来源: test]" for i in range(200)]

        asm = PromptAssembler(version_registry=HugeRegistry())
        content = asm.assemble_prompt().content
        start = content.index("【规范版本状态")
        end = content.index("【公式口径")
        segment = content[start:end].strip()
        assert len(segment) <= SEGMENT_BUDGETS["version_snapshot"]
        assert "省略" in segment and "行" in segment, "截断必须可见，否则模型会把没列出误当不存在"
        assert "GB00000-2019" in segment  # 前面的行保留
        assert "GB00199-2019" not in segment  # 后面的行被省略

    def test_dynamic_segment_within_budget_not_truncated(self) -> None:
        asm = PromptAssembler(version_registry=_FakeRegistry())
        content = asm.assemble_prompt().content
        assert "省略" not in content

    def test_static_segments_within_budget(self) -> None:
        """静态段（角色铁律/公式口径）不截断：必须本身就在预算内，否则装配直接抛错。"""
        from water_agent.prompting.assembler import FORMULA_SPECS, _check_static_budget

        assert len(ROLE_AND_PRINCIPLES) <= SEGMENT_BUDGETS["role_and_principles"]
        assert len(FORMULA_SPECS) <= SEGMENT_BUDGETS["formula_specs"]
        with pytest.raises(ValueError, match="超预算"):
            _check_static_budget("role_and_principles", "x" * (SEGMENT_BUDGETS["role_and_principles"] + 1))

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