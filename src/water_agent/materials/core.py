"""材料统计引擎（P2）：管段表 → 按「材质+管径」聚合的材料清单。

铁律对齐：
- **数值程序化**：所有数量由本模块对输入管段长度求和，LLM 不参与数值运算；
- **可追溯**：每行材料都带 ``segments``（参与该行的管段号清单）与 ``detail``
  （逐段长度明细），任何数值都能指回来源管段；
- **不静默造数**：缺字段/负长度/重复管段号一律返回结构化 ``errors``（ok=False），
  不猜数、不丢弃输入。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 数量保留小数位：口径单一来源，评测 gold 与工具输出共用（sum → round(3)）
QUANTITY_PRECISION = 3
PIPE_UNIT = "m"

# 常见管材（用于 assumptions 提示；未知材质不阻断，只提示人工核对）
_KNOWN_MATERIALS = frozenset(
    {
        "球墨铸铁", "铸铁", "灰口铸铁",
        "钢管", "镀锌钢管", "不锈钢", "钢塑复合",
        "HDPE", "PE", "PPR", "PVC-U", "UPVC", "钢筋混凝土", "混凝土",
    }
)

# 允许的给排水系统（数据校验用；其他系统显式拒绝，防打错字静默通过）
_ALLOWED_SYSTEMS = frozenset({"water_supply", "drainage"})


class MaterialInputError(ValueError):
    """管段输入不合法（结构化问题清单），与工具层的结构化错误语义对齐。"""


@dataclass(frozen=True, slots=True)
class PipeRun:
    """一条管段：唯一段号 + 系统 + 管径 + 长度 + 材质。

    长度/管径必须为正、材质与段号非空；数值约束在聚合入口统一校验，
    dataclass 本身不做（保持输入模型与工具 pydantic schema 单点校验）。
    """

    segment_id: str
    system: str
    dn_mm: int
    length_m: float
    material: str


@dataclass(slots=True)
class MaterialRow:
    """一行材料：材质+规格聚合数量，带完整来源。"""

    material: str
    spec: str              # 规格，如 DN150
    unit: str = PIPE_UNIT
    quantity: float = 0.0  # round(QUANTITY_PRECISION) 后的数量
    raw_quantity: float = 0.0  # 未舍入的精确和（口径审计用）
    segments: list[str] = field(default_factory=list)  # 参与本行的管段号（有序去重）
    detail: str = ""       # 逐段长度明细，如 "S1(120m)、S2(80m)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "material": self.material,
            "spec": self.spec,
            "unit": self.unit,
            "quantity": self.quantity,
            "raw_quantity": round(self.raw_quantity, 6),
            "segments": list(self.segments),
            "detail": self.detail,
        }


@dataclass(slots=True)
class MaterialTable:
    """聚合结果：行清单 + 汇总 + 问题（ok=False 时 rows 为空）。"""

    rows: list[MaterialRow] = field(default_factory=list)
    total_length_m: float = 0.0
    errors: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "rows": [row.to_dict() for row in self.rows],
            "total_length_m": round(self.total_length_m, QUANTITY_PRECISION),
            "errors": list(self.errors),
            "assumptions": list(self.assumptions),
        }


def _fmt_length(value: float) -> str:
    """长度展示：去尾零（120.0 → 120），保留足够精度。"""
    return f"{value:g}"


def _fmt_qty(value: float) -> float:
    """数量舍入（口径单一来源：round(QUANTITY_PRECISION)）。"""
    return round(value, QUANTITY_PRECISION)


def _parse_segments(segments: list[dict[str, Any]] | list[PipeRun]) -> tuple[list[PipeRun], list[str]]:
    """把输入统一为 PipeRun 并做整体校验；返回 (管段, 问题清单)。"""
    errors: list[str] = []
    runs: list[PipeRun] = []
    if not segments:
        return [], ["管段表为空：没有可统计的管段"]
    seen_ids: set[str] = set()
    for index, raw in enumerate(segments):
        tag = f"第 {index + 1} 段"
        if isinstance(raw, PipeRun):
            run = raw
            raw = {
                "segment_id": run.segment_id,
                "system": run.system,
                "dn_mm": run.dn_mm,
                "length_m": run.length_m,
                "material": run.material,
            }
        if not isinstance(raw, dict):
            errors.append(f"{tag}：不是对象")
            continue
        segment_id = str(raw.get("segment_id") or "").strip()
        material = str(raw.get("material") or "").strip()
        if not segment_id:
            errors.append(f"{tag}：缺少 segment_id")
        elif segment_id in seen_ids:
            errors.append(f"{tag}：segment_id 重复（{segment_id!r}），会造成长度重复计数")
        seen_ids.add(segment_id)
        if not material:
            errors.append(f"{tag}（{segment_id or '?'}）：缺少材质 material")

        dn_raw = raw.get("dn_mm")
        length_raw = raw.get("length_m")
        try:
            dn_number = float(str(dn_raw)) if dn_raw is not None else 0
            # 管径必须是整数：小数/字符串型"150.7"按非法处理，不静默截断
            dn = int(dn_number) if float(dn_number).is_integer() else 0
        except (TypeError, ValueError):
            dn = 0
        try:
            length = float(str(length_raw)) if length_raw is not None else 0.0
        except (TypeError, ValueError):
            length = 0.0
        system = str(raw.get("system") or "water_supply").strip()
        if dn <= 0:
            errors.append(f"{tag}（{segment_id or '?'}）：dn_mm 必须为正整数，得到 {dn_raw!r}")
        if not (length > 0):
            errors.append(f"{tag}（{segment_id or '?'}）：length_m 必须为正数，得到 {length_raw!r}")
        if system and system not in _ALLOWED_SYSTEMS:
            errors.append(
                f"{tag}（{segment_id or '?'}）：未知系统 {system!r}，允许：{sorted(_ALLOWED_SYSTEMS)}"
            )
        runs.append(
            PipeRun(
                segment_id=segment_id or f"<空:{index}>",
                system=system or "water_supply",
                dn_mm=dn or -1,
                length_m=length,
                material=material or "<空>",
            )
        )
    return runs, errors


def aggregate_materials(
    segments: list[dict[str, Any]] | list[PipeRun],
) -> MaterialTable:
    """管段表 → 按 (材质, 规格 DNxxx) 分组的材料清单。

    - 数量 = 组内各管段长度之和（程序化）；舍入到 QUANTITY_PRECISION；
    - 行顺序 = (材质, 规格) 字典序，确定性输出；
    - 任意输入问题（空表/负长度/重复段号/未知系统）→ errors，不产出部分行。
    """
    runs, errors = _parse_segments(segments)
    if errors:
        return MaterialTable(errors=errors)

    assumptions: list[str] = []
    groups: dict[tuple[str, int], list[PipeRun]] = {}
    for run in runs:
        groups.setdefault((run.material, run.dn_mm), []).append(run)
        if run.material not in _KNOWN_MATERIALS:
            assumptions.append(f"管段 {run.segment_id} 材质 {run.material!r} 不在常见管材清单内，请人工核对")

    rows: list[MaterialRow] = []
    total = 0.0
    for (material, dn) in sorted(groups):
        group_runs = groups[(material, dn)]
        raw_total = sum(run.length_m for run in group_runs)
        total += raw_total
        seg_ids: list[str] = []
        details: list[str] = []
        for run in group_runs:
            if run.segment_id not in seg_ids:
                seg_ids.append(run.segment_id)
            details.append(f"{run.segment_id}({_fmt_length(run.length_m)}{PIPE_UNIT})")
        rows.append(
            MaterialRow(
                material=material,
                spec=f"DN{dn}",
                unit=PIPE_UNIT,
                quantity=_fmt_qty(raw_total),
                raw_quantity=raw_total,
                segments=seg_ids,
                detail="、".join(details),
            )
        )

    return MaterialTable(
        rows=rows,
        total_length_m=_fmt_qty(total),
        assumptions=assumptions,
    )


def material_rows_from_dicts(rows: list[dict[str, Any]]) -> list[MaterialRow]:
    """把 JSON 形态的材料行转回 MaterialRow（导出/评测读取用）。"""
    output: list[MaterialRow] = []
    for row in rows:
        output.append(
            MaterialRow(
                material=str(row["material"]),
                spec=str(row.get("spec", "")),
                unit=str(row.get("unit", PIPE_UNIT)),
                quantity=float(row.get("quantity", 0.0)),
                raw_quantity=float(row.get("raw_quantity", row.get("quantity", 0.0))),
                segments=[str(s) for s in row.get("segments", [])],
                detail=str(row.get("detail", "")),
            )
        )
    return output
