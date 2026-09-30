"""给水系统轴测图的几何规划（P6-S1 Step 3）：45° 斜等测，纯数据，不碰 ezdxf。

画法：立管竖直、楼层横干管水平（x 向）、各单元支管沿进深方向画成 45° 斜线。
竖向按真实层高（楼层标高 = (层号 − 1) × 层高，层高是输入），水平方向为示意。

- **立管逐层分段**：第 k 段从下一层（或引入管底部）的三通到第 k 层的三通，段数 = 层数，
  每段各有管径标注——"每层一根立管"是可数的事实，不是一根线从底画到顶。
- **每层一组支管**：组数 = 层数；楼层附件（阀门 / 楼层水表）紧跟立管三通。
- 支管斜向随安装方式定：沿地面安装（住宅）向右上，吊顶内安装（办公、商业）向右下，
  保证斜支管不越过上下楼层线。
- 管径变化点就是横干管上的各三通：横干管按主管管径、支管按支管管径分别标注。
  立管全高按输入的主管管径，**不做逐层变径**（缺逐层设计秒流量，不猜）。
"""

from __future__ import annotations

import math

from water_agent.drawing.building_rules import BuildingRules, FittingKind
from water_agent.drawing.generation_models import (
    FITTING_SYMBOL,
    LAYER_DIM,
    LAYER_PIPE_BRANCH,
    LAYER_PIPE_MAIN,
    LAYER_TEXT,
    GenerationRequest,
    LabelPlan,
    LinePlan,
    PipeRunPlan,
    PlanNode,
    Point,
    SchematicPlan,
    SymbolKind,
    SymbolPlan,
    TextPlan,
    check_velocities,
    drawing_velocity_note,
    rounded,
)
from water_agent.drawing.layout_engine import (
    END_MARGIN_MM,
    FITTING_GAP_MM,
    LABEL_GAP_MM,
    RISER_TAG_TEXT,
    SYMBOL_LENGTH_MM,
    TEXT_HEIGHT_MM,
    TITLE_HEIGHT_MM,
    Fitting,
    compose_notes,
    label_in_zone,
    legend,
    mount_label,
    note_blocks,
    place_fittings,
    text_width,
)

SCHEMATIC_SCALE = 100
#: 楼层横干管上相邻两个单元三通的间距、斜支管长度（图纸毫米）。
UNIT_SPACING_MM = 24.0
BRANCH_LENGTH_MM = 24.0
#: 引入管画在 ±0.000 以下这么深的位置（示意，**不标注标高**）。
ENTRY_DEPTH_MM = 15.0
#: 楼层线从立管右侧这么远处起画，避开立管左侧的管径标注。
FLOOR_LINE_START_MM = 3.0
_SQRT_HALF = math.sqrt(0.5)


def _fitting_span(fittings: list[Fitting], scale: int) -> float:
    total = 0.0
    for fitting in fittings:
        total += SYMBOL_LENGTH_MM[FITTING_SYMBOL[fitting.kind]] * scale + FITTING_GAP_MM * scale
    return total


def _ceil_to(value: float, step: float) -> float:
    return math.ceil(value / step) * step


def elevation_text(elevation_mm: float) -> str:
    """标高写法：米、三位小数，零点写 ±0.000。"""

    metres = round(elevation_mm / 1000.0, 3)
    return "±0.000" if metres == 0 else f"{metres:.3f}"


def plan_schematic(request: GenerationRequest, rules: BuildingRules) -> SchematicPlan:
    typed = rules.for_type(request.building_type)
    routing, fittings_rule, entry = typed.routing, typed.fittings, typed.entry
    scale = SCHEMATIC_SCALE
    height = TEXT_HEIGHT_MM * scale
    main, branch = request.main_pipe.dn, request.branch_pipe.dn
    floors, units = request.floors, request.units_per_floor
    floor_height = request.floor_height_m * 1000.0
    if routing.mount == "floor":
        mount = routing.mount_offset_m * 1000.0
        slope = 1.0
    else:
        mount = floor_height - routing.mount_offset_m * 1000.0
        slope = -1.0
    header_y = [(k - 1) * floor_height + mount for k in range(1, floors + 1)]
    entry_y = -ENTRY_DEPTH_MM * scale
    margin, gap = END_MARGIN_MM * scale, LABEL_GAP_MM * scale

    floor_fittings = [Fitting(fittings_rule.floor_valve)] if fittings_rule.floor_valve else []
    if fittings_rule.floor_meter:
        floor_fittings.append(Fitting(FittingKind.WATER_METER, "楼层水表"))
    unit_fittings = [Fitting(fittings_rule.unit_valve)]
    if fittings_rule.unit_meter:
        unit_fittings.append(Fitting(FittingKind.WATER_METER))
    entry_fittings = [Fitting(kind) for kind in entry.components]

    label_main = text_width(main.value, height)
    first_gap = _ceil_to(margin + _fitting_span(floor_fittings, scale) + gap + label_main + margin, scale)
    spacing = UNIT_SPACING_MM * scale
    branch_length = BRANCH_LENGTH_MM * scale
    entry_length = _ceil_to(margin + _fitting_span(entry_fittings, scale) + gap + label_main + margin, scale)
    direction = (_SQRT_HALF, slope * _SQRT_HALF)

    base = rounded((0.0, entry_y))
    start = rounded((-entry_length, entry_y))
    nodes = [
        PlanNode(node_id="entry", kind="entry", point=start),
        PlanNode(node_id="base", kind="base", point=base),
    ]
    pipes: list[PipeRunPlan] = []
    labels: list[LabelPlan] = []
    symbols: list[SymbolPlan] = []
    texts: list[TextPlan] = []

    entry_run = PipeRunPlan(
        run_id="sch.entry",
        layer=LAYER_PIPE_MAIN,
        dn=main,
        role="entry",
        node_ids=("entry", "base"),
        points=(start, base),
    )
    placed, used = place_fittings(entry_run, entry_fittings, start=start, end=base, offset_mm=END_MARGIN_MM, scale=scale)
    symbols += placed
    labels.append(
        label_in_zone(entry_run, start=start, end=base, zone_from=used + gap, zone_to=entry_length - margin, height=height)
    )
    pipes.append(entry_run)

    riser_ids: list[str] = []
    previous_id, previous = "base", base
    tee_last: Point = base
    for k in range(1, floors + 1):
        junction = rounded((0.0, header_y[k - 1]))
        junction_id = f"junction.{k}"
        nodes.append(PlanNode(node_id=junction_id, kind="junction", point=junction))
        riser = PipeRunPlan(
            run_id=f"sch.riser.{k}",
            layer=LAYER_PIPE_MAIN,
            dn=main,
            role="riser",
            node_ids=(previous_id, junction_id),
            points=(previous, junction),
        )
        segment = abs(junction[1] - previous[1])
        labels.append(
            label_in_zone(riser, start=previous, end=junction, zone_from=margin, zone_to=segment - margin, height=height)
        )
        pipes.append(riser)
        riser_ids.append(riser.run_id)

        tees = [rounded((first_gap + i * spacing, header_y[k - 1])) for i in range(units)]
        tee_ids = [f"tee.{k}.{i + 1}" for i in range(units)]
        for tee_id, tee in zip(tee_ids, tees, strict=True):
            nodes.append(PlanNode(node_id=tee_id, kind="tee", point=tee))
        header = PipeRunPlan(
            run_id=f"sch.header.{k}",
            layer=LAYER_PIPE_MAIN,
            dn=main,
            role="header",
            node_ids=(junction_id, *tee_ids),
            points=(junction, *tees),
        )
        placed, used = place_fittings(
            header, floor_fittings, start=junction, end=tees[0], offset_mm=END_MARGIN_MM, scale=scale
        )
        symbols += placed
        labels.append(
            label_in_zone(
                header,
                start=junction,
                end=tees[0],
                zone_from=used + (gap if floor_fittings else 0.0),
                zone_to=first_gap - margin,
                height=height,
            )
        )
        pipes.append(header)

        for i, (tee_id, tee) in enumerate(zip(tee_ids, tees, strict=True), start=1):
            end = rounded((tee[0] + direction[0] * branch_length, tee[1] + direction[1] * branch_length))
            end_id = f"end.{k}.{i}"
            nodes.append(PlanNode(node_id=end_id, kind="end", point=end))
            run = PipeRunPlan(
                run_id=f"sch.branch.{k}.{i}",
                layer=LAYER_PIPE_BRANCH,
                dn=branch,
                role="branch",
                node_ids=(tee_id, end_id),
                points=(tee, end),
            )
            placed, used = place_fittings(run, unit_fittings, start=tee, end=end, offset_mm=END_MARGIN_MM, scale=scale)
            symbols += placed
            labels.append(
                label_in_zone(run, start=tee, end=end, zone_from=used + gap, zone_to=branch_length - margin, height=height)
            )
            pipes.append(run)
            if k == floors:
                texts.append(
                    TextPlan(
                        text=f"{typed.unit_label} {i}",
                        point=rounded((end[0] + 1.5 * scale, end[1])),
                        height=height,
                        align="MIDDLE_LEFT",
                        layer=LAYER_TEXT,
                        role="unit_label",
                    )
                )
            tee_last = tee
        previous_id, previous = junction_id, junction

    # 楼层线与标高（只画在立管右侧；标高 = (层号 − 1) × 层高）。
    right = tee_last[0] + direction[0] * branch_length + 12.0 * scale
    lines: list[LinePlan] = []
    for k in range(1, floors + 2):
        elevation = (k - 1) * floor_height
        y = rounded((0.0, elevation))[1]
        lines.append(
            LinePlan(
                layer=LAYER_DIM,
                points=(rounded((FLOOR_LINE_START_MM * scale, y)), rounded((right, y))),
                role="floor_line",
            )
        )
        mark = rounded((right - 18.0 * scale, y))
        symbols.append(
            SymbolPlan(symbol=SymbolKind.ELEVATION_MARK, layer=LAYER_DIM, point=mark, rotation=0.0, scale=float(scale), name="标高")
        )
        texts.append(
            TextPlan(
                text=elevation_text(elevation),
                point=rounded((mark[0] + 3.0 * scale, y + 3.0 * scale)),
                height=height,
                align="BOTTOM_LEFT",
                layer=LAYER_DIM,
                role="elevation",
            )
        )
        texts.append(
            TextPlan(
                text=f"{k}F" if k <= floors else "屋面",
                point=rounded((right + 3.0 * scale, y)),
                height=height,
                align="MIDDLE_LEFT",
                layer=LAYER_TEXT,
                role="floor_name",
            )
        )

    top = header_y[-1]
    tag_center = rounded((0.0, top + 10.0 * scale))
    symbols.append(
        SymbolPlan(symbol=SymbolKind.RISER_TAG, layer=LAYER_TEXT, point=tag_center, rotation=0.0, scale=float(scale), name=RISER_TAG_TEXT)
    )
    texts.append(TextPlan(text=RISER_TAG_TEXT, point=tag_center, height=height, align="MIDDLE_CENTER", layer=LAYER_TEXT, role="riser_tag"))
    texts.append(
        TextPlan(
            text=f"横干管 {mount_label(request, typed)}（各层同）",
            point=rounded((-6.0 * scale, top)),
            height=height,
            align="MIDDLE_RIGHT",
            layer=LAYER_TEXT,
            role="elevation",
        )
    )
    texts.append(
        TextPlan(
            text=entry.entry_text,
            point=rounded((start[0] - 3.0 * scale, entry_y)),
            height=height,
            align="MIDDLE_RIGHT",
            layer=LAYER_TEXT,
            role="entry_note",
        )
    )
    check = check_velocities(request, rules)
    for index, record in enumerate(check.calculations, start=1):
        texts.append(
            TextPlan(
                text=drawing_velocity_note(record),
                point=rounded((start[0], entry_y - (6.0 + 6.0 * index) * scale)),
                height=height,
                align="BOTTOM_LEFT",
                layer=LAYER_TEXT,
                role="velocity_note",
            )
        )

    legend_symbols, legend_texts = legend(
        [s.symbol for s in symbols], left=right + 30.0 * scale, top=top, scale=scale
    )
    symbols += legend_symbols
    texts += legend_texts

    left = start[0] - 60.0 * scale
    title_y = entry_y - 40.0 * scale
    title = f"给水系统轴测图 1:{scale}（水平方向为示意）"
    texts.append(
        TextPlan(
            text=title,
            point=rounded(((left + right) / 2, title_y)),
            height=TITLE_HEIGHT_MM * scale,
            align="BOTTOM_CENTER",
            layer=LAYER_TEXT,
            role="title",
        )
    )
    rule_ids = (
        routing.rule_id,
        fittings_rule.rule_id,
        entry.rule_id,
        *entry.related_rules,
        rules.velocity.rule_id,
    )
    extra = [
        (
            "系统图按 45° 斜等测示意；楼层标高 = (层号 - 1) × 层高（输入），±0.000 为首层建筑地面；"
            f"引入管位置为示意，未标注标高；立管全高按主管 {main.value}，未做逐层变径"
        ),
        f"横干管安装高度 {mount_label(request, typed)}（h 为本层建筑地面标高，数值见 {routing.rule_id}）；各层布置相同",
    ]
    notes, _ = note_blocks(
        compose_notes(request=request, rules=rules, rule_ids=rule_ids, extra=extra),
        left=left,
        top=title_y - 10.0 * scale,
        height=height,
    )
    return SchematicPlan(
        title=title,
        scale=scale,
        nodes=tuple(nodes),
        pipes=tuple(pipes),
        labels=tuple(labels),
        symbols=tuple(symbols),
        texts=tuple(texts),
        lines=tuple(lines),
        notes=tuple(notes),
        rule_ids=rule_ids,
        branch_groups=floors,
        riser_run_ids=tuple(riser_ids),
    )


__all__ = [
    "BRANCH_LENGTH_MM",
    "ENTRY_DEPTH_MM",
    "SCHEMATIC_SCALE",
    "UNIT_SPACING_MM",
    "elevation_text",
    "plan_schematic",
]
