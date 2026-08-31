"""从受控语料生成候选检索集，供人工审核后固化。"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from water_agent.ingestion.retrieval_candidates import build_candidate_manifest, read_curated_records, select_candidates
from water_agent.ingestion.data_root import resolve_data_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate candidate retrieval questions for human review")
    parser.add_argument("--curated-root", default="data/curated", help="root of the curated corpus (must contain clauses/)")
    parser.add_argument("--count", type=int, default=70)
    parser.add_argument("--output", default="data/eval/retrieval_candidates.json")
    args = parser.parse_args(argv)

    curated = Path(args.curated_root)
    if not (curated / "clauses").is_dir():
        print(json.dumps({"ok": False, "status": "no_curated_clauses", "message": f"missing {curated / 'clauses'}"}, ensure_ascii=False))
        return 2
    rows = read_curated_records(curated)
    if not rows:
        print(json.dumps({"ok": False, "status": "no_records", "message": "no curated records found"}, ensure_ascii=False))
        return 2
    candidates = select_candidates(rows, count=args.count)
    manifest = build_candidate_manifest(candidates, curated_root=str(curated), output_path=args.output)
    print(json.dumps({
        "ok": True,
        "status": "ok",
        "output": args.output,
        "total_candidates": len(candidates),
        "by_spec": dict(Counter(c["spec_id"] for c in candidates)),
        "review_command": f"_venv/bin/python scripts/finalize_retrieval_gold.py --candidates {args.output} --confirm-your-ids-here",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())