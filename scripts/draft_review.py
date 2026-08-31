"""生成预填 approve 的人工审核草稿表（不改正式 md，供你复核）。

正式固化仍以你最终确认的动作为准；草稿只是减少手填工作量。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Draft a review table with all candidates pre-filled as approve")
    p.add_argument("--candidates", default="data/eval/retrieval_candidates_70.jsonl")
    p.add_argument("--out", default="data/eval/retrieval_candidates_70.review_draft.md")
    args = p.parse_args(argv)

    candidates = [json.loads(l) for l in Path(args.candidates).read_text(encoding="utf-8").splitlines() if l.strip()]
    lines = [
        "# 检索评测候选集（审核草稿：全部预填 approve，待你复核）",
        "",
        f"- 候选数量：{len(candidates)}",
        "- 动作列已全部预填 `approve`：直接采用 = 保留原 query/gold；"
        "如要改请把该行动作改为 `edit` 并填「修改后query / 修改后gold」，或改为 `reject`。",
        "- 确认后运行：`_venv/bin/python scripts/finalize_gold.py`（默认读取正式审核表，需你同步填表）。",
        "",
        "| id | query | gold_articles_any | category | source_article | 条款摘要 | 审核动作 | 修改后query | 修改后gold | 备注 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in candidates:
        summary = c.get("text_summary", "")[:40].replace("|", "/")
        lines.append(
            f"| {c['id']} | {c['query']} | {';'.join(c['gold_articles_any'])} | {c['category']} | "
            f"{c.get('source_article','')} | {summary} | approve |  |  |  |"
        )
    out = Path(args.out)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "out": str(out), "rows": len(candidates)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
