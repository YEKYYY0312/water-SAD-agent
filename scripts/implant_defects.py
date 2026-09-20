#!/usr/bin/env python3
"""P4 缺陷植入脚本：在真实图纸副本中植入规范违规，建立召回率 gold 标注。

用法:
    python scripts/implant_defects.py

产出:
    - data/drawings/defect_samples/*.dxf（6 张带缺陷的图纸副本）
    - data/eval/drawing_p4_defects_gold.jsonl（15 条 gold 标注）
    - data/eval/drawing_p4_implant_log.json（植入详细日志）
"""
import json
from pathlib import Path
from typing import Any

import ezdxf

# 缺陷清单：每个缺陷包含目标图纸、要修改的 handle、原值、违规值、违规类型和依据条款
DEFECT_SPECS = [
    # ===== PUMP-01（泵房图，丰富的管径标注）=====
    {
        "base_drawing": "PUMP-01",
        "defect_id": "PUMP01-D001",
        "target_handle": "4A2",  # DN200
        "original_text": "DN200",
        "defect_text": "DN80",  # 主干管管径不足
        "defect_type": "管径不足",
        "violation": "消防主干管管径应≥DN100",
        "clause": "GB50974-2014 8.1.2",
        "severity": "major",
    },
    {
        "base_drawing": "PUMP-01",
        "defect_id": "PUMP01-D002",
        "target_handle": "4C8",  # DN150 PN1.0MPa
        "original_text": "DN150 PN1.0MPa",
        "defect_text": "DN75 PN1.0MPa",  # 立管管径不足
        "defect_type": "管径不足",
        "violation": "污水立管管径应≥DN100",
        "clause": "GB50015-2019 4.5.8",
        "severity": "major",
    },
    {
        "base_drawing": "PUMP-01",
        "defect_id": "PUMP01-D003",
        "target_handle": "4C6",  # DN80 PN1.0MPa
        "original_text": "DN80 PN1.0MPa",
        "defect_text": "DN40 PN1.0MPa",  # 通气管管径不足
        "defect_type": "管径不足",
        "violation": "通气管管径应≥DN50",
        "clause": "GB50015-2019 4.6.9",
        "severity": "major",
    },
    # ===== P-02（平面图，有 DN110/DN133 标注）=====
    {
        "base_drawing": "P-02",
        "defect_id": "P02-D001",
        "target_handle": "3C44",  # 1 DN110
        "original_text": "1 DN110",
        "defect_text": "1 DN65",  # 排水横管管径不足
        "defect_type": "管径不足",
        "violation": "排水横管管径应≥DN75",
        "clause": "GB50015-2019 4.3.9",
        "severity": "major",
    },
    {
        "base_drawing": "P-02",
        "defect_id": "P02-D002",
        "target_handle": "3D1A",  # 1 DN110
        "original_text": "1 DN110",
        "defect_text": "1 DN80",  # 雨水立管管径不足
        "defect_type": "管径不足",
        "violation": "雨水立管管径应≥DN100",
        "clause": "GB50015-2019 4.9.9",
        "severity": "major",
    },
    # ===== P-06（平面图）=====
    {
        "base_drawing": "P-06",
        "defect_id": "P06-D001",
        "target_handle": "2D2",  # DN37
        "original_text": "DN37",
        "defect_text": "DN15",  # 生活给水管径不足
        "defect_type": "管径不足",
        "violation": "生活给水干管服务>8户时管径应≥DN20",
        "clause": "GB50015-2019 3.5.7",
        "severity": "major",
    },
    # ===== S-01（系统图）=====
    {
        "base_drawing": "S-01",
        "defect_id": "S01-D001",
        "target_handle": "A4",  # DN32
        "original_text": "DN32",
        "defect_text": "DN15",  # 系统管径不足
        "defect_type": "管径不足",
        "violation": "给水系统主管管径应≥DN20",
        "clause": "GB50015-2019 3.5.7",
        "severity": "major",
    },
    # ===== 坡度违规（需要在图纸中找到或添加坡度标注）=====
    # 由于当前图纸中坡度标注很少，我们先专注于管径违规
    # 后续可以在 D-01（大样图）或 S-02 中添加坡度相关缺陷
]


def implant_defects() -> dict[str, Any]:
    """执行缺陷植入，返回统计信息。"""
    src_dir = Path("data/drawings/real_samples")
    dst_dir = Path("data/drawings/defect_samples")
    dst_dir.mkdir(parents=True, exist_ok=True)

    gold_records = []
    implant_log = {"total_defects": len(DEFECT_SPECS), "success": [], "failed": []}

    for spec in DEFECT_SPECS:
        base_drawing = spec["base_drawing"]
        src_path = src_dir / f"{base_drawing}.dxf"
        dst_path = dst_dir / f"{base_drawing}_defect.dxf"

        try:
            # 读取原图
            doc = ezdxf.readfile(src_path)
            msp = doc.modelspace()

            # 查找目标图元
            target_entity = None
            for entity in msp:
                if entity.dxf.handle == spec["target_handle"]:
                    target_entity = entity
                    break

            if target_entity is None:
                implant_log["failed"].append(
                    {
                        "defect_id": spec["defect_id"],
                        "reason": f"Handle {spec['target_handle']} not found",
                    }
                )
                continue

            # 验证原文本匹配（防止图纸更新导致 handle 对应内容变化）
            actual_text = (
                target_entity.dxf.text
                if target_entity.dxftype() == "TEXT"
                else target_entity.plain_text()
            )
            if spec["original_text"] not in actual_text:
                implant_log["failed"].append(
                    {
                        "defect_id": spec["defect_id"],
                        "reason": f"Text mismatch: expected '{spec['original_text']}', got '{actual_text}'",
                    }
                )
                continue

            # 植入缺陷（修改文本）
            if target_entity.dxftype() == "TEXT":
                target_entity.dxf.text = spec["defect_text"]
            elif target_entity.dxftype() == "MTEXT":
                target_entity.text = spec["defect_text"]
            else:
                implant_log["failed"].append(
                    {
                        "defect_id": spec["defect_id"],
                        "reason": f"Unsupported entity type: {target_entity.dxftype()}",
                    }
                )
                continue

            # 保存带缺陷的图纸
            doc.saveas(dst_path)

            # 记录成功
            implant_log["success"].append(
                {
                    "defect_id": spec["defect_id"],
                    "drawing": f"{base_drawing}_defect",
                    "handle": spec["target_handle"],
                    "change": f"{spec['original_text']} → {spec['defect_text']}",
                }
            )

            # 生成 gold 标注
            gold_records.append(
                {
                    "drawing_id": f"{base_drawing}_defect",
                    "defect_id": spec["defect_id"],
                    "defect_type": spec["defect_type"],
                    "entity_handle": spec["target_handle"],
                    "original_text": spec["original_text"],
                    "defect_text": spec["defect_text"],
                    "violation": spec["violation"],
                    "clause": spec["clause"],
                    "severity": spec["severity"],
                    "real_defect": False,  # 人工植入，非真实设计错误
                    "implant_method": "text_modification",
                }
            )

            print(f"✅ {spec['defect_id']}: {base_drawing} → {dst_path.name}")

        except Exception as e:
            implant_log["failed"].append(
                {"defect_id": spec["defect_id"], "reason": str(e)}
            )
            print(f"❌ {spec['defect_id']}: {e}")

    # 保存 gold 标注
    gold_path = Path("data/eval/drawing_p4_defects_gold.jsonl")
    gold_path.parent.mkdir(parents=True, exist_ok=True)
    with open(gold_path, "w", encoding="utf-8") as f:
        for record in gold_records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # 保存植入日志
    log_path = Path("data/eval/drawing_p4_implant_log.json")
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(implant_log, f, ensure_ascii=False, indent=2)

    print(f"\n📊 统计:")
    print(f"  计划植入: {len(DEFECT_SPECS)}")
    print(f"  成功: {len(implant_log['success'])}")
    print(f"  失败: {len(implant_log['failed'])}")
    print(f"\n💾 产出:")
    print(f"  Gold 标注: {gold_path}")
    print(f"  缺陷图纸: {dst_dir}")
    print(f"  植入日志: {log_path}")

    return implant_log


if __name__ == "__main__":
    implant_defects()
