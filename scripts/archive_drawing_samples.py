#!/usr/bin/env python3
"""P2 Step 4：把脱敏 DXF 按规范命名归档到 `data/drawings/real_samples/`。

用法::

    _venv/bin/python scripts/archive_drawing_samples.py \
        --src /tmp/dxf_desens \
        --map /tmp/p2_samples/classification.json \
        --out data/drawings/real_samples

命名规范（任务书）：平面图 ``P-nn``、系统图 ``S-nn``、大样图 ``D-nn``、泵房图 ``PUMP-nn``。
编号在每个前缀内从 01 连续，``dxf_reader._DRAWING_ID_RE`` 允许字母数字与 ``._-``，
所以 ``P-01`` 是合法登记号。

两条安全纪律
------------
1. **溯源映射不进仓库。** 样本 ID → 原始工程目录/文件名的映射本身就是可识别信息
   （例如「P-05 来自某市某污水处理厂」），因此本脚本把它写到 ``--provenance``
   指定的仓库外路径，**不写进 samples 目录**。``MANIFEST.md`` 只记录
   类型/图层数/实体数/版本/单位这类非识别性元信息。
2. **不做覆盖。** 目标已存在同名文件时默认拒绝（``--force`` 才覆盖），
   避免把上一次的样本静默替换掉。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

#: 前缀 → 中文类型名（MANIFEST 用）。
PREFIX_LABEL = {
    "P": "平面图",
    "S": "系统图",
    "D": "大样图",
    "PUMP": "泵房图",
}

#: 前缀 → 排序权重（决定 MANIFEST 与编号顺序）。
PREFIX_ORDER = ["P", "S", "D", "PUMP"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按规范命名归档脱敏 DXF 样本")
    ap.add_argument("--src", required=True, help="脱敏 DXF 目录")
    ap.add_argument("--map", required=True, help="分类映射 JSON：{sid: {cls, ...}}")
    ap.add_argument("--out", required=True, help="归档目录")
    ap.add_argument("--provenance", default="/tmp/p2_samples/provenance.json",
                    help="溯源映射输出路径（**必须在仓库外**）")
    ap.add_argument("--force", action="store_true", help="允许覆盖已存在的同名样本")
    args = ap.parse_args(argv)

    src, out = Path(args.src), Path(args.out)
    if not src.is_dir():
        print(f"前置条件缺失：脱敏目录不存在 {src}", file=sys.stderr)
        return 2
    mapping = json.loads(Path(args.map).read_text("utf-8"))
    if not mapping:
        print("分类映射为空", file=sys.stderr)
        return 2

    # 校验分类合法
    bad = {k: v for k, v in mapping.items() if v.get("cls") not in PREFIX_LABEL}
    if bad:
        print(f"分类非法（只允许 {sorted(PREFIX_LABEL)}）：{bad}", file=sys.stderr)
        return 2

    # 按前缀分组，组内按原 sid 排序后连续编号
    groups: dict[str, list[str]] = {p: [] for p in PREFIX_ORDER}
    for sid, meta in sorted(mapping.items()):
        groups[meta["cls"]].append(sid)

    out.mkdir(parents=True, exist_ok=True)
    provenance: dict[str, dict[str, str]] = {}
    plan: list[tuple[str, str, str]] = []  # (原 sid, 新样本号, 前缀)

    for prefix in PREFIX_ORDER:
        for i, sid in enumerate(groups[prefix], 1):
            new_id = f"{prefix}-{i:02d}"
            plan.append((sid, new_id, prefix))

    # 先整体检查冲突，再落盘 —— 避免"复制到一半发现冲突"
    conflicts = [
        new_id for _, new_id, _ in plan
        if (out / f"{new_id}.dxf").exists() and not args.force
    ]
    if conflicts:
        print(f"目标已存在同名样本（用 --force 覆盖）：{conflicts}", file=sys.stderr)
        return 2

    missing = [sid for sid, _, _ in plan if not (src / f"{sid}.dxf").is_file()]
    if missing:
        print(f"源目录缺少脱敏文件：{missing}", file=sys.stderr)
        return 2

    print(f"{'样本号':<9}{'类型':<7}{'来源sid':<9}{'字节':>10}")
    print("-" * 44)
    for sid, new_id, prefix in plan:
        s = src / f"{sid}.dxf"
        d = out / f"{new_id}.dxf"
        shutil.copy2(s, d)
        provenance[new_id] = {
            "cls": prefix,
            "label": PREFIX_LABEL[prefix],
            "source_sid": sid,
            "src_name": mapping[sid].get("orig_name", ""),
            "src_project": mapping[sid].get("proj", ""),
            "src_path": mapping[sid].get("path", ""),
        }
        print(f"{new_id:<9}{PREFIX_LABEL[prefix]:<7}{sid:<9}{d.stat().st_size:>10}")

    prov_path = Path(args.provenance)
    prov_path.parent.mkdir(parents=True, exist_ok=True)
    prov_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=1), encoding="utf-8")

    total = sum((out / f"{n}.dxf").stat().st_size for _, n, _ in plan)
    print(f"\n归档 {len(plan)} 份，合计 {total / 1048576:.1f} MB")
    for prefix in PREFIX_ORDER:
        n = len(groups[prefix])
        if n:
            print(f"  {PREFIX_LABEL[prefix]:<6} {prefix}-01 … {prefix}-{n:02d}  ({n} 份)")
    print(f"\n溯源映射（仓库外）: {prov_path}")
    print("注意：溯源映射含原始工程信息，**不得**复制进仓库或随样本分发。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
