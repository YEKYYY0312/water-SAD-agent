"""P4.5 multi-agent contracts: ArtifactEnvelope, ProjectBrief, packages, hashes, error codes."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Error codes (MULTI_* namespace, stable)
# ---------------------------------------------------------------------------

MULTI_INVALID_BRIEF = "MULTI_INVALID_BRIEF"
MULTI_MISSING_INPUTS = "MULTI_MISSING_INPUTS"
MULTI_CALC_BLOCKED = "MULTI_CALC_BLOCKED"
MULTI_DESIGN_CONFLICT = "MULTI_DESIGN_CONFLICT"
MULTI_RECALC_REQUIRED = "MULTI_RECALC_REQUIRED"
MULTI_DRAWING_CONFLICT = "MULTI_DRAWING_CONFLICT"
MULTI_DRAWING_UNAVAILABLE = "MULTI_DRAWING_UNAVAILABLE"
MULTI_REVIEW_REJECTED = "MULTI_REVIEW_REJECTED"
MULTI_RETRY_EXHAUSTED = "MULTI_RETRY_EXHAUSTED"
MULTI_PROVENANCE_INVALID = "MULTI_PROVENANCE_INVALID"
MULTI_APPROVAL_REQUIRED = "MULTI_APPROVAL_REQUIRED"
MULTI_APPROVAL_STALE = "MULTI_APPROVAL_STALE"
MULTI_EXPORT_FAILED = "MULTI_EXPORT_FAILED"

MULTI_ERROR_CODES: frozenset[str] = frozenset(
    {
        MULTI_INVALID_BRIEF,
        MULTI_MISSING_INPUTS,
        MULTI_CALC_BLOCKED,
        MULTI_DESIGN_CONFLICT,
        MULTI_RECALC_REQUIRED,
        MULTI_DRAWING_CONFLICT,
        MULTI_DRAWING_UNAVAILABLE,
        MULTI_REVIEW_REJECTED,
        MULTI_RETRY_EXHAUSTED,
        MULTI_PROVENANCE_INVALID,
        MULTI_APPROVAL_REQUIRED,
        MULTI_APPROVAL_STALE,
        MULTI_EXPORT_FAILED,
    }
)

# ---------------------------------------------------------------------------
# Helpers: finite check, pattern constants, hash
# ---------------------------------------------------------------------------

_PROJECT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_ARTIFACT_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_SEGMENT_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_PAYLOAD_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _check_finite_value(v: Any) -> None:
    if isinstance(v, float) and not math.isfinite(v):
        raise ValueError(f"float value must be finite, got {v!r}")
    if isinstance(v, dict):
        for vv in v.values():
            _check_finite_value(vv)
    elif isinstance(v, (list, tuple)):
        for item in v:
            _check_finite_value(item)


def compute_payload_hash(payload: dict[str, Any]) -> str:
    """Canonical JSON SHA256 hex (sort_keys, separators (',',':'), ensure_ascii=False)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(canonical).hexdigest()


def compute_artifact_collection_hash(
    artifact_hashes: list[str],
    *,
    artifact_ids: list[str] | None = None,
) -> str:
    """Hash the final artifact set using the same canonical JSON rules.

    When ids are available they are paired with their payload hashes, so the
    binding covers both identity and content.  Legacy callers may provide only
    hashes; those are sorted to make the set ordering-independent.
    """
    if artifact_ids is not None:
        if len(artifact_ids) != len(artifact_hashes):
            raise ValueError("artifact_ids and artifact_hashes must have equal length")
        items = sorted(
            ({"artifact_id": aid, "payload_hash": ah} for aid, ah in zip(artifact_ids, artifact_hashes)),
            key=lambda item: item["artifact_id"],
        )
        payload: dict[str, Any] = {"artifacts": items}
    else:
        payload = {"artifact_hashes": sorted(artifact_hashes)}
    return compute_payload_hash(payload)


# ---------------------------------------------------------------------------
# ProjectBrief
# ---------------------------------------------------------------------------


class HydraulicCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    case_id: str = Field(min_length=1)
    formula: str = Field(min_length=1)
    inputs: dict[str, float | str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_finite(self) -> HydraulicCaseRequest:
        _check_finite_value(self.inputs)
        return self


class TopologyEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    start_node_id: str = Field(min_length=1)
    end_node_id: str = Field(min_length=1)
    length_m: float | None = None

    @field_validator("length_m")
    @classmethod
    def _validate_length(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if not math.isfinite(v):
            raise ValueError("length_m must be finite")
        if v <= 0:
            raise ValueError("length_m must be >0 when provided")
        return v


class SpatialConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    kind: str = Field(min_length=1)
    value: str | float

    @field_validator("value")
    @classmethod
    def _validate_value(cls, v: str | float) -> str | float:
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("value must be finite")
        return v


class ProjectBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    project_id: str = Field(min_length=1, max_length=64)
    request: str = Field(min_length=1)
    systems: list[Literal["water_supply", "drainage", "fire_protection"]]
    unit_system: Literal["SI"]
    hydraulic_cases: list[HydraulicCaseRequest] = Field(default_factory=list)
    topology: list[TopologyEdge] = Field(default_factory=list)
    spatial_constraints: list[SpatialConstraint] = Field(default_factory=list)
    required_outputs: list[Literal["calculation_book", "design_note", "drawing"]] = Field(
        default_factory=list
    )
    user_constraints: list[str] = Field(default_factory=list)

    @field_validator("project_id")
    @classmethod
    def _validate_project_id(cls, v: str) -> str:
        if not _PROJECT_ID_RE.match(v):
            raise ValueError(f"project_id must match {_PROJECT_ID_RE.pattern!r}")
        return v


# ---------------------------------------------------------------------------
# ArtifactEnvelope
# ---------------------------------------------------------------------------


class ArtifactEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    artifact_id: str = Field(min_length=1, max_length=64)
    artifact_type: Literal[
        "calc_package", "design_package", "drawing_plan", "drawing_package", "review_package", "document"
    ]
    schema_version: str = Field(min_length=1)
    producer: Literal["hydraulic", "design", "drawing", "review", "orchestrator"]
    revision: int = Field(ge=1)
    parent_artifact_ids: list[str] = Field(default_factory=list)
    payload_hash: str = Field(min_length=64, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    citation_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    status: Literal["ready", "missing_inputs", "blocked", "failed"]

    @field_validator("artifact_id")
    @classmethod
    def _validate_artifact_id(cls, v: str) -> str:
        if not _ARTIFACT_ID_RE.match(v):
            raise ValueError(f"artifact_id must match {_ARTIFACT_ID_RE.pattern!r}")
        return v

    @field_validator("payload_hash")
    @classmethod
    def _validate_hash(cls, v: str) -> str:
        if not _PAYLOAD_HASH_RE.match(v):
            raise ValueError("payload_hash must be 64 hex chars")
        return v

    @model_validator(mode="after")
    def _verify_payload_integrity(self) -> ArtifactEnvelope:
        _check_finite_value(self.payload)
        expected = compute_payload_hash(self.payload)
        if self.payload_hash != expected:
            raise ValueError(
                f"{MULTI_PROVENANCE_INVALID}: payload_hash does not match canonical payload"
            )
        # A model-supplied success status cannot override deterministic
        # validation signals carried by the structured payload.
        if self.status == "ready" and (
            bool(self.payload.get("errors")) or bool(self.payload.get("missing_inputs"))
        ):
            raise ValueError("status=ready conflicts with payload errors or missing_inputs")
        return self


def envelope_hash(envelope: ArtifactEnvelope) -> str:
    """Hash of envelope payload (runtime-computed, never from model)."""
    return compute_payload_hash(envelope.payload)


def verify_envelope_hash(envelope: ArtifactEnvelope) -> bool:
    """Return True iff envelope.payload_hash matches canonical hash of payload."""
    if not _PAYLOAD_HASH_RE.match(envelope.payload_hash):
        return False
    return envelope.payload_hash == compute_payload_hash(envelope.payload)


# ---------------------------------------------------------------------------
# CalcPackage
# ---------------------------------------------------------------------------


class CalcCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    calc_case_id: str = Field(min_length=1)
    formula: str = Field(min_length=1)
    inputs: dict[str, float | str] = Field(default_factory=dict)
    outputs: dict[str, float] = Field(default_factory=dict)
    tool_invocation_id: str = Field(min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    status: Literal["verified", "missing_inputs", "failed"] = "verified"

    @model_validator(mode="after")
    def _check_finite(self) -> CalcCase:
        _check_finite_value(self.inputs)
        _check_finite_value(self.outputs)
        if self.status == "verified" and not self.outputs:
            raise ValueError("verified calc case must contain outputs")
        if self.status != "verified" and self.outputs:
            raise ValueError("failed or missing-input calc case must not contain outputs")
        return self


class CalcPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    cases: list[CalcCase] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    supersedes_artifact_id: str | None = None

    @model_validator(mode="after")
    def _check_unique(self) -> CalcPackage:
        seen: set[str] = set()
        for c in self.cases:
            if c.calc_case_id in seen:
                raise ValueError(f"duplicate calc_case_id {c.calc_case_id!r}")
            seen.add(c.calc_case_id)
        return self


# ---------------------------------------------------------------------------
# DesignPackage
# ---------------------------------------------------------------------------


class DesignPipeSegment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    segment_id: str = Field(min_length=1, max_length=64)
    system: Literal["water_supply", "drainage", "fire_protection"]
    start_node_id: str = Field(min_length=1)
    end_node_id: str = Field(min_length=1)
    length_m: float | None = None
    dn_mm: int | None = None
    material: str | None = None
    slope: float | None = None
    calc_case_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    hydraulic_status: Literal["verified", "not_verified", "recalc_required"]

    @field_validator("segment_id")
    @classmethod
    def _validate_segment_id(cls, v: str) -> str:
        if not _SEGMENT_ID_RE.match(v):
            raise ValueError(f"segment_id must match {_SEGMENT_ID_RE.pattern!r}")
        return v

    @field_validator("length_m")
    @classmethod
    def _validate_length(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if not math.isfinite(v):
            raise ValueError("length_m must be finite")
        if v <= 0:
            raise ValueError("length_m must be >0 when provided")
        return v

    @field_validator("slope")
    @classmethod
    def _validate_slope(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if not math.isfinite(v):
            raise ValueError("slope must be finite")
        return v

    @field_validator("dn_mm")
    @classmethod
    def _validate_dn(cls, v: int | None) -> int | None:
        if v is None:
            return v
        if v <= 0:
            raise ValueError("dn_mm must be >0 when provided")
        return v


class DesignPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    segments: list[DesignPipeSegment] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    recalculation_requests: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique(self) -> DesignPackage:
        seen: set[str] = set()
        for s in self.segments:
            if s.segment_id in seen:
                raise ValueError(f"duplicate segment_id {s.segment_id!r}")
            seen.add(s.segment_id)
        return self


# ---------------------------------------------------------------------------
# DrawingPlan / DrawingPackage
# ---------------------------------------------------------------------------


class PlannedEntity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    entity_id: str = Field(min_length=1)
    #: 图元直接取值的上游产物。P0 图元均来自 DesignPackage，不能由模型自报来源。
    source_artifact_id: str = Field(min_length=1, max_length=64)
    segment_id: str | None = None
    kind: Literal["line", "polyline", "text", "mtext"]
    layer: str = Field(min_length=1)
    points: list[tuple[float, float]] = Field(default_factory=list)
    text: str = ""
    #: 图元级设计快照。对有 segment_id 的图元必须完整，供跨产物逐项对账。
    system: Literal["water_supply", "drainage", "fire_protection"] | None = None
    dn_mm: int | None = None
    material: str | None = None

    @field_validator("source_artifact_id")
    @classmethod
    def _validate_source_artifact_id(cls, v: str) -> str:
        if not _ARTIFACT_ID_RE.match(v):
            raise ValueError(f"source_artifact_id must match {_ARTIFACT_ID_RE.pattern!r}")
        return v

    @field_validator("dn_mm")
    @classmethod
    def _validate_dn(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("dn_mm must be >0 when provided")
        return v

    @model_validator(mode="after")
    def _check_finite(self) -> PlannedEntity:
        for x, y in self.points:
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError(f"points must be finite, got {(x, y)!r}")
        if self.segment_id is None:
            if any(value is not None for value in (self.system, self.dn_mm, self.material)):
                raise ValueError("entities without segment_id cannot carry segment design fields")
        elif self.system is None or self.dn_mm is None or not self.material:
            raise ValueError(
                "entities with segment_id require system, dn_mm and material from the source artifact"
            )
        return self


class DrawingPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    drawing_id: str = Field(min_length=1)
    drawing_class: Literal["schematic", "project_coordinates"]
    insunits: int
    entities: list[PlannedEntity] = Field(default_factory=list)
    source_design_artifact_id: str = Field(min_length=1)
    source_calc_artifact_id: str = Field(min_length=1)
    integration_conflicts: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    #: Drawing Agent 复用既有 docgen/materials 的真实输出或结构化受阻信息。
    output_adaptations: dict[str, Any] = Field(default_factory=dict)

    @field_validator("insunits")
    @classmethod
    def _validate_insunits(cls, v: int) -> int:
        if v == 0:
            raise ValueError("insunits must be explicit, 0 (unitless) is forbidden")
        return v

    @model_validator(mode="after")
    def _validate_entity_sources(self) -> DrawingPlan:
        for entity in self.entities:
            if entity.source_artifact_id != self.source_design_artifact_id:
                raise ValueError(
                    "PlannedEntity.source_artifact_id must equal "
                    "DrawingPlan.source_design_artifact_id"
                )
        return self


class DrawingPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    plan_artifact_id: str = Field(min_length=1)
    preview_status: str = Field(min_length=1)
    staged_file_id: str | None = None
    final_file_id: str | None = None
    roundtrip_review: dict[str, Any] | None = None
    staged_file_verified: bool = False
    p3_pipeline_executed: bool = False
    unresolved_items: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# ReviewPackage / ApprovalRecord / FinalProjectPackage
# ---------------------------------------------------------------------------


class ReviewIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    issue_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    severity: Literal["info", "warning", "error", "blocking"]
    owner: Literal["hydraulic", "design", "drawing", "human"]
    artifact_id: str = Field(min_length=1)
    object_ref: str | None = None
    message: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class ReviewPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    decision: Literal["pass", "revise", "blocked"]
    issues: list[ReviewIssue] = Field(default_factory=list)
    not_checked: list[str] = Field(default_factory=list)
    reviewed_artifact_hashes: list[str] = Field(default_factory=list)


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    approved: bool
    reason: str = Field(min_length=1)
    artifact_ids: list[str] = Field(default_factory=list)
    artifact_hashes: list[str] = Field(default_factory=list)
    artifact_collection_hash: str | None = None
    approved_at: str = Field(min_length=1)

    @field_validator("artifact_ids")
    @classmethod
    def _validate_artifact_ids(cls, v: list[str]) -> list[str]:
        for artifact_id in v:
            if not _ARTIFACT_ID_RE.match(artifact_id):
                raise ValueError(f"artifact_id must match {_ARTIFACT_ID_RE.pattern!r}")
        return v

    @field_validator("artifact_hashes")
    @classmethod
    def _validate_hashes(cls, v: list[str]) -> list[str]:
        for h in v:
            if not _PAYLOAD_HASH_RE.match(h):
                raise ValueError(f"artifact_hash must be 64 hex, got {h!r}")
        return v

    @model_validator(mode="after")
    def _bind_collection_hash(self) -> ApprovalRecord:
        if self.artifact_ids and len(self.artifact_ids) != len(self.artifact_hashes):
            raise ValueError("artifact_ids and artifact_hashes must have equal length")
        if len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise ValueError("artifact_ids must be unique")
        expected = compute_artifact_collection_hash(
            list(self.artifact_hashes),
            artifact_ids=list(self.artifact_ids) if self.artifact_ids else None,
        )
        if self.artifact_collection_hash is None:
            self.artifact_collection_hash = expected
        elif self.artifact_collection_hash != expected:
            raise ValueError(f"{MULTI_APPROVAL_STALE}: artifact collection hash mismatch")
        return self


class FinalProjectPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    calc_artifact_id: str = Field(min_length=1)
    design_artifact_id: str = Field(min_length=1)
    document_artifact_id: str | None = None
    drawing_artifact_id: str | None = None
    review_artifact_id: str = Field(min_length=1)
    approval_record: ApprovalRecord
    release_manifest_hash: str = Field(min_length=64, max_length=64)

    @field_validator("release_manifest_hash")
    @classmethod
    def _validate_hash(cls, v: str) -> str:
        if not _PAYLOAD_HASH_RE.match(v):
            raise ValueError("release_manifest_hash must be 64 hex")
        return v

    @model_validator(mode="after")
    def _validate_approval_ids(self) -> FinalProjectPackage:
        declared = [
            self.calc_artifact_id,
            self.design_artifact_id,
            self.document_artifact_id,
            self.drawing_artifact_id,
            self.review_artifact_id,
        ]
        expected = sorted(item for item in declared if item is not None)
        if self.approval_record.artifact_ids and sorted(self.approval_record.artifact_ids) != expected:
            raise ValueError(f"{MULTI_APPROVAL_STALE}: approval artifact_ids do not match final package")
        return self
