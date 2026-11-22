from __future__ import annotations

import io
import zipfile
from pathlib import Path

import ezdxf
import pytest

from test_drawing_routes import _generate, _login
from test_drawing_routes import app as app, database as database, export_root as export_root
from water_agent.drawing.drainage import generate_drainage_set, plan_drainage, verify_drainage_dxf
from water_agent.drawing.generation_models import GenerationError
from water_agent.webapp.auth.models import UserRole


def example():
    return {
        "system_type": "排水",
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
    }


def test_four_segments_have_explicit_inverts_and_gravity_direction():
    segments = plan_drainage(example()).segments
    assert [(p.start_id, p.end_id, p.dn_mm) for p in segments] == [
        ("WC", "J", 100),
        ("WB", "J", 50),
        ("FD", "J", 50),
        ("J", "S", 100),
    ]
    assert [p.length_mm for p in segments] == [1000, 1000, 1000, 1000]
    assert [p.drop_mm for p in segments] == [20, 30, 10, 20]
    assert [p.slope_percent for p in segments] == pytest.approx([2, 3, 1, 2])


@pytest.mark.parametrize(
    ("point", "fields", "expected"),
    [
        ("riser", {"invert_mm": -150}, "riser.invert_mm"),
        ("riser", {"invert_mm": -145}, "riser.invert_mm"),
        ("riser", {"x_mm": 1000}, "riser"),
        ("riser", {"x_mm": 4000}, "riser.x_mm"),
        ("riser", {"x_mm": 1200.2}, "riser.x_mm"),
        ("riser", {"x_mm": True}, "riser.x_mm"),
        ("basin", {"invert_mm": -150}, "fixtures.basin.invert_mm"),
        ("basin", {"x_mm": 400, "y_mm": 1800}, "fixtures.basin"),
        ("basin", {"x_mm": 700, "y_mm": 1400}, "fixtures.basin"),
        ("floor_drain", {"dn_mm": 125}, "collector_dn_mm"),
        ("floor_drain", {"dn_mm": 50.0}, "fixtures.floor_drain.dn_mm"),
    ],
)
def test_inconsistent_geometry_or_raw_type_blocks_generation(tmp_path, point, fields, expected):
    payload = example()
    target = payload[point] if point in ("junction", "riser") else payload["fixtures"][point]
    target.update(fields)
    with pytest.raises(GenerationError) as error:
        generate_drainage_set(payload, export_root=tmp_path, owner_user_id="owner")
    assert error.value.code == "GEN_INVALID_INPUT"
    assert expected in {f["loc"] for f in error.value.fields}
    assert not (tmp_path / "drawings").exists()


def test_dxf_roundtrip_and_changed_geometry_is_rejected(tmp_path):
    payload = example()
    result = generate_drainage_set(payload, export_root=tmp_path, owner_user_id="owner")
    path = tmp_path / "drawings" / result["generation_id"] / "drawing_set.zip"
    with zipfile.ZipFile(path) as archive:
        assert archive.namelist() == [
            "01-生活排水平面图.dxf",
            "02-生活排水系统图.dxf",
            "README.txt",
        ]
        plan_bytes = archive.read(archive.namelist()[0])
        model = ezdxf.read(io.StringIO(plan_bytes.decode("utf-8")))
        assert model.units == 4
        assert len(model.modelspace().query('LINE[layer=="DR-PIPE"]')) == 4
        assert "未校核" in archive.read("README.txt").decode("utf-8-sig")
    verify_drainage_dxf(plan_bytes, plan_drainage(payload), "plan")
    arrow = model.modelspace().query('LINE[layer=="DR-FLOW"]')[0]
    original = arrow.dxf.start
    arrow.dxf.start = (999, 0, 0)
    corrupted_arrow = io.StringIO()
    model.write(corrupted_arrow)
    with pytest.raises(RuntimeError, match="流向标记读回"):
        verify_drainage_dxf(corrupted_arrow.getvalue().encode(), plan_drainage(payload), "plan")
    arrow.dxf.start = original
    pipe = model.modelspace().query('LINE[layer=="DR-PIPE"]')[0]
    pipe.dxf.end = (900, 1000, 0)
    stream = io.StringIO()
    model.write(stream)
    with pytest.raises(RuntimeError, match="读回"):
        verify_drainage_dxf(stream.getvalue().encode(), plan_drainage(payload), "plan")


def test_authenticated_route_lists_generates_and_protects_drainage_zip(
    app, database, export_root: Path
):
    owner = _login(app, database)
    outsider = _login(app, database)
    viewer = _login(app, database, UserRole.VIEWER)
    options = owner.get("/api/drawings/options").json()["data"]
    assert options["system_types"] == ["给水", "排水"]
    assert options["drainage"]["example"]["system_type"] == "排水"
    assert _generate(owner, example(), csrf=False).status_code == 403
    assert _generate(viewer, example()).status_code == 403
    response = _generate(owner, example())
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["system_type"] == "排水"
    assert len(data["segments"]) == 4
    assert len(data["sheets"]) == 2
    url = data["download"]["url"]
    assert outsider.get(url).status_code == 404
    download = owner.get(url)
    assert download.status_code == 200
    assert "filename*=" in download.headers["Content-Disposition"]
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert archive.testzip() is None
