"""Bounded, explicitly connected two-outlet drainage for one room."""
from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Annotated, Any, Literal, get_args

from pydantic import Field

from water_agent.drawing.drainage import (
    DRAINAGE_SYSTEM,
    Connection,
    DrainageInput,
    DrainagePlan,
    DrainageRoom,
    DrainageSegment,
    DualLayout,
    _invalid,
    parse_room,
    publish_drainage_set,
)

DUAL_OUTLET = "dual_outlet"
MAX_NODES = 12
PointKind = Literal["toilet", "basin", "floor_drain", "other", "junction", "outlet"]
PipeMark = Literal["DN", "De"]
PointId = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9]{0,4}$")]
PipeSize = Annotated[int, Field(ge=15, le=200)]
POINT_NAMES: dict[PointKind, str] = {
    "toilet": "坐便器",
    "basin": "洗手盆",
    "floor_drain": "地漏",
    "other": "其他排水接点",
    "junction": "汇合或转弯点",
    "outlet": "排水出口",
}
LIMITATION = (
    "单卫生间双出口，两路在本图范围内分别排出。仅核对连接、平面位置与正向落差。"
    "DN/De 按输入原样标注，不互相换算。未校核排水能力、通气、水封、管材、结构或规范适用性；"
    "出口下游需另行设计。"
)


class DualNode(Connection):
    id: PointId
    kind: PointKind


class DualPipe(DrainageInput):
    start: PointId
    end: PointId
    mark: PipeMark
    size_mm: PipeSize


class DualRequest(DrainageRoom):
    topology: Literal["dual_outlet"]
    nodes: Annotated[list[DualNode], Field(min_length=4, max_length=MAX_NODES)]
    pipes: Annotated[list[DualPipe], Field(min_length=2, max_length=MAX_NODES - 2)]


def dual_options() -> dict[str, Any]:
    nodes = [
        ("B", "basin", 300, 600, -110),
        ("F", "floor_drain", 700, 200, -120),
        ("T", "toilet", 300, 1800, -130),
        ("X", "other", 700, 2200, -140),
        ("J1", "junction", 700, 600, -130),
        ("J2", "junction", 700, 1800, -150),
        ("S1", "outlet", 2200, 600, -160),
        ("S2", "outlet", 2200, 1800, -180),
    ]
    pipes = [
        ("B", "J1", "De", 50), ("F", "J1", "De", 50), ("T", "J2", "De", 110),
        ("X", "J2", "DN", 50), ("J1", "S1", "De", 75), ("J2", "S2", "De", 110),
    ]
    return {
        "topology": DUAL_OUTLET,
        "max_nodes": MAX_NODES,
        "point_kinds": [{"value": kind, "label": label} for kind, label in POINT_NAMES.items()],
        "diameter_marks": list(get_args(PipeMark)),
        "example": {
            "system_type": DRAINAGE_SYSTEM,
            "topology": DUAL_OUTLET,
            "room_width_mm": 2600,
            "room_depth_mm": 2400,
            "nodes": [{"id": id_, "kind": kind, "x_mm": x, "y_mm": y, "invert_mm": z} for id_, kind, x, y, z in nodes],
            "pipes": [{"start": start, "end": end, "mark": mark, "size_mm": size} for start, end, mark, size in pipes],
        },
    }


_Point = tuple[int, int]


def _cross(a: _Point, b: _Point, c: _Point) -> int:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: _Point, b: _Point, c: _Point) -> bool:
    return _cross(a, b, c) == 0 and all(min(x, y) <= z <= max(x, y) for x, y, z in zip(a, b, c, strict=True))


def _bad_intersection(a: _Point, b: _Point, c: _Point, d: _Point) -> bool:
    shared = {a, b} & {c, d}
    if shared:
        # With one shared endpoint, only collinear overlap extends past that point.
        return any(
            point not in shared and _on_segment(start, end, point)
            for start, end, point in ((a, b, c), (a, b, d), (c, d, a), (c, d, b))
        )
    ab, cd = (_cross(a, b, c), _cross(a, b, d)), (_cross(c, d, a), _cross(c, d, b))
    return (ab[0] * ab[1] < 0 and cd[0] * cd[1] < 0) or any(
        _on_segment(start, end, point)
        for start, end, point in ((a, b, c), (a, b, d), (c, d, a), (c, d, b))
    )


def _schematic(
    outlets: tuple[str, str], incoming: dict[str, list[str]]
) -> dict[str, _Point]:
    diagram: dict[str, _Point] = {}
    next_y = 350

    def layout(point: str, depth: int) -> int:
        nonlocal next_y
        children = sorted(incoming[point])
        if children:
            ys = [layout(child, depth + 1) for child in children]
            y = (ys[0] + ys[-1]) // 2
        else:
            y = next_y
            next_y += 700
        diagram[point] = (-depth * 1050, y)
        return y

    for outlet in outlets:
        layout(outlet, 0)
        next_y += 1000
    shift = -min(x for x, _ in diagram.values())
    return {point: (x + shift, y) for point, (x, y) in diagram.items()}


def plan_dual_drainage(payload: object) -> DrainagePlan:
    request = parse_room(DualRequest, payload)
    by_id: dict[str, DualNode] = {}
    paths: dict[str, str] = {}
    occupied: set[_Point] = set()
    for index, node in enumerate(request.nodes):
        path = f"nodes.{index}"
        if node.id in by_id:
            raise _invalid(f"{path}.id", "接点编号重复")
        if node.x_mm > request.room_width_mm:
            raise _invalid(f"{path}.x_mm", "X 坐标超出卫生间宽度")
        if node.y_mm > request.room_depth_mm:
            raise _invalid(f"{path}.y_mm", "Y 坐标超出卫生间深度")
        if node.xy in occupied:
            raise _invalid(path, "接点平面坐标重合")
        occupied.add(node.xy)
        by_id[node.id], paths[node.id] = node, path

    outlets = [node.id for node in by_id.values() if node.kind == "outlet"]
    if len(outlets) != 2:
        raise _invalid("nodes", "双出口必须恰好指定两个排水出口")
    ends = (outlets[0], outlets[1])
    outgoing: dict[str, str] = {}
    incoming: dict[str, list[str]] = defaultdict(list)
    for index, pipe in enumerate(request.pipes):
        path = f"pipes.{index}"
        if pipe.start not in by_id:
            raise _invalid(f"{path}.start", "未找到上游接点")
        if pipe.end not in by_id:
            raise _invalid(f"{path}.end", "未找到下游接点")
        a, b = by_id[pipe.start], by_id[pipe.end]
        if a.kind == "outlet" or a.id in outgoing:
            raise _invalid(f"{path}.start", "排水出口不能继续接管，其他接点最多接一个下游")
        if b.kind not in {"junction", "outlet"}:
            raise _invalid(f"{path}.end", "下游必须是汇合或转弯点，或者排水出口")
        if a.invert_mm <= b.invert_mm:
            raise _invalid(path, f"{a.id}-{b.id} 下游管底须低于上游")
        incoming[b.id].append(a.id)
        outgoing[a.id] = b.id

    outlet_for: dict[str, str] = {}
    for node in by_id.values():
        if node.kind == "outlet":
            if not incoming[node.id]:
                raise _invalid(paths[node.id], "每个排水出口必须接入管线")
            outlet_for[node.id] = node.id
            continue
        if node.id not in outgoing:
            raise _invalid(paths[node.id], "接点必须连接下游")
        if node.kind == "junction" and not incoming[node.id]:
            raise _invalid(paths[node.id], "汇合或转弯点必须接入上游管线")
        root = node.id
        # Strictly decreasing inverts rule out cycles; every link reaches one outlet.
        while root in outgoing:
            root = outgoing[root]
        outlet_for[node.id] = root

    segments = tuple(
        DrainageSegment(
            pipe.start, pipe.end, by_id[pipe.start], by_id[pipe.end], pipe.size_mm,
            diameter_mark=pipe.mark, outlet_id=outlet_for[pipe.start],
        )
        for pipe in request.pipes
    )
    for (i, first), (j, second) in combinations(enumerate(segments), 2):
        if _bad_intersection(first.start.xy, first.end.xy, second.start.xy, second.end.xy):
            raise _invalid(f"pipes.{j}", f"管段 {i + 1} 与 {j + 1} 重叠或在未设接点处相交")
    return DrainagePlan(
        request, dict(by_id), segments,
        DualLayout(
            {node.id: POINT_NAMES[node.kind] for node in by_id.values()},
            ends, _schematic(ends, incoming), LIMITATION,
        ),
    )


def generate_dual_drainage_set(
    payload: object, *, export_root: Path, owner_user_id: str
) -> dict[str, Any]:
    return publish_drainage_set(
        plan_dual_drainage(payload), export_root=export_root, owner_user_id=owner_user_id
    )
