from __future__ import annotations

import io
from collections import Counter
from zipfile import ZipFile

import ezdxf
import pytest
import test_drawing_routes as drawing_route_fixtures
from test_drawing_routes import _generate, _login

from water_agent.webapp.auth.models import UserRole

app = drawing_route_fixtures.app
database = drawing_route_fixtures.database
export_root = drawing_route_fixtures.export_root


def dual_example():
    raw = [
        ("B", "basin", 200, 300, -10),
        ("F", "floor_drain", 400, 100, -20),
        ("T", "toilet", 200, 1300, -20),
        ("X", "other", 400, 1700, -40),
        ("J1", "junction", 400, 300, -30),
        ("J2", "junction", 400, 1300, -60),
        ("S1", "outlet", 1000, 300, -60),
        ("S2", "outlet", 1000, 1300, -120),
    ]
    runs = [
        ("B", "J1", "De", 50), ("F", "J1", "De", 50), ("J1", "S1", "De", 75),
        ("T", "J2", "De", 110), ("X", "J2", "DN", 50), ("J2", "S2", "De", 110),
    ]
    return {
        "system_type": "排水", "topology": "dual_outlet", "room_width_mm": 1300, "room_depth_mm": 1900,
        "nodes": [{"id": key, "kind": kind, "x_mm": x, "y_mm": y, "invert_mm": z} for key, kind, x, y, z in raw],
        "pipes": [{"start": start, "end": end, "mark": mark, "size_mm": size} for start, end, mark, size in runs],
    }


def test_two_outlets_remain_independent_in_both_downloaded_drawings(app, database):
    owner = _login(app, database)
    outsider = _login(app, database)
    viewer = _login(app, database, UserRole.VIEWER)
    payload = dual_example()
    options = owner.get("/api/drawings/options").json()["data"]["drainage"]
    assert options["dual_outlet"]["example"]["topology"] == "dual_outlet"
    assert _generate(owner, payload, csrf=False).status_code == 403
    assert _generate(viewer, payload).status_code == 403
    response = _generate(owner, payload)
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["topology"] == "dual_outlet"
    assert [(row["id"], row["outlet_id"], row["diameter_label"]) for row in data["segments"]] == [
        ("B-J1", "S1", "De50"), ("F-J1", "S1", "De50"), ("J1-S1", "S1", "De75"),
        ("T-J2", "S2", "De110"), ("X-J2", "S2", "DN50"), ("J2-S2", "S2", "De110"),
    ]
    assert [row["slope_percent"] for row in data["segments"]] == pytest.approx([10, 5, 5, 20, 5, 10])
    assert outsider.get(data["download"]["url"]).status_code == 404
    download = owner.get(data["download"]["url"])
    assert download.status_code == 200
    with ZipFile(io.BytesIO(download.content)) as archive:
        assert archive.testzip() is None
        readme = archive.read("README.txt").decode("utf-8-sig")
        assert "双出口" in readme and "未校核" in readme
        for sheet in data["sheets"]:
            doc = ezdxf.read(io.StringIO(archive.read(sheet["filename"]).decode()))
            assert doc.units == 4
            model = doc.modelspace()
            pipes = [entity for entity in model.query("LINE") if entity.has_xdata("WA_DRAINAGE")]
            assert Counter(entity.dxf.layer for entity in pipes) == {"DR-PIPE-A": 3, "DR-PIPE-B": 3}
            assert len(model.query('LINE[layer=="DR-FLOW"]')) == 12
            tags = [[tag.value for tag in e.get_xdata("WA_DRAINAGE")] for e in pipes]
            assert tags[2] == ["J1-S1", 75, -30, -60, "De", "S1"]
            assert tags[4] == ["X-J2", 50, -40, -60, "DN", "S2"]
            if sheet is data["sheets"][0]:
                by_id = {tags[i][0]: e for i, e in enumerate(pipes)}
                assert tuple(by_id["J1-S1"].dxf.end) == (1000, 300, 0)
                assert tuple(by_id["J2-S2"].dxf.end) == (1000, 1300, 0)


@pytest.mark.parametrize(("mutation", "field"), [
    (lambda p: p["nodes"][0].pop("invert_mm"), "nodes.0.invert_mm"),
    (lambda p: p["nodes"][1].update(x_mm=200, y_mm=300), "nodes.1"),
    (lambda p: p["nodes"][6].update(invert_mm=-30), "pipes.2"),
    (lambda p: p["nodes"][4].update(kind="outlet"), "nodes"),
    (lambda p: p["pipes"][4].update(mark=""), "pipes.4.mark"),
    (lambda p: p["pipes"][4].update(size_mm=True), "pipes.4.size_mm"),
    (lambda p: p["pipes"][0].update(end="NOTREAL"), "pipes.0.end"),
    (lambda p: p["pipes"].append({"start": "B", "end": "J2", "mark": "De", "size_mm": 50}), "pipes.6.start"),
    (lambda p: (p["pipes"][5].update(end="S1"), p["nodes"][6].update(invert_mm=-70)), "nodes.7"),
    (lambda p: p["nodes"][6].update(x_mm=399, y_mm=1600), "pipes.3"),
    (lambda p: p["nodes"][1].update(x_mm=300, y_mm=300), "pipes.1"),
])
def test_bad_or_ambiguous_dual_geometry_is_rejected_before_publication(app, database, export_root, mutation, field):
    owner = _login(app, database)
    payload = dual_example()
    mutation(payload)
    response = _generate(owner, payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "GEN_INVALID_INPUT"
    assert field in {row["loc"] for row in response.json()["error"]["fields"]}
    assert not (export_root / "drawings").exists()


def test_route_can_have_distinct_turn_and_join_points():
    from water_agent.drawing.drainage_dual import plan_dual_drainage

    payload = dual_example()
    payload["nodes"][5].update(x_mm=600, y_mm=1300)
    payload["nodes"].append({
        "id": "K", "kind": "junction", "x_mm": 400, "y_mm": 1300, "invert_mm": -50,
    })
    payload["pipes"][3]["end"] = "K"
    payload["pipes"].append({"start": "K", "end": "J2", "mark": "De", "size_mm": 110})
    # The existing connection at J2 is fed by X at an angle; K feeds it separately.
    plan = plan_dual_drainage(payload)
    assert plan.dual is not None
    assert plan.dual.outlets == ("S1", "S2")
    assert {pipe.segment_id for pipe in plan.segments if pipe.outlet_id == "S2"} == {
        "T-K", "K-J2", "X-J2", "J2-S2",
    }
    assert len(set(plan.dual.schematic.values())) == len(payload["nodes"])
