"""P4.5 Step 33: bounded four-agent graph, HITL approval, and release gate."""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import tempfile
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from functools import wraps
from operator import add
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict, cast
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from water_agent.ingestion.version_registry import get_version_registry
from water_agent.multi_agent import export_adapter
from water_agent.multi_agent.agents.design import run_design_agent
from water_agent.multi_agent.agents.drawing import run_drawing_agent
from water_agent.multi_agent.agents.hydraulic import run_hydraulic_agent
from water_agent.multi_agent.agents.review import run_review_agent
from water_agent.multi_agent.contracts import (
    MULTI_APPROVAL_REQUIRED,
    MULTI_APPROVAL_STALE,
    MULTI_CALC_BLOCKED,
    MULTI_DESIGN_CONFLICT,
    MULTI_DRAWING_CONFLICT,
    MULTI_DRAWING_UNAVAILABLE,
    MULTI_EXPORT_FAILED,
    MULTI_INVALID_BRIEF,
    MULTI_MISSING_INPUTS,
    MULTI_RECALC_REQUIRED,
    MULTI_RETRY_EXHAUSTED,
    MULTI_REVIEW_REJECTED,
    ApprovalRecord,
    ArtifactEnvelope,
    CalcPackage,
    DesignPackage,
    DrawingPackage,
    DrawingPlan,
    FinalProjectPackage,
    ProjectBrief,
    ReviewIssue,
    ReviewPackage,
    compute_artifact_collection_hash,
    compute_payload_hash,
    project_brief_conflict_fields,
    project_brief_conflicts,
    verify_envelope_hash,
)
from water_agent.multi_agent.drawing_executor import stage_drawing_package
from water_agent.multi_agent.provenance import validate_approval_binding
from water_agent.multi_agent.routing import (
    MAX_HYDRAULIC_REVISIONS,
    MAX_MULTI_AGENT_STEPS,
    next_owner_for_issues,
    route_review,
)
from water_agent.session import default_checkpoint_serde

DEFAULT_APPROVAL_TTL_SECONDS = 24 * 60 * 60
_SCHEMA_VERSION = "p4.5-2026-09-11"
#: 发布集（批准绑定的最终包）由这六个角色的活动产物组成，顺序即审批摘要里的展示顺序。
_RELEASE_ROLES = ("calc", "design", "drawing_plan", "document", "drawing", "review")
#: 审批摘要里 payload_hash 只给前缀（完整哈希在 final_package_hash 与产物里）。
_HASH_PREFIX_CHARS = 12
_TRACE_SOURCE = "multi_agent_orchestrator"
_TRACE_DELIVERY = "local_event_only"
_INTERNAL_TRACE_ID_RE = re.compile(
    r"^ma-(?:p0-\d{2}|[0-9a-f]{24}|[0-9a-f]{32})$"
)
_TRACE_ID_CONTEXT: ContextVar[str | None] = ContextVar(
    "multi_agent_trace_id",
    default=None,
)
_RECALCULABLE_FIELDS = frozenset(
    {
        "flow_lps",
        "diameter_mm",
        "length_m",
        "velocity_mps",
        "friction_factor",
        "hazen_williams_c",
        "manning_n",
        "fill_ratio",
        "slope",
        "static_head_m",
        "friction_head_m",
        "local_head_m",
        "residual_head_m",
    }
)


class MultiAgentProjectState(TypedDict, total=False):
    """JSON-shaped state persisted by SqliteSaver across approval resumes."""

    project_brief: dict[str, Any]
    trace_id: str
    retrieved_source_ids: list[str]
    artifacts: dict[str, dict[str, Any]]
    active_artifact_ids: dict[str, str]
    revision_counts: dict[str, int]
    phase: str
    route_target: str | None
    approval_record: dict[str, Any] | None
    approval_context_hash: str | None
    final_project_package: dict[str, Any] | None
    release_checks: dict[str, bool]
    staging_dir: str
    final_dir: str
    output_file_id: str
    exported_file_path: str | None
    step_count: int
    status: str
    errors: list[str]
    missing_inputs: list[str]
    trace_events: Annotated[list[dict[str, Any]], add]


def default_multi_agent_state(**overrides: Any) -> MultiAgentProjectState:
    state: MultiAgentProjectState = {
        "project_brief": {}, "retrieved_source_ids": [], "artifacts": {}, "active_artifact_ids": {},
        "revision_counts": {"hydraulic": 0, "design": 0, "drawing": 0, "review": 0},
        "phase": "intake", "route_target": None, "approval_record": None,
        "approval_context_hash": None, "final_project_package": None, "release_checks": {},
        "staging_dir": "", "final_dir": "", "output_file_id": "project-drawing",
        "exported_file_path": None, "step_count": 0, "status": "running", "errors": [],
        "missing_inputs": [], "trace_id": safe_multi_agent_trace_id(overrides.get("trace_id")),
        "trace_events": [],
    }
    state.update(cast(MultiAgentProjectState, overrides))
    state["trace_id"] = safe_multi_agent_trace_id(state.get("trace_id"))
    return state


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _short_repr(value: Any, limit: int = 80) -> str:
    """Bounded repr for an error message that is persisted in the interrupt payload."""
    text = repr(value)
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


#: 原因摘要有界：errors 是给人读的顶层输出，完整细节留在产物与 checkpoint 里。
_MAX_FAILURE_REASONS = 20
_MAX_REASON_CHARS = 300

#: 一个阶段的收口原因：(MULTI_* 码, 该码下逐条的原因摘要)。
_Failure = tuple[str, list[str]]


def _bounded_reason(text: Any) -> str:
    cleaned = " ".join(str(text).split())
    return cleaned if len(cleaned) <= _MAX_REASON_CHARS else f"{cleaned[: _MAX_REASON_CHARS - 1]}…"


def _failure_errors(state: Mapping[str, Any], failures: list[_Failure]) -> list[str]:
    """errors 追加阶段收口的 MULTI_* 码与原因摘要（任务书 §10）。

    此前各阶段节点失败只设 status、不追加 errors，真实原因只在 checkpoint 的产物里，CLI 顶层输出
    ``errors=[]``（review 2026-09-30）。口径沿用 intake 的 MULTI_INVALID_BRIEF：码单独一项（调用方
    按码精确匹配），原因逐条跟在码后面、带阶段前缀；同一个码只写一次，原因去重、有界。
    """
    grouped: dict[str, list[str]] = {}
    for code, reasons in failures:
        bucket = grouped.setdefault(code, [])
        for reason in reasons:
            line = _bounded_reason(reason)
            if line and line not in bucket:
                bucket.append(line)
    errors = list(state.get("errors") or [])
    for code, lines in grouped.items():
        if len(lines) > _MAX_FAILURE_REASONS:
            hidden = len(lines) - _MAX_FAILURE_REASONS
            lines = [*lines[:_MAX_FAILURE_REASONS], f"（另有 {hidden} 条原因未列出，详见产物）"]
        errors.extend([code, *lines])
    return errors


def _field_path(loc: tuple[Any, ...]) -> str:
    """pydantic 错误位置 → ``topology[0].length_m`` 形态（与 CLI 缺项列表同一写法）。"""
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path = f"{path}.{part}" if path else str(part)
    return path or "project_brief"


def _calc_stage_failures(
    env: ArtifactEnvelope,
    package: CalcPackage | None,
) -> tuple[list[_Failure], list[str]]:
    """水力阶段收口：缺参 → MULTI_MISSING_INPUTS（缺项不得为空）；其余 → MULTI_CALC_BLOCKED。"""
    if env.status == "ready":
        return [], []
    if package is None:
        return [(MULTI_CALC_BLOCKED, [f"hydraulic: 计算产物 {env.artifact_id} 的 payload 不是合法 CalcPackage"])], []
    missing = list(package.missing_inputs)
    missing_reasons = [
        f"hydraulic: 工况 {case.calc_case_id}（{case.formula}）缺少计算输入"
        for case in package.cases
        if case.status == "missing_inputs"
    ]
    if not package.cases:
        missing.append("hydraulic_cases")
        missing_reasons.append("hydraulic: ProjectBrief 没有任何水力计算工况（hydraulic_cases 为空）")
    if env.status == "missing_inputs":
        if not missing:
            missing = [f"calc:{env.artifact_id} 缺少计算输入"]
        return [(MULTI_MISSING_INPUTS, missing_reasons)], missing
    reasons = [
        f"hydraulic: 工况 {case.calc_case_id}（{case.formula}）：{error}"
        for case in package.cases
        if case.status == "failed"
        for error in (case.errors or ["水力计算失败"])
    ]
    reasons.extend(missing_reasons)
    if not reasons:
        reasons = [
            f"hydraulic: 计算产物 {env.artifact_id} 状态为 {env.status}",
            *(f"hydraulic: {error}" for error in package.errors),
        ]
    return [(MULTI_CALC_BLOCKED, reasons)], missing


def _design_stage_failures(env: ArtifactEnvelope, package: DesignPackage, status: str) -> list[_Failure]:
    """设计阶段收口：缺参 → MULTI_MISSING_INPUTS（缺项即 unresolved_items）；
    brief 冲突 → MULTI_INVALID_BRIEF；未检索的依据、无计算支撑等 → MULTI_DESIGN_CONFLICT。"""
    if status == "missing_inputs":
        return [(MULTI_MISSING_INPUTS, [])]
    if status not in {"blocked", "failed"}:
        return []
    brief_lines = [item for item in package.unresolved_items if item.startswith("invalid project brief:")]
    if brief_lines:
        return [(MULTI_INVALID_BRIEF, brief_lines)]
    reasons = [
        f"design: 设计请求的规范依据 {source_id} 不在本轮检索集合中"
        "（retrieved_source_ids ∪ 引用校验器已确认的引用），未进入设计依据"
        for source_id in package.invalid_citation_ids
    ]
    reasons.extend(
        f"design: 管段 {segment.segment_id} 没有活动计算工况支撑（hydraulic_status=not_verified）"
        for segment in package.segments
        if segment.hydraulic_status == "not_verified"
    )
    if not reasons:
        reasons = [f"design: {item}" for item in package.unresolved_items] or [
            f"design: 设计产物 {env.artifact_id} 状态为 {env.status}",
            *(f"design: {warning}" for warning in env.warnings),
        ]
    return [(MULTI_DESIGN_CONFLICT, reasons)]


def _drawing_stage_failures(
    plan: ArtifactEnvelope,
    package: ArtifactEnvelope,
    document: ArtifactEnvelope,
) -> tuple[list[_Failure], list[str]]:
    """出图阶段收口：DrawingPlan 与上游冲突 → MULTI_DRAWING_CONFLICT；计划缺输入 →
    MULTI_MISSING_INPUTS（缺项来自 DrawingPlan）；草图渲染/回读或文档模板不可用 → MULTI_DRAWING_UNAVAILABLE。"""
    failures: list[_Failure] = []
    missing: list[str] = []
    try:
        plan_payload: DrawingPlan | None = DrawingPlan.model_validate(plan.payload)
    except ValidationError:
        plan_payload = None
    if plan.status == "blocked" or (plan_payload is not None and plan_payload.integration_conflicts):
        conflicts = list(plan_payload.integration_conflicts) if plan_payload is not None else []
        failures.append(
            (
                MULTI_DRAWING_CONFLICT,
                [f"drawing: {conflict}" for conflict in conflicts]
                or [f"drawing: DrawingPlan {plan.artifact_id} 状态为 {plan.status}"],
            )
        )
    if plan.status == "missing_inputs" or (plan_payload is not None and plan_payload.missing_inputs):
        missing = list(plan_payload.missing_inputs) if plan_payload is not None else []
        failures.append((MULTI_MISSING_INPUTS, []))
    if plan.status == "failed":
        failures.append((MULTI_DRAWING_UNAVAILABLE, [f"drawing: DrawingPlan {plan.artifact_id} 生成失败"]))
    # 计划本身就绪时，草图包失败/受阻只能是渲染或回读环节的问题（依赖、格式或环境）。
    if package.status == "failed" or (package.status != "ready" and plan.status == "ready"):
        try:
            unresolved = list(DrawingPackage.model_validate(package.payload).unresolved_items)
        except ValidationError:
            unresolved = []
        failures.append(
            (
                MULTI_DRAWING_UNAVAILABLE,
                [f"drawing: {item}" for item in unresolved]
                or [f"drawing: 草图包 {package.artifact_id} 状态为 {package.status}"],
            )
        )
    if document.status == "failed":
        document_errors = document.payload.get("errors")
        failures.append(
            (
                MULTI_DRAWING_UNAVAILABLE,
                [f"document: {item}" for item in document_errors]
                if isinstance(document_errors, list) and document_errors
                else [f"document: 设计说明 {document.artifact_id} 生成失败"],
            )
        )
    statuses = {plan.status, package.status, document.status}
    if statuses != {"ready"} and not failures:
        failures.append((MULTI_DRAWING_UNAVAILABLE, [f"drawing: 出图阶段产物状态 {sorted(statuses)}"]))
    if "missing_inputs" in statuses and not missing:
        missing = [f"drawing_plan:{plan.artifact_id} 缺少出图输入"]
    return failures, missing


def _issue_reason(issue: ReviewIssue) -> str:
    where = f" @{issue.object_ref}" if issue.object_ref else ""
    return f"review: {issue.rule_id}（{issue.owner}/{issue.severity}）{where}：{issue.message}"


def approval_decision_error(value: Any) -> str | None:
    """Validate an approval resume value: ``None`` when valid, else the reason.

    It never raises.  By the time ``interrupt()`` returns, LangGraph has already
    recorded the value in the task's resume list; an exception would persist it
    together with the ERROR, and every later resume would replay that bad value
    at interrupt #0 first — no valid decision could ever be accepted again (the
    session is poisoned).  The approval node therefore re-interrupts with the
    error instead.  Only ``{"approved": <strict bool>}`` (plus optional text
    ``reason`` and ISO-8601 ``expires_at``) is a decision.
    """
    if not isinstance(value, dict) or not isinstance(value.get("approved"), bool):
        return (
            "multi-agent approval resume requires {'approved': True/False}, "
            f"got {_short_repr(value)}"
        )
    reason = value.get("reason", "")
    expires_at = value.get("expires_at")
    if not isinstance(reason, str) or (expires_at is not None and not isinstance(expires_at, str)):
        return "multi-agent approval reason/expires_at have invalid types"
    if expires_at is not None and _parse_time(expires_at) is None:
        return "multi-agent approval expires_at must be ISO-8601 text"
    return None


def _strict_approval(value: Any) -> tuple[bool, str, str | None]:
    """Normalize a resume value that already passed ``approval_decision_error``."""
    error = approval_decision_error(value)
    if error is not None:  # callers validate first; reaching this is a wiring bug
        raise TypeError(error)
    return value["approved"], value.get("reason", ""), value.get("expires_at")


def _decision_record(
    artifacts: dict[str, ArtifactEnvelope],
    *,
    approved: bool,
    reason: str,
    package_hash: str,
    decided_at: str,
    expires_at: str | None,
) -> ApprovalRecord:
    """The human decision bound to the exact release manifest it was made on."""
    ids = sorted(artifacts)
    return ApprovalRecord(
        approved=approved,
        reason=reason,
        artifact_ids=ids,
        artifact_hashes=[compute_payload_hash(artifacts[artifact_id].payload) for artifact_id in ids],
        artifact_revisions=[artifacts[artifact_id].revision for artifact_id in ids],
        artifact_parent_artifact_ids=[
            list(artifacts[artifact_id].parent_artifact_ids) for artifact_id in ids
        ],
        artifact_collection_hash=package_hash,
        final_package_hash=package_hash,
        approved_at=decided_at,
        expires_at=expires_at,
    )


def review_disclosures(state: Mapping[str, Any]) -> list[str]:
    """当前审查包的披露项（审批请求与 CLI 输出共用一个来源）。

    只读已通过哈希校验的活动审查包；读不到就返回空——不在这里补任何内容。
    ``state`` 接受任意映射：CLI 手里是 ``app.invoke`` 返回的普通 dict。
    """
    registry = _registry(cast(MultiAgentProjectState, state))
    env = registry.get(str((state.get("active_artifact_ids") or {}).get("review", "")))
    if env is None:
        return []
    try:
        return list(ReviewPackage.model_validate(env.payload).disclosed_limitations)
    except ValidationError:
        return []


def _registry(state: MultiAgentProjectState) -> dict[str, ArtifactEnvelope]:
    result: dict[str, ArtifactEnvelope] = {}
    for artifact_id, raw in (state.get("artifacts") or {}).items():
        try:
            env = ArtifactEnvelope.model_validate(raw)
            # Re-compute payload hash to detect checkpoint tampering.
            # model_validate only checks the stored hash field is well-formed;
            # this second check verifies it still matches the actual payload.
            if not verify_envelope_hash(env):
                continue
            result[artifact_id] = env
        except ValidationError:
            continue
    return result


def _dump(registry: dict[str, ArtifactEnvelope]) -> dict[str, dict[str, Any]]:
    return {artifact_id: env.model_dump(mode="json") for artifact_id, env in registry.items()}


def safe_multi_agent_trace_id(value: Any) -> str:
    """Return an internal opaque correlation ID without retaining caller data."""
    if isinstance(value, str) and _INTERNAL_TRACE_ID_RE.fullmatch(value):
        return value
    if value is None or value == "":
        return f"ma-{uuid4().hex}"
    digest = hashlib.sha256(
        f"multi-agent-trace-id-v1\0{value}".encode()
    ).hexdigest()[:24]
    return f"ma-{digest}"


def _trace_event(event: str, **facts: Any) -> dict[str, Any]:
    """Create a local-only event from fixed structured facts, never raw text."""
    trace_id = _TRACE_ID_CONTEXT.get()
    return {
        "event": event,
        "source": _TRACE_SOURCE,
        "delivery": _TRACE_DELIVERY,
        **({"trace_id": trace_id} if trace_id is not None else {}),
        **{key: value for key, value in facts.items() if value is not None},
    }


def _with_trace_context(
    node: Callable[[MultiAgentProjectState], dict[str, Any]],
) -> Callable[[MultiAgentProjectState], dict[str, Any]]:
    """Bind the persisted run id while one graph node emits trace facts."""

    @wraps(node)
    def wrapped(state: MultiAgentProjectState) -> dict[str, Any]:
        trace_id = safe_multi_agent_trace_id(state.get("trace_id"))
        token = _TRACE_ID_CONTEXT.set(trace_id)
        try:
            result = node(state)
            result.setdefault("trace_id", trace_id)
            return result
        finally:
            _TRACE_ID_CONTEXT.reset(token)

    return wrapped


def _review_summary(
    registry: dict[str, ArtifactEnvelope],
    active: dict[str, str],
) -> dict[str, Any]:
    review_env = registry.get(active.get("review", ""))
    if review_env is None:
        return {
            "review_decision": None,
            "not_checked_count": 0,
            "blocking_issue_count": 0,
        }
    try:
        package = ReviewPackage.model_validate(review_env.payload)
    except ValidationError:
        return {
            "review_decision": None,
            "not_checked_count": 0,
            "blocking_issue_count": 0,
        }
    return {
        "review_decision": package.decision,
        "not_checked_count": len(package.not_checked),
        "blocking_issue_count": sum(
            issue.severity == "blocking" for issue in package.issues
        ),
    }


def _run_finished_event(
    state: MultiAgentProjectState,
    *,
    run_status: str,
    registry: dict[str, ArtifactEnvelope] | None = None,
    active: dict[str, str] | None = None,
) -> dict[str, Any]:
    current_registry = registry if registry is not None else _registry(state)
    current_active = active if active is not None else dict(state.get("active_artifact_ids") or {})
    return _trace_event(
        "run_finished",
        run_status=run_status,
        **_review_summary(current_registry, current_active),
    )


def _artifact_trace_events(
    env: ArtifactEnvelope,
    *,
    registry_before: dict[str, ArtifactEnvelope],
    active_before: dict[str, str],
    active_after: dict[str, str],
) -> list[dict[str, Any]]:
    events = [
        _trace_event(
            "artifact_created",
            artifact_id=env.artifact_id,
            artifact_type=env.artifact_type,
            artifact_hash=env.payload_hash,
            status=env.status,
        )
    ]
    events.extend(
        _invalidation_trace_events(
            registry_before=registry_before,
            active_before=active_before,
            active_after=active_after,
        )
    )
    return events


def _invalidation_trace_events(
    *,
    registry_before: dict[str, ArtifactEnvelope],
    active_before: dict[str, str],
    active_after: dict[str, str],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for role, artifact_id in active_before.items():
        if active_after.get(role) == artifact_id:
            continue
        old = registry_before.get(artifact_id)
        events.append(
            _trace_event(
                "artifact_invalidated",
                artifact_id=artifact_id,
                artifact_type=old.artifact_type if old is not None else None,
                owner=role,
            )
        )
    return events


def _brief_with_recalculation_updates(
    state: MultiAgentProjectState,
    registry: dict[str, ArtifactEnvelope],
    active: dict[str, str],
) -> tuple[ProjectBrief | None, list[str]]:
    """Apply only explicit, source-bound numeric design requests to the brief."""
    design_env = registry.get(active.get("design", ""))
    calc_env = registry.get(active.get("calc", ""))
    if design_env is None or calc_env is None:
        return None, ["recalculation source artifacts"]
    try:
        brief = ProjectBrief.model_validate(state.get("project_brief") or {})
        design = DesignPackage.model_validate(design_env.payload)
        calc_package = CalcPackage.model_validate(calc_env.payload)
    except ValidationError:
        return None, ["recalculation source payload"]

    requests = design.recalculation_requests
    if not requests:
        return None, ["recalculation_requests"]

    source_cases = {case.calc_case_id: case for case in calc_package.cases}
    updates_by_source: dict[str, dict[str, float]] = {}
    length_updates: list[tuple[str, float]] = []
    missing: list[str] = []

    for index, request in enumerate(requests):
        prefix = f"recalculation_requests[{index}]"
        if not isinstance(request, dict):
            missing.append(prefix)
            continue
        fields_raw = request.get("fields")
        fields = (
            {field for field in fields_raw if isinstance(field, str) and field}
            if isinstance(fields_raw, list)
            else set()
        )
        source_case_id = request.get("source_calc_case_id")
        formula = request.get("formula")
        input_updates = request.get("input_updates")
        if not fields:
            missing.append(f"{prefix}.fields")
        for field in sorted(fields):
            if field not in _RECALCULABLE_FIELDS:
                missing.append(f"{prefix}.fields.{field}")
        if not isinstance(source_case_id, str) or not source_case_id:
            missing.append(f"{prefix}.source_calc_case_id")
        if not isinstance(formula, str) or not formula:
            missing.append(f"{prefix}.formula")
        if not isinstance(input_updates, dict):
            input_updates = {}
        for field in sorted(fields):
            if field not in input_updates:
                missing.append(f"{prefix}.input_updates.{field}")
        for field in sorted(set(input_updates) - fields):
            missing.append(f"{prefix}.fields.{field}")

        source = source_cases.get(source_case_id) if isinstance(source_case_id, str) else None
        if source is None and isinstance(source_case_id, str) and source_case_id:
            missing.append(f"{prefix}.source_calc_case_id")
        elif source is not None and formula != source.formula:
            missing.append(f"{prefix}.formula")

        cleaned_updates: dict[str, float] = {}
        for field, value in input_updates.items():
            if field not in fields or field not in _RECALCULABLE_FIELDS:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                missing.append(f"{prefix}.input_updates.{field}")
                continue
            numeric = float(value)
            if not math.isfinite(numeric):
                missing.append(f"{prefix}.input_updates.{field}")
                continue
            cleaned_updates[field] = numeric

        if source is not None and cleaned_updates:
            combined = updates_by_source.setdefault(source.calc_case_id, {})
            for field, value in cleaned_updates.items():
                previous = combined.get(field)
                if previous is not None and previous != value:
                    missing.append(f"{prefix}.input_updates.{field}")
                else:
                    combined[field] = value
            segment_id = request.get("segment_id")
            if "length_m" in cleaned_updates:
                if not isinstance(segment_id, str) or not segment_id:
                    missing.append(f"{prefix}.segment_id")
                else:
                    length_updates.append((segment_id, cleaned_updates["length_m"]))

    if missing:
        return None, list(dict.fromkeys(missing))

    brief_data = brief.model_dump(mode="json")
    case_rows = brief_data["hydraulic_cases"]
    changed = False
    used_case_indexes: set[int] = set()
    for source_case_id, updates in updates_by_source.items():
        source = source_cases[source_case_id]
        candidates = [
            index
            for index, row in enumerate(case_rows)
            if index not in used_case_indexes
            and (
                row["case_id"] == source.calc_case_id
                or (
                    row["formula"] == source.formula
                    and row["inputs"] == source.inputs
                )
            )
        ]
        if len(candidates) != 1:
            missing.append(f"source_calc_case_id.{source_case_id}.project_brief_case")
            continue
        case_index = candidates[0]
        used_case_indexes.add(case_index)
        old_inputs = dict(source.inputs)
        new_inputs = {**old_inputs, **updates}
        if new_inputs == old_inputs:
            missing.append(f"source_calc_case_id.{source_case_id}.new_input_value")
            continue
        case_rows[case_index]["inputs"] = new_inputs
        changed = True

    design_segments = {segment.segment_id: segment for segment in design.segments}
    for segment_id, value in length_updates:
        segment = design_segments.get(segment_id)
        if segment is None:
            missing.append(f"segment_id.{segment_id}")
            continue
        # Two edges may share both nodes in different declared systems; the
        # segment's own system selects its edge.
        edge_candidates = [
            edge
            for edge in brief_data["topology"]
            if edge["start_node_id"] == segment.start_node_id
            and edge["end_node_id"] == segment.end_node_id
            and edge.get("system") in (None, segment.system)
        ]
        if len(edge_candidates) != 1:
            missing.append(f"segment_id.{segment_id}.topology")
            continue
        edge_candidates[0]["length_m"] = value

    if missing or not changed:
        return None, list(dict.fromkeys(missing or ["recalculation input value"]))
    try:
        return ProjectBrief.model_validate(brief_data), []
    except ValidationError:
        return None, ["recalculation input value"]


def _put(
    registry: dict[str, ArtifactEnvelope], active: dict[str, str], env: ArtifactEnvelope,
    *,
    role: str,
    invalidates: AbstractSet[str] = frozenset(),
    keep_previous: bool = False,
) -> tuple[dict[str, ArtifactEnvelope], dict[str, str]]:
    registry, active = dict(registry), dict(active)
    for active_role, artifact_id in list(active.items()):
        old = registry.get(artifact_id)
        if old is not None and old.artifact_type in invalidates:
            registry.pop(artifact_id, None)
            active.pop(active_role, None)
    previous = active.get(role)
    if previous and not keep_previous:
        registry.pop(previous, None)
    registry[env.artifact_id] = env
    active[role] = env.artifact_id
    return registry, active


def _release_artifacts(registry: dict[str, ArtifactEnvelope], active: dict[str, str]) -> dict[str, ArtifactEnvelope] | None:
    if any(role not in active or active[role] not in registry for role in _RELEASE_ROLES):
        return None
    return {active[role]: registry[active[role]] for role in _RELEASE_ROLES}


def approval_summary(state: Mapping[str, Any]) -> dict[str, Any] | None:
    """待批准最终包的可审阅摘要（审批请求、CLI 与最终输出共用这一个来源）。

    此前审批请求只有最终包哈希，批准人看不到要批准的是什么（盲批，review 2026-09-30）。
    摘要只读当前发布集里已通过哈希校验的登记产物，原样搬运其中的数值与文本——不重新计算、
    不换算、不取整任何数值（铁律 1）；发布集不完整（没有可批准的最终包）时返回 None。
    ``state`` 接受任意映射：CLI 手里是 ``app.invoke`` 返回的普通 dict。
    """
    registry = _registry(cast(MultiAgentProjectState, state))
    active = dict(state.get("active_artifact_ids") or {})
    artifacts = _release_artifacts(registry, active)
    if artifacts is None:
        return None
    envelopes = {role: registry[active[role]] for role in _RELEASE_ROLES}
    try:
        calc = CalcPackage.model_validate(envelopes["calc"].payload)
        design = DesignPackage.model_validate(envelopes["design"].payload)
        drawing = DrawingPackage.model_validate(envelopes["drawing"].payload)
        review = ReviewPackage.model_validate(envelopes["review"].payload)
    except ValidationError:
        return None
    staging_dir = state.get("staging_dir")
    staged_file_id = drawing.staged_file_id if drawing.staged_file_verified else None
    # 与 drawing_executor / export_adapter 同一个命名规则：<staging_dir>/<file_id>.dxf
    staging_dxf_path = (
        str(Path(staging_dir) / f"{staged_file_id}.dxf")
        if isinstance(staging_dir, str) and staging_dir and staged_file_id
        else None
    )
    citations: dict[str, dict[str, Any]] = {}
    for citation in design.citations:
        citations.setdefault(
            citation.source_id,
            {"source_id": citation.source_id, "version_status": citation.version_status},
        )
    return {
        "final_package_hash": _package_hash(artifacts),
        "artifacts": [
            {
                "role": role,
                "artifact_type": envelopes[role].artifact_type,
                "artifact_id": envelopes[role].artifact_id,
                "revision": envelopes[role].revision,
                "payload_hash_prefix": envelopes[role].payload_hash[:_HASH_PREFIX_CHARS],
                "status": envelopes[role].status,
            }
            for role in _RELEASE_ROLES
        ],
        "segments": [
            {
                "segment_id": segment.segment_id,
                "system": segment.system,
                "start_node_id": segment.start_node_id,
                "end_node_id": segment.end_node_id,
                "dn_mm": segment.dn_mm,
                "length_m": segment.length_m,
                "material": segment.material,
                "slope": segment.slope,
                "hydraulic_status": segment.hydraulic_status,
                "calc_case_ids": list(segment.calc_case_ids),
            }
            for segment in design.segments
        ],
        "calc_cases": [
            {
                "calc_case_id": case.calc_case_id,
                "formula": case.formula,
                "status": case.status,
                "inputs": dict(case.inputs),
                "outputs": dict(case.outputs),
                "assumptions": list(case.assumptions),
            }
            for case in calc.cases
        ],
        "citations": list(citations.values()),
        "review": {
            "decision": review.decision,
            "issues": [
                {
                    "rule_id": issue.rule_id,
                    "severity": issue.severity,
                    "owner": issue.owner,
                    "object_ref": issue.object_ref,
                    "message": issue.message,
                }
                for issue in review.issues
            ],
            "not_checked": list(review.not_checked),
            "disclosed_limitations": list(review.disclosed_limitations),
        },
        "staging_dxf_path": staging_dxf_path,
    }


def _package_hash(artifacts: dict[str, ArtifactEnvelope]) -> str:
    ids = sorted(artifacts)
    return compute_artifact_collection_hash(
        [compute_payload_hash(artifacts[artifact_id].payload) for artifact_id in ids],
        artifact_ids=ids,
        artifact_revisions=[artifacts[artifact_id].revision for artifact_id in ids],
        artifact_parent_artifact_ids=[
            list(artifacts[artifact_id].parent_artifact_ids) for artifact_id in ids
        ],
    )


def _document_from_plan(plan_env: ArtifactEnvelope, calc_env: ArtifactEnvelope, design_env: ArtifactEnvelope) -> ArtifactEnvelope:
    plan = DrawingPlan.model_validate(plan_env.payload)
    docgen = plan.output_adaptations.get("docgen")
    rendered = docgen.get("rendered") if isinstance(docgen, dict) else None
    declared_status = docgen.get("status") if isinstance(docgen, dict) else None
    allowed_statuses = {"ready", "missing_inputs", "blocked", "failed"}
    unresolved = (
        [str(item) for item in docgen.get("unresolved_items", [])]
        if isinstance(docgen, dict) and isinstance(docgen.get("unresolved_items"), list)
        else []
    )
    if isinstance(rendered, dict):
        payload = {
            "doc_type": rendered.get("doc_type", "design_note"),
            "citation_ids": list(rendered.get("citation_ids") or []),
            "calc_values": dict(rendered.get("calc_values") or {}),
            "sections": list(rendered.get("sections") or []),
        }
        status: Literal["ready", "missing_inputs", "blocked", "failed"] = (
            cast(Literal["ready", "missing_inputs", "blocked", "failed"], declared_status)
            if declared_status in allowed_statuses
            else "failed"
        )
        if status == "missing_inputs":
            payload["missing_inputs"] = unresolved or ["docgen adaptation missing inputs"]
        elif status in {"blocked", "failed"}:
            payload["errors"] = unresolved or ["docgen adaptation unavailable"]
    else:
        payload = {"doc_type": "design_note", "citation_ids": [], "calc_values": {}, "sections": [], "errors": ["docgen adaptation missing"]}
        status = "failed"
    payload_hash = compute_payload_hash(payload)
    return ArtifactEnvelope(
        artifact_id=f"doc-{hashlib.sha256(f'{plan_env.artifact_id}:{payload_hash}'.encode()).hexdigest()[:16]}",
        artifact_type="document", schema_version=_SCHEMA_VERSION, producer="orchestrator", revision=plan_env.revision,
        parent_artifact_ids=[calc_env.artifact_id, design_env.artifact_id, plan_env.artifact_id],
        payload_hash=payload_hash, payload=payload, citation_ids=list(payload["citation_ids"]),
        assumptions=[], warnings=[], status=status,
    )


def _final_package(artifacts: dict[str, ArtifactEnvelope], approval: ApprovalRecord) -> FinalProjectPackage:
    by_type = {env.artifact_type: env for env in artifacts.values()}
    return FinalProjectPackage(
        calc_artifact_id=by_type["calc_package"].artifact_id,
        design_artifact_id=by_type["design_package"].artifact_id,
        document_artifact_id=by_type["document"].artifact_id,
        drawing_plan_artifact_id=by_type["drawing_plan"].artifact_id,
        drawing_artifact_id=by_type["drawing_package"].artifact_id,
        review_artifact_id=by_type["review_package"].artifact_id,
        approval_record=approval, release_manifest_hash=_package_hash(artifacts),
    )


def is_approval_valid(
    approval: ApprovalRecord,
    current: list[str] | dict[str, ArtifactEnvelope] | list[ArtifactEnvelope],
) -> bool:
    """Validate legacy hash lists or the complete canonical artifact binding."""
    if isinstance(current, dict):
        return validate_approval_binding(approval, current)
    if current and isinstance(current[0], ArtifactEnvelope):
        return validate_approval_binding(approval, cast(list[ArtifactEnvelope], current))
    return sorted(approval.artifact_hashes) == sorted(cast(list[str], current))


def build_orchestrator_graph(
    *, checkpointer: Any, now: Callable[[], datetime] | None = None,
    approval_ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
    hydraulic_runner: Callable[..., ArtifactEnvelope] = run_hydraulic_agent,
    design_runner: Callable[..., ArtifactEnvelope] = run_design_agent,
    drawing_runner: Callable[..., ArtifactEnvelope] = run_drawing_agent,
    review_runner: Callable[..., ArtifactEnvelope] = run_review_agent,
    drawing_stager: Callable[..., ArtifactEnvelope] = stage_drawing_package,
    exporter: Callable[..., str] = export_adapter.assemble_export_path,
    version_registry: Any | None = None,
) -> Any:
    """Compile the only P4.5 orchestration graph; interrupt needs persistence.

    ``version_registry`` 缺省用 ``data/versions.json`` 单例（与 legacy/agentic RAG 图同口径），
    交给设计 Agent 解析设计依据的规范版本状态——此前从不传，版本状态恒为 None。
    """
    if checkpointer is None:
        raise ValueError("multi-agent graph requires a persistent checkpointer for interrupt/resume")
    if approval_ttl_seconds <= 0:
        raise ValueError("approval_ttl_seconds must be positive")
    clock = now or (lambda: datetime.now(UTC))
    registry_for_design = version_registry if version_registry is not None else get_version_registry()

    def consume(state: MultiAgentProjectState, phase: str) -> dict[str, Any] | None:
        if state.get("step_count", 0) >= MAX_MULTI_AGENT_STEPS:
            return {
                "phase": phase,
                "status": "blocked",
                "errors": _failure_errors(
                    state,
                    [
                        (
                            MULTI_RETRY_EXHAUSTED,
                            [f"{phase}: 已达多 Agent 步数上限 {MAX_MULTI_AGENT_STEPS}，不再调用任何 Agent"],
                        )
                    ],
                ),
                "trace_events": [_run_finished_event(state, run_status="blocked")],
            }
        return None

    def intake(state: MultiAgentProjectState) -> dict[str, Any]:
        events = [_trace_event("run_started", phase="intake")]
        try:
            brief = ProjectBrief.model_validate(state.get("project_brief") or {})
        except ValidationError as exc:
            # 字段级校验失败同样是 MULTI_INVALID_BRIEF（此前只有一句 str(exc)、不带码），缺项列出要改的字段；
            # 原因只取字段位置与校验说明，不回显输入值。
            details = exc.errors()
            fields = [_field_path(tuple(error.get("loc", ()))) for error in details]
            reasons = [
                f"invalid project brief: {field}: {error.get('msg', 'invalid')}"
                for field, error in zip(fields, details, strict=True)
            ]
            events.append(_run_finished_event(state, run_status="missing_inputs"))
            return {
                "phase": "intake",
                "status": "missing_inputs",
                "errors": _failure_errors(state, [(MULTI_INVALID_BRIEF, reasons)]),
                "missing_inputs": [*state.get("missing_inputs", []), *dict.fromkeys(fields)],
                "trace_events": events,
            }
        # 跨字段冲突（重复管段 (system, start, end)、引用未声明的系统/工况、重复 ID）在任何
        # Agent 运行前结构化拒绝；此前会在设计阶段抛裸 ValidationError 冒出 app.invoke。
        conflicts = project_brief_conflicts(brief)
        if conflicts:
            events.append(_run_finished_event(state, run_status="missing_inputs"))
            return {
                "phase": "intake",
                "status": "missing_inputs",
                "errors": _failure_errors(
                    state,
                    [(MULTI_INVALID_BRIEF, [f"invalid project brief: {conflict}" for conflict in conflicts])],
                ),
                "missing_inputs": [*state.get("missing_inputs", []), *project_brief_conflict_fields(brief)],
                "trace_events": events,
            }
        return {"phase": "intake", "status": "running", "trace_events": events}

    def hydraulic(state: MultiAgentProjectState) -> dict[str, Any]:
        if result := consume(state, "hydraulic"):
            return result
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        registry_before, active_before = dict(registry), dict(active)
        env = hydraulic_runner(
            ProjectBrief.model_validate(state["project_brief"]),
            supersedes_artifact_id=active.get("calc"),
        )
        registry, active = _put(
            registry,
            active,
            env,
            role="calc",
            invalidates={
                "design_package",
                "drawing_plan",
                "drawing_package",
                "document",
                "review_package",
            },
            keep_previous=True,
        )
        try:
            package: CalcPackage | None = CalcPackage.model_validate(env.payload)
        except ValidationError:
            package = None
        failures, missing = _calc_stage_failures(env, package)
        status = "running" if env.status == "ready" else env.status
        events = [
            _trace_event("agent_call", owner="hydraulic", after_exhaustion=False),
            *_artifact_trace_events(
                env,
                registry_before=registry_before,
                active_before=active_before,
                active_after=active,
            ),
        ]
        if status != "running":
            events.append(
                _run_finished_event(
                    state,
                    run_status=status,
                    registry=registry,
                    active=active,
                )
            )
        return {
            "phase": "hydraulic",
            "artifacts": _dump(registry),
            "active_artifact_ids": active,
            "step_count": state.get("step_count", 0) + 1,
            "status": status,
            "missing_inputs": [*state.get("missing_inputs", []), *missing],
            **({"errors": _failure_errors(state, failures)} if failures else {}),
            "trace_events": events,
        }

    def design(state: MultiAgentProjectState) -> dict[str, Any]:
        if result := consume(state, "design"):
            return result
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        registry_before, active_before = dict(registry), dict(active)
        calc = registry[active["calc"]]
        previous = DesignPackage.model_validate(registry[active["design"]].payload) if active.get("design") in registry else None
        env = design_runner(
            ProjectBrief.model_validate(state["project_brief"]),
            calc,
            retrieved_source_ids=list(state.get("retrieved_source_ids") or []),
            prev_design=previous,
            version_registry=registry_for_design,
        )
        registry, active = _put(registry, active, env, role="design", invalidates={"design_package", "drawing_plan", "drawing_package", "document", "review_package"})
        package = DesignPackage.model_validate(env.payload)
        status = "running" if env.status == "ready" or package.recalculation_requests else env.status
        failures = _design_stage_failures(env, package, status)
        missing = list(package.unresolved_items)
        if status == "missing_inputs" and not missing:
            missing = [f"design:{env.artifact_id} 缺少设计输入"]
        events = [
            _trace_event("agent_call", owner="design", after_exhaustion=False),
            *_artifact_trace_events(
                env,
                registry_before=registry_before,
                active_before=active_before,
                active_after=active,
            ),
        ]
        if status != "running":
            events.append(
                _run_finished_event(
                    state,
                    run_status=status,
                    registry=registry,
                    active=active,
                )
            )
        return {
            "phase": "design",
            "artifacts": _dump(registry),
            "active_artifact_ids": active,
            "step_count": state.get("step_count", 0) + 1,
            "status": status,
            "missing_inputs": [*state.get("missing_inputs", []), *missing],
            **({"errors": _failure_errors(state, failures)} if failures else {}),
            "trace_events": events,
        }

    def recalculate(state: MultiAgentProjectState) -> dict[str, Any]:
        counts = dict(state.get("revision_counts") or {})
        if counts.get("hydraulic", 0) >= MAX_HYDRAULIC_REVISIONS:
            return {
                "phase": "recalculate",
                "status": "blocked",
                "errors": _failure_errors(
                    state,
                    [
                        (
                            MULTI_RETRY_EXHAUSTED,
                            [
                                (
                                    f"recalculate: 水力重算已达上限 {MAX_HYDRAULIC_REVISIONS} 次，"
                                    "设计仍要求重算，不再调用水力 Agent"
                                )
                            ],
                        )
                    ],
                ),
                "trace_events": [_run_finished_event(state, run_status="blocked")],
            }
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        updated_brief, missing = _brief_with_recalculation_updates(
            state,
            registry,
            active,
        )
        if updated_brief is None:
            # 必须重算却执行不了：MULTI_RECALC_REQUIRED 说明为什么停，MULTI_MISSING_INPUTS 指向缺项。
            reason = (
                "recalculate: 需要重算水力工况，但设计产物里没有可执行的结构化重算请求"
                if missing == ["recalculation_requests"]
                else "recalculate: 设计改变了水力输入、必须重算，但重算请求缺少可执行的结构化输入（缺项见 missing_inputs）"
            )
            return {
                "phase": "recalculate",
                "status": "waiting",
                "route_target": "human",
                "errors": _failure_errors(
                    state,
                    [(MULTI_RECALC_REQUIRED, [reason]), (MULTI_MISSING_INPUTS, [])],
                ),
                "missing_inputs": [*state.get("missing_inputs", []), *missing],
                "trace_events": [_run_finished_event(state, run_status="waiting")],
            }
        counts["hydraulic"] = counts.get("hydraulic", 0) + 1
        return {
            "phase": "recalculate",
            "revision_counts": counts,
            "route_target": "hydraulic",
            "project_brief": updated_brief.model_dump(mode="json"),
            "status": "running",
        }

    def drawing(state: MultiAgentProjectState) -> dict[str, Any]:
        if result := consume(state, "drawing"):
            return result
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        events = [_trace_event("agent_call", owner="drawing", after_exhaustion=False)]
        calc, design_env = registry[active["calc"]], registry[active["design"]]
        plan = drawing_runner(design_env, calc, drawing_id=state.get("output_file_id") or "project-drawing")
        registry_before, active_before = dict(registry), dict(active)
        registry, active = _put(registry, active, plan, role="drawing_plan", invalidates={"drawing_plan", "drawing_package", "document", "review_package"})
        events.extend(
            _artifact_trace_events(
                plan,
                registry_before=registry_before,
                active_before=active_before,
                active_after=active,
            )
        )
        package = drawing_stager(plan, staging_dir=state["staging_dir"], file_id=state.get("output_file_id") or "project-drawing")
        registry_before, active_before = dict(registry), dict(active)
        registry, active = _put(registry, active, package, role="drawing", invalidates={"drawing_package", "document", "review_package"})
        events.extend(
            _artifact_trace_events(
                package,
                registry_before=registry_before,
                active_before=active_before,
                active_after=active,
            )
        )
        document = _document_from_plan(plan, calc, design_env)
        registry_before, active_before = dict(registry), dict(active)
        registry, active = _put(registry, active, document, role="document", invalidates={"document", "review_package"})
        events.extend(
            _artifact_trace_events(
                document,
                registry_before=registry_before,
                active_before=active_before,
                active_after=active,
            )
        )
        statuses = {plan.status, package.status, document.status}
        status = "running" if statuses == {"ready"} else ("failed" if "failed" in statuses else ("blocked" if "blocked" in statuses else "missing_inputs"))
        failures, missing = _drawing_stage_failures(plan, package, document) if status != "running" else ([], [])
        if status != "running":
            events.append(
                _run_finished_event(
                    state,
                    run_status=status,
                    registry=registry,
                    active=active,
                )
            )
        return {
            "phase": "drawing",
            "artifacts": _dump(registry),
            "active_artifact_ids": active,
            "step_count": state.get("step_count", 0) + 1,
            "status": status,
            # 出图阶段此前不写 missing_inputs：$INSUNITS 不支持等缺项只在 DrawingPlan 里，顶层为空列表
            "missing_inputs": [*state.get("missing_inputs", []), *missing],
            **({"errors": _failure_errors(state, failures)} if failures else {}),
            "trace_events": events,
        }

    def review(state: MultiAgentProjectState) -> dict[str, Any]:
        if result := consume(state, "review"):
            return result
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        registry_before, active_before = dict(registry), dict(active)
        counts = dict(state.get("revision_counts") or {})
        counts["review"] = counts.get("review", 0) + 1
        env = review_runner(registry, review_id="review-project", document_artifact_id=active.get("document"), revision=counts["review"])
        registry, active = _put(registry, active, env, role="review", invalidates={"review_package"})
        package = ReviewPackage.model_validate(env.payload)
        events = [
            _trace_event("agent_call", owner="review", after_exhaustion=False),
            *_artifact_trace_events(
                env,
                registry_before=registry_before,
                active_before=active_before,
                active_after=active,
            ),
            *[
                _trace_event(
                    "review_issue",
                    artifact_id=issue.artifact_id,
                    severity=issue.severity,
                    owner=issue.owner,
                    rule_id=issue.rule_id,
                )
                for issue in package.issues
            ],
        ]
        return {
            "phase": "review",
            "artifacts": _dump(registry),
            "active_artifact_ids": active,
            "revision_counts": counts,
            "step_count": state.get("step_count", 0) + 1,
            "status": "running",
            "trace_events": events,
        }

    def router(state: MultiAgentProjectState) -> dict[str, Any]:
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        env = registry.get(active.get("review", ""))
        if env is None:
            events = [
                _trace_event(
                    "route_selected",
                    review_owner=None,
                    next_owner=None,
                    exhausted=False,
                    blocked=True,
                    error_code=MULTI_REVIEW_REJECTED,
                ),
                _run_finished_event(
                    state,
                    run_status="blocked",
                    registry=registry,
                    active=active,
                ),
            ]
            return {
                "phase": "router",
                "status": "blocked",
                "errors": _failure_errors(
                    state,
                    [(MULTI_REVIEW_REJECTED, ["review: 没有可用的审查包（缺失或未通过哈希校验），不能放行"])],
                ),
                "trace_events": events,
            }
        package = ReviewPackage.model_validate(env.payload)
        review_owner = next_owner_for_issues(package.issues)
        if package.decision == "blocked":
            events = [
                _trace_event(
                    "route_selected",
                    review_owner=review_owner,
                    next_owner=review_owner,
                    exhausted=False,
                    blocked=True,
                    error_code=MULTI_REVIEW_REJECTED,
                ),
                _run_finished_event(
                    state,
                    run_status="blocked",
                    registry=registry,
                    active=active,
                ),
            ]
            blocking = [
                _issue_reason(issue) for issue in package.issues if issue.severity in {"blocking", "error"}
            ]
            return {
                "phase": "router",
                "status": "blocked",
                "errors": _failure_errors(
                    state,
                    [(MULTI_REVIEW_REJECTED, blocking or ["review: 审查结论为 blocked"])],
                ),
                "trace_events": events,
            }
        decision = route_review(package, revision_counts=dict(state.get("revision_counts") or {}))
        route_event = _trace_event(
            "route_selected",
            review_owner=review_owner,
            next_owner=decision.get("next_owner"),
            exhausted=bool(decision.get("exhausted")),
            blocked=bool(decision.get("blocked")),
            error_code=decision.get("error_code"),
        )
        if decision.get("blocked"):
            return {
                "phase": "router",
                "status": "blocked",
                "route_target": None,
                "errors": _failure_errors(
                    state, [(decision["error_code"], [f"router: {decision.get('reason', '')}"])]
                ),
                "trace_events": [
                    route_event,
                    _run_finished_event(
                        state,
                        run_status="blocked",
                        registry=registry,
                        active=active,
                    ),
                ],
            }
        owner = decision.get("next_owner")
        if owner is None:
            status = "running" if package.decision == "pass" and not package.not_checked else "waiting"
            events = [route_event]
            if status == "waiting":
                events.append(
                    _run_finished_event(
                        state,
                        run_status="waiting",
                        registry=registry,
                        active=active,
                    )
                )
            return {
                "phase": "router",
                "route_target": "approval" if status == "running" else "human",
                "status": status,
                **(
                    {
                        "errors": _failure_errors(
                            state,
                            [
                                (
                                    MULTI_REVIEW_REJECTED,
                                    [f"review: 未检查：{item}" for item in package.not_checked]
                                    or [f"review: 审查结论为 {package.decision}，没有可回流的责任方"],
                                )
                            ],
                        )
                    }
                    if status == "waiting"
                    else {}
                ),
                "trace_events": events,
            }
        if owner == "human":
            # 只有人能处理的审查问题（例如 P3 在草图上查出的缺陷）：停在 waiting，原因带到顶层输出。
            human_issues = [_issue_reason(issue) for issue in package.issues if issue.owner == "human"]
            return {
                "phase": "router",
                "route_target": "human",
                "status": "waiting",
                "errors": _failure_errors(state, [(MULTI_REVIEW_REJECTED, human_issues)]),
                "trace_events": [
                    route_event,
                    _run_finished_event(
                        state,
                        run_status="waiting",
                        registry=registry,
                        active=active,
                    ),
                ],
            }
        counts = dict(state.get("revision_counts") or {})
        # Hydraulic feedback must pass through the structured recalculate node.
        # That node owns the hydraulic revision counter after it validates and
        # applies explicit input_updates; incrementing here would consume a
        # retry budget before a calculation has actually been requested.
        if owner != "hydraulic":
            counts[owner] = counts.get(owner, 0) + 1
        registry_before, active_before = dict(registry), dict(active)
        invalidates = {
            # Keep the DesignPackage alive until recalculate has consumed its
            # source-bound request.  A new CalcPackage then invalidates it
            # transitively in the hydraulic node.
            "hydraulic": {"drawing_plan", "drawing_package", "document", "review_package"},
            "design": {"drawing_plan", "drawing_package", "document", "review_package"},
            "drawing": {"drawing_plan", "drawing_package", "document", "review_package"},
        }[owner]
        for role, artifact_id in list(active.items()):
            artifact = registry.get(artifact_id)
            if artifact is not None and artifact.artifact_type in invalidates:
                registry.pop(artifact_id, None)
                active.pop(role, None)
        return {
            "phase": "router",
            "route_target": "recalculate" if owner == "hydraulic" else owner,
            "revision_counts": counts,
            "artifacts": _dump(registry),
            "active_artifact_ids": active,
            "status": "running",
            "trace_events": [
                route_event,
                *_invalidation_trace_events(
                    registry_before=registry_before,
                    active_before=active_before,
                    active_after=active,
                ),
            ],
        }

    def prepare_approval(state: MultiAgentProjectState) -> dict[str, Any]:
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        artifacts = _release_artifacts(registry, active)
        requested = _trace_event(
            "approval_requested",
            approval_present=False,
            approval_valid=False,
            review_present=bool(active.get("review") in registry),
        )
        if artifacts is None:
            absent = [role for role in _RELEASE_ROLES if active.get(role) not in registry]
            return {
                "phase": "prepare_approval",
                "status": "blocked",
                "errors": _failure_errors(
                    state,
                    [
                        (
                            MULTI_APPROVAL_REQUIRED,
                            [f"approval: 发布产物集不完整（缺 {', '.join(absent)}），无法请求人工审批"],
                        )
                    ],
                ),
                "trace_events": [
                    requested,
                    _run_finished_event(
                        state,
                        run_status="blocked",
                        registry=registry,
                        active=active,
                    ),
                ],
            }
        return {
            "phase": "prepare_approval",
            "approval_context_hash": _package_hash(artifacts),
            "status": "waiting",
            "trace_events": [requested],
        }

    def approval_stale(state: MultiAgentProjectState, *, approval_present: bool) -> dict[str, Any]:
        # 漂移在 approval 节点收口，只记一次 STALE（此前无条件进 release_gate 又补一个）。
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        return {
            "phase": "approval",
            "status": "blocked",
            "errors": [*state.get("errors", []), MULTI_APPROVAL_STALE],
            "trace_events": [
                _trace_event(
                    "approval_resumed",
                    approval_present=approval_present,
                    approval_valid=False,
                    package_hash_matches=False,
                    blocked=True,
                    error_code=MULTI_APPROVAL_STALE,
                ),
                _run_finished_event(state, run_status="blocked", registry=registry, active=active),
            ],
        }

    def approval(state: MultiAgentProjectState) -> dict[str, Any]:
        artifacts = _release_artifacts(_registry(state), dict(state.get("active_artifact_ids") or {}))
        expected_hash = _package_hash(artifacts) if artifacts else None
        if artifacts is None or expected_hash != state.get("approval_context_hash"):
            return approval_stale(state, approval_present=True)
        # 披露项与可审阅摘要随审批请求一起给到批准人（带条件进入人审）：此前请求里只有一个
        # 哈希，人工批准实际是盲批（review 2026-09-30）。摘要与 expected_hash 读的是同一个发布集。
        request = {
            "approval_required": True,
            "final_package_hash": expected_hash,
            "artifact_ids": sorted(artifacts),
            "expires_in_seconds": approval_ttl_seconds,
            "disclosed_limitations": review_disclosures(state),
            "summary": approval_summary(state),
        }
        decision = interrupt(request)
        # 非法 resume 值不抛异常：带 error 再挂起一次（原因见 approval_decision_error）。每次恢复
        # 本节点都从头重跑、按序号依次重放此前收到的值：非法值仍落在原序号上被判非法，合法值
        # 接在最后被取到；没有新值时停在这里继续挂起。
        while (error := approval_decision_error(decision)) is not None:
            decision = interrupt({**request, "error": error})
        approved, reason, supplied_expiry = _strict_approval(decision)
        # The restart boundary must recompute the persisted context before accepting approval.
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        artifacts = _release_artifacts(registry, active)
        resumed_hash = _package_hash(artifacts) if artifacts else None
        if artifacts is None or resumed_hash is None or resumed_hash != expected_hash or resumed_hash != state.get("approval_context_hash"):
            return approval_stale(state, approval_present=approved)
        timestamp = clock()
        if not approved:
            # 人工拒绝在这里收口，不进 release_gate（那里会把"无批准记录"再判成 STALE、并发出
            # export_attempt 让 unapproved_export 误报）。拒绝本身写进状态并绑定被拒绝的那个
            # 最终包哈希——此前原因只在 LangGraph 的 writes 表里，state.approval_record 为空。
            rejection = _decision_record(
                artifacts,
                approved=False,
                reason=reason or "rejected",
                package_hash=resumed_hash,
                decided_at=_iso(timestamp),
                expires_at=None,
            )
            return {
                "phase": "approval",
                "status": "blocked",
                "approval_record": rejection.model_dump(mode="json"),
                "errors": [*state.get("errors", []), MULTI_APPROVAL_REQUIRED],
                "trace_events": [
                    _trace_event(
                        "approval_resumed",
                        approval_present=False,
                        approval_valid=False,
                        package_hash_matches=True,
                        blocked=True,
                        error_code=MULTI_APPROVAL_REQUIRED,
                        approval_decision="rejected",
                    ),
                    _run_finished_event(state, run_status="blocked", registry=registry, active=active),
                ],
            }
        expiry = supplied_expiry or _iso(timestamp + timedelta(seconds=approval_ttl_seconds))
        record = _decision_record(
            artifacts,
            approved=True,
            reason=reason or "approved",
            package_hash=resumed_hash,
            decided_at=_iso(timestamp),
            expires_at=expiry,
        )
        final = _final_package(artifacts, record)
        return {
            "phase": "approval",
            "approval_record": record.model_dump(mode="json"),
            "final_project_package": final.model_dump(mode="json"),
            "status": "running",
            "trace_events": [
                _trace_event(
                    "approval_resumed",
                    approval_present=True,
                    approval_valid=True,
                    package_hash_matches=True,
                    blocked=False,
                    approval_decision="approved",
                )
            ],
        }

    def release_gate(state: MultiAgentProjectState) -> dict[str, Any]:
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        artifacts = _release_artifacts(registry, active)
        try:
            record = ApprovalRecord.model_validate(state["approval_record"]) if state.get("approval_record") else None
        except ValidationError:
            record = None
        review_env = registry.get(active.get("review", ""))
        try:
            review = ReviewPackage.model_validate(review_env.payload) if review_env else None
        except ValidationError:
            review = None
        active_envelopes = [
            registry[artifact_id]
            for artifact_id in active.values()
            if artifact_id in registry
        ]
        package_hash = _package_hash(artifacts) if artifacts else None
        expiry = _parse_time(record.expires_at) if record else None
        reviewed_hashes_match = bool(
            review
            and artifacts
            and sorted(review.reviewed_artifact_hashes)
            == sorted(
                artifact.payload_hash
                for artifact in artifacts.values()
                if artifact.artifact_type != "review_package"
            )
        )
        checks = {
            "approval_exists": bool(record and record.approved),
            "approval_not_expired": bool(expiry and expiry > clock()),
            "package_hash_matches": bool(record and artifacts and package_hash and record.final_package_hash == package_hash and record.artifact_collection_hash == package_hash and validate_approval_binding(record, artifacts)),
            "no_stale_artifacts": bool(
                artifacts
                and len(active_envelopes) == len(active)
                and all(
                    verify_envelope_hash(env) and env.status == "ready"
                    for env in active_envelopes
                )
            ),
            "review_passed": bool(
                review
                and review_env
                and review.decision == "pass"
                and review_env.status == "ready"
                and reviewed_hashes_match
            ),
            "no_unresolved_blocking_findings": bool(review is not None and not any(issue.severity == "blocking" for issue in review.issues)),
        }
        approval_valid = bool(
            checks["approval_exists"]
            and checks["approval_not_expired"]
            and checks["package_hash_matches"]
        )
        export_attempt = _trace_event(
            "export_attempt",
            approval_present=checks["approval_exists"],
            approval_valid=approval_valid,
            review_present=review is not None,
            blocked=not all(checks.values()),
        )
        if not all(checks.values()):
            previous_errors = list(state.get("errors", []))
            # 没有有效的人工批准（审批被跳过、或只有一条拒绝记录）是"未获批准"，不是"批准后
            # 产物漂移"：此前缺记录时 package_hash_matches 恒 False，被误报成 STALE。
            code = (
                MULTI_APPROVAL_REQUIRED
                if not checks["approval_exists"]
                else (
                    MULTI_APPROVAL_STALE
                    if MULTI_APPROVAL_STALE in previous_errors or not checks["package_hash_matches"]
                    else MULTI_REVIEW_REJECTED
                )
            )
            checked = _trace_event(
                "release_gate_checked",
                **checks,
                approval_present=checks["approval_exists"],
                approval_valid=False,
                blocked=True,
                error_code=code,
            )
            return {
                "phase": "release_gate",
                "release_checks": checks,
                "status": "blocked",
                "errors": [*previous_errors, code],
                "trace_events": [
                    export_attempt,
                    checked,
                    _run_finished_event(
                        state,
                        run_status="blocked",
                        registry=registry,
                        active=active,
                    ),
                ],
            }
        return {
            "phase": "release_gate",
            "release_checks": checks,
            "status": "running",
            "trace_events": [
                export_attempt,
                _trace_event(
                    "release_gate_checked",
                    **checks,
                    approval_present=True,
                    approval_valid=True,
                    blocked=False,
                )
            ],
        }

    def export(state: MultiAgentProjectState) -> dict[str, Any]:
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        approval_valid = bool(state.get("release_checks")) and all(
            state.get("release_checks", {}).values()
        )
        review_present = bool(active.get("review") in registry)
        started = _trace_event(
            "export_started",
            approval_present=state.get("approval_record") is not None,
            approval_valid=approval_valid,
            review_present=review_present,
        )
        drawing = registry[active["drawing"]]
        file_id = drawing.payload.get("staged_file_id")
        if not isinstance(file_id, str):
            return {
                "phase": "export",
                "status": "failed",
                "errors": [*state.get("errors", []), MULTI_EXPORT_FAILED],
                "trace_events": [
                    started,
                    _trace_event(
                        "export_completed",
                        approval_valid=approval_valid,
                        review_present=review_present,
                        exported=False,
                        blocked=True,
                        error_code=MULTI_EXPORT_FAILED,
                    ),
                    _run_finished_event(
                        state,
                        run_status="failed",
                        registry=registry,
                        active=active,
                    ),
                ],
            }
        try:
            path = exporter(file_id=file_id, staging_dir=state["staging_dir"], final_dir=state["final_dir"], approval=ApprovalRecord.model_validate(state["approval_record"]), artifacts=_release_artifacts(registry, active))
        except Exception as exc:  # noqa: BLE001
            approval_stale = isinstance(exc, ValueError) and str(exc).startswith(
                MULTI_APPROVAL_STALE
            )
            error_code = MULTI_APPROVAL_STALE if approval_stale else MULTI_EXPORT_FAILED
            run_status = "blocked" if approval_stale else "failed"
            return {
                "phase": "export",
                "status": run_status,
                "errors": [
                    *state.get("errors", []),
                    (
                        MULTI_APPROVAL_STALE
                        if approval_stale
                        else f"{MULTI_EXPORT_FAILED}: {type(exc).__name__}"
                    ),
                ],
                "trace_events": [
                    started,
                    _trace_event(
                        "export_completed",
                        approval_valid=approval_valid,
                        review_present=review_present,
                        exported=False,
                        blocked=True,
                        error_code=error_code,
                    ),
                    _run_finished_event(
                        state,
                        run_status=run_status,
                        registry=registry,
                        active=active,
                    ),
                ],
            }
        return {
            "phase": "export",
            "exported_file_path": path,
            "status": "succeeded",
            "trace_events": [
                started,
                _trace_event(
                    "export_completed",
                    approval_valid=approval_valid,
                    review_present=review_present,
                    exported=True,
                    blocked=False,
                ),
                _run_finished_event(
                    state,
                    run_status="succeeded",
                    registry=registry,
                    active=active,
                ),
            ],
        }

    def after_intake(state: MultiAgentProjectState) -> str: return "hydraulic" if state.get("status") == "running" else END
    def after_hydraulic(state: MultiAgentProjectState) -> str: return "design" if state.get("status") == "running" else END
    def after_design(state: MultiAgentProjectState) -> str:
        registry, active = _registry(state), state.get("active_artifact_ids") or {}
        design_id = active.get("design")
        if design_id in registry and DesignPackage.model_validate(registry[design_id].payload).recalculation_requests:
            return "recalculate"
        return "drawing" if state.get("status") == "running" else END
    def after_recalculate(state: MultiAgentProjectState) -> str: return "hydraulic" if state.get("status") == "running" else END
    def after_drawing(state: MultiAgentProjectState) -> str: return "review" if state.get("status") == "running" else END
    def after_router(state: MultiAgentProjectState) -> str:
        if state.get("status") in {"blocked", "failed"}: return END
        return state.get("route_target") or END
    def after_prepare(state: MultiAgentProjectState) -> str: return "approval" if state.get("status") == "waiting" else END
    # 只有拿到有效批准才进发布门；拒绝/漂移已在 approval 收口（run_finished 已发出）。
    def after_approval(state: MultiAgentProjectState) -> str: return "release_gate" if state.get("status") == "running" else END
    def after_gate(state: MultiAgentProjectState) -> str: return "export" if state.get("status") == "running" else END

    graph = StateGraph(MultiAgentProjectState)
    for name, node in (
        ("intake", intake),
        ("hydraulic", hydraulic),
        ("design", design),
        ("recalculate", recalculate),
        ("drawing", drawing),
        ("review", review),
        ("router", router),
        ("prepare_approval", prepare_approval),
        ("approval", approval),
        ("release_gate", release_gate),
        ("export", export),
    ):
        graph.add_node(name, cast(Any, _with_trace_context(node)))
    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", after_intake, {"hydraulic": "hydraulic", END: END})
    graph.add_conditional_edges("hydraulic", after_hydraulic, {"design": "design", END: END})
    graph.add_conditional_edges("design", after_design, {"drawing": "drawing", "recalculate": "recalculate", END: END})
    graph.add_conditional_edges("recalculate", after_recalculate, {"hydraulic": "hydraulic", END: END})
    graph.add_conditional_edges("drawing", after_drawing, {"review": "review", END: END})
    graph.add_edge("review", "router")
    graph.add_conditional_edges("router", after_router, {"recalculate": "recalculate", "hydraulic": "hydraulic", "design": "design", "drawing": "drawing", "human": END, "approval": "prepare_approval", END: END})
    graph.add_conditional_edges("prepare_approval", after_prepare, {"approval": "approval", END: END})
    graph.add_conditional_edges("approval", after_approval, {"release_gate": "release_gate", END: END})
    graph.add_conditional_edges("release_gate", after_gate, {"export": "export", END: END})
    graph.add_edge("export", END)
    return graph.compile(checkpointer=checkpointer)


def run_orchestrator(brief: dict[str, Any] | ProjectBrief, *, staging_dir: str | None = None, final_dir: str | None = None, approval: dict[str, Any] | None = None, thread_id: str = "multi-agent-run") -> dict[str, Any]:
    """Run the real graph and optionally resume its one approval interrupt."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    staging, final = staging_dir or tempfile.mkdtemp(prefix="ma-orch-staging-"), final_dir or tempfile.mkdtemp(prefix="ma-orch-final-")
    db_path = Path(tempfile.mkdtemp(prefix="ma-orch-state-")) / "state.sqlite"
    with sqlite3.connect(str(db_path), check_same_thread=False) as conn:
        app = build_orchestrator_graph(checkpointer=SqliteSaver(conn, serde=default_checkpoint_serde()))
        config = {"configurable": {"thread_id": thread_id}}
        project = brief.model_dump(mode="json") if isinstance(brief, ProjectBrief) else brief
        result = app.invoke(default_multi_agent_state(project_brief=project, staging_dir=staging, final_dir=final), config=config)
        return dict(app.invoke(Command(resume=approval), config=config)) if result.get("__interrupt__") and approval is not None else dict(result)
