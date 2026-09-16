#!/usr/bin/env python3
"""Finalize drawing_p1 as a negative (false-positive) set.

2026-09-16 CAD visual review verdict: all 21 previously kept
low-confidence candidates are false positives; confirmed violations = 0.

Review trail (per defect):
- 7 dropped at text review (pump accessory specs, equipment
  connection sizes, context-free literals) — reasons in TEXT_DROPS.
- 21 dropped at CAD visual review (P-01 9, P-07 10, S-03 1, S-04 1).

Outputs:
- data/eval/drawing_p1_gold.jsonl        -> single placeholder line, 0 defects
- data/eval/drawing_p1/REVIEW.md         -> rewritten with visual verdict
- data/eval/drawing_p1/README.md         -> status header updated (keep table)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).parent.parent
EVAL_DIR = REPO_ROOT / "data" / "eval" / "drawing_p1"
GOLD_JSONL = REPO_ROOT / "data" / "eval" / "drawing_p1_gold.jsonl"
NEGATIVE_JSONL = EVAL_DIR / "negative_set.jsonl"
REVIEW_MD = EVAL_DIR / "REVIEW.md"
README_MD = EVAL_DIR / "README.md"
CANDIDATE_JSONL = REPO_ROOT / "data" / "eval" / "drawing_p1.jsonl"

TEXT_DROPS: dict[str, str] = {
    "P-06-D001": "文本复核：DN37 孤立文本，处潜水泵/污泥池/调节池注释之间，无系统上下文",
    "PUMP-01-D001": "文本复核：泵房配件规格（DN32 PN1.0MPa / DN50x32 变径），非系统管径",
    "PUMP-01-D002": "文本复核：DN50x40 L=300 等配件加工长度，非系统管径",
    "PUMP-01-D003": "文本复核：DN80x50/DN80 PN1.0MPa 配件规格与压力等级，非系统管径违规证据",
    "S-01-D001": "文本复核：DN32 邻自来水 P>=0.15MPa，工艺流程图，立管/支管与室内外不明",
    "S-03-D001": "文本复核：喷嘴DN25 为设备接口尺寸，非管段",
    "S-03-D002": "文本复核：LZD转子流量计DN50 为设备接口尺寸，非管段",
}

# 目视撤回（原“保留待复核”21 条）：按图纸整批判无问题
VISUAL_DROP_BY_DRAWING = {
    "P-01": "目视复核（2026-09-16）：管径/材料/坡度文本观测经 CAD 目视确认合规，误报撤回",
    "P-07": "目视复核（2026-09-16）：DN25~DN75 为支管/设备接管，同图 DN100/DN150 干管，合规，误报撤回",
    "S-03": "目视复核（2026-09-16）：UPVC 管线经 CAD 确认为正常工艺管线，非热水/消防，误报撤回",
    "S-04": "目视复核（2026-09-16）：DN150 供气主管/ DN80 支管为供气系统正常配置，误报撤回",
}


def drop_reason_for(did: str, defect_id: str) -> tuple[str, str]:
    if defect_id in TEXT_DROPS:
        return "text_review", TEXT_DROPS[defect_id]
    stage = "visual_review"
    reason = VISUAL_DROP_BY_DRAWING.get(
        did, "目视复核（2026-09-16）：CAD 图纸无规范违规，误报撤回"
    )
    return stage, reason


def main() -> int:
    rows = [json.loads(l) for l in CANDIDATE_JSONL.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 28, f"候选数应为 28，实际 {len(rows)}"
    by_drawing: dict[str, int] = {}
    negative_rows: list[dict[str, Any]] = []
    for r in rows:
        did = r["drawing_id"]
        by_drawing[did] = by_drawing.get(did, 0) + 1
        stage, reason = drop_reason_for(did, r["defect_id"])
        negative_rows.append(
            {
                "drawing_id": did,
                "defect_id": r["defect_id"],
                "review_stage": stage,
                "drop_reason": reason,
                "false_positive_of": r["candidate_defect"],
                "metadata": r["metadata"],
                "review_status": "dropped_false_positive",
                "real_samples": True,
                "accepts_recall_gate": False,
            }
        )
    text_count = sum(1 for x in negative_rows if x["review_stage"] == "text_review")
    visual_count = len(negative_rows) - text_count
    assert (text_count, visual_count) == (7, 21), (text_count, visual_count)

    NEGATIVE_JSONL.write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in negative_rows), encoding="utf-8"
    )
    placeholder: dict[str, Any] = {
        "drawing_id": None,
        "defect_id": None,
        "gold_defects": [],
        "review_status": "visual_review_closed_no_defects",
        "review_note": "2026-09-16 CAD 目视复核：18 张图纸均未发现规范违规；0 条 gold 缺陷。仅可作阴性误报集（drawing_p1/negative_set.jsonl），不可作召回率门禁。",
        "real_samples": True,
        "accepts_recall_gate": False,
    }
    GOLD_JSONL.write_text(json.dumps(placeholder, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# Drawing P1 复核记录（终：目视复核关闭，0 缺陷）",
        "",
        "**结论（2026-09-16）**：18 张真实图纸经 CAD 目视复核，**均未发现规范违规**。",
        "文本候选 28 条全部撤回：文本复核删 7 条（配件规格/设备接口/孤立文本），目视复核删 21 条（管径/材料/坡度观测均合规）。",
        "",
        "## 用途裁定",
        "",
        "- `data/eval/drawing_p1_gold.jsonl`：0 条 gold 缺陷占位（`review_status=visual_review_closed_no_defects`）。",
        "- `data/eval/drawing_p1/negative_set.jsonl`：28 条误报负例，用于误报率（FP）测试。",
        "- 两文件 `accepts_recall_gate=false`：**不构成召回率验收证据**，不可作 recall 分母。",
        "- 召回率门禁仍需带已知缺陷图纸或植入缺陷副本（blocked）。",
        "",
        "## 口径提醒",
        "",
        "- 目视对象为 DWG/DXF 图纸本身；P3 链路读 ODA 转换件且不展开 INSERT（MANIFEST：含 INSERT 16/18），链路盲区仍在。",
        f"- 逐图纸候选数: {json.dumps(by_drawing, ensure_ascii=False)}",
        "",
    ]
    REVIEW_MD.write_text("\n".join(lines), encoding="utf-8")

    readme = README_MD.read_text(encoding="utf-8")
    old_header = """# Drawing P1 候选标注集（待人工复核，非 gold）

**状态**: 启发式文本扫描 + P3 程序化管线产出的候选标注，全部 `needs_manual_review=true`。
转评测 gold 前必须人工复核；本目录产物 `accepts_recall_gate=false`，**不构成召回率验收证据**。"""
    new_header = """# Drawing P1 候选标注集（终：目视复核关闭，0 缺陷，转阴性误报集）

**状态（2026-09-16 目视复核终判）**: 18 张图纸 CAD 目视均无规范违规；28 条文本候选全部撤回
（7 条文本复核删 + 21 条目视复核删）。本目录转**阴性误报集**（`negative_set.jsonl`），
`drawing_p1_gold.jsonl` 为 0 缺陷占位；两产物 `accepts_recall_gate=false`，
**不构成召回率验收证据**，仅可作误报率（FP）测试。"""
    if old_header not in readme:
        raise AssertionError("README 表头与预期不一致，拒绝盲改")
    readme = readme.replace(old_header, new_header)
    tail_marker = "## 复核指引"
    if tail_marker in readme:
        head, _sep, _rest = readme.partition(tail_marker)
        readme = head + (
            "## 复核记录（终）\n\n"
            "- 2026-09-16 文本复核：删 7 条（P-06 DN37、PUMP-01 DN32/50/80、S-01 DN32、S-03 喷嘴DN25/流量计DN50）。\n"
            "- 2026-09-16 CAD 目视复核：其余 21 条观测全部判合规撤回；18 张图纸 0 违规。\n"
            "- 产物：`negative_set.jsonl`（28 条误报负例，`review_status=dropped_false_positive`）；"
            "`../drawing_p1_gold.jsonl`（0 缺陷占位，`review_status=visual_review_closed_no_defects`）。\n"
            "- 详见 `REVIEW.md`。INSERT 块内文字对链路仍不可见（MANIFEST：含 INSERT 16/18）。\n\n"
            "## 文件\n\n"
            "- `annotations/*.json`：18 张图纸的候选标注快照（保留原文，便于回溯候选来源）。\n"
            "- `negative_set.jsonl`：28 条误报负例（误报率测试用）。\n"
            "- `drawing_p1.jsonl`：复核前候选汇总（历史快照，不做评测用）。\n"
            "- `REVIEW.md`：复核记录（含目视终判）。\n"
            "- 本 README：统计与终判说明。\n"
        )
    README_MD.write_text(readme, encoding="utf-8")
    print(f"negative_set: {NEGATIVE_JSONL} ({len(negative_rows)} 行)")
    print(f"gold placeholder: {GOLD_JSONL} (0 缺陷)")
    print(f"review: {REVIEW_MD}")
    print("README 状态已更新为目视复核终判")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
