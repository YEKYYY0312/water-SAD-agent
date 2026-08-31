"""P1 增量 3D：从受控语料生成 70 条人工审核候选集（不固化为 gold）。"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from water_agent.curation.gold_candidate_generator import (
    generate_candidate_queries,
    SPEC_TARGET,
)
from water_agent.curation.candidate_quality import validate_candidate_set


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate 70 retrieval candidate queries for human review")
    parser.add_argument("--curated-root", default="data/curated")
    parser.add_argument("--output", default="data/eval/retrieval_candidates_70.jsonl")
    args = parser.parse_args(argv)

    candidates = generate_candidate_queries(args.curated_root, output=args.output, target=dict(SPEC_TARGET))
    md_path = Path(args.output).with_suffix(".md")
    issues = validate_candidate_set(candidates, curated_root=args.curated_root)
    fatal = [i for i in issues if i.get("fatal")]
    report = {
        "ok": not fatal,
        "total_candidates": len(candidates),
        "by_spec": dict(Counter(c["spec_id"] for c in candidates)),
        "by_category": dict(Counter(c["category"] for c in candidates)),
        "jsonl": args.output,
        "markdown": str(md_path),
        "fatal_issues": fatal,
        "message": "候选集已生成，is_gold=false，等待人工审核。",
    }
    (Path(args.output).parent / (Path(args.output).stem + "_report.json")).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not fatal else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
