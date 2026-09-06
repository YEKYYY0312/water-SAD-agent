"""人工固化评测用例契约。"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from water_agent.ingestion.gold_set import finalize_gold_set
from water_agent.ingestion.retrieval_candidates import read_curated_records, select_candidates

VALID = {
    "spec_id": "GB50014-2021",
    "chapter": "4",
    "section": "4.4",
    "article": "4.4.2",
    "title": "污水管道最大设计充满度",
    "text": "污水管道的最大设计充满度应按下列规定取值。",
    "fragment_id": "frag-A",
    "audit_labels": ["normal_article"],
}


def _write_candidate_file(root: Path, candidates: list[dict]) -> Path:
    path = root / "candidates.json"
    path.write_text(json.dumps({"type": "retrieval_candidates", "candidates": candidates, "total_candidates": len(candidates)}, ensure_ascii=False), encoding="utf-8")
    return path


def test_only_human_confirmed_ids_become_gold():
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "curated" / "clauses").mkdir(parents=True)
        (base / "curated" / "clauses" / "GB50014-2021.jsonl").write_text(
            json.dumps(VALID) + "\n", encoding="utf-8"
        )
        rows = read_curated_records(base / "curated")
        candidates = select_candidates(rows, count=1)
        candidate_file = _write_candidate_file(base, candidates)
        confirmed_id = candidates[0]["id"]
        manifest = finalize_gold_set(
            candidate_file,
            human_confirmed_ids=[confirmed_id, "does-not-exist"],
            gold_path=base / "gold.jsonl",
            manifest_path=base / "gold.manifest.json",
        )
        assert manifest["finalized_count"] == 1
        assert manifest["skipped_unmatched_ids"] == 1
        gold = [json.loads(line) for line in (base / "gold.jsonl").read_text().splitlines()]
        assert len(gold) == 1
        assert gold[0]["status"] == "gold"
        assert gold[0]["gold_articles_any"] == ["GB50014-2021 4.4.2"]


def test_no_confirmed_ids_produces_empty_gold_and_no_auto_upgrade():
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "curated" / "clauses").mkdir(parents=True)
        (base / "curated" / "clauses" / "GB50014-2021.jsonl").write_text(
            json.dumps(VALID) + "\n", encoding="utf-8"
        )
        rows = read_curated_records(base / "curated")
        candidates = select_candidates(rows, count=1)
        candidate_file = _write_candidate_file(base, candidates)
        finalize_gold_set(
            candidate_file,
            human_confirmed_ids=[],
            gold_path=base / "gold.jsonl",
            manifest_path=base / "gold.manifest.json",
        )
        assert (base / "gold.jsonl").read_text(encoding="utf-8").strip() == ""
        candidates_on_disk = json.loads(candidate_file.read_text(encoding="utf-8"))
        assert all(c["status"] == "candidate" for c in candidates_on_disk["candidates"])