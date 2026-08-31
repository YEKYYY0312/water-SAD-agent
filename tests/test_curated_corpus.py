"""P1 增量 3B：受控正式语料集构建。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from water_agent.ingestion.curated_corpus import build_curated_corpus


VALID = {
    "spec_id": "GB50013-2018",
    "chapter": "4",
    "section": "4.4",
    "article": "4.4.2",
    "title": "给水系统设计",
    "text": "给水系统设计应符合安全、卫生和经济合理的要求。",
}


def _write(root: Path, name: str, records: list[dict]) -> Path:
    path = root / "clauses" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    return path


def _audit(root: Path, audit_rows: list[dict]) -> Path:
    path = root / "audit.json"
    path.write_text(json.dumps({"summary": {}, "records": audit_rows}, ensure_ascii=False), encoding="utf-8")
    return path


def _audit_row(path: Path, line: int, record: dict, labels: list[str]) -> dict:
    return {
        "source_file": str(path),
        "line_number": line,
        "spec_id": record["spec_id"],
        "article": record["article"],
        "chapter": record.get("chapter"),
        "section": record.get("section"),
        "title": record.get("title", ""),
        "text_preview": record.get("text", "")[:180],
        "content_hash": "audit-hash",
        "categories": labels,
        "primary_category": labels[0] if labels else "normal_article",
        "recommended_strategy": "normal_parent_child_ingestion",
        "recommendation_reason": "test",
    }


def test_includes_normal_articles_and_preserves_original_fields():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _write(root, "GB50013-2018.jsonl", [VALID])
        audit = _audit(root, [_audit_row(source, 1, VALID, ["normal_article"])])
        result = build_curated_corpus(root, audit, root / "out")
        output = json.loads((root / "out" / "clauses" / "GB50013-2018.jsonl").read_text())
        assert output["article"] == VALID["article"]
        assert output["text"] == VALID["text"]
        assert output["ingestion_decision"] == "include"
        assert output["source_line"] == 1
        assert output["fragment_id"]
        assert result.manifest["records_included"] == 1


def test_preserves_legal_duplicate_fragments_and_stable_fragment_ids():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        second = {**VALID, "text": "同一条款的第二个合法正文分片，补充适用条件和设计边界。", "spec_id": "GB50014-2021"}
        source = _write(root, "GB50014-2021.jsonl", [{**VALID, "spec_id": "GB50014-2021"}, second])
        audit = _audit(root, [
            _audit_row(source, 1, VALID, ["normal_article", "duplicate_article_variant"]),
            _audit_row(source, 2, second, ["normal_article", "duplicate_article_variant"]),
        ])
        first = build_curated_corpus(root, audit, root / "out")
        second_build = build_curated_corpus(root, audit, root / "out")
        rows = [json.loads(line) for line in (root / "out" / "clauses" / "GB50014-2021.jsonl").read_text().splitlines()]
        assert len(rows) == 2
        assert rows[0]["fragment_id"] == rows[0]["fragment_id"]
        assert [r["fragment_id"] for r in rows] == [
            r["fragment_id"] for r in [json.loads(line) for line in (root / "out" / "clauses" / "GB50014-2021.jsonl").read_text().splitlines()]
        ]
        assert first.manifest["duplicate_article_groups_preserved"] == 1
        assert second_build.manifest["records_included"] == 2


def test_only_valid_article_table_fragment_is_included():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        table = {**VALID, "spec_id": "GB50014-2021", "text": "| 管径 | 充满度 |\n|---|---|\n| DN200 | 0.55 |"}
        bad = {**VALID, "article": "0.01", "text": table["text"]}
        source = _write(root, "GB50014-2021.jsonl", [table, bad])
        audit = _audit(root, [
            _audit_row(source, 1, table, ["table_fragment"]),
            _audit_row(source, 2, bad, ["table_fragment", "invalid_article_format"]),
        ])
        result = build_curated_corpus(root, audit, root / "out")
        rows = [json.loads(line) for line in (root / "out" / "clauses" / "GB50014-2021.jsonl").read_text().splitlines()]
        assert len(rows) == 1
        assert rows[0]["article"] == VALID["article"]
        assert result.manifest["quarantined_by_reason"]["invalid_article_format"] == 1


def test_excluded_specs_are_all_quarantined():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        records = [dict(VALID, spec_id=spec, article="1.1") for spec in ["GB18918-2002", "GB5749-2022", "GBT50104-2010"]]
        paths = [_write(root, f"{record['spec_id']}.jsonl", [record]) for record in records]
        audit = _audit(root, [_audit_row(path, 1, record, ["normal_article"]) for path, record in zip(paths, records)])
        result = build_curated_corpus(root, audit, root / "out")
        assert result.manifest["records_included"] == 0
        assert result.manifest["records_quarantined"] == 3
        assert not list((root / "out" / "clauses").glob("GB18918-2002.jsonl"))
        assert len((root / "out" / "quarantine.jsonl").read_text().splitlines()) == 3


def test_abnormal_labels_are_quarantined_and_source_hash_unchanged():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        records = [
            dict(VALID, article="0.5", text="OCR 断裂内容 ###"),
            dict(VALID, article="4.4.3", text="--- ### |||"),
        ]
        source = _write(root, "GB50013-2018.jsonl", records)
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        audit = _audit(root, [
            _audit_row(source, 1, records[0], ["invalid_article_format", "ocr_suspect"]),
            _audit_row(source, 2, records[1], ["empty_or_low_signal"]),
        ])
        result = build_curated_corpus(root, audit, root / "out")
        after = hashlib.sha256(source.read_bytes()).hexdigest()
        assert before == after
        assert result.manifest["records_included"] == 0
        assert result.manifest["records_quarantined"] == 2
        quarantine = json.loads((root / "out" / "quarantine.jsonl").read_text().splitlines()[0])
        assert quarantine["source_line"] == 1
        assert quarantine["audit_labels"]
        assert quarantine["quarantine_reason"]


def test_manifest_is_json_serializable_and_no_db_created():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _write(root, "GB50318-2017.jsonl", [dict(VALID, spec_id="GB50318-2017")])
        audit = _audit(root, [_audit_row(source, 1, dict(VALID, spec_id="GB50318-2017"), ["normal_article"])])
        build_curated_corpus(root, audit, root / "out")
        manifest = json.loads((root / "out" / "manifest.json").read_text())
        assert manifest["included_specs"] == ["GB50318-2017"]
        assert manifest["content_hash"]
        assert not list((root / "out").rglob("*.db"))
