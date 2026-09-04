"""P2 测试：material_stats 工具（ToolSpec 校验 / 注册表唯一入口 / 结构化错误）。"""
from __future__ import annotations

import pytest

from water_agent.tools.base import ToolSpec
from water_agent.tools.material_stats import material_stats_spec
from water_agent.tools.registry import ToolRegistry

SEGMENTS = [
    {"segment_id": "S1", "system": "water_supply", "dn_mm": 150, "length_m": 120.5, "material": "球墨铸铁"},
    {"segment_id": "S2", "system": "water_supply", "dn_mm": 150, "length_m": 80, "material": "球墨铸铁"},
]


class TestToolSpec:
    def test_spec_shape(self) -> None:
        assert isinstance(material_stats_spec, ToolSpec)
        assert material_stats_spec.name == "material_stats"
        assert "Use when" in material_stats_spec.description
        assert "Do not use" in material_stats_spec.description

    def test_openai_tool_serializable(self) -> None:
        tool = material_stats_spec.to_openai_tool()
        assert tool["function"]["name"] == "material_stats"
        assert tool["function"]["parameters"]["type"] == "object"

    def test_invoke_via_registry_returns_aggregated_rows(self) -> None:
        registry = ToolRegistry()
        registry.register(material_stats_spec)
        result = registry.invoke("material_stats", segments=SEGMENTS)
        assert result["ok"] is True
        rows = result["rows"]
        assert rows == [
            {
                "material": "球墨铸铁",
                "spec": "DN150",
                "unit": "m",
                "quantity": 200.5,
                "raw_quantity": 200.5,
                "segments": ["S1", "S2"],
                "detail": "S1(120.5m)、S2(80m)",
            }
        ]
        assert result["total_length_m"] == 200.5

    def test_duplicate_registration_rejected(self) -> None:
        registry = ToolRegistry()
        registry.register(material_stats_spec)
        with pytest.raises(ValueError, match="已注册"):
            registry.register(material_stats_spec)

    def test_duplicate_segment_id_structured_error(self) -> None:
        """通过 schema 但引擎拒绝（重复段号）→ TOOL_ERROR 结构化错误。"""
        registry = ToolRegistry()
        registry.register(material_stats_spec)
        result = registry.invoke(
            "material_stats",
            segments=[
                {"segment_id": "N1", "system": "water_supply", "dn_mm": 150, "length_m": 50, "material": "钢管"},
                {"segment_id": "N1", "system": "water_supply", "dn_mm": 150, "length_m": 30, "material": "钢管"},
            ],
        )
        assert result["error"] is True
        assert result["code"] == "TOOL_ERROR"
        assert "重复" in result["message"]

    def test_negative_length_guard_in_schema(self) -> None:
        """pydantic gt=0 直接挡掉负长度（不落引擎）。"""
        registry = ToolRegistry()
        registry.register(material_stats_spec)
        result = registry.invoke(
            "material_stats",
            segments=[{"segment_id": "N1", "system": "water_supply", "dn_mm": 150, "length_m": -1, "material": "钢管"}],
        )
        assert result["error"] is True
        assert result["code"] == "INVALID_ARGUMENTS"

    def test_missing_system_rejected_by_schema(self) -> None:
        registry = ToolRegistry()
        registry.register(material_stats_spec)
        result = registry.invoke(
            "material_stats",
            segments=[{"segment_id": "M1", "dn_mm": 150, "length_m": 10, "material": "钢管"}],
        )
        assert result["error"] is True
        assert result["code"] == "INVALID_ARGUMENTS"
        assert "system" in result["message"]

    def test_noninteger_dn_rejected_by_schema(self) -> None:
        registry = ToolRegistry()
        registry.register(material_stats_spec)
        result = registry.invoke(
            "material_stats",
            segments=[{"segment_id": "D1", "system": "water_supply", "dn_mm": 150.5, "length_m": 10, "material": "钢管"}],
        )
        assert result["error"] is True
        assert result["code"] == "INVALID_ARGUMENTS"
