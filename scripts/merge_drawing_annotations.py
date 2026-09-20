#!/usr/bin/env python3
"""汇总 drawing_p1 候选标注为 JSONL（诚实版）。

输入：data/eval/drawing_p1/annotations/*.json（auto_annotate_drawings.py 产出）
输出：data/eval/drawing_p1.jsonl（每缺陷一行）+ 打印统计

诚实性约束（铁律2）：
- 输出字段叫 candidate_defect，不叫 gold_defect；每行带
  review_status=pending_manual_review、accepts_recall_gate=False。
- 该 JSONL 不可直接作为 drawing_eval 的召回率分母，
  转 gold 前须人工复核（见输出 README）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).parent.parent
ANNOTATIONS_DIR = REPO_ROOT / "data" / "eval" / "drawing_p1" / "annotations"
OUTPUT_FILE = REPO_ROOT / "data" / "eval" / "drawing_p1.jsonl"


def merge_annotations(annotations_dir: Path, output_file: Path) -> int:
    all_cases: list[dict[str, Any]] = []
    for json_file in sorted(annotations_dir.glob("*.json")):
        annotation = json.loads(json_file.read_text(encoding="utf-8"))
        for defect in annotation.get("defects", []):
            all_cases.append(
                {
                    "drawing_id": annotation["drawing_id"],
                    "defect_id": defect["defect_id"],
                    "query": f"审查图纸 {annotation['drawing_id']}",
                    "candidate_defect": {
                        "category": defect["category"],
                        "description": defect["description"],
                        "regulation": defect["regulation"],
                        "location": defect["location"],
                        "expected": defect.get("expected"),
                        "actual": defect.get("actual"),
                        "occurrences": defect.get("occurrences", 1),
                    },
                    "metadata": {
                        "drawing_type": annotation.get("drawing_type"),
                        "severity": defect.get("severity"),
                        "confidence": defect.get("confidence"),
                        "needs_manual_review": True,
                        "rule_id": defect.get("rule_id"),
                    },
                    "review_status": "pending_manual_review",
                    "real_samples": True,
                    "accepts_recall_gate": False,
                }
            )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in all_cases),
        encoding="utf-8",
    )
    print(f"✅ 生成候选集: {output_file}")
    print(f"   总候选数: {len(all_cases)}")
    print("   ⚠️ 全部 pending_manual_review，不可当召回率 gold 使用。")
    return len(all_cases)


if __name__ == "__main__":
    merge_annotations(ANNOTATIONS_DIR, OUTPUT_FILE)
