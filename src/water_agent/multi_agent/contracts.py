"""P4.5 multi-agent contracts: ArtifactEnvelope, ProjectBrief, packages, hashes, error codes."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
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
    artifact_revisions: list[int] | None = None,
    artifact_parent_artifact_ids: list[list[str]] | None = None,
) -> str:
    """Hash the final artifact set using the same canonical JSON rules.

    When ids are available they are paired with their payload hashes.  Runtime
    approval records additionally provide each envelope's revision and parent
    ids, so metadata-only drift cannot silently reuse an approval.  Legacy
    callers may provide only hashes (or ids plus hashes); those forms remain
    ordering-independent for persisted pre-Step-33 records.
    """
    for payload_hash in artifact_hashes:
        if not _PAYLOAD_HASH_RE.match(payload_hash):
            raise ValueError(f"artifact hash must be 64 hex, got {payload_hash!r}")
    if (artifact_revisions is None) != (artifact_parent_artifact_ids is None):
        raise ValueError(
            "artifact_revisions and artifact_parent_artifact_ids must be supplied together"
        )
    if artifact_ids is not None and len(artifact_ids) != len(artifact_hashes):
        raise ValueError("artifact_ids and artifact_hashes must have equal length")
    if artifact_ids is not None:
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("artifact_ids must be unique")
        for artifact_id in artifact_ids:
            if not _ARTIFACT_ID_RE.match(artifact_id):
                raise ValueError(f"artifact_id must match {_ARTIFACT_ID_RE.pattern!r}")
    if artifact_revisions is not None:
        if len(artifact_revisions) != len(artifact_hashes):
            raise ValueError("artifact_revisions and artifact_hashes must have equal length")
        if artifact_parent_artifact_ids is None or len(
            artifact_parent_artifact_ids
        ) != len(artifact_hashes):
            raise ValueError(
                "artifact_parent_artifact_ids and artifact_hashes must have equal length"
            )
        if artifact_ids is None:
            raise ValueError("artifact_ids are required when envelope metadata is supplied")
        for revision in artifact_revisions:
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise ValueError("artifact_revisions must contain positive integers")
        for parent_ids in artifact_parent_artifact_ids:
            for parent_id in parent_ids:
                if not _ARTIFACT_ID_RE.match(parent_id):
                    raise ValueError(
                        f"parent artifact_id must match {_ARTIFACT_ID_RE.pattern!r}"
                    )

    if artifact_ids is not None:
        if artifact_revisions is not None and artifact_parent_artifact_ids is not None:
            items: list[dict[str, Any]] = [
                {
                    "artifact_id": aid,
                    "payload_hash": payload_hash,
                    "revision": revision,
                    "parent_artifact_ids": sorted(parent_ids),
                }
                for aid, payload_hash, revision, parent_ids in zip(
                    artifact_ids,
                    artifact_hashes,
                    artifact_revisions,
                    artifact_parent_artifact_ids,
                )
            ]
        else:
            items = [
                {"artifact_id": aid, "payload_hash": payload_hash}
                for aid, payload_hash in zip(artifact_ids, artifact_hashes)
            ]
        items.sort(key=lambda item: item["artifact_id"])
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
    #: 这条边属于哪个系统（必须在 ``ProjectBrief.systems`` 内）。缺省时只有 systems 恰好
    #: 一个才自动归属；多系统时缺省 = 归属不明，设计 Agent 不做系统 × 拓扑的笛卡尔积。
    system: Literal["water_supply", "drainage", "fire_protection"] | None = None
    #: 这条边由哪一个计算工况支撑（``ProjectBrief.hydraulic_cases[].case_id``，任务书 §2.1
    #: "每个设计管段引用哪一个计算工况"）。缺省时只有恰好一个活动工况才自动绑定；多工况时
    #: 缺省 = 绑定不明，转人工，不把某一个工况的输入套给别的管段。
    calc_case_id: str | None = Field(default=None, min_length=1)

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


def edge_segment_systems(
    brief: ProjectBrief,
    edge: TopologyEdge,
) -> list[Literal["water_supply", "drainage", "fire_protection"]]:
    """Systems a topology edge yields design segments for.

    A declared ``edge.system`` is the only system.  Otherwise the edge belongs to
    the brief's single system, or — with several systems — every system is only
    a candidate whose ownership is unresolved (the design agent never marks such
    a candidate verified).
    """
    if edge.system is not None:
        return [edge.system]
    return list(dict.fromkeys(brief.systems))


def project_brief_conflicts(brief: ProjectBrief) -> list[str]:
    """Cross-field conflicts that make the brief unusable (``MULTI_INVALID_BRIEF``).

    Single-field validation stays in the models.  These checks need the whole
    brief: duplicate identifiers, edges referencing an undeclared system or case,
    and two edges yielding the same ``(system, start, end)`` segment — which
    previously surfaced as a raw ``duplicate segment_id`` ValidationError.
    """
    conflicts: list[str] = []
    for system in sorted({s for s in brief.systems if brief.systems.count(s) > 1}):
        conflicts.append(f"systems: duplicate entry {system!r}")
    case_ids = [case.case_id for case in brief.hydraulic_cases]
    for case_id in sorted({c for c in case_ids if case_ids.count(c) > 1}):
        conflicts.append(f"hydraulic_cases: duplicate case_id {case_id!r}")

    owners: dict[tuple[str, str, str], int] = {}
    for index, edge in enumerate(brief.topology):
        if edge.system is not None and edge.system not in brief.systems:
            conflicts.append(f"topology[{index}].system {edge.system!r} is not in systems")
        if edge.calc_case_id is not None and edge.calc_case_id not in case_ids:
            conflicts.append(
                f"topology[{index}].calc_case_id {edge.calc_case_id!r} "
                "does not match any hydraulic_cases[].case_id"
            )
        for system in edge_segment_systems(brief, edge):
            key = (system, edge.start_node_id, edge.end_node_id)
            first = owners.setdefault(key, index)
            if first != index:
                conflicts.append(
                    f"topology[{first}] and topology[{index}] both define segment "
                    f"({system}, {edge.start_node_id}->{edge.end_node_id})"
                )
    return list(dict.fromkeys(conflicts))


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


class DesignCitation(BaseModel):
    """A design citation with enough context to audit its use later.

    ``source_ids`` remains on each segment for the existing drawing/review
    adapters.  This record retains the validator's version result and the
    exact DesignPackage location without asking those adapters to infer it.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    source_id: str = Field(min_length=1)
    spec_version: str | None = None
    version_status: Literal["current", "superseded", "deprecated", "unknown"] | None = None
    location: str = Field(min_length=1)

    @field_validator("source_id", "location")
    @classmethod
    def _strip_required_text(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("citation field must not be blank")
        return cleaned


class DesignPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    segments: list[DesignPipeSegment] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    recalculation_requests: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_items: list[str] = Field(default_factory=list)
    citations: list[DesignCitation] = Field(default_factory=list)
    invalid_citation_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique(self) -> DesignPackage:
        seen: set[str] = set()
        for s in self.segments:
            if s.segment_id in seen:
                raise ValueError(f"duplicate segment_id {s.segment_id!r}")
            seen.add(s.segment_id)
        for citation in self.citations:
            if not citation.location.startswith("segment:"):
                raise ValueError("design citation location must refer to a segment")
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
    length_m: float | None = None

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

    @field_validator("length_m")
    @classmethod
    def _validate_length_m(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if not math.isfinite(v) or v <= 0:
            raise ValueError("length_m must be finite and >0 when provided")
        return v

    @model_validator(mode="after")
    def _check_finite(self) -> PlannedEntity:
        for x, y in self.points:
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError(f"points must be finite, got {(x, y)!r}")
        if self.segment_id is None:
            if any(
                value is not None
                for value in (self.system, self.dn_mm, self.material, self.length_m)
            ):
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
    staged_content_sha256: str | None = None
    final_file_id: str | None = None
    roundtrip_review: dict[str, Any] | None = None
    staged_file_verified: bool = False
    p3_pipeline_executed: bool = False
    unresolved_items: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_staged_content_binding(self) -> DrawingPackage:
        if self.staged_content_sha256 is not None and not _PAYLOAD_HASH_RE.match(
            self.staged_content_sha256
        ):
            raise ValueError("staged_content_sha256 must be 64 hex")
        if not self.staged_file_verified and self.staged_content_sha256 is not None:
            raise ValueError("unverified staged drawing cannot carry staged_content_sha256")
        return self


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
    #: 带条件进入人审的披露项（任务书 §5.6 "是否允许带条件进入人审由明确规则决定"，
    #: 2026-09-30 定为：P3 图纸规则的覆盖缺口 + 水力越界提示）。不参与 decision，但随
    #: 审查包进入审批请求、在批准前逐条展示，并与批准绑定在同一个审查包哈希上。
    #: 关键未检查项仍在 ``not_checked``，照旧阻止 pass。
    disclosed_limitations: list[str] = Field(default_factory=list)


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    approved: bool
    reason: str = Field(min_length=1)
    artifact_ids: list[str] = Field(default_factory=list)
    artifact_hashes: list[str] = Field(default_factory=list)
    artifact_revisions: list[int] = Field(default_factory=list)
    artifact_parent_artifact_ids: list[list[str]] = Field(default_factory=list)
    artifact_collection_hash: str | None = None
    # Step 33 binds a human decision to the complete release manifest. Keep
    # this optional for pre-Step-33 persisted records; release_gate rejects a
    # record that does not carry it.
    final_package_hash: str | None = None
    approved_at: str = Field(min_length=1)
    expires_at: str | None = None

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
        has_revisions = bool(self.artifact_revisions)
        has_parents = bool(self.artifact_parent_artifact_ids)
        if has_revisions != has_parents:
            raise ValueError(
                "artifact_revisions and artifact_parent_artifact_ids must be supplied together"
            )
        if has_revisions:
            if not self.artifact_ids:
                raise ValueError("artifact_ids are required for envelope metadata binding")
            if len(self.artifact_revisions) != len(self.artifact_ids):
                raise ValueError("artifact_revisions and artifact_ids must have equal length")
            if len(self.artifact_parent_artifact_ids) != len(self.artifact_ids):
                raise ValueError(
                    "artifact_parent_artifact_ids and artifact_ids must have equal length"
                )
        expected = compute_artifact_collection_hash(
            list(self.artifact_hashes),
            artifact_ids=list(self.artifact_ids) if self.artifact_ids else None,
            artifact_revisions=(list(self.artifact_revisions) if has_revisions else None),
            artifact_parent_artifact_ids=(
                [list(parent_ids) for parent_ids in self.artifact_parent_artifact_ids]
                if has_parents
                else None
            ),
        )
        if self.artifact_collection_hash is None:
            self.artifact_collection_hash = expected
        elif self.artifact_collection_hash != expected:
            raise ValueError(f"{MULTI_APPROVAL_STALE}: artifact collection hash mismatch")
        if self.final_package_hash is not None:
            if not _PAYLOAD_HASH_RE.match(self.final_package_hash):
                raise ValueError("final_package_hash must be 64 hex")
            if self.final_package_hash != expected:
                raise ValueError(f"{MULTI_APPROVAL_STALE}: final package hash mismatch")
        for name, value in (("approved_at", self.approved_at), ("expires_at", self.expires_at)):
            if value is None:
                continue
            try:
                datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
        return self


class FinalProjectPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)

    calc_artifact_id: str = Field(min_length=1)
    design_artifact_id: str = Field(min_length=1)
    document_artifact_id: str | None = None
    drawing_plan_artifact_id: str = Field(min_length=1)
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
            self.drawing_plan_artifact_id,
            self.drawing_artifact_id,
            self.review_artifact_id,
        ]
        expected = sorted(item for item in declared if item is not None)
        if sorted(self.approval_record.artifact_ids) != expected:
            raise ValueError(f"{MULTI_APPROVAL_STALE}: approval artifact_ids do not match final package")
        if len(self.approval_record.artifact_revisions) != len(expected) or len(
            self.approval_record.artifact_parent_artifact_ids
        ) != len(expected):
            raise ValueError(
                f"{MULTI_APPROVAL_STALE}: approval envelope metadata binding is required"
            )
        if self.approval_record.final_package_hash is None:
            raise ValueError(f"{MULTI_APPROVAL_STALE}: approval final package hash is required")
        if self.approval_record.final_package_hash != self.release_manifest_hash:
            raise ValueError(f"{MULTI_APPROVAL_STALE}: approval final package hash does not match")
        if self.approval_record.artifact_collection_hash != self.release_manifest_hash:
            raise ValueError(
                f"{MULTI_APPROVAL_STALE}: approval artifact collection hash does not match"
            )
        return self
