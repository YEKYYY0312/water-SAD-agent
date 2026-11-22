"""Immutable document candidates and version-two publication over existing checkpoints.

The checkpoint is the commit authority. Snapshot/blob presence never grants access.
No rendering, calculations or model calls occur on a committed read/replay.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from water_agent.documents.storage import ExportSnapshotStore
from water_agent.graphs.project_calculation_answer import render_project_calculation_summary
from water_agent.multi_agent.contracts import ApprovalRecord, ArtifactEnvelope, compute_payload_hash
from water_agent.multi_agent.project_calculation import _json
from water_agent.multi_agent.project_calculation_contracts import (
    ID,
    Hash,
    ProjectCalculationResult,
    ProjectExecutionContext,
    StrictModel,
    UUIDText,
)
from water_agent.multi_agent.project_calculation_service import ROLES, CalculationService
from water_agent.multi_agent.provenance import validate_approval_binding


class ProjectBinding(StrictModel):
    project_id: ID
    owner_user_id: UUIDText
    thread_id: UUIDText
    run_id: UUIDText
    plan_id: ID
    branch_id: ID
    revision: int = Field(ge=1)
    generation: int = Field(ge=1)
    plan_hash: Hash
    release_manifest_hash: Hash


class DocumentBlob(StrictModel):
    document_file_id: UUIDText
    run_id: UUIDText
    owner_user_id: UUIDText
    thread_id: UUIDText
    project_id: ID
    plan_hash: Hash
    revision: int = Field(ge=1)
    release_manifest_hash: Hash
    content_sha256: Hash
    byte_size: int = Field(ge=1, le=4 * 1024 * 1024)
    renderer_version: Literal["t27-docx-v1"]


class RenderedCandidate(StrictModel):
    contract_version: Literal["t27-v1"]
    state: Literal["ready_for_approval"]
    renderer_version: Literal["t27-docx-v1"]
    content_sha256: Hash
    byte_size: int = Field(ge=1, le=4 * 1024 * 1024)
    execution_manifest_hash: Hash


class Publication(StrictModel):
    publication_id: UUIDText
    run_id: UUIDText
    owner_user_id: UUIDText
    thread_id: UUIDText
    project_id: ID
    plan_id: ID
    branch_id: ID
    revision: int = Field(ge=1)
    generation: int = Field(ge=1)
    plan_hash: Hash
    manifest_hash: Hash
    release_manifest_hash: Hash
    snapshot_version: Literal[2]
    snapshot_hash: Hash
    state: Literal["prepared", "committed", "aborted"]
    published_at: str | None


def check_output(text: str) -> bool:
    from water_agent.runtime.governance.binding import local_request_context
    from water_agent.runtime.governance.pipeline import default_guardrail_pipeline

    outcome = default_guardrail_pipeline().check_output(
        text, local_request_context(thread_id="project-publication")
    )
    return outcome.allowed and outcome.content == text


def validate_snapshot_v2(data: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
    if (
        set(data) != {"version", "owner_user_id", "snapshot", "finalize"}
        or type(data["version"]) is not int
        or data["version"] != 2
    ):
        raise ValueError("PROJECT_PERSISTENCE_FAILED")
    snapshot = data["snapshot"]
    extra = {"project_calculation", "project_binding", "document_blob", "publication_id"}
    base_keys = {
        "run_id",
        "status",
        "answer",
        "citations",
        "calculations",
        "calc_inputs",
        "material_table",
    }
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != base_keys | extra
        or snapshot["calc_inputs"] != {}
    ):
        raise ValueError("PROJECT_PERSISTENCE_FAILED")
    binding = ProjectBinding.model_validate(snapshot["project_binding"])
    blob = DocumentBlob.model_validate(snapshot["document_blob"])
    result = ProjectCalculationResult.model_validate(snapshot["project_calculation"])
    ExportSnapshotStore._uuid(snapshot["publication_id"])
    if (
        binding.run_id != snapshot["run_id"]
        or data["owner_user_id"] != binding.owner_user_id
        or data["finalize"]["thread_id"] != binding.thread_id
        or result.status != "ready"
        or result.plan_hash != binding.plan_hash
    ):
        raise ValueError("PROJECT_PERSISTENCE_FAILED")
    for node in result.node_records:
        if node.plan_hash != binding.plan_hash:
            raise ValueError("PROJECT_PERSISTENCE_FAILED")
        for key in (
            "owner_user_id",
            "thread_id",
            "run_id",
            "project_id",
            "plan_id",
            "branch_id",
            "revision",
            "generation",
        ):
            if node.context_binding[key] != getattr(binding, key):
                raise ValueError("PROJECT_PERSISTENCE_FAILED")
    for key in ("project_id", "plan_id", "branch_id", "revision"):
        if getattr(result, key) != getattr(binding, key):
            raise ValueError("PROJECT_PERSISTENCE_FAILED")
    for key in (
        "run_id",
        "owner_user_id",
        "thread_id",
        "project_id",
        "plan_hash",
        "revision",
        "release_manifest_hash",
    ):
        if getattr(blob, key) != getattr(binding, key):
            raise ValueError("PROJECT_PERSISTENCE_FAILED")
    payload = data["finalize"]["payload"]
    if _json(payload.get("project_binding")) != _json(binding.model_dump()):
        raise ValueError("PROJECT_PERSISTENCE_FAILED")
    base = {
        "version": 1,
        "owner_user_id": data["owner_user_id"],
        "snapshot": {k: snapshot[k] for k in base_keys},
        "finalize": {
            **data["finalize"],
            "payload": {k: v for k, v in payload.items() if k != "project_binding"},
        },
    }
    ExportSnapshotStore._validate(base, run_id)
    return data


class DocumentBackend:
    def __init__(self, root: Path):
        self.root = root.absolute()

    def create(self, run: dict) -> dict:
        from water_agent.docgen.project_calculation_document import (
            project_document_text,
            render_project_calculation_docx,
        )

        content = render_project_calculation_docx(run)
        if not check_output(project_document_text(content)):
            raise ValueError("PROJECT_OUTPUT_BLOCKED")
        if not 0 < len(content) <= 4 * 1024 * 1024:
            raise ValueError("PROJECT_PERSISTENCE_FAILED")
        file_id = str(uuid4())
        directory = ExportSnapshotStore(self.root)._open_root(create=True)
        try:
            fd = os.open(
                file_id + ".docx",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.fsync(directory)
        finally:
            os.close(directory)
        run["candidate_file_id"] = file_id
        return RenderedCandidate(
            contract_version="t27-v1",
            state="ready_for_approval",
            renderer_version="t27-docx-v1",
            content_sha256=hashlib.sha256(content).hexdigest(),
            byte_size=len(content),
            execution_manifest_hash=run["execution_manifest_hash"],
        ).model_dump()

    def bytes(self, file_id: str) -> bytes:
        try:
            ExportSnapshotStore._uuid(file_id)
            directory = ExportSnapshotStore(self.root)._open_root(create=False)
            try:
                fd = os.open(
                    file_id + ".docx", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
            finally:
                os.close(directory)
            with os.fdopen(fd, "rb") as f:
                info = os.fstat(f.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or not 0 < info.st_size <= 4 * 1024 * 1024
                ):
                    raise ValueError()
                content = f.read(4 * 1024 * 1024 + 1)
                after = os.fstat(f.fileno())
                if (
                    len(content) != info.st_size
                    or after.st_mtime_ns != info.st_mtime_ns
                    or after.st_nlink != 1
                ):
                    raise ValueError()
                return content
        except (OSError, ValueError) as exc:
            raise ValueError("PROJECT_PERSISTENCE_FAILED") from exc

    def validate(self, run: dict, raw: dict) -> None:
        from water_agent.docgen.project_calculation_document import project_document_text

        candidate = RenderedCandidate.model_validate(raw)
        content = self.bytes(run["candidate_file_id"])
        if (
            hashlib.sha256(content).hexdigest() != candidate.content_sha256
            or len(content) != candidate.byte_size
            or candidate.execution_manifest_hash != run["execution_manifest_hash"]
        ):
            raise ValueError("PROJECT_PERSISTENCE_FAILED")
        if not check_output(project_document_text(content)):
            raise ValueError("PROJECT_OUTPUT_BLOCKED")


class ProjectPublisher:
    def __init__(
        self,
        service: CalculationService,
        snapshots: ExportSnapshotStore,
        documents: DocumentBackend,
        *,
        set_succeeded: Callable[[ProjectExecutionContext], None],
    ):
        self.service = service
        self.snapshots = snapshots
        self.documents = documents
        self.set_succeeded = set_succeeded
        self.fault: Callable[[str], None] = lambda _: None
        self.check_output: Callable[[str], bool] = check_output

    def publish(self, ctx: ProjectExecutionContext) -> dict:
        s = self.service
        with s._locked(ctx):
            auth = s._auth(ctx)
            state = s._load(ctx)
            existing = state.get("run_publications", {}).get(ctx.run_id)
            if existing:
                if existing["state"] != "committed":
                    raise ValueError("PROJECT_PERSISTENCE_FAILED")
                # Read outside the project lock, never render another candidate.
            else:
                run = s._current(state, ctx, auth)
                s._validate(run)
                s._validate_approval(run, ctx)
                payload_doc = run["artifacts"][run["active_artifact_ids"]["document"]]["payload"]
                self.documents.validate(run, payload_doc)
                answer = render_project_calculation_summary(run)
                if not self.check_output(answer):
                    raise ValueError("PROJECT_OUTPUT_BLOCKED")
                identity = {
                    k: run["context"][k]
                    for k in (
                        "project_id",
                        "owner_user_id",
                        "thread_id",
                        "run_id",
                        "plan_id",
                        "branch_id",
                        "generation",
                    )
                }
                binding = ProjectBinding(
                    **identity,
                    revision=run["plan"]["revision"],
                    plan_hash=run["plan_hash"],
                    release_manifest_hash=run["release_manifest_hash"],
                )
                blob = DocumentBlob(
                    **{
                        k: getattr(binding, k)
                        for k in (
                            "project_id",
                            "owner_user_id",
                            "thread_id",
                            "run_id",
                            "plan_hash",
                            "revision",
                            "release_manifest_hash",
                        )
                    },
                    document_file_id=run["candidate_file_id"],
                    content_sha256=payload_doc["content_sha256"],
                    byte_size=payload_doc["byte_size"],
                    renderer_version="t27-docx-v1",
                )
                publication_id = str(uuid4())
                now = s.now().isoformat()
                final = {
                    "event_id": "2",
                    "type": "finalize",
                    "run_id": ctx.run_id,
                    "thread_id": ctx.thread_id,
                    "time": now,
                    "payload_version": "v1",
                    "payload": {
                        "status": "succeeded",
                        "answer": answer,
                        "citations": [],
                        "calculations": [],
                        "warnings": [],
                        "errors": [],
                        "export_formats": ["docx"],
                        "project_binding": binding.model_dump(),
                    },
                }
                record = {
                    "version": 2,
                    "owner_user_id": ctx.owner_user_id,
                    "snapshot": {
                        "run_id": ctx.run_id,
                        "status": "succeeded",
                        "answer": answer,
                        "citations": [],
                        "calculations": [],
                        "calc_inputs": {},
                        "material_table": None,
                        "project_calculation": run["result"],
                        "project_binding": binding.model_dump(),
                        "document_blob": blob.model_dump(),
                        "publication_id": publication_id,
                    },
                    "finalize": final,
                }
                validate_snapshot_v2(record)
                if _json(ExportSnapshotStore._sanitize(record)) != _json(record):
                    raise ValueError("PROJECT_OUTPUT_BLOCKED")
                pub = Publication(
                    publication_id=publication_id,
                    **identity,
                    revision=binding.revision,
                    plan_hash=binding.plan_hash,
                    manifest_hash=run["execution_manifest_hash"],
                    release_manifest_hash=binding.release_manifest_hash,
                    snapshot_version=2,
                    snapshot_hash=compute_payload_hash(record),
                    state="prepared",
                    published_at=None,
                )
                state.setdefault("run_publications", {})[ctx.run_id] = pub.model_dump()
                state.setdefault("publication_events", {})[ctx.run_id] = final
                state.setdefault("publication_stream_ends", {})[ctx.run_id] = {
                    **final,
                    "event_id": "3",
                    "type": "stream_end",
                    "payload": {"status": "succeeded"},
                }
                s._save(ctx, state)
                self.fault("prepared")
                self.snapshots.write(record)
                self.fault("snapshot_written")
                # Both persistence writes succeeded; authorization can have changed meanwhile.
                auth = s._auth(ctx)
                s._current(state, ctx, auth)
                s._validate(run)
                s._validate_approval(run, ctx)
                loaded = self.snapshots.read(ctx.run_id)
                if loaded is None or compute_payload_hash(loaded) != pub.snapshot_hash:
                    raise ValueError("PROJECT_PERSISTENCE_FAILED")
                self.documents.validate(run, payload_doc)
                committed = pub.model_copy(
                    update={"state": "committed", "published_at": s.now().isoformat()}
                )
                state["run_publications"][ctx.run_id] = committed.model_dump()
                run["status"] = "published"
                run["updated_at"] = committed.published_at
                s._save(ctx, state)
                self.fault("committed")
        return self.read(ctx)

    def read(self, ctx: ProjectExecutionContext) -> dict:
        s = self.service
        with s._locked(ctx):
            s._auth(ctx)
            state = s._load(ctx)
            run = s._run(state, ctx)
            pub = Publication.model_validate(state.get("run_publications", {}).get(ctx.run_id))
            if pub.state != "committed" or not pub.published_at:
                raise ValueError("PROJECT_PERSISTENCE_FAILED")
            record = self.snapshots.read(ctx.run_id)
            if (
                record is None
                or record["version"] != 2
                or compute_payload_hash(record) != pub.snapshot_hash
            ):
                raise ValueError("PROJECT_PERSISTENCE_FAILED")
            binding = ProjectBinding.model_validate(record["snapshot"]["project_binding"])
            if pub.manifest_hash != run["execution_manifest_hash"] or datetime.fromisoformat(
                pub.published_at
            ) < datetime.fromisoformat(record["finalize"]["time"]):
                raise ValueError("PROJECT_PERSISTENCE_FAILED")

            for key in (
                "project_id",
                "owner_user_id",
                "thread_id",
                "run_id",
                "plan_id",
                "branch_id",
                "revision",
                "generation",
                "plan_hash",
                "release_manifest_hash",
            ):
                if getattr(binding, key) != getattr(pub, key):
                    raise ValueError("PROJECT_PERSISTENCE_FAILED")
            if (
                record["snapshot"]["publication_id"] != pub.publication_id
                or record["snapshot"]["project_calculation"] != run["result"]
                or state["publication_events"].get(ctx.run_id) != record["finalize"]
            ):
                raise ValueError("PROJECT_PERSISTENCE_FAILED")
            s._validate(run)
            approval = run["approval"]
            rec = ApprovalRecord.model_validate(approval["record"], strict=True)
            envelopes = {
                run["active_artifact_ids"][r]: ArtifactEnvelope.model_validate(
                    run["artifacts"][run["active_artifact_ids"][r]], strict=True
                )
                for r in ROLES
            }
            if (
                not rec.approved
                or rec.final_package_hash != binding.release_manifest_hash
                or not validate_approval_binding(rec, envelopes)
                or _json(approval["context"]) != _json(run["context"])
                or approval["release_manifest_hash"] != binding.release_manifest_hash
                or approval["review_artifact_id"] != run["active_artifact_ids"]["review"]
                or approval["approved_by_user_id"] != ctx.owner_user_id
                or approval["plan_hash"] != run["plan_hash"]
                or not rec.expires_at
                or not datetime.fromisoformat(rec.approved_at)
                <= datetime.fromisoformat(pub.published_at)
                < datetime.fromisoformat(rec.expires_at)
            ):
                raise ValueError("PROJECT_PERSISTENCE_FAILED")
            blob = DocumentBlob.model_validate(record["snapshot"]["document_blob"])
            content = self.documents.bytes(blob.document_file_id)
            if (
                hashlib.sha256(content).hexdigest() != blob.content_sha256
                or len(content) != blob.byte_size
            ):
                raise ValueError("PROJECT_PERSISTENCE_FAILED")
            if not self.check_output(record["snapshot"]["answer"]):
                raise ValueError("PROJECT_OUTPUT_BLOCKED")
        self.set_succeeded(ctx)
        return record

    def document(self, ctx: ProjectExecutionContext) -> bytes:
        record = self.read(ctx)
        blob = DocumentBlob.model_validate(record["snapshot"]["document_blob"])
        content = self.documents.bytes(blob.document_file_id)
        if (
            len(content) != blob.byte_size
            or hashlib.sha256(content).hexdigest() != blob.content_sha256
        ):
            raise ValueError("PROJECT_PERSISTENCE_FAILED")
        return content
