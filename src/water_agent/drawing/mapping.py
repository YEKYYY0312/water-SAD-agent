"""图纸图元 → 给排水语义对象（P3 Step 21）。

规格 `docs/design/13-p3-drawing-review.md` §5.2 与 Step 20 决策记录 §4。

本层的职责与边界
------------------
**输入是 `DrawingExtraction`，不是文件路径** —— 映射层不碰文件系统，
路径防御全部留在 `drawing/dxf_reader.py`。这不是分层洁癖：一旦映射层也能打开文件，
Step 20 建立的"模型无法表达路径"就多了一个绕过口。

三条硬纪律
------------
1. **`unit_known=False` → 结构化 `missing_inputs`，绝不换算长度**（规格 §5.2）。
   坐标是无单位数字，猜一个单位等于凭空造出工程量 —— 估算的长度进了材料清单
   就是错的采购数量，比"算不出来"危险得多。
2. **`suspicious` 非空的标注不参与任何数值推导**。可以报告"这条标注可疑"，
   但不能用它的内容定管径。投毒标注写个 `DN2000` 就能污染整张材料清单。
3. **管径只来自标注文本的显式声明**，不从线宽/图层猜。给排水图纸的线宽约定各院不同，
   猜出来的管径无法追溯到图面证据。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import pairwise

from water_agent.drawing.models import DrawingEntity, DrawingEntityKind, DrawingExtraction
from water_agent.materials.core import PipeRun

#: `$INSUNITS` → 每单位的米数。只登记工程图纸现实中会出现的单位。
#: **刻意不含 0（unitless）** —— 那是 `unit_known=False`，走 missing_inputs 而非换算。
_INSUNITS_TO_METRE: dict[int, float] = {
    1: 0.0254,   # in
    2: 0.3048,   # ft
    4: 0.001,    # mm —— 设计院最常见
    5: 0.01,     # cm
    6: 1.0,      # m
}

#: 管径标注：`DN200`、`dn 150`、`De110`（外径系列，塑料管常用）。
#: 只认显式的直径前缀 —— 裸数字 `200` 在图纸上可能是标高、编号、坐标，不可当管径。
_DN_RE = re.compile(r"\b(?:DN|De|dn|de)\s*(\d{2,4})\b")

#: 材质关键词 → 归一名称。按最长匹配优先（"镀锌钢管" 先于 "钢管"）。
_MATERIAL_HINTS: tuple[tuple[str, str], ...] = (
    ("球墨铸铁", "球墨铸铁"),
    ("镀锌钢管", "镀锌钢管"),
    ("钢筋混凝土", "钢筋混凝土"),
    ("不锈钢", "不锈钢"),
    ("灰口铸铁", "灰口铸铁"),
    ("铸铁", "铸铁"),
    ("钢管", "钢管"),
    ("混凝土", "混凝土"),
    ("HDPE", "HDPE"),
    ("PVC-U", "PVC-U"),
    ("UPVC", "UPVC"),
    ("PPR", "PPR"),
    ("PE", "PE"),
)

#: 标注与管线的关联半径（图纸单位）。标注通常压在管线旁而非端点上，
#: 故按"标注点到线段的距离"判定，超过此值视为不相关。
#: 这个值是**工程惯例而非规范**，故写死为常量并在结果里如实标注为假设。
ANNOTATION_MATCH_RADIUS = 12.0

#: 管线类图元（能成为管段的图元类型）。
_PIPE_KINDS = frozenset({DrawingEntityKind.LINE, DrawingEntityKind.POLYLINE})

#: 文本类图元（能承载标注的图元类型）。
_TEXT_KINDS = frozenset({DrawingEntityKind.TEXT, DrawingEntityKind.MTEXT})


@dataclass(frozen=True, slots=True)
class MappedPipe:
    """一条从图纸映射出的管段，附带图面证据。

    与 `materials.PipeRun` 的关系：`PipeRun` 是材料统计的**输入契约**（要求
    dn/length/material 齐全），`MappedPipe` 是**图纸事实**（这些字段可能缺）。
    两者不合并是刻意的：把可缺字段塞进 `PipeRun` 会让材料统计的必填约束失效，
    而那条约束正是"不猜工程量"的实现。转换由 `to_pipe_runs` 承担，缺字段就不转。
    """

    #: 段号。由图层 + handle 生成，稳定且能指回图元。
    segment_id: str
    #: 承载本段的图元 handle —— 缺陷定位与去重的主键。
    handle: str
    layer: str
    #: 系统归属（`water_supply` / `drainage` / None 表示图层未表达）。
    system: str | None
    #: 图纸单位下的长度（坐标算出，**未换算**）。
    length_raw: float
    #: 换算为米的长度；`unit_known=False` 时为 `None`（**不猜单位**）。
    length_m: float | None
    #: 公称管径 mm，只来自标注的显式声明。
    dn_mm: int | None
    material: str | None
    #: 提供 dn/material 的标注图元 handle —— 让每个数值都能指回图面证据。
    evidence_handles: tuple[str, ...] = ()
    #: 被跳过的可疑标注 handle（`suspicious` 非空，其内容不参与推导）。
    ignored_suspicious_handles: tuple[str, ...] = ()

    @property
    def ready_for_materials(self) -> bool:
        """是否可转为 `PipeRun`（材料统计要求 dn/length/material/system 齐全）。"""
        return (
            self.dn_mm is not None
            and self.length_m is not None
            and self.length_m > 0
            and bool(self.material)
            and self.system in ("water_supply", "drainage")
        )


@dataclass(slots=True)
class MappingResult:
    """映射结果：管段 + 缺参 + 假设。

    ``missing_inputs`` 与 `tools/hydraulics.py::CalcResult` 同一语义：
    **不是错误，是"要人补的输入"**。单位未声明就落在这里，而不是塞进 errors ——
    图纸本身没问题，是缺一个人才知道的事实。
    """

    pipes: list[MappedPipe] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    #: 图纸单位是否已知（原样承自 `DrawingExtraction`，便于下游不必回看）。
    unit_known: bool = False

    def to_dict(self) -> dict:
        return {
            "pipes": [
                {
                    "segment_id": p.segment_id,
                    "handle": p.handle,
                    "layer": p.layer,
                    "system": p.system,
                    "length_raw": round(p.length_raw, 6),
                    "length_m": None if p.length_m is None else round(p.length_m, 6),
                    "dn_mm": p.dn_mm,
                    "material": p.material,
                    "evidence_handles": list(p.evidence_handles),
                    "ignored_suspicious_handles": list(p.ignored_suspicious_handles),
                }
                for p in self.pipes
            ],
            "missing_inputs": list(self.missing_inputs),
            "assumptions": list(self.assumptions),
            "unit_known": self.unit_known,
        }


def _polyline_length(points: tuple[tuple[float, float], ...]) -> float:
    """折线累计长度（图纸单位）。少于 2 点长度为 0。"""
    total = 0.0
    for (x1, y1), (x2, y2) in pairwise(points):
        total += float(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5)
    return total


def _point_to_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    """点到线段（非直线）的最短距离。

    用线段而非无限直线：一条管线的标注应该在**这一段旁边**，
    而不是在它延长线的百米之外。
    """
    px, py = point
    x1, y1 = start
    x2, y2 = end
    dx, dy = x2 - x1, y2 - y1
    span_sq = dx * dx + dy * dy
    if span_sq == 0.0:
        return float(((px - x1) ** 2 + (py - y1) ** 2) ** 0.5)
    # 投影参数裁剪到 [0,1]，即落在线段内
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / span_sq))
    nx, ny = x1 + t * dx, y1 + t * dy
    return float(((px - nx) ** 2 + (py - ny) ** 2) ** 0.5)


def _distance_to_polyline(
    point: tuple[float, float], points: tuple[tuple[float, float], ...]
) -> float:
    """点到折线的最短距离（逐段取最小）。"""
    if len(points) < 2:
        if not points:
            return float("inf")
        return float(((point[0] - points[0][0]) ** 2 + (point[1] - points[0][1]) ** 2) ** 0.5)
    return min(
        _point_to_segment_distance(point, a, b) for a, b in pairwise(points)
    )


def parse_dn(text: str) -> int | None:
    """从标注文本提取公称管径 mm。多个取**最小**值。

    取最小而非最大：一条标注里同时出现 `DN200` 与 `DN150` 时（如"DN200 变 DN150"），
    按最小管径做后续校验是保守侧 —— 最小管径规则若因取大值而漏判，
    结果是把不合规的管段报成合规，那正是审查工具最不该犯的错。
    """
    matches = _DN_RE.findall(text or "")
    if not matches:
        return None
    values = [int(m) for m in matches]
    return min(values)


def parse_material(text: str) -> str | None:
    """从标注文本提取材质（最长匹配优先，未识别返回 None）。

    **不猜默认材质**：`_MATERIAL_HINTS` 没命中就是 None，让它落进 missing_inputs。
    给排水管材差价很大，猜错材质的材料清单直接影响造价。
    """
    haystack = text or ""
    for keyword, normalised in _MATERIAL_HINTS:
        if keyword in haystack:
            return normalised
    return None


def metres_per_drawing_unit(insunits: int | None) -> float | None:
    """`$INSUNITS` → 每图纸单位的米数；未知或不支持时不猜。"""
    if insunits is None:
        return None
    return _INSUNITS_TO_METRE.get(insunits)


def _layer_system(layer: str) -> str | None:
    """图层名 → 系统归属。复用解析器同一口径，避免两处漂移。"""
    from water_agent.drawing.dxf_reader import _layer_system as reader_layer_system

    return reader_layer_system(layer)


def map_pipes(extraction: DrawingExtraction) -> MappingResult:
    """`DrawingExtraction` → 管段语义对象。

    步骤：管线图元定长度 → 邻近标注补 dn/material → 单位换算（可用时）。
    每一步缺什么就如实留空 + 记 missing_inputs，**不填默认值**。
    """
    result = MappingResult(unit_known=extraction.unit_known)
    if not extraction.ok:
        result.missing_inputs.append(
            "图纸抽取失败，无可映射图元（先解决 DrawingExtraction.errors）"
        )
        return result

    scale = metres_per_drawing_unit(extraction.insunits) if extraction.unit_known else None
    if scale is None:
        # 规格 §5.2：这里是硬性的 missing_inputs，不是 assumption。
        # assumption 的语义是"我算了但你要核对"，这里是"我拒绝算"。
        result.missing_inputs.append(
            "drawing_unit：图纸未声明单位（$INSUNITS 缺失/为 0/为不支持的单位），"
            "长度无法换算为米。请人工确认图纸单位后重新提供，"
            "解析器不按默认单位换算（估算的长度会变成错误的工程量）"
        )

    pipes_raw = [e for e in extraction.entities if e.kind in _PIPE_KINDS and len(e.points) >= 2]
    texts = [e for e in extraction.entities if e.kind in _TEXT_KINDS and e.points]

    # 可疑标注整体排除：它们的内容不得参与任何数值推导（Step 20 决策记录 §4）。
    trusted_texts = [t for t in texts if not t.suspicious]
    suspicious_texts = [t for t in texts if t.suspicious]
    if suspicious_texts:
        result.assumptions.append(
            f"{len(suspicious_texts)} 条标注命中内容护栏（suspicious 非空），"
            "其文本未参与管径/材质推导；如需采用请人工核对图面原文"
        )

    for entity in pipes_raw:
        length_raw = _polyline_length(entity.points)
        nearby = _nearby_annotations(entity, trusted_texts)
        dn, material, evidence = _extract_attributes(nearby)
        ignored = tuple(
            t.handle
            for t in suspicious_texts
            if _distance_to_polyline(t.points[0], entity.points) <= ANNOTATION_MATCH_RADIUS
        )
        result.pipes.append(
            MappedPipe(
                segment_id=f"{entity.layer}#{entity.handle}",
                handle=entity.handle,
                layer=entity.layer,
                system=_layer_system(entity.layer),
                length_raw=length_raw,
                length_m=None if scale is None else length_raw * scale,
                dn_mm=dn,
                material=material,
                evidence_handles=evidence,
                ignored_suspicious_handles=ignored,
            )
        )

    _record_attribute_gaps(result)
    if result.pipes:
        result.assumptions.append(
            f"管段属性按标注与管线的图面邻近关系匹配（半径 {ANNOTATION_MATCH_RADIUS} 图纸单位）；"
            "这是工程惯例而非规范规定，标注位置异常的图纸需人工复核对应关系"
        )
    return result


def _nearby_annotations(
    pipe: DrawingEntity, texts: list[DrawingEntity]
) -> list[DrawingEntity]:
    """按图面距离找与管线相关的标注（近者优先）。"""
    scored = [
        (_distance_to_polyline(t.points[0], pipe.points), t)
        for t in texts
        if t.points
    ]
    return [t for distance, t in sorted(scored, key=lambda item: item[0])
            if distance <= ANNOTATION_MATCH_RADIUS]


def _extract_attributes(
    annotations: list[DrawingEntity],
) -> tuple[int | None, str | None, tuple[str, ...]]:
    """从邻近标注取 dn/material，返回 (dn, material, 证据 handle)。

    近者优先：第一条给出某属性的标注即采用，后续不覆盖 ——
    覆盖会让结果依赖遍历顺序，而顺序不是工程事实。
    """
    dn: int | None = None
    material: str | None = None
    evidence: list[str] = []
    for annotation in annotations:
        used = False
        if dn is None:
            parsed = parse_dn(annotation.text)
            if parsed is not None:
                dn = parsed
                used = True
        if material is None:
            parsed_material = parse_material(annotation.text)
            if parsed_material is not None:
                material = parsed_material
                used = True
        if used:
            evidence.append(annotation.handle)
        if dn is not None and material is not None:
            break
    return dn, material, tuple(evidence)


def _record_attribute_gaps(result: MappingResult) -> None:
    """把缺失的管段属性汇总进 missing_inputs（按属性聚合，不逐段刷屏）。"""
    no_dn = [p.segment_id for p in result.pipes if p.dn_mm is None]
    no_material = [p.segment_id for p in result.pipes if p.material is None]
    no_system = [p.segment_id for p in result.pipes if p.system is None]
    if no_dn:
        result.missing_inputs.append(
            f"dn_mm：{len(no_dn)} 条管段无管径标注（{_sample(no_dn)}）。"
            "管径只取自标注的显式 DN/De 声明，不从线宽或图层推断"
        )
    if no_material:
        result.missing_inputs.append(
            f"material：{len(no_material)} 条管段无材质标注（{_sample(no_material)}）。"
            "不假定默认管材（管材差价大，猜错直接影响造价）"
        )
    if no_system:
        result.missing_inputs.append(
            f"system：{len(no_system)} 条管段的图层名未表达给水/排水归属（{_sample(no_system)}）"
        )


def _sample(items: list[str], limit: int = 3) -> str:
    shown = "、".join(items[:limit])
    return shown + ("…" if len(items) > limit else "")


def to_pipe_runs(result: MappingResult) -> tuple[list[PipeRun], list[str]]:
    """把**属性齐全**的管段转为 `materials.PipeRun`，返回 (管段, 跳过说明)。

    这是 P3 → P2 材料统计的接缝。**不齐全的段一律跳过并说明** ——
    补个默认值凑进材料清单，等于用猜出来的数字当采购依据。
    """
    runs: list[PipeRun] = []
    skipped: list[str] = []
    for pipe in result.pipes:
        if not pipe.ready_for_materials:
            missing = [
                name
                for name, value in (
                    ("dn_mm", pipe.dn_mm),
                    ("length_m", pipe.length_m),
                    ("material", pipe.material),
                    ("system", pipe.system if pipe.system in ("water_supply", "drainage") else None),
                )
                if value is None
            ]
            skipped.append(f"{pipe.segment_id}：缺 {'/'.join(missing)}")
            continue
        # mypy：ready_for_materials 已保证以下字段非 None
        assert pipe.dn_mm is not None and pipe.length_m is not None
        assert pipe.material is not None and pipe.system is not None
        runs.append(
            PipeRun(
                segment_id=pipe.segment_id,
                system=pipe.system,
                dn_mm=pipe.dn_mm,
                length_m=pipe.length_m,
                material=pipe.material,
            )
        )
    return runs, skipped
