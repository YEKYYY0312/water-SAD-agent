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
    segment_id: str | None = None
    kind: Literal["line", "polyline", "text", "mtext"]
    layer: str = Field(min_length=1)
    points: list[tuple[float, float]] = Field(default_factory=list)
    text: str = ""

    @model_validator(mode="after")
    def _check_finite(self) -> PlannedEntity:
        for x, y in self.points:
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError(f"points must be finite, got {(x, y)!r}")
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

    @field_validator("insunits")
    @classmethod
    def _validate_insunits(cls, v: int) -> int:
        if v == 0:
            raise ValueError("insunits must be explicit, 0 (unitless) is forbidden")
        return v


class DrawingPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    plan_artifact_id: str = Field(min_length=1)
    preview_status: str = Field(min_length=1)
    staged_file_id: str | None = None
    final_file_id: str | None = None
    roundtrip_review: dict[str, Any] | None = None


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
    artifact_hashes: list[str] = Field(default_factory=list)
    approved_at: str = Field(min_length=1)

    @field_validator("artifact_hashes")
    @classmethod
    def _validate_hashes(cls, v: list[str]) -> list[str]:
        for h in v:
            if not _PAYLOAD_HASH_RE.match(h):
                raise ValueError(f"artifact_hash must be 64 hex, got {h!r}")
        return v


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
