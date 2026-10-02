"""Step 30 design agent — deterministic, offline, no hydraulic compute."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Literal, cast

from pydantic import ValidationError

from water_agent.domain.citation import CitationClaim, enrich_claims, validate_citations
from water_agent.multi_agent.contracts import (
    ArtifactEnvelope,
    CalcCase,
    CalcPackage,
    DesignCitation,
    DesignPackage,
    DesignPipeSegment,
    ProjectBrief,
    TopologyEdge,
    compute_payload_hash,
    edge_segment_systems,
    project_brief_conflicts,
)

# 2026-10-01：管段按 TopologyEdge.system / calc_case_id 绑定单一工况（不再全连接）。
_SCHEMA_VERSION = "p4.5-2026-10-01"

_DN_TOKEN_RE = re.compile(r"DN\s*(\d+)", re.IGNORECASE)
#: Hydraulic Agent 重算时派生的工况 ID 形如 ``<case_id>-<12 位 hex>``。
_DERIVED_CASE_SUFFIX_RE = re.compile(r"-[0-9a-f]{12}")


def _stable_segment_id(project_id: str, start: str, end: str, system: str) -> str:
    raw = f"{project_id}:{start}:{end}:{system}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"seg-{h}"


def _explicit_dn_values(brief: ProjectBrief) -> list[int]:
    """Explicit design DN constraints — the only DN source besides a segment's own case.

    Structured ``spatial_constraints`` (``diameter_mm``/``dn_mm``) outrank DN tokens
    parsed from free-text ``user_constraints`` (the pre-existing precedence: a
    structured diameter revision supersedes a restated ``DN150``).  Several
    distinct values from the winning source are returned as-is; the caller
    decides whether they can be attributed.
    """
    spatial: list[int] = []
    for constraint in brief.spatial_constraints:
        if constraint.kind.lower() not in {"diameter_mm", "dn_mm"}:
            continue
        try:
            value = float(constraint.value)
        except (TypeError, ValueError):
            continue
        if value > 0 and value.is_integer():
            spatial.append(int(value))
    if spatial:
        return list(dict.fromkeys(spatial))
    user: list[int] = []
    for uc in brief.user_constraints:
        for match in _DN_TOKEN_RE.finditer(uc):
            dn = int(match.group(1))
            if dn > 0:
                user.append(dn)
    return list(dict.fromkeys(user))


def _case_dn(case: CalcCase) -> int | None:
    """DN implied by one calc case's own diameter input — never another case's."""
    for key in ("diameter_mm", "diameter"):
        raw = case.inputs.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        dn = int(value)
        if dn > 0:
            return dn
    return None


def _case_slope(case: CalcCase | None) -> float | None:
    """Slope of the segment's own bound calc case — never another case's.

    The programmatic result (``outputs.slope``, e.g. ``slope_manning``) comes first; a
    slope given as a case input is used only when the formula does not compute one.
    Previously only the input was read, so a drainage case whose slope is the
    calculated result was reported as "missing slope" (review 2026-09-30).
    """
    if case is None:
        return None
    for raw in (case.outputs.get("slope"), case.inputs.get("slope")):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        value = float(raw)
        if math.isfinite(value):
            return value
    return None


def _resolve_declared_case(
    declared: str,
    brief: ProjectBrief,
    active_cases: list[CalcCase],
) -> CalcCase | None:
    """Map ``TopologyEdge.calc_case_id`` (a brief case_id) to its active calc case.

    The first run keeps the brief ID; a recalculation derives
    ``<case_id>-<12 hex>``.  Anything other than exactly one match is unresolved.
    """
    formula = next((case.formula for case in brief.hydraulic_cases if case.case_id == declared), None)
    matches = [
        case
        for case in active_cases
        if (
            case.calc_case_id == declared
            or (
                case.calc_case_id.startswith(declared)
                and _DERIVED_CASE_SUFFIX_RE.fullmatch(case.calc_case_id[len(declared):]) is not None
            )
        )
        and (formula is None or case.formula == formula)
    ]
    return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class _CaseBinding:
    case: CalcCase | None
    note: str | None = None
    #: 绑定只能由人补声明（多个活动工况而边未声明）——转人工，不是计算缺失。
    awaits_human: bool = False


def _bind_case(
    brief: ProjectBrief,
    edge: TopologyEdge,
    index: int,
    active_cases: list[CalcCase],
) -> _CaseBinding:
    if edge.calc_case_id is not None:
        case = _resolve_declared_case(edge.calc_case_id, brief, active_cases)
        if case is None:
            return _CaseBinding(
                None,
                f"topology[{index}].calc_case_id {edge.calc_case_id!r} has no active verified calc result",
            )
        return _CaseBinding(case)
    if len(active_cases) == 1:
        return _CaseBinding(active_cases[0])
    if not active_cases:
        return _CaseBinding(None)
    ids = ", ".join(case.calc_case_id for case in active_cases)
    return _CaseBinding(
        None,
        (
            f"topology[{index}] ({edge.start_node_id}->{edge.end_node_id}) declares no calc_case_id "
            f"and {len(active_cases)} active calc cases exist ({ids}); "
            f"declare topology[{index}].calc_case_id"
        ),
        awaits_human=True,
    )


@dataclass
class _DesignBuild:
    package: DesignPackage
    #: not_verified 只因"需要人补声明"（系统/工况归属、显式 DN 归属）的管段 → missing_inputs。
    awaiting_human: set[str] = dataclass_field(default_factory=set)
    brief_conflicts: list[str] = dataclass_field(default_factory=list)
    #: 引用校验器给出的版本提示（只标记、不判失败），进设计 envelope 的 warnings。
    citation_warnings: list[str] = dataclass_field(default_factory=list)


def _infer_material(brief: ProjectBrief) -> str | None:
    for sc in brief.spatial_constraints:
        if sc.kind.lower() == "material" and isinstance(sc.value, str) and sc.value.strip():
            return sc.value.strip()
    for uc in brief.user_constraints:
        low = uc.lower()
        if "material" in low:
            if ":" in uc:
                part = uc.split(":", 1)[1].strip()
                if part:
                    return part
            if "=" in uc:
                part = uc.split("=", 1)[1].strip()
                if part:
                    return part
    return None


def _requested_source_ids(brief: ProjectBrief) -> list[str]:
    """Read user citation requests without treating them as trusted citations."""
    requested: list[str] = []
    for uc in brief.user_constraints:
        if uc.strip().lower().startswith("source_id:"):
            sid = uc.split(":", 1)[1].strip()
            if sid:
                requested.append(sid)
        elif uc.strip().lower().startswith("source_ids:"):
            rest = uc.split(":", 1)[1].strip()
            for part in re.split(r"[;,]", rest):
                p = part.strip()
                if p:
                    requested.append(p)
    return list(dict.fromkeys(requested))


_VERSION_STATUSES = frozenset({"current", "superseded", "deprecated", "unknown"})


@dataclass(frozen=True)
class _CitationSelection:
    #: 进设计依据的 source_id（已对照检索集合校验），顺序即管段 source_ids 的顺序。
    source_ids: list[str]
    #: 每个入选 source_id 带版本状态的 claim（版本信息来自 validate_citations）。
    claims: dict[str, CitationClaim]
    #: 被请求、但不在检索集合里的 source_id —— 不进任何管段。
    invalid: list[str]
    #: 版本无法确认 / 非现行的提示（只标记、不判失败，给人看）。
    warnings: list[str]


def _select_citations(
    brief: ProjectBrief,
    retrieved_source_ids: list[str] | None,
    validated_citations: list[CitationClaim] | None,
    *,
    version_registry: Any | None,
) -> _CitationSelection:
    """被引候选对照真实检索到的集合校验，不再自证（review 2026-09-30）。

    此前 ``validate_citations(candidates, candidates)``：被引集合就是检索集合本身，invalid 恒为空；
    编排器也不传版本注册表，版本状态恒为 None。现在：

    - 检索集合 = 本轮 ``retrieved_source_ids`` ∪ 引用校验器已交入的 claims；
    - 被引候选 = 用户请求的 source_id（user_constraints 里只是请求，不是可信来源），没有请求时取
      检索集合全体；
    - 两者交给 ``validate_citations``：不在检索集合里的候选进 invalid、不进任何管段；传入版本注册表
      时解析版本状态，无法确认现行的只标记（warnings），不判失败。
    """
    retrieved: dict[str, CitationClaim] = {}
    for source_id in retrieved_source_ids or []:
        cleaned = source_id.strip()
        if cleaned:
            retrieved[cleaned] = CitationClaim(source_id=cleaned, declared_by="system")
    for claim in validated_citations or []:
        cleaned = claim.source_id.strip()
        if cleaned:
            retrieved[cleaned] = claim.model_copy(update={"source_id": cleaned})

    cited = _requested_source_ids(brief) or sorted(retrieved)
    if not cited:
        return _CitationSelection(source_ids=[], claims={}, invalid=[], warnings=[])
    check = validate_citations(cited, retrieved, version_registry=version_registry)
    invalid = set(check.invalid)
    selected = [source_id for source_id in cited if source_id not in invalid]
    claims = {
        claim.source_id: claim
        for claim in enrich_claims((retrieved[source_id] for source_id in selected), check)
    }
    return _CitationSelection(
        source_ids=selected,
        claims=claims,
        invalid=list(check.invalid),
        warnings=list(check.warnings),
    )


def _citation_version_status(
    claim: CitationClaim,
) -> Literal["current", "superseded", "deprecated", "unknown"] | None:
    """未经注册表校验保持 None（审查会如实披露）；注册表给出契约外的取值一律按 unknown。"""
    status = claim.version_status
    if status is None:
        return None
    if status not in _VERSION_STATUSES:
        return "unknown"
    return cast(Literal["current", "superseded", "deprecated", "unknown"], status)


def _infer_length(edge: TopologyEdge) -> float | None:
    return edge.length_m


def _suggested_formula(fields: set[str]) -> str:
    if fields & {"diameter_mm", "flow_lps", "velocity_mps"}:
        return "velocity"
    return "head_loss_darcy_weisbach"


def _add_recalculation_request(
    requests: list[dict[str, Any]],
    *,
    segment_id: str,
    reason: str,
    fields: set[str],
    formula: str | None = None,
    source_calc_case_id: str | None = None,
    input_updates: dict[str, float | str] | None = None,
) -> None:
    cleaned_fields = sorted({field.strip() for field in fields if field.strip()})
    if not cleaned_fields:
        return
    request: dict[str, Any] = {
        "reason": reason,
        "fields": cleaned_fields,
        "segment_id": segment_id,
    }
    if formula is not None and source_calc_case_id is not None and input_updates:
        request.update(
            {
                "formula": formula,
                "source_calc_case_id": source_calc_case_id,
                "input_updates": dict(input_updates),
            }
        )
    else:
        request["suggested_formula"] = _suggested_formula(set(cleaned_fields))
    if request not in requests:
        requests.append(request)


def build_design_package(
    brief: ProjectBrief,
    calc_package: CalcPackage,
    *,
    retrieved_source_ids: list[str] | None = None,
    validated_citations: list[CitationClaim] | None = None,
    version_registry: Any | None = None,
    prev_design: DesignPackage | None = None,
    active_calc_case_ids: set[str] | None = None,
    recalc_trigger_fields: set[str] | None = None,
) -> DesignPackage:
    return _build_design_package(
        brief,
        calc_package,
        retrieved_source_ids=retrieved_source_ids,
        validated_citations=validated_citations,
        version_registry=version_registry,
        prev_design=prev_design,
        active_calc_case_ids=active_calc_case_ids,
        recalc_trigger_fields=recalc_trigger_fields,
    ).package


def _build_design_package(
    brief: ProjectBrief,
    calc_package: CalcPackage,
    *,
    retrieved_source_ids: list[str] | None,
    validated_citations: list[CitationClaim] | None,
    version_registry: Any | None,
    prev_design: DesignPackage | None,
    active_calc_case_ids: set[str] | None,
    recalc_trigger_fields: set[str] | None,
) -> _DesignBuild:
    # Failed/missing cases remain in CalcPackage for provenance, but cannot
    # support a verified design segment.
    verified_case_ids = (
        {
            c.calc_case_id
            for c in calc_package.cases
            if c.status == "verified" and c.outputs
        }
        if not calc_package.missing_inputs and not calc_package.errors
        else set()
    )
    active_ids = verified_case_ids if active_calc_case_ids is None else verified_case_ids & active_calc_case_ids
    citation_selection = _select_citations(
        brief, retrieved_source_ids, validated_citations, version_registry=version_registry
    )
    inferred_source_ids = citation_selection.source_ids
    invalid_citation_ids = citation_selection.invalid

    material = _infer_material(brief)
    explicit_dns = _explicit_dn_values(brief)

    segments: list[DesignPipeSegment] = []
    unresolved: list[str] = []
    recalc_requests: list[dict[str, Any]] = []
    awaiting_human: set[str] = set()

    if not brief.topology:
        unresolved.append("missing topology: no edges, cannot derive segments")

    systems = brief.systems
    if not systems:
        unresolved.append("missing system: brief.systems empty")
    systems_for_gen = list(systems)

    conflicts = project_brief_conflicts(brief)
    if conflicts:
        # The orchestrator rejects these at intake (MULTI_INVALID_BRIEF).  A direct
        # caller gets no segments either: keeping one of two conflicting edges
        # would be a guess, and duplicate segment IDs cannot form a package.
        unresolved.extend(f"invalid project brief: {conflict}" for conflict in conflicts)
        package = DesignPackage(
            segments=[],
            decisions=[],
            alternatives=[],
            recalculation_requests=[],
            unresolved_items=list(dict.fromkeys(unresolved)),
            citations=[],
            invalid_citation_ids=invalid_citation_ids,
        )
        return _DesignBuild(package, brief_conflicts=conflicts)

    # 每段管只引用它绑定的那一个工况；DN 只取该工况自己的输入，或能唯一归属的显式设计
    # 约束。单工况设计里唯一的显式 DN 就是设计采用值（与工况不同 → 重算，§7.3）；多工况
    # 设计里项目级 DN 归属不到某一个工况，只做一致性核对，不一致就转人工。
    active_cases = [case for case in calc_package.cases if case.calc_case_id in active_ids]
    single_case_design = len(active_cases) <= 1
    adopted_dn = explicit_dns[0] if single_case_design and len(explicit_dns) == 1 else None
    explicit_label = ", ".join(f"DN{value}" for value in explicit_dns)

    for index, edge in enumerate(brief.topology):
        edge_systems = edge_segment_systems(brief, edge)
        system_unresolved = edge.system is None and len(edge_systems) > 1
        binding = _bind_case(brief, edge, index, active_cases)
        case = binding.case
        case_dn = _case_dn(case) if case is not None else None
        for sys_val in edge_systems:
            sid = _stable_segment_id(brief.project_id, edge.start_node_id, edge.end_node_id, sys_val)
            length_m = _infer_length(edge)
            slope = _case_slope(case)

            # Items only a human can settle.  Such a segment is never verified and
            # never emits a recalculation that would alter a user-supplied case.
            human_notes: list[str] = []
            if system_unresolved:
                human_notes.append(
                    f"topology[{index}] ({edge.start_node_id}->{edge.end_node_id}) declares no system "
                    f"and brief.systems lists {len(edge_systems)} systems ({', '.join(edge_systems)}); "
                    f"declare topology[{index}].system"
                )
            if binding.awaits_human and binding.note is not None:
                human_notes.append(binding.note)

            dn_mm: int | None = case_dn
            recalc_dn: int | None = None
            if adopted_dn is not None:
                dn_mm = adopted_dn
                if case is not None and case_dn is not None and case_dn != adopted_dn:
                    if "diameter_mm" in case.inputs:
                        recalc_dn = adopted_dn
                    else:
                        human_notes.append(
                            f"explicit DN{adopted_dn} differs from calc case {case.calc_case_id} "
                            f"diameter {case_dn}, which has no diameter_mm input to recalculate"
                        )
            elif explicit_dns and single_case_design:
                human_notes.append(
                    f"explicit DN constraints disagree ({explicit_label}); cannot choose the design DN"
                )
            elif explicit_dns and case is not None and case_dn not in explicit_dns:
                human_notes.append(
                    f"project-level DN constraint ({explicit_label}) cannot be attributed to this "
                    f"segment: bound calc case {case.calc_case_id} has diameter "
                    f"{case_dn if case_dn is not None else 'absent'}, and with {len(active_cases)} "
                    "active calc cases a project-level DN is not imposed on another case"
                )

            seg_unresolved: list[str] = []
            if material is None:
                seg_unresolved.append(f"segment {sid}: missing material")
            if dn_mm is None:
                seg_unresolved.append(f"segment {sid}: missing dn_mm")
            if length_m is None:
                seg_unresolved.append(f"segment {sid}: missing length_m")

            # fire_protection device unresolved per Q3
            if sys_val == "fire_protection":
                seg_unresolved.append(f"segment {sid}: fire hazard level / sprinkler selection unresolved")

            # A segment is verified only when it is bound to exactly one active,
            # successful calc case and the selected design values match its inputs.
            hydraulic_status: Literal["verified", "not_verified", "recalc_required"]
            if case is None or human_notes:
                hydraulic_status = "not_verified"
            elif recalc_dn is not None:
                hydraulic_status = "recalc_required"
                _add_recalculation_request(
                    recalc_requests,
                    segment_id=sid,
                    reason="hydraulic_input_changed",
                    fields={"diameter_mm"},
                    formula=case.formula,
                    source_calc_case_id=case.calc_case_id,
                    input_updates={"diameter_mm": float(recalc_dn)},
                )
            else:
                hydraulic_status = "verified"
            if human_notes and (case is not None or binding.awaits_human):
                awaiting_human.add(sid)

            # unresolved for slope when drainage and slope missing
            if sys_val == "drainage" and slope is None:
                unresolved.append(f"segment {sid}: missing slope for drainage")

            for u in seg_unresolved:
                if u not in unresolved:
                    unresolved.append(u)
            if binding.note is not None and not binding.awaits_human:
                unresolved.append(f"segment {sid}: {binding.note}")
            unresolved.extend(f"segment {sid}: {note}" for note in human_notes)

            segments.append(
                DesignPipeSegment(
                    segment_id=sid,
                    system=sys_val,
                    start_node_id=edge.start_node_id,
                    end_node_id=edge.end_node_id,
                    length_m=length_m,
                    dn_mm=dn_mm,
                    material=material,
                    slope=slope,
                    calc_case_ids=[case.calc_case_id] if case is not None else [],
                    source_ids=list(inferred_source_ids),
                    hydraulic_status=hydraulic_status,
                )
            )

    if explicit_dns and not single_case_design:
        attributed = {seg.dn_mm for seg in segments if seg.calc_case_ids}
        unresolved.extend(
            f"explicit DN{value} constraint matches no segment's calc case; "
            "a project-level DN is not imposed on a multi-case design"
            for value in explicit_dns
            if value not in attributed
        )

    # recalc detection vs prev_design
    if prev_design is not None and segments:
        prev_map = {s.segment_id: s for s in prev_design.segments}
        for seg in segments:
            old = prev_map.get(seg.segment_id)
            if old is None:
                _add_recalculation_request(
                    recalc_requests,
                    segment_id=seg.segment_id,
                    reason="topology_changed",
                    fields={"topology"},
                )
                if seg.hydraulic_status == "verified":
                    seg.hydraulic_status = "recalc_required"
                continue
            fields: dict[str, float | str | None] = {}
            if old.dn_mm != seg.dn_mm:
                fields["diameter_mm"] = float(seg.dn_mm) if seg.dn_mm is not None else None
            if old.length_m != seg.length_m:
                fields["length_m"] = seg.length_m
            if old.slope != seg.slope:
                fields["slope"] = seg.slope
            if fields:
                existing_fields = {
                    field
                    for request in recalc_requests
                    if request.get("segment_id") == seg.segment_id
                    for field in request.get("fields", [])
                    if isinstance(field, str)
                }
                pending_fields = set(fields) - existing_fields
                if pending_fields:
                    source_cases = [
                        case
                        for case in calc_package.cases
                        if case.calc_case_id in seg.calc_case_ids
                    ]
                    updates = {
                        field: value
                        for field, value in fields.items()
                        if field in pending_fields and value is not None
                    }
                    # A segment still awaiting a human declaration never gets an
                    # automatic, source-bound update of a user-supplied case.
                    if len(source_cases) == 1 and updates and seg.segment_id not in awaiting_human:
                        source = source_cases[0]
                        _add_recalculation_request(
                            recalc_requests,
                            segment_id=seg.segment_id,
                            reason="hydraulic_input_changed",
                            fields=pending_fields,
                            formula=source.formula,
                            source_calc_case_id=source.calc_case_id,
                            input_updates=updates,
                        )
                    else:
                        _add_recalculation_request(
                            recalc_requests,
                            segment_id=seg.segment_id,
                            reason="hydraulic_input_changed",
                            fields=pending_fields,
                        )
                if seg.hydraulic_status == "verified":
                    seg.hydraulic_status = "recalc_required"

    # Inputs newly supplied by a design revision (for example flow, roughness
    # or a revised slope) have no safe implicit mapping to an old calc result.
    explicit_recalc_fields = {
        field.strip() for field in recalc_trigger_fields or set() if field.strip()
    }
    if explicit_recalc_fields:
        for seg in segments:
            _add_recalculation_request(
                recalc_requests,
                segment_id=seg.segment_id,
                reason="hydraulic_input_changed",
                fields=explicit_recalc_fields,
            )
            if seg.hydraulic_status == "verified":
                seg.hydraulic_status = "recalc_required"

    decisions: list[str] = []
    alternatives: list[str] = []
    if segments:
        decisions.append(f"design for systems {','.join(systems_for_gen)} based on calc cases {sorted(active_ids)}")
        alternatives.append("alternative: keep current diameter vs upsized diameter (requires recalc)")

    # ensure unresolved dedup preserve order
    seen_u: set[str] = set()
    dedup_u: list[str] = []
    for u in unresolved:
        if u not in seen_u:
            seen_u.add(u)
            dedup_u.append(u)

    citations = [
        DesignCitation(
            source_id=source_id,
            spec_version=citation_selection.claims[source_id].spec_version,
            version_status=_citation_version_status(citation_selection.claims[source_id]),
            location=f"segment:{segment.segment_id}:source_ids",
        )
        for segment in segments
        for source_id in segment.source_ids
    ]

    pkg = DesignPackage(
        segments=segments,
        decisions=decisions,
        alternatives=alternatives,
        recalculation_requests=recalc_requests,
        unresolved_items=dedup_u,
        citations=citations,
        invalid_citation_ids=invalid_citation_ids,
    )
    return _DesignBuild(
        pkg,
        awaiting_human=awaiting_human,
        citation_warnings=citation_selection.warnings,
    )


def run_design_agent(
    brief: ProjectBrief,
    calc_envelope: ArtifactEnvelope,
    *,
    retrieved_source_ids: list[str] | None = None,
    validated_citations: list[CitationClaim] | None = None,
    version_registry: Any | None = None,
    prev_design: DesignPackage | None = None,
    recalc_trigger_fields: set[str] | None = None,
    active_calc_case_ids: set[str] | None = None,
) -> ArtifactEnvelope:
    try:
        calc_pkg = CalcPackage.model_validate(calc_envelope.payload)
    except ValidationError as e:
        payload = DesignPackage(
            segments=[],
            decisions=[],
            alternatives=[],
            recalculation_requests=[],
            unresolved_items=[f"calc payload invalid: {e}"],
        ).model_dump()
        ph = compute_payload_hash(payload)
        raw = f"{brief.project_id}:{calc_envelope.artifact_id}:{ph}"
        aid = f"design-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"
        return ArtifactEnvelope(
            artifact_id=aid,
            artifact_type="design_package",
            schema_version=_SCHEMA_VERSION,
            producer="design",
            revision=2 if prev_design is not None else 1,
            parent_artifact_ids=[calc_envelope.artifact_id],
            payload_hash=ph,
            payload=payload,
            citation_ids=[],
            assumptions=[],
            warnings=["calc payload invalid"],
            status="blocked",
        )

    # A successful case is usable only from an active, successful CalcPackage.
    # Package-level failure/missing signals prevent an unrelated-looking case
    # from lending a false "verified" label to a design segment.
    current_case_ids: set[str] = set()
    if (
        calc_envelope.artifact_type == "calc_package"
        and calc_envelope.status == "ready"
        and not calc_pkg.missing_inputs
        and not calc_pkg.errors
    ):
        current_case_ids = {
            case.calc_case_id
            for case in calc_pkg.cases
            if case.status == "verified" and case.outputs
        }
    if active_calc_case_ids is not None:
        current_case_ids &= active_calc_case_ids

    build = _build_design_package(
        brief,
        calc_pkg,
        retrieved_source_ids=retrieved_source_ids,
        validated_citations=validated_citations,
        version_registry=version_registry,
        prev_design=prev_design,
        active_calc_case_ids=current_case_ids,
        recalc_trigger_fields=recalc_trigger_fields,
    )
    design_pkg = build.package

    payload = design_pkg.model_dump()
    payload_hash = compute_payload_hash(payload)
    raw = f"{brief.project_id}:{calc_envelope.artifact_id}:{payload_hash}"
    artifact_id = f"design-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"
    revision = 2 if prev_design is not None else 1

    has_brief_conflict = bool(build.brief_conflicts)
    has_invalid_source = bool(design_pkg.invalid_citation_ids)
    not_verified_ids = {
        s.segment_id for s in design_pkg.segments if s.hydraulic_status == "not_verified"
    }
    has_not_verified = bool(not_verified_ids)
    # not_verified only because a human must declare the system / calc case /
    # DN attribution is a missing input (转人工); any other not_verified segment
    # (no active calc support at all) still blocks.
    blocking_not_verified = bool(not_verified_ids - build.awaiting_human)
    has_recalculation = bool(design_pkg.recalculation_requests) or any(
        s.hydraulic_status == "recalc_required" for s in design_pkg.segments
    )
    has_unresolved = len(design_pkg.unresolved_items) > 0

    warnings: list[str] = []
    if has_brief_conflict:
        warnings.append("invalid project brief: " + "; ".join(build.brief_conflicts))
    if has_invalid_source:
        warnings.append(
            "invalid citation source_id rejected: "
            + ", ".join(design_pkg.invalid_citation_ids)
        )
    # 版本提示与零依据只标记、不改状态：由审查包披露给批准人（任务书 §7.3）。
    warnings.extend(build.citation_warnings)
    if design_pkg.segments and not design_pkg.citations:
        warnings.append("design basis has no retrieved or validator-confirmed citation")
    if has_not_verified:
        warnings.append("hydraulic not_verified present")
    if has_recalculation:
        warnings.append("recalculation_requests pending")
    if has_unresolved:
        warnings.append("unresolved_items present")

    if has_brief_conflict or has_invalid_source or has_recalculation or blocking_not_verified:
        status: str = "blocked"
    elif has_unresolved and any("missing" in u for u in design_pkg.unresolved_items):
        # missing required inputs -> missing_inputs
        # but if topology missing etc, treat as missing_inputs
        status = "missing_inputs"
    elif has_unresolved:
        status = "missing_inputs"
    else:
        status = "ready"

    if not design_pkg.segments and not brief.topology and not has_brief_conflict:
        status = "missing_inputs"

    # provenance invalid marker is via status blocked
    envelope = ArtifactEnvelope(
        artifact_id=artifact_id,
        artifact_type="design_package",
        schema_version=_SCHEMA_VERSION,
        producer="design",
        revision=revision,
        parent_artifact_ids=[calc_envelope.artifact_id],
        payload_hash=payload_hash,
        payload=payload,
        citation_ids=sorted({citation.source_id for citation in design_pkg.citations}),
        assumptions=[],
        warnings=warnings,
        status=status,  # type: ignore[arg-type]
    )
    return envelope
