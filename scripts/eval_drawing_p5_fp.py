#!/usr/bin/env python3
"""P5 误报率评测：验证图纸审查功能的误报率（False Positive Rate）。

用法:
    # 1. 运行评测，生成初步结果
    python scripts/eval_drawing_p5_fp.py

    # 2. 人工审核 findings，标注误报（编辑生成的 annotations 文件）
    # 编辑 data/eval/drawing_p5_fp_annotations.json

    # 3. 重新计算指标
    python scripts/eval_drawing_p5_fp.py --with-annotations

    # 4. 生成报告
    python scripts/eval_drawing_p5_fp.py --report

输入:
    - data/drawings/real_samples/*.dxf（18 张阴性集图纸）
    - data/eval/drawing_p1/README.md（阴性集清单）

输出:
    - data/eval/drawing_p5_fp_results.json（评测结果）
    - data/eval/drawing_p5_fp_annotations.json（待人工标注）
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path

# 添加项目根目录到 Python 路径
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.water_agent.drawing.dxf_reader import read_drawing
from src.water_agent.drawing.mapping import map_pipes
from src.water_agent.drawing.rules import review_drawing
from src.water_agent.runtime.governance.pipeline import ToolPolicy

ROOT = Path(__file__).parent.parent
NEGATIVE_SET_DIR = ROOT / "data/drawings/real_samples"
RESULTS_FILE = ROOT / "data/eval/drawing_p5_fp_results.json"
ANNOTATIONS_FILE = ROOT / "data/eval/drawing_p5_fp_annotations.json"

# 阴性集清单（18 张图纸，来自 drawing_p1/README.md）
NEGATIVE_DRAWINGS = [
    "D-01", "D-02", "D-03",
    "P-01", "P-02", "P-03", "P-04", "P-05", "P-06", "P-07",
    "PUMP-01", "PUMP-02", "PUMP-03",
    "S-01", "S-02", "S-03", "S-04", "S-05",
]

# 创建允许读取阴性集目录的 policy
EVAL_POLICY = ToolPolicy(allowed_read_dirs=[NEGATIVE_SET_DIR])


def run_review(drawing_id: str) -> dict:
    """运行图纸审查链，返回审查结果。"""
    dxf_path = NEGATIVE_SET_DIR / f"{drawing_id}.dxf"
    if not dxf_path.exists():
        return {"error": f"图纸文件不存在: {dxf_path}"}

    try:
        # 1. 解析图纸
        extraction = read_drawing(
            drawing_id=drawing_id,
            root=NEGATIVE_SET_DIR,
            policy=EVAL_POLICY,
            max_entities=5000,
        )

        # 2. 映射为管段
        mapping_result = map_pipes(extraction)

        # 3. 运行规则检查
        review_result = review_drawing(mapping_result)

        return {
            "drawing_id": drawing_id,
            "extraction": extraction,
            "mapping": mapping_result,
            "review": review_result,
        }
    except Exception as e:
        return {"drawing_id": drawing_id, "error": str(e)}


def serialize_finding(finding) -> dict:
    """将 Finding 对象序列化为 JSON 可保存格式。"""
    return {
        "rule_id": finding.rule.rule_id,
        "severity": finding.rule.severity,
        "detail": finding.detail,
        "handle": finding.handle,
        "evidence_handles": finding.evidence_handles,
    }


def run_evaluation() -> dict:
    """运行完整评测，返回结果。"""
    print("🧪 P5 误报率评测\n")
    print(f"阴性集: {len(NEGATIVE_DRAWINGS)} 张图纸")
    print(f"来源: {NEGATIVE_SET_DIR.relative_to(ROOT)}\n")

    results = []
    total_findings = 0

    for drawing_id in NEGATIVE_DRAWINGS:
        print(f"审查 {drawing_id}...", end=" ")
        review_result = run_review(drawing_id)

        if "error" in review_result:
            print(f"✗ 失败: {review_result['error']}")
            results.append({
                "drawing_id": drawing_id,
                "error": review_result["error"],
                "findings": [],
            })
            continue

        findings = review_result["review"].findings
        finding_count = len(findings)
        total_findings += finding_count

        print(f"✓ {finding_count} 个检出")

        # 序列化 findings
        serialized_findings = [serialize_finding(f) for f in findings]

        results.append({
            "drawing_id": drawing_id,
            "findings": serialized_findings,
            "stats": {
                "entities": len(review_result["extraction"].entities),
                "pipes": len(review_result["mapping"].pipes),
                "findings": finding_count,
            },
        })

    print(f"\n总检出数: {total_findings}")

    return {
        "summary": {
            "total_drawings": len(NEGATIVE_DRAWINGS),
            "total_findings": total_findings,
            "timestamp": "2026-09-16",
        },
        "details": results,
    }


def create_annotation_template(results: dict) -> dict:
    """从评测结果创建人工标注模板。"""
    annotations = []

    for detail in results["details"]:
        if "error" in detail:
            continue

        for idx, finding in enumerate(detail["findings"]):
            annotations.append({
                "drawing_id": detail["drawing_id"],
                "finding_index": idx,
                "rule_id": finding["rule_id"],
                "handle": finding["handle"],
                "detail": finding["detail"],
                "is_false_positive": None,  # 待人工标注: true/false
                "reason": "",  # 待人工填写误报原因
            })

    return {
        "instructions": (
            "请为每个 finding 标注 is_false_positive:\n"
            "  - true: 误报（检出的问题在工程上完全不成立）\n"
            "  - false: 真实问题（即使在阴性集中，可能仍存在我们之前未注意的真实缺陷）\n"
            "\n"
            "如果 is_false_positive=true，请在 reason 中说明误报原因：\n"
            "  - 系统类型混淆（如给水管被误判为排水管）\n"
            "  - 标注关联错误（标注被关联到错误的管段）\n"
            "  - 管径阈值过严（DN250 被判违规，但实际场景允许）\n"
            "  - 其他（请具体说明）\n"
        ),
        "annotations": annotations,
    }


def calculate_fp_rate_with_annotations() -> dict:
    """根据人工标注计算误报率。"""
    if not ANNOTATIONS_FILE.exists():
        print(f"❌ 标注文件不存在: {ANNOTATIONS_FILE.relative_to(ROOT)}")
        print("请先运行 `python scripts/eval_drawing_p5_fp.py` 生成标注模板")
        sys.exit(1)

    with open(ANNOTATIONS_FILE, encoding="utf-8") as f:
        annotations_data = json.load(f)

    annotations = annotations_data["annotations"]

    # 统计
    total = len(annotations)
    annotated = sum(1 for a in annotations if a["is_false_positive"] is not None)
    false_positives = sum(1 for a in annotations if a["is_false_positive"] is True)

    if annotated < total:
        print(f"⚠️  仅标注了 {annotated}/{total} 个 findings")
        print("请完成所有标注后再计算误报率\n")

    fp_rate = false_positives / total if total > 0 else 0.0

    print("=" * 60)
    print(f"总检出数: {total}")
    print(f"误报数: {false_positives}")
    print(f"误报率: {fp_rate:.1%}\n")

    # 按误报原因分组
    fp_by_reason = defaultdict(int)
    for a in annotations:
        if a["is_false_positive"] is True:
            reason = a["reason"] or "未填写原因"
            fp_by_reason[reason] += 1

    if fp_by_reason:
        print("误报原因分布:")
        for reason, count in sorted(fp_by_reason.items(), key=lambda x: -x[1]):
            print(f"  - {reason}: {count}")
        print()

    return {
        "summary": {
            "total_findings": total,
            "annotated_findings": annotated,
            "false_positives": false_positives,
            "fp_rate": fp_rate,
        },
        "fp_by_reason": dict(fp_by_reason),
        "false_positive_details": [
            a for a in annotations if a["is_false_positive"] is True
        ],
    }


def generate_report() -> None:
    """生成 P5 完成报告。"""
    if not RESULTS_FILE.exists():
        print(f"❌ 结果文件不存在: {RESULTS_FILE.relative_to(ROOT)}")
        sys.exit(1)

    if not ANNOTATIONS_FILE.exists():
        print(f"❌ 标注文件不存在: {ANNOTATIONS_FILE.relative_to(ROOT)}")
        sys.exit(1)

    with open(RESULTS_FILE, encoding="utf-8") as f:
        results = json.load(f)

    fp_analysis = calculate_fp_rate_with_annotations()

    # 生成报告
    report_path = ROOT / "docs/P5_COMPLETION_REPORT.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("# P5 阶段完成报告：误报率验证\n\n")
        f.write("**状态**: ✅ 已完成\n")
        f.write("**完成时间**: 2026-09-16\n\n")

        fp_rate = fp_analysis["summary"]["fp_rate"]
        status = "✅ 通过" if fp_rate <= 0.10 else "❌ 未通过"
        f.write(f"**验收结果**: {status}（误报率 {fp_rate:.1%}，目标 ≤ 10%）\n\n")

        f.write("---\n\n")
        f.write("## 一、评测概况\n\n")
        f.write(f"- 阴性集图纸数: {results['summary']['total_drawings']}\n")
        f.write(f"- 总检出数: {results['summary']['total_findings']}\n")
        f.write(f"- 误报数: {fp_analysis['summary']['false_positives']}\n")
        f.write(f"- 误报率: {fp_rate:.1%}\n\n")

        if fp_analysis["fp_by_reason"]:
            f.write("## 二、误报原因分析\n\n")
            for reason, count in sorted(
                fp_analysis["fp_by_reason"].items(), key=lambda x: -x[1]
            ):
                f.write(f"- {reason}: {count} 条\n")
            f.write("\n")

        f.write("## 三、详细结果\n\n")
        f.write(f"- 评测结果: {RESULTS_FILE.relative_to(ROOT)}\n")
        f.write(f"- 人工标注: {ANNOTATIONS_FILE.relative_to(ROOT)}\n\n")

        if fp_rate > 0.10:
            f.write("## 四、改进建议\n\n")
            f.write("误报率超过 10% 目标，建议从以下方向优化：\n\n")
            f.write("1. **系统类型识别增强**：从标注文本/符号/管径推断系统类型\n")
            f.write("2. **标注关联优化**：自适应阈值、图层优先级\n")
            f.write("3. **管径阈值调整**：根据误报类型调整阈值或降低断言强度\n\n")

    print(f"✅ 报告已生成: {report_path.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="P5 误报率评测")
    parser.add_argument(
        "--with-annotations",
        action="store_true",
        help="使用人工标注计算误报率",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="生成完成报告",
    )
    args = parser.parse_args()

    if args.report:
        generate_report()
        return

    if args.with_annotations:
        calculate_fp_rate_with_annotations()
        return

    # 默认：运行评测
    results = run_evaluation()

    # 保存结果
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n✅ 结果已保存: {RESULTS_FILE.relative_to(ROOT)}")

    # 生成标注模板
    annotations = create_annotation_template(results)
    with open(ANNOTATIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(annotations, f, indent=2, ensure_ascii=False)

    print(f"✅ 标注模板已生成: {ANNOTATIONS_FILE.relative_to(ROOT)}")
    print("\n下一步:")
    print(f"  1. 编辑 {ANNOTATIONS_FILE.relative_to(ROOT)}，标注每个 finding 是否为误报")
    print("  2. 运行 `python scripts/eval_drawing_p5_fp.py --with-annotations` 计算误报率")
    print("  3. 运行 `python scripts/eval_drawing_p5_fp.py --report` 生成完成报告")


if __name__ == "__main__":
    main()
