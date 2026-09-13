"""P5 Step 46 local-model opt-in and compatibility evidence gates."""

from __future__ import annotations

import json
from pathlib import Path

from water_agent.generation.local_model import (
    LOCAL_MODEL_BASE_URL,
    LOCAL_MODEL_COMPATIBILITY_REPORT,
    LOCAL_MODEL_ENABLED,
    LocalModelCompatibilityReport,
    local_model_configured,
    local_model_ready,
    read_compatibility_report,
)
from water_agent.harness.local_model_compat import run_local_model_compatibility
from water_agent.runtime.agentic import real_llm_configured


def test_local_model_is_disabled_without_explicit_opt_in(monkeypatch) -> None:
    monkeypatch.delenv(LOCAL_MODEL_ENABLED, raising=False)
    monkeypatch.delenv(LOCAL_MODEL_COMPATIBILITY_REPORT, raising=False)
    assert local_model_configured() is False
    assert local_model_ready() is False


def test_local_model_requires_compatible_evidence_not_just_endpoint(monkeypatch) -> None:
    monkeypatch.setenv(LOCAL_MODEL_ENABLED, "true")
    monkeypatch.setenv(LOCAL_MODEL_BASE_URL, "http://local-model:11434/v1")
    monkeypatch.delenv(LOCAL_MODEL_COMPATIBILITY_REPORT, raising=False)
    assert local_model_configured() is True
    assert local_model_ready() is False


def test_unverified_local_selection_does_not_silently_fall_back_to_deepseek(
    monkeypatch,
) -> None:
    monkeypatch.setenv(LOCAL_MODEL_ENABLED, "true")
    monkeypatch.setenv(LOCAL_MODEL_BASE_URL, "http://local-model:11434/v1")
    monkeypatch.delenv(LOCAL_MODEL_COMPATIBILITY_REPORT, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "present-but-not-used")

    assert real_llm_configured() is False


def test_compatibility_report_requires_every_step46_check_and_schema_budget(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "local-model-compatibility.json"
    report_path.write_text(
        json.dumps(
            {
                "version": "p5-step46-2026-09-13",
                "status": "compatible",
                "model": "unverified-example",
                "endpoint": "http://127.0.0.1:11434/v1",
                "checks": {
                    "function_calling": "passed",
                    "structured_citations": "passed",
                    "programmatic_calculation": "passed",
                    "memory_p0": "passed",
                    "cancellation_timeout": "passed",
                    "error_handling": "passed",
                    "tool_schema_budget": "passed",
                },
                "schema_tokens": 4499,
                "evidence": {"probe_version": "p5-step46-2026-09-13"},
            }
        ),
        encoding="utf-8",
    )
    report = read_compatibility_report(report_path)
    assert report is not None
    assert report.is_compatible is True

    report_path.write_text(
        report_path.read_text(encoding="utf-8").replace("4499", "4500"),
        encoding="utf-8",
    )
    assert read_compatibility_report(report_path).is_compatible is False


def test_incomplete_or_malformed_evidence_is_not_compatible(tmp_path: Path) -> None:
    report = LocalModelCompatibilityReport(
        version="p5-step46-2026-09-13",
        status="compatible",
        model="example",
        endpoint="http://127.0.0.1:11434/v1",
        checks={"function_calling": "passed"},
        schema_tokens=100,
    )
    assert report.is_compatible is False


def test_compatibility_probe_is_blocked_without_an_opted_in_endpoint(monkeypatch) -> None:
    monkeypatch.delenv(LOCAL_MODEL_ENABLED, raising=False)
    result = run_local_model_compatibility()

    assert result.status == "blocked"
    assert result.is_compatible is False
    assert set(result.checks) == {
        "function_calling",
        "structured_citations",
        "programmatic_calculation",
        "memory_p0",
        "cancellation_timeout",
        "error_handling",
        "tool_schema_budget",
    }
    assert all(value == "blocked" for value in result.checks.values())


def test_optional_step46_imports_do_not_mutate_the_default_tool_registry(tmp_path: Path) -> None:
    from water_agent.mcp.memory_adapter import MemoryMCPAdapter
    from water_agent.memory.providers import PgVectorMemoryProvider
    from water_agent.prompting.assembler import _estimate_tokens
    from water_agent.retrieval.in_memory import InMemoryRetriever
    from water_agent.tools.calc_velocity import calc_velocity_spec
    from water_agent.tools.cite import build_cite_clauses_tool
    from water_agent.tools.material_stats import material_stats_spec
    from water_agent.tools.read_drawing import build_read_drawing_tool
    from water_agent.tools.registry import ToolRegistry
    from water_agent.tools.retrieve_clauses import build_retrieve_clauses_tool

    def registry() -> ToolRegistry:
        value = ToolRegistry()
        value.register(build_retrieve_clauses_tool(InMemoryRetriever([])))
        value.register(build_cite_clauses_tool())
        value.register(calc_velocity_spec)
        value.register(material_stats_spec)
        value.register(build_read_drawing_tool(tmp_path / "drawings"))
        return value

    before = registry()
    # Importing optional providers must not register model-facing tools or
    # alter the default registry's schema.
    assert MemoryMCPAdapter is not None
    assert PgVectorMemoryProvider is not None
    after = registry()
    assert before.list_all_names() == after.list_all_names()
    assert before.list_tools() == after.list_tools()
    assert _estimate_tokens(str(after.list_tools())) < 4500
