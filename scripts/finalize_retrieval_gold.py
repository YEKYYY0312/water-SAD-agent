"""把人工确认的候选固化为正式 gold 评测集。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from water_agent.ingestion.gold_set import finalize_gold_set


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Harden human-confirmed retrieval candidates into a gold eval set")
    parser.add_argument("--candidates", required=True, help="candidate JSON produced by generate_retrieval_candidates.py")
    parser.add_argument("--confirm-id", required=True, action="append", help="candidate id approved by human review (repeatable)")
    parser.add_argument("--output", default="data/eval/retrieval_gold_50.jsonl")
    parser.add_argument("--manifest", default="data/eval/retrieval_gold.manifest.json")
    args = parser.parse_args(argv)

    source = Path(args.candidates)
    if not source.exists():
        print(json.dumps({"ok": False, "status": "missing_candidates", "message": str(source)}, ensure_ascii=False))
        return 2
    manifest = finalize_gold_set(
        source,
        human_confirmed_ids=args.confirm_id,
        gold_path=args.output,
        manifest_path=args.manifest,
    )
    print(json.dumps({"ok": True, "status": "gold_hardened", "finalized_count": manifest["finalized_count"], "gold_path": args.output, "skipped_unmatched_ids": manifest["skipped_unmatched_ids"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())