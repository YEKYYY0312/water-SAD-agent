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

from water_agent.drawing.building_rules import BuildingRules, BuildingType
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


__all__ = [
    "DIAMETER_BASIS_NOTE",
    "GENERATION_ERROR_STATUS",
    "GEN_INVALID_INPUT",
    "GEN_RENDER_FAILED",
    "GEN_UNSUPPORTED_SYSTEM",
    "RENDER_FAILED_MESSAGE",
    "SUPPORTED_SYSTEM",
    "CalculationRecord",
    "GenerationError",
    "GenerationRequest",
    "NominalDiameter",
    "PipeMaterial",
    "PipeSpec",
    "VelocityCheck",
    "check_velocities",
    "drawing_velocity_note",
    "format_velocity",
    "parse_generation_request",
    "resolve_riser_position",
    "velocity_warning",
]
