"""Step 29 hydraulic agent thin adapter (offline deterministic)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from water_agent.multi_agent.contracts import (
    ArtifactEnvelope,
    CalcCase,
    CalcPackage,
    HydraulicCaseRequest,
    ProjectBrief,
    compute_payload_hash,
)
from water_agent.tools.hydraulics import CalcResult, run_calc

_SCHEMA_VERSION = "p4.5-2026-09-10"

def _tool_invocation_id(formula: str, inputs: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"formula": formula, "inputs": inputs},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()[:16]
    return f"hyd-{digest}"


def _case_calc_id(
    case: HydraulicCaseRequest,
    project_id: str,
    supersedes_artifact_id: str | None,
) -> str:
    """Keep first-run IDs readable; revisions get immutable, input-bound IDs."""
    if not supersedes_artifact_id:
        return case.case_id
    canonical = json.dumps(
        {
            "project_id": project_id,
            "supersedes_artifact_id": supersedes_artifact_id,
            "case_id": case.case_id,
            "formula": case.formula,
            "inputs": case.inputs,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    suffix = hashlib.sha256(canonical).hexdigest()[:12]
    return f"{case.case_id}-{suffix}"


def run_hydraulic_cases(
    cases: list[HydraulicCaseRequest],
    *,
    supersedes_artifact_id: str | None = None,
    project_id: str = "",
) -> CalcPackage:
    calc_cases: list[CalcCase] = []
    missing_all: list[str] = []
    errors_all: list[str] = []

    for req in cases:
        inputs_snapshot = dict(req.inputs)
        tool_id = _tool_invocation_id(req.formula, inputs_snapshot)
        calc_case_id = _case_calc_id(req, project_id, supersedes_artifact_id)
        try:
            result = run_calc(req.formula, inputs_snapshot)
            if not isinstance(result, CalcResult):
                raise TypeError(f"工具返回类型错误: {type(result).__name__}")
        except Exception as exc:  # noqa: BLE001 - normalize tool failures at the agent boundary
            result = CalcResult(
                ok=False,
                errors=[f"水力计算工具异常: {type(exc).__name__}: {exc}"],
            )

        if result.ok:
            try:
                calc_cases.append(
                    CalcCase(
                        calc_case_id=calc_case_id,
                        formula=req.formula,
                        inputs=inputs_snapshot,
                        outputs=dict(result.values),
                        tool_invocation_id=tool_id,
                        assumptions=list(result.assumptions),
                        errors=[],
                        status="verified",
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 - normalize malformed tool results
                result = CalcResult(
                    ok=False,
                    errors=[f"水力计算结果无效: {type(exc).__name__}: {exc}"],
                )

        case_errors = list(result.errors)
        if result.missing_inputs:
            case_status: Literal["verified", "missing_inputs", "failed"] = "missing_inputs"
            missing_all.extend(result.missing_inputs)
        else:
            case_status = "failed"
            if not case_errors:
                case_errors.append("水力计算失败")
            errors_all.extend(case_errors)
        calc_cases.append(
            CalcCase(
                calc_case_id=calc_case_id,
                formula=req.formula,
                inputs=inputs_snapshot,
                outputs={},
                tool_invocation_id=tool_id,
                assumptions=list(result.assumptions),
                errors=case_errors,
                status=case_status,
            )
        )

    missing_unique = sorted(set(missing_all))
    pkg = CalcPackage(
        cases=calc_cases,
        missing_inputs=missing_unique,
        errors=errors_all,
        supersedes_artifact_id=supersedes_artifact_id,
    )
    return pkg


def run_hydraulic_agent(
    brief: ProjectBrief,
    *,
    supersedes_artifact_id: str | None = None,
) -> ArtifactEnvelope:
    pkg = run_hydraulic_cases(
        list(brief.hydraulic_cases),
        supersedes_artifact_id=supersedes_artifact_id,
        project_id=brief.project_id,
    )

    if pkg.errors:
        status: Literal["ready", "missing_inputs", "blocked", "failed"] = "blocked"
    elif pkg.missing_inputs or not pkg.cases:
        status = "missing_inputs"
    else:
        status = "ready"

    payload = pkg.model_dump()
    payload_hash = compute_payload_hash(payload)
    raw = f"{brief.project_id}:{supersedes_artifact_id or ''}:{payload_hash}"
    artifact_id = f"calc-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"
    revision = 2 if supersedes_artifact_id is not None else 1
    parent_ids: list[str] = [supersedes_artifact_id] if supersedes_artifact_id else []

    envelope = ArtifactEnvelope(
        artifact_id=artifact_id,
        artifact_type="calc_package",
        schema_version=_SCHEMA_VERSION,
        producer="hydraulic",
        revision=revision,
        parent_artifact_ids=parent_ids,
        payload_hash=payload_hash,
        payload=payload,
        citation_ids=[],
        assumptions=[],
        warnings=[],
        status=status,
    )
    return envelope
