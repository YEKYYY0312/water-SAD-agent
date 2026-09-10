"""Step 29 hydraulic agent thin adapter (offline deterministic)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from water_agent.multi_agent.contracts import (
    ArtifactEnvelope,
    CalcCase,
    CalcPackage,
    HydraulicCaseRequest,
    ProjectBrief,
    compute_payload_hash,
)
from water_agent.tools.hydraulics import run_calc

_SCHEMA_VERSION = "p4.5-2026-09-10"

_FormulaLiteral = Literal[
    "velocity",
    "head_loss_darcy_weisbach",
    "head_loss_hazen_williams",
    "head_loss_shevelev",
    "diameter",
    "slope_manning",
    "pump_head",
]


class _FormulaCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    formula: _FormulaLiteral


def _validate_formula(formula: str) -> None:
    _FormulaCheck.model_validate({"formula": formula})


def _tool_invocation_id(formula: str, inputs: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"formula": formula, "inputs": inputs},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()[:16]
    return f"hyd-{digest}"


def _case_calc_id(case: HydraulicCaseRequest, project_id: str) -> str:
    _ = project_id
    return case.case_id


def run_hydraulic_cases(
    cases: list[HydraulicCaseRequest],
    *,
    supersedes_artifact_id: str | None = None,
) -> CalcPackage:
    calc_cases: list[CalcCase] = []
    missing_all: list[str] = []
    errors_all: list[str] = []

    for req in cases:
        _validate_formula(req.formula)
        tool_id = _tool_invocation_id(req.formula, req.inputs)
        result = run_calc(req.formula, dict(req.inputs))
        if result.ok:
            cc = CalcCase(
                calc_case_id=_case_calc_id(req, ""),
                formula=req.formula,
                inputs=dict(req.inputs),
                outputs=dict(result.values),
                tool_invocation_id=tool_id,
                assumptions=list(result.assumptions),
            )
            calc_cases.append(cc)
        elif result.missing_inputs:
            cc = CalcCase(
                calc_case_id=_case_calc_id(req, ""),
                formula=req.formula,
                inputs=dict(req.inputs),
                outputs={},
                tool_invocation_id=tool_id,
                assumptions=list(result.assumptions),
            )
            calc_cases.append(cc)
            missing_all.extend(result.missing_inputs)
            if result.errors:
                errors_all.extend(result.errors)
        else:
            cc = CalcCase(
                calc_case_id=_case_calc_id(req, ""),
                formula=req.formula,
                inputs=dict(req.inputs),
                outputs={},
                tool_invocation_id=tool_id,
                assumptions=list(result.assumptions),
            )
            calc_cases.append(cc)
            errors_all.extend(result.errors)

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
    )

    if pkg.errors:
        status: Literal["ready", "missing_inputs", "blocked", "failed"] = "blocked"
    elif pkg.missing_inputs:
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
