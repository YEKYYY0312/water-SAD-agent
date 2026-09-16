#!/usr/bin/env python3
"""drawing_p1_implant 植入缺陷子集生成器（真实修正思路）。

设计（与 build_cases.py 的关系）
--------------------------------
不照搬 build_cases.py 的“整图合成”思路：全合成图测的是链路，
测不出真实图纸的噪声（非标图层、INSERT、多余几何）。

本脚本只做一件事：**在真实 DXF 的副本上，加一条新排水管段
（DR-PIPE 图层 LINE + 标注 TEXT），其余图元原样保留**。
植入管段的参数（管径/材质/坐标）由 CASE_SPECS 声明，gold 的
handle 从保存后重读取权威值（与 build_cases.py 同一手法）。

判分口径与 drawing_p0 完全一致：
- gold 缺陷按 (rule_id, handle) 精确匹配；
- 缺陷判定与规则引擎一致：显式 DN < 300 即 gold 缺陷；
- 每个 case 必须期望坡度/流速两条恒未检查（真实图含排水管段时）；
- 合规管段（DN400）只测误报，不产生 gold 缺陷。

植入位置选择（避免与原图几何纠缠，且保证默认 max_entities=500 可见）：
- 植入段插在模型空间**最前面**（``entity_space.remove`` + ``insert(0)`` 前移，
  经实测 read_drawing 默认前 500 即含植入段；此前“包围盒外 +100 追加尾部”
  方案因截断丢弃已废弃；``delete_entity`` 会销毁实体—— 不得用，已实测）；
- 坐标取包围盒 min - 100（远离原图几何，避免邻近匹配串扰）；
- 标注贴管线旁 3 个单位（ANNOTATION_MATCH_RADIUS=12 之内）；
- 查找原图包围盒时只看 LINE/POLYLINE 顶点，失败则回退到固定原点。

脱敏与口径：
- 输出 DXF 经 read_drawing 可读性验证；审查结论仅报告植入段，
  不对底图做任何合规断言（negative_set 已证明底图干净）。
- suite.json 明确 real_samples=true（底图真实）+ synthetic_implants=true
  （缺陷为程序化植入）+ accepts_recall_gate=false（门禁语义待定）。

用法：
    _venv/bin/python tests/eval/drawing_p1_implant/build_implants.py
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import ezdxf

from water_agent.drawing.rules import MIN_DRAIN_DN_MM  # type: ignore[import-untyped]

HERE = Path(__file__).resolve().parent
CASES = HERE / "cases"
REAL_SAMPLES = Path(__file__).resolve().parents[3] / "data" / "drawings" / "real_samples"

DXF_VERSION = "R2010"
#: 植入段统一用 DR-PIPE（LINE + 标注同层，保证 _layer_system 判 drainage；
#: 故意不用 DR-排水-标注—— 那一层 _layer_system 判 None，会把 dn 关联丢掉）。
LAYER_DRAIN_PIPE = "DR-PIPE"

SLOPE_RULE = "drain.horizontal_slope_range"
VELOCITY_RULE = "drain.pressure_flow_velocity"
MIN_DN_RULE = "drain.min_diameter"
DRAIN_ALWAYS_UNCHECKED = [SLOPE_RULE, VELOCITY_RULE]

# 底图选择：P-01（平面图，毫米单位）+ S-03（系统图）。
# 两张都是目视确认干净的阴性样本；类型覆盖平面/系统。
CASE_SPECS: list[dict] = [
    {
        "case_id": "pi-001-p01-single-violation",
        "base_drawing": "P-01",
        "description": "P-01 底图植入 DN200 排水管 → 应报 drain.min_diameter",
        "expected_status": "succeeded",
        "expected_summary_contains": ["发现 1 条待核问题"],
        "expected_not_checked_contains": DRAIN_ALWAYS_UNCHECKED,
        "implant": {"dn": 200, "material": "钢筋混凝土"},
        "gold_defect_expected": True,
    },
    {
        "case_id": "pi-002-p01-two-violations",
        "base_drawing": "P-01",
        "description": "P-01 底图植入 DN150 + DN200 两段 → 逐条覆盖（分母 2）",
        "expected_status": "succeeded",
        "expected_summary_contains": ["发现 2 条待核问题"],
        "expected_not_checked_contains": DRAIN_ALWAYS_UNCHECKED,
        "implants": [
            {"dn": 150, "material": "钢筋混凝土"},
            {"dn": 200, "material": "PVC-U"},
        ],
        "gold_defect_expected": True,
    },
    {
        "case_id": "pi-003-p01-compliant",
        "base_drawing": "P-01",
        "description": "P-01 底图植入 DN400 合规管 → 0 缺陷（植入不误报）",
        "expected_status": "succeeded",
        "expected_summary_contains": ["未检查"],
        "expected_not_checked_contains": DRAIN_ALWAYS_UNCHECKED,
        "implant": {"dn": 400, "material": "钢筋混凝土"},
        "gold_defect_expected": False,
    },
    {
        "case_id": "pi-004-s03-single-violation",
        "base_drawing": "S-03",
        "description": "S-03 底图植入 DN200 排水管 → 应报 drain.min_diameter",
        "expected_status": "succeeded",
        "expected_summary_contains": ["发现 1 条待核问题"],
        "expected_not_checked_contains": DRAIN_ALWAYS_UNCHECKED,
        "implant": {"dn": 200, "material": "钢筋混凝土"},
        "gold_defect_expected": True,
    },
    {
        "case_id": "pi-005-s03-compliant",
        "base_drawing": "S-03",
        "description": "S-03 底图植入 DN400 合规管 → 0 缺陷（植入不误报）",
        "expected_status": "succeeded",
        "expected_summary_contains": ["未检查"],
        "expected_not_checked_contains": DRAIN_ALWAYS_UNCHECKED,
        "implant": {"dn": 400, "material": "钢筋混凝土"},
        "gold_defect_expected": False,
    },
]

IMPLANT_LENGTH = 40.0
IMPLANT_GAP = 100.0
SECOND_OFFSET_Y = 100.0


def _model_extent(doc: ezdxf.document.Drawing) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for e in doc.modelspace():
        try:
            dxftype = e.dxftype()
        except Exception:  # noqa: BLE001,S112 — 坏图元跳过，包围盒只取可读顶点
            continue
        if dxftype not in ("LINE", "LWPOLYLINE", "POLYLINE"):
            continue
        try:
            if dxftype == "LINE":
                xs += [float(e.dxf.start[0]), float(e.dxf.end[0])]
                ys += [float(e.dxf.start[1]), float(e.dxf.end[1])]
            else:
                pts = list(e.get_points())  # type: ignore[attr-defined]
                xs += [float(p[0]) for p in pts]
                ys += [float(p[1]) for p in pts]
        except Exception:  # noqa: BLE001,S112 — 同上，单图元失败不中断包围盒
            continue
    if not xs:
        return 0.0, 0.0, 0.0, 0.0
    return min(xs), min(ys), max(xs), max(ys)


def _add_implant(
    msp: ezdxf.layouts.BaseLayout, *, x0: float, y0: float, dn: int, material: str
) -> str:
    """加一段植入管线，返回生成时 handle（调用方重读校验权威值）。

    植入段与底图隔离：LINE 与标注都用 ``DR-PIPE``（``_layer_system`` 判 drainage
    的前缀），且坐标取包围盒 min - 100—— 邻近匹配只会把植入标注配给植入管段，
    不会污染底图管段；底图管段因无 DN 标注本就判不出缺陷（阴性底图已验证）。
    """
    try:
        msp.doc.layers.add(LAYER_DRAIN_PIPE)
    except Exception:  # noqa: BLE001,S110 — 层已存在时 add 抛错，忽略即可
        pass
    line = msp.add_line(
        (x0, y0), (x0 + IMPLANT_LENGTH, y0), dxfattribs={"layer": LAYER_DRAIN_PIPE}
    )
    handle = str(line.dxf.handle)
    text = msp.add_text(
        f"DN{dn} {material}", dxfattribs={"layer": LAYER_DRAIN_PIPE, "height": 2.5}
    )
    text.set_placement((x0 + 5.0, y0 + 3.0))
    _move_to_front(msp, line)
    _move_to_front(msp, text)
    return handle


def _move_to_front(msp: ezdxf.layouts.BaseLayout, entity: Any) -> None:
    """把刚加的实体搬到模型空间最前面（保证默认截断下可见）。

    实测 ``Modelspace.delete_entity`` 会销毁实体（``dxf`` 属性丢失，
    保存后实体消失）；正确做法是底层 ``entity_space.remove`` +
    ``insert(0, entity)``，实体存活且重读后仍在首位（同文件内 probe 验证）。
    失败即抛错，不静默产出坏图。
    """
    msp.entity_space.remove(entity)
    msp.entity_space.insert(0, entity)


def _find_handle_by_start(doc: ezdxf.document.Drawing, start: tuple[float, float]) -> str:
    for e in doc.modelspace():
        if e.dxftype() != "LINE":
            continue
        s = e.dxf.start
        if abs(float(s[0]) - start[0]) < 1e-9 and abs(float(s[1]) - start[1]) < 1e-9:
            return str(e.dxf.handle)
    raise RuntimeError(f"找不到起点 {start} 的植入 LINE")


def build_case(spec: dict) -> dict:
    case_id = spec["case_id"]
    base = REAL_SAMPLES / f"{spec['base_drawing']}.dxf"
    if not base.is_file():
        raise FileNotFoundError(f"底图缺失: {base}（real_samples 未检出？）")
    CASES.mkdir(parents=True, exist_ok=True)
    dxf_path = CASES / f"{case_id}.dxf"
    gold_path = CASES / f"{case_id}.json"

    shutil.copyfile(base, dxf_path)
    doc = ezdxf.readfile(str(dxf_path))
    minx, miny, _maxx, _maxy = _model_extent(doc)
    msp = doc.modelspace()

    implants = spec.get("implants") or [spec["implant"]]
    starts: list[tuple[float, float]] = []
    gen_handles: list[str] = []
    for i, imp in enumerate(implants):
        x0 = minx - IMPLANT_GAP - IMPLANT_LENGTH
        y0 = miny - IMPLANT_GAP - i * SECOND_OFFSET_Y
        gen_handles.append(
            _add_implant(msp, x0=x0, y0=y0, dn=int(imp["dn"]), material=str(imp["material"]))
        )
        starts.append((x0, y0))
    doc.saveas(str(dxf_path))

    reopened = ezdxf.readfile(str(dxf_path))
    defects: list[dict] = []
    for imp, start, gen in zip(implants, starts, gen_handles, strict=True):
        authoritative = _find_handle_by_start(reopened, start)
        if authoritative != gen:
            raise RuntimeError(
                f"{case_id}: 植入 handle 与重读权威值不一致（生成时 {gen}，重读 {authoritative}）"
            )
        if int(imp["dn"]) < MIN_DRAIN_DN_MM:
            defects.append(
                {
                    "rule_id": MIN_DN_RULE,
                    "handle": authoritative,
                    "segment_id": f"{LAYER_DRAIN_PIPE}#{authoritative}",
                    "layer": LAYER_DRAIN_PIPE,
                    "evidence": (
                        f"植入标注 DN{imp['dn']} {imp['material']}"
                        f"（DN{imp['dn']} < 下限 DN{MIN_DRAIN_DN_MM}）"
                    ),
                }
            )
    if spec.get("gold_defect_expected"):
        assert defects, f"{case_id}: 期望 gold 缺陷但未生成"
    else:
        assert not defects, f"{case_id}: 合规植入不应产生 gold 缺陷"

    gold = {
        "case_id": case_id,
        "description": spec.get("description", ""),
        "drawing": f"{case_id}.dxf",
        "base_drawing": spec["base_drawing"],
        "synthetic_implants": True,
        "expected_status": spec.get("expected_status", "succeeded"),
        "expected_summary_contains": list(spec.get("expected_summary_contains", [])),
        "expected_not_checked_contains": list(spec.get("expected_not_checked_contains", [])),
        "expected_missing_inputs_contains": list(spec.get("expected_missing_inputs_contains", [])),
        "defects": defects,
    }
    gold_path.write_text(json.dumps(gold, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return gold


def main() -> int:
    generated = [build_case(spec) for spec in CASE_SPECS]
    summary = {
        "ok": True,
        "cases_dir": str(CASES),
        "generated": len(generated),
        "gold_defects": sum(len(g["defects"]) for g in generated),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
