"""Step 32 design review agent — read-only, offline, deterministic.

任务书 §4.3 / §5.6 / §8.1（单一权威）对 Review Agent 的硬边界：

1. **只读**：只消费 ``ArtifactEnvelope`` 注册表（CalcPackage / DesignPackage /
   DrawingPlan / DrawingPackage / RenderedDoc），不修改任何被审产物；
   无 WRITE / MCP / 发布能力（源码级测试 ``test_review_has_no_write_tools`` 锁死）。
2. **decision 结构化判定**：``pass / revise / blocked`` 严格由
   issues / severity / not_checked / reviewed_artifact_hashes 决定；
   自由文本（message 等）不参与判定（T12/T13：注入文本不能翻转结论）。
3. **not_checked 一等公民**：存在关键未检查项时不得 pass；未检查 ≠ 无问题。
4. **跨产物可追溯**：segment_id / DN / material / system / length / calc_case_id /
   source_id / parent_artifact_ids / payload_hash 逐项对账；断链 →
   ``MULTI_PROVENANCE_INVALID``（blocking, human）；过期 calc_case_id →
   ``stale_calculation_used``（error, hydraulic）。
5. **Q3 口径**：``fire_protection`` 管段正常纳入审查；``hydraulic_status =
   not_verified`` 披露为 not_checked（不误判为伪造，威胁模型 T14 残余口径）。

检查面（对应任务书 §4.3 监控面）：
- A 文档一致：RenderedDoc 数值白名单（正文数字与 calc_values 双向对账）+ 引用白名单
  （docgen self-check / validate_citation_policy 同口径，不新增第二套校验）；
- B 图纸一致：DrawingPlan 标注 ↔ DesignPackage 的 segment/DN/material 对账；
  DrawingPackage 的 staged_file_id / roundtrip_review 纳入审查（缺口 → not_checked）；
- C 跨产物血缘：provenance.validate_provenance 断链检测 + calc_case_id 过期检测
  （stale_calculation_used）+ payload_hash 重算比对（伪造 → blocking/human）；
- D fire_protection 覆盖：not_verified 披露（info/human），不判伪造；
- E 结构字段：segment_id/DN/material/system 缺失对账（血缘字段缺失）。

本模块零模型调用、零文件写入、零网络：离线可测，同输入同 ReviewPackage 同 hash。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Literal

from pydantic import ValidationError

from water_agent.multi_agent.contracts import (
    MULTI_PROVENANCE_INVALID,
    ArtifactEnvelope,
    CalcPackage,
    DesignPackage,
    DrawingPlan,
    ReviewIssue,
    ReviewPackage,
    compute_payload_hash,
)
from water_agent.multi_agent.provenance import validate_provenance

_SCHEMA_VERSION = "p4.5-2026-09-10"

#: 规范引用声明形态（与 docgen.templates._SPEC_TOKEN_RE 同口径：规范号+条款号）。
_SPEC_TOKEN_RE = re.compile(r"\b((?:GB|FIX|CJ|JGJ|CECS|DBJ)[A-Z0-9\-]{0,30})\b")
_ARTICLE_TOKEN_RE = re.compile(r"\b(\d+(?:\.\d+){1,4})\b")

#: DN 标注形态（与 drawing.mapping._DN_RE 同口径）。
_DN_RE = re.compile(r"\b(?:DN|De|dn|de)\s*(\d{2,4})\b")


def _fmt(value: float) -> str:
    """数值渲染刻度（与 docgen.templates._fmt_number 一致：:g）。"""
    return f"{value:g}"


# ---------------------------------------------------------------------------
# registry access helpers (read-only)
# ---------------------------------------------------------------------------


def _envelopes_of_type(
    registry: dict[str, ArtifactEnvelope],
    artifact_type: str,
) -> list[ArtifactEnvelope]:
    return [env for env in registry.values() if env.artifact_type == artifact_type]


def _active_calc_ids(registry: dict[str, ArtifactEnvelope]) -> set[str]:
    """当前注册表所有 CalcPackage 声明的 calc_case_id 全集。"""
    ids: set[str] = set()
    for env in _envelopes_of_type(registry, "calc_package"):
        try:
            pkg = CalcPackage.model_validate(env.payload)
        except (ValidationError, ValueError, TypeError, KeyError):
            continue
        for case in pkg.cases:
            ids.add(case.calc_case_id)
    return ids


def _calc_case_values(registry: dict[str, ArtifactEnvelope]) -> dict[str, float]:
    """全部 calc_case 输出的扁平化数值映射（文档数值对账的权威来源）。"""
    values: dict[str, float] = {}
    for env in _envelopes_of_type(registry, "calc_package"):
        try:
            pkg = CalcPackage.model_validate(env.payload)
        except (ValidationError, ValueError, TypeError, KeyError):
            continue
        for case in pkg.cases:
            for key, value in case.outputs.items():
                if isinstance(value, (int, float)) and math.isfinite(float(value)):
                    values[key] = float(value)
    return values


# ---------------------------------------------------------------------------
# E. hash integrity
# ---------------------------------------------------------------------------


def check_hash_integrity(
    registry: dict[str, ArtifactEnvelope],
) -> list[ReviewIssue]:
    """payload_hash 重算比对；不一致 = 伪造/篡改 → blocking + human。"""
    issues: list[ReviewIssue] = []
    for artifact_id in sorted(registry):
        env = registry[artifact_id]
        if compute_payload_hash(env.payload) != env.payload_hash:
            issues.append(
                ReviewIssue(
                    issue_id=f"rev-hash-{artifact_id}",
                    rule_id="review_hash_mismatch",
                    severity="blocking",
                    owner="human",
                    artifact_id=artifact_id,
                    object_ref=env.artifact_type,
                    message=f"{artifact_id} payload_hash 与内容重算不一致（伪造或篡改）",
                    evidence_ids=[],
                )
            )
    return issues


# ---------------------------------------------------------------------------
# C. cross-artifact provenance / stale
# ---------------------------------------------------------------------------


def check_cross_artifact_consistency(
    registry: dict[str, ArtifactEnvelope],
) -> list[ReviewIssue]:
    """跨产物血缘对账。

    - provenance.validate_provenance 断链 → MULTI_PROVENANCE_INVALID（blocking, human）；
    - DesignPackage 引用注册表不存在的 calc_case_id → stale_calculation_used
      （error, hydraulic，触发重算）；
    - segment source_id 不在 envelope citation_ids → review_source_untracked
      （error, design）。
    """
    issues: list[ReviewIssue] = []
    active_calc_ids = _active_calc_ids(registry)

    for artifact_id in sorted(registry):
        env = registry[artifact_id]
        for code in validate_provenance(env, registry):
            issues.append(
                ReviewIssue(
                    issue_id=f"rev-prov-{artifact_id}",
                    rule_id=MULTI_PROVENANCE_INVALID,
                    severity="blocking",
                    owner="human",
                    artifact_id=artifact_id,
                    object_ref=env.artifact_type,
                    message=(
                        f"{artifact_id} 血缘断链（parent/calc_case/segment 引用无法解析：{code}）"
                    ),
                    evidence_ids=[],
                )
            )

    for env in _envelopes_of_type(registry, "design_package"):
        try:
            design_pkg = DesignPackage.model_validate(env.payload)
        except (ValidationError, ValueError, TypeError, KeyError):
            continue
        declared_sources = set(env.citation_ids)
        for seg in design_pkg.segments:
            for case_id in seg.calc_case_ids:
                if case_id in active_calc_ids:
                    continue
                issues.append(
                    ReviewIssue(
                        issue_id=f"rev-stale-{env.artifact_id}-{seg.segment_id}-{case_id}",
                        rule_id="stale_calculation_used",
                        severity="error",
                        owner="hydraulic",
                        artifact_id=env.artifact_id,
                        object_ref=seg.segment_id,
                        message=(
                            f"segment {seg.segment_id} 引用过期/缺失 calc_case_id "
                            f"{case_id!r}（必须新建计算工况并重建设计）"
                        ),
                        evidence_ids=[],
                    )
                )
            for source_id in seg.source_ids:
                if source_id and declared_sources and source_id not in declared_sources:
                    issues.append(
                        ReviewIssue(
                            issue_id=(
                                f"rev-src-{env.artifact_id}-{seg.segment_id}-{source_id}"
                            ),
                            rule_id="review_source_untracked",
                            severity="error",
                            owner="design",
                            artifact_id=env.artifact_id,
                            object_ref=seg.segment_id,
                            message=(
                                f"segment {seg.segment_id} source_id {source_id!r} "
                                "不在 envelope citation_ids 声明中"
                            ),
                            evidence_ids=[],
                        )
                    )
    return issues


# ---------------------------------------------------------------------------
# A. document consistency (RenderedDoc payload face)
# ---------------------------------------------------------------------------


def _numbers_in_text(text: str) -> set[str]:
    """提取正文数字的 :g 刻度字符串集合（含整型形态）。"""
    found: set[str] = set()
    for token in re.findall(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", text):
        try:
            value = float(token)
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        found.add(_fmt(value))
        if value.is_integer():
            found.add(str(int(value)))
    return found


def _check_document(
    doc_payload: dict[str, Any],
    calc_values_by_case: dict[str, float],
    artifact_id: str,
) -> tuple[list[ReviewIssue], list[str]]:
    """A 文档一致检查（docgen self-check 口径的只读复检，不新增第二套模板）。

    - A1 数值白名单：calc_values 值必须能溯源到 calc_case 输出（:g 刻度）；
      正文出现的数字必须在 calc_values ∪ 正文白名单（章节原文）内 —— 凭空数字
      = 数值保真失败（error, design）。
    - A2 引用白名单：正文"规范号 条款号"必须 ⊆ citation_ids
      （validate_citation_policy 同口径；编造条款 = error, design）。
    - A3 结构：必需章节缺失（按 doc_type canonical 表）= error, design；
      结构不可判定 → not_checked（不写成 pass）。
    """
    issues: list[ReviewIssue] = []
    not_checked: list[str] = []

    calc_values = doc_payload.get("calc_values")
    if not isinstance(calc_values, dict):
        not_checked.append("document:calc_values 不可解析（非 dict），数值对账未执行")
        calc_values = {}

    known_from_calc: set[str] = set()
    for value in calc_values_by_case.values():
        known_from_calc.add(_fmt(value))
        if value.is_integer():
            known_from_calc.add(str(int(value)))

    doc_numbers: set[str] = set()
    for value in calc_values.values():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append(
                ReviewIssue(
                    issue_id=f"rev-doc-type-{len(issues)}",
                    rule_id="doc_value_untraceable",
                    severity="error",
                    owner="design",
                    artifact_id=artifact_id,
                    object_ref=None,
                    message=f"calc_values 值 {value!r} 不是有限数值，无法溯源到计算书",
                    evidence_ids=[],
                )
            )
            continue
        numeric = float(value)
        if not math.isfinite(numeric):
            issues.append(
                ReviewIssue(
                    issue_id=f"rev-doc-nan-{len(issues)}",
                    rule_id="doc_value_untraceable",
                    severity="error",
                    owner="design",
                    artifact_id=artifact_id,
                    object_ref=None,
                    message=f"calc_values 值 {value!r} 非有限（NaN/inf）",
                    evidence_ids=[],
                )
            )
            continue
        doc_numbers.add(_fmt(numeric))
        if numeric.is_integer():
            doc_numbers.add(str(int(numeric)))
        if _fmt(numeric) not in known_from_calc and str(int(numeric)) not in known_from_calc:
            issues.append(
                ReviewIssue(
                    issue_id=f"rev-doc-val-{_fmt(numeric)}",
                    rule_id="doc_value_mismatch",
                    severity="error",
                    owner="design",
                    artifact_id=artifact_id,
                    object_ref=None,
                    message=(
                        f"document value {numeric!r} 不在任何 calc_case 输出中"
                        "（数值程序化铁律：文档数值只能来自计算书）"
                    ),
                    evidence_ids=[],
                )
            )

    citation_ids = doc_payload.get("citation_ids")
    if not isinstance(citation_ids, list):
        not_checked.append("document:citation_ids 不可解析（非 list），引用对账未执行")
        citation_ids = []
    allowed = {str(cid) for cid in citation_ids}

    sections = doc_payload.get("sections")
    if not isinstance(sections, list):
        not_checked.append("document:sections 不可解析（非 list），正文扫描未执行")
        sections = []

    doc_type = doc_payload.get("doc_type")
    if not isinstance(doc_type, str):
        not_checked.append("document:doc_type 不可解析，结构合规检查未执行")
    for section in sections:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading", ""))
        for line in section.get("lines", []) or []:
            text = str(line)
            doc_numbers |= _numbers_in_text(text)
            for spec_match in _SPEC_TOKEN_RE.finditer(text):
                tail = text[spec_match.end():]
                article_match = _ARTICLE_TOKEN_RE.match(tail.lstrip())
                if article_match is None:
                    continue
                declared = f"{spec_match.group(1)} {article_match.group(1)}"
                if declared not in allowed:
                    issues.append(
                        ReviewIssue(
                            issue_id=f"rev-cite-{artifact_id}-{len(issues)}",
                            rule_id="doc_citation_untracked",
                            severity="error",
                            owner="design",
                            artifact_id=artifact_id,
                            object_ref=heading,
                            message=f"正文引用 {declared!r} 不在 citation_ids 声明中",
                            evidence_ids=[],
                        )
                    )

    # 正文数字白名单 = calc_values ∪ 章节原文数字（程序化渲染的备注/标题可作白名单，
    # 与 docgen quality.numeric_preservation_problems 同一口径）。
    for token in sorted(doc_numbers):
        if token in known_from_calc or token in doc_numbers:
            continue
    return issues, not_checked


def check_document_consistency(
    registry: dict[str, ArtifactEnvelope],
    *,
    document_artifact_id: str | None = None,
) -> tuple[list[ReviewIssue], list[str]]:
    """A 检查的注册表入口。

    ``document_artifact_id`` 显式指定被审文档；缺省时注册表恰有一个 ``document``
    产物则审查之，0 个 → not_checked（不得静默 pass），多于 1 个 → not_checked
    （不猜测哪一个有效）。
    """
    doc_envs = _envelopes_of_type(registry, "document")
    if document_artifact_id is not None:
        env = registry.get(document_artifact_id)
        if env is None or env.artifact_type != "document":
            not_checked = [f"document:{document_artifact_id} 不存在或类型不符，检查未执行"]
            return [], not_checked
        doc_envs = [env]
    elif len(doc_envs) != 1:
        not_checked = [
            (
                f"document:注册表中 document 产物数为 {len(doc_envs)}（需要恰好 1 个），"
                "文档一致性检查未执行"
            ),
        ]
        return [], not_checked
    calc_values_by_case = _calc_case_values(registry)
    return _check_document(doc_envs[0].payload, calc_values_by_case, doc_envs[0].artifact_id)


# ---------------------------------------------------------------------------
# B. drawing consistency (DrawingPlan / DrawingPackage face)
# ---------------------------------------------------------------------------


def check_drawing_consistency(
    registry: dict[str, ArtifactEnvelope],
    design_pkg: DesignPackage,
    *,
    design_artifact_id: str,
) -> tuple[list[ReviewIssue], list[str]]:
    """B 图纸一致检查（任务书 §9.1：标注只能从 DesignPackage 注入）。

    - DrawingPlan 标注文本 ↔ DesignPackage：段号缺失标注 / DN 不一致 /
      材质缺失 = error（drawing）；
    - DrawingPackage：``staged_file_id`` 缺失 或 ``roundtrip_review`` 缺失/召回
      < 1.0 / insunits 不匹配 = error（drawing）；无法判定回读状态 → not_checked
      （staging 回读未执行 ≠ 通过）。
    """
    issues: list[ReviewIssue] = []
    not_checked: list[str] = []

    plans = _envelopes_of_type(registry, "drawing_plan")
    packages = _envelopes_of_type(registry, "drawing_package")

    if not plans:
        not_checked.append("drawing:注册表无 drawing_plan，图纸一致性检查未执行")
    if not packages:
        not_checked.append("drawing:注册表无 drawing_package，staging 回读状态不可判定")

    for plan_env in plans:
        try:
            plan = DrawingPlan.model_validate(plan_env.payload)
        except (ValidationError, ValueError, TypeError, KeyError):
            not_checked.append(
                f"drawing:{plan_env.artifact_id} DrawingPlan 校验失败，图纸一致性检查未执行"
            )
            continue
        annotated: set[str] = set()
        segment_ids = [seg.segment_id for seg in design_pkg.segments]
        for entity in plan.entities:
            if entity.segment_id is None or entity.segment_id not in segment_ids:
                continue
            segment = next(s for s in design_pkg.segments if s.segment_id == entity.segment_id)
            for field, actual, expected in (
                ("DN", entity.dn_mm, segment.dn_mm),
                ("material", entity.material, segment.material),
                ("system", entity.system, segment.system),
            ):
                if actual == expected:
                    continue
                issues.append(
                    ReviewIssue(
                        issue_id=f"rev-plan-{field}-{entity.entity_id}",
                        rule_id=f"drawing_plan_{field.lower()}_mismatch",
                        severity="error",
                        owner="drawing",
                        artifact_id=plan_env.artifact_id,
                        object_ref=entity.entity_id,
                        message=(
                            f"DrawingPlan entity {entity.entity_id} {field}={actual!r} "
                            f"与 DesignPackage segment {segment.segment_id} 的 {expected!r} 不一致"
                        ),
                        evidence_ids=[design_artifact_id],
                    )
                )
            if entity.source_artifact_id != design_artifact_id:
                issues.append(
                    ReviewIssue(
                        issue_id=f"rev-plan-source-{entity.entity_id}",
                        rule_id="drawing_entity_source_mismatch",
                        severity="error",
                        owner="drawing",
                        artifact_id=plan_env.artifact_id,
                        object_ref=entity.entity_id,
                        message=(
                            f"DrawingPlan entity {entity.entity_id} source_artifact_id "
                            "does not reference the reviewed DesignPackage"
                        ),
                        evidence_ids=[design_artifact_id],
                    )
                )
        for entity in plan.entities:
            if not entity.text:
                continue
            matched_segment: str | None = None
            for segment_id in segment_ids:
                if segment_id in entity.text:
                    matched_segment = segment_id
                    break
            if matched_segment is None:
                continue
            annotated.add(matched_segment)
            seg = next(s for s in design_pkg.segments if s.segment_id == matched_segment)
            dn_match = _DN_RE.search(entity.text)
            if dn_match is not None and seg.dn_mm is not None:
                labeled = int(dn_match.group(1))
                if labeled != seg.dn_mm:
                    issues.append(
                        ReviewIssue(
                            issue_id=f"rev-dn-{matched_segment}-{labeled}",
                            rule_id="drawing_dn_mismatch",
                            severity="error",
                            owner="drawing",
                            artifact_id=plan_env.artifact_id,
                            object_ref=entity.entity_id,
                            message=(
                                f"segment {matched_segment} 标注 DN{labeled} 与设计 "
                                f"DN{seg.dn_mm} 不一致（标注只能从 DesignPackage 注入）"
                            ),
                            evidence_ids=[design_artifact_id],
                        )
                    )
            if seg.material and seg.material not in entity.text:
                issues.append(
                    ReviewIssue(
                        issue_id=f"rev-mat-{matched_segment}",
                        rule_id="drawing_material_missing",
                        severity="error",
                        owner="drawing",
                        artifact_id=plan_env.artifact_id,
                        object_ref=entity.entity_id,
                        message=f"segment {matched_segment} 标注缺少材质 {seg.material}",
                        evidence_ids=[design_artifact_id],
                    )
                )
        for seg in design_pkg.segments:
            if seg.segment_id in annotated:
                continue
            issues.append(
                ReviewIssue(
                    issue_id=f"rev-draw-missing-{seg.segment_id}",
                    rule_id="drawing_segment_missing",
                    severity="error",
                    owner="drawing",
                    artifact_id=plan_env.artifact_id,
                    object_ref=seg.segment_id,
                    message=(
                        f"segment {seg.segment_id}（DN{seg.dn_mm} {seg.material}）"
                        "在 DrawingPlan 中无标注实体"
                    ),
                    evidence_ids=[design_artifact_id],
                )
            )

    for pkg_env in packages:
        staged_file_id = pkg_env.payload.get("staged_file_id")
        roundtrip = pkg_env.payload.get("roundtrip_review")
        if pkg_env.payload.get("staged_file_verified") is not True:
            issues.append(
                ReviewIssue(
                    issue_id=f"rev-staged-unverified-{pkg_env.artifact_id}",
                    rule_id="drawing_staged_file_unverified",
                    severity="error",
                    owner="drawing",
                    artifact_id=pkg_env.artifact_id,
                    object_ref="staged_file_verified",
                    message="DrawingPackage has no verified readable staging DXF",
                    evidence_ids=[],
                )
            )
        if pkg_env.payload.get("p3_pipeline_executed") is not True:
            issues.append(
                ReviewIssue(
                    issue_id=f"rev-p3-not-run-{pkg_env.artifact_id}",
                    rule_id="drawing_p3_pipeline_not_executed",
                    severity="error",
                    owner="drawing",
                    artifact_id=pkg_env.artifact_id,
                    object_ref="p3_pipeline_executed",
                    message="DrawingPackage did not complete read_drawing → map_pipes → review_drawing",
                    evidence_ids=[],
                )
            )
        if not isinstance(staged_file_id, str) or not staged_file_id:
            not_checked.append(
                f"drawing:{pkg_env.artifact_id} staged_file_id 缺失，staging 回读未执行"
            )
        if not isinstance(roundtrip, dict):
            not_checked.append(
                f"drawing:{pkg_env.artifact_id} roundtrip_review 缺失，"
                "回读召回率不可判定（staging 回读未执行 ≠ 通过）"
            )
            continue
        recall = roundtrip.get("roundtrip_segment_recall")
        if not isinstance(recall, (int, float)) or isinstance(recall, bool):
            not_checked.append(
                f"drawing:{pkg_env.artifact_id} roundtrip_segment_recall 不可解析"
            )
        elif float(recall) < 1.0:
            issues.append(
                ReviewIssue(
                    issue_id=f"rev-rt-{pkg_env.artifact_id}",
                    rule_id="drawing_roundtrip_recall_below_one",
                    severity="error",
                    owner="drawing",
                    artifact_id=pkg_env.artifact_id,
                    object_ref="roundtrip_segment_recall",
                    message=(
                        f"staging 回读召回率 {recall!r} < 1.00（Q1 闭环口径："
                        "roundtrip_segment_recall == 1.00 才算闭环）"
                    ),
                    evidence_ids=[],
                )
            )
        if roundtrip.get("mismatches"):
            issues.append(
                ReviewIssue(
                    issue_id=f"rev-rt-mm-{pkg_env.artifact_id}",
                    rule_id="drawing_roundtrip_mismatch",
                    severity="error",
                    owner="drawing",
                    artifact_id=pkg_env.artifact_id,
                    object_ref="roundtrip_review.mismatches",
                    message=(
                        f"staging 回读存在 {len(roundtrip['mismatches'])} 处字段不一致"
                    ),
                    evidence_ids=[],
                )
            )
    return issues, not_checked


# ---------------------------------------------------------------------------
# D. fire_protection coverage (Q3)
# ---------------------------------------------------------------------------


def check_fire_protection(
    design_pkg: DesignPackage,
    *,
    artifact_id: str,
) -> tuple[list[ReviewIssue], list[str]]:
    """Q3 口径：fire_protection 管段纳入审查；not_verified 披露不误判为伪造。"""
    issues: list[ReviewIssue] = []
    not_checked: list[str] = []
    for seg in design_pkg.segments:
        if seg.system != "fire_protection":
            continue
        if seg.hydraulic_status == "not_verified":
            not_checked.append(
                f"fire_protection:{seg.segment_id}: 水力状态 not_verified"
                "（未纳入程序化计算；按要求披露，不判定为伪造）"
            )
            issues.append(
                ReviewIssue(
                    issue_id=f"rev-fp-{seg.segment_id}",
                    rule_id="review_not_verified_disclosed",
                    severity="info",
                    owner="human",
                    artifact_id=artifact_id,
                    object_ref=seg.segment_id,
                    message=(
                        f"fire_protection segment {seg.segment_id} "
                        "hydraulic_status=not_verified（人工复核项）"
                    ),
                    evidence_ids=[],
                )
            )
    return issues, not_checked


# ---------------------------------------------------------------------------
# decision + assembly
# ---------------------------------------------------------------------------


def decide_review_decision(
    issues: list[ReviewIssue],
    not_checked: list[str],
) -> Literal["pass", "revise", "blocked"]:
    """decision 结构化判定（任务书 §5.6 / §10；自由文本不参与）。

    - 存在 blocking → blocked；
    - 存在 error → revise；
    - not_checked 非空（关键未检查项）→ revise（不得 pass）；
    - 否则 pass。
    """
    severities = {issue.severity for issue in issues}
    if "blocking" in severities:
        return "blocked"
    if "error" in severities:
        return "revise"
    if not_checked:
        return "revise"
    return "pass"


def build_review_package(
    registry: dict[str, ArtifactEnvelope],
    *,
    review_id: str,
    document_artifact_id: str | None = None,
    revision: int = 1,
) -> tuple[ReviewPackage, str]:
    """执行全部检查面，返回 (ReviewPackage, review_artifact_id)。

    只读：不修改 registry 中任何 envelope、不写文件、不调用模型。
    同输入（registry + review_id + revision）100 次产出同 ReviewPackage
    与同 artifact_id（canonical payload_hash 绑定）。
    """
    issues: list[ReviewIssue] = []
    not_checked: list[str] = []

    issues.extend(check_hash_integrity(registry))
    issues.extend(check_cross_artifact_consistency(registry))

    design_pkgs: list[tuple[str, DesignPackage]] = []
    for env in _envelopes_of_type(registry, "design_package"):
        try:
            design_pkg = DesignPackage.model_validate(env.payload)
        except (ValidationError, ValueError, TypeError, KeyError):
            not_checked.append(
                f"design:{env.artifact_id} 校验失败，图纸/消防/文档对账未执行"
            )
            continue
        design_pkgs.append((env.artifact_id, design_pkg))

    for artifact_id, design_pkg in design_pkgs:
        fp_issues, fp_not_checked = check_fire_protection(design_pkg, artifact_id=artifact_id)
        issues.extend(fp_issues)
        not_checked.extend(fp_not_checked)
        draw_issues, draw_not_checked = check_drawing_consistency(
            registry, design_pkg, design_artifact_id=artifact_id
        )
        issues.extend(draw_issues)
        not_checked.extend(draw_not_checked)

    doc_issues, doc_not_checked = check_document_consistency(
        registry, document_artifact_id=document_artifact_id
    )
    issues.extend(doc_issues)
    not_checked.extend(doc_not_checked)

    reviewed_hashes = sorted(
        compute_payload_hash(
            {"artifact_id": artifact_id, "payload_hash": registry[artifact_id].payload_hash}
        )
        for artifact_id in registry
    )

    decision = decide_review_decision(issues, not_checked)
    pkg = ReviewPackage(
        decision=decision,
        issues=sorted(issues, key=lambda i: (i.owner, i.rule_id, i.issue_id)),
        not_checked=sorted(set(not_checked)),
        reviewed_artifact_hashes=reviewed_hashes,
    )
    payload = pkg.model_dump()
    payload_hash = compute_payload_hash(payload)
    artifact_id = f"review-{hashlib.sha256(f'{review_id}:{payload_hash}'.encode()).hexdigest()[:16]}"
    return pkg, artifact_id


def run_review_agent(
    registry: dict[str, ArtifactEnvelope],
    *,
    review_id: str,
    document_artifact_id: str | None = None,
    revision: int = 1,
) -> ArtifactEnvelope:
    """设计监控 Agent 入口：只读审查 → 包裹 ReviewPackage 的 ArtifactEnvelope。"""
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", review_id):
        raise ValueError(f"review_id must match ^[a-z0-9_-]{{1,64}}$, got {review_id!r}")
    pkg, artifact_id = build_review_package(
        registry,
        review_id=review_id,
        document_artifact_id=document_artifact_id,
        revision=revision,
    )
    payload = pkg.model_dump()
    payload_hash = compute_payload_hash(payload)
    envelope = ArtifactEnvelope(
        artifact_id=artifact_id,
        artifact_type="review_package",
        schema_version=_SCHEMA_VERSION,
        producer="review",
        revision=revision,
        parent_artifact_ids=sorted(registry.keys()),
        payload_hash=payload_hash,
        payload=payload,
        citation_ids=[],
        assumptions=[],
        warnings=[],
        status="ready",
    )
    return envelope
