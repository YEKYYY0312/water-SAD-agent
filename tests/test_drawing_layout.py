"""P6-S1 Step 3：几何规划（纯数据，不碰 ezdxf）的属性测试。

守住：
- 管段每个顶点都落在已登记节点上；每条管段恰有一条管径标注，在自己管线 16 mm 以内，
  且是 20 mm 半径内离它最近的带管径文字（审查器按这个读管径）——`validate_sheet`；
- 平面图一根立管；系统图立管段数 = 层数、支管组数 = 层数，立管逐层首尾相接；
- 斜支管不越过上下楼层线；管径标注的文字框不压别的管线或楼层线；同一管段上的附件不重叠；
- 附件种类与顺序只来自规则表；
- 确定性：同一请求两次规划逐字节相同；规划模块不用随机数、不读时钟、不 import 模型/HTTP 客户端。
"""

from __future__ import annotations

import ast
import itertools
import math
from pathlib import Path

import pytest

from water_agent.drawing.building_rules import BuildingType, FittingKind, load_building_rules
from water_agent.drawing.detail_drawings import plan_details
from water_agent.drawing.generation_models import (
    FITTING_SYMBOL,
    LABEL_MAX_DISTANCE,
    GenerationRequest,
    SheetPlan,
    parse_generation_request,
    validate_sheet,
)
from water_agent.drawing.layout_engine import (
    DISCLAIMER,
    LABEL_OFFSET,
    SYMBOL_LENGTH_MM,
    plan_floor,
    text_width,
)
from water_agent.drawing.mapping import ANNOTATION_MATCH_RADIUS
from water_agent.drawing.system_schematic import plan_schematic

REPO_ROOT = Path(__file__).resolve().parents[1]
DRAWING_DIR = REPO_ROOT / "src" / "water_agent" / "drawing"
PLANNER_MODULES = ("generation_models.py", "layout_engine.py", "system_schematic.py", "detail_drawings.py")

RISERS = {
    BuildingType.RESIDENTIAL: ("楼梯间", "管道井"),
    BuildingType.OFFICE: ("核心筒",),
    BuildingType.COMMERCIAL: ("边角",),
}


def _request(
    building_type: BuildingType,
    *,
    riser: str | None = None,
    floors: int = 6,
    units: int = 2,
    floor_height: float = 3.0,
    main: str = "DN50",
    branch: str = "DN25",
    flow: float = 2.5,
) -> GenerationRequest:
    payload: dict = {
        "building_type": building_type.value,
        "floors": floors,
        "units_per_floor": units,
        "main_pipe": {"dn": main, "material": "PPR"},
        "branch_pipe": {"dn": branch, "material": "PPR"},
        "flow_rate_lps": flow,
        "pressure_mpa": 0.35,
        "floor_height_m": floor_height,
    }
    if riser is not None:
        payload["riser_position"] = riser
    return parse_generation_request(payload)


def _sheets(request: GenerationRequest) -> list[SheetPlan]:
    rules = load_building_rules()
    return [plan_floor(request, rules), plan_schematic(request, rules), *plan_details(request, rules)]


def _sweep() -> list[GenerationRequest]:
    requests: list[GenerationRequest] = []
    for building_type, risers in RISERS.items():
        for riser in risers:
            for units, floors in ((1, 1), (2, 6), (3, 2), (8, 33), (5, 12)):
                requests.append(_request(building_type, riser=riser, units=units, floors=floors))
            for height in (2.4, 6.0):
                requests.append(_request(building_type, riser=riser, units=4, floors=3, floor_height=height))
            for main, branch in (("DN15", "DN15"), ("DN150", "DN150"), ("DN100", "DN20")):
                requests.append(_request(building_type, riser=riser, units=3, floors=3, main=main, branch=branch))
    return requests


SWEEP = _sweep()


def _ids(request: GenerationRequest) -> str:
    return (
        f"{request.building_type.value}-{request.riser_position}-{request.floors}F-{request.units_per_floor}u-"
        f"{request.floor_height_m}m-{request.main_pipe.dn.value}-{request.branch_pipe.dn.value}"
    )


# ---------------------------------------------------------------------------
# 几何工具：旋转矩形 vs 线段
# ---------------------------------------------------------------------------


def _to_local(point, anchor, angle):
    rad = math.radians(angle)
    dx, dy = point[0] - anchor[0], point[1] - anchor[1]
    return (dx * math.cos(rad) + dy * math.sin(rad), -dx * math.sin(rad) + dy * math.cos(rad))


def _segment_hits_box(a, b, box) -> bool:
    """Liang–Barsky：线段 ab 是否穿过轴对齐矩形 box=(x0, y0, x1, y1)。"""

    x0, y0, x1, y1 = box
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def _label_box(label):
    width = text_width(label.text, label.height)
    # 缩一圈 1 mm：文字框贴着别的线擦边不算压线。
    return (-width / 2 + 1, 1.0, width / 2 - 1, label.height - 1)


# ---------------------------------------------------------------------------
# 属性测试
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("request_", SWEEP, ids=_ids)
def test_every_sheet_satisfies_the_reviewer_binding_invariants(request_: GenerationRequest) -> None:
    for sheet in _sheets(request_):
        assert validate_sheet(sheet) == [], sheet.sheet_id


@pytest.mark.parametrize("request_", SWEEP, ids=_ids)
def test_labels_do_not_overprint_other_pipes_or_floor_lines(request_: GenerationRequest) -> None:
    for sheet in _sheets(request_):
        obstacles = [(run.run_id, run.points) for run in sheet.pipes]
        obstacles += [
            (f"line:{line.role}", line.points) for line in sheet.lines if line.role in ("floor_line", "outline", "ceiling")
        ]
        for label in sheet.labels:
            box = _label_box(label)
            for owner, points in obstacles:
                if owner == label.run_id:
                    continue
                for a, b in itertools.pairwise(points):
                    hit = _segment_hits_box(_to_local(a, label.anchor, label.rotation), _to_local(b, label.anchor, label.rotation), box)
                    assert not hit, f"{sheet.sheet_id}：{label.run_id} 的 {label.text} 压到 {owner}"


@pytest.mark.parametrize("request_", SWEEP, ids=_ids)
def test_fittings_on_the_same_pipe_do_not_overlap(request_: GenerationRequest) -> None:
    for sheet in _sheets(request_):
        by_run: dict[str, list] = {}
        for symbol in sheet.symbols:
            if symbol.run_id is not None:
                by_run.setdefault(symbol.run_id, []).append(symbol)
        for run_id, symbols in by_run.items():
            for first, second in itertools.combinations(symbols, 2):
                half = (SYMBOL_LENGTH_MM[first.symbol] * first.scale + SYMBOL_LENGTH_MM[second.symbol] * second.scale) / 2
                gap = math.dist(first.point, second.point)
                assert gap >= half, f"{sheet.sheet_id}：{run_id} 上 {first.name} 与 {second.name} 重叠"


def test_label_offset_is_inside_the_reviewer_radius() -> None:
    assert LABEL_OFFSET < LABEL_MAX_DISTANCE < ANNOTATION_MATCH_RADIUS


class TestFloorPlan:
    @pytest.mark.parametrize("building_type", list(BuildingType))
    def test_one_riser_and_one_branch_per_unit(self, building_type: BuildingType) -> None:
        for units in (1, 2, 7, 8):
            plan = plan_floor(_request(building_type, units=units), load_building_rules())
            assert plan.riser_count == 1
            assert [c.role for c in plan.circles].count("riser_section") == 1
            assert sum(1 for run in plan.pipes if run.role == "branch") == units
            assert sum(1 for run in plan.pipes if run.role == "connector") == 1
            assert plan.units == units

    def test_fittings_follow_the_rule_table(self) -> None:
        rules = load_building_rules()
        for building_type in BuildingType:
            fittings = rules.for_type(building_type).fittings
            plan = plan_floor(_request(building_type, units=3), rules)
            branch = [s for s in plan.symbols if s.run_id == "plan.branch.1"]
            expected = [fittings.unit_valve] + ([FittingKind.WATER_METER] if fittings.unit_meter else [])
            assert [s.symbol for s in branch] == [FITTING_SYMBOL[k] for k in expected]
            connector = [s.symbol for s in plan.symbols if s.run_id == "plan.connector"]
            floor = ([fittings.floor_valve] if fittings.floor_valve else []) + (
                [FittingKind.WATER_METER] if fittings.floor_meter else []
            )
            assert connector == [FITTING_SYMBOL[k] for k in floor]

    def test_riser_position_moves_the_riser(self) -> None:
        rules = load_building_rules()
        stair = plan_floor(_request(BuildingType.RESIDENTIAL, riser="楼梯间"), rules)
        shaft = plan_floor(_request(BuildingType.RESIDENTIAL, riser="管道井"), rules)
        assert next(n.point for n in stair.nodes if n.node_id == "riser") != next(
            n.point for n in shaft.nodes if n.node_id == "riser"
        )

    def test_axis_lines_use_the_center_linetype_layer(self) -> None:
        plan = plan_floor(_request(BuildingType.OFFICE), load_building_rules())
        axes = [line for line in plan.lines if line.role == "axis"]
        assert axes and all(line.layer == "WS-AXIS" and line.linetype == "CENTER" for line in axes)
        letters = [t.text for t in plan.texts if t.role == "axis_label" and t.text.isalpha()]
        assert not set(letters) & {"I", "O", "Z"}


class TestSchematic:
    @pytest.mark.parametrize("floors", [1, 2, 6, 33])
    @pytest.mark.parametrize("building_type", list(BuildingType))
    def test_one_riser_segment_and_one_branch_group_per_floor(self, building_type: BuildingType, floors: int) -> None:
        units = 3
        plan = plan_schematic(_request(building_type, floors=floors, units=units), load_building_rules())
        assert plan.branch_groups == floors
        assert len(plan.riser_run_ids) == floors
        risers = [run for run in plan.pipes if run.role == "riser"]
        assert [run.run_id for run in risers] == list(plan.riser_run_ids)
        for lower, upper in itertools.pairwise(risers):
            assert lower.points[-1] == upper.points[0], "立管逐层首尾相接"
        assert all(run.points[0][0] == run.points[1][0] == 0.0 for run in risers), "立管竖直"
        assert sum(1 for run in plan.pipes if run.role == "header") == floors
        assert sum(1 for run in plan.pipes if run.role == "branch") == floors * units

    @pytest.mark.parametrize("height", [2.4, 3.0, 6.0])
    @pytest.mark.parametrize("building_type", list(BuildingType))
    def test_inclined_branches_stay_between_their_floor_lines(self, building_type: BuildingType, height: float) -> None:
        floors = 4
        plan = plan_schematic(_request(building_type, floors=floors, units=2, floor_height=height), load_building_rules())
        floor_height = height * 1000
        for run in plan.pipes:
            if run.role != "branch":
                continue
            floor = int(run.run_id.split(".")[2])
            low, high = (floor - 1) * floor_height, floor * floor_height
            for _, y in run.points:
                assert low < y < high, f"{run.run_id} 越过楼层线"

    def test_floor_elevations_come_from_the_input_floor_height(self) -> None:
        plan = plan_schematic(_request(BuildingType.RESIDENTIAL, floors=3, floor_height=2.9), load_building_rules())
        elevations = [t.text for t in plan.texts if t.role == "elevation" and not t.text.startswith("横干管")]
        assert elevations == ["±0.000", "2.900", "5.800", "8.700"]

    def test_entry_fittings_follow_the_rule_table(self) -> None:
        rules = load_building_rules()
        for building_type in BuildingType:
            plan = plan_schematic(_request(building_type), rules)
            entry = [s.symbol for s in plan.symbols if s.run_id == "sch.entry"]
            assert entry == [FITTING_SYMBOL[k] for k in rules.for_type(building_type).entry.components]


class TestDetails:
    def test_detail_type_and_components_follow_the_rule_table(self) -> None:
        rules = load_building_rules()
        for building_type in BuildingType:
            detail_rule = rules.for_type(building_type).detail
            (detail,) = plan_details(_request(building_type), rules)
            assert detail.detail_type == detail_rule.detail_type
            assert detail.scale == 20
            first_run = detail.pipes[0].run_id if detail_rule.detail_type != "泵房接管" else "det.pump.1"
            names = [s.name for s in detail.symbols if s.run_id == first_run]
            assert names == [c.label for c in detail_rule.components]

    def test_pump_room_has_one_branch_per_pump(self) -> None:
        rules = load_building_rules()
        (detail,) = plan_details(_request(BuildingType.COMMERCIAL), rules)
        pumps = [run for run in detail.pipes if run.run_id.startswith("det.pump.")]
        assert len(pumps) == rules.for_type(BuildingType.COMMERCIAL).detail.pump_count


class TestNotes:
    @pytest.mark.parametrize("building_type", list(BuildingType))
    def test_every_sheet_carries_the_fixed_disclaimer_and_its_rule_notes(self, building_type: BuildingType) -> None:
        rules = load_building_rules()
        for sheet in _sheets(_request(building_type)):
            # 说明按宽度折行（续行缩进），比对时去掉空白。
            text = "".join("".join(line for block in sheet.notes for line in block.lines).split())
            assert "".join(DISCLAIMER.split()) in text
            for rule_id in sheet.rule_ids:
                note = "".join(rules.rule(rule_id).rendered_note().split())
                assert note in text, f"{sheet.sheet_id}：{rule_id} 的说明不是从规则表渲染的"

    def test_note_lines_are_wrapped_to_a_bounded_width(self) -> None:
        """每行 ≤ 60 个中文字宽：出图可读，也让 GBK 下每行不超过 R2000 字符串的 255 字节。"""

        from water_agent.drawing.layout_engine import NOTE_LINE_UNITS, wrap_note_line

        for request in SWEEP[:12]:
            for sheet in _sheets(request):
                for note in sheet.notes:
                    for line in note.lines:
                        assert len(line.encode("gbk")) <= 255, line
                        units = sum(1.0 if ord(c) > 0x2E7F else 0.55 for c in line)
                        assert units <= NOTE_LINE_UNITS + 12, line  # 单个长西文词不拆，允许略超
        parts = wrap_note_line("甲" * 70 + " GB50013-2018 6.3.5 乙")
        assert len(parts) == 2 and parts[1].startswith("   ")
        assert all("GB50013-2018" not in part[:-1] or "GB50013-2018" in part for part in parts)

    def test_out_of_band_velocity_is_annotated_on_the_drawing(self) -> None:
        rules = load_building_rules()
        schematic = plan_schematic(_request(BuildingType.RESIDENTIAL, flow=2.5), rules)
        notes = [t.text for t in schematic.texts if t.role == "velocity_note"]
        assert "主管流速 1.27 m/s，超出建议区间，需复核" in notes
        assert any("支管流速未校核" in n for n in notes)
        within = plan_schematic(_request(BuildingType.RESIDENTIAL, flow=1.5), rules)
        assert not any("超出建议区间" in t.text for t in within.texts)

    def test_only_rule_table_numbers_and_inputs_appear_as_elevations(self) -> None:
        rules = load_building_rules()
        plan = plan_floor(_request(BuildingType.OFFICE, floor_height=3.6), rules)
        mount = rules.for_type(BuildingType.OFFICE).routing.mount_offset_m
        assert any(t.text == f"横干管 h+{3.6 - mount:.2f}" for t in plan.texts)


class TestDeterminism:
    @pytest.mark.parametrize("building_type", list(BuildingType))
    def test_same_request_same_bytes(self, building_type: BuildingType) -> None:
        request = _request(building_type, floors=12, units=5)
        first = [sheet.model_dump_json() for sheet in _sheets(request)]
        second = [sheet.model_dump_json() for sheet in _sheets(request)]
        assert first == second

    def test_different_requests_differ(self) -> None:
        a = plan_schematic(_request(BuildingType.RESIDENTIAL, floors=6), load_building_rules())
        b = plan_schematic(_request(BuildingType.RESIDENTIAL, floors=7), load_building_rules())
        assert a.model_dump_json() != b.model_dump_json()

    def test_planner_modules_use_no_randomness_clock_model_or_network(self) -> None:
        forbidden_modules = {
            "random",
            "secrets",
            "uuid",
            "time",
            "datetime",
            "langchain",
            "langchain_core",
            "langchain_openai",
            "langgraph",
            "openai",
            "anthropic",
            "httpx",
            "requests",
            "urllib",
            "http",
            "socket",
            "ezdxf",
        }
        for name in PLANNER_MODULES:
            tree = ast.parse((DRAWING_DIR / name).read_text(encoding="utf-8"))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            assert not imported & forbidden_modules, f"{name} 引入了 {sorted(imported & forbidden_modules)}"


def test_overlap_checks_are_not_vacuous() -> None:
    """重叠检查依赖 role 名与矩形求交；改名或求交写错时本测试先红（否则检查会悄悄变空）。"""

    roles = {line.role for request in SWEEP[:6] for sheet in _sheets(request) for line in sheet.lines}
    assert {"axis", "floor_line", "outline"} <= roles
    sheet = _sheets(_request(BuildingType.RESIDENTIAL, floors=2, units=2))[1]
    label = sheet.labels[3]
    other = next(run for run in sheet.pipes if run.run_id != label.run_id)
    a, b = other.points[0], other.points[1]
    through = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2 - label.height / 2)
    away = (through[0], through[1] - 2 * label.height)
    for angle in (0.0, 45.0, 90.0):
        assert _segment_hits_box(_to_local(a, through, angle), _to_local(b, through, angle), _label_box(label))
    assert not _segment_hits_box(_to_local(a, away, 0.0), _to_local(b, away, 0.0), _label_box(label))
