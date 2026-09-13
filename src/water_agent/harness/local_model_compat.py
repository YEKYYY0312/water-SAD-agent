"""Compatibility probe for an explicitly selected local chat endpoint.

The probe is deliberately conservative: it writes a compatible report only
when every required check is passed for the exact endpoint/model. No report is
created as a side effect unless the caller supplies ``out_path``.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

from water_agent.generation.local_model import (
    COMPATIBILITY_CHECKS,
    LocalModelCompatibilityReport,
    local_model_configured,
    local_model_settings,
)

PROBE_VERSION = "p5-step46-2026-09-13"
CheckStatus = Literal["passed", "failed", "blocked", "skipped"]
ReportStatus = Literal["compatible", "blocked", "incomplete"]


def _report(
    *,
    settings: Mapping[str, Any],
    status: ReportStatus,
    checks: Mapping[str, CheckStatus],
    schema_tokens: int = 0,
    evidence: Mapping[str, Any] | None = None,
) -> LocalModelCompatibilityReport:
    return LocalModelCompatibilityReport(
        version=PROBE_VERSION,
        status=status,
        model=str(settings["model"]),
        endpoint=str(settings["base_url"]),
        checks=dict(checks),
        schema_tokens=schema_tokens,
        evidence={"probe_version": PROBE_VERSION, **dict(evidence or {})},
    )


def _all_blocked(*, settings: Mapping[str, Any], reason: str) -> LocalModelCompatibilityReport:
    return _report(
        settings=settings,
        status="blocked",
        checks={name: "blocked" for name in COMPATIBILITY_CHECKS},
        evidence={"reason": reason},
    )


def _real_checks(
    settings: Mapping[str, Any],
) -> tuple[dict[str, CheckStatus], int, dict[str, Any]]:
    """Run the model-facing checks against the configured OpenAI-compatible endpoint."""


    from water_agent.generation.chat_model import build_chat_model
    from water_agent.prompting.assembler import _estimate_tokens
    from water_agent.retrieval.in_memory import InMemoryRetriever
    from water_agent.runtime.governance.binding import local_request_context
    from water_agent.tools.calc_velocity import calc_velocity_spec
    from water_agent.tools.cite import build_cite_clauses_tool
    from water_agent.tools.ds_adapters import tool_spec_to_base_tool
    from water_agent.tools.read_drawing import build_read_drawing_tool
    from water_agent.tools.registry import ToolRegistry
    from water_agent.tools.retrieve_clauses import build_retrieve_clauses_tool

    registry = ToolRegistry()
    registry.register(build_retrieve_clauses_tool(InMemoryRetriever([])))
    registry.register(build_cite_clauses_tool())
    registry.register(calc_velocity_spec)
    registry.register(build_read_drawing_tool(Path(tempfile.gettempdir()) / "water-agent-compat"))
    calc_tool = tool_spec_to_base_tool(registry, "calc_velocity")
    cite_tool = tool_spec_to_base_tool(registry, "cite_clauses")
    model = build_chat_model(
        model=str(settings["model"]),
        base_url=str(settings["base_url"]),
        api_key=str(settings["api_key"]),
        request_context=local_request_context(thread_id="local-model-compat"),
    )

    checks: dict[str, CheckStatus] = {}
    evidence: dict[str, Any] = {}

    function_response = model.bind_tools([calc_tool]).invoke(
        "请调用 calc_velocity，使用 flow_lps=20、diameter_mm=150、scenario=water_supply。"
    )
    tool_calls = getattr(function_response, "tool_calls", []) or []
    checks["function_calling"] = (
        "passed" if any(call.get("name") == "calc_velocity" for call in tool_calls) else "failed"
    )
    evidence["function_call_names"] = [call.get("name", "") for call in tool_calls]

    citation_response = model.bind_tools([cite_tool]).invoke(
        "请调用 cite_clauses，声明 source_id 为 GB50015-2019 4.7.1。"
    )
    citation_calls = getattr(citation_response, "tool_calls", []) or []
    structured_citation = any(
        call.get("name") == "cite_clauses"
        and isinstance(call.get("args", {}).get("article_ids"), list)
        and all(isinstance(value, str) and value for value in call["args"]["article_ids"])
        for call in citation_calls
        if isinstance(call, dict) and isinstance(call.get("args"), dict)
    )
    checks["structured_citations"] = "passed" if structured_citation else "failed"
    evidence["citation_call_names"] = [call.get("name", "") for call in citation_calls]

    calc_payload: dict[str, Any] | None = None
    for call in tool_calls:
        if call.get("name") == "calc_velocity" and isinstance(call.get("args"), dict):
            calc_payload = dict(call["args"])
            break
    calculated = registry.invoke("calc_velocity", **(calc_payload or {}))
    checks["programmatic_calculation"] = (
        "passed" if calculated.get("error") is not True and "velocity_mps" in calculated else "failed"
    )
    evidence["calculation_result_keys"] = sorted(calculated)

    from water_agent.harness.memory_eval import DEFAULT_SUITE, run_suite

    with tempfile.TemporaryDirectory(prefix="water-agent-local-model-p0-") as workdir:
        memory_report = run_suite(
            DEFAULT_SUITE,
            out_path=Path(workdir) / "memory_p0.json",
            workdir=Path(workdir) / "suite",
        )
    checks["memory_p0"] = "passed" if memory_report["failed"] == 0 else "failed"
    evidence["memory_p0"] = {
        "failed": memory_report["failed"],
        "skipped": memory_report["skipped"],
    }

    schema_tokens = _estimate_tokens(json.dumps(registry.list_tools(), ensure_ascii=False))
    checks["tool_schema_budget"] = "passed" if schema_tokens < 4500 else "failed"

    # These two checks are intentionally explicit rather than inferred from a
    # successful response. The gateway/transport must be exercised with a
    # bounded cancellation and a provider error by the host probe before a
    # compatibility report can be accepted.
    checks["cancellation_timeout"] = "blocked"
    checks["error_handling"] = "blocked"
    evidence["manual_required"] = ["cancellation_timeout", "error_handling"]
    return checks, schema_tokens, evidence


def run_local_model_compatibility(
    *,
    environment: Callable[[str], str | None] = os.getenv,
    check_runner: Callable[[Mapping[str, Any]], Mapping[str, CheckStatus]] | None = None,
    out_path: str | Path | None = None,
) -> LocalModelCompatibilityReport:
    """Run or assemble conservative local-model evidence.

    ``check_runner`` exists for host-specific test harnesses that can exercise
    cancellation and provider-error behavior. The default runner performs a
    reachability check and the model-facing checks, but keeps the two host
    lifecycle checks blocked until explicitly measured.
    """

    settings = local_model_settings(environment)
    if not local_model_configured(environment):
        result = _all_blocked(
            settings=settings,
            reason="LOCAL_MODEL_ENABLED is not explicitly enabled",
        )
    elif check_runner is not None:
        checks = dict(check_runner(settings))
        probe_status: ReportStatus = (
            "compatible"
            if set(checks) == set(COMPATIBILITY_CHECKS)
            and all(value == "passed" for value in checks.values())
            else "incomplete"
        )
        result = _report(
            settings=settings,
            status=probe_status,
            checks=checks,
            schema_tokens=0,
            evidence={"runner": "injected"},
        )
    else:
        try:
            checks, schema_tokens, evidence = _real_checks(settings)
            endpoint_status: ReportStatus = (
                "compatible"
                if all(checks.get(name) == "passed" for name in COMPATIBILITY_CHECKS)
                else "incomplete"
            )
            result = _report(
                settings=settings,
                status=endpoint_status,
                checks=checks,
                schema_tokens=schema_tokens,
                evidence=evidence,
            )
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            result = _all_blocked(
                settings=settings,
                reason=f"local model endpoint could not be verified: {type(exc).__name__}",
            )

    if out_path is not None:
        target = Path(out_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P5 Step 46 local model compatibility probe")
    parser.add_argument("--out", default=None, help="optional JSON evidence output path")
    args = parser.parse_args(argv)
    result = run_local_model_compatibility(out_path=args.out)
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0 if result.is_compatible else 2


__all__ = ["PROBE_VERSION", "main", "run_local_model_compatibility"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
