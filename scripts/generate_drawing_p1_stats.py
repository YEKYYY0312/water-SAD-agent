#!/usr/bin/env python3
"""生成 drawing_p1 候选标注统计报告（诚实版）。"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).parent.parent
ANNOTATIONS_DIR = REPO_ROOT / "data" / "eval" / "drawing_p1" / "annotations"
OUTPUT_README = REPO_ROOT / "data" / "eval" / "drawing_p1" / "README.md"
JSONL = REPO_ROOT / "data" / "eval" / "drawing_p1.jsonl"


def main() -> int:
    files = sorted(ANNOTATIONS_DIR.glob("*.json"))
    if not files:
        print(f"无标注文件: {ANNOTATIONS_DIR}")
        return 2
    by_type: dict[str, list[Any]] = {}
    cat: Counter[str] = Counter()
    conf: Counter[str] = Counter()
    total = 0
    rows: list[tuple[str, str, int]] = []
    for fp in files:
        ann = json.loads(fp.read_text(encoding="utf-8"))
        ds = ann.get("defects", [])
        total += len(ds)
        by_type.setdefault(ann.get("drawing_type", "未知"), []).append((ann["drawing_id"], len(ds)))
        for d in ds:
            cat[d.get("category", "?")] += 1
            conf[d.get("confidence", "?")] += 1
        rows.append((ann["drawing_id"], ann.get("drawing_type", "?"), len(ds)))

    lines = [
        "# Drawing P1 阴性样本集（CAD 目视确认无违规，仅测误报率）",
        "",
        "**状态**: 2026-09-16 CAD 目视终判：18 张图纸本身无规范违规，28 条文本候选全部撤回，确认违规 0 条。",
        "本目录产物 `accepts_recall_gate=false`：仅可做误报率（false-positive）测试，**不可做召回率门禁**。",
        "",
        "## 统计",
        "",
        f"- 图纸数: {len(files)}",
        f"- 文本候选总数: {total}（全部撤回）",
        "- 确认违规: 0",
        f"- 信心度分布: {dict(conf)}",
        f"- 分类分布: {dict(cat)}",
        "",
        "### 按图纸类型",
        "",
        "| 类型 | 图纸数 | 候选数 |",
        "|---|---|---|",
    ]
    for t, items in sorted(by_type.items()):
        lines.append(f"| {t} | {len(items)} | {sum(n for _, n in items)} |")
    lines += ["", "### 逐图纸", "", "| 图纸 | 类型 | 候选数 |", "|---|---|---|"]
    for did, t, n in sorted(rows):
        lines.append(f"| {did} | {t} | {n} |")
    lines += [
        "",
        "## 目视终判（2026-09-16）",
        "",
        "- 28 条文本候选全部撤回：7 条文本复核删除（配件规格/设备接口/孤立文本）+ 21 条 CAD 目视判误报。",
        "- 确认违规 0 条；本批转阴性误报集，链路若报警计误报。召回率门禁仍 blocked，需另采带已知缺陷图纸。",
        "- 终判记录见 `REVIEW.md`；撤回明细见 `drawing_p1_negatives.jsonl`（`review_status=dropped_by_visual_review`）。",
        "",
        "## 文件",
        "",
        "- `annotations/*.json`：18 张图纸的文本候选标注（复核前快照，`needs_manual_review=true`，已全部撤回）。",
        f"- `{JSONL.name}`：复核前候选汇总（28 行，历史快照，不做 gold 用）。",
        "- `drawing_p1_negatives.jsonl`：28 条撤回候选（`review_status=dropped_by_visual_review` + 撤回原因）。",
        "- `../drawing_p1_gold.jsonl`：0 条确认缺陷占位（`review_status=visual_review_clean`）。",
        "- `REVIEW.md`：复核记录（含 CAD 目视终判）。",
        "- 本 README：统计与终判说明。",
        "",
    ]
    OUTPUT_README.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_README.write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ 统计报告: {OUTPUT_README}")
    print(f"   图纸 {len(files)} 张，候选 {total} 条，分类 {dict(cat)}，信心度 {dict(conf)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
