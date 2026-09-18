#!/usr/bin/env python3
"""验证真实图纸样本的可读性（P2 Step 5）。

绕过 ToolPolicy，直接调用 ezdxf 解析器验证所有样本都能成功读取。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

try:
    import ezdxf
except ImportError:
    print("❌ ezdxf 未安装", file=sys.stderr)
    sys.exit(1)


def verify_sample(dxf_path: Path) -> dict[str, Any]:
    """验证单个 DXF 样本的可读性。"""
    result = {
        "drawing_id": dxf_path.stem,
        "file_size_mb": round(dxf_path.stat().st_size / 1024 / 1024, 2),
        "ok": False,
        "error": None,
    }

    try:
        doc = ezdxf.readfile(dxf_path)
        msp = doc.modelspace()

        # 基础信息
        result["ok"] = True
        result["dxf_version"] = doc.dxfversion
        result["layers"] = len(doc.layers)

        # 统计实体
        entities = list(msp)
        result["entity_count"] = len(entities)

        # 按类型统计
        entity_types: dict[str, int] = {}
        for entity in entities:
            dxftype = entity.dxftype()
            entity_types[dxftype] = entity_types.get(dxftype, 0) + 1

        result["entity_types"] = entity_types

        # 检查单位
        insunits = doc.header.get("$INSUNITS", None)
        result["insunits"] = insunits
        result["unit_known"] = insunits is not None and insunits != 0

    except Exception as e:
        result["error"] = str(e)

    return result


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="验证真实图纸样本的可读性")
    parser.add_argument("--root", type=Path, default=Path("data/drawings/real_samples"),
                       help="图纸样本目录")
    parser.add_argument("--report", type=Path,
                       default=Path("data/eval/reports/real_samples_readability.json"),
                       help="输出报告路径")
    args = parser.parse_args()

    if not args.root.exists():
        print(f"❌ 目录不存在: {args.root}", file=sys.stderr)
        return 1

    # 验证所有 DXF 文件
    dxf_files = sorted(args.root.glob("*.dxf"))
    if not dxf_files:
        print(f"❌ 未找到 DXF 文件: {args.root}", file=sys.stderr)
        return 1

    print("=" * 80)
    print("真实图纸样本验证报告")
    print("=" * 80)
    print(f"样本目录: {args.root}")
    print(f"DXF 文件数: {len(dxf_files)}")
    print()

    results = []
    success_count = 0

    for dxf_file in dxf_files:
        result = verify_sample(dxf_file)
        results.append(result)

        print(f"{'='*80}")
        print(f"📄 图纸: {result['drawing_id']}")
        print(f"   文件大小: {result['file_size_mb']} MB")
        print(f"   解析状态: {'✅ 成功' if result['ok'] else '❌ 失败'}")

        if result["ok"]:
            success_count += 1
            print(f"   DXF 版本: {result['dxf_version']}")
            print(f"   单位已知: {result['unit_known']} (INSUNITS={result['insunits']})")
            print(f"   图层数: {result['layers']}")
            print(f"   实体数: {result['entity_count']}")

            # 实体类型分布
            entity_types = result['entity_types']
            print(f"   实体类型分布 (前 10):")
            for dxftype, count in sorted(entity_types.items(),
                                        key=lambda x: x[1], reverse=True)[:10]:
                print(f"      - {dxftype}: {count}")

            # 检查关键实体类型
            if entity_types.get('INSERT', 0) > 0:
                print(f"   ⚠️  包含 INSERT 实体（块引用）: {entity_types['INSERT']} 个")
            if entity_types.get('ARC', 0) > 0:
                print(f"   ⚠️  包含 ARC 实体（圆弧）: {entity_types['ARC']} 个")
        else:
            print(f"   ❌ 错误: {result['error']}")

    # 总体统计
    print(f"\n{'='*80}")
    print("📊 总体统计")
    print("=" * 80)
    print(f"总图纸数: {len(results)}")
    print(f"成功解析: {success_count}/{len(results)}")
    print(f"解析率: {success_count/len(results)*100:.1f}%")

    total_size = sum(r['file_size_mb'] for r in results)
    print(f"总文件大小: {total_size:.2f} MB")

    if success_count > 0:
        total_entities = sum(r['entity_count'] for r in results if r['ok'])
        print(f"总实体数: {total_entities:,}")

        # 统计所有实体类型
        all_types: dict[str, int] = {}
        for r in results:
            if r['ok'] and 'entity_types' in r:
                for dxftype, count in r['entity_types'].items():
                    all_types[dxftype] = all_types.get(dxftype, 0) + count

        print(f"\n实体类型汇总:")
        for dxftype, count in sorted(all_types.items(),
                                     key=lambda x: x[1], reverse=True):
            print(f"   - {dxftype}: {count:,}")

        # 统计包含 INSERT/ARC 的图纸
        insert_count = sum(1 for r in results
                          if r.get('entity_types', {}).get('INSERT', 0) > 0)
        arc_count = sum(1 for r in results
                       if r.get('entity_types', {}).get('ARC', 0) > 0)
        print(f"\n包含 INSERT 实体的图纸: {insert_count}/{success_count}")
        print(f"包含 ARC 实体的图纸: {arc_count}/{success_count}")

        # 单位声明情况
        unit_known_count = sum(1 for r in results if r['ok'] and r['unit_known'])
        print(f"\n单位已知的图纸: {unit_known_count}/{success_count}")

    # 保存报告
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "total": len(results),
        "success": success_count,
        "success_rate": success_count / len(results) if results else 0,
        "total_size_mb": total_size,
        "samples": results,
    }

    with open(args.report, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n报告已保存: {args.report}")

    return 0 if success_count == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
