"""P2.5 Step 12 测试：工具运行时策略（死字段清理 + 协作式 deadline）。

覆盖规格 §4「测试与验收」的 ``tests/test_tool_runtime_policy.py`` 条目：

1. ``ToolSpec`` 的 timeout/retry **要么真实生效，要么字段不存在** ——
   ``max_retries`` 已删除（从未被读取的死字段），``timeout_seconds`` 接入
   ``ToolRegistry.invoke`` 的协作式 deadline。
2. **纯计算结果与引用校验不被治理层改变** —— 铁律①②不因 P2.5 而漂移。

全部离线，不发起远程调用。
"""
from __future__ import annotations

import time

import pytest
from pydantic import BaseModel, ValidationError

from water_agent.domain.citation import validate_citations
from water_agent.runtime.governance.contracts import Capability
from water_agent.runtime.governance.pipeline import GuardrailPipeline
from water_agent.tools.base import ToolSpec
from water_agent.tools.calc_velocity import calc_velocity_spec
from water_agent.tools.material_stats import material_stats_spec
from water_agent.tools.registry import (
    CODE_INVALID_ARGUMENTS,
    CODE_TOOL_ERROR,
    CODE_TOOL_TIMEOUT,
    CODE_UNKNOWN_TOOL,
    ToolRegistry,
)

_VALID_DESCRIPTION = (
    "Use when the caller needs a deterministic echo of the given integer. "
    "Do not use for hydraulic calculations or clause retrieval of any kind."
)


class _EchoArgs(BaseModel):
    value: int = 0


def _spec(
    *,
    name: str = "echo_tool",
    timeout_seconds: float = 30.0,
    execute=None,
    guard=None,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=_VALID_DESCRIPTION,
        args_model=_EchoArgs,
        execute=execute or (lambda args: {"value": args.value}),
        timeout_seconds=timeout_seconds,
        guard=guard,
    )


def _registry() -> ToolRegistry:
    """带装配期能力声明的注册表。

    P2.5 Step 14 后 ``ToolRegistry.invoke`` 强制过护栏，未登记 capability 的工具
    fail closed。测试夹具 ``echo_tool`` 不在治理清单里，故在**装配期显式声明**
    它需要 ``CALCULATE``（与真实纯计算工具同级）——这是声明而非豁免：
    主体授权、危险能力拒绝、参数大小与 allowlist 检查照旧生效。
    """
    return ToolRegistry(
        pipeline=GuardrailPipeline(declared_capabilities={"echo_tool": Capability.CALCULATE})
    )


class TestDeadFieldsRemoved:
    """规格 §4：禁止保留看似生效的死字段。"""

    def test_max_retries_field_no_longer_exists(self) -> None:
        assert "max_retries" not in ToolSpec.model_fields

    def test_max_retries_now_rejected_as_extra_field(self) -> None:
        """extra="forbid" 让旧调用方立即发现字段已删，而不是静默忽略。"""
        with pytest.raises(ValidationError, match="[Ee]xtra"):
            ToolSpec(
                name="echo_tool",
                description=_VALID_DESCRIPTION,
                args_model=_EchoArgs,
                execute=lambda args: {},
                max_retries=3,
            )

    def test_no_source_reads_tool_max_retries(self) -> None:
        """静态保护：源码中不得再出现 ToolSpec.max_retries 的读取。"""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "src" / "water_agent"
        offenders = []
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in ("spec.max_retries", "ToolSpec.max_retries"):
                if pattern in text:
                    offenders.append(f"{path.name}:{pattern}")
        assert offenders == []


class TestTimeoutIsReal:
    """``timeout_seconds`` 必须真实生效（协作式 deadline）。"""

    def test_timeout_field_exists_and_is_positive(self) -> None:
        assert "timeout_seconds" in ToolSpec.model_fields
        assert _spec().timeout_seconds == 30.0

    def test_non_positive_timeout_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _spec(timeout_seconds=0)
        with pytest.raises(ValidationError):
            _spec(timeout_seconds=-1)

    def test_slow_tool_returns_structured_timeout(self) -> None:
        """超时返回结构化 TOOL_TIMEOUT，不抛裸异常。"""

        def slow(args: _EchoArgs) -> dict:
            time.sleep(0.05)
            return {"value": args.value}

        registry = _registry()
        registry.register(_spec(timeout_seconds=0.01, execute=slow))
        result = registry.invoke("echo_tool", value=1)
        assert result["error"] is True
        assert result["code"] == CODE_TOOL_TIMEOUT
        assert "超过上限" in result["message"]

    def test_fast_tool_within_timeout_succeeds(self) -> None:
        registry = _registry()
        registry.register(_spec(timeout_seconds=30.0))
        assert registry.invoke("echo_tool", value=7) == {"value": 7}

    def test_timeout_does_not_kill_thread(self) -> None:
        """协作式 deadline：工具跑完才判定，不中途杀线程（规格 §4）。

        断言 execute 完整执行（副作用可见），只是结果被丢弃。
        """
        completed: list[str] = []

        def slow(args: _EchoArgs) -> dict:
            time.sleep(0.02)
            completed.append("finished")  # 若被杀线程则永不执行
            return {"value": args.value}

        registry = _registry()
        registry.register(_spec(timeout_seconds=0.001, execute=slow))
        result = registry.invoke("echo_tool", value=1)
        assert result["code"] == CODE_TOOL_TIMEOUT
        assert completed == ["finished"]

    def test_registered_tools_all_have_positive_timeout(self) -> None:
        for spec in (calc_velocity_spec, material_stats_spec):
            assert spec.timeout_seconds > 0


class TestExistingErrorSemanticsUnchanged:
    """治理层不得改变既有结构化错误语义。"""

    def test_unknown_tool_still_structured(self) -> None:
        result = ToolRegistry().invoke("nope")
        assert result["code"] == CODE_UNKNOWN_TOOL

    def test_invalid_arguments_still_structured(self) -> None:
        registry = _registry()
        registry.register(_spec())
        result = registry.invoke("echo_tool", value="not-an-int")
        assert result["code"] == CODE_INVALID_ARGUMENTS

    def test_tool_exception_still_structured(self) -> None:
        registry = _registry()
        registry.register(_spec(execute=lambda args: (_ for _ in ()).throw(RuntimeError("boom"))))
        result = registry.invoke("echo_tool", value=1)
        assert result["code"] == CODE_TOOL_ERROR
        assert "boom" in result["message"]

    def test_guard_failure_still_structured(self) -> None:
        def guard(_args: _EchoArgs) -> None:
            raise ValueError("guard rejected")

        registry = _registry()
        registry.register(_spec(guard=guard))
        result = registry.invoke("echo_tool", value=1)
        assert result["code"] == CODE_TOOL_ERROR
        assert "安全校验未通过" in result["message"]

    def test_non_dict_return_still_structured(self) -> None:
        registry = _registry()
        registry.register(_spec(execute=lambda args: ["not", "a", "dict"]))
        result = registry.invoke("echo_tool", value=1)
        assert result["code"] == CODE_TOOL_ERROR


class TestPureCalculationUnaffectedByGovernance:
    """铁律①：计算永远程序化，治理层不参与数值运算。"""

    def test_calc_velocity_result_identical_via_registry(self) -> None:
        registry = _registry()
        registry.register(calc_velocity_spec)
        args = {"diameter_mm": 200.0, "flow_lps": 30.0}
        result = registry.invoke("calc_velocity", **args)
        assert "error" not in result
        # 直接调用 execute 与经 registry 调用结果一致（治理层不改数值）
        direct = calc_velocity_spec.execute(calc_velocity_spec.args_model.model_validate(args))
        assert result == direct

    def test_calc_is_deterministic_across_repeated_invocations(self) -> None:
        registry = _registry()
        registry.register(calc_velocity_spec)
        args = {"diameter_mm": 150.0, "flow_lps": 20.0}
        first = registry.invoke("calc_velocity", **args)
        assert "error" not in first
        for _ in range(5):
            assert registry.invoke("calc_velocity", **args) == first

    def test_material_stats_result_identical_via_registry(self) -> None:
        registry = _registry()
        registry.register(material_stats_spec)
        segments = [
            {"segment_id": "S1", "system": "water_supply", "dn_mm": 100,
             "length_m": 12.5, "material": "球墨铸铁"},
            {"segment_id": "S2", "system": "water_supply", "dn_mm": 100,
             "length_m": 7.5, "material": "球墨铸铁"},
        ]
        result = registry.invoke("material_stats", segments=segments)
        assert "error" not in result
        # 数量 = 长度之和，程序化聚合（治理层不参与）
        assert result["total_length_m"] == 20.0
        direct = material_stats_spec.execute(
            material_stats_spec.args_model.model_validate({"segments": segments})
        )
        assert result == direct


class TestCitationValidationUnaffectedByGovernance:
    """铁律②：引用校验不被治理层改变。"""

    def test_valid_citation_still_passes(self) -> None:
        result = validate_citations(
            ["GB50015-2019 3.1.1"], ["GB50015-2019 3.1.1", "GB50013-2018 5.1.1"]
        )
        assert result.ok is True
        assert result.errors == []

    def test_out_of_context_citation_still_rejected(self) -> None:
        """编造引用仍然是错误 —— 治理层没有放宽这条。"""
        result = validate_citations(["GB99999-2099 9.9.9"], ["GB50015-2019 3.1.1"])
        assert result.ok is False
        assert result.errors
