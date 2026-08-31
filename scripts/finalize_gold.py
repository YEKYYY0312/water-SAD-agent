"""人工审核 Markdown → 正式 Gold 固化 CLI。"""
from __future__ import annotations

import argparse
import json
import sys

from water_agent.curation.gold_finalize import GoldFinalizeError, finalize_gold


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Harden human-reviewed candidates into formal gold")
    p.add_argument("--candidates", default="data/eval/retrieval_candidates_70.jsonl")
    p.add_argument("--review-md", default="data/eval/retrieval_candidates_70.md")
    p.add_argument("--curated-root", default="data/curated")
    p.add_argument("--gold-out", default="data/eval/retrieval_gold.jsonl")
    p.add_argument("--manifest-out", default="data/eval/retrieval_gold_manifest.json")
    p.add_argument("--min-gold", type=int, default=50)
    args = p.parse_args(argv)

    try:
        result = finalize_gold(
            candidates_path=args.candidates,
            review_md_path=args.review_md,
            curated_root=args.curated_root,
            gold_path=args.gold_out,
            manifest_path=args.manifest_out,
            min_gold=args.min_gold,
        )
    except GoldFinalizeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
