"""DATA_ROOT 质量扫描器：多文件 JSONL 契约校验与质量报告。

契约
-----
每条记录最少要求 ``spec_id / chapter / section / article / title / text``；
其中 ``spec_id``、``article``、``text`` 必填且非空（``text`` 不能为空白），
``chapter``、``section``、``title`` 允许为空。

原则
-----
- 非法 JSON 必须携带文件与行号；
- 去重使用稳定逻辑键 ``spec_id | article | content_hash(text)``；
- 只读扫描，不修改原始 JSONL；
- 报告可序列化为 JSON。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from water_agent.domain.models import content_hash_of, utc_now

REQUIRED_FIELDS = ("spec_id", "article", "text")

MD_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)


class ClauseRecord(BaseModel):
    """条款记录契约模型（允许额外字段，但最少字段必须满足）。"""

    model_config = ConfigDict(extra="allow")

    spec_id: str
    chapter: str | None = None
    section: str | None = None
    article: str
    title: str = ""
    text: str


class FileQuality(BaseModel):
    """单个 JSONL 文件的质量统计。"""

    source_path: str
    records_total: int = 0
    records_valid: int = 0
    records_invalid: int = 0
    records_duplicate: int = 0
    empty_text_count: int = 0
    missing_required_fields: int = 0
    invalid_json_lines: int = 0
    article_duplicates: int = 0
    content_hash_duplicates: int = 0
    table_like_records: int = 0
    chapter_section_coverage: dict[str, int | float] = Field(default_factory=dict)
    spec_id_distribution: dict[str, int] = Field(default_factory=dict)
    invalid_json_details: list[dict[str, Any]] = Field(default_factory=list)
    missing_field_details: list[dict[str, Any]] = Field(default_factory=list)


class QualityReport(BaseModel):
    """多文件总体质量报告（可序列化为 JSON）。"""

    generated_at: str
    data_root: str
    mode: str
    status: str = "ok"
    summary: dict[str, Any] = Field(default_factory=dict)
    per_file: list[FileQuality] = Field(default_factory=list)


def _coverage(records_with_chapter: int, records_with_section: int, total: int) -> dict[str, int | float]:
    return {
        "records_with_chapter": records_with_chapter,
        "records_with_section": records_with_section,
        "records_total": total,
        "chapter_coverage_ratio": round(records_with_chapter / total, 4) if total else 0.0,
        "section_coverage_ratio": round(records_with_section / total, 4) if total else 0.0,
    }


def scan_clause_file(path: str | Path) -> FileQuality:
    """扫描单个 JSONL 文件，返回该文件的质量统计（文件内去重）。"""
    path = Path(path)
    seen_keys: set[str] = set()
    seen_articles: set[tuple[str, str]] = set()
    seen_hashes: set[str] = set()
    dist: dict[str, int] = {}
    with_chapter = 0
    with_section = 0
    valid_count = 0

    file_stats = FileQuality(source_path=str(path))
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                file_stats.invalid_json_lines += 1
                file_stats.invalid_json_details.append(
                    {"file": str(path), "line": line_no, "error": exc.msg}
                )
                continue
            if not isinstance(obj, dict):
                file_stats.records_invalid += 1
                file_stats.missing_field_details.append(
                    {"file": str(path), "line": line_no, "missing": ["<object>"], "error": "record is not a JSON object"}
                )
                continue

            file_stats.records_total += 1
            missing = [k for k in REQUIRED_FIELDS if k not in obj]
            if missing:
                file_stats.records_invalid += 1
                file_stats.missing_required_fields += 1
                file_stats.missing_field_details.append(
                    {"file": str(path), "line": line_no, "missing": missing, "error": "missing required field(s)"}
                )
                continue

            try:
                record = ClauseRecord.model_validate(obj)
            except ValidationError as exc:
                missing_names = sorted({e["loc"][0] for e in exc.errors() if isinstance(e.get("loc"), tuple) and e["loc"]})
                file_stats.records_invalid += 1
                file_stats.missing_required_fields += 1
                file_stats.missing_field_details.append(
                    {"file": str(path), "line": line_no, "missing": missing_names or ["<field>"], "error": "invalid required field"}
                )
                continue

            if not record.spec_id.strip() or not record.article.strip():
                blank = [k for k in ("spec_id", "article") if not getattr(record, k).strip()]
                file_stats.records_invalid += 1
                file_stats.missing_required_fields += 1
                file_stats.missing_field_details.append(
                    {"file": str(path), "line": line_no, "missing": blank, "error": "required field is blank"}
                )
                continue
            if not record.text.strip():
                file_stats.records_invalid += 1
                file_stats.empty_text_count += 1
                file_stats.missing_required_fields += 1
                file_stats.missing_field_details.append(
                    {"file": str(path), "line": line_no, "missing": ["text"], "error": "text is blank"}
                )
                continue

            file_stats.records_valid += 1
            valid_count += 1
            if record.chapter and record.chapter.strip():
                with_chapter += 1
            if record.section and record.section.strip():
                with_section += 1
            dist[record.spec_id] = dist.get(record.spec_id, 0) + 1

            content_hash = content_hash_of(record.text)
            key = f"{record.spec_id}|{record.article}|{content_hash}"
            if key in seen_keys:
                file_stats.records_duplicate += 1
            else:
                seen_keys.add(key)
            article_key = (record.spec_id, record.article)
            if article_key in seen_articles:
                file_stats.article_duplicates += 1
            else:
                seen_articles.add(article_key)
            if content_hash in seen_hashes:
                file_stats.content_hash_duplicates += 1
            else:
                seen_hashes.add(content_hash)
            if MD_TABLE_ROW_RE.search(record.text):
                file_stats.table_like_records += 1

    file_stats.chapter_section_coverage = _coverage(with_chapter, with_section, valid_count)
    file_stats.spec_id_distribution = dict(sorted(dist.items()))
    return file_stats


def scan_clause_files(paths: list[str | Path]) -> QualityReport:
    """扫描多个 JSONL 文件，聚合总体报告（跨文件全局去重）。"""
    files = [scan_clause_file(p) for p in paths]

    summary: dict[str, Any] = {
        "source_paths": [str(p) for p in paths],
        "records_total": 0,
        "records_valid": 0,
        "records_invalid": 0,
        "records_duplicate": 0,
        "empty_text_count": 0,
        "missing_required_fields": 0,
        "invalid_json_lines": 0,
        "article_duplicates": 0,
        "content_hash_duplicates": 0,
        "table_like_records": 0,
    }
    for key in ("records_total", "records_valid", "records_invalid", "records_duplicate", "empty_text_count", "missing_required_fields", "invalid_json_lines", "article_duplicates", "content_hash_duplicates", "table_like_records"):
        summary[key] = sum(getattr(f, key) for f in files)

    # 跨文件全局去重：文件级已各自去重，这里用全局键补算总体重复
    global_keys: set[str] = set()
    global_articles: set[tuple[str, str]] = set()
    global_hashes: set[str] = set()
    global_duplicates = 0
    global_article_dups = 0
    global_hash_dups = 0
    with_chapter = 0
    with_section = 0
    valid_total = 0
    dist: dict[str, int] = {}
    for path in paths:
        with Path(path).open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                try:
                    record = ClauseRecord.model_validate(obj)
                except ValidationError:
                    continue
                if not record.spec_id.strip() or not record.article.strip() or not record.text.strip():
                    continue
                valid_total += 1
                dist[record.spec_id] = dist.get(record.spec_id, 0) + 1
                if record.chapter and record.chapter.strip():
                    with_chapter += 1
                if record.section and record.section.strip():
                    with_section += 1
                content_hash = content_hash_of(record.text)
                key = f"{record.spec_id}|{record.article}|{content_hash}"
                if key in global_keys:
                    global_duplicates += 1
                else:
                    global_keys.add(key)
                article_key = (record.spec_id, record.article)
                if article_key in global_articles:
                    global_article_dups += 1
                else:
                    global_articles.add(article_key)
                if content_hash in global_hashes:
                    global_hash_dups += 1
                else:
                    global_hashes.add(content_hash)

    summary["records_duplicate"] = global_duplicates
    summary["article_duplicates"] = global_article_dups
    summary["content_hash_duplicates"] = global_hash_dups
    summary["chapter_section_coverage"] = _coverage(with_chapter, with_section, valid_total)
    summary["spec_id_distribution"] = dict(sorted(dist.items()))

    status = "ok" if not (summary["invalid_json_lines"] or summary["missing_required_fields"] or summary["records_duplicate"]) else "issues"
    return QualityReport(
        generated_at=utc_now().isoformat(),
        data_root="",
        mode="",
        status=status,
        summary=summary,
        per_file=files,
    )


def report_has_strict_blockers(report: QualityReport) -> bool:
    """strict 门禁：非法 JSON、缺少 spec_id/article/text、或重复逻辑条款。"""
    summary = report.summary
    return bool(
        summary["invalid_json_lines"] > 0
        or summary["missing_required_fields"] > 0
        or summary["records_duplicate"] > 0
    )
