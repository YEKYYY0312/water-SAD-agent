"""Single-bathroom gravity drainage drawing from user-supplied connection points."""

from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Annotated, Any, Literal, get_args
from uuid import uuid4

import ezdxf
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from water_agent.drawing.generation_models import (
    GEN_INVALID_INPUT,
    GEN_RENDER_FAILED,
    RENDER_FAILED_MESSAGE,
    GenerationError,
)
from water_agent.drawing.generation_service import DRAFT_NOTICE, publish_drawing_zip

DRAINAGE_SYSTEM = "排水"
FIXTURES = (
    ("toilet", "WC", "坐便器"),
    ("basin", "WB", "洗手盆"),
    ("floor_drain", "FD", "地漏"),
)
FILENAMES = {"plan": "01-生活排水平面图.dxf", "schematic": "02-生活排水系统图.dxf"}
LAYERS = {"DR-PIPE": 4, "DR-POINT": 1, "DR-OUTLINE": 8, "DR-NOTE": 7, "DR-FLOW": 2}
DUAL_PIPE_LAYERS = {"DR-PIPE-A": 4, "DR-PIPE-B": 6}
APP_ID = "WA_DRAINAGE"
LIMITATION = (
    "单卫生间污废合流布置。仅核对接点几何与正向落差。"
    "管径为用户填写，未校核排水能力、通气、水封、管材、结构或规范适用性。"
    "图纸终点为立管接驳点，立管全高及出户管需另行设计。"
)
logger = logging.getLogger(__name__)

Coordinate = Annotated[int, Field(ge=0, le=30000)]
RoomSize = Annotated[int, Field(ge=500, le=30000)]
Invert = Annotated[int, Field(ge=-100000, le=100000)]
NominalSizes = Literal[32, 40, 50, 65, 75, 80, 90, 100, 110, 125, 150, 160, 200]


def _integer_diameter(value: object) -> int:
    if type(value) is not int:
        raise ValueError("管径必须是整数")
    return value


Diameter = Annotated[NominalSizes, BeforeValidator(_integer_diameter)]


class DrainageInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Connection(DrainageInput):
    x_mm: Coordinate
    y_mm: Coordinate
    invert_mm: Invert

    @property
    def xy(self) -> tuple[int, int]:
        return self.x_mm, self.y_mm


class FixtureOutlet(Connection):
    dn_mm: Diameter


class FixtureOutlets(DrainageInput):
    toilet: FixtureOutlet
    basin: FixtureOutlet
    floor_drain: FixtureOutlet


class DrainageRoom(DrainageInput):
    system_type: Literal["排水"]
    room_width_mm: RoomSize
    room_depth_mm: RoomSize


class DrainageRequest(DrainageRoom):
    fixtures: FixtureOutlets
    junction: Connection
    riser: Connection
    collector_dn_mm: Diameter


def drainage_options() -> dict[str, Any]:
    """Form vocabulary and an explicitly labelled synthetic example."""
    return {
        "system_type": DRAINAGE_SYSTEM,
        "fixtures": [{"key": k, "label": label} for k, _, label in FIXTURES],
        "diameters_mm": list(get_args(NominalSizes)),
        "example": {
            "system_type": DRAINAGE_SYSTEM,
            "room_width_mm": 2600,
            "room_depth_mm": 2400,
            "fixtures": {
                "toilet": {"x_mm": 400, "y_mm": 1800, "invert_mm": -130, "dn_mm": 100},
                "basin": {"x_mm": 1000, "y_mm": 2000, "invert_mm": -120, "dn_mm": 50},
                "floor_drain": {"x_mm": 400, "y_mm": 200, "invert_mm": -140, "dn_mm": 50},
            },
            "junction": {"x_mm": 1000, "y_mm": 1000, "invert_mm": -150},
            "riser": {"x_mm": 2000, "y_mm": 1000, "invert_mm": -170},
            "collector_dn_mm": 100,
        },
    }


@dataclass(frozen=True, slots=True)
class DrainageSegment:
    start_id: str
    end_id: str
    start: Connection
    end: Connection
    dn_mm: int
    diameter_mark: Literal["DN", "De"] = "DN"
    outlet_id: str | None = None

    @property
    def segment_id(self) -> str:
        return f"{self.start_id}-{self.end_id}"

    @property
    def length_mm(self) -> float:
        return math.dist(self.start.xy, self.end.xy)

    @property
    def drop_mm(self) -> int:
        return self.start.invert_mm - self.end.invert_mm

    @property
    def slope_percent(self) -> float:
        return 100 * self.drop_mm / self.length_mm

    @property
    def diameter_label(self) -> str:
        return f"{self.diameter_mark}{self.dn_mm}"

    def label(self) -> str:
        group = f"出口 {self.outlet_id} · " if self.outlet_id is not None else ""
        return (
            f"{group}{self.segment_id}  {self.diameter_label}  i={self.slope_percent:.4f}%"
            f"  L={self.length_mm:.1f} mm  落差={self.drop_mm} mm"
        )

    def as_dict(self) -> dict[str, Any]:
        result = {
            "id": self.segment_id,
            "from_invert_mm": self.start.invert_mm,
            "to_invert_mm": self.end.invert_mm,
            "note": self.label(),
        }
        if self.outlet_id is not None:
            return {**result, "outlet_id": self.outlet_id, "diameter_label": self.diameter_label,
                    "slope_percent": self.slope_percent}
        return result


@dataclass(frozen=True, slots=True)
class DualLayout:
    point_names: dict[str, str]
    outlets: tuple[str, str]
    schematic: dict[str, tuple[int, int]]
    limitation: str


@dataclass(frozen=True, slots=True)
class DrainagePlan:
    request: DrainageRoom
    points: dict[str, Connection]
    segments: tuple[DrainageSegment, ...]
    dual: DualLayout | None = None


def _invalid(field: str, message: str) -> GenerationError:
    return GenerationError(
        GEN_INVALID_INPUT,
        "请检查排水出图参数",
        fields=[{"loc": field, "type": "drainage_geometry", "message": message}],
    )


def parse_room[RoomT: DrainageRoom](model: type[RoomT], payload: object) -> RoomT:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        fields = []
        for error in exc.errors(include_input=False, include_context=False, include_url=False):
            kind = error["type"]
            # Extra field names come from the request body, so report their parent path only.
            path = error["loc"][:-1] if kind == "extra_forbidden" else error["loc"]
            fields.append(
                {"loc": ".".join(map(str, path)) or "(root)", "type": kind, "message": error["msg"]}
            )
        raise GenerationError(GEN_INVALID_INPUT, "请检查排水出图参数", fields=fields) from None


def plan_drainage(payload: object) -> DrainagePlan:
    request = parse_room(DrainageRequest, payload)
    fixture_points: list[tuple[str, FixtureOutlet]] = [
        (node, getattr(request.fixtures, key)) for key, node, _ in FIXTURES
    ]
    points: dict[str, Connection] = {
        **dict(fixture_points),
        "J": request.junction,
        "S": request.riser,
    }
    paths = {
        **{node: f"fixtures.{key}" for key, node, _ in FIXTURES},
        "J": "junction",
        "S": "riser",
    }
    seen: set[tuple[int, int]] = set()
    for node, point in points.items():
        path = paths[node]
        if point.x_mm > request.room_width_mm:
            raise _invalid(f"{path}.x_mm", "X 坐标超出卫生间宽度")
        if point.y_mm > request.room_depth_mm:
            raise _invalid(f"{path}.y_mm", "Y 坐标超出卫生间深度")
        if point.xy in seen:
            raise _invalid(path, "接点平面坐标重合，无法绘制独立接点")
        seen.add(point.xy)

    if any(outlet.dn_mm > request.collector_dn_mm for _, outlet in fixture_points):
        raise _invalid("collector_dn_mm", "此模板的合流管管径不得小于任一支管管径")

    segments = tuple(
        DrainageSegment(node, "J", outlet, request.junction, outlet.dn_mm)
        for node, outlet in fixture_points
    ) + (DrainageSegment("J", "S", request.junction, request.riser, request.collector_dn_mm),)
    for segment in segments:
        if segment.drop_mm <= 0:
            path_id = segment.start_id if segment.end_id == "J" else segment.end_id
            raise _invalid(
                f"{paths[path_id]}.invert_mm",
                f"{segment.segment_id} 下游管底须低于上游，请核对统一标高基准",
            )

    # All four segments meet at J. Same-ray vectors are the only way two straight segments overlap.
    jx, jy = request.junction.xy
    rays = {
        node: (point.x_mm - jx, point.y_mm - jy) for node, point in points.items() if node != "J"
    }
    for (first, (ax, ay)), (second, (bx, by)) in combinations(rays.items(), 2):
        if ax * by == ay * bx and ax * bx + ay * by > 0:
            raise _invalid(paths[second], f"{first} 与 {second} 接往汇合点的管线重叠")
    return DrainagePlan(request, points, segments)


def _coordinates(plan: DrainagePlan, sheet_id: str) -> dict[str, tuple[int, int]]:
    if sheet_id == "plan":
        return {node: point.xy for node, point in plan.points.items()}
    if sheet_id == "schematic":
        if plan.dual is not None:
            return plan.dual.schematic
        return {"WC": (0, 1700), "WB": (0, 850), "FD": (0, 0), "J": (2800, 550), "S": (4700, 240)}
    raise ValueError("unknown drainage sheet")


def _tags(segment: DrainageSegment) -> list[tuple[int, str | int]]:
    tags: list[tuple[int, str | int]] = [
        (1000, segment.segment_id),
        (1071, segment.dn_mm),
        (1071, segment.start.invert_mm),
        (1071, segment.end.invert_mm),
    ]
    if segment.outlet_id is not None:
        tags.extend([(1000, segment.diameter_mark), (1000, segment.outlet_id)])
    return tags


def _point_note(node: str, plan: DrainagePlan) -> str:
    names = (plan.dual.point_names if plan.dual is not None else
             {code: label for _, code, label in FIXTURES} | {"J": "汇合点", "S": "立管接驳点"})
    point = plan.points[node]
    return f"{node} {names[node]}  X={point.x_mm} Y={point.y_mm}  管底={point.invert_mm:+d} mm"


def _title(plan: DrainagePlan, sheet_id: str) -> str:
    if plan.dual is not None:
        return "生活排水平面图（单卫生间双出口）" if sheet_id == "plan" else "生活排水系统图（双出口示意，不按比例）"
    return (
        "标准层生活排水平面图（单卫生间）"
        if sheet_id == "plan"
        else "生活排水系统图（连接示意，不按比例）"
    )


def _sheet_dimensions(plan: DrainagePlan, sheet_id: str) -> tuple[int, int]:
    if sheet_id == "schematic" and plan.dual is not None:
        return (max(x for x, _ in plan.dual.schematic.values()) + 600,
                max(y for _, y in plan.dual.schematic.values()) + 450)
    return (
        (plan.request.room_width_mm, plan.request.room_depth_mm)
        if sheet_id == "plan"
        else (4900, 1900)
    )


def _label_height(plan: DrainagePlan, sheet_id: str) -> int:
    return max(45, min(230, max(_sheet_dimensions(plan, sheet_id)) // 50))


def _pipe_layer(plan: DrainagePlan, segment: DrainageSegment) -> str:
    if plan.dual is None:
        return "DR-PIPE"
    return "DR-PIPE-A" if segment.outlet_id == plan.dual.outlets[0] else "DR-PIPE-B"


def _render(plan: DrainagePlan, sheet_id: str) -> bytes:
    doc = ezdxf.new("R2010")
    doc.units = 4
    doc.header["$MEASUREMENT"] = 1
    doc.styles.new("DR-FS", dxfattribs={"font": "simfang.ttf"})
    doc.appids.new(APP_ID)
    layers = LAYERS if plan.dual is None else LAYERS | DUAL_PIPE_LAYERS
    for layer, color in layers.items():
        doc.layers.new(layer, dxfattribs={"color": color})
    msp = doc.modelspace()
    xy = _coordinates(plan, sheet_id)
    width, height = _sheet_dimensions(plan, sheet_id)
    label_height = _label_height(plan, sheet_id)
    gap = label_height * 1.7

    def note(content: str, x: float, y: float, size: int = label_height) -> None:
        msp.add_text(
            content,
            dxfattribs={"layer": "DR-NOTE", "style": "DR-FS", "height": size, "insert": (x, y)},
        )

    if sheet_id == "plan":
        msp.add_lwpolyline(
            [(0, 0), (width, 0), (width, height), (0, height)],
            close=True,
            dxfattribs={"layer": "DR-OUTLINE"},
        )
        note("卫生间边界与接点坐标，单位 mm", 0, -gap * 1.2)
    else:
        outlets = plan.dual.outlets if plan.dual is not None else ("S",)
        for outlet in outlets:
            sx, sy = xy[outlet]
            msp.add_line((sx, sy - 260), (sx, sy + 350), dxfattribs={"layer": "DR-OUTLINE"})
            if plan.dual is None:
                note("接驳至立管", sx + 100, sy + 265)

    for segment in plan.segments:
        start, end = xy[segment.start_id], xy[segment.end_id]
        line = msp.add_line(start, end, dxfattribs={"layer": _pipe_layer(plan, segment)})
        line.set_xdata(APP_ID, _tags(segment))
        for wing, tip in _flow_arrow(start, end, label_height):
            msp.add_line(wing, tip, dxfattribs={"layer": "DR-FLOW"})
    for node, (x, y) in xy.items():
        msp.add_circle((x, y), max(15, label_height * 0.35), dxfattribs={"layer": "DR-POINT"})
        if plan.dual is not None and node in plan.dual.outlets:
            note(f"出口 {node}", x + label_height * 0.65, y + label_height * 0.65)
        else:
            note(node, x + label_height * 0.65, y + label_height * 0.65)

    note(_title(plan, sheet_id), 0, height + 3 * gap, label_height * 2)
    note(DRAFT_NOTICE, 0, height + 1.5 * gap)
    heading = "双出口分别排出" if plan.dual is not None else "污废合流"
    note(f"{heading} · 箭头为流向 · 坐标及管底标高同一基准（mm）", 0, height + 0.35 * gap)
    tx = width + gap * 3
    note("接点与管底标高（用户输入）", tx, height + gap)
    for index, node in enumerate(plan.points, start=1):
        note(_point_note(node, plan), tx, height + (1 - index) * gap)
    slope_offset = len(plan.points) + 0.5
    note("管段坡度（由两端落差与水平长度计算）", tx, height - slope_offset * gap)
    for index, segment in enumerate(plan.segments, start=1):
        note(segment.label(), tx, height - (slope_offset + index) * gap)
    note("管径为用户输入；过流、通气、水封、出户和规范复核待完成。",
         tx, height - (slope_offset + len(plan.segments) + 1.5) * gap)

    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode("utf-8")


def _flow_arrow(
    start: tuple[int, int], end: tuple[int, int], size: int
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    length = math.dist(start, end)
    ux, uy = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
    tip = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
    wing = min(size * 1.3, length * 0.23)
    return [
        (
            (
                tip[0] - wing * ux + side * wing * 0.6 * uy,
                tip[1] - wing * uy - side * wing * 0.6 * ux,
            ),
            tip,
        )
        for side in (-1, 1)
    ]


def verify_drainage_dxf(data: bytes, plan: DrainagePlan, sheet_id: str) -> None:
    """Check units, segment direction, physical metadata and the visible annotations."""
    doc = ezdxf.read(io.StringIO(data.decode("utf-8")))
    msp = doc.modelspace()
    xy = _coordinates(plan, sheet_id)
    pipe_layers = {"DR-PIPE"} if plan.dual is None else set(DUAL_PIPE_LAYERS)
    lines = [line for line in msp.query("LINE") if line.dxf.layer in pipe_layers]
    labels = {entity.dxf.text for entity in msp.query('TEXT[layer=="DR-NOTE"]')}
    expected_labels = {
        _title(plan, sheet_id),
        DRAFT_NOTICE,
        *(_point_note(node, plan) for node in xy),
        *(segment.label() for segment in plan.segments),
    }
    if plan.dual is not None:
        expected_labels.update(f"出口 {outlet}" for outlet in plan.dual.outlets)
    if doc.units != 4 or len(lines) != len(plan.segments) or not expected_labels <= labels:
        raise RuntimeError("排水 DXF 读回不一致")
    arrows = [
        (tuple(line.dxf.start), tuple(line.dxf.end)) for line in msp.query('LINE[layer=="DR-FLOW"]')
    ]
    planned_arrows = [
        ((*wing, 0), (*tip, 0))
        for segment in plan.segments
        for wing, tip in _flow_arrow(
            xy[segment.start_id], xy[segment.end_id], _label_height(plan, sheet_id)
        )
    ]
    if arrows != planned_arrows:
        raise RuntimeError("排水流向标记读回不一致")
    for line, segment in zip(lines, plan.segments, strict=True):
        if (
            tuple(line.dxf.start) != (*xy[segment.start_id], 0)
            or tuple(line.dxf.end) != (*xy[segment.end_id], 0)
            or line.dxf.layer != _pipe_layer(plan, segment)
            or not line.has_xdata(APP_ID)
            or list(line.get_xdata(APP_ID)) != _tags(segment)
        ):
            raise RuntimeError("排水管段坐标、方向或标高读回不一致")


def generate_drainage_set(
    payload: object, *, export_root: Path, owner_user_id: str
) -> dict[str, Any]:
    return publish_drainage_set(plan_drainage(payload), export_root=export_root, owner_user_id=owner_user_id)


def publish_drainage_set(
    plan: DrainagePlan, *, export_root: Path, owner_user_id: str
) -> dict[str, Any]:
    sheets = []
    files = {}
    try:
        for sheet_id, filename in FILENAMES.items():
            data = _render(plan, sheet_id)
            verify_drainage_dxf(data, plan, sheet_id)
            files[filename] = data
            sheets.append({"title": _title(plan, sheet_id), "filename": filename})
    except Exception as exc:
        logger.error("drainage rendering or readback failed: %s", type(exc).__name__, exc_info=exc)
        raise GenerationError(GEN_RENDER_FAILED, RENDER_FAILED_MESSAGE) from None

    generation_id = uuid4().hex
    created_at = datetime.now(UTC).isoformat(timespec="seconds")
    name = "生活排水双出口图纸" if plan.dual is not None else "生活排水图纸"
    zip_name = f"{name}-{generation_id[:8]}.zip"
    limitation = plan.dual.limitation if plan.dual is not None else LIMITATION
    readme = (
        "\n".join(
            [
                DRAFT_NOTICE,
                limitation,
                "坐标单位 mm。管底标高采用用户提供的统一基准。",
                f"卫生间边界：{plan.request.room_width_mm} × {plan.request.room_depth_mm} mm",
                *(_point_note(node, plan) for node in plan.points),
                *(segment.label() for segment in plan.segments),
                "两张图均已读回核对图元和标注，系统图为连接示意。",
            ]
        )
        + "\n"
    )
    size = publish_drawing_zip(
        export_root,
        generation_id,
        files,
        readme,
        {
            "owner_user_id": owner_user_id,
            "created_at": created_at,
            "zip_name": zip_name,
            "request": plan.request.model_dump(mode="json"),
            "rules_version": "drainage-dual-v1" if plan.dual is not None else "drainage-gravity-v1",
        },
    )
    result = {
        "system_type": DRAINAGE_SYSTEM,
        "generation_id": generation_id,
        "created_at": created_at,
        "notice": DRAFT_NOTICE,
        "limitation": limitation,
        "sheets": sheets,
        "segments": [segment.as_dict() for segment in plan.segments],
        "download": {
            "url": f"/api/drawings/{generation_id}/download",
            "filename": zip_name,
            "size_bytes": size,
        },
    }
    if plan.dual is not None:
        result.update(topology="dual_outlet", outlet_ids=list(plan.dual.outlets))
    return result
