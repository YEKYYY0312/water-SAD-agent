"""标准层给水平面图的几何规划（P6-S1 Step 3）+ 三张图共用的规划工具。

**纯函数，不碰 ezdxf**：输出只有坐标、管段、附件位、标注位这类数据
（`generation_models` 里的 `SheetPlan` 家族），渲染层 `drawing/generator.py` 只做翻译。
先出数据再渲染，是为了让几何可以脱离 ezdxf 做单元测试与属性测试
（管段端点都落在已知节点上、标注都在自己管线 20 mm 以内、层数 = 系统图支管组数）。

确定性：不用随机数、不读时钟；坐标统一 ``rounded()`` 到 0.001 mm；遍历顺序只来自
规则表与请求里的整数，同一请求两次规划逐字节相同（有测试断言）。

与审查器的兼容纪律（执行 prompt §3，违反即 `validate_sheet` 报错）：
- 管段只画在 ``WS-PIPE-MAIN`` / ``WS-PIPE-BRANCH``；
- 每条管段一条显式 ``DN50`` 式标注，基线距管线 ``LABEL_OFFSET`` = 12 mm
  （< `ANNOTATION_MATCH_RADIUS` 20），且是半径内离它最近的带管径文字；
- 说明、图例、编号等其他文字都放在离任何管段 20 mm 以外。
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence

from water_agent.drawing.building_rules import (
    BuildingRules,
    BuildingTypeRules,
    FittingKind,
    RiserPosition,
)
from water_agent.drawing.generation_models import (
    FITTING_SYMBOL,
    LAYER_AXIS,
    LAYER_METER,
    LAYER_PIPE_BRANCH,
    LAYER_PIPE_MAIN,
    LAYER_TEXT,
    LAYER_VALVE,
    CalculationRecord,
    CirclePlan,
    FloorPlan,
    GenerationRequest,
    LabelPlan,
    LinePlan,
    NoteBlockPlan,
    PipeRunPlan,
    PlanNode,
    Point,
    SymbolKind,
    SymbolPlan,
    TextPlan,
    check_velocities,
    drawing_velocity_note,
    resolve_riser_position,
    rounded,
)

# ---------------------------------------------------------------------------
# 图面尺度（图纸毫米；乘以出图比例分母得到模型空间毫米）
# ---------------------------------------------------------------------------

TEXT_HEIGHT_MM = 3.5
TITLE_HEIGHT_MM = 5.0
#: 长仿宋体的宽高比（文字样式 WS-FS 的宽度因子）。
WIDTH_FACTOR = 0.7
#: 标注基线距管线（**模型空间**毫米，与比例无关）：审查器按 20 图纸单位关联标注。
LABEL_OFFSET = 12.0

#: 各图块沿管线方向的长度（图纸毫米），与 `drawing/blocks.py` 的块定义一致（有测试核对）。
SYMBOL_LENGTH_MM: dict[SymbolKind, float] = {
    SymbolKind.GATE_VALVE: 4.0,
    SymbolKind.GLOBE_VALVE: 4.0,
    SymbolKind.CHECK_VALVE: 4.0,
    SymbolKind.WATER_METER: 2.8,
    SymbolKind.PUMP: 5.0,
}
#: 附件、标注与管段端点之间的留空（图纸毫米）。
END_MARGIN_MM = 2.0
FITTING_GAP_MM = 2.5
LABEL_GAP_MM = 3.0
RISER_SECTION_RADIUS_MM = 1.0
RISER_TAG_RADIUS_MM = 4.0
AXIS_BUBBLE_RADIUS_MM = 4.0
AXIS_OVERHANG_MM = 15.0

PLAN_SCALE = 100
RISER_TAG_TEXT = "JL-1"
#: 执行 prompt Step 4 规定的固定说明，每张图左下角原样写出。
DISCLAIMER = "本图由程序按参数生成，是初稿，需设计人复核；管道敷设未考虑结构梁高与其他专业管线。"
#: 轴号字母：GB/T 50001 不用 I、O、Z（易与数字混淆）。
AXIS_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXY"

_FITTING_LAYER: dict[SymbolKind, str] = {
    SymbolKind.GATE_VALVE: LAYER_VALVE,
    SymbolKind.GLOBE_VALVE: LAYER_VALVE,
    SymbolKind.CHECK_VALVE: LAYER_VALVE,
    SymbolKind.PUMP: LAYER_VALVE,
    SymbolKind.WATER_METER: LAYER_METER,
}


class PlanningError(RuntimeError):
    """几何规划的内部失败（规则表参数放不下标注等）。对外一律 ``GEN_RENDER_FAILED``。"""


# ---------------------------------------------------------------------------
# 几何工具
# ---------------------------------------------------------------------------


def text_width(text: str, height: float) -> float:
    """按字宽估算文字长度：中文 1.0 em、西文 0.6 em（偏保守），乘宽度因子。"""

    ems = sum(1.0 if ord(char) > 0x2E7F else 0.6 for char in text)
    return ems * height * WIDTH_FACTOR


def _length(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _along(a: Point, b: Point, distance: float) -> Point:
    total = _length(a, b)
    ratio = distance / total
    return (a[0] + (b[0] - a[0]) * ratio, a[1] + (b[1] - a[1]) * ratio)


def _angle(a: Point, b: Point) -> float:
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))


def reading_angle(a: Point, b: Point) -> float:
    """文字方向：沿管线、且从左往右（或从下往上）读，取 (-90°, 90°]。"""

    angle = _angle(a, b)
    if angle > 90.0 + 1e-9:
        angle -= 180.0
    elif angle <= -90.0 + 1e-9:
        angle += 180.0
    return round(angle, 6) + 0.0


def _up(angle_deg: float) -> Point:
    rad = math.radians(angle_deg)
    return (-math.sin(rad), math.cos(rad))


class Fitting:
    """一个待放置的附件：种类 + 图面名称。"""

    __slots__ = ("kind", "name")

    def __init__(self, kind: FittingKind, name: str | None = None) -> None:
        self.kind = kind
        self.name = name or kind.value


def place_fittings(
    run: PipeRunPlan,
    fittings: Sequence[Fitting],
    *,
    start: Point,
    end: Point,
    offset_mm: float,
    scale: int,
) -> tuple[list[SymbolPlan], float]:
    """沿 ``start → end`` 从 ``offset_mm``（图纸毫米）处起依次放附件，返回 (符号, 占用到的距离)。"""

    symbols: list[SymbolPlan] = []
    cursor = offset_mm * scale
    rotation = round(_angle(start, end), 6) + 0.0
    for fitting in fittings:
        symbol = FITTING_SYMBOL[fitting.kind]
        length = SYMBOL_LENGTH_MM[symbol] * scale
        center = _along(start, end, cursor + length / 2)
        symbols.append(
            SymbolPlan(
                symbol=symbol,
                layer=_FITTING_LAYER[symbol],
                point=rounded(center),
                rotation=rotation,
                scale=float(scale),
                run_id=run.run_id,
                name=fitting.name,
            )
        )
        cursor += length + FITTING_GAP_MM * scale
    occupied = cursor - FITTING_GAP_MM * scale if fittings else offset_mm * scale
    return symbols, occupied


def label_in_zone(
    run: PipeRunPlan,
    *,
    start: Point,
    end: Point,
    zone_from: float,
    zone_to: float,
    height: float,
) -> LabelPlan:
    """在 ``start → end`` 的 ``[zone_from, zone_to]``（模型毫米）区间中点放管径标注。"""

    text = run.dn.value
    width = text_width(text, height)
    if zone_to - zone_from < width:
        raise PlanningError(
            f"{run.run_id}：标注区间 {zone_to - zone_from:.0f} mm 放不下 {text}（需 {width:.0f} mm）"
        )
    center = _along(start, end, (zone_from + zone_to) / 2)
    angle = reading_angle(start, end)
    up = _up(angle)
    anchor = (center[0] + up[0] * LABEL_OFFSET, center[1] + up[1] * LABEL_OFFSET)
    return LabelPlan(run_id=run.run_id, text=text, anchor=rounded(anchor), rotation=angle, height=height)


# ---------------------------------------------------------------------------
# 说明文字（三张图共用）
# ---------------------------------------------------------------------------


def parameter_lines(
    request: GenerationRequest, rules: BuildingRules, *, riser_position: str, defaulted: bool
) -> list[str]:
    typed = rules.for_type(request.building_type)
    riser = f"{riser_position}（未指定，按规则表默认）" if defaulted else riser_position
    branch_flow = (
        f"；支管设计流量 {request.branch_flow_rate_lps:.2f} L/s"
        if request.branch_flow_rate_lps is not None
        else ""
    )
    return [
        (
            f"设计参数（用户输入）：{request.building_type.value}，{request.floors} 层，"
            f"每层 {unit_count(request.units_per_floor, typed.unit_label)}，层高 {request.floor_height_m:.2f} m；"
            f"立管位置：{riser}"
        ),
        (
            f"主管（含立管）{request.main_pipe.dn.value} {request.main_pipe.material.value}，"
            f"支管 {request.branch_pipe.dn.value} {request.branch_pipe.material.value}；"
            f"设计流量 {request.flow_rate_lps:.2f} L/s{branch_flow}；供水压力 {request.pressure_mpa:.2f} MPa"
        ),
        "图面管线旁只注管径，管材见本条；供水分区、减压措施、横管坡度与泄水未表达，需设计人确定",
    ]


def unit_count(count: int, unit_label: str) -> str:
    """"2 户" / "2 个用水单元"：单字量词直接跟数字，其余加"个"。"""

    return f"{count} {unit_label}" if len(unit_label) == 1 else f"{count} 个{unit_label}"


def calculation_lines(calculations: Iterable[CalculationRecord]) -> list[str]:
    parts = [drawing_velocity_note(record) for record in calculations]
    return [
        "流速校核（tools/hydraulics.py::calc_velocity，按公称直径估算内径）：" + "；".join(parts)
    ]


def rule_lines(rules: BuildingRules, rule_ids: Sequence[str]) -> list[str]:
    return [rules.rule(rule_id).rendered_note() for rule_id in rule_ids]


def compose_notes(
    *,
    request: GenerationRequest,
    rules: BuildingRules,
    rule_ids: Sequence[str],
    extra: Sequence[str] = (),
) -> list[str]:
    riser_position, defaulted = resolve_riser_position(request, rules)
    check = check_velocities(request, rules)
    body = [
        *parameter_lines(request, rules, riser_position=riser_position, defaulted=defaulted),
        *calculation_lines(check.calculations),
        *extra,
        *rule_lines(rules, rule_ids),
        f"规则表版本 {rules.rules_version}；未标注长度尺寸",
    ]
    return ["说明："] + [f"{index}. {line}" for index, line in enumerate(body, start=1)]


#: 说明每行的最大宽度（中文字数计，西文按 0.55 个）。也让每行在 GBK 下不超过 255 字节：
#: R2000 的字符串字段有这个上限，实验性 DWG 的源文件用得到（见 `generator`）。
NOTE_LINE_UNITS = 60.0
_NOTE_TOKEN = re.compile(r"[A-Za-z0-9_.\-/:~+%±]+|.")


def _units(token: str) -> float:
    return sum(1.0 if ord(char) > 0x2E7F else 0.55 for char in token)


def wrap_note_line(line: str, *, max_units: float = NOTE_LINE_UNITS, indent: str = "   ") -> list[str]:
    """按宽度折行：西文词、规范号、数值不从中间断开；续行缩进。"""

    wrapped: list[str] = []
    current, width = "", 0.0
    for token in _NOTE_TOKEN.findall(line):
        size = _units(token)
        if current and width + size > max_units:
            wrapped.append(current.rstrip())
            current, width = indent, _units(indent)
            if token == " ":
                continue
        current += token
        width += size
    if current.strip():
        wrapped.append(current.rstrip())
    return wrapped or [line]


def note_blocks(
    lines: Sequence[str], *, left: float, top: float, height: float
) -> tuple[list[NoteBlockPlan], float]:
    """说明 + 固定声明两块 MTEXT，返回 (块, 最低处 y)。长行按 `NOTE_LINE_UNITS` 折行。"""

    spacing = height * 5.0 / 3.0
    lines = [part for line in lines for part in wrap_note_line(line)]
    notes = NoteBlockPlan(
        lines=tuple(lines), point=rounded((left, top)), height=height, layer=LAYER_TEXT, role="notes"
    )
    disclaimer_top = top - spacing * len(lines) - height * 2
    disclaimer = NoteBlockPlan(
        lines=tuple(wrap_note_line(DISCLAIMER)),
        point=rounded((left, disclaimer_top)),
        height=height,
        layer=LAYER_TEXT,
        role="disclaimer",
    )
    return [notes, disclaimer], disclaimer_top - spacing


def legend(
    kinds: Sequence[SymbolKind], *, left: float, top: float, scale: int, symbol_scale: float | None = None
) -> tuple[list[SymbolPlan], list[TextPlan]]:
    """图例：本图用到的符号，固定顺序。``symbol_scale`` 与图面符号的缩放一致。"""

    names = {
        SymbolKind.GATE_VALVE: "闸阀",
        SymbolKind.GLOBE_VALVE: "截止阀",
        SymbolKind.CHECK_VALVE: "止回阀",
        SymbolKind.WATER_METER: "水表",
        SymbolKind.PUMP: "水泵",
        SymbolKind.RISER_TAG: "给水立管编号",
        SymbolKind.ELEVATION_MARK: "标高",
    }
    order = list(names)
    used = [kind for kind in order if kind in set(kinds)]
    height = TEXT_HEIGHT_MM * scale
    row = 10.0 * scale
    symbols: list[SymbolPlan] = []
    texts = [
        TextPlan(
            text="图例", point=rounded((left, top)), height=height, align="BOTTOM_LEFT", layer=LAYER_TEXT, role="legend"
        )
    ]
    for index, kind in enumerate(used, start=1):
        y = top - index * row
        layer = _FITTING_LAYER.get(kind, LAYER_TEXT)
        symbols.append(
            SymbolPlan(
                symbol=kind,
                layer=layer,
                point=rounded((left + 4.0 * scale, y)),
                rotation=0.0,
                scale=float(symbol_scale or scale),
                name=names[kind],
            )
        )
        texts.append(
            TextPlan(
                text=names[kind],
                point=rounded((left + 12.0 * scale, y)),
                height=height,
                align="MIDDLE_LEFT",
                layer=LAYER_TEXT,
                role="legend",
            )
        )
    return symbols, texts


def mount_label(request: GenerationRequest, typed: BuildingTypeRules) -> str:
    """横干管安装高度的写法（h = 本层建筑地面标高）。数值只来自规则表与层高输入。"""

    routing = typed.routing
    if routing.mount == "floor":
        height = routing.mount_offset_m
    else:
        height = request.floor_height_m - routing.mount_offset_m
    return f"h+{height:.2f}"


# ---------------------------------------------------------------------------
# 标准层平面图
# ---------------------------------------------------------------------------


def _riser_point(position: RiserPosition, *, core_x0: float, core_x1: float, route_y: float) -> Point:
    if position.x_ref == "core":
        x = core_x0 + position.x_value * (core_x1 - core_x0)
    else:
        x = position.x_value
    return (x, route_y + position.dy_from_route_mm)


def plan_floor(request: GenerationRequest, rules: BuildingRules) -> FloorPlan:
    """标准层平面：轴网 + 立管 + 连接管 + 楼层横干管 + 各单元支管（阀门、水表）+ 标注。"""

    typed = rules.for_type(request.building_type)
    grid, routing, fittings_rule = typed.grid, typed.routing, typed.fittings
    riser_name, _ = resolve_riser_position(request, rules)
    scale = PLAN_SCALE
    height = TEXT_HEIGHT_MM * scale
    main, branch = request.main_pipe.dn, request.branch_pipe.dn

    units = request.units_per_floor
    left_units = math.ceil(units / 2) if grid.core_bays > 0 else 0
    right_units = units - left_units
    bay_x, bay_y, per_unit = grid.bay_x_mm, grid.bay_y_mm, grid.bays_per_unit
    nx = units * per_unit + grid.core_bays
    core_x0 = left_units * per_unit * bay_x
    core_x1 = core_x0 + grid.core_bays * bay_x
    route_y = routing.route_axis * bay_y + routing.route_offset_mm

    unit_x: list[float] = [(k * per_unit + per_unit / 2) * bay_x for k in range(left_units)]
    unit_x += [
        (left_units * per_unit + grid.core_bays + k * per_unit + per_unit / 2) * bay_x
        for k in range(right_units)
    ]

    riser = rounded(_riser_point(typed.riser.positions[riser_name], core_x0=core_x0, core_x1=core_x1, route_y=route_y))
    junction = rounded((riser[0], route_y))
    nodes: list[PlanNode] = [
        PlanNode(node_id="riser", kind="riser", point=riser),
        PlanNode(node_id="junction", kind="junction", point=junction),
    ]
    tees = [rounded((x, route_y)) for x in unit_x]
    ends = [rounded((x, route_y + routing.branch_direction * routing.branch_length_mm)) for x in unit_x]
    for index, (tee, end) in enumerate(zip(tees, ends, strict=True), start=1):
        nodes.append(PlanNode(node_id=f"tee.{index}", kind="tee", point=tee))
        nodes.append(PlanNode(node_id=f"end.{index}", kind="end", point=end))

    pipes: list[PipeRunPlan] = []
    labels: list[LabelPlan] = []
    symbols: list[SymbolPlan] = []

    # 连接管：立管 → 楼层横干管，楼层附件（阀门 / 楼层水表）装在这段上。
    connector = PipeRunPlan(
        run_id="plan.connector",
        layer=LAYER_PIPE_MAIN,
        dn=main,
        role="connector",
        node_ids=("riser", "junction"),
        points=(riser, junction),
    )
    floor_fittings = [Fitting(fittings_rule.floor_valve)] if fittings_rule.floor_valve else []
    if fittings_rule.floor_meter:
        floor_fittings.append(Fitting(FittingKind.WATER_METER, "楼层水表"))
    placed, used = place_fittings(
        connector,
        floor_fittings,
        start=riser,
        end=junction,
        offset_mm=RISER_SECTION_RADIUS_MM + END_MARGIN_MM,
        scale=scale,
    )
    symbols += placed
    connector_length = _length(riser, junction)
    zone_from = used + (LABEL_GAP_MM if floor_fittings else 0.0) * scale
    labels.append(
        label_in_zone(
            connector,
            start=riser,
            end=junction,
            zone_from=zone_from,
            zone_to=connector_length - END_MARGIN_MM * scale,
            height=height,
        )
    )
    pipes.append(connector)

    # 楼层横干管：从交汇点分别向左、向右，依次经过各单元的三通。
    header_sides: list[tuple[str, list[int]]] = []
    if left_units:
        header_sides.append(("left", list(range(left_units - 1, -1, -1))))
    if right_units:
        header_sides.append(("right", list(range(left_units, units))))
    last_header_end: tuple[str, Point] | None = None
    for side, order in header_sides:
        vertices = [junction] + [tees[i] for i in order]
        run = PipeRunPlan(
            run_id=f"plan.header.{side}",
            layer=LAYER_PIPE_MAIN,
            dn=main,
            role="header",
            node_ids=("junction", *[f"tee.{i + 1}" for i in order]),
            points=tuple(vertices),
        )
        first = _length(vertices[0], vertices[1])
        labels.append(
            label_in_zone(
                run,
                start=vertices[0],
                end=vertices[1],
                zone_from=END_MARGIN_MM * scale,
                zone_to=first - END_MARGIN_MM * scale,
                height=height,
            )
        )
        pipes.append(run)
        last_header_end = (side, vertices[-1])

    # 各单元支管：三通 → 入户点，先附件（阀门、水表）后标注。
    unit_fittings = [Fitting(fittings_rule.unit_valve)]
    if fittings_rule.unit_meter:
        unit_fittings.append(Fitting(FittingKind.WATER_METER))
    texts: list[TextPlan] = []
    for index, (tee, end) in enumerate(zip(tees, ends, strict=True), start=1):
        run = PipeRunPlan(
            run_id=f"plan.branch.{index}",
            layer=LAYER_PIPE_BRANCH,
            dn=branch,
            role="branch",
            node_ids=(f"tee.{index}", f"end.{index}"),
            points=(tee, end),
        )
        placed, used = place_fittings(
            run, unit_fittings, start=tee, end=end, offset_mm=END_MARGIN_MM, scale=scale
        )
        symbols += placed
        labels.append(
            label_in_zone(
                run,
                start=tee,
                end=end,
                zone_from=used + LABEL_GAP_MM * scale,
                zone_to=routing.branch_length_mm - END_MARGIN_MM * scale,
                height=height,
            )
        )
        pipes.append(run)
        beyond = routing.branch_direction * 1.5 * scale
        texts.append(
            TextPlan(
                text=typed.branch_end_text,
                point=rounded((end[0], end[1] + beyond)),
                height=height,
                align="TOP_CENTER" if routing.branch_direction < 0 else "BOTTOM_CENTER",
                layer=LAYER_TEXT,
                role="branch_end",
            )
        )
        texts.append(
            TextPlan(
                text=f"{typed.unit_label} {index}",
                point=rounded((end[0], end[1] + routing.branch_direction * 11.0 * scale)),
                height=height,
                align="MIDDLE_CENTER",
                layer=LAYER_TEXT,
                role="unit_label",
            )
        )

    # 立管断面圆 + 编号圈（放在连接管的另一侧，不压管线）。
    away = 1.0 if riser[1] > route_y else -1.0
    tag_center = rounded((riser[0] + 7.0 * scale, riser[1] + away * 7.0 * scale))
    circles = [
        CirclePlan(layer=LAYER_PIPE_MAIN, center=riser, radius=RISER_SECTION_RADIUS_MM * scale, role="riser_section")
    ]
    symbols.append(
        SymbolPlan(
            symbol=SymbolKind.RISER_TAG,
            layer=LAYER_TEXT,
            point=tag_center,
            rotation=0.0,
            scale=float(scale),
            name=RISER_TAG_TEXT,
        )
    )
    texts.append(
        TextPlan(
            text=RISER_TAG_TEXT, point=tag_center, height=height, align="MIDDLE_CENTER", layer=LAYER_TEXT, role="riser_tag"
        )
    )
    if last_header_end is not None:
        side, point = last_header_end
        gap = 3.0 * scale
        texts.append(
            TextPlan(
                text=f"横干管 {mount_label(request, typed)}",
                point=rounded((point[0] + gap, point[1]) if side == "right" else (point[0] - gap, point[1])),
                height=height,
                align="MIDDLE_LEFT" if side == "right" else "MIDDLE_RIGHT",
                layer=LAYER_TEXT,
                role="elevation",
            )
        )

    # 轴网：覆盖建筑与全部管线，轴号圈在下方与左侧。
    content_y = [0.0, grid.depth_bays * bay_y] + [p[1] for p in (riser, *ends, tag_center)]
    x_lo, x_hi = -AXIS_OVERHANG_MM * scale, nx * bay_x + AXIS_OVERHANG_MM * scale
    y_lo = min(content_y) - AXIS_OVERHANG_MM * scale - 12.0 * scale
    y_hi = max(content_y) + AXIS_OVERHANG_MM * scale
    lines: list[LinePlan] = []
    bubble = AXIS_BUBBLE_RADIUS_MM * scale
    for i in range(nx + 1):
        x = float(i * bay_x)
        lines.append(LinePlan(layer=LAYER_AXIS, points=(rounded((x, y_lo)), rounded((x, y_hi))), linetype="CENTER", role="axis"))
        center = rounded((x, y_lo - bubble))
        circles.append(CirclePlan(layer=LAYER_AXIS, center=center, radius=bubble, role="axis_bubble"))
        texts.append(TextPlan(text=str(i + 1), point=center, height=height, align="MIDDLE_CENTER", layer=LAYER_AXIS, role="axis_label"))
    for j in range(grid.depth_bays + 1):
        y = float(j * bay_y)
        lines.append(LinePlan(layer=LAYER_AXIS, points=(rounded((x_lo, y)), rounded((x_hi, y))), linetype="CENTER", role="axis"))
        center = rounded((x_lo - bubble, y))
        circles.append(CirclePlan(layer=LAYER_AXIS, center=center, radius=bubble, role="axis_bubble"))
        texts.append(
            TextPlan(text=AXIS_LETTERS[j], point=center, height=height, align="MIDDLE_CENTER", layer=LAYER_AXIS, role="axis_label")
        )

    legend_symbols, legend_texts = legend(
        [s.symbol for s in symbols], left=x_hi + 20.0 * scale, top=y_hi, scale=scale
    )
    symbols += legend_symbols
    texts += legend_texts

    title_y = y_lo - 2 * bubble - 12.0 * scale
    title = f"给水平面图（标准层） 1:{scale}"
    texts.append(
        TextPlan(
            text=title,
            point=rounded(((x_lo + x_hi) / 2, title_y)),
            height=TITLE_HEIGHT_MM * scale,
            align="BOTTOM_CENTER",
            layer=LAYER_TEXT,
            role="title",
        )
    )
    rule_ids = (
        typed.grid.rule_id,
        typed.riser.rule_id,
        typed.routing.rule_id,
        typed.fittings.rule_id,
        rules.velocity.rule_id,
    )
    extra = [
        (
            f"横干管安装高度 {mount_label(request, typed)}（h 为本层建筑地面标高，数值见 {typed.routing.rule_id}）；"
            f"{typed.unit_label}编号仅为示意"
        ),
    ]
    notes, _ = note_blocks(
        compose_notes(request=request, rules=rules, rule_ids=rule_ids, extra=extra),
        left=x_lo,
        top=title_y - 10.0 * scale,
        height=height,
    )
    return FloorPlan(
        title=title,
        scale=scale,
        nodes=tuple(nodes),
        pipes=tuple(pipes),
        labels=tuple(labels),
        symbols=tuple(symbols),
        texts=tuple(texts),
        lines=tuple(lines),
        circles=tuple(circles),
        notes=tuple(notes),
        rule_ids=rule_ids,
        riser_count=1,
        units=units,
    )


__all__ = [
    "AXIS_LETTERS",
    "DISCLAIMER",
    "END_MARGIN_MM",
    "FITTING_GAP_MM",
    "LABEL_GAP_MM",
    "LABEL_OFFSET",
    "NOTE_LINE_UNITS",
    "PLAN_SCALE",
    "RISER_TAG_TEXT",
    "SYMBOL_LENGTH_MM",
    "TEXT_HEIGHT_MM",
    "TITLE_HEIGHT_MM",
    "WIDTH_FACTOR",
    "Fitting",
    "PlanningError",
    "calculation_lines",
    "compose_notes",
    "label_in_zone",
    "legend",
    "mount_label",
    "note_blocks",
    "parameter_lines",
    "place_fittings",
    "plan_floor",
    "reading_angle",
    "rule_lines",
    "text_width",
    "unit_count",
    "wrap_note_line",
]
