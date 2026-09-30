"""节点大样图的几何规划（P6-S1 Step 3）：1:20，纯数据，不碰 ezdxf。

按建筑类型选节点（spec D2）：住宅 → 水表井（平面）；办公 → 吊顶内阀门组（立面）；
商业 → 泵房接管（平面）。附件的种类与顺序只来自规则表 ``detail.components``。

大样里的井体、楼板、吊顶、水泵基础都是**示意轮廓**：本步不做长度尺寸标注，
构造做法按设计院选用的标准图集确定，图面说明如实写明。所有管段按输入的主管管径，
泵房的水泵进出水管径另注"按水泵选型复核"。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from water_agent.drawing.building_rules import BuildingRules, DetailComponent, FittingKind
from water_agent.drawing.generation_models import (
    FITTING_SYMBOL,
    LAYER_METER,
    LAYER_OUTLINE,
    LAYER_PIPE_MAIN,
    LAYER_TEXT,
    LAYER_VALVE,
    DetailPlan,
    GenerationRequest,
    LabelPlan,
    LinePlan,
    PipeRunPlan,
    PlanNode,
    Point,
    SymbolKind,
    SymbolPlan,
    TextAlign,
    TextPlan,
    rounded,
)
from water_agent.drawing.layout_engine import (
    END_MARGIN_MM,
    SYMBOL_LENGTH_MM,
    TEXT_HEIGHT_MM,
    TITLE_HEIGHT_MM,
    compose_notes,
    label_in_zone,
    legend,
    note_blocks,
)

DETAIL_SCALE = 20
#: 大样里的符号按 2 倍图纸尺寸画（1:20 下 4 mm 的阀门太小，看不清组成）。
DETAIL_SYMBOL_SCALE = 2.0 * DETAIL_SCALE
_SYMBOL_LAYER = {SymbolKind.WATER_METER: LAYER_METER}


@dataclass(slots=True)
class _Parts:
    """一个大样的图元（尚未加标题、图例、说明）。``extent`` 为 (x0, y0, x1, y1)。"""

    nodes: list[PlanNode]
    pipes: list[PipeRunPlan]
    labels: list[LabelPlan]
    symbols: list[SymbolPlan]
    texts: list[TextPlan]
    lines: list[LinePlan]
    extent: tuple[float, float, float, float]
    extra: list[str] = field(default_factory=list)


def _rect(x0: float, y0: float, x1: float, y1: float) -> tuple[Point, ...]:
    return (rounded((x0, y0)), rounded((x1, y0)), rounded((x1, y1)), rounded((x0, y1)))


def _outline(points: Sequence[Point], *, closed: bool = True, linetype: str | None = None, role: str = "outline") -> LinePlan:
    return LinePlan(layer=LAYER_OUTLINE, points=tuple(points), closed=closed, linetype=linetype, role=role)


def _fitting(component: DetailComponent, run_id: str, point: Point, rotation: float) -> SymbolPlan:
    symbol = FITTING_SYMBOL[component.kind]
    return SymbolPlan(
        symbol=symbol,
        layer=_SYMBOL_LAYER.get(symbol, LAYER_VALVE),
        point=rounded(point),
        rotation=rotation,
        scale=DETAIL_SYMBOL_SCALE,
        run_id=run_id,
        name=component.label,
    )


def _name(text: str, point: Point, align: TextAlign = "BOTTOM_CENTER") -> TextPlan:
    return TextPlan(
        text=text,
        point=rounded(point),
        height=TEXT_HEIGHT_MM * DETAIL_SCALE,
        align=align,
        layer=LAYER_TEXT,
        role="fitting_label",
    )


def _arrow(point: Point, rotation: float) -> SymbolPlan:
    return SymbolPlan(
        symbol=SymbolKind.FLOW_ARROW,
        layer=LAYER_TEXT,
        point=rounded(point),
        rotation=rotation,
        scale=DETAIL_SYMBOL_SCALE,
        name="水流方向",
    )


def _meter_pit(request: GenerationRequest, components: Sequence[DetailComponent]) -> _Parts:
    """水表井（平面）：井内沿引入管依次布置附件。"""

    inner_w, inner_d, wall = 1400.0, 800.0, 240.0
    y = inner_d / 2
    left, right = -1000.0, inner_w + wall + 760.0
    nodes = [
        PlanNode(node_id="in", kind="entry", point=rounded((left, y))),
        PlanNode(node_id="out", kind="end", point=rounded((right, y))),
    ]
    run = PipeRunPlan(
        run_id="det.pipe",
        layer=LAYER_PIPE_MAIN,
        dn=request.main_pipe.dn,
        role="detail",
        node_ids=("in", "out"),
        points=(nodes[0].point, nodes[1].point),
    )
    height = TEXT_HEIGHT_MM * DETAIL_SCALE
    count = len(components)
    symbols, texts = [], []
    for index, component in enumerate(components, start=1):
        x = inner_w * index / (count + 1)
        symbols.append(_fitting(component, run.run_id, (x, y), 0.0))
        texts.append(_name(component.label, (x, y + 6.0 * DETAIL_SCALE)))
    label = label_in_zone(
        run,
        start=run.points[0],
        end=run.points[1],
        zone_from=END_MARGIN_MM * DETAIL_SCALE,
        zone_to=(-wall - left) - END_MARGIN_MM * DETAIL_SCALE,
        height=height,
    )
    lines = [_outline(_rect(0, 0, inner_w, inner_d)), _outline(_rect(-wall, -wall, inner_w + wall, inner_d + wall))]
    texts += [
        _name("水表井（井内尺寸示意）", (inner_w / 2, -wall - 4.0 * DETAIL_SCALE), "TOP_CENTER"),
        _name("引入管（接室外给水管网）", (left - 3.0 * DETAIL_SCALE, y), "MIDDLE_RIGHT"),
        _name("接给水立管 JL-1", (right + 3.0 * DETAIL_SCALE, y), "MIDDLE_LEFT"),
    ]
    symbols += [_arrow((left + 600.0, y - 0.0), 0.0), _arrow((right - 350.0, y), 0.0)]
    return _Parts(
        nodes=nodes,
        pipes=[run],
        labels=[label],
        symbols=symbols,
        texts=texts,
        lines=lines,
        extent=(left, -wall, right, inner_d + wall),
        extra=["井体尺寸为示意（本步不标注长度尺寸），井体、井盖与防冻做法按选用的标准图集确定"],
    )


def _ceiling_valve_group(request: GenerationRequest, components: Sequence[DetailComponent]) -> _Parts:
    """吊顶内阀门组（立面）：楼板下吊架托管，阀门组正下方吊顶留检修口。"""

    length, slab, pipe_y = 3000.0, 900.0, 450.0
    access_from, access_to = 900.0, 2100.0
    nodes = [
        PlanNode(node_id="in", kind="entry", point=rounded((0.0, pipe_y))),
        PlanNode(node_id="out", kind="end", point=rounded((length, pipe_y))),
    ]
    run = PipeRunPlan(
        run_id="det.pipe",
        layer=LAYER_PIPE_MAIN,
        dn=request.main_pipe.dn,
        role="detail",
        node_ids=("in", "out"),
        points=(nodes[0].point, nodes[1].point),
    )
    height = TEXT_HEIGHT_MM * DETAIL_SCALE
    count = len(components)
    symbols, texts = [], []
    for index, component in enumerate(components, start=1):
        x = access_from + (access_to - access_from) * index / (count + 1)
        symbols.append(_fitting(component, run.run_id, (x, pipe_y), 0.0))
        texts.append(_name(component.label, (x, pipe_y + 6.0 * DETAIL_SCALE)))
    label = label_in_zone(
        run, start=run.points[0], end=run.points[1], zone_from=2150.0, zone_to=2650.0, height=height
    )
    lines = [
        _outline(_rect(0, slab, length, slab + 120.0)),
        _outline((rounded((0.0, 0.0)), rounded((access_from, 0.0))), closed=False, linetype="DASHED", role="ceiling"),
        _outline((rounded((access_to, 0.0)), rounded((length, 0.0))), closed=False, linetype="DASHED", role="ceiling"),
        _outline((rounded((access_from, -30.0)), rounded((access_to, -30.0))), closed=False, role="access_panel"),
    ]
    for x in (300.0, 2700.0):
        lines.append(_outline((rounded((x, pipe_y + 60.0)), rounded((x, slab))), closed=False, role="hanger"))
    texts += [
        _name("楼板", (length + 3.0 * DETAIL_SCALE, slab + 60.0), "MIDDLE_LEFT"),
        _name("吊顶", (length + 3.0 * DETAIL_SCALE, 0.0), "MIDDLE_LEFT"),
        _name("检修口", ((access_from + access_to) / 2, -30.0 - 4.0 * DETAIL_SCALE), "TOP_CENTER"),
        _name("吊架", (300.0 + 2.0 * DETAIL_SCALE, (pipe_y + slab) / 2), "MIDDLE_LEFT"),
    ]
    symbols += [_arrow((600.0, pipe_y), 0.0)]
    return _Parts(
        nodes=nodes,
        pipes=[run],
        labels=[label],
        symbols=symbols,
        texts=texts,
        lines=lines,
        extent=(0.0, -30.0, length, slab + 120.0),
        extra=["楼板、吊顶与吊架为示意（本步不标注长度尺寸），检修口尺寸与吊架间距按选用的标准图集确定"],
    )


def _pump_room(request: GenerationRequest, components: Sequence[DetailComponent], pump_count: int) -> _Parts:
    """泵房接管（平面）：吸水总管 → 各泵支路（检修阀、水泵、缓闭止回阀、工作阀、检修阀）→ 出水总管。"""

    top, pitch, first = 2600.0, 1000.0, 1000.0
    xs = [first + i * pitch for i in range(pump_count)]
    exit_x = xs[-1] + 1800.0
    dn = request.main_pipe.dn
    nodes = [PlanNode(node_id="suction.start", kind="entry", point=rounded((0.0, 0.0)))]
    nodes += [PlanNode(node_id=f"suction.{i}", kind="tee", point=rounded((x, 0.0))) for i, x in enumerate(xs, start=1)]
    nodes += [PlanNode(node_id=f"discharge.{i}", kind="tee", point=rounded((x, top))) for i, x in enumerate(xs, start=1)]
    nodes.append(PlanNode(node_id="discharge.end", kind="end", point=rounded((exit_x, top))))
    suction = PipeRunPlan(
        run_id="det.suction",
        layer=LAYER_PIPE_MAIN,
        dn=dn,
        role="detail",
        node_ids=("suction.start", *[f"suction.{i}" for i in range(1, pump_count + 1)]),
        points=(rounded((0.0, 0.0)), *[rounded((x, 0.0)) for x in xs]),
    )
    discharge = PipeRunPlan(
        run_id="det.discharge",
        layer=LAYER_PIPE_MAIN,
        dn=dn,
        role="detail",
        node_ids=(*[f"discharge.{i}" for i in range(1, pump_count + 1)], "discharge.end"),
        points=(*[rounded((x, top)) for x in xs], rounded((exit_x, top))),
    )
    height = TEXT_HEIGHT_MM * DETAIL_SCALE
    margin = END_MARGIN_MM * DETAIL_SCALE
    pipes = [suction, discharge]
    labels = [
        label_in_zone(suction, start=suction.points[0], end=suction.points[1], zone_from=margin, zone_to=first - margin, height=height),
        label_in_zone(
            discharge,
            start=discharge.points[-2],
            end=discharge.points[-1],
            zone_from=margin,
            zone_to=exit_x - xs[-1] - margin,
            height=height,
        ),
    ]
    symbols: list[SymbolPlan] = []
    texts: list[TextPlan] = []
    lines: list[LinePlan] = []
    count = len(components)
    step = top / (count + 1)
    for i, x in enumerate(xs, start=1):
        run = PipeRunPlan(
            run_id=f"det.pump.{i}",
            layer=LAYER_PIPE_MAIN,
            dn=dn,
            role="detail",
            node_ids=(f"suction.{i}", f"discharge.{i}"),
            points=(rounded((x, 0.0)), rounded((x, top))),
        )
        pipes.append(run)
        for index, component in enumerate(components, start=1):
            y = step * index
            symbols.append(_fitting(component, run.run_id, (x, y), 90.0))
            if component.kind is FittingKind.PUMP:
                lines.append(_outline(_rect(x - 160.0, y - 190.0, x + 160.0, y + 190.0), role="pump_base"))
                texts.append(_name(f"{i}#泵" + ("（备用）" if i > 1 else ""), (x - 12.0 * DETAIL_SCALE, y), "MIDDLE_RIGHT"))
            elif i == 1:
                texts.append(_name(component.label, (x + 4.0 * DETAIL_SCALE, y), "MIDDLE_LEFT"))
        first_half = SYMBOL_LENGTH_MM[FITTING_SYMBOL[components[0].kind]] * DETAIL_SYMBOL_SCALE / 2
        labels.append(
            label_in_zone(
                run,
                start=run.points[0],
                end=run.points[1],
                zone_from=margin,
                zone_to=step - first_half - margin,
                height=height,
            )
        )
    texts += [
        _name("吸水总管（接自贮水箱，自灌式）", (-3.0 * DETAIL_SCALE, 0.0), "MIDDLE_RIGHT"),
        _name("出水总管（接至给水立管 JL-1）", (exit_x + 3.0 * DETAIL_SCALE, top), "MIDDLE_LEFT"),
        _name(f"水泵 {pump_count} 台（1 用 {pump_count - 1} 备，示意），进出水管径按水泵选型复核", (xs[0], -10.0 * DETAIL_SCALE), "TOP_CENTER"),
    ]
    symbols += [_arrow((400.0, 0.0), 0.0), _arrow((exit_x - 400.0, top), 0.0)]
    return _Parts(
        nodes=nodes,
        pipes=pipes,
        labels=labels,
        symbols=symbols,
        texts=texts,
        lines=lines,
        extent=(0.0, -10.0 * DETAIL_SCALE, exit_x, top),
        extra=[
            "水泵基础为示意轮廓；水泵选型、减振与管道支架按设计计算与选用图集确定",
            "图中水泵进出水管按主管管径示意，实际管径按水泵选型确定",
        ],
    )


def plan_details(request: GenerationRequest, rules: BuildingRules) -> list[DetailPlan]:
    """按建筑类型出节点大样（当前每种一张）。"""

    typed = rules.for_type(request.building_type)
    detail = typed.detail
    if detail.detail_type == "水表井":
        parts = _meter_pit(request, detail.components)
    elif detail.detail_type == "吊顶内阀门组":
        parts = _ceiling_valve_group(request, detail.components)
    else:
        parts = _pump_room(request, detail.components, detail.pump_count or 1)

    x0, y0, x1, y1 = parts.extent
    scale = DETAIL_SCALE
    height = TEXT_HEIGHT_MM * scale
    symbols = parts.symbols
    texts = parts.texts
    legend_symbols, legend_texts = legend(
        [s.symbol for s in symbols], left=x1 + 70.0 * scale, top=y1, scale=scale, symbol_scale=DETAIL_SYMBOL_SCALE
    )
    title = f"{detail.title} 1:{scale}"
    title_y = y0 - 25.0 * scale
    texts = [
        *texts,
        *legend_texts,
        TextPlan(
            text=title,
            point=rounded(((x0 + x1) / 2, title_y)),
            height=TITLE_HEIGHT_MM * scale,
            align="BOTTOM_CENTER",
            layer=LAYER_TEXT,
            role="title",
        ),
    ]
    rule_ids = (detail.rule_id, *detail.related_rules)
    notes, _ = note_blocks(
        compose_notes(request=request, rules=rules, rule_ids=rule_ids, extra=parts.extra),
        left=x0 - 40.0 * scale,
        top=title_y - 10.0 * scale,
        height=height,
    )
    return [
        DetailPlan(
            title=title,
            scale=scale,
            nodes=tuple(parts.nodes),
            pipes=tuple(parts.pipes),
            labels=tuple(parts.labels),
            symbols=tuple(symbols + legend_symbols),
            texts=tuple(texts),
            lines=tuple(parts.lines),
            notes=tuple(notes),
            rule_ids=rule_ids,
            detail_type=detail.detail_type,
        )
    ]


__all__ = ["DETAIL_SCALE", "DETAIL_SYMBOL_SCALE", "plan_details"]
