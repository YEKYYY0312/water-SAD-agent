"""P4.5 Step 33: bounded four-agent graph, HITL approval, and release gate."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

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
    route_review,
)
from water_agent.session import default_checkpoint_serde

DEFAULT_APPROVAL_TTL_SECONDS = 24 * 60 * 60
_SCHEMA_VERSION = "p4.5-2026-09-11"


class MultiAgentProjectState(TypedDict, total=False):
    """JSON-shaped state persisted by SqliteSaver across approval resumes."""

    project_brief: dict[str, Any]
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


def default_multi_agent_state(**overrides: Any) -> MultiAgentProjectState:
    state: MultiAgentProjectState = {
        "project_brief": {}, "retrieved_source_ids": [], "artifacts": {}, "active_artifact_ids": {},
        "revision_counts": {"hydraulic": 0, "design": 0, "drawing": 0, "review": 0},
        "phase": "intake", "route_target": None, "approval_record": None,
        "approval_context_hash": None, "final_project_package": None, "release_checks": {},
        "staging_dir": "", "final_dir": "", "output_file_id": "project-drawing",
        "exported_file_path": None, "step_count": 0, "status": "running", "errors": [],
        "missing_inputs": [],
    }
    state.update(cast(MultiAgentProjectState, overrides))
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


def _registry(state: MultiAgentProjectState) -> dict[str, ArtifactEnvelope]:
    result: dict[str, ArtifactEnvelope] = {}
    for artifact_id, raw in (state.get("artifacts") or {}).items():
        try:
            result[artifact_id] = ArtifactEnvelope.model_validate(raw)
        except ValidationError:
            continue
    return result


def _dump(registry: dict[str, ArtifactEnvelope]) -> dict[str, dict[str, Any]]:
    return {artifact_id: env.model_dump(mode="json") for artifact_id, env in registry.items()}


def _put(
    registry: dict[str, ArtifactEnvelope], active: dict[str, str], env: ArtifactEnvelope,
    *, role: str, invalidates: AbstractSet[str] = frozenset(),
) -> tuple[dict[str, ArtifactEnvelope], dict[str, str]]:
    registry, active = dict(registry), dict(active)
    for active_role, artifact_id in list(active.items()):
        old = registry.get(artifact_id)
        if old is not None and old.artifact_type in invalidates:
            registry.pop(artifact_id, None)
            active.pop(active_role, None)
    previous = active.get(role)
    if previous:
        registry.pop(previous, None)
    registry[env.artifact_id] = env
    active[role] = env.artifact_id
    return registry, active


def _release_artifacts(registry: dict[str, ArtifactEnvelope], active: dict[str, str]) -> dict[str, ArtifactEnvelope] | None:
    roles = ("calc", "design", "document", "drawing", "review")
    if any(role not in active or active[role] not in registry for role in roles):
        return None
    return {active[role]: registry[active[role]] for role in roles}


def _package_hash(artifacts: dict[str, ArtifactEnvelope]) -> str:
    ids = sorted(artifacts)
    return compute_artifact_collection_hash(
        [compute_payload_hash(artifacts[artifact_id].payload) for artifact_id in ids], artifact_ids=ids
    )


def _document_from_plan(plan_env: ArtifactEnvelope, calc_env: ArtifactEnvelope, design_env: ArtifactEnvelope) -> ArtifactEnvelope:
    plan = DrawingPlan.model_validate(plan_env.payload)
    docgen = plan.output_adaptations.get("docgen")
    rendered = docgen.get("rendered") if isinstance(docgen, dict) else None
    if isinstance(rendered, dict):
        payload = {
            "doc_type": rendered.get("doc_type", "design_note"),
            "citation_ids": list(rendered.get("citation_ids") or []),
            "calc_values": dict(rendered.get("calc_values") or {}),
            "sections": list(rendered.get("sections") or []),
        }
        status: Literal["ready", "missing_inputs", "blocked", "failed"] = "ready"
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
        drawing_artifact_id=by_type["drawing_package"].artifact_id,
        review_artifact_id=by_type["review_package"].artifact_id,
        approval_record=approval, release_manifest_hash=_package_hash(artifacts),
    )


def is_approval_valid(approval: ApprovalRecord, current_hashes: list[str]) -> bool:
    return sorted(approval.artifact_hashes) == sorted(current_hashes)


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
            return {"phase": phase, "status": "blocked", "errors": [*state.get("errors", []), MULTI_RETRY_EXHAUSTED]}
        return None

    def intake(state: MultiAgentProjectState) -> dict[str, Any]:
        try:
            ProjectBrief.model_validate(state.get("project_brief") or {})
        except ValidationError as exc:
            return {"phase": "intake", "status": "missing_inputs", "errors": [*state.get("errors", []), f"invalid project brief: {exc}"]}
        return {"phase": "intake", "status": "running"}

    def hydraulic(state: MultiAgentProjectState) -> dict[str, Any]:
        if result := consume(state, "hydraulic"):
            return result
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        env = hydraulic_runner(ProjectBrief.model_validate(state["project_brief"]), supersedes_artifact_id=active.get("calc"))
        registry, active = _put(registry, active, env, role="calc", invalidates={"calc_package", "design_package", "drawing_plan", "drawing_package", "document", "review_package"})
        missing = CalcPackage.model_validate(env.payload).missing_inputs if env.status == "missing_inputs" else []
        return {"phase": "hydraulic", "artifacts": _dump(registry), "active_artifact_ids": active, "step_count": state.get("step_count", 0) + 1, "status": "running" if env.status == "ready" else env.status, "missing_inputs": [*state.get("missing_inputs", []), *missing]}

    def design(state: MultiAgentProjectState) -> dict[str, Any]:
        if result := consume(state, "design"):
            return result
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        calc = registry[active["calc"]]
        previous = DesignPackage.model_validate(registry[active["design"]].payload) if active.get("design") in registry else None
        env = design_runner(ProjectBrief.model_validate(state["project_brief"]), calc, retrieved_source_ids=list(state.get("retrieved_source_ids") or []), prev_design=previous)
        registry, active = _put(registry, active, env, role="design", invalidates={"design_package", "drawing_plan", "drawing_package", "document", "review_package"})
        package = DesignPackage.model_validate(env.payload)
        return {"phase": "design", "artifacts": _dump(registry), "active_artifact_ids": active, "step_count": state.get("step_count", 0) + 1, "status": "running" if env.status == "ready" or package.recalculation_requests else env.status, "missing_inputs": [*state.get("missing_inputs", []), *package.unresolved_items]}

    def recalculate(state: MultiAgentProjectState) -> dict[str, Any]:
        counts = dict(state.get("revision_counts") or {})
        if counts.get("hydraulic", 0) >= MAX_HYDRAULIC_REVISIONS:
            return {"phase": "recalculate", "status": "blocked", "errors": [*state.get("errors", []), MULTI_RETRY_EXHAUSTED]}
        counts["hydraulic"] = counts.get("hydraulic", 0) + 1
        return {"phase": "recalculate", "revision_counts": counts, "route_target": "hydraulic"}

    def drawing(state: MultiAgentProjectState) -> dict[str, Any]:
        if result := consume(state, "drawing"):
            return result
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        calc, design_env = registry[active["calc"]], registry[active["design"]]
        plan = drawing_runner(design_env, calc, drawing_id=state.get("output_file_id") or "project-drawing")
        registry, active = _put(registry, active, plan, role="drawing_plan", invalidates={"drawing_plan", "drawing_package", "document", "review_package"})
        package = drawing_stager(plan, staging_dir=state["staging_dir"], file_id=state.get("output_file_id") or "project-drawing")
        registry, active = _put(registry, active, package, role="drawing", invalidates={"drawing_package", "document", "review_package"})
        document = _document_from_plan(plan, calc, design_env)
        registry, active = _put(registry, active, document, role="document", invalidates={"document", "review_package"})
        statuses = {plan.status, package.status, document.status}
        status = "running" if statuses == {"ready"} else ("failed" if "failed" in statuses else ("blocked" if "blocked" in statuses else "missing_inputs"))
        return {"phase": "drawing", "artifacts": _dump(registry), "active_artifact_ids": active, "step_count": state.get("step_count", 0) + 1, "status": status}

    def review(state: MultiAgentProjectState) -> dict[str, Any]:
        if result := consume(state, "review"):
            return result
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        counts = dict(state.get("revision_counts") or {})
        counts["review"] = counts.get("review", 0) + 1
        env = review_runner(registry, review_id="review-project", document_artifact_id=active.get("document"), revision=counts["review"])
        registry, active = _put(registry, active, env, role="review", invalidates={"review_package"})
        return {"phase": "review", "artifacts": _dump(registry), "active_artifact_ids": active, "revision_counts": counts, "step_count": state.get("step_count", 0) + 1, "status": "running"}

    def router(state: MultiAgentProjectState) -> dict[str, Any]:
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        env = registry.get(active.get("review", ""))
        if env is None:
            return {"phase": "router", "status": "blocked", "errors": [*state.get("errors", []), MULTI_REVIEW_REJECTED]}
        package = ReviewPackage.model_validate(env.payload)
        if package.decision == "blocked":
            return {"phase": "router", "status": "blocked", "errors": [*state.get("errors", []), MULTI_REVIEW_REJECTED]}
        decision = route_review(package, revision_counts=dict(state.get("revision_counts") or {}))
        if decision.get("blocked"):
            return {"phase": "router", "status": "blocked", "route_target": None, "errors": [*state.get("errors", []), decision["error_code"]]}
        owner = decision.get("next_owner")
        if owner is None:
            return {"phase": "router", "route_target": "approval" if package.decision == "pass" and not package.not_checked else "human", "status": "running" if package.decision == "pass" and not package.not_checked else "waiting"}
        if owner == "human":
            return {"phase": "router", "route_target": "human", "status": "waiting"}
        counts = dict(state.get("revision_counts") or {})
        counts[owner] = counts.get(owner, 0) + 1
        invalidates = {"hydraulic": {"design_package", "drawing_plan", "drawing_package", "document", "review_package"}, "design": {"drawing_plan", "drawing_package", "document", "review_package"}, "drawing": {"drawing_plan", "drawing_package", "document", "review_package"}}[owner]
        for role, artifact_id in list(active.items()):
            artifact = registry.get(artifact_id)
            if artifact is not None and artifact.artifact_type in invalidates:
                registry.pop(artifact_id, None)
                active.pop(role, None)
        return {"phase": "router", "route_target": owner, "revision_counts": counts, "artifacts": _dump(registry), "active_artifact_ids": active, "status": "running"}

    def prepare_approval(state: MultiAgentProjectState) -> dict[str, Any]:
        artifacts = _release_artifacts(_registry(state), dict(state.get("active_artifact_ids") or {}))
        if artifacts is None:
            return {"phase": "prepare_approval", "status": "blocked", "errors": [*state.get("errors", []), MULTI_APPROVAL_REQUIRED]}
        return {"phase": "prepare_approval", "approval_context_hash": _package_hash(artifacts), "status": "waiting"}

    def approval(state: MultiAgentProjectState) -> dict[str, Any]:
        artifacts = _release_artifacts(_registry(state), dict(state.get("active_artifact_ids") or {}))
        expected_hash = _package_hash(artifacts) if artifacts else None
        if artifacts is None or expected_hash != state.get("approval_context_hash"):
            return {"phase": "approval", "status": "blocked", "errors": [*state.get("errors", []), MULTI_APPROVAL_STALE]}
        decision = interrupt({"approval_required": True, "final_package_hash": expected_hash, "artifact_ids": sorted(artifacts), "expires_in_seconds": approval_ttl_seconds})
        approved, reason, supplied_expiry = _strict_approval(decision)
        # The restart boundary must recompute the persisted context before accepting approval.
        artifacts = _release_artifacts(_registry(state), dict(state.get("active_artifact_ids") or {}))
        resumed_hash = _package_hash(artifacts) if artifacts else None
        if artifacts is None or resumed_hash != expected_hash or resumed_hash != state.get("approval_context_hash"):
            return {"phase": "approval", "status": "blocked", "errors": [*state.get("errors", []), MULTI_APPROVAL_STALE]}
        if not approved:
            return {"phase": "approval", "status": "blocked", "errors": [*state.get("errors", []), MULTI_APPROVAL_REQUIRED]}
        timestamp = clock()
        expiry = supplied_expiry or _iso(timestamp + timedelta(seconds=approval_ttl_seconds))
        ids = sorted(artifacts)
        hashes = [compute_payload_hash(artifacts[artifact_id].payload) for artifact_id in ids]
        record = ApprovalRecord(approved=True, reason=reason or "approved", artifact_ids=ids, artifact_hashes=hashes, artifact_collection_hash=resumed_hash, final_package_hash=resumed_hash, approved_at=_iso(timestamp), expires_at=expiry)
        final = _final_package(artifacts, record)
        return {"phase": "approval", "approval_record": record.model_dump(mode="json"), "final_project_package": final.model_dump(mode="json"), "status": "running"}

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
            "review_passed": bool(review and review_env and review.decision == "pass" and review_env.status == "ready"),
            "no_unresolved_blocking_findings": bool(review is not None and not any(issue.severity == "blocking" for issue in review.issues)),
        }
        if not all(checks.values()):
            code = MULTI_APPROVAL_REQUIRED if not checks["approval_exists"] else (MULTI_APPROVAL_STALE if not checks["package_hash_matches"] else MULTI_REVIEW_REJECTED)
            return {"phase": "release_gate", "release_checks": checks, "status": "blocked", "errors": [*state.get("errors", []), code]}
        return {"phase": "release_gate", "release_checks": checks, "status": "running"}

    def export(state: MultiAgentProjectState) -> dict[str, Any]:
        registry, active = _registry(state), dict(state.get("active_artifact_ids") or {})
        drawing = registry[active["drawing"]]
        file_id = drawing.payload.get("staged_file_id")
        if not isinstance(file_id, str):
            return {"phase": "export", "status": "failed", "errors": [*state.get("errors", []), MULTI_EXPORT_FAILED]}
        try:
            path = exporter(file_id=file_id, staging_dir=state["staging_dir"], final_dir=state["final_dir"], approval=ApprovalRecord.model_validate(state["approval_record"]), artifacts=_release_artifacts(registry, active))
        except Exception as exc:  # noqa: BLE001
            return {"phase": "export", "status": "failed", "errors": [*state.get("errors", []), f"{MULTI_EXPORT_FAILED}: {type(exc).__name__}"]}
        return {"phase": "export", "exported_file_path": path, "status": "succeeded"}

    def after_intake(state: MultiAgentProjectState) -> str: return "hydraulic" if state.get("status") == "running" else END
    def after_hydraulic(state: MultiAgentProjectState) -> str: return "design" if state.get("status") == "running" else END
    def after_design(state: MultiAgentProjectState) -> str:
        registry, active = _registry(state), state.get("active_artifact_ids") or {}
        design_id = active.get("design")
        if design_id in registry and DesignPackage.model_validate(registry[design_id].payload).recalculation_requests:
            return "recalculate"
        return "drawing" if state.get("status") == "running" else END
    def after_recalculate(state: MultiAgentProjectState) -> str: return "hydraulic" if state.get("status") != "blocked" else END
    def after_drawing(state: MultiAgentProjectState) -> str: return "review" if state.get("status") == "running" else END
    def after_router(state: MultiAgentProjectState) -> str:
        if state.get("status") in {"blocked", "failed"}: return END
        return state.get("route_target") or END
    def after_prepare(state: MultiAgentProjectState) -> str: return "approval" if state.get("status") == "waiting" else END
    def after_gate(state: MultiAgentProjectState) -> str: return "export" if state.get("status") == "running" else END

    graph = StateGraph(MultiAgentProjectState)
    for name, node in (("intake", intake), ("hydraulic", hydraulic), ("design", design), ("recalculate", recalculate), ("drawing", drawing), ("review", review), ("router", router), ("prepare_approval", prepare_approval), ("approval", approval), ("release_gate", release_gate), ("export", export)):
        graph.add_node(name, node)
    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", after_intake, {"hydraulic": "hydraulic", END: END})
    graph.add_conditional_edges("hydraulic", after_hydraulic, {"design": "design", END: END})
    graph.add_conditional_edges("design", after_design, {"drawing": "drawing", "recalculate": "recalculate", END: END})
    graph.add_conditional_edges("recalculate", after_recalculate, {"hydraulic": "hydraulic", END: END})
    graph.add_conditional_edges("drawing", after_drawing, {"review": "review", END: END})
    graph.add_edge("review", "router")
    graph.add_conditional_edges("router", after_router, {"hydraulic": "hydraulic", "design": "design", "drawing": "drawing", "human": END, "approval": "prepare_approval", END: END})
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
