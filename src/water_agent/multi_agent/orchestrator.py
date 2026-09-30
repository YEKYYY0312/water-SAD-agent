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

from water_agent.multi_agent import export_adapter
from water_agent.multi_agent.agents.design import run_design_agent
from water_agent.multi_agent.agents.drawing import run_drawing_agent
from water_agent.multi_agent.agents.hydraulic import run_hydraulic_agent
from water_agent.multi_agent.agents.review import run_review_agent
from water_agent.multi_agent.contracts import (
    MULTI_APPROVAL_REQUIRED,
    MULTI_APPROVAL_STALE,
    MULTI_EXPORT_FAILED,
    MULTI_MISSING_INPUTS,
    MULTI_RETRY_EXHAUSTED,
    MULTI_REVIEW_REJECTED,
    ApprovalRecord,
    ArtifactEnvelope,
    CalcPackage,
    DesignPackage,
    DrawingPlan,
    FinalProjectPackage,
    ProjectBrief,
    ReviewPackage,
    compute_artifact_collection_hash,
    compute_payload_hash,
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


def _strict_approval(value: Any) -> tuple[bool, str, str | None]:
    if not isinstance(value, dict) or not isinstance(value.get("approved"), bool):
        raise TypeError("multi-agent approval resume requires {'approved': True/False}")
    reason = value.get("reason", "")
    expires_at = value.get("expires_at")
    if not isinstance(reason, str) or (expires_at is not None and not isinstance(expires_at, str)):
        raise TypeError("multi-agent approval reason/expires_at have invalid types")
    if expires_at is not None and _parse_time(expires_at) is None:
        raise TypeError("multi-agent approval expires_at must be ISO-8601 text")
    return value["approved"], reason, expires_at


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
        edge_candidates = [
            edge
            for edge in brief_data["topology"]
            if edge["start_node_id"] == segment.start_node_id
            and edge["end_node_id"] == segment.end_node_id
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
    roles = ("calc", "design", "drawing_plan", "document", "drawing", "review")
    if any(role not in active or active[role] not in registry for role in roles):
        return None
    return {active[role]: registry[active[role]] for role in roles}


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
) -> Any:
    """Compile the only P4.5 orchestration graph; interrupt needs persistence."""
    if checkpointer is None:
        raise ValueError("multi-agent graph requires a persistent checkpointer for interrupt/resume")
    if approval_ttl_seconds <= 0:
        raise ValueError("approval_ttl_seconds must be positive")
    clock = now or (lambda: datetime.now(UTC))

    def consume(state: MultiAgentProjectState, phase: str) -> dict[str, Any] | None:
        if state.get("step_count", 0) >= MAX_MULTI_AGENT_STEPS:
            return {
                "phase": phase,
                "status": "blocked",
                "errors": [*state.get("errors", []), MULTI_RETRY_EXHAUSTED],
                "trace_events": [_run_finished_event(state, run_status="blocked")],
            }
        return None

    def intake(state: MultiAgentProjectState) -> dict[str, Any]:
        events = [_trace_event("run_started", phase="intake")]
        try:
            ProjectBrief.model_validate(state.get("project_brief") or {})
        except ValidationError as exc:
            events.append(_run_finished_event(state, run_status="missing_inputs"))
            return {
                "phase": "intake",
                "status": "missing_inputs",
                "errors": [*state.get("errors", []), f"invalid project brief: {exc}"],
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
        missing = CalcPackage.model_validate(env.payload).missing_inputs if env.status == "missing_inputs" else []
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
            "trace_events": events,
        }

    def design(state: MultiAgentProjectState) -> dict[str, Any]:
        if result := consume(state, "design"):
            return result
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        registry_before, active_before = dict(registry), dict(active)
        calc = registry[active["calc"]]
        previous = DesignPackage.model_validate(registry[active["design"]].payload) if active.get("design") in registry else None
        env = design_runner(ProjectBrief.model_validate(state["project_brief"]), calc, retrieved_source_ids=list(state.get("retrieved_source_ids") or []), prev_design=previous)
        registry, active = _put(registry, active, env, role="design", invalidates={"design_package", "drawing_plan", "drawing_package", "document", "review_package"})
        package = DesignPackage.model_validate(env.payload)
        status = "running" if env.status == "ready" or package.recalculation_requests else env.status
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
            "missing_inputs": [*state.get("missing_inputs", []), *package.unresolved_items],
            "trace_events": events,
        }

    def recalculate(state: MultiAgentProjectState) -> dict[str, Any]:
        counts = dict(state.get("revision_counts") or {})
        if counts.get("hydraulic", 0) >= MAX_HYDRAULIC_REVISIONS:
            return {
                "phase": "recalculate",
                "status": "blocked",
                "errors": [*state.get("errors", []), MULTI_RETRY_EXHAUSTED],
                "trace_events": [_run_finished_event(state, run_status="blocked")],
            }
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        updated_brief, missing = _brief_with_recalculation_updates(
            state,
            registry,
            active,
        )
        if updated_brief is None:
            return {
                "phase": "recalculate",
                "status": "waiting",
                "route_target": "human",
                "errors": [*state.get("errors", []), MULTI_MISSING_INPUTS],
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
                "errors": [*state.get("errors", []), MULTI_REVIEW_REJECTED],
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
            return {
                "phase": "router",
                "status": "blocked",
                "errors": [*state.get("errors", []), MULTI_REVIEW_REJECTED],
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
                "errors": [*state.get("errors", []), decision["error_code"]],
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
                "trace_events": events,
            }
        if owner == "human":
            return {
                "phase": "router",
                "route_target": "human",
                "status": "waiting",
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
            return {
                "phase": "prepare_approval",
                "status": "blocked",
                "errors": [*state.get("errors", []), MULTI_APPROVAL_REQUIRED],
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

    def approval(state: MultiAgentProjectState) -> dict[str, Any]:
        artifacts = _release_artifacts(_registry(state), dict(state.get("active_artifact_ids") or {}))
        expected_hash = _package_hash(artifacts) if artifacts else None
        if artifacts is None or expected_hash != state.get("approval_context_hash"):
            return {
                "phase": "approval",
                "status": "blocked",
                "errors": [*state.get("errors", []), MULTI_APPROVAL_STALE],
                "trace_events": [
                    _trace_event(
                        "approval_resumed",
                        approval_present=True,
                        approval_valid=False,
                        package_hash_matches=False,
                        blocked=True,
                        error_code=MULTI_APPROVAL_STALE,
                    )
                ],
            }
        # 披露项随审批请求一起给到批准人（带条件进入人审）：此前请求里只有一个哈希，
        # 人工批准实际是盲批（review 2026-09-30）。
        decision = interrupt({"approval_required": True, "final_package_hash": expected_hash, "artifact_ids": sorted(artifacts), "expires_in_seconds": approval_ttl_seconds, "disclosed_limitations": review_disclosures(state)})
        approved, reason, supplied_expiry = _strict_approval(decision)
        # The restart boundary must recompute the persisted context before accepting approval.
        artifacts = _release_artifacts(_registry(state), dict(state.get("active_artifact_ids") or {}))
        resumed_hash = _package_hash(artifacts) if artifacts else None
        if artifacts is None or resumed_hash != expected_hash or resumed_hash != state.get("approval_context_hash"):
            return {
                "phase": "approval",
                "status": "blocked",
                "errors": [*state.get("errors", []), MULTI_APPROVAL_STALE],
                "trace_events": [
                    _trace_event(
                        "approval_resumed",
                        approval_present=approved,
                        approval_valid=False,
                        package_hash_matches=False,
                        blocked=True,
                        error_code=MULTI_APPROVAL_STALE,
                    )
                ],
            }
        if not approved:
            return {
                "phase": "approval",
                "status": "blocked",
                "errors": [*state.get("errors", []), MULTI_APPROVAL_REQUIRED],
                "trace_events": [
                    _trace_event(
                        "approval_resumed",
                        approval_present=False,
                        approval_valid=False,
                        package_hash_matches=True,
                        blocked=True,
                        error_code=MULTI_APPROVAL_REQUIRED,
                    )
                ],
            }
        timestamp = clock()
        expiry = supplied_expiry or _iso(timestamp + timedelta(seconds=approval_ttl_seconds))
        ids = sorted(artifacts)
        hashes = [compute_payload_hash(artifacts[artifact_id].payload) for artifact_id in ids]
        record = ApprovalRecord(
            approved=True,
            reason=reason or "approved",
            artifact_ids=ids,
            artifact_hashes=hashes,
            artifact_revisions=[artifacts[artifact_id].revision for artifact_id in ids],
            artifact_parent_artifact_ids=[
                list(artifacts[artifact_id].parent_artifact_ids) for artifact_id in ids
            ],
            artifact_collection_hash=resumed_hash,
            final_package_hash=resumed_hash,
            approved_at=_iso(timestamp),
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
            code = (
                MULTI_APPROVAL_STALE
                if MULTI_APPROVAL_STALE in previous_errors or not checks["package_hash_matches"]
                else (
                    MULTI_APPROVAL_REQUIRED
                    if not checks["approval_exists"]
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
    graph.add_edge("approval", "release_gate")
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
