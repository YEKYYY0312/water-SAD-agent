"""Internal calculation_only lifecycle. Single host, trusted authority and storage.

No production authentication, transport, document renderer or publication is wired here.
Project writes use a shared OS lock; execution leases never block cancellation writes.
Only JSON-shaped values enter the existing strict checkpoint serializer.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver

from water_agent.multi_agent.contracts import (
    ApprovalRecord,
    ArtifactEnvelope,
    compute_artifact_collection_hash,
    compute_payload_hash,
)
from water_agent.multi_agent.project_calculation import (
    ProjectState,
    _identity,
    _json,
    execute_project_plan,
)
from water_agent.multi_agent.project_calculation_contracts import (
    NodeExecutionRecord,
    ProjectCalculationPlan,
    ProjectCalculationResult,
    ProjectExecutionContext,
    StrictModel,
)
from water_agent.runtime.governance.contracts import Capability, Principal
from water_agent.session import default_checkpoint_serde


@dataclass(frozen=True)
class Authorization:
    principal: Principal
    epoch: int


class Authority(Protocol):
    def query(self, ctx: ProjectExecutionContext) -> Authorization: ...


class DocumentCandidate(StrictModel):
    contract_version: Literal["t27-v1"] = "t27-v1"
    state: Literal["ready_for_approval"] = "ready_for_approval"
    renderer_version: Literal["c-synthetic-v1"] = "c-synthetic-v1"
    content: str
    content_sha256: str
    byte_size: int
    execution_manifest_hash: str
    limitations: list[str]


ROLES = ("v", "hf", "H", "calculation", "document", "review")
LIMITATIONS = [
    "仅验证数学和来源一致性；未检查工程适用性、选泵或条文合规。",
    "工具提示，未进行规范校核。发布审批见随附记录。",
]


def algorithm_version() -> str:
    return hashlib.sha256(
        (Path(__file__).parents[1] / "tools/hydraulics.py").read_bytes()
    ).hexdigest()


class CalculationService:
    """Host-only service; callers must supply an authoritative repository, never request JSON."""

    def __init__(self, private_root: Path, authority: Authority, *, document_backend: Any = None):
        self.root = private_root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.authority = authority
        self.document_backend = document_backend
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.fault: Callable[[str], None] = lambda _: None

    def _key(self, ctx: ProjectExecutionContext) -> str:
        return compute_payload_hash(
            {k: getattr(ctx, k) for k in ("owner_user_id", "project_id", "thread_id")}
        )

    @contextmanager
    def _locked(self, ctx: ProjectExecutionContext) -> Iterator[None]:
        with (self.root / (self._key(ctx) + ".lock")).open("a+b") as f:
            Path(f.name).chmod(0o600)
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    @contextmanager
    def _saver(self, ctx: ProjectExecutionContext):
        path = self.root / (self._key(ctx) + ".sqlite")
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        with sqlite3.connect(path) as conn:
            yield SqliteSaver(conn, serde=default_checkpoint_serde())

    def _config(self, ctx: ProjectExecutionContext) -> dict[str, Any]:
        return {"configurable": {"thread_id": self._key(ctx), "checkpoint_ns": "t27-v1"}}

    def _auth(self, ctx: ProjectExecutionContext) -> Authorization:
        try:
            auth = self.authority.query(ctx)
            if (
                type(auth.epoch) is not int
                or auth.epoch < 1
                or Capability.CALCULATE not in auth.principal.capabilities
            ):
                raise ValueError()
            return auth
        except Exception as exc:
            raise ValueError("PROJECT_SCOPE_MISMATCH") from exc

    def _load(self, ctx: ProjectExecutionContext) -> dict[str, Any]:
        try:
            with self._saver(ctx) as saver:
                saved = saver.get_tuple(self._config(ctx))
            if saved is None:
                return {
                    "contract_version": "t27-v1",
                    "identity": self._key(ctx),
                    "global_revision_counter": 0,
                    "generation": 0,
                    "active_run": None,
                    "active_branch_id": "main",
                    "runs": {},
                    "branches": [],
                }
            state = saved.checkpoint["channel_values"]["project_calculation"]
            if (
                state["contract_version"] != "t27-v1"
                or state["identity"] != self._key(ctx)
                or type(state["generation"]) is not int
                or type(state["global_revision_counter"]) is not int
            ):
                raise ValueError()
            # Detach each transaction's mutable values from serializer internals.
            return cast(dict[str, Any], json.loads(_json(state)))
        except Exception as exc:
            raise ValueError("PROJECT_UNTRUSTED_SOURCE") from exc

    def _save(self, ctx: ProjectExecutionContext, state: dict[str, Any]) -> None:
        try:
            # Explicit projections in the existing checkpoint; no new database schema.
            runs = list(state["runs"].values())
            head = state["runs"].get(state["active_run"])
            state.update(
                owner_user_id=ctx.owner_user_id,
                project_id=ctx.project_id,
                thread_id=ctx.thread_id,
                active_revision=head["plan"]["revision"] if head else 0,
                branch_id=state["active_branch_id"],
                authorization_epoch=head["authorization_epoch"] if head else 1,
                plans={r["plan_hash"]: r["plan"] for r in runs},
                execution_records={k: v for r in runs for k, v in r["execution_records"].items()},
                history_artifacts={k: v for r in runs for k, v in r["artifacts"].items()},
                active_artifact_ids=head["active_artifact_ids"]
                if head
                and head["status"]
                not in ("cancelled", "blocked", "failed", "indeterminate", "stale")
                else {},
                approval_binding=head["approval"]
                if head and head["status"] == "approved"
                else None,
                plan_lifecycle={
                    r["plan_hash"]: {
                        "plan_hash": r["plan_hash"],
                        "status": r["status"],
                        "generation": r["generation"],
                        "created_at": r["created_at"],
                        "updated_at": r["updated_at"],
                        "supersedes_plan_hash": r["plan"]["supersedes_plan_hash"],
                    }
                    for r in runs
                },
                run_publications=state.get("run_publications", {}),
                publication_events=state.get("publication_events", {}),
            )
            cp = empty_checkpoint()
            cp["channel_values"] = {"project_calculation": json.loads(_json(state))}
            cp["channel_versions"] = {"project_calculation": cp["id"]}
            with self._saver(ctx) as saver:
                saver.put(
                    self._config(ctx),
                    cp,
                    {"source": "update", "step": -1, "parents": {}},
                    cp["channel_versions"],
                )
        except Exception as exc:
            raise ValueError("PROJECT_PERSISTENCE_FAILED") from exc

    def _run(self, state: dict[str, Any], ctx: ProjectExecutionContext) -> dict[str, Any]:
        run: dict[str, Any] | None = state["runs"].get(ctx.run_id)
        if run is None:
            raise ValueError("PROJECT_SCOPE_MISMATCH")
        try:
            identity_matches = all(
                run["context"][k] == getattr(ctx, k)
                for k in ("owner_user_id", "thread_id", "run_id", "project_id", "plan_id")
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("PROJECT_UNTRUSTED_SOURCE") from exc
        if not identity_matches:
            raise ValueError("PROJECT_SCOPE_MISMATCH")
        try:
            plan = ProjectCalculationPlan.model_validate(run["plan"])
            if (
                run["plan_hash"] != compute_payload_hash(plan.model_dump())
                or type(run["generation"]) is not int
                or type(run["authorization_epoch"]) is not int
                or run["status"]
                not in {
                    "confirmed",
                    "running",
                    "waiting_approval",
                    "approved",
                    "published",
                    "failed",
                    "cancelled",
                    "stale",
                    "indeterminate",
                    "blocked",
                    "missing_inputs",
                }
            ):
                raise ValueError()
            context = ProjectExecutionContext(
                **run["context"], principal=ctx.principal, cancel_check=lambda: False
            )
            if (
                context.plan_revision != plan.revision
                or context.plan_id != plan.plan_id
                or context.project_id != plan.project_id
                or context.branch_id != plan.branch_id
                or context.generation != run["generation"]
                or context.authorization_epoch != run["authorization_epoch"]
            ):
                raise ValueError()
            for execution_id, record in run["execution_records"].items():
                parsed = NodeExecutionRecord.model_validate(record)
                if parsed.execution_id != execution_id:
                    raise ValueError()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("PROJECT_UNTRUSTED_SOURCE") from exc
        return run

    def _current(
        self, state: dict[str, Any], ctx: ProjectExecutionContext, auth: Authorization
    ) -> dict[str, Any]:
        run = self._run(state, ctx)
        if run["status"] == "cancelled":
            raise ValueError("PROJECT_CANCELLED")
        if (
            state["active_run"] != ctx.run_id
            or state["generation"] != run["generation"]
            or state["active_branch_id"] != run["plan"]["branch_id"]
        ):
            raise ValueError("PROJECT_STALE_SOURCE")
        if (
            auth.epoch != run["authorization_epoch"]
            or auth.principal.principal_hash != run["principal_id"]
        ):
            raise ValueError("PROJECT_SCOPE_MISMATCH")
        if run["algorithm_version"] != algorithm_version():
            raise ValueError("PROJECT_STALE_SOURCE")
        return run

    def inspect(self, ctx: ProjectExecutionContext) -> dict[str, Any]:
        with self._locked(ctx):
            self._auth(ctx)
            return self._load(ctx)

    def confirm(
        self,
        raw: object,
        ctx: ProjectExecutionContext,
        *,
        base_revision: int,
        generation: int,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """Synthetic trusted host confirmation; not a login or human approval endpoint."""
        with self._locked(ctx):
            auth = self._auth(ctx)
            state = self._load(ctx)
            head = state["runs"].get(state["active_run"])
            active_revision = head["plan"]["revision"] if head else 0
            if (
                type(base_revision) is not int
                or type(generation) is not int
                or base_revision != active_revision
                or generation != state["generation"]
                or ctx.run_id in state["runs"]
            ):
                raise ValueError("PROJECT_REVISION_CONFLICT")
            selected = branch or state["active_branch_id"]
            if selected != state["active_branch_id"] and selected in state["branches"]:
                raise ValueError("PROJECT_REVISION_CONFLICT")
            if not isinstance(raw, (dict, ProjectCalculationPlan)):
                raise ValueError("PROJECT_INVALID_PLAN")  # noqa: TRY004 - stable project error
            data = raw.model_dump() if isinstance(raw, ProjectCalculationPlan) else dict(raw)
            data.update(
                revision=state["global_revision_counter"] + 1,
                base_revision=base_revision,
                supersedes_plan_hash=head["plan_hash"] if head else None,
                branch_id=selected,
            )
            plan = ProjectCalculationPlan.model_validate(data)
            if plan.project_id != ctx.project_id or plan.plan_id != ctx.plan_id:
                raise ValueError("PROJECT_SCOPE_MISMATCH")
            if head:
                self._settle_started(head, "cancelled", "PROJECT_STALE_SOURCE")
                head["status"] = "stale" if head["status"] != "cancelled" else "cancelled"
            gen = state["generation"] + 1
            context = replace(
                ctx,
                plan_revision=plan.revision,
                branch_id=selected,
                generation=gen,
                authorization_epoch=auth.epoch,
                checkpoint_namespace="t27-v1",
                principal=auth.principal,
            )
            run = {
                "context": _identity(context),
                "plan": plan.model_dump(),
                "plan_hash": compute_payload_hash(plan.model_dump()),
                "generation": gen,
                "authorization_epoch": auth.epoch,
                "principal_id": auth.principal.principal_hash,
                "algorithm_version": algorithm_version(),
                "status": "confirmed",
                "created_at": self.now().isoformat(),
                "updated_at": self.now().isoformat(),
                "confirmation_expires_at": (self.now() + timedelta(minutes=15)).isoformat(),
                "execution_records": {},
                "receipts": {},
                "artifacts": {},
                "active_artifact_ids": {},
                "result": None,
                "approval": None,
                "execution_manifest_hash": None,
                "release_manifest_hash": None,
                "error": None,
            }
            state["runs"][ctx.run_id] = run
            state.update(
                global_revision_counter=plan.revision,
                generation=gen,
                active_run=ctx.run_id,
                active_branch_id=selected,
            )
            if selected not in state["branches"]:
                state["branches"].append(selected)
            self._save(ctx, state)
            return run

    def _check_execution(self, ctx: ProjectExecutionContext) -> None:
        with self._locked(ctx):
            auth = self._auth(ctx)
            run = self._current(self._load(ctx), ctx, auth)
            if run["status"] != "running":
                raise ValueError("PROJECT_STALE_SOURCE")
            if self.now() >= datetime.fromisoformat(run["confirmation_expires_at"]):
                raise ValueError("PROJECT_STALE_SOURCE")

    def _persist_execution(
        self,
        ctx: ProjectExecutionContext,
        stage: str,
        record: NodeExecutionRecord,
        policy: dict | None = None,
        artifact: ArtifactEnvelope | None = None,
    ) -> None:
        with self._locked(ctx):
            auth = self._auth(ctx)
            state = self._load(ctx)
            run = self._current(state, ctx, auth)
            if run["status"] != "running":
                raise ValueError("PROJECT_STALE_SOURCE")
            self.fault(stage)
            if artifact is None:
                run["execution_records"][record.execution_id] = record.model_dump()
                if policy is not None:
                    run["receipts"][record.execution_id] = {
                        "invocation_id": record.invocation_id,
                        "args_hash": record.args_hash,
                        "policy": policy,
                    }
            else:
                run["artifacts"][artifact.artifact_id] = artifact.model_dump()
                run["active_artifact_ids"][record.node_id] = artifact.artifact_id
            self._save(ctx, state)
            self.fault("after_" + stage)

    def _lease(self, ctx: ProjectExecutionContext):
        path = self.root / (self._key(ctx) + "-" + ctx.run_id + ".lease")
        f = path.open("a+b")
        path.chmod(0o600)
        return f

    def execute(self, ctx: ProjectExecutionContext) -> dict[str, Any]:
        self._auth(ctx)
        with self._lease(ctx) as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("PROJECT_REVISION_CONFLICT") from exc
            with self._locked(ctx):
                auth = self._auth(ctx)
                state = self._load(ctx)
                run = self._current(state, ctx, auth)
                if run["status"] != "confirmed":
                    raise ValueError("PROJECT_REVISION_CONFLICT")
                if self.now() >= datetime.fromisoformat(run["confirmation_expires_at"]):
                    raise ValueError("PROJECT_STALE_SOURCE")
                run["status"] = "running"
                self._save(ctx, state)
                context = ProjectExecutionContext(
                    **run["context"], principal=auth.principal, cancel_check=lambda: False
                )
            local = ProjectState.confirm_for_offline(run["plan"], context)
            local.authority_check = lambda: self._check_execution(context)
            local.on_started = lambda rec: self._persist_execution(context, "started", rec)

            def associate(execution_id, invocation_id, args_hash, policy):
                rec = local.execution_records[execution_id].model_copy(
                    update={"invocation_id": invocation_id}
                )
                self._persist_execution(context, "associated", rec, policy)

            local.before_execute = associate
            local.on_verified = lambda rec: self._persist_execution(context, "verified", rec)
            local.on_artifact = lambda env, rec: self._persist_execution(
                context, "artifact", rec, artifact=env
            )
            try:
                result = execute_project_plan(run["plan"], context, local)
                with self._locked(ctx):
                    auth = self._auth(ctx)
                    state = self._load(ctx)
                    run = self._current(state, ctx, auth)
                    if result.status != "ready":
                        run["status"] = result.status
                        run["error"] = (
                            result.errors[0].code if result.errors else "PROJECT_EXECUTION_FAILED"
                        )
                        self._settle_started(run, "failed", run["error"])
                        run["active_artifact_ids"] = {}
                    else:
                        run["result"] = result.model_dump()
                        run["execution_manifest_hash"] = result.manifest_hash
                        self._candidates(run)
                        run["status"] = "waiting_approval"
                        run["updated_at"] = self.now().isoformat()
                    self._save(ctx, state)
                    return run
            except Exception as exc:  # noqa: BLE001 - fail closed at the host boundary
                return self._fail(ctx, str(exc))

    def _settle_started(self, run: dict, status: str, code: str) -> None:
        for rec in run["execution_records"].values():
            if rec["status"] == "started":
                rec.update(
                    status=status,
                    outputs={},
                    finished_at=self.now().isoformat(),
                    error={
                        "code": code,
                        "node_id": rec["node_id"],
                        "field_path": None,
                        "retryable": False,
                        "message": code,
                    },
                )

    def _fail(self, ctx: ProjectExecutionContext, code: str) -> dict[str, Any]:
        with self._locked(ctx):
            state = self._load(ctx)
            run = self._run(state, ctx)
            if state["active_run"] != ctx.run_id or run["status"] in ("cancelled", "stale"):
                return run
            run["status"] = "blocked"
            self._settle_started(run, "failed", "PROJECT_PERSISTENCE_FAILED")
            run["error"] = (
                code
                if code in ("PROJECT_SCOPE_MISMATCH", "PROJECT_STALE_SOURCE")
                else "PROJECT_PERSISTENCE_FAILED"
            )
            run["active_artifact_ids"] = {}
            self._save(ctx, state)
            return run

    def _envelope(self, run: dict, role: str, payload: dict, parents: list[str]) -> None:
        aid = "calc-" + uuid4().hex
        typ: Literal["document", "review_package", "calc_package"] = (
            "document"
            if role == "document"
            else "review_package"
            if role == "review"
            else "calc_package"
        )
        env = ArtifactEnvelope(
            artifact_id=aid,
            artifact_type=typ,
            schema_version="t27-v1",
            producer="review" if role == "review" else "hydraulic",
            revision=run["plan"]["revision"],
            parent_artifact_ids=parents,
            payload_hash=compute_payload_hash(payload),
            payload=payload,
            citation_ids=[],
            assumptions=[],
            warnings=[],
            status="ready",
        )
        run["artifacts"][aid] = env.model_dump()
        run["active_artifact_ids"][role] = aid

    def _candidates(self, run: dict) -> None:
        ids = run["active_artifact_ids"]
        nodes = [ids[n] for n in ROLES[:3]]
        self._envelope(run, "calculation", run["result"], nodes)
        if self.document_backend is not None:
            document_payload = self.document_backend.create(run)
        else:
            content = _json({"nodes": run["result"]["node_records"], "limitations": LIMITATIONS})
            document = DocumentCandidate(
                content=content,
                content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                byte_size=len(content.encode()),
                execution_manifest_hash=run["execution_manifest_hash"],
                limitations=LIMITATIONS,
            )
            document_payload = document.model_dump()
        self._envelope(run, "document", document_payload, [*nodes, ids["calculation"]])
        self._envelope(
            run,
            "review",
            {
                "contract_version": "t27-v1",
                "decision": "pass",
                "scope": "mathematics_and_sources_only",
                "limitations": LIMITATIONS,
                "disclosures": run["result"]["assumptions"],
                "reviewed_artifact_hashes": [
                    run["artifacts"][a]["payload_hash"] for a in [ids[r] for r in ROLES[:-1]]
                ],
            },
            [*nodes, ids["calculation"], ids["document"]],
        )
        run["release_manifest_hash"] = self._collection(run)
        self._validate(run)

    def _collection(self, run: dict) -> str:
        artifacts = [run["artifacts"][run["active_artifact_ids"][r]] for r in ROLES]
        return compute_artifact_collection_hash(
            [a["payload_hash"] for a in artifacts],
            artifact_ids=[a["artifact_id"] for a in artifacts],
            artifact_revisions=[a["revision"] for a in artifacts],
            artifact_parent_artifact_ids=[a["parent_artifact_ids"] for a in artifacts],
        )

    def _validate(self, run: dict) -> None:
        """Strict dispatch. Hash consistency alone cannot authenticate an executed node."""
        try:
            plan = ProjectCalculationPlan.model_validate(run["plan"])
            result = ProjectCalculationResult.model_validate(run["result"])
            for key in ("plan_id", "project_id", "branch_id", "revision", "input_set_id"):
                if getattr(result, key) != getattr(plan, key):
                    raise ValueError()
            if result.plan_hash != run["plan_hash"]:
                raise ValueError()
            if run["plan_hash"] != compute_payload_hash(plan.model_dump()):
                raise ValueError()
            if set(run["active_artifact_ids"]) != set(ROLES):
                raise ValueError()
            auth_ctx = run["context"]
            # Reuse B's exact source and context checks, reconstructed only from trusted checkpoint.
            principal = Principal.local("checkpoint-validation")
            ctx = ProjectExecutionContext(
                **auth_ctx, principal=principal, cancel_check=lambda: False
            )
            local = ProjectState.confirm_for_offline(plan, ctx)
            local.execution_records = {
                k: NodeExecutionRecord.model_validate(v)
                for k, v in run["execution_records"].items()
            }
            local._trusted = {k: _json(v.model_dump()) for k, v in local.execution_records.items()}
            if len(local.execution_records) != 3 or len(run["receipts"]) != 3:
                raise ValueError()
            ids = run["active_artifact_ids"]
            for i, role in enumerate(ROLES):
                env = ArtifactEnvelope.model_validate(run["artifacts"][ids[role]], strict=True)
                if (
                    env.schema_version != "t27-v1"
                    or env.status != "ready"
                    or env.revision != plan.revision
                    or env.payload_hash != compute_payload_hash(env.payload)
                    or env.artifact_id != ids[role]
                ):
                    raise ValueError()
                expected = (
                    [] if i == 0 else [ids[ROLES[i - 1]]] if i < 3 else [ids[r] for r in ROLES[:i]]
                )
                if env.parent_artifact_ids != expected:
                    raise ValueError()
                expected_type = (
                    "document"
                    if role == "document"
                    else "review_package"
                    if role == "review"
                    else "calc_package"
                )
                expected_producer = "review" if role == "review" else "hydraulic"
                if env.artifact_type != expected_type or env.producer != expected_producer:
                    raise ValueError()
                local.artifacts[env.artifact_id] = env
                if i < 3:
                    from water_agent.multi_agent.project_calculation_contracts import (
                        OUTPUTS,
                        ProgramOutputBinding,
                    )

                    rec = NodeExecutionRecord.model_validate(env.payload["node_record"])
                    receipt = run["receipts"][rec.execution_id]
                    if (
                        receipt["invocation_id"] != rec.invocation_id
                        or receipt["args_hash"] != rec.args_hash
                        or receipt["policy"].get("action") != "allow"
                        or rec.algorithm_version != run["algorithm_version"]
                        or rec.plan_hash != run["plan_hash"]
                        or rec != result.node_records[i]
                    ):
                        raise ValueError()
                    local.active_artifact_ids[role] = env.artifact_id
                    binding = ProgramOutputBinding(
                        kind="program",
                        node_id=role,
                        case_id=plan.nodes[i].case_id,
                        artifact_id=env.artifact_id,
                        revision=plan.revision,
                        output_key=cast(Any, OUTPUTS[i]),
                        payload_hash=env.payload_hash,
                        unit="m/s" if i == 0 else "m",
                    )
                    local.validate_source(binding, ctx)
                    if rec.args_hash != compute_payload_hash(rec.resolved_inputs):
                        raise ValueError()
                    for key, source in rec.input_sources.items():
                        if source.kind == "program":
                            prev = local.validate_source(source, ctx)
                            if (
                                rec.resolved_inputs[key].hex()
                                != prev.outputs[source.output_key].hex()
                            ):
                                raise ValueError()
                        elif (
                            source != plan.nodes[i].inputs[key]
                            or rec.resolved_inputs[key].hex() != source.value.hex()
                        ):
                            raise ValueError()
            for i, rec in enumerate(result.node_records):
                from water_agent.multi_agent.project_calculation_contracts import (
                    INPUT_UNITS,
                    TOOL_NAMES,
                )

                expected_units = {**INPUT_UNITS[i], OUTPUTS[i]: "m/s" if i == 0 else "m"}
                if (
                    rec.units != expected_units
                    or rec.tool_name != TOOL_NAMES[i]
                    or rec.tool_identity_version != "t27-v1"
                    or rec.formula != plan.nodes[i].formula
                    or set(rec.resolved_inputs) != set(INPUT_UNITS[i])
                ):
                    raise ValueError()
                own_notes = [
                    n.message
                    for n in rec.assumptions
                    if n.source_node_id == rec.node_id and n.code == "kernel_assumption"
                ]
                if rec.result_hash != compute_payload_hash(
                    {"outputs": rec.outputs, "assumptions": own_notes}
                ):
                    raise ValueError()
                a = rec.resolved_inputs
                expected_numeric = (
                    a["flow_lps"] / 1000 / (math.pi * (a["diameter_mm"] / 1000 / 2) ** 2)
                    if i == 0
                    else a["friction_factor"]
                    * a["length_m"]
                    / (a["diameter_mm"] / 1000)
                    * a["velocity_mps"] ** 2
                    / (2 * a["gravity_mps2"])
                    if i == 1
                    else math.fsum(a.values())
                )
                actual = rec.outputs[OUTPUTS[i]]
                tolerance = math.ulp(expected_numeric) if i == 2 else abs(expected_numeric) * 1e-12
                if abs(actual - expected_numeric) > tolerance:
                    raise ValueError()
            expected_manifest = compute_payload_hash(
                {
                    "plan_hash": run["plan_hash"],
                    "context": run["context"],
                    "artifact_ids": [ids[r] for r in ROLES[:3]],
                    "artifact_hashes": [local.artifacts[ids[r]].payload_hash for r in ROLES[:3]],
                    "revision": plan.revision,
                }
            )
            if expected_manifest != run["execution_manifest_hash"]:
                raise ValueError()
            if result.status != "ready" or result.manifest_hash != run["execution_manifest_hash"]:
                raise ValueError()
            if local.artifacts[ids["calculation"]].payload != result.model_dump():
                raise ValueError()
            document_payload = local.artifacts[ids["document"]].payload
            if document_payload.get("renderer_version") == "t27-docx-v1":
                if self.document_backend is None:
                    raise ValueError()
                self.document_backend.validate(run, document_payload)
            else:
                doc = DocumentCandidate.model_validate(local.artifacts[ids["document"]].payload)
                if (
                    doc.content_sha256 != hashlib.sha256(doc.content.encode()).hexdigest()
                    or doc.byte_size != len(doc.content.encode())
                    or doc.execution_manifest_hash != result.manifest_hash
                ):
                    raise ValueError()
                expected_content = _json(
                    {"nodes": run["result"]["node_records"], "limitations": LIMITATIONS}
                )
                if doc.content != expected_content or doc.limitations != LIMITATIONS:
                    raise ValueError()
            review = local.artifacts[ids["review"]].payload
            if (
                review["decision"] != "pass"
                or review["contract_version"] != "t27-v1"
                or review["disclosures"] != run["result"]["assumptions"]
                or review["reviewed_artifact_hashes"]
                != [local.artifacts[ids[r]].payload_hash for r in ROLES[:-1]]
            ):
                raise ValueError()
            if self._collection(run) != run["release_manifest_hash"]:
                raise ValueError()
        except Exception as exc:
            raise ValueError("PROJECT_UNTRUSTED_SOURCE") from exc

    def cancel(self, ctx: ProjectExecutionContext) -> dict[str, Any]:
        with self._locked(ctx):
            self._auth(ctx)
            state = self._load(ctx)
            run = self._run(state, ctx)
            if state.get("run_publications", {}).get(ctx.run_id, {}).get("state") == "committed":
                return run
            if run["status"] in ("failed", "blocked", "indeterminate") or ctx.run_id in state.get(
                "project_terminal_events", {}
            ):
                return run
            if state["active_run"] != ctx.run_id:
                raise ValueError("PROJECT_STALE_SOURCE")
            if run["status"] != "cancelled":
                state["generation"] += 1
                run["status"] = "cancelled"
                run["error"] = "PROJECT_CANCELLED"
                self._settle_started(run, "cancelled", "PROJECT_CANCELLED")
                run["active_artifact_ids"] = {}
                self._save(ctx, state)
            return run

    def approve(
        self, ctx: ProjectExecutionContext, decision: object, reason: str
    ) -> dict[str, Any]:
        with self._locked(ctx):
            auth = self._auth(ctx)
            state = self._load(ctx)
            run = self._current(state, ctx, auth)
            if run["status"] != "waiting_approval":
                raise ValueError("PROJECT_APPROVAL_STALE")
            self._validate(run)
            if self.now() >= datetime.fromisoformat(run["updated_at"]) + timedelta(minutes=30):
                raise ValueError("PROJECT_APPROVAL_STALE")
            if type(decision) is not bool:
                return run
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("PROJECT_INVALID_VALUE")
            artifacts = [run["artifacts"][run["active_artifact_ids"][r]] for r in ROLES]
            decided_at = self.now()
            record = ApprovalRecord(
                approved=decision,
                reason=reason,
                artifact_ids=[a["artifact_id"] for a in artifacts],
                artifact_hashes=[a["payload_hash"] for a in artifacts],
                artifact_revisions=[a["revision"] for a in artifacts],
                artifact_parent_artifact_ids=[a["parent_artifact_ids"] for a in artifacts],
                artifact_collection_hash=run["release_manifest_hash"],
                final_package_hash=run["release_manifest_hash"],
                approved_at=decided_at.isoformat(),
                expires_at=(decided_at + timedelta(minutes=30)).isoformat(),
            )
            run["approval"] = {
                "record": record.model_dump(),
                "context": run["context"],
                "plan_hash": run["plan_hash"],
                "review_artifact_id": run["active_artifact_ids"]["review"],
                "approved_by_user_id": ctx.owner_user_id,
                "release_manifest_hash": run["release_manifest_hash"],
            }
            run["status"] = "approved" if decision else "failed"
            self._save(ctx, state)
            return run

    def _validate_approval(self, run: dict[str, Any], ctx: ProjectExecutionContext) -> None:
        approval = run["approval"]
        if (
            run["status"] != "approved"
            or not approval
            or approval["context"] != run["context"]
            or approval["plan_hash"] != run["plan_hash"]
            or approval["review_artifact_id"] != run["active_artifact_ids"]["review"]
            or approval["approved_by_user_id"] != ctx.owner_user_id
            or approval["release_manifest_hash"] != run["release_manifest_hash"]
        ):
            raise ValueError()
        record = ApprovalRecord.model_validate(approval["record"], strict=True)
        from water_agent.multi_agent.provenance import validate_approval_binding

        envelopes = {
            run["active_artifact_ids"][r]: ArtifactEnvelope.model_validate(
                run["artifacts"][run["active_artifact_ids"][r]], strict=True
            )
            for r in ROLES
        }
        if not validate_approval_binding(record, envelopes):
            raise ValueError("PROJECT_APPROVAL_STALE")
        if (
            record.approved is not True
            or record.final_package_hash != run["release_manifest_hash"]
            or not record.expires_at
            or self.now() >= datetime.fromisoformat(record.expires_at)
            or datetime.fromisoformat(record.expires_at)
            - datetime.fromisoformat(record.approved_at)
            > timedelta(minutes=30)
            or datetime.fromisoformat(record.expires_at)
            <= datetime.fromisoformat(record.approved_at)
        ):
            raise ValueError()

    def release_check(self, run_id: str, ctx: ProjectExecutionContext) -> dict[str, Any]:
        """Validate a future commit boundary; never commits or labels a run published."""
        try:
            bound = replace(ctx, run_id=run_id)
            with self._locked(bound):
                auth = self._auth(bound)
                run = self._current(self._load(bound), bound, auth)
                self._validate(run)
                self._validate_approval(run, ctx)
                return {"valid": True, "status": "approved"}
        except Exception:  # noqa: BLE001 - commit validation fails closed
            return {"valid": False, "error": "PROJECT_APPROVAL_STALE"}

    def recover(self, ctx: ProjectExecutionContext) -> dict[str, Any]:
        with self._locked(ctx):
            auth = self._auth(ctx)
            state = self._load(ctx)
            run = self._run(state, ctx)
            if auth.epoch != run["authorization_epoch"]:
                raise ValueError("PROJECT_SCOPE_MISMATCH")
            if run["status"] == "running":
                with self._lease(ctx) as lease:
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return run
                    run["status"] = "indeterminate"
                    run["error"] = "PROJECT_EXECUTION_INDETERMINATE"
                    run["active_artifact_ids"] = {}
                    for rec in run["execution_records"].values():
                        if rec["status"] == "started":
                            rec.update(
                                status="indeterminate",
                                outputs={},
                                finished_at=self.now().isoformat(),
                                error={
                                    "code": "PROJECT_EXECUTION_INDETERMINATE",
                                    "node_id": rec["node_id"],
                                    "field_path": None,
                                    "retryable": False,
                                    "message": "PROJECT_EXECUTION_INDETERMINATE",
                                },
                            )
                    self._save(ctx, state)
            elif run["status"] in ("waiting_approval", "approved"):
                self._current(state, ctx, auth)
                self._validate(run)
                if run["status"] == "approved":
                    self._validate_approval(run, ctx)
            return run
