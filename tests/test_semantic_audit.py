"""P1 增量 3A：重复条号与低质量条款语义审计。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from water_agent.ingestion.semantic_audit import (
    AuditCategory,
    audit_clause_files,
    classify_clause,
)
from water_agent.ingestion.semantic_audit_cli import main


VALID = {
    "spec_id": "GB50014-2021",
    "chapter": "4",
    "section": "4.4",
    "article": "4.4.2",
    "title": "污水管道水力计算",
    "text": "污水管道的最大设计充满度应按下列规定取值，并结合管径和流量确定。",
}


def _write(root: Path, records: list[dict | str]) -> Path:
    clauses = root / "clauses"
    clauses.mkdir(parents=True, exist_ok=True)
    path = clauses / "sample.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(record if isinstance(record, str) else json.dumps(record, ensure_ascii=False))
            handle.write("\n")
    return path


def test_classifies_normal_article():
    result = classify_clause(VALID, "sample.jsonl", 1)
    assert result.primary_category == AuditCategory.NORMAL_ARTICLE.value


def test_classifies_invalid_article_format_and_ocr_suspect():
    result = classify_clause(dict(VALID, article="0.01", text="0 . 5 0 . 01 | | | ###"), "sample.jsonl", 1)
    assert AuditCategory.INVALID_ARTICLE_FORMAT.value in result.categories
    assert AuditCategory.OCR_SUSPECT.value in result.categories


def test_classifies_table_fragment_without_calling_every_table_invalid():
    table = "| 项目 | 限值 |\n|---|---|\n| 浊度 | 1 |\n| 色度 | 15 |"
    result = classify_clause(dict(VALID, article="5.1", text=table), "sample.jsonl", 1)
    assert result.primary_category == AuditCategory.TABLE_FRAGMENT.value
    assert AuditCategory.INVALID_ARTICLE_FORMAT.value not in result.categories


def test_classifies_ocr_table_fragment():
    text = "0 . 5 0 . 01 0 . 1\n| | | | |\n### ### ###"
    result = classify_clause(dict(VALID, article="0.5", text=text), "sample.jsonl", 1)
    assert AuditCategory.TABLE_FRAGMENT.value in result.categories
    assert AuditCategory.OCR_SUSPECT.value in result.categories
    assert AuditCategory.INVALID_ARTICLE_FORMAT.value in result.categories


def test_duplicate_article_variants_and_legitimate_table_split():
    table = "| 管径 | 充满度 |\n|---|---|\n| DN200 | 0.55 |"
    records = [
        VALID,
        dict(VALID, text="本条正文的第二个分段，继续说明设计充满度的适用条件。"),
        dict(VALID, article="4.4.3", text=table),
        dict(VALID, article="4.4.3", text="本条的正文说明与对应表格共同构成完整条款。"),
    ]
    with TemporaryDirectory() as tmp:
        path = _write(Path(tmp), records)
        report = audit_clause_files([path], data_root=tmp)
        group = next(g for g in report.duplicate_groups if g.article == "4.4.2")
        assert group.likely_legitimate is False
        table_group = next(g for g in report.duplicate_groups if g.article == "4.4.3")
        assert table_group.likely_legitimate is True
        assert table_group.recommended_strategy == "retain_ordered_fragments"
        assert report.summary["duplicate_groups"] == 2


def test_low_signal_record_is_quarantine_candidate():
    result = classify_clause(dict(VALID, text="--- ### |||"), "sample.jsonl", 1)
    assert AuditCategory.EMPTY_OR_LOW_SIGNAL.value in result.categories
    assert result.recommended_strategy == "quarantine"


def test_report_is_json_serializable_and_source_is_unchanged():
    with TemporaryDirectory() as tmp:
        path = _write(Path(tmp), [VALID, dict(VALID, article="4.4.3")])
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        report = audit_clause_files([path], data_root=tmp)
        after = hashlib.sha256(path.read_bytes()).hexdigest()
        assert before == after
        payload = report.model_dump(mode="json")
        assert json.loads(json.dumps(payload, ensure_ascii=False))["summary"]["records_total"] == 2
        assert "recommended_ingestion_policy" in payload
        assert "manual_decisions_required" in payload


def test_cli_fails_clearly_when_data_root_not_configured(monkeypatch, capsys):
    monkeypatch.delenv("DATA_ROOT", raising=False)
    exit_code = main([])
    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "NOT_CONFIGURED"
    assert payload["report_path"] is None


def test_cli_writes_json_and_markdown_reports_without_indexing(capsys):
    with TemporaryDirectory() as tmp, TemporaryDirectory() as output:
        _write(Path(tmp), [VALID])
        exit_code = main(["--data-root", tmp, "--output-dir", output])
        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        json_path = Path(payload["report_json"])
        markdown_path = Path(payload["report_markdown"])
        assert json_path.exists()
        assert markdown_path.exists()
        assert json.loads(json_path.read_text(encoding="utf-8"))["summary"]["records_total"] == 1
        assert "语义审计" in markdown_path.read_text(encoding="utf-8")
        assert not list(Path(tmp).glob("*.db"))
