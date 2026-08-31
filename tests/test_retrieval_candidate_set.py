"""候选检索集生成与人工固化契约。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from water_agent.ingestion.retrieval_candidates import (
    read_curated_records,
    select_candidates,
)


VALID = {
    "spec_id": "GB50014-2021",
    "chapter": "4",
    "section": "4.4",
    "article": "4.4.2",
    "title": "污水管道水力计算",
    "text": "污水管道的最大设计充满度应按下列规定取值，并结合管径和流量确定。",
    "fragment_id": "frag-A",
    "audit_labels": ["normal_article"],
}


def _write_curated(root: Path, records: list[dict]) -> None:
    out = root / "clauses" / "GB50014-2021.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


def test_read_curated_records_flattens_files():
    with TemporaryDirectory() as tmp:
        _write_curated(Path(tmp), [VALID])
        rows = read_curated_records(Path(tmp))
        assert len(rows) == 1
        assert rows[0]["article"] == "4.4.2"


def test_select_candidates_marks_candidate_and_not_gold():
    with TemporaryDirectory() as tmp:
        _write_curated(Path(tmp), [VALID])
        rows = read_curated_records(Path(tmp))
        candidates = select_candidates(rows, count=1)
        assert len(candidates) == 1
        row = candidates[0]
        assert row["is_candidate"] is True
        assert row["status"] == "candidate"
        assert "gold_articles_any" not in row
        assert "candidate_gold_articles" in row


def test_candidate_set_within_target_range():
    with TemporaryDirectory() as tmp:
        rows = [
            {**VALID, "article": f"4.4.{i}", "text": f"采样条款正文 {i}", "fragment_id": f"frag-{i}"}
            for i in range(1, 80)
        ]
        _write_curated(Path(tmp), rows)
        loaded = read_curated_records(Path(tmp))
        result = select_candidates(loaded, count=70)
        assert 60 <= len(result) <= 70


def test_generator_does_not_modify_curated_source():
    with TemporaryDirectory() as tmp:
        _write_curated(Path(tmp), [VALID])
        before = hashlib.sha256((Path(tmp) / "clauses" / "GB50014-2021.jsonl").read_bytes()).hexdigest()
        read_curated_records(Path(tmp))
        after = hashlib.sha256((Path(tmp) / "clauses" / "GB50014-2021.jsonl").read_bytes()).hexdigest()
        assert before == after