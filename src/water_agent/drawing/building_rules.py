"""参数化出图的建筑类型规则表（P6-S1 Step 1）：`building_type_rules.json` 的契约与加载。

决策记录 `docs/design/p6-step-1-decision.md` §2。

规则表是**布置规则的唯一来源**：立管位置、横管敷设方式、柱网、阀门与水表设置、
节点大样类型都从这里读，几何规划层不另写常量。每条规则都带来源等级，沿用
P3 `drawing/rules.py` 的 `EvidenceLevel` 三档，而不是执行 prompt 写的两档：

- `formal_clause`：能指回语料里一条**正式**条文（原文不自述"非正式"）；
- `informal_summary`：能指回语料里的 source_id，但原文自述"非正式规范条文"；
- `engineering_practice`：语料里没有支撑，**不得**携带 source_id。

多出来的中间一档不是措辞偏好：实测 `GB50015-2019` 在库的 9 条**全部**自述
"联网整理的设计要点综述，非正式规范条文"（不止 4.7.1）。只允许两档的话，
3.7.3 这类能指回真实 source_id 的要点综述只能降成工程惯例，溯源就丢了；
而升成正式条文又是伪造依据。

说明文字的唯一出口是 `RuleText.rendered_note()`：图面上的规则说明只由规则表的
`statement` / `basis` / `source_id` / `scope_note` / `applied_as` 渲染，生成器不手写
任何规范措辞（同 P3 `Finding.rendered_finding()` 的集中纪律）。
"""

from __future__ import annotations

import json
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from water_agent.drawing.rules import EvidenceLevel

RULES_PATH = Path(__file__).with_name("building_type_rules.json")


class BuildingRulesError(ValueError):
    """规则表缺失或不合契约。属于服务端配置错误，不是用户输入问题。"""


class BuildingType(str, Enum):
    """建筑类型（spec D2，用户拍板三种同时支持）。"""

    RESIDENTIAL = "住宅"
    OFFICE = "办公"
    COMMERCIAL = "商业"


class FittingKind(str, Enum):
    """管道附件。值是图面与说明里用的中文名称。"""

    GATE_VALVE = "闸阀"
    GLOBE_VALVE = "截止阀"
    CHECK_VALVE = "止回阀"
    SLOW_CLOSING_CHECK_VALVE = "缓闭止回阀"
    WATER_METER = "水表"
    PUMP = "水泵"


VALVE_KINDS: frozenset[FittingKind] = frozenset(
    {
        FittingKind.GATE_VALVE,
        FittingKind.GLOBE_VALVE,
        FittingKind.CHECK_VALVE,
        FittingKind.SLOW_CLOSING_CHECK_VALVE,
    }
)

#: 渲染到图面的等级前缀。措辞与 P3 审查报告同一纪律，但这里是"出图说明"而不是
#: "校验结论"，所以正式条文写"依据"而不是"按规范校验"。
_NOTE_PREFIX: dict[EvidenceLevel, str] = {
    EvidenceLevel.FORMAL_CLAUSE: "依据正式条文",
    EvidenceLevel.INFORMAL_SUMMARY: "设计要点提示（非正式条文，需人工确认）",
    EvidenceLevel.ENGINEERING_PRACTICE: "工程惯例（无条文支撑，需人工确认）",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuleText(_Strict):
    """一条带来源等级的规则。所有具体规则都继承它。"""

    rule_id: str = Field(pattern=r"^[a-z]+(\.[a-z_]+)+$")
    #: 渲染进图面说明的规则原文（唯一来源）。
    statement: str = Field(min_length=4)
    basis: EvidenceLevel
    source_id: str | None = None
    #: `source_id` 那条语料原文里的逐字片段（去空白后比对，有测试核对）。
    corpus_quote: str | None = None
    #: 依据说明：写进决策记录与 README，不进图面。
    basis_note: str = Field(min_length=4)
    #: 适用范围提醒，**会**进图面（如"室外标准条文用于建筑内需确认"）。
    scope_note: str | None = None
    #: 本图的具体做法（进图面，紧跟规则原文）。
    applied_as: str | None = None

    @model_validator(mode="after")
    def _gate_source_id(self) -> RuleText:
        if self.basis is EvidenceLevel.ENGINEERING_PRACTICE:
            if self.source_id is not None or self.corpus_quote is not None:
                raise ValueError(
                    f"{self.rule_id}：engineering_practice 不得携带 source_id / corpus_quote"
                    "——给无条文支撑的规则配 source_id 就是伪造依据"
                )
        elif not self.source_id or not self.corpus_quote:
            raise ValueError(
                f"{self.rule_id}：{self.basis.value} 必须同时给出 source_id 与 corpus_quote"
            )
        return self

    def rendered_note(self) -> str:
        """图面说明的一行。**生成器里的规则措辞只能从这里出来。**"""

        cite = f"（{self.source_id}）" if self.source_id else ""
        scope = f"（{self.scope_note}）" if self.scope_note else ""
        applied = f"；本图做法：{self.applied_as}" if self.applied_as else ""
        return f"[{_NOTE_PREFIX[self.basis]}{cite}] {self.statement}{scope}{applied}"


class VelocityRule(RuleText):
    """流速建议区间。超出只写 warnings 并在图面提示复核，**不阻断生成**。"""

    recommended_min_mps: float = Field(gt=0)
    recommended_max_mps: float = Field(gt=0)
    limit_max_mps: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> VelocityRule:
        if not self.recommended_min_mps < self.recommended_max_mps <= self.limit_max_mps:
            raise ValueError(f"{self.rule_id}：流速区间须满足 min < max ≤ limit")
        return self


class GridRule(RuleText):
    """默认轴网（spec D2"默认 3.6 m × 3.6 m，可调"——可调的是这张表）。"""

    bay_x_mm: int = Field(ge=1000, le=20000)
    bay_y_mm: int = Field(ge=1000, le=20000)
    bays_per_unit: int = Field(ge=1, le=6)
    depth_bays: int = Field(ge=1, le=6)
    core_bays: int = Field(ge=0, le=4)


class RiserPosition(_Strict):
    """立管在平面上的位置。

    ``x_ref="core"`` 时 ``x_value`` 是核心筒宽度内的比例（0–1 开区间）；
    ``x_ref="building_edge"`` 时是距建筑左侧轴线的毫米数。``dy_from_route_mm`` 是
    立管相对楼层横干管的纵向偏移，两者之间用一段连接管相连。
    """

    x_ref: Literal["core", "building_edge"]
    x_value: float
    dy_from_route_mm: int

    @model_validator(mode="after")
    def _in_range(self) -> RiserPosition:
        if self.x_ref == "core" and not 0.0 < self.x_value < 1.0:
            raise ValueError("x_ref=core 时 x_value 须在 (0, 1) 内")
        if self.x_ref == "building_edge" and self.x_value <= 0:
            raise ValueError("x_ref=building_edge 时 x_value 须为正的毫米数")
        if self.dy_from_route_mm == 0:
            raise ValueError("dy_from_route_mm 不能为 0（立管与横干管之间要有连接管）")
        return self


class RiserRule(RuleText):
    default_position: str = Field(min_length=1)
    positions: dict[str, RiserPosition] = Field(min_length=1)

    @model_validator(mode="after")
    def _default_listed(self) -> RiserRule:
        if self.default_position not in self.positions:
            raise ValueError(f"{self.rule_id}：默认立管位置 {self.default_position!r} 不在 positions 里")
        return self


class RoutingRule(RuleText):
    method: Literal["沿墙", "沿走廊吊顶", "沿柱网"]
    #: 横干管所沿的轴线序号（纵向，从 0 起）与相对它的偏移。
    route_axis: int = Field(ge=0)
    route_offset_mm: int
    #: 支管相对横干管的去向（-1 = 向 -y）。
    branch_direction: Literal[-1, 1]
    branch_length_mm: int = Field(ge=1000, le=20000)
    #: 安装高度的参照：本层地面（floor）或上层楼板（ceiling）。
    mount: Literal["floor", "ceiling"]
    mount_offset_m: float = Field(gt=0, le=1.5)


class FittingRule(RuleText):
    floor_valve: FittingKind | None = None
    floor_meter: bool
    unit_valve: FittingKind
    unit_meter: bool

    @model_validator(mode="after")
    def _valves_are_valves(self) -> FittingRule:
        for kind in (self.floor_valve, self.unit_valve):
            if kind is not None and kind not in VALVE_KINDS:
                raise ValueError(f"{self.rule_id}：{kind.value} 不是阀门")
        return self


class EntryRule(RuleText):
    """引入管上的附件（系统图底部）。"""

    components: tuple[FittingKind, ...] = Field(min_length=1)
    entry_text: str = Field(min_length=2)
    related_rules: tuple[str, ...] = ()


class DetailComponent(_Strict):
    kind: FittingKind
    #: 图面上的名称（同一种阀门在大样里可以承担不同职能，如"工作阀"/"检修阀"）。
    label: str = Field(min_length=1, max_length=12)


class DetailRule(RuleText):
    detail_type: Literal["水表井", "吊顶内阀门组", "泵房接管"]
    title: str = Field(min_length=2)
    #: 沿管线依次排列的附件；泵房接管时是一台水泵支路上的附件。
    components: tuple[DetailComponent, ...] = Field(min_length=1)
    related_rules: tuple[str, ...] = ()
    pump_count: int | None = Field(default=None, ge=1, le=4)

    @model_validator(mode="after")
    def _pump_shape(self) -> DetailRule:
        has_pump = any(c.kind is FittingKind.PUMP for c in self.components)
        if self.detail_type == "泵房接管":
            if self.pump_count is None or not has_pump:
                raise ValueError(f"{self.rule_id}：泵房接管须给出 pump_count 且附件含水泵")
        elif self.pump_count is not None or has_pump:
            raise ValueError(f"{self.rule_id}：只有泵房接管可以含水泵")
        return self


class BuildingTypeRules(_Strict):
    unit_label: str = Field(min_length=1)
    #: 支管末端的说明（"接户内给水管"等）。
    branch_end_text: str = Field(min_length=2)
    grid: GridRule
    riser: RiserRule
    routing: RoutingRule
    fittings: FittingRule
    entry: EntryRule
    detail: DetailRule

    @model_validator(mode="after")
    def _consistent(self) -> BuildingTypeRules:
        if self.routing.route_axis > self.grid.depth_bays:
            raise ValueError("routing.route_axis 超出轴网进深")
        for name, position in self.riser.positions.items():
            if position.x_ref == "core" and self.grid.core_bays == 0:
                raise ValueError(f"立管位置 {name!r} 以核心筒定位，但 grid.core_bays = 0")
        return self

    def rules(self) -> tuple[RuleText, ...]:
        return (self.grid, self.riser, self.routing, self.fittings, self.entry, self.detail)


class BuildingRules(_Strict):
    schema_version: Literal[1]
    rules_version: str = Field(min_length=1)
    corpus_checked_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    velocity: VelocityRule
    backflow_prevention: RuleText
    pump_suction_valve: RuleText
    pump_discharge_valves: RuleText
    building_types: dict[BuildingType, BuildingTypeRules]

    @model_validator(mode="after")
    def _complete(self) -> BuildingRules:
        missing = set(BuildingType) - set(self.building_types)
        if missing:
            raise ValueError(f"缺少建筑类型：{sorted(t.value for t in missing)}")
        ids = [rule.rule_id for rule in self.all_rules()]
        duplicated = sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1})
        if duplicated:
            raise ValueError(f"rule_id 重复：{duplicated}")
        known = set(ids)
        for typed in self.building_types.values():
            for rule_id in (*typed.entry.related_rules, *typed.detail.related_rules):
                if rule_id not in known:
                    raise ValueError(f"related_rules 引用了不存在的规则 {rule_id!r}")
        return self

    def global_rules(self) -> tuple[RuleText, ...]:
        return (
            self.velocity,
            self.backflow_prevention,
            self.pump_suction_valve,
            self.pump_discharge_valves,
        )

    def all_rules(self) -> tuple[RuleText, ...]:
        typed = tuple(
            rule
            for building_type in BuildingType
            if building_type in self.building_types
            for rule in self.building_types[building_type].rules()
        )
        return self.global_rules() + typed

    def rule(self, rule_id: str) -> RuleText:
        for candidate in self.all_rules():
            if candidate.rule_id == rule_id:
                return candidate
        raise KeyError(rule_id)

    def for_type(self, building_type: BuildingType) -> BuildingTypeRules:
        return self.building_types[building_type]


def parse_building_rules(payload: object) -> BuildingRules:
    try:
        return BuildingRules.model_validate(payload)
    except ValidationError as exc:
        raise BuildingRulesError(f"规则表不合契约：{exc.error_count()} 处错误（{exc.errors()[0]['msg']}）") from exc


@lru_cache(maxsize=1)
def _load_default() -> BuildingRules:
    return _load(RULES_PATH)


def _load(path: Path) -> BuildingRules:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildingRulesError(f"规则表无法读取：{path.name}（{type(exc).__name__}）") from exc
    return parse_building_rules(payload)


def load_building_rules(path: Path | None = None) -> BuildingRules:
    """读取并校验规则表。默认表只解析一次（进程内缓存，规则表随镜像只读分发）。"""

    return _load_default() if path is None else _load(path)


__all__ = [
    "RULES_PATH",
    "VALVE_KINDS",
    "BuildingRules",
    "BuildingRulesError",
    "BuildingType",
    "BuildingTypeRules",
    "DetailComponent",
    "DetailRule",
    "EntryRule",
    "FittingKind",
    "FittingRule",
    "GridRule",
    "RiserPosition",
    "RiserRule",
    "RoutingRule",
    "RuleText",
    "VelocityRule",
    "load_building_rules",
    "parse_building_rules",
]
