"""人工审核 Markdown -> 正式 Gold 固化 测试。"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from water_agent.curation.gold_finalize import (
    GoldFinalizeError,
    build_curated_article_index,
    finalize_gold,
    parse_review_markdown,
)


def _write_curated(root: Path, records: list[dict]) -> None:
    out = root / "clauses"
    out.mkdir(parents=True, exist_ok=True)
    by_spec: dict[str, list[dict]] = {}
    for record in records:
        by_spec.setdefault(record["spec_id"], []).append(record)
    for spec, rows in by_spec.items():
        path = out / f"{spec}.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _candidate(seq: int, spec: str, article: str) -> dict:
    return {
        "id": f"candidate-{seq:03d}",
        "query": f"关于{spec} {article}的条款，应如何执行？",
        "gold_articles_any": [f"{spec} {article}"],
        "gold_articles_all": [],
        "forbidden_articles": [],
        "category": "规定要求",
        "spec_id": spec,
        "article": article,
        "source_article": article,
        "source_chunk_ids": [f"chunk-{seq}"],
        "source_file": f"/tmp/clauses/{spec}.jsonl",
        "source_line": seq,
        "text_summary": f"{spec} {article} 摘要",
        "rationale": "test",
        "needs_multiple_articles": False,
        "is_gold": False,
        "review_status": "pending",
        "is_candidate": True,
        "status": "candidate",
    }


def _candidates_jsonl(path: Path, candidates: list[dict]) -> None:
    path.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in candidates), encoding="utf-8")


def _review_md(path: Path, decisions: list[tuple[str, str, str, str]]) -> None:
    lines = [
        "# 检索评测候选集（供人工审核）",
        "",
        "| id | query | gold_articles_any | category | source_article | 条款摘要 | 审核动作 | 修改后query | 修改后gold | 备注 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cand_id, action, edited_query, edited_gold in decisions:
        lines.append(
            f"| {cand_id} | 原问题 {cand_id} | GB50013-2018 5.1.1 | 规定要求 | 5.1.1 | 摘要 | {action} | {edited_query} | {edited_gold} | 备注 |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_review_markdown_extracts_actions():
    with TemporaryDirectory() as tmp:
        md = Path(tmp) / "review.md"
        _review_md(md, [
            ("candidate-001", "approve", "", ""),
            ("candidate-002", "edit", "修改后的问题", "GB50014-2021 3.1.2"),
            ("candidate-003", "reject", "", ""),
            ("candidate-004", "", "", ""),
        ])
        reviews = parse_review_markdown(md)
        assert reviews["candidate-001"].action == "approve"
        assert reviews["candidate-002"].action == "edit"
        assert reviews["candidate-002"].edited_query == "修改后的问题"
        assert reviews["candidate-002"].edited_gold == ["GB50014-2021 3.1.2"]
        assert reviews["candidate-003"].action == "reject"
        assert "candidate-004" not in reviews


def test_build_curated_article_index_accepts_allowed_specs_only():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_curated(root, [
            _candidate(1, "GB50013-2018", "5.1.1"),
            _candidate(2, "GB50014-2021", "3.1.1"),
            _candidate(3, "GB50318-2017", "3.1.2"),
            _candidate(4, "GB18918-2002", "4.1.1"),
            _candidate(5, "GB5749-2022", "6.1"),
        ])
        index = build_curated_article_index(root)
        assert ("GB50013-2018", "5.1.1") in index
        assert ("GB50014-2021", "3.1.1") in index
        assert ("GB50318-2017", "3.1.2") in index
        assert ("GB18918-2002", "4.1.1") not in index
        assert ("GB5749-2022", "6.1") not in index


def test_finalize_approve_keeps_original_query_and_gold():
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        cand = _candidate(1, "GB50013-2018", "5.1.1")
        _write_curated(base, [cand])
        _candidates_jsonl(base / "candidates.jsonl", [cand])
        _review_md(base / "review.md", [("candidate-001", "approve", "", "")])
        result = finalize_gold(
            candidates_path=base / "candidates.jsonl",
            review_md_path=base / "review.md",
            curated_root=base,
            gold_path=base / "gold.jsonl",
            manifest_path=base / "manifest.json",
            min_gold=1,
        )
        assert result["finalized_count"] == 1
        gold = json.loads((base / "gold.jsonl").read_text(encoding="utf-8").strip())
        assert gold["query"] == cand["query"]
        assert gold["gold_articles_any"] == ["GB50013-2018 5.1.1"]
        assert gold["is_gold"] is True
        assert gold["review_status"] == "approved"


def test_finalize_edit_uses_modified_query_and_gold():
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        cand = _candidate(1, "GB50013-2018", "5.1.1")
        _write_curated(base, [cand, _candidate(2, "GB50014-2021", "3.1.2")])
        _candidates_jsonl(base / "candidates.jsonl", [cand])
        _review_md(base / "review.md", [("candidate-001", "edit", "修改后的专业问题", "GB50014-2021 3.1.2")])
        result = finalize_gold(
            candidates_path=base / "candidates.jsonl",
            review_md_path=base / "review.md",
            curated_root=base,
            gold_path=base / "gold.jsonl",
            manifest_path=base / "manifest.json",
            min_gold=1,
        )
        gold = json.loads((base / "gold.jsonl").read_text(encoding="utf-8").strip())
        assert gold["query"] == "修改后的专业问题"
        assert gold["gold_articles_any"] == ["GB50014-2021 3.1.2"]
        assert gold["review_status"] == "edited"
        assert result["edit_count"] == 1


def test_finalize_reject_excludes_candidate():
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        cand = _candidate(1, "GB50013-2018", "5.1.1")
        _write_curated(base, [cand])
        _candidates_jsonl(base / "candidates.jsonl", [cand])
        _review_md(base / "review.md", [("candidate-001", "reject", "", "")])
        result = finalize_gold(
            candidates_path=base / "candidates.jsonl",
            review_md_path=base / "review.md",
            curated_root=base,
            gold_path=base / "gold.jsonl",
            manifest_path=base / "manifest.json",
            min_gold=0,
        )
        assert result["finalized_count"] == 0
        assert result["rejected_count"] == 1
        assert not (base / "gold.jsonl").exists()


def test_finalize_excludes_blank_query():
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        cand = _candidate(1, "GB50013-2018", "5.1.1")
        _write_curated(base, [cand])
        _candidates_jsonl(base / "candidates.jsonl", [cand])
        _review_md(base / "review.md", [("candidate-001", "edit", "", "GB50013-2018 5.1.1")])
        result = finalize_gold(
            candidates_path=base / "candidates.jsonl",
            review_md_path=base / "review.md",
            curated_root=base,
            gold_path=base / "gold.jsonl",
            manifest_path=base / "manifest.json",
            min_gold=0,
        )
        assert result["finalized_count"] == 0
        assert result["skipped_blank_query"] == 1


def test_finalize_excludes_disallowed_spec_gold():
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        cand = _candidate(1, "GB18918-2002", "4.1.1")
        _write_curated(base, [_candidate(9, "GB50013-2018", "5.1.1")])
        _candidates_jsonl(base / "candidates.jsonl", [cand])
        _review_md(base / "review.md", [("candidate-001", "approve", "", "")])
        result = finalize_gold(
            candidates_path=base / "candidates.jsonl",
            review_md_path=base / "review.md",
            curated_root=base,
            gold_path=base / "gold.jsonl",
            manifest_path=base / "manifest.json",
            min_gold=0,
        )
        assert result["finalized_count"] == 0
        assert result["skipped_disallowed_spec"] == 1


def test_finalize_fails_when_under_50():
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        cand = _candidate(1, "GB50013-2018", "5.1.1")
        _write_curated(base, [cand])
        _candidates_jsonl(base / "candidates.jsonl", [cand])
        _review_md(base / "review.md", [("candidate-001", "approve", "", "")])
        with pytest.raises(GoldFinalizeError, match="50"):
            finalize_gold(
                candidates_path=base / "candidates.jsonl",
                review_md_path=base / "review.md",
                curated_root=base,
                gold_path=base / "gold.jsonl",
                manifest_path=base / "manifest.json",
            )
        assert not (base / "gold.jsonl").exists()


def test_manifest_reports_distribution_and_duplicates():
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        specs = ["GB50013-2018"] * 6 + ["GB50014-2021"] * 6 + ["GB50318-2017"] * 6
        articles = (
            [f"5.1.{i}" for i in range(1, 7)]
            + [f"3.1.{i}" for i in range(1, 7)]
            + [f"3.1.{i}" for i in range(1, 7)]
        )
        candidates = [
            _candidate(i, spec, article) for i, (spec, article) in enumerate(zip(specs, articles), start=1)
        ]
        candidates[1]["query"] = candidates[0]["query"]
        _write_curated(base, candidates)
        _candidates_jsonl(base / "candidates.jsonl", candidates)
        decisions = [(c["id"], "approve", "", "") for c in candidates[:18]]
        _review_md(base / "review.md", decisions)
        result = finalize_gold(
            candidates_path=base / "candidates.jsonl",
            review_md_path=base / "review.md",
            curated_root=base,
            gold_path=base / "gold.jsonl",
            manifest_path=base / "manifest.json",
            min_gold=1,
        )
        manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["by_spec"]["GB50013-2018"] == 5
        assert manifest["by_spec"]["GB50014-2021"] == 6
        assert manifest["by_spec"]["GB50318-2017"] == 6
        assert manifest["total"] == 17
        assert result["approve_count"] == 17
        assert manifest["duplicate_query_ids"] == ["candidate-002"]
        assert manifest["duplicate_id_ids"] == []
