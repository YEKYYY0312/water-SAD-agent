#!/usr/bin/env python3
"""P4 召回率评测：验证图纸审查链能否检出植入的缺陷。

用法:
    python scripts/eval_drawing_p4_recall.py

输入:
    - data/eval/drawing_p4_defects_gold_v2.jsonl（gold 标注）
    - data/drawings/defect_samples_v2/*.dxf（缺陷图纸）

输出:
    - 召回率指标（检出 / 总缺陷数）
    - 未检出的缺陷详情
"""
import json
from pathlib import Path

# 添加项目根目录到 Python 路径
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.water_agent.drawing.dxf_reader import read_drawing
from src.water_agent.drawing.mapping import map_pipes
from src.water_agent.drawing.rules import review_drawing
from src.water_agent.runtime.governance.pipeline import ToolPolicy

ROOT = Path(__file__).parent.parent
GOLD_FILE = ROOT / "data/eval/drawing_p4_defects_gold_v2.jsonl"
DEFECT_DIR = ROOT / "data/drawings/defect_samples_v2"

# 创建允许读取缺陷目录的 policy
EVAL_POLICY = ToolPolicy(allowed_read_dirs=[DEFECT_DIR])


def load_gold_annotations() -> list[dict]:
    """加载 gold 标注。"""
    annotations = []
    with open(GOLD_FILE, encoding="utf-8") as f:
        for line in f:
            annotations.append(json.loads(line))
    return annotations


def run_review(drawing_id: str) -> dict:
    """运行图纸审查链，返回审查结果。"""
    dxf_path = DEFECT_DIR / f"{drawing_id}.dxf"
    if not dxf_path.exists():
        return {"error": f"图纸文件不存在: {dxf_path}"}

    try:
        # 1. 解析图纸（提高 max_entities 避免截断）
        extraction = read_drawing(
            drawing_id=drawing_id,
            root=DEFECT_DIR,
            policy=EVAL_POLICY,
            max_entities=5000,
        )

        # 2. 映射为管段
        mapping_result = map_pipes(extraction)

        # 3. 运行规则检查
        review_result = review_drawing(mapping_result)

        return {
            "extraction": extraction,
            "mapping": mapping_result,
            "review": review_result,
        }
    except Exception as e:
        return {"error": str(e)}


def check_recall(gold: dict, review_result: dict) -> dict:
    """检查单个缺陷是否被召回。

    匹配逻辑：
    1. 优先匹配 finding.handle == gold["handle"]（直接匹配）
    2. 如果未匹配，检查 gold["handle"] 是否在 finding.evidence_handles 中（通过证据匹配）
    """
    if "error" in review_result:
        return {
            "defect_id": gold["defect_id"],
            "recalled": False,
            "handle": gold["handle"],
            "expected_violation": gold["violation"],
            "reason": f"审查失败: {review_result['error']}",
        }

    # 检查是否有 finding 指向该 handle（直接或通过证据）
    review = review_result["review"]
    recalled = False
    matched_finding = None

    for finding in review.findings:
        # 方式 1：直接匹配（finding.handle 就是缺陷对象）
        if finding.handle == gold["handle"]:
            recalled = True
            matched_finding = {
                "rule_id": finding.rule.rule_id,
                "severity": finding.rule.severity,
                "detail": finding.detail,
                "match_type": "direct",
            }
            break

        # 方式 2：通过证据匹配（finding.evidence_handles 包含缺陷标注）
        if gold["handle"] in finding.evidence_handles:
            recalled = True
            matched_finding = {
                "rule_id": finding.rule.rule_id,
                "severity": finding.rule.severity,
                "detail": finding.detail,
                "match_type": "evidence",
                "pipe_handle": finding.handle,
            }
            break

    return {
        "defect_id": gold["defect_id"],
        "recalled": recalled,
        "handle": gold["handle"],
        "expected_violation": gold["violation"],
        "matched_finding": matched_finding,
    }


def main() -> None:
    print("🧪 P4 召回率评测\n")

    # 加载 gold 标注
    gold_annotations = load_gold_annotations()
    print(f"已加载 {len(gold_annotations)} 条 gold 标注\n")

    # 按图纸分组
    from collections import defaultdict
    defects_by_drawing = defaultdict(list)
    for gold in gold_annotations:
        defects_by_drawing[gold["drawing_id"]].append(gold)

    # 运行审查
    results = []
    for drawing_id, golds in defects_by_drawing.items():
        print(f"审查 {drawing_id}...")
        review_result = run_review(drawing_id)

        if "error" not in review_result:
            extraction = review_result["extraction"]
            mapping = review_result["mapping"]
            review = review_result["review"]
            print(f"  图元: {len(extraction.entities)} 条")
            print(f"  管段: {len(mapping.pipes)} 条")
            print(f"  问题: {len(review.findings)} 条")

        # 检查召回
        for gold in golds:
            recall_check = check_recall(gold, review_result)
            results.append(recall_check)

            status = "✓" if recall_check["recalled"] else "✗"
            print(f"    {status} {gold['defect_id']}")

        print()

    # 计算召回率
    total = len(results)
    recalled = sum(1 for r in results if r["recalled"])
    recall_rate = recalled / total if total > 0 else 0.0

    print("=" * 60)
    print(f"召回率: {recalled}/{total} = {recall_rate:.1%}\n")

    # 显示未召回的缺陷
    missed = [r for r in results if not r["recalled"]]
    if missed:
        print(f"未检出的缺陷 ({len(missed)}):\n")
        for r in missed:
            print(f"  {r['defect_id']}")
            print(f"    Handle: {r['handle']}")
            print(f"    预期违规: {r['expected_violation']}")
            print(f"    原因: {r.get('reason', '规则未覆盖')}")
            print()

    # 保存详细结果
    result_file = ROOT / "data/eval/drawing_p4_recall_results_v2.json"
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "summary": {
                    "total": total,
                    "recalled": recalled,
                    "recall_rate": recall_rate,
                },
                "details": results,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )

    print(f"详细结果已保存: {result_file.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
