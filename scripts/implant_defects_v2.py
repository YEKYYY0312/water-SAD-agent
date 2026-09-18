#!/usr/bin/env python3
"""P4 缺陷植入脚本 v2：只植入现有规则能检测的缺陷，快速验证评测框架。

当前规则引擎支持的检查项：
1. 室外排水管最小管径 ≥ DN300（RULE_DRAIN_MIN_DN）
2. 排水横管坡度 0.005-0.01（RULE_DRAIN_SLOPE）
3. 压力流设计流速 0.7-2.0 m/s（RULE_PRESSURE_VELOCITY）

本版本只植入「室外排水管管径不足」缺陷，确保至少有一类缺陷能被检出。

用法:
    python scripts/implant_defects_v2.py

产出:
    - data/drawings/defect_samples_v2/*.dxf（带缺陷的图纸副本）
    - data/eval/drawing_p4_defects_gold_v2.jsonl（gold 标注）
"""
import json
from pathlib import Path
from typing import Any

import ezdxf

# 简化版缺陷清单：只植入「室外排水管管径 < 300mm」
DEFECT_SPECS = [
    # P-03: DN500 → DN250（明确违反 ≥300mm 要求）
    {
        "base_drawing": "P-03",
        "defect_id": "P03-D001",
        "target_handle": "289",
        "original_expected": "DN500",
        "defect_text": "DN250",  # 违规值
        "defect_type": "室外排水管管径不足",
        "violation": "室外排水管（污水/合流）公称管径不宜小于 300mm",
        "severity": "major",
    },
    # P-03: DN700 → DN200（更严重的违规）
    {
        "base_drawing": "P-03",
        "defect_id": "P03-D002",
        "target_handle": "2BA",
        "original_expected": "DN700",
        "defect_text": "DN200",
        "defect_type": "室外排水管管径不足",
        "violation": "室外排水管（污水/合流）公称管径不宜小于 300mm",
        "severity": "major",
    },
]

# 项目根目录
ROOT = Path(__file__).parent.parent
REAL_SAMPLES = ROOT / "data/drawings/real_samples"
DEFECT_DIR = ROOT / "data/drawings/defect_samples_v2"
GOLD_FILE = ROOT / "data/eval/drawing_p4_defects_gold_v2.jsonl"


def find_text_entity(doc: ezdxf.document.Drawing, handle: str) -> Any | None:
    """根据 handle 查找 TEXT/MTEXT 实体。"""
    try:
        entity = doc.entitydb.get(handle)
        if entity and entity.dxftype() in ("TEXT", "MTEXT"):
            return entity
        return None
    except Exception:
        return None


def find_text_by_pattern(
    doc: ezdxf.document.Drawing, pattern: str
) -> list[tuple[str, Any]]:
    """搜索包含指定文本模式的 TEXT/MTEXT 实体，返回 (handle, entity) 列表。"""
    matches = []
    for entity in doc.modelspace():
        if entity.dxftype() in ("TEXT", "MTEXT"):
            text = entity.dxf.text if entity.dxftype() == "TEXT" else entity.text
            if pattern in text:
                matches.append((entity.dxf.handle, entity))
    return matches


def implant_defect(spec: dict[str, Any]) -> dict[str, Any]:
    """植入单个缺陷，返回植入日志。"""
    drawing_id = spec["base_drawing"]
    source_path = REAL_SAMPLES / f"{drawing_id}.dxf"

    log: dict[str, Any] = {
        "defect_id": spec["defect_id"],
        "drawing": drawing_id,
        "status": "pending",
    }

    if not source_path.exists():
        log["status"] = "skipped"
        log["reason"] = f"源图纸不存在: {source_path}"
        return log

    try:
        doc = ezdxf.readfile(source_path)
    except Exception as e:
        log["status"] = "error"
        log["reason"] = f"无法读取图纸: {e}"
        return log

    # 策略 1: 如果提供了 target_handle，直接查找
    entity = None
    if "target_handle" in spec:
        entity = find_text_entity(doc, spec["target_handle"])

    # 策略 2: 如果没找到或未提供 handle，按文本模式搜索
    if entity is None and "search_pattern" in spec:
        matches = find_text_by_pattern(doc, spec["search_pattern"])
        if matches:
            log["search_candidates"] = [
                {"handle": h, "text": e.dxf.text if e.dxftype() == "TEXT" else e.text}
                for h, e in matches
            ]
            # 使用第一个匹配
            entity = matches[0][1]
            log["used_handle"] = matches[0][0]

    if entity is None:
        log["status"] = "skipped"
        log["reason"] = "未找到目标文本实体"
        return log

    # 记录原始值
    original_text = entity.dxf.text if entity.dxftype() == "TEXT" else entity.text
    log["original_text"] = original_text
    log["handle"] = entity.dxf.handle

    # 修改文本（保留 MTEXT 的格式前缀）
    if entity.dxftype() == "TEXT":
        entity.dxf.text = spec["defect_text"]
    else:  # MTEXT - 可能有格式代码，需要智能替换
        # 如果原文本有格式代码（如 {\fSimSun|...;DN500}），保留格式只改数字
        if original_text.startswith("{") and ";" in original_text:
            prefix = original_text.split(";")[0] + ";"
            entity.text = prefix + spec["defect_text"] + "}"
        else:
            entity.text = spec["defect_text"]

    # 保存缺陷图纸
    defect_path = DEFECT_DIR / f"{drawing_id}_defect.dxf"
    doc.saveas(defect_path)

    log["status"] = "success"
    log["defect_text"] = spec["defect_text"]
    log["output_path"] = str(defect_path.relative_to(ROOT))

    return log


def main() -> None:
    print("🧪 P4 缺陷植入 v2（仅现有规则覆盖）\n")

    # 创建输出目录
    DEFECT_DIR.mkdir(parents=True, exist_ok=True)

    # 按图纸分组缺陷（累积修改，避免覆盖）
    from collections import defaultdict
    defects_by_drawing = defaultdict(list)
    for spec in DEFECT_SPECS:
        defects_by_drawing[spec["base_drawing"]].append(spec)

    logs = []
    gold_records = []

    # 逐图纸处理
    for drawing_id, specs in defects_by_drawing.items():
        source_path = REAL_SAMPLES / f"{drawing_id}.dxf"

        if not source_path.exists():
            for spec in specs:
                logs.append({
                    "defect_id": spec["defect_id"],
                    "drawing": drawing_id,
                    "status": "skipped",
                    "reason": f"源图纸不存在: {source_path}",
                })
            continue

        try:
            doc = ezdxf.readfile(source_path)
        except Exception as e:
            for spec in specs:
                logs.append({
                    "defect_id": spec["defect_id"],
                    "drawing": drawing_id,
                    "status": "error",
                    "reason": f"无法读取图纸: {e}",
                })
            continue

        # 累积修改所有缺陷
        for spec in specs:
            print(f"  植入 {spec['defect_id']} ({drawing_id})...", end=" ")

            entity = find_text_entity(doc, spec["target_handle"])
            if entity is None:
                print("✗ (实体不存在)")
                logs.append({
                    "defect_id": spec["defect_id"],
                    "drawing": drawing_id,
                    "status": "skipped",
                    "reason": "未找到目标文本实体",
                })
                continue

            original_text = entity.dxf.text if entity.dxftype() == "TEXT" else entity.text

            # 修改文本（保留 MTEXT 的格式前缀）
            if entity.dxftype() == "TEXT":
                entity.dxf.text = spec["defect_text"]
            else:  # MTEXT
                if original_text.startswith("{") and ";" in original_text:
                    prefix = original_text.split(";")[0] + ";"
                    entity.text = prefix + spec["defect_text"] + "}"
                else:
                    entity.text = spec["defect_text"]

            print("✓")
            logs.append({
                "defect_id": spec["defect_id"],
                "drawing": drawing_id,
                "status": "success",
                "original_text": original_text,
                "handle": spec["target_handle"],
                "defect_text": spec["defect_text"],
            })

            # 生成 gold 标注
            gold_records.append({
                "defect_id": spec["defect_id"],
                "drawing_id": f"{drawing_id}_defect",
                "handle": spec["target_handle"],
                "defect_type": spec["defect_type"],
                "violation": spec["violation"],
                "severity": spec["severity"],
                "original_text": original_text,
                "defect_text": spec["defect_text"],
            })

        # 保存该图纸的缺陷版本（只保存一次）
        defect_path = DEFECT_DIR / f"{drawing_id}_defect.dxf"
        doc.saveas(defect_path)
        print(f"  → 已保存 {defect_path.relative_to(ROOT)}\n")

    # 保存 gold 标注
    with open(GOLD_FILE, "w", encoding="utf-8") as f:
        for record in gold_records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # 保存详细日志
    log_file = ROOT / "data/eval/drawing_p4_implant_log_v2.json"
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump({"logs": logs, "summary": {"total": len(logs)}}, f, indent=2, ensure_ascii=False)

    print(f"\n✅ 完成")
    print(f"   Gold 标注: {len(gold_records)} 条 → {GOLD_FILE.relative_to(ROOT)}")
    print(f"   缺陷图纸: {DEFECT_DIR.relative_to(ROOT)}")
    print(f"   详细日志: {log_file.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
