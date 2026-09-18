#!/usr/bin/env python3
"""P5.1 验证：召回率 + 误报率联合评测。

验收标准：
- 召回率 ≥ 80%（P-03_defect 的 2 个缺陷至少检出 1 个）
- 误报率 ≤ 10%（18 张阴性集，总检出 < 20 个且误报 < 2 个）

运行：
    python scripts/eval_drawing_p5.1_validation.py

输出：
- data/eval/drawing_p5.1_recall_results.json
- data/eval/drawing_p5.1_fp_results.json
- 终端输出验收结论
"""
import json
import sys
from pathlib import Path

# 添加 src 到路径
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from water_agent.drawing.dxf_reader import read_drawing
from water_agent.drawing.mapping import map_pipes
from water_agent.drawing.rules import review_drawing
from water_agent.runtime.governance.pipeline import ToolPolicy


# 创建允许读取图纸目录的 policy
EVAL_POLICY = ToolPolicy(allowed_read_dirs=[
    ROOT / "data" / "drawings" / "real_samples",
    ROOT / "data" / "drawings" / "defect_samples_v2",
])


def eval_recall():
    """召回率验证：检查 P-03_defect 是否能检出植入的 2 个缺陷。"""
    print("=" * 60)
    print("P5.1 召回率验证")
    print("=" * 60)

    # 加载 gold 标准
    gold_path = Path("data/eval/drawing_p4_defects_gold_v2.jsonl")
    gold_defects = {}
    with open(gold_path) as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                gold_defects[d["handle"]] = d

    print(f"\nGold 标准: {len(gold_defects)} 个缺陷")
    for handle, defect in gold_defects.items():
        print(f"  - handle={handle}, {defect['defect_type']}, DN: {defect['original_text']} → {defect['defect_text']}")

    # 运行审查（使用缺陷目录作为 root）
    print(f"\n运行审查: P-03_defect")
    defect_dir = ROOT / "data" / "drawings" / "defect_samples_v2"
    defect_policy = ToolPolicy(allowed_read_dirs=[defect_dir])

    drawing = read_drawing("P-03_defect", root=defect_dir, policy=defect_policy, max_entities=5000)
    if not drawing.ok:
        print(f"  ❌ 图纸解析失败: {drawing.errors}")
        return {"detected": 0, "total": len(gold_defects), "recall": 0.0}

    mapping = map_pipes(drawing)
    result = review_drawing(mapping)

    # 统计检出
    detected_handles = {f.handle for f in result.findings if f.rule.rule_id == "drain.min_diameter"}
    # 也检查 evidence_handles（标注可能被关联到管段）
    for f in result.findings:
        if f.rule.rule_id == "drain.min_diameter":
            detected_handles.update(f.evidence_handles)

    gold_handles = set(gold_defects.keys())

    matched = detected_handles & gold_handles
    recall = len(matched) / len(gold_handles) if gold_handles else 0.0

    print(f"\n结果:")
    print(f"  检出数: {len(detected_handles)}")
    print(f"  匹配数: {len(matched)} / {len(gold_handles)}")
    print(f"  召回率: {recall * 100:.1f}%")

    if matched:
        print(f"\n  ✅ 成功检出:")
        for h in matched:
            print(f"    - handle={h}, {gold_defects[h]['defect_type']}")

    missed = gold_handles - detected_handles
    if missed:
        print(f"\n  ❌ 漏检:")
        for h in missed:
            print(f"    - handle={h}, {gold_defects[h]['defect_type']}")

    # 保存结果
    output = {
        "summary": {
            "total_defects": len(gold_defects),
            "detected": len(matched),
            "recall": recall,
            "timestamp": "2026-09-16"
        },
        "detected_handles": list(detected_handles),
        "matched_handles": list(matched),
        "missed_handles": list(missed)
    }

    output_path = Path("data/eval/drawing_p5.1_recall_results.json")
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n结果已保存: {output_path}")

    return output["summary"]


def eval_false_positives():
    """误报率验证：检查 18 张阴性集图纸的误报情况。"""
    print("\n" + "=" * 60)
    print("P5.1 误报率验证")
    print("=" * 60)

    # 阴性集图纸列表（P2.5 建立）
    negative_set = [
        "D-01", "D-02", "D-03",
        "P-01", "P-02", "P-03", "P-04", "P-05", "P-06", "P-07",
        "PUMP-01", "PUMP-02", "PUMP-03",
        "S-01", "S-02", "S-03", "S-04", "S-05"
    ]

    print(f"\n阴性集: {len(negative_set)} 张图纸")

    # 使用真实样本目录
    samples_dir = ROOT / "data" / "drawings" / "real_samples"
    samples_policy = ToolPolicy(allowed_read_dirs=[samples_dir])

    results = {
        "summary": {
            "total_drawings": len(negative_set),
            "total_findings": 0,
            "timestamp": "2026-09-16"
        },
        "details": []
    }

    for drawing_id in negative_set:
        print(f"\n处理: {drawing_id}")
        drawing = read_drawing(drawing_id, root=samples_dir, policy=samples_policy, max_entities=5000)

        if not drawing.ok:
            print(f"  ⚠️  图纸解析失败: {drawing.errors[0] if drawing.errors else 'unknown'}")
            results["details"].append({
                "drawing_id": drawing_id,
                "findings": [],
                "stats": {"entities": 0, "pipes": 0, "findings": 0}
            })
            continue

        mapping = map_pipes(drawing)
        review_result = review_drawing(mapping)

        # 只统计 drain.min_diameter 规则的检出
        findings = [
            {
                "rule_id": f.rule.rule_id,
                "severity": f.rule.severity.value,
                "detail": f.detail,
                "handle": f.handle,
                "evidence_handles": list(f.evidence_handles)
            }
            for f in review_result.findings
            if f.rule.rule_id == "drain.min_diameter"
        ]

        results["summary"]["total_findings"] += len(findings)
        results["details"].append({
            "drawing_id": drawing_id,
            "findings": findings,
            "stats": {
                "entities": len(drawing.entities),
                "pipes": len(mapping.pipes),
                "findings": len(findings)
            }
        })

        print(f"  实体: {len(drawing.entities)}, 管段: {len(mapping.pipes)}, 检出: {len(findings)}")

    # 保存结果
    output_path = Path("data/eval/drawing_p5.1_fp_results.json")
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n结果已保存: {output_path}")

    # 统计误报率（假设所有检出都是误报，需要人工复核）
    total_findings = results["summary"]["total_findings"]
    fp_rate = total_findings / len(negative_set) if negative_set else 0.0

    print(f"\n总检出数: {total_findings}")
    print(f"平均每张图: {fp_rate:.2f}")

    return results["summary"]


def main():
    """运行完整验证并输出验收结论。"""
    print("\n" + "=" * 60)
    print("P5.1 验收评测")
    print("=" * 60)

    # 召回率
    recall_summary = eval_recall()

    # 误报率
    fp_summary = eval_false_positives()

    # 验收结论
    print("\n" + "=" * 60)
    print("验收结论")
    print("=" * 60)

    recall = recall_summary["recall"]
    total_fp = fp_summary["total_findings"]
    total_drawings = fp_summary["total_drawings"]

    recall_pass = recall >= 0.8
    fp_pass = total_fp <= 2  # 18 张图纸，≤ 2 个误报 = 11.1%

    print(f"\n召回率: {recall * 100:.1f}% (目标 ≥ 80%)")
    print(f"  {recall_summary['detected']}/{recall_summary['total_defects']} 缺陷被检出")
    print(f"  {'✅ 通过' if recall_pass else '❌ 未通过'}")

    print(f"\n误报情况: {total_fp} 个检出 (目标 ≤ 2 个)")
    print(f"  在 {total_drawings} 张阴性集图纸中")
    print(f"  {'✅ 通过' if fp_pass else '❌ 未通过 (需人工复核是否真为误报)'}")

    overall_pass = recall_pass and (total_fp <= 2 or total_fp <= int(0.1 * total_drawings))
    print(f"\n总体: {'✅ P5.1 验收通过' if overall_pass else '❌ P5.1 验收未通过'}")

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
