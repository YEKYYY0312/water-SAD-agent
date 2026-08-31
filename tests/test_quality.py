"""DATA_ROOT 质量扫描器：指标定义、去重键、行号定位、只读保证。"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from water_agent.ingestion.quality import (
    QualityReport,
    report_has_strict_blockers,
    scan_clause_file,
    scan_clause_files,
)

VALID = {
    "spec_id": "GB50014-2021",
    "chapter": "4",
    "section": "4.4",
    "article": "4.4.2",
    "title": "污水管道水力计算",
    "text": "污水管道的最大设计充满度，应按下列规定取值。",
}


def _write(root: Path, name: str, lines: list[object | str]) -> Path:
    clauses = root / "clauses"
    clauses.mkdir(parents=True, exist_ok=True)
    target = clauses / name
    with target.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write((line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)) + "\n")
    return target


class TestScanClauseFile:
    def test_single_valid_file(self):
        with TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "a.jsonl", [VALID])
            q = scan_clause_file(path)
            assert q.records_total == 1
            assert q.records_valid == 1
            assert q.records_invalid == 0
            assert q.invalid_json_lines == 0
            assert q.missing_required_fields == 0
            assert q.records_duplicate == 0
            assert q.spec_id_distribution == {"GB50014-2021": 1}

    def test_invalid_json_reports_file_and_line(self):
        with TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "a.jsonl", ['{"spec_id": 1', VALID])
            q = scan_clause_file(path)
            assert q.records_total == 1
            assert q.invalid_json_lines == 1
            assert q.invalid_json_details
            assert q.invalid_json_details[0]["line"] == 1
            assert q.invalid_json_details[0]["file"].endswith("a.jsonl")
            assert "line" in q.invalid_json_details[0]

    def test_missing_spec_id(self):
        with TemporaryDirectory() as tmp:
            bad = {k: v for k, v in VALID.items() if k != "spec_id"}
            path = _write(Path(tmp), "a.jsonl", [bad])
            q = scan_clause_file(path)
            assert q.missing_required_fields == 1
            assert q.records_invalid == 1
            assert q.records_valid == 0
            detail = q.missing_field_details[0]
            assert "spec_id" in detail["missing"]

    def test_missing_article(self):
        with TemporaryDirectory() as tmp:
            bad = {k: v for k, v in VALID.items() if k != "article"}
            path = _write(Path(tmp), "a.jsonl", [bad])
            q = scan_clause_file(path)
            assert q.missing_required_fields == 1
            assert "article" in q.missing_field_details[0]["missing"]

    def test_empty_text_counted(self):
        with TemporaryDirectory() as tmp:
            bad = dict(VALID, text="   ")
            path = _write(Path(tmp), "a.jsonl", [bad])
            q = scan_clause_file(path)
            assert q.empty_text_count == 1
            assert q.records_invalid == 1
            assert q.missing_required_fields == 1

    def test_blank_article_counted_missing(self):
        with TemporaryDirectory() as tmp:
            bad = dict(VALID, article="")
            path = _write(Path(tmp), "a.jsonl", [bad])
            q = scan_clause_file(path)
            assert q.missing_required_fields == 1
            assert q.records_invalid == 1

    def test_duplicate_full_key(self):
        with TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "a.jsonl", [VALID, VALID])
            q = scan_clause_file(path)
            assert q.records_total == 2
            assert q.records_valid == 2  # 结构上均合法
            assert q.records_duplicate == 1  # 逻辑键完全重复

    def test_article_duplicate_with_different_text(self):
        with TemporaryDirectory() as tmp:
            other = dict(VALID, text="另一段完全不同的正文内容。")
            path = _write(Path(tmp), "a.jsonl", [VALID, other])
            q = scan_clause_file(path)
            assert q.records_duplicate == 0  # content_hash 不同
            assert q.article_duplicates == 1  # 同 spec_id+article 出现两次
            assert q.content_hash_duplicates == 0

    def test_content_hash_duplicate_across_articles(self):
        with TemporaryDirectory() as tmp:
            same_text = dict(VALID, article="4.4.3")
            path = _write(Path(tmp), "a.jsonl", [VALID, same_text])
            q = scan_clause_file(path)
            assert q.records_duplicate == 0
            assert q.content_hash_duplicates == 1
            assert q.article_duplicates == 0

    def test_table_like_records(self):
        with TemporaryDirectory() as tmp:
            table = dict(
                VALID,
                article="4.4.4",
                text="| 管径 | 充满度 |\n|------|--------|\n| DN200 | 0.55 |",
            )
            path = _write(Path(tmp), "a.jsonl", [VALID, table])
            q = scan_clause_file(path)
            assert q.table_like_records == 1

    def test_chapter_section_nullable(self):
        with TemporaryDirectory() as tmp:
            minimal = {"spec_id": "X", "article": "1.1", "text": "正文", "chapter": None, "section": None, "title": ""}
            path = _write(Path(tmp), "a.jsonl", [minimal])
            q = scan_clause_file(path)
            assert q.records_valid == 1
            assert q.records_invalid == 0


class TestScanClauseFiles:
    def test_multi_file_aggregation_and_cross_file_duplicate(self):
        with TemporaryDirectory() as tmp:
            _write(Path(tmp), "a.jsonl", [VALID])
            _write(Path(tmp), "b.jsonl", [VALID, dict(VALID, article="5.2.1")])
            report = scan_clause_files([Path(tmp) / "clauses" / "a.jsonl", Path(tmp) / "clauses" / "b.jsonl"])
            assert report.summary["records_total"] == 3
            assert report.summary["records_valid"] == 3
            # a.jsonl 中的记录在 b.jsonl 重复 → 跨文件重复
            assert report.summary["records_duplicate"] == 1
            assert len(report.per_file) == 2
            assert report.per_file[0].source_path.endswith("a.jsonl")

    def test_chapter_section_coverage(self):
        with TemporaryDirectory() as tmp:
            no_chapter = dict(VALID, chapter=None)
            _write(Path(tmp), "a.jsonl", [VALID, no_chapter])
            report = scan_clause_files([Path(tmp) / "clauses" / "a.jsonl"])
            cov = report.summary["chapter_section_coverage"]
            assert cov["records_with_chapter"] == 1
            assert cov["records_with_section"] == 2
            assert cov["records_total"] == 2

    def test_report_is_json_serializable(self):
        with TemporaryDirectory() as tmp:
            _write(Path(tmp), "a.jsonl", [VALID])
            report = scan_clause_files([Path(tmp) / "clauses" / "a.jsonl"])
            payload = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
            parsed = json.loads(payload)
            assert isinstance(parsed, dict)
            assert parsed["summary"]["records_total"] == 1

    def test_scan_does_not_modify_source_files(self):
        with TemporaryDirectory() as tmp:
            path = _write(Path(tmp), "a.jsonl", [VALID, dict(VALID, article="5.2.1")])
            before = path.read_text(encoding="utf-8")
            scan_clause_files([path])
            after = path.read_text(encoding="utf-8")
            assert before == after

    def test_strict_blockers_empty_when_clean(self):
        with TemporaryDirectory() as tmp:
            _write(Path(tmp), "a.jsonl", [VALID])
            report = scan_clause_files([Path(tmp) / "clauses" / "a.jsonl"])
            assert report_has_strict_blockers(report) is False

    def test_strict_blockers_on_invalid_json(self):
        with TemporaryDirectory() as tmp:
            _write(Path(tmp), "a.jsonl", ["not-json"])
            report = scan_clause_files([Path(tmp) / "clauses" / "a.jsonl"])
            assert report_has_strict_blockers(report) is True

    def test_strict_blockers_on_missing_required(self):
        with TemporaryDirectory() as tmp:
            bad = {k: v for k, v in VALID.items() if k != "article"}
            _write(Path(tmp), "a.jsonl", [bad])
            report = scan_clause_files([Path(tmp) / "clauses" / "a.jsonl"])
            assert report_has_strict_blockers(report) is True

    def test_strict_blockers_on_duplicate(self):
        with TemporaryDirectory() as tmp:
            _write(Path(tmp), "a.jsonl", [VALID, VALID])
            report = scan_clause_files([Path(tmp) / "clauses" / "a.jsonl"])
            assert report_has_strict_blockers(report) is True

    def test_empty_text_is_strict_blocker_and_reported(self):
        # 验收口径：strict 门禁含非法 JSON / 缺必填（含空 text）/ 重复逻辑条款
        with TemporaryDirectory() as tmp:
            _write(Path(tmp), "a.jsonl", [dict(VALID, text="   ")])
            report = scan_clause_files([Path(tmp) / "clauses" / "a.jsonl"])
            assert report.summary["empty_text_count"] == 1
            assert report_has_strict_blockers(report) is True
