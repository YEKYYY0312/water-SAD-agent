"""工具抽象层测试（Step 4）：ToolSpec + ToolRegistry + 首批工具。

覆盖：schema 拒绝缺参/类型错（结构化错误，不抛异常）、重名/非法名拒绝、
execute 异常转结构化错误、list_tools() 产出标准 JSON Schema、calc_velocity
枚举约束、retrieve_clauses 包装检索并截断结果 ≤10KB。
"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, ValidationError

from water_agent.domain.models import RetrievedClause
from water_agent.retrieval.base import Retriever
from water_agent.runtime.governance.contracts import Capability
from water_agent.runtime.governance.pipeline import GuardrailPipeline
from water_agent.tools.base import ToolSpec
from water_agent.tools.calc_velocity import VelocityArgs, calc_velocity_spec
from water_agent.tools.registry import ToolRegistry
from water_agent.tools.retrieve_clauses import build_retrieve_clauses_tool


class _StubRetriever(Retriever):
    """返回指定条数检索结果的替身，text 可放大以测截断。"""

    def __init__(self, text: str = "条文内容", total: int = 5) -> None:
        self._text = text
        self._total = total

    def retrieve(self, query: str, top_k: int = 5) -> list[RetrievedClause]:
        return [
            RetrievedClause(spec_id="FIX", article=f"{i}", title="标题", text=self._text)
            for i in range(min(top_k, self._total))
        ]


class TestRegistryBasic:
    def test_register_and_invoke_ok(self) -> None:
        registry = ToolRegistry()
        registry.register(calc_velocity_spec)
        out = registry.invoke("calc_velocity", diameter_mm=150, flow_lps=20)
        assert out["velocity_mps"] == pytest.approx(1.132, abs=1e-3)
        assert out["assumptions"] == []

    def test_duplicate_register_rejected(self) -> None:
        registry = ToolRegistry()
        registry.register(calc_velocity_spec)
        with pytest.raises(ValueError):
            registry.register(calc_velocity_spec)

    def test_unknown_tool_invoke_structured_error(self) -> None:
        registry = ToolRegistry()
        registry.register(calc_velocity_spec)
        out = registry.invoke("not_exist")
        assert out.get("error") is True
        assert out["code"] == "UNKNOWN_TOOL"


class TestSchemaValidation:
    def test_missing_arg_structured_error(self) -> None:
        registry = ToolRegistry()
        registry.register(calc_velocity_spec)
        out = registry.invoke("calc_velocity", flow_lps=20)
        assert out.get("error") is True
        assert out["code"] == "INVALID_ARGUMENTS"
        assert "diameter_mm" in out["message"]

    def test_type_error_structured_error(self) -> None:
        registry = ToolRegistry()
        registry.register(calc_velocity_spec)
        out = registry.invoke("calc_velocity", diameter_mm="abc", flow_lps=20)
        assert out.get("error") is True
        assert out["code"] == "INVALID_ARGUMENTS"

    def test_negative_flow_structured_error(self) -> None:
        registry = ToolRegistry()
        registry.register(calc_velocity_spec)
        out = registry.invoke("calc_velocity", diameter_mm=150, flow_lps=-5)
        assert out.get("error") is True

    def test_bad_name_rejected_at_build(self) -> None:
        with pytest.raises(ValidationError):
            ToolSpec(
                name="Bad Name!",
                description="A tool with a forbidden name. Use when validating. Do not use otherwise.",
                args_model=VelocityArgs,
                execute=lambda args: {},
            )

    def test_short_description_rejected_at_build(self) -> None:
        with pytest.raises(ValidationError):
            ToolSpec(
                name="calc_velocity",
                description="很短",
                args_model=VelocityArgs,
                execute=lambda args: {},
            )


class TestErrorConversion:
    def test_execute_exception_becomes_structured_error(self) -> None:
        class BoomArgs(BaseModel):
            x: int

        def boom(args) -> dict:
            raise RuntimeError("boom inside tool")

        registry = ToolRegistry(
            pipeline=GuardrailPipeline(
                # 测试夹具不在治理清单，装配期显式声明所需能力（声明≠豁免）
                declared_capabilities={"boom_tool": Capability.CALCULATE}
            )
        )
        registry.register(
            ToolSpec(
                name="boom_tool",
                description="A tool that always fails. Use when testing error conversion. Do not use otherwise.",
                args_model=BoomArgs,
                execute=boom,
            )
        )
        out = registry.invoke("boom_tool", x=1)
        assert out.get("error") is True
        assert out["code"] == "TOOL_ERROR"
        assert "boom" in out["message"]
        assert isinstance(out.get("suggestions"), list)


class TestListTools:
    def test_schema_fields_complete(self) -> None:
        registry = ToolRegistry()
        registry.register(calc_velocity_spec)
        tools = registry.list_tools()
        assert tools
        entry = tools[0]
        assert entry["type"] == "function"
        func = entry["function"]
        assert "name" in func and func["name"] == "calc_velocity"
        assert "description" in func and len(func["description"]) >= 40
        params = func["parameters"]
        assert params["type"] == "object"
        assert params.get("additionalProperties") is False
        assert set(params["required"]) == {"flow_lps", "diameter_mm"}
        json.dumps(tools, ensure_ascii=False)  # 可 JSON 序列化

    def test_scenario_enum_constrained(self) -> None:
        schema = VelocityArgs.model_json_schema()
        assert "scenario" in schema["properties"]
        assert "enum" in schema["properties"]["scenario"]


class TestCalcVelocityTool:
    def test_scenario_defaults_to_water_supply(self) -> None:
        schema = VelocityArgs.model_json_schema()
        assert schema["properties"]["scenario"].get("default") == "water_supply"

    def test_drainage_scenario_accepted(self) -> None:
        registry = ToolRegistry()
        registry.register(calc_velocity_spec)
        out = registry.invoke("calc_velocity", diameter_mm=300, flow_lps=60, scenario="drainage")
        assert out["velocity_mps"] == pytest.approx(0.849, abs=1e-3)


class TestRetrieveClausesTool:
    def test_wraps_retriever(self) -> None:
        tool = build_retrieve_clauses_tool(_StubRetriever(total=4))
        registry = ToolRegistry()
        registry.register(tool)
        out = registry.invoke("retrieve_clauses", query="设计秒流量", top_k=3)
        assert out["ok"] is True
        assert out["count"] == 3
        assert all("source_id" in hit for hit in out["hits"])

    def test_result_truncated_under_10kb(self) -> None:
        big = "条" * 5000
        tool = build_retrieve_clauses_tool(_StubRetriever(text=big, total=5))
        registry = ToolRegistry()
        registry.register(tool)
        out = registry.invoke("retrieve_clauses", query="巨大上下文", top_k=5)
        serialized = json.dumps(out, ensure_ascii=False).encode("utf-8")
        assert len(serialized) <= 10 * 1024, "检索结果必须截断在 10KB 以内"

    def test_name_and_schema(self) -> None:
        tool = build_retrieve_clauses_tool(_StubRetriever())
        schema = tool.args_model.model_json_schema()
        assert schema["properties"]["top_k"]["default"] == 5
        assert tool.name == "retrieve_clauses"