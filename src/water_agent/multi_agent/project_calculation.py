"""Offline-only T2-7B execution. No route, persistence, revision or approval wiring.

The authority is the service-owned state, not any submitted envelope/hash/status.
confirm_for_offline is a synthetic host-side confirmation adapter, never an API parser.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from pydantic import ValidationError

from water_agent.multi_agent.contracts import ArtifactEnvelope, compute_payload_hash
from water_agent.multi_agent.project_calculation_contracts import (
    INPUT_UNITS,
    OUTPUTS,
    TOOL_NAMES,
    Disclosure,
    NodeExecutionRecord,
    ProgramOutputBinding,
    ProjectCalculationPlan,
    ProjectCalculationResult,
    ProjectError,
    ProjectExecutionContext,
    Unit,
    UserInputBinding,
    number,
)
from water_agent.runtime.governance.contracts import Capability
from water_agent.tools.project_calculation import _ACTIVE_SCOPE, PROJECT_SPECS
from water_agent.tools.registry import ToolRegistry


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _identity(ctx: ProjectExecutionContext) -> dict[str, Any]:
    return {
        f.name: getattr(ctx, f.name)
        for f in fields(ctx)
        if f.name not in ("principal", "cancel_check")
    }


def _record_context(ctx: ProjectExecutionContext) -> dict[str, Any]:
    return {
        **{
            k: v
            for k, v in _identity(ctx).items()
            if k not in ("plan_revision", "checkpoint_namespace")
        },
        "revision": ctx.plan_revision,
    }


def _error(code: str, node: str | None = None) -> ProjectError:
    return ProjectError(code=code, node_id=node, field_path=None, message=code)


def validate_project_plan(raw: object, ctx: ProjectExecutionContext) -> ProjectCalculationPlan:
    data = raw.model_dump() if isinstance(raw, ProjectCalculationPlan) else raw
    if len(_json(data).encode()) > 32768:
        raise ValueError("PROJECT_BUDGET_EXCEEDED")
    plan = ProjectCalculationPlan.model_validate(data)
    if (plan.project_id, plan.plan_id, plan.branch_id, plan.revision) != (
        ctx.project_id,
        ctx.plan_id,
        ctx.branch_id,
        ctx.plan_revision,
    ):
        raise ValueError("PROJECT_SCOPE_MISMATCH")
    return plan


class ProjectState:
    """One confirmed offline run; trusted host code owns this object, never model JSON."""

    _plan: str
    _context: dict[str, Any]
    _principal: Any
    generation: int
    authorization_epoch: int
    expires_at: datetime
    _values: dict[str, str]

    def __init__(self) -> None:
        self.execution_records: dict[str, NodeExecutionRecord] = {}
        self.artifacts: dict[str, ArtifactEnvelope] = {}
        self.active_artifact_ids: dict[str, str] = {}
        self._trusted: dict[str, str] = {}
        self._scopes: dict[str, ProjectNodeScope] = {}
        self._consumed = False
        self.before_execute = lambda *_: None
        self.authority_check = lambda: None
        self.on_started: Callable[[NodeExecutionRecord], None] = lambda *_: None
        self.on_verified: Callable[[NodeExecutionRecord], None] = lambda *_: None
        self.on_artifact: Callable[[ArtifactEnvelope, NodeExecutionRecord], None] = lambda *_: None

    @classmethod
    def confirm_for_offline(cls, raw: object, ctx: ProjectExecutionContext) -> ProjectState:
        plan = validate_project_plan(raw, ctx)
        state = cls()
        state._plan = _json(plan.model_dump())
        state._context = _identity(ctx)
        state._principal = ctx.principal
        state.generation = ctx.generation
        state.authorization_epoch = ctx.authorization_epoch
        state.expires_at = datetime.now(UTC) + timedelta(minutes=15)
        state._values = {
            b.input_id: _json(b.model_dump())
            for n in plan.nodes
            for b in n.inputs.values()
            if isinstance(b, UserInputBinding)
        }
        for node in plan.nodes:
            for binding in node.inputs.values():
                if isinstance(binding, UserInputBinding) and state._values[
                    binding.input_id
                ] != _json(binding.model_dump()):
                    raise ValueError("PROJECT_INVALID_PLAN")
        return state

    def check(self, ctx: ProjectExecutionContext) -> None:
        self.authority_check()
        if ctx.cancel_check():
            raise ValueError("PROJECT_CANCELLED")
        if (
            self._context != _identity(ctx)
            or self._principal != ctx.principal
            or self.generation != ctx.generation
            or self.authorization_epoch != ctx.authorization_epoch
        ):
            raise ValueError("PROJECT_SCOPE_MISMATCH")
        if datetime.now(UTC) >= self.expires_at:
            raise ValueError("PROJECT_STALE_SOURCE")

    def validate_source(
        self, binding: ProgramOutputBinding, ctx: ProjectExecutionContext
    ) -> NodeExecutionRecord:
        self.check(ctx)
        env = self.artifacts.get(binding.artifact_id)
        if env is None or self.active_artifact_ids.get(binding.node_id) != binding.artifact_id:
            raise ValueError("PROJECT_STALE_SOURCE")
        if (
            env.payload_hash != binding.payload_hash
            or env.revision != binding.revision
            or compute_payload_hash(env.payload) != env.payload_hash
            or env.status != "ready"
            or env.schema_version != "t27-v1"
            or env.artifact_type != "calc_package"
        ):
            raise ValueError("PROJECT_UNTRUSTED_SOURCE")
        record = NodeExecutionRecord.model_validate(env.payload["node_record"])
        stored = self.execution_records.get(record.execution_id)
        if (
            record.status != "verified"
            or stored is None
            or stored != record
            or self._trusted.get(record.execution_id) != _json(record.model_dump())
            or record.context_binding != _record_context(ctx)
            or record.node_id != binding.node_id
            or record.case_id != binding.case_id
            or binding.output_key not in record.outputs
            or record.units[binding.output_key] != binding.unit
        ):
            raise ValueError("PROJECT_UNTRUSTED_SOURCE")
        return record


@dataclass(frozen=True)
class GovernedExecution:
    record: NodeExecutionRecord
    result: dict[str, Any]
    verified: bool


class ProjectNodeScope:
    def __init__(
        self,
        state: ProjectState,
        ctx: ProjectExecutionContext,
        record: NodeExecutionRecord,
        index: int,
    ):
        self.state, self.ctx, self.record, self.index = state, ctx, record, index
        self._invoked = False
        self._sealed_record = _json(record.model_dump(exclude={"invocation_id"}))
        self._actual_invocation_id: str | None = None
        self._args_json = _json(record.resolved_inputs)
        self._sources_json = _json({k: v.model_dump() for k, v in record.input_sources.items()})

    def check(self, args: dict[str, Any]) -> None:
        self.state.check(self.ctx)
        if (
            self.state._scopes.get(self.record.execution_id) is not self
            or _json(self.record.model_dump(exclude={"invocation_id"})) != self._sealed_record
            or self.record.invocation_id != self._actual_invocation_id
            or _json(args) != self._args_json
            or _json(self.record.resolved_inputs) != self._args_json
            or compute_payload_hash(args) != self.record.args_hash
            or _json({k: v.model_dump() for k, v in self.record.input_sources.items()})
            != self._sources_json
        ):
            raise ValueError("PROJECT_UNTRUSTED_SOURCE")
        for value in args.values():
            number(value)

    def invoke(self, registry: ToolRegistry, name: str, args: dict[str, Any]) -> GovernedExecution:
        self.check(args)
        if (
            self._invoked
            or name != TOOL_NAMES[self.index]
            or registry.get(name) is not PROJECT_SPECS[self.index]
        ):
            raise ValueError("PROJECT_UNTRUSTED_SOURCE")
        self._invoked = True

        def before(invocation_id: str, policy: dict[str, Any]) -> None:
            self.check(args)
            self.state.before_execute(
                self.record.execution_id, invocation_id, self.record.args_hash, policy
            )
            self._actual_invocation_id = invocation_id
            self.record = self.record.model_copy(update={"invocation_id": invocation_id})
            self.state.execution_records[self.record.execution_id] = self.record

        token = _ACTIVE_SCOPE.set(self)
        try:
            result = registry._invoke(name, args, grant=None, before_execute=before)
        finally:
            _ACTIVE_SCOPE.reset(token)
        try:
            self.check(args)
            if result.get("error") or set(result) != {"outputs", "assumptions"}:
                raise ValueError(str(result.get("code", "PROJECT_EXECUTION_FAILED")))
            outputs = result["outputs"]
            if set(outputs) != {OUTPUTS[self.index]}:
                raise ValueError("PROJECT_EXECUTION_FAILED")
            v = number(outputs[OUTPUTS[self.index]])
            if v < 0 or (self.index < 2 and v <= 0) or not self.record.invocation_id:
                raise ValueError("PROJECT_EXECUTION_FAILED")
            if not isinstance(result["assumptions"], list) or any(
                not isinstance(s, str) for s in result["assumptions"]
            ):
                raise ValueError("PROJECT_EXECUTION_FAILED")
            notes = [
                Disclosure(
                    source_node_id=self.record.node_id,
                    code="kernel_assumption",
                    message=s,
                    severity="warning",
                )
                for s in result["assumptions"]
            ]
            record = self.record.model_dump()
            record.update(
                status="verified",
                outputs=outputs,
                result_hash=compute_payload_hash(result),
                finished_at=_now(),
                assumptions=[*self.record.assumptions, *notes],
            )
            self.record = NodeExecutionRecord.model_validate(record)
            self.state.on_verified(self.record)
            self.state.execution_records[self.record.execution_id] = self.record
            self.state._trusted[self.record.execution_id] = _json(self.record.model_dump())
            return GovernedExecution(self.record, result, True)
        except (ValueError, TypeError, KeyError) as exc:
            code = (
                "PROJECT_CANCELLED"
                if str(exc) == "PROJECT_CANCELLED"
                else "PROJECT_EXECUTION_FAILED"
            )
            if str(exc) == "PROJECT_PERSISTENCE_FAILED":
                code = str(exc)
            data = self.record.model_dump()
            data.update(
                status="cancelled" if code == "PROJECT_CANCELLED" else "failed",
                outputs={},
                finished_at=_now(),
                error=_error(code, self.record.node_id).model_dump(),
            )
            self.record = NodeExecutionRecord.model_validate(data)
            self.state.execution_records[self.record.execution_id] = self.record
            return GovernedExecution(self.record, result, False)


def resolve_node(
    plan: ProjectCalculationPlan, node_id: str, state: ProjectState, ctx: ProjectExecutionContext
) -> ProjectNodeScope:
    state.check(ctx)
    if _json(plan.model_dump()) != state._plan:
        raise ValueError("PROJECT_UNTRUSTED_SOURCE")
    if len(state.execution_records) >= 3 or any(
        r.node_id == node_id for r in state.execution_records.values()
    ):
        raise ValueError("PROJECT_BUDGET_EXCEEDED")
    index = ["v", "hf", "H"].index(node_id)
    node = plan.nodes[index]
    sources: dict[str, Any] = {}
    inputs: dict[str, float] = {}
    notes: list[Disclosure] = []
    for key, binding in node.inputs.items():
        if isinstance(binding, UserInputBinding):
            if state._values.get(binding.input_id) != _json(binding.model_dump()):
                raise ValueError("PROJECT_UNTRUSTED_SOURCE")
            sources[key] = binding.model_dump()
            inputs[key] = binding.value
        else:
            aid = state.active_artifact_ids.get(binding.node_id)
            if aid is None:
                raise ValueError("PROJECT_UNTRUSTED_SOURCE")
            env = state.artifacts[aid]
            resolved = ProgramOutputBinding(
                kind="program",
                node_id=binding.node_id,
                case_id=binding.case_id,
                artifact_id=aid,
                revision=env.revision,
                output_key=binding.output_key,
                payload_hash=env.payload_hash,
                unit=binding.unit,
            )
            prev = state.validate_source(resolved, ctx)
            sources[key] = resolved.model_dump()
            inputs[key] = prev.outputs[binding.output_key]
            notes.extend(prev.assumptions)
    binding_ctx = _record_context(ctx)
    version = hashlib.sha256(
        (Path(__file__).parents[1] / "tools/hydraulics.py").read_bytes()
    ).hexdigest()
    record = NodeExecutionRecord(
        execution_id=str(uuid4()),
        invocation_id=None,
        context_binding=binding_ctx,
        node_id=node.node_id,
        case_id=node.case_id,
        formula=node.formula,
        tool_name=TOOL_NAMES[index],
        tool_identity_version="t27-v1",
        plan_hash=compute_payload_hash(plan.model_dump()),
        args_hash=compute_payload_hash(inputs),
        result_hash=None,
        resolved_inputs=inputs,
        input_sources=sources,
        outputs={},
        units={
            k: cast(Unit, v)
            for k, v in {**INPUT_UNITS[index], OUTPUTS[index]: "m/s" if index == 0 else "m"}.items()
        },
        algorithm_version=version,
        contract_version="t27-v1",
        status="started",
        assumptions=notes,
        warnings=[],
        error=None,
        started_at=_now(),
        finished_at=None,
    )
    scope = ProjectNodeScope(state, ctx, record, index)
    state._scopes[record.execution_id] = scope
    state.execution_records[record.execution_id] = record
    return scope


def execute_project_plan(
    raw: object,
    ctx: ProjectExecutionContext,
    state: ProjectState,
    registry: ToolRegistry | None = None,
) -> ProjectCalculationResult:
    records: list[NodeExecutionRecord] = []
    errors: list[ProjectError] = []
    status = "blocked"
    manifest = None
    plan: ProjectCalculationPlan | None = None
    try:
        plan = validate_project_plan(raw, ctx)
        state.check(ctx)
        if state._consumed or _json(plan.model_dump()) != state._plan:
            raise ValueError("PROJECT_UNTRUSTED_SOURCE")
        if Capability.CALCULATE not in ctx.principal.capabilities:
            raise ValueError("PROJECT_SCOPE_MISMATCH")
        state._consumed = True
        if registry is None:
            registry = ToolRegistry(principal=ctx.principal)
            for spec in PROJECT_SPECS:
                registry.register(spec)
        if registry._resolve_principal() != ctx.principal:
            raise ValueError("PROJECT_SCOPE_MISMATCH")
        for node in plan.nodes:
            scope = resolve_node(plan, node.node_id, state, ctx)
            state.on_started(scope.record)
            execution = registry.invoke_with_evidence(
                scope.record.tool_name, scope.record.resolved_inputs, scope=scope
            )
            records.append(execution.record)
            if not execution.verified:
                err = execution.record.error
                code = err.code if err else "PROJECT_EXECUTION_FAILED"
                raise ValueError(code)
            rec = execution.record
            payload = {"node_record": rec.model_dump()}
            aid = "calc-" + uuid4().hex
            env = ArtifactEnvelope(
                artifact_id=aid,
                artifact_type="calc_package",
                schema_version="t27-v1",
                producer="hydraulic",
                revision=plan.revision,
                parent_artifact_ids=[state.active_artifact_ids[n] for n in node.depends_on],
                payload_hash=compute_payload_hash(payload),
                payload=payload,
                citation_ids=[],
                assumptions=[n.message for n in rec.assumptions],
                warnings=[],
                status="ready",
            )
            state.on_artifact(env, rec)
            state.artifacts[aid] = env
            state.active_artifact_ids[node.node_id] = aid
        status = "ready"
        manifest = compute_payload_hash(
            {
                "plan_hash": compute_payload_hash(plan.model_dump()),
                "context": state._context,
                "artifact_ids": list(state.active_artifact_ids.values()),
                "artifact_hashes": [e.payload_hash for e in state.artifacts.values()],
                "revision": plan.revision,
            }
        )
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, ValidationError) as exc:
        text = str(exc)
        if isinstance(exc, ValidationError) and any(e["type"] == "missing" for e in exc.errors()):
            text = "PROJECT_MISSING_INPUT"
        code = next(
            (
                s
                for s in (
                    "PROJECT_CANCELLED",
                    "PROJECT_MISSING_INPUT",
                    "PROJECT_PERSISTENCE_FAILED",
                    "PROJECT_INVALID_VALUE",
                    "PROJECT_SCOPE_MISMATCH",
                    "PROJECT_STALE_SOURCE",
                    "PROJECT_UNTRUSTED_SOURCE",
                    "PROJECT_UNIT_MISMATCH",
                    "PROJECT_BUDGET_EXCEEDED",
                    "PROJECT_EXECUTION_FAILED",
                )
                if s in text
            ),
            "PROJECT_INVALID_PLAN",
        )
        errors.append(_error(code))
        status = (
            "cancelled"
            if code == "PROJECT_CANCELLED"
            else "missing_inputs"
            if code == "PROJECT_MISSING_INPUT"
            else "failed"
            if code == "PROJECT_EXECUTION_FAILED"
            else "blocked"
        )
    notes = list(
        {
            (n.source_node_id, n.code, n.message): n for rec in records for n in rec.assumptions
        }.values()
    )
    return ProjectCalculationResult(
        plan_id=ctx.plan_id,
        project_id=ctx.project_id,
        branch_id=ctx.branch_id,
        revision=ctx.plan_revision,
        plan_hash=compute_payload_hash(plan.model_dump()) if plan else "",
        input_set_id=plan.input_set_id if plan else "",
        node_records=records,
        status=status,  # type: ignore[arg-type]
        assumptions=notes,
        warnings=[],
        errors=errors,
        manifest_hash=manifest,
    )
