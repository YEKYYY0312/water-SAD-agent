"""P1 增量 3D：候选检索集质量契约（不允许自动固化为 gold）。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from water_agent.curation.gold_candidate_generator import generate_candidate_queries
from water_agent.curation.candidate_quality import validate_candidate_set


CURATED = {
    "GB50013-2018": [
        {
            "spec_id": "GB50013-2018", "chapter": "4", "section": "4.4", "article": "4.4.2",
            "title": "生活给水管道设计秒流量",
            "text": "生活给水管道的设计秒流量应按最大小时流量和设计秒流量确定，结合用水定额与用水人数计算。",
            "fragment_id": "f13-1", "source_file": "GB50013-2018.jsonl", "source_line": 1,
            "audit_labels": ["normal_article"],
        },
        {
            "spec_id": "GB50013-2018", "chapter": "4", "section": "4.7", "article": "4.7.3",
            "title": "给水管径",
            "text": "给水管道的公称直径不应小于DN15，支管与户内管的最小管径应符合相关规定。",
            "fragment_id": "f13-2", "source_file": "GB50013-2018.jsonl", "source_line": 2,
            "audit_labels": ["normal_article"],
        },
    ],
    "GB50014-2021": [
        {
            "spec_id": "GB50014-2021", "chapter": "4", "section": "4.4", "article": "4.4.2",
            "title": "污水管道最大设计充满度",
            "text": "污水管道的最大设计充满度应按下列规定取值，结合管径和流量确定。",
            "fragment_id": "f14-1", "source_file": "GB50014-2021.jsonl", "source_line": 1,
            "audit_labels": ["normal_article"],
        },
    ],
    "GB50318-2017": [
        {
            "spec_id": "GB50318-2017", "chapter": "5", "section": "5.1", "article": "5.1.1",
            "title": "泵站布置要求",
            "text": "泵站应尽量靠近主要用水区域，减少输水管长，且应便于进出水与运行管理。",
            "fragment_id": "f18-1", "source_file": "GB50318-2017.jsonl", "source_line": 1,
            "audit_labels": ["normal_article"],
        },
    ],
}


def _write_curated(root: Path) -> None:
    for spec, records in CURATED.items():
        out = root / "clauses" / f"{spec}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")


def test_candidates_have_3d_fields_and_not_gold():
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "curated"
        _write_curated(root)
        out_file = Path(tmp) / "retrieval_candidates_70.jsonl"
        candidates = generate_candidate_queries(root, output=out_file)
        assert candidates
        for c in candidates:
            assert c["is_gold"] is False
            assert c["review_status"] == "pending"
            assert c["id"].startswith("candidate-")
            assert "category" in c
            assert "rationale" in c
            assert "source_article" in c
            assert "spec_id" in c


def test_query_not_equal_to_title():
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "curated"
        _write_curated(root)
        candidates = generate_candidate_queries(root)
        for c in candidates:
            assert c["query"].strip() != c["title"].strip()


def test_query_does_not_fabricate_conditions_not_in_text():
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "curated"
        _write_curated(root)
        candidates = generate_candidate_queries(root)
        for c in candidates:
            record = next(r for r in CURATED[c["spec_id"]] if r["article"] == c["source_article"])
            q = c["query"]
            forbidden = ["DN300", "0.55", "1.5m/s"]
            for word in forbidden:
                if word not in record["text"]:
                    assert word not in q


def test_validation_flags_missing_gold_and_spec():
    bad = [
        {"id": "candidate-001", "query": "", "gold_articles_any": ["GB50013-2018 4.4.2"], "category": "x", "spec_id": "GB50013-2018", "source_article": "4.4.2", "is_gold": False, "review_status": "pending"},
        {"id": "candidate-002", "query": "合法问题", "gold_articles_any": ["GB18918-2002 1.1"], "category": "x", "spec_id": "GB18918-2002", "source_article": "1.1", "is_gold": False, "review_status": "pending"},
    ]
    issues = validate_candidate_set(bad)
    assert any("empty" in issue["message"] for issue in issues)
    assert any("excluded_spec" in issue["message"] for issue in issues)


def test_validation_passes_clean_set():
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "curated"
        _write_curated(root)
        candidates = generate_candidate_queries(root)
        issues = validate_candidate_set(candidates, curated_root=root)
        assert not [i for i in issues if i.get("fatal")]
