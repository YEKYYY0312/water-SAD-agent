"""语义质检模块：对规范条款语料做分类、重复组识别、给出入库策略建议。"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from water_agent.domain.models import content_hash_of, utc_now

STANDARD_ARTICLE_RE = re.compile(r"^[1-9]\d*(?:\.[1-9]\d*){1,3}$")
TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*[-:]+\s*\|", re.MULTILINE)
OCR_SPACED_NUMBER_RE = re.compile(r"\d\s+\.\s*\d|\d\s+\d")
# TODO(需人工确认): 原字符在编码事故中丢失，当前 [??] 是占位，待用户确认真实字符集后替换
OCR_SYMBOL_RE = re.compile(r"[#|]{3,}|[??]{1,}")


class AuditCategory(str, Enum):
    NORMAL_ARTICLE = "normal_article"
    ARTICLE_FRAGMENT = "article_fragment"
    TABLE_FRAGMENT = "table_fragment"
    OCR_SUSPECT = "ocr_suspect"
    INVALID_ARTICLE_FORMAT = "invalid_article_format"
    EMPTY_OR_LOW_SIGNAL = "empty_or_low_signal"
    DUPLICATE_ARTICLE_VARIANT = "duplicate_article_variant"


class ClauseAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_file: str
    line_number: int
    spec_id: str = ""
    article: str = ""
    chapter: str | None = None
    section: str | None = None
    title: str = ""
    text_preview: str = ""
    content_hash: str = ""
    categories: list[str] = Field(default_factory=list)
    primary_category: str
    recommended_strategy: str
    recommendation_reason: str


class DuplicateGroup(BaseModel):
    spec_id: str
    article: str
    records: list[ClauseAudit] = Field(default_factory=list)
    likely_legitimate: bool = False
    recommended_strategy: str
    recommendation_reason: str


class SemanticAuditReport(BaseModel):
    generated_at: str
    data_root: str
    summary: dict[str, Any] = Field(default_factory=dict)
    records: list[ClauseAudit] = Field(default_factory=list)
    per_spec_statistics: dict[str, dict[str, Any]] = Field(default_factory=dict)
    duplicate_groups: list[DuplicateGroup] = Field(default_factory=list)
    duplicate_group_samples: dict[str, list[DuplicateGroup]] = Field(default_factory=dict)
    quarantine_candidates: list[ClauseAudit] = Field(default_factory=list)
    recommended_ingestion_policy: dict[str, dict[str, str]] = Field(default_factory=dict)
    manual_decisions_required: list[str] = Field(default_factory=list)


def _preview(text: str, limit: int = 180) -> str:
    return " ".join(text.split())[:limit]


def _is_table_like(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    table_lines = sum(bool(TABLE_LINE_RE.match(line)) for line in lines)
    return table_lines >= 2 or (table_lines >= 1 and (bool(TABLE_SEPARATOR_RE.search(text)) or text.count("|") >= 3))


def _effective_signal_ratio(text: str) -> float:
    if not text:
        return 0.0
    useful = sum(char.isalnum() or "\u4e00" <= char <= "\u9fff" for char in text)
    return useful / len(text)


def _looks_ocr_suspect(article: str, title: str, text: str) -> bool:
    compact = f"{article} {title} {text}"
    low_signal = _effective_signal_ratio(text) < 0.35
    return bool(OCR_SPACED_NUMBER_RE.search(compact) or OCR_SYMBOL_RE.search(compact) or (low_signal and len(text.strip()) < 30))


def classify_clause(record: dict[str, Any], source_file: str, line_number: int) -> ClauseAudit:
    spec_id = str(record.get("spec_id") or "")
    article = str(record.get("article") or "")
    chapter = record.get("chapter")
    section = record.get("section")
    title = str(record.get("title") or "")
    text = str(record.get("text") or "")
    table_like = _is_table_like(text)
    invalid_article = not bool(STANDARD_ARTICLE_RE.fullmatch(article))
    low_signal = not text.strip() or len(text.strip()) < 12 or _effective_signal_ratio(text) < 0.35
    ocr_suspect = _looks_ocr_suspect(article, title, text)
    categories: list[str] = []
    if table_like:
        categories.append(AuditCategory.TABLE_FRAGMENT.value)
    if invalid_article:
        categories.append(AuditCategory.INVALID_ARTICLE_FORMAT.value)
    if ocr_suspect:
        categories.append(AuditCategory.OCR_SUSPECT.value)
    if low_signal and not table_like:
        categories.append(AuditCategory.EMPTY_OR_LOW_SIGNAL.value)
    if not categories:
        categories.append(AuditCategory.NORMAL_ARTICLE.value)

    quarantine_categories = {
        AuditCategory.OCR_SUSPECT.value,
        AuditCategory.INVALID_ARTICLE_FORMAT.value,
        AuditCategory.EMPTY_OR_LOW_SIGNAL.value,
    }
    quarantine = bool(quarantine_categories & set(categories))
    if quarantine:
        primary = next(category for category in categories if category in quarantine_categories)
        strategy = "quarantine"
        reason = "命中 OCR 疑似 / 条款号非法 / 低信息量，整条隔离待人工复核"
    elif table_like:
        primary = AuditCategory.TABLE_FRAGMENT.value
        strategy = "retain_as_table_fragment"
        reason = "识别为表格片段，按 table 块保留原表格结构"
    else:
        primary = AuditCategory.NORMAL_ARTICLE.value
        strategy = "normal_parent_child_ingestion"
        reason = "条款格式规范，按父子级结构正常入库"
    return ClauseAudit(
        source_file=source_file,
        line_number=line_number,
        spec_id=spec_id,
        article=article,
        chapter=chapter,
        section=section,
        title=title,
        text_preview=_preview(text),
        content_hash=content_hash_of(text),
        categories=categories,
        primary_category=primary,
        recommended_strategy=strategy,
        recommendation_reason=reason,
    )


def _scan_records(paths: list[str | Path]) -> list[ClauseAudit]:
    results: list[ClauseAudit] = []
    for raw_path in paths:
        path = Path(raw_path)
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    results.append(classify_clause(record, str(path), line_number))
    return results


def _group_recommendation(records: list[ClauseAudit]) -> tuple[bool, str, str]:
    categories = {category for record in records for category in record.categories}
    has_table = AuditCategory.TABLE_FRAGMENT.value in categories
    has_quarantine = bool(categories & {
        AuditCategory.OCR_SUSPECT.value,
        AuditCategory.INVALID_ARTICLE_FORMAT.value,
        AuditCategory.EMPTY_OR_LOW_SIGNAL.value,
    })
    if has_quarantine:
        return False, "quarantine", "重复组内存在隔离项，整组隔离"
    if has_table:
        return True, "retain_ordered_fragments", "组内为同一 article 的表格片段，按原文顺序保留"
    return False, "manual_review", "同一 spec_id + article 的重复组，需人工判定是否为合法片段"


def audit_clause_files(paths: list[str | Path], data_root: str | Path = "") -> SemanticAuditReport:
    records = _scan_records(paths)
    groups: dict[tuple[str, str], list[ClauseAudit]] = defaultdict(list)
    for record in records:
        groups[(record.spec_id, record.article)].append(record)
    duplicate_groups: list[DuplicateGroup] = []
    for (spec_id, article), group in sorted(groups.items()):
        if len(group) < 2:
            continue
        likely, strategy, reason = _group_recommendation(group)
        updated = [record.model_copy(update={"categories": record.categories + [AuditCategory.DUPLICATE_ARTICLE_VARIANT.value]}) for record in group]
        duplicate_groups.append(DuplicateGroup(
            spec_id=spec_id,
            article=article,
            records=updated,
            likely_legitimate=likely,
            recommended_strategy=strategy,
            recommendation_reason=reason,
        ))

    by_spec: dict[str, dict[str, Any]] = {}
    for spec_id in sorted({record.spec_id for record in records}):
        subset = [record for record in records if record.spec_id == spec_id]
        spec_groups = [group for group in duplicate_groups if group.spec_id == spec_id]
        counts = Counter(record.primary_category for record in subset)
        by_spec[spec_id] = {
            "records_total": len(subset),
            "normal_article": counts[AuditCategory.NORMAL_ARTICLE.value],
            "table_fragment": counts[AuditCategory.TABLE_FRAGMENT.value],
            "ocr_suspect": sum(AuditCategory.OCR_SUSPECT.value in record.categories for record in subset),
            "invalid_article_format": sum(AuditCategory.INVALID_ARTICLE_FORMAT.value in record.categories for record in subset),
            "empty_or_low_signal": sum(AuditCategory.EMPTY_OR_LOW_SIGNAL.value in record.categories for record in subset),
            "duplicate_article_variant": sum(len(group.records) for group in spec_groups),
            "duplicate_groups": len(spec_groups),
            "quarantine_candidates": sum(1 for record in subset if record.recommended_strategy == "quarantine"),
        }

    normal_groups = [group for group in duplicate_groups if group.likely_legitimate]
    suspicious_groups = [group for group in duplicate_groups if not group.likely_legitimate]
    quarantine = [record for record in records if record.recommended_strategy == "quarantine"]
    policy = {
        AuditCategory.NORMAL_ARTICLE.value: {"strategy": "normal_parent_child_ingestion", "reason": "正常父子级入库"},
        AuditCategory.ARTICLE_FRAGMENT.value: {"strategy": "retain_ordered_fragments", "reason": "按序保留片段，作为 child 挂到同一 article"},
        AuditCategory.TABLE_FRAGMENT.value: {"strategy": "retain_as_table_fragment", "reason": "保留为 table chunk，沿用所属 article 引用"},
        AuditCategory.OCR_SUSPECT.value: {"strategy": "quarantine", "reason": "隔离（OCR 质量存疑）"},
        AuditCategory.INVALID_ARTICLE_FORMAT.value: {"strategy": "quarantine", "reason": "隔离（条款号格式不合规）"},
        AuditCategory.EMPTY_OR_LOW_SIGNAL.value: {"strategy": "quarantine", "reason": "隔离（内容为空或信息量过低）"},
        AuditCategory.DUPLICATE_ARTICLE_VARIANT.value: {"strategy": "retain_ordered_fragments_or_manual_review", "reason": "保留有序片段或转人工复核"},
    }
    summary = {
        "source_paths": [str(path) for path in paths],
        "records_total": len(records),
        "duplicate_groups": len(duplicate_groups),
        "duplicate_records_beyond_first": sum(len(group.records) - 1 for group in duplicate_groups),
        "groups_by_spec": dict(Counter(group.spec_id for group in duplicate_groups)),
        "groups_by_article_format": dict(Counter("standard" if STANDARD_ARTICLE_RE.fullmatch(group.article) else "non_standard" for group in duplicate_groups)),
        "groups_with_table_fragments": sum(any(AuditCategory.TABLE_FRAGMENT.value in record.categories for record in group.records) for group in duplicate_groups),
        "groups_with_ocr_suspect": sum(any(AuditCategory.OCR_SUSPECT.value in record.categories for record in group.records) for group in duplicate_groups),
        "groups_likely_legitimate_fragments": len(normal_groups),
        "groups_requiring_manual_review": len(suspicious_groups),
        "category_counts": dict(Counter(record.primary_category for record in records)),
    }
    return SemanticAuditReport(
        generated_at=utc_now().isoformat(),
        data_root=str(data_root),
        summary=summary,
        records=records,
        per_spec_statistics=by_spec,
        duplicate_groups=duplicate_groups,
        duplicate_group_samples={"normal": normal_groups[:10], "suspicious": suspicious_groups[:10]},
        quarantine_candidates=quarantine,
        recommended_ingestion_policy=policy,
        manual_decisions_required=[
            "同一 spec_id + article 的多条记录，如何区分合法片段与 OCR 重复",
            "GB18918-2002 是否整本排除（限值表为主，条款文本有限）",
            "GB5749-2022 是否只保留限值 table",
            "458 组需人工复核的重复组，复核优先级与判定标准",
        ],
    )
