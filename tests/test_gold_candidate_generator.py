"""候选检索问题生成器测试：不伪造正式 gold。"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from water_agent.curation.gold_candidate_generator import generate_candidate_queries


def _write_clause(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def test_generates_candidates_for_single_article():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "clauses").mkdir(parents=True)
        _write_clause(
            root / "clauses" / "GB50014-2021.jsonl",
            {
                "spec_id": "GB50014-2021",
                "chapter": "4",
                "section": "4.4",
                "article": "4.4.2",
                "title": "污水管道水力计算",
                "text": "污水管道的最大设计充满度应符合规定，并结合管径和流量确定。",
                "fragment_id": "frag-1",
                "source_line": 1,
            },
        )
        candidates = generate_candidate_queries(root, output=_candidates_path(root))
        assert candidates, "expected at least one candidate"
        first = candidates[0]
        assert first["is_candidate"] is True
        assert first["status"] == "candidate"
        assert "GB50014-2021 4.4.2" in first["gold_articles_any"]
        assert first["id"].startswith("candidate-")
        assert first["source"]["article"] == "4.4.2"


def test_candidate_never_fabricates_non_included_spec():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "clauses").mkdir(parents=True)
        _write_clause(
            root / "clauses" / "GB5749-2022.jsonl",
            {
                "spec_id": "GB5749-2022",
                "chapter": "3",
                "section": "3.1",
                "article": "3.1.1",
                "title": "限值",
                "text": "某污染物的饮用水限值为 0.01 mg/L。",
                "fragment_id": "frag-x",
                "source_line": 1,
            },
        )
        candidates = generate_candidate_queries(root, output=_candidates_path(root))
        assert all("GB5749-2022" not in " ".join(c["gold_articles_any"]) for c in candidates)


def _candidates_path(root: Path) -> Path:
    return root / "candidates.jsonl"