#!/usr/bin/env python3
"""P4 召回率评测脚本：运行图纸审查链并计算缺陷召回率。

用法:
    python scripts/eval_recall.py [--threshold 0.8]

产出:
    - data/eval/reports/drawing_p4_recall.json（召回率详细报告）
    - 退出码 0（通过）或 1（未达标）
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any

from water_agent.graphs.drawing_review import run_drawing_review


def load_gold_defects(gold_path: Path) -> list[dict[str, Any]]:
    """加载 gold 标注。"""
    defects = []
    with open(gold_path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                defects.append(json.loads(line))
    return defects


def run_drawing_review_safe(drawing_id: str, defect_samples_dir: Path) -> dict[str, Any]:
    """运行图纸审查链并捕获异常。

    返回格式:
        {
            "drawing_id": str,
            "findings": [
                {
                    "finding_id": str,
                    "entity_handle": str,
                    "issue_type": str,
                    "description": str,
                    "clause": str,
                    "severity": str
                },
                ...
            ],
            "ok": bool,
            "errors": list[str]
        }
    """
    try:
        result = run_drawing_review(drawing_id, root=defect_samples_dir)
        return {
            "drawing_id": drawing_id,
            "findings": result.get("findings", []),
            "ok": result.get("ok", False),
            "errors": list(result.get("extraction_errors", [])),
            "pipes_reviewed": result.get("pipes_reviewed", 0),
            "summary": result.get("summary", ""),
        }
    except Exception as e:
        return {
            "drawing_id": drawing_id,
            "findings": [],
            "ok": False,
            "errors": [f"Exception: {str(e)}"],
            "pipes_reviewed": 0,
            "summary": "",
        }


def match_finding_to_defect(
    finding: dict[str, Any], defect: dict[str, Any]
) -> bool:
    """判断一个审查发现是否匹配 gold 缺陷。

    匹配条件:
        1. entity_handle 相同（精确匹配）
        2. 或者 issue_type/violation 语义相似（模糊匹配）
    """
    # 精确匹配：handle 相同
    if finding.get("entity_handle") == defect.get("entity_handle"):
        return True

    # 模糊匹配：缺陷类型和违规描述相似
    # 这里可以实现更复杂的语义匹配逻辑
    # 暂时只做精确匹配
    return False


def evaluate_recall(
    gold_defects: list[dict[str, Any]],
    review_results: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """计算召回率、精确率和 F1。

    返回:
        {
            "total_defects": int,  # gold 缺陷总数（分母）
            "detected_defects": int,  # 实际检出数（分子）
            "recall": float,  # 召回率
            "total_findings": int,  # 系统报警总数
            "true_positives": int,  # 真阳性（正确检出）
            "false_positives": int,  # 假阳性（误报）
            "precision": float,  # 精确率
            "f1_score": float,
            "missed_defects": list,  # 漏检列表
            "false_alarms": list,  # 误报列表
        }
    """
    total_defects = len(gold_defects)
    detected = []
    missed = []

    # 检查每个 gold 缺陷是否被检出
    for defect in gold_defects:
        drawing_id = defect["drawing_id"]
        review_result = review_results.get(drawing_id, {})
        findings = review_result.get("findings", [])

        # 判断该缺陷是否被检出
        is_detected = any(match_finding_to_defect(f, defect) for f in findings)

        if is_detected:
            detected.append(defect["defect_id"])
        else:
            missed.append({
                "defect_id": defect["defect_id"],
                "drawing_id": drawing_id,
                "entity_handle": defect["entity_handle"],
                "violation": defect["violation"],
            })

    # 计算误报（所有 findings 中不匹配任何 gold 的）
    all_findings = []
    for drawing_id, result in review_results.items():
        for finding in result.get("findings", []):
            all_findings.append((drawing_id, finding))

    false_alarms = []
    true_positives = 0
    for drawing_id, finding in all_findings:
        # 查找该 finding 对应的 gold 缺陷
        matched_gold = None
        for defect in gold_defects:
            if defect["drawing_id"] == drawing_id and match_finding_to_defect(finding, defect):
                matched_gold = defect
                break

        if matched_gold:
            true_positives += 1
        else:
            false_alarms.append({
                "drawing_id": drawing_id,
                "entity_handle": finding.get("entity_handle", "unknown"),
                "issue": finding.get("description", ""),
            })

    # 计算指标
    detected_count = len(detected)
    recall = detected_count / total_defects if total_defects > 0 else 0.0

    total_findings = len(all_findings)
    precision = true_positives / total_findings if total_findings > 0 else 0.0

    f1_score = (
        2 * (precision * recall) / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    return {
        "total_defects": total_defects,
        "detected_defects": detected_count,
        "recall": recall,
        "total_findings": total_findings,
        "true_positives": true_positives,
        "false_positives": len(false_alarms),
        "precision": precision,
        "f1_score": f1_score,
        "missed_defects": missed,
        "false_alarms": false_alarms,
        "detected_ids": detected,
    }


def main():
    parser = argparse.ArgumentParser(description="P4 图纸审查召回率评测")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.8,
        help="召回率通过阈值（默认 0.8）",
    )
    parser.add_argument(
        "--gold",
        type=Path,
        default=Path("data/eval/drawing_p4_defects_gold.jsonl"),
        help="Gold 标注文件路径",
    )
    parser.add_argument(
        "--defect-dir",
        type=Path,
        default=Path("data/drawings/defect_samples"),
        help="缺陷图纸目录",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/eval/reports/drawing_p4_recall.json"),
        help="评测报告输出路径",
    )
    args = parser.parse_args()

    print("🔍 P4 图纸审查召回率评测")
    print(f"  Gold 标注: {args.gold}")
    print(f"  缺陷图纸: {args.defect_dir}")
    print(f"  召回率阈值: {args.threshold * 100}%\n")

    # 加载 gold 标注
    gold_defects = load_gold_defects(args.gold)
    print(f"📋 Gold 缺陷总数: {len(gold_defects)}\n")

    # 按图纸分组
    drawings = set(d["drawing_id"] for d in gold_defects)
    print(f"📄 待审查图纸: {len(drawings)}")
    for drawing_id in sorted(drawings):
        count = sum(1 for d in gold_defects if d["drawing_id"] == drawing_id)
        print(f"  - {drawing_id}: {count} 个缺陷")
    print()

    # 运行审查链
    print("🚀 运行图纸审查链...\n")
    review_results = {}
    for drawing_id in sorted(drawings):
        # 缺陷图纸的 drawing_id 已经包含 _defect 后缀（如 "P-02_defect"）
        # 直接传给审查链，它会查找 P-02_defect.dxf
        print(f"  审查 {drawing_id}...")

        result = run_drawing_review_safe(drawing_id, args.defect_dir)
        review_results[drawing_id] = result

        findings_count = len(result.get("findings", []))
        pipes_count = result.get("pipes_reviewed", 0)
        ok = result.get("ok", False)
        status = "✓" if ok else "✗"
        print(f"    {status} {pipes_count} 管段审查完成，{findings_count} 个发现")

        if result.get("errors"):
            print(f"    ⚠️  错误: {result['errors'][0]}")
    print()

    # 计算召回率
    print("📊 计算召回率...\n")
    metrics = evaluate_recall(gold_defects, review_results)

    # 输出结果
    print("=" * 60)
    print("评测结果")
    print("=" * 60)
    print(f"Gold 缺陷总数:    {metrics['total_defects']}")
    print(f"实际检出数:       {metrics['detected_defects']}")
    print(f"召回率 (Recall):  {metrics['recall']:.2%}")
    print()
    print(f"系统报警总数:     {metrics['total_findings']}")
    print(f"真阳性 (TP):      {metrics['true_positives']}")
    print(f"假阳性 (FP):      {metrics['false_positives']}")
    print(f"精确率 (Precision): {metrics['precision']:.2%}")
    print()
    print(f"F1 Score:         {metrics['f1_score']:.2%}")
    print("=" * 60)
    print()

    # 漏检分析
    if metrics["missed_defects"]:
        print("⚠️  漏检缺陷:")
        for missed in metrics["missed_defects"]:
            print(f"  - {missed['defect_id']} ({missed['drawing_id']})")
            print(f"    handle: {missed['entity_handle']}")
            print(f"    违规: {missed['violation']}")
        print()

    # 误报分析
    if metrics["false_alarms"]:
        print("⚠️  误报项:")
        for alarm in metrics["false_alarms"]:
            print(f"  - {alarm['drawing_id']} (handle: {alarm['entity_handle']})")
            print(f"    {alarm['issue']}")
        print()

    # 保存报告
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "threshold": args.threshold,
        "metrics": metrics,
        "gold_defects_count": len(gold_defects),
        "drawings_tested": len(drawings),
        "passed": metrics["recall"] >= args.threshold,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"💾 报告已保存: {args.output}\n")

    # 判定通过/失败
    if metrics["recall"] >= args.threshold:
        print(f"✅ 通过：召回率 {metrics['recall']:.2%} ≥ 阈值 {args.threshold:.2%}")
        return 0
    else:
        print(f"❌ 未通过：召回率 {metrics['recall']:.2%} < 阈值 {args.threshold:.2%}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
