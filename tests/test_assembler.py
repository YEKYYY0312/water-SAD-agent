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

    def test_contains_citation_precision_rule(self) -> None:
        """引用精准作为普通执行规则保留（多本规范并存时口径最匹配优先）。"""
        asm = PromptAssembler(version_registry=_FakeRegistry())
        msg = asm.assemble_prompt()
        assert "引用精准" in msg.content
        assert "口径最匹配" in msg.content
        assert "同一主张不堆砌" in msg.content

    def test_three_iron_laws_restored_and_precision_outside_iron_laws(self) -> None:
        """P1 评审：canonical 名称为【三条铁律】；引用精准只能是执行规则，不得改铁律定义。"""
        asm = PromptAssembler(version_registry=_FakeRegistry())
        content = asm.assemble_prompt().content
        assert "【三条铁律】" in content
        assert "四条铁律" not in content, "评审前把引用精准混入铁律的四条铁律定义必须移除"
        assert content.count("【三条铁律】") == 1
        assert "引用精准" in content
        assert content.index("引用精准") > content.index("【执行规则】"), (
            "引用精准必须位于铁律之外的执行规则区，不得改变项目铁律定义"
        )

    def test_informal_corpus_is_clue_only_with_explicit_disclosure(self) -> None:
        """P1 评审：非正式语料不得"照常引用、标注不构成禁止引用"。

        早期为让 rag-p1-0049 通过而加入"标注只是语料来源说明、不构成禁止引用"——
        那等于让模型把联网整理的要点综述当正式条文引用。正确策略：带“要点综述 /
        联网整理 / 非正式规范条文”标记的内容只能作为检索线索，必须标注其非正式性质、
        不得表述为正式规范原文、不得据此给未核实的强制结论或精确限值，需要正式依据
        时说明需核对正式出版标准。
        """
        asm = PromptAssembler(version_registry=_FakeRegistry())
        content = asm.assemble_prompt().content
        assert "要点综述" in content and "非正式规范条文" in content
        assert "不构成禁止引用" not in content, "旧的'照常引用'规则必须删除"
        assert "检索线索" in content
        assert "正式出版标准" in content

    def test_prompt_rules_single_source_no_drift(self) -> None:
        """P1 评审：DEFAULT_AGENTIC_PROMPT 与 ROLE_AND_PRINCIPLES 不得各自维护一套规则。

        两条提示路径（prompt=None 默认 / PromptAssembler 装配）必须复用一个公共静态
        提示定义，否则规则文案会像评审前一样漂移（三条铁律 vs 四条铁律）。
        """
        from water_agent.graphs.agent_rag import DEFAULT_AGENTIC_PROMPT

        assert ROLE_AND_PRINCIPLES in DEFAULT_AGENTIC_PROMPT, (
            "默认 agentic 提示必须整体复用 ROLE_AND_PRINCIPLES 单一规则来源"
        )
        # 规则正文在任一提示成品里只出现一次（无重复段落）
        assert DEFAULT_AGENTIC_PROMPT.count("引用可追溯") == 1
        assert DEFAULT_AGENTIC_PROMPT.count("【三条铁律】") == 1
        asm = PromptAssembler(version_registry=_FakeRegistry())
        content = asm.assemble_prompt().content
        assert content.count("【三条铁律】") == 1

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