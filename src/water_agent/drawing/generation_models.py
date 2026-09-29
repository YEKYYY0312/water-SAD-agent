"""参数化出图的输入契约、错误码与流速校核（P6-S1 Step 2）。

决策记录 `docs/design/p6-step-1-decision.md`。

输入是**结构化参数**，不是自然语言：`GenerationRequest` 为 pydantic
``extra="forbid"`` + ``frozen``，枚举字段只收白名单值。三个稳定错误码：

- ``GEN_INVALID_INPUT``（422）：参数不合法。只回显字段路径与原因，**不回显提交的值**
  （与 `webapp/app.py` 的 422 处理同一纪律）；
- ``GEN_UNSUPPORTED_SYSTEM``（422）：本步只做给水；
- ``GEN_RENDER_FAILED``（500）：规划或渲染的内部失败，**不回显内部异常文本**。

流速校核（铁律①）：主管、支管分别调用 `tools/hydraulics.py::calc_velocity`，数值只来自
它的返回值。缺支管设计流量时，`calc_velocity` 自己返回 ``missing_inputs``，本模块原样
记录，**不按总流量均分去估一个支管流量**。流速超出规则表的建议区间不阻断生成，只写
warnings 并在图面提示复核。
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator

from water_agent.drawing.building_rules import BuildingRules, BuildingType, FittingKind
from water_agent.tools.hydraulics import calc_velocity

# ---------------------------------------------------------------------------
# 稳定错误码
# ---------------------------------------------------------------------------

GEN_INVALID_INPUT = "GEN_INVALID_INPUT"
GEN_UNSUPPORTED_SYSTEM = "GEN_UNSUPPORTED_SYSTEM"
GEN_RENDER_FAILED = "GEN_RENDER_FAILED"

GENERATION_ERROR_STATUS: dict[str, int] = {
    GEN_INVALID_INPUT: 422,
    GEN_UNSUPPORTED_SYSTEM: 422,
    GEN_RENDER_FAILED: 500,
}

#: 500 时对外的唯一说法。内部异常只进服务端日志，不进响应体。
RENDER_FAILED_MESSAGE = "图纸生成失败（内部错误），请稍后重试；如持续出现请联系管理员"

SUPPORTED_SYSTEM: Literal["给水"] = "给水"


class GenerationError(Exception):
    """生成链路的结构化失败。``fields`` 只含字段路径与原因，不含用户提交的值。"""

    def __init__(self, code: str, message: str, *, fields: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        if code not in GENERATION_ERROR_STATUS:
            raise ValueError(f"未登记的生成错误码 {code!r}")
        self.code = code
        self.message = message
        self.fields = list(fields or [])

    @property
    def status_code(self) -> int:
        return GENERATION_ERROR_STATUS[self.code]

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.fields:
            payload["fields"] = list(self.fields)
        return payload


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class NominalDiameter(str, Enum):
    """公称直径白名单（DN15–DN150）。"""

    DN15 = "DN15"
    DN20 = "DN20"
    DN25 = "DN25"
    DN32 = "DN32"
    DN40 = "DN40"
    DN50 = "DN50"
    DN65 = "DN65"
    DN80 = "DN80"
    DN100 = "DN100"
    DN125 = "DN125"
    DN150 = "DN150"

    @property
    def mm(self) -> int:
        return int(self.value[2:])


class PipeMaterial(str, Enum):
    PPR = "PPR"
    PE = "PE"
    STEEL_PLASTIC_COMPOSITE = "钢塑复合"
    STAINLESS_STEEL = "不锈钢"


class PipeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dn: NominalDiameter
    material: PipeMaterial


class GenerationRequest(BaseModel):
    """一次出图的输入（spec D1 + 执行 prompt Step 2 的取值范围）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    system_type: Literal["给水"] = SUPPORTED_SYSTEM
    building_type: BuildingType
    floors: int = Field(ge=1, le=33)
    units_per_floor: int = Field(ge=1, le=8)
    #: 缺省时取规则表默认（spec D2"用户指定或规则默认"），结果里如实写明用了默认。
    riser_position: str | None = Field(default=None, min_length=1, max_length=16)
    main_pipe: PipeSpec
    branch_pipe: PipeSpec
    flow_rate_lps: float = Field(gt=0, allow_inf_nan=False)
    #: 支管设计流量。缺省 = 支管流速不校核（进 missing_inputs），不按总流量均分估算。
    branch_flow_rate_lps: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    pressure_mpa: float = Field(ge=0.1, le=1.0, allow_inf_nan=False)
    floor_height_m: float = Field(default=3.0, ge=2.4, le=6.0, allow_inf_nan=False)
    #: 实验性 DWG 输出（LibreDWG），需显式勾选；不影响任何几何。
    include_dwg: bool = False

    # 字段级而非 model 级校验：错误的 loc 才能指到具体字段（前端据此标红那一格）。
    # 依赖的字段（main_pipe / flow_rate_lps）在类里先声明，校验时已在 info.data 里；
    # 它们自身不合法时不在 info.data 里，此时跳过交叉校验，只报它们自己的错。
    @field_validator("branch_pipe")
    @classmethod
    def _branch_not_larger_than_main(cls, value: PipeSpec, info: ValidationInfo) -> PipeSpec:
        main = info.data.get("main_pipe")
        if isinstance(main, PipeSpec) and value.dn.mm > main.dn.mm:
            raise ValueError("支管管径不得大于主管管径")
        return value

    @field_validator("branch_flow_rate_lps")
    @classmethod
    def _branch_flow_within_total(cls, value: float | None, info: ValidationInfo) -> float | None:
        total = info.data.get("flow_rate_lps")
        if value is not None and isinstance(total, float) and value > total:
            raise ValueError("支管设计流量不得大于总设计流量")
        return value


def _error_fields(exc: ValidationError) -> list[dict[str, str]]:
    """把 pydantic 错误压成 ``{loc, type, message}``。**不带 input 值**。"""

    fields: list[dict[str, str]] = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        loc = ".".join(str(part)[:64] for part in error.get("loc", ())) or "(root)"
        fields.append({"loc": loc, "type": str(error.get("type", "")), "message": str(error.get("msg", ""))})
    return fields


def parse_generation_request(payload: object) -> GenerationRequest:
    """校验输入。失败抛 `GenerationError`（``GEN_UNSUPPORTED_SYSTEM`` 优先于一般非法）。"""

    if isinstance(payload, GenerationRequest):
        return payload
    try:
        return GenerationRequest.model_validate(payload)
    except ValidationError as exc:
        fields = _error_fields(exc)
        if any(field["loc"] == "system_type" and field["type"] == "literal_error" for field in fields):
            raise GenerationError(
                GEN_UNSUPPORTED_SYSTEM,
                "本步只支持给水系统（排水、消防、热水为后续 Step）",
                fields=[field for field in fields if field["loc"] == "system_type"],
            ) from None
        raise GenerationError(GEN_INVALID_INPUT, "出图参数不合法", fields=fields) from None


def resolve_riser_position(request: GenerationRequest, rules: BuildingRules) -> tuple[str, bool]:
    """返回 ``(立管位置, 是否取的规则默认)``；位置不属于该建筑类型时报 ``GEN_INVALID_INPUT``。"""

    riser = rules.for_type(request.building_type).riser
    if request.riser_position is None:
        return riser.default_position, True
    if request.riser_position not in riser.positions:
        allowed = "、".join(riser.positions)
        raise GenerationError(
            GEN_INVALID_INPUT,
            "出图参数不合法",
            fields=[
                {
                    "loc": "riser_position",
                    "type": "not_allowed_for_building_type",
                    "message": f"{request.building_type.value}的立管位置只能是：{allowed}",
                }
            ],
        )
    return request.riser_position, False


# ---------------------------------------------------------------------------
# 流速校核
# ---------------------------------------------------------------------------

#: `calc_velocity` 的 ``diameter_mm`` 是管内径；本步取公称直径近似（没有壁厚/压力等级
#: 输入，不猜内径），这条假设随结果一起返回。
DIAMETER_BASIS_NOTE = "diameter_mm 取公称直径 DN 近似管内径（未按管材壁厚换算，结果为估算）"

VelocityAdvisory = Literal["within", "below", "above", "above_limit", "not_checked"]


class CalculationRecord(BaseModel):
    """一次程序化计算的完整记录：调用了什么、输入是什么、工具返回了什么。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Literal["main_pipe_velocity", "branch_pipe_velocity"]
    label: str
    tool: Literal["tools/hydraulics.py::calc_velocity"] = "tools/hydraulics.py::calc_velocity"
    inputs: dict[str, float | str | None]
    ok: bool
    values: dict[str, float]
    missing_inputs: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    #: `calc_velocity` 自己的越界标注（口径表 §1 的 0.6–1.5 m/s），原样保留。
    assumptions: tuple[str, ...] = ()
    diameter_basis: str = DIAMETER_BASIS_NOTE
    #: 规则表 `ws.pipe_velocity` 的判定；缺输入时为 not_checked。
    advisory: VelocityAdvisory = "not_checked"
    advisory_rule_id: str = ""

    @property
    def velocity_mps(self) -> float | None:
        value = self.values.get("velocity_mps")
        return value if value is not None and math.isfinite(value) else None


class VelocityCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    calculations: tuple[CalculationRecord, ...]
    warnings: tuple[str, ...]
    missing_inputs: tuple[str, ...]


def format_velocity(value: float) -> str:
    """图面与文字里的流速写法（两位小数）；完整精度留在 ``values`` 里。"""

    return f"{value:.2f}"


def _advisory(velocity: float, rules: BuildingRules) -> VelocityAdvisory:
    rule = rules.velocity
    if velocity > rule.limit_max_mps:
        return "above_limit"
    if velocity > rule.recommended_max_mps:
        return "above"
    if velocity < rule.recommended_min_mps:
        return "below"
    return "within"


def velocity_warning(record: CalculationRecord, rules: BuildingRules) -> str | None:
    """超出建议区间时的一句提示（warnings 用）；区间与依据只取自规则表。"""

    velocity = record.velocity_mps
    if velocity is None or record.advisory in ("within", "not_checked"):
        return None
    rule = rules.velocity
    band = f"{rule.recommended_min_mps:g}~{rule.recommended_max_mps:g} m/s"
    extra = f"，且大于上限约 {rule.limit_max_mps:g} m/s" if record.advisory == "above_limit" else ""
    return (
        f"{record.label}流速 {format_velocity(velocity)} m/s，超出建议区间（{band}{extra}），需复核"
        f"（{rule.rule_id}，{rule.source_id}，非正式条文）"
    )


def drawing_velocity_note(record: CalculationRecord) -> str:
    """图面上的流速标注。超出区间时用执行 prompt 规定的原话"超出建议区间，需复核"。"""

    velocity = record.velocity_mps
    if velocity is None:
        reason = f"缺{record.label}设计流量" if "flow_lps" in record.missing_inputs else "见计算记录"
        return f"{record.label}流速未校核（{reason}）"
    text = f"{record.label}流速 {format_velocity(velocity)} m/s"
    if record.advisory not in ("within", "not_checked"):
        text += "，超出建议区间，需复核"
    return text


def check_velocities(request: GenerationRequest, rules: BuildingRules) -> VelocityCheck:
    """主管、支管分别调用 `calc_velocity`。支管缺流量时如实返回 missing_inputs。"""

    specs: tuple[
        tuple[Literal["main_pipe_velocity", "branch_pipe_velocity"], str, float | None, PipeSpec], ...
    ] = (
        ("main_pipe_velocity", "主管", request.flow_rate_lps, request.main_pipe),
        ("branch_pipe_velocity", "支管", request.branch_flow_rate_lps, request.branch_pipe),
    )
    records: list[CalculationRecord] = []
    warnings: list[str] = []
    missing: list[str] = []
    for name, label, flow, pipe in specs:
        inputs: dict[str, Any] = {
            "flow_lps": flow,
            "diameter_mm": float(pipe.dn.mm),
            "scenario": "water_supply",
            "material": pipe.material.value,
        }
        result = calc_velocity(inputs)
        velocity = result.values.get("velocity_mps") if result.ok else None
        record = CalculationRecord(
            name=name,
            label=label,
            inputs=inputs,
            ok=result.ok,
            values=dict(result.values),
            missing_inputs=tuple(result.missing_inputs),
            errors=tuple(result.errors),
            assumptions=tuple(result.assumptions),
            advisory=_advisory(velocity, rules) if velocity is not None else "not_checked",
            advisory_rule_id=rules.velocity.rule_id,
        )
        records.append(record)
        warning = velocity_warning(record, rules)
        if warning:
            warnings.append(warning)
        if not result.ok and result.missing_inputs:
            source = "branch_flow_rate_lps" if name == "branch_pipe_velocity" else "flow_rate_lps"
            missing.append(
                f"{source}：{label}设计流量未提供，{label}流速未校核（不按总流量均分估算）"
            )
            warnings.append(f"{label}流速未校核：缺{label}设计流量（{source}）")
    return VelocityCheck(
        calculations=tuple(records), warnings=tuple(warnings), missing_inputs=tuple(missing)
    )



# ---------------------------------------------------------------------------
# 几何规划的数据模型（Step 3）：规划层只产出这些数据，渲染层只做翻译
# ---------------------------------------------------------------------------

Point = tuple[float, float]

#: 规划坐标统一保留到 0.001 mm，同一请求两次规划逐字节相同。
COORD_DECIMALS = 3

LAYER_PIPE_MAIN: Literal["WS-PIPE-MAIN"] = "WS-PIPE-MAIN"
LAYER_PIPE_BRANCH: Literal["WS-PIPE-BRANCH"] = "WS-PIPE-BRANCH"
LAYER_VALVE = "WS-VALVE"
LAYER_METER = "WS-METER"
LAYER_TEXT = "WS-TEXT"
LAYER_DIM = "WS-DIM"
LAYER_AXIS = "WS-AXIS"
#: 大样里的井壁、楼板、吊顶、吊架（执行 prompt 的七个图层里没有能放构筑物轮廓的）。
LAYER_OUTLINE = "WS-OUTLINE"

PIPE_LAYERS: tuple[str, ...] = (LAYER_PIPE_MAIN, LAYER_PIPE_BRANCH)
STANDARD_LAYERS: tuple[str, ...] = (
    LAYER_PIPE_MAIN,
    LAYER_PIPE_BRANCH,
    LAYER_VALVE,
    LAYER_METER,
    LAYER_TEXT,
    LAYER_DIM,
    LAYER_AXIS,
    LAYER_OUTLINE,
)


def rounded(point: tuple[float, float]) -> Point:
    return (round(float(point[0]), COORD_DECIMALS) + 0.0, round(float(point[1]), COORD_DECIMALS) + 0.0)


class SymbolKind(str, Enum):
    """图块种类。附件与注记符号都在这里；块名由 `drawing/blocks.py` 决定。"""

    GATE_VALVE = "gate_valve"
    GLOBE_VALVE = "globe_valve"
    CHECK_VALVE = "check_valve"
    WATER_METER = "water_meter"
    PUMP = "pump"
    RISER_TAG = "riser_tag"
    ELEVATION_MARK = "elevation_mark"
    FLOW_ARROW = "flow_arrow"


#: 附件 → 图块。缓闭止回阀与止回阀同一图例，名称靠旁注区分。
FITTING_SYMBOL: dict[FittingKind, SymbolKind] = {
    FittingKind.GATE_VALVE: SymbolKind.GATE_VALVE,
    FittingKind.GLOBE_VALVE: SymbolKind.GLOBE_VALVE,
    FittingKind.CHECK_VALVE: SymbolKind.CHECK_VALVE,
    FittingKind.SLOW_CLOSING_CHECK_VALVE: SymbolKind.CHECK_VALVE,
    FittingKind.WATER_METER: SymbolKind.WATER_METER,
    FittingKind.PUMP: SymbolKind.PUMP,
}


class _Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanNode(_Plan):
    node_id: str
    kind: Literal["riser", "junction", "tee", "end", "entry", "base", "header_end"]
    point: Point


class PipeRunPlan(_Plan):
    """一条管段（渲染成一个 LWPOLYLINE）。每个顶点都必须是已登记的节点。"""

    run_id: str
    layer: Literal["WS-PIPE-MAIN", "WS-PIPE-BRANCH"]
    dn: NominalDiameter
    role: Literal["connector", "header", "branch", "riser", "entry", "detail"]
    node_ids: tuple[str, ...] = Field(min_length=2)
    points: tuple[Point, ...] = Field(min_length=2)


class LabelPlan(_Plan):
    """管径标注。``anchor`` 是 TEXT 的插入点（BOTTOM_CENTER，即基线中点）。"""

    run_id: str
    text: str
    anchor: Point
    rotation: float
    height: float


class SymbolPlan(_Plan):
    symbol: SymbolKind
    layer: str
    point: Point
    rotation: float
    #: 块的缩放 = 出图比例分母（块按图纸毫米定义，出图后符号大小一致）。
    scale: float
    #: 附件所在管段；注记符号（立管编号、标高）为 None。
    run_id: str | None = None
    name: str = ""


TextAlign = Literal["BOTTOM_LEFT", "BOTTOM_CENTER", "MIDDLE_LEFT", "MIDDLE_CENTER", "MIDDLE_RIGHT", "TOP_CENTER"]


class TextPlan(_Plan):
    text: str
    point: Point
    height: float
    rotation: float = 0.0
    align: TextAlign
    layer: str
    role: str


class LinePlan(_Plan):
    """非管线的线：轴线、楼层线、构筑物轮廓、吊架。"""

    layer: str
    points: tuple[Point, ...] = Field(min_length=2)
    closed: bool = False
    linetype: str | None = None
    role: str


class CirclePlan(_Plan):
    layer: str
    center: Point
    radius: float = Field(gt=0)
    role: str


class NoteBlockPlan(_Plan):
    """多行说明（MTEXT）。``point`` 是左上角插入点。"""

    lines: tuple[str, ...] = Field(min_length=1)
    point: Point
    height: float
    layer: str
    role: Literal["notes", "disclaimer", "legend_title"]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class SheetPlan(_Plan):
    sheet_id: Literal["plan", "schematic", "detail"]
    title: str
    #: 出图比例分母（1:100 → 100）。
    scale: int = Field(ge=1)
    nodes: tuple[PlanNode, ...] = ()
    pipes: tuple[PipeRunPlan, ...] = ()
    labels: tuple[LabelPlan, ...] = ()
    symbols: tuple[SymbolPlan, ...] = ()
    texts: tuple[TextPlan, ...] = ()
    lines: tuple[LinePlan, ...] = ()
    circles: tuple[CirclePlan, ...] = ()
    notes: tuple[NoteBlockPlan, ...] = ()
    #: 本图说明里引用了哪些规则（顺序即说明顺序）。
    rule_ids: tuple[str, ...] = ()


class FloorPlan(SheetPlan):
    sheet_id: Literal["plan"] = "plan"
    riser_count: int = Field(ge=1)
    units: int = Field(ge=1)


class SchematicPlan(SheetPlan):
    sheet_id: Literal["schematic"] = "schematic"
    #: 每层一组支管；组数 = 层数（有测试断言）。
    branch_groups: int = Field(ge=1)
    #: 立管逐层分段：第 k 段供第 k 层，段数 = 层数。
    riser_run_ids: tuple[str, ...] = Field(min_length=1)


class DetailPlan(SheetPlan):
    sheet_id: Literal["detail"] = "detail"
    detail_type: str


class GenerationPlan(_Plan):
    """一次出图的全部规划数据（尚未渲染）。同一请求两次规划逐字节相同。"""

    request: GenerationRequest
    rules_version: str
    riser_position: str
    riser_position_defaulted: bool
    calculations: tuple[CalculationRecord, ...]
    warnings: tuple[str, ...]
    missing_inputs: tuple[str, ...]
    assumptions: tuple[str, ...]
    floor_plan: FloorPlan
    schematic: SchematicPlan
    details: tuple[DetailPlan, ...]

    def sheets(self) -> tuple[SheetPlan, ...]:
        return (self.floor_plan, self.schematic, *self.details)


# ---------------------------------------------------------------------------
# 规划不变量：生成图必须能被本仓库自己的审查器正确读懂
# ---------------------------------------------------------------------------

#: 管径标注离自己管段的上限。审查器的关联半径是 20 图纸单位，留出余量。
LABEL_MAX_DISTANCE = 16.0
#: 最近的标注必须比第二近的"异径"标注至少近这么多，否则先后顺序会决定结果。
_BINDING_MARGIN = 1.0


def _annotations(sheet: SheetPlan) -> list[tuple[str, Point, str]]:
    """图上所有会被审查器当作"标注"读取的文字：``(来源, 插入点, 文本)``。"""

    items: list[tuple[str, Point, str]] = [
        (f"label:{label.run_id}", label.anchor, label.text) for label in sheet.labels
    ]
    items.extend((f"text:{index}", text.point, text.text) for index, text in enumerate(sheet.texts))
    items.extend((f"note:{index}", note.point, note.text) for index, note in enumerate(sheet.notes))
    return items


def validate_sheet(sheet: SheetPlan) -> list[str]:
    """检查规划不变量，返回问题清单（空 = 通过）。

    与审查器同一口径（`drawing/mapping.py` 的 ``parse_dn`` 与点到折线距离，直接复用，
    不另写一份）：对每条管段，半径内最近的带管径文字必须是它自己的标注，且管径一致。
    """

    from water_agent.drawing.mapping import (
        ANNOTATION_MATCH_RADIUS,
        _distance_to_polyline,
        parse_dn,
    )

    problems: list[str] = []
    nodes = {node.node_id: node.point for node in sheet.nodes}
    if len(nodes) != len(sheet.nodes):
        problems.append("节点 id 重复")
    run_ids = [run.run_id for run in sheet.pipes]
    if len(set(run_ids)) != len(run_ids):
        problems.append("管段 id 重复")
    labels_by_run: dict[str, list[LabelPlan]] = {}
    for label in sheet.labels:
        labels_by_run.setdefault(label.run_id, []).append(label)
    runs = {run.run_id: run for run in sheet.pipes}
    for label in sheet.labels:
        if label.run_id not in runs:
            problems.append(f"标注 {label.text} 指向不存在的管段 {label.run_id}")

    annotations = [
        (source, anchor, dn)
        for source, anchor, text in _annotations(sheet)
        if (dn := parse_dn(text)) is not None
    ]
    for run in sheet.pipes:
        if len(run.node_ids) != len(run.points):
            problems.append(f"{run.run_id}：节点数与顶点数不一致")
        for node_id, point in zip(run.node_ids, run.points, strict=False):
            if nodes.get(node_id) != point:
                problems.append(f"{run.run_id}：顶点 {point} 不在已登记节点 {node_id} 上")
        own = labels_by_run.get(run.run_id, [])
        if len(own) != 1:
            problems.append(f"{run.run_id}：应恰有 1 条管径标注，实际 {len(own)} 条")
            continue
        label = own[0]
        if label.text != run.dn.value:
            problems.append(f"{run.run_id}：标注 {label.text} 与管径 {run.dn.value} 不一致")
        own_distance = _distance_to_polyline(label.anchor, run.points)
        if own_distance > LABEL_MAX_DISTANCE:
            problems.append(f"{run.run_id}：标注距管线 {own_distance:.1f} > {LABEL_MAX_DISTANCE}")
        rivals = sorted(
            (distance, source, dn)
            for source, anchor, dn in annotations
            if source != f"label:{run.run_id}"
            and (distance := _distance_to_polyline(anchor, run.points)) <= ANNOTATION_MATCH_RADIUS
        )
        for distance, source, dn in rivals:
            if dn != run.dn.mm and distance < own_distance + _BINDING_MARGIN:
                problems.append(
                    f"{run.run_id}：{source} 的 DN{dn} 离管线 {distance:.1f}，"
                    f"不比自己的标注（{own_distance:.1f}）远，审查器会读错管径"
                )
    for symbol in sheet.symbols:
        if symbol.run_id is None:
            continue
        host = runs.get(symbol.run_id)
        if host is None:
            problems.append(f"符号 {symbol.symbol.value} 指向不存在的管段 {symbol.run_id}")
        elif _distance_to_polyline(symbol.point, host.points) > 0.01:
            problems.append(f"符号 {symbol.symbol.value} 不在管段 {symbol.run_id} 上")
    return problems

__all__ = [
    "COORD_DECIMALS",
    "DIAMETER_BASIS_NOTE",
    "FITTING_SYMBOL",
    "GENERATION_ERROR_STATUS",
    "GEN_INVALID_INPUT",
    "GEN_RENDER_FAILED",
    "GEN_UNSUPPORTED_SYSTEM",
    "LABEL_MAX_DISTANCE",
    "LAYER_AXIS",
    "LAYER_DIM",
    "LAYER_METER",
    "LAYER_OUTLINE",
    "LAYER_PIPE_BRANCH",
    "LAYER_PIPE_MAIN",
    "LAYER_TEXT",
    "LAYER_VALVE",
    "PIPE_LAYERS",
    "RENDER_FAILED_MESSAGE",
    "STANDARD_LAYERS",
    "SUPPORTED_SYSTEM",
    "CalculationRecord",
    "CirclePlan",
    "DetailPlan",
    "FloorPlan",
    "GenerationError",
    "GenerationPlan",
    "GenerationRequest",
    "LabelPlan",
    "LinePlan",
    "NominalDiameter",
    "NoteBlockPlan",
    "PipeMaterial",
    "PipeRunPlan",
    "PipeSpec",
    "PlanNode",
    "Point",
    "SchematicPlan",
    "SheetPlan",
    "SymbolKind",
    "SymbolPlan",
    "TextAlign",
    "TextPlan",
    "VelocityCheck",
    "check_velocities",
    "drawing_velocity_note",
    "format_velocity",
    "parse_generation_request",
    "resolve_riser_position",
    "rounded",
    "validate_sheet",
    "velocity_warning",
]
