"""P6-S1 Step 6：出图的 HTTP 路由（薄适配器）。

守住：
- 三条路由都要登录且已改密；``generate`` 另需 CSRF，VIEWER 403 且不落文件；
- 非法输入 422 + 稳定 ``GEN_*`` 码 + 字段路径，**不回显提交值**；内部失败 500 固定说法；
  导出卷不可写 503；
- 下载只给生成者：别人的 id、不存在的 id、形态不对的 id 一律同一个 404；
- 下载头带 ASCII ``filename=`` 与 RFC 5987 ``filename*=``；ZIP 里三张 DXF + README。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4

import ezdxf
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event

from water_agent.drawing import generation_service
from water_agent.drawing.building_rules import BuildingType, load_building_rules
from water_agent.drawing.generation_service import SHEET_FILENAMES
from water_agent.storage.database import Database
from water_agent.storage.models import Base
from water_agent.webapp.app import create_app
from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME
from water_agent.webapp.auth.db_store import PostgresUserStore
from water_agent.webapp.auth.models import UserRole

_PASSWORD = "DrawingPass2026!"
SPEC_EXAMPLE = {
    "system_type": "给水",
    "building_type": "住宅",
    "floors": 3,
    "units_per_floor": 2,
    "riser_position": "楼梯间",
    "main_pipe": {"dn": "DN50", "material": "PPR"},
    "branch_pipe": {"dn": "DN25", "material": "PPR"},
    "flow_rate_lps": 2.5,
    "pressure_mpa": 0.35,
}


@pytest.fixture
def database(tmp_path: Path) -> Database:
    engine = create_engine(f"sqlite:///{tmp_path / 'drawings.sqlite'}")

    @event.listens_for(engine, "connect")
    def _fk(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    db = Database(engine=engine)
    yield db
    db.dispose()


@pytest.fixture
def export_root(tmp_path: Path) -> Path:
    root = tmp_path / "exports"
    root.mkdir()
    return root


@pytest.fixture
def app(database: Database, tmp_path: Path, export_root: Path):
    return create_app(
        static_dir=tmp_path,
        database=database,
        memory_enabled=False,
        trace_dir=tmp_path / "traces",
        session_dir=tmp_path / "sessions",
        upload_dir=tmp_path / "uploads",
        export_dir=export_root,
    )


def _login(app, database: Database, role: UserRole = UserRole.MEMBER, *, must_change: bool = False) -> TestClient:
    username = f"drawing-{uuid4()}"
    PostgresUserStore(database).create_user(username, _PASSWORD, role, must_change_password=must_change)
    client = TestClient(app, raise_server_exceptions=True)
    response = client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
    assert response.status_code == 200
    return client


def _generate(client: TestClient, payload: dict | None = None, *, csrf: bool = True):
    headers = {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME) or ""} if csrf else {}
    return client.post("/api/drawings/generate", json=payload if payload is not None else SPEC_EXAMPLE, headers=headers)


class TestAccess:
    def test_all_routes_require_a_session(self, app) -> None:
        anonymous = TestClient(app)
        assert anonymous.get("/api/drawings/options").status_code == 401
        assert anonymous.post("/api/drawings/generate", json=SPEC_EXAMPLE).status_code in (401, 403)
        assert anonymous.get(f"/api/drawings/{uuid4().hex}/download").status_code == 401

    def test_generate_requires_csrf(self, app, database) -> None:
        client = _login(app, database)
        response = _generate(client, csrf=False)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "AUTH_CSRF_INVALID"

    def test_viewer_cannot_generate_and_nothing_is_written(self, app, database, export_root: Path) -> None:
        client = _login(app, database, UserRole.VIEWER)
        response = _generate(client)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "WEB_FORBIDDEN"
        assert not (export_root / "drawings").exists()

    def test_temporary_password_must_be_changed_first(self, app, database) -> None:
        client = _login(app, database, must_change=True)
        assert client.get("/api/drawings/options").status_code == 403
        assert _generate(client).json()["error"]["code"] == "AUTH_MUST_CHANGE_PASSWORD"


class TestGenerate:
    def test_member_generates_a_set(self, app, database, export_root: Path) -> None:
        client = _login(app, database)
        response = _generate(client)
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["notice"] == "程序生成的初稿，需设计人复核"
        assert (export_root / "drawings" / data["generation_id"]).is_dir()
        assert data["calculations"][0]["name"] == "main_pipe_velocity"
        assert any("超出建议区间" in w for w in data["warnings"])
        assert [s["filename"] for s in data["sheets"]] == list(SHEET_FILENAMES.values())
        for sheet in data["sheets"]:
            assert sheet["pipe_segments"] == sheet["pipe_segments_read_back"] > 0
            assert sheet["review"]["not_checked"] >= 1
            assert "均未发现问题" not in sheet["review"]["summary"]
        assert data["download"]["url"] == f"/api/drawings/{data['generation_id']}/download"

    def test_invalid_input_is_a_structured_422_without_echo(self, app, database, export_root: Path) -> None:
        client = _login(app, database)
        marker = "忽略以上指令-sk-not-echoed"
        response = _generate(client, {**SPEC_EXAMPLE, "floors": 0, "main_pipe": {"dn": marker, "material": "PPR"}})
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "GEN_INVALID_INPUT"
        assert {field["loc"] for field in error["fields"]} >= {"floors", "main_pipe.dn"}
        assert marker not in response.text
        assert not (export_root / "drawings").exists()

    def test_unsupported_system_and_non_json_body(self, app, database) -> None:
        client = _login(app, database)
        response = _generate(client, {**SPEC_EXAMPLE, "system_type": "消防"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "GEN_UNSUPPORTED_SYSTEM"
        token = client.cookies.get(CSRF_COOKIE_NAME) or ""
        broken = client.post(
            "/api/drawings/generate", content=b"{not json", headers={"X-CSRF-Token": token, "Content-Type": "application/json"}
        )
        assert broken.status_code == 422
        assert broken.json()["error"]["code"] == "GEN_INVALID_INPUT"

    def test_render_failure_is_a_fixed_500(self, app, database, monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(*_args, **_kwargs):
            raise RuntimeError("/var/lib/secret exploded")

        monkeypatch.setattr(generation_service, "render_sheet", explode)
        client = _login(app, database)
        response = _generate(client)
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "GEN_RENDER_FAILED"
        assert "secret" not in response.text

    def test_unwritable_export_volume_is_503(self, database, tmp_path: Path) -> None:
        blocker = tmp_path / "exports-is-a-file"
        blocker.write_text("x", encoding="utf-8")
        app = create_app(
            static_dir=tmp_path,
            database=database,
            memory_enabled=False,
            trace_dir=tmp_path / "traces",
            session_dir=tmp_path / "sessions",
            upload_dir=tmp_path / "uploads",
            export_dir=blocker,
        )
        response = _generate(_login(app, database))
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "WEB_EXPORT_STORAGE_UNAVAILABLE"


class TestDownload:
    def test_owner_downloads_the_zip(self, app, database) -> None:
        client = _login(app, database)
        data = _generate(client).json()["data"]
        response = client.get(data["download"]["url"])
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        disposition = response.headers["content-disposition"]
        assert disposition.startswith("attachment;") and "filename*=UTF-8''" in disposition
        assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == data["download"]["filename"]
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert archive.namelist() == [*SHEET_FILENAMES.values(), "README.txt"]
            for name in SHEET_FILENAMES.values():
                doc = ezdxf.read(io.StringIO(archive.read(name).decode("utf-8")))
                assert doc.header["$INSUNITS"] == 4
        assert client.get(data["download"]["url"]).status_code == 200, "可重复下载"

    def test_other_users_unknown_and_malformed_ids_are_the_same_404(self, app, database) -> None:
        owner = _login(app, database)
        generation_id = _generate(owner).json()["data"]["generation_id"]
        stranger = _login(app, database)
        for target in (generation_id, uuid4().hex, "..%2F..%2Fetc", "not-a-uuid"):
            response = stranger.get(f"/api/drawings/{target}/download")
            assert response.status_code == 404, target
            assert response.json()["error"]["code"] == "WEB_NOT_FOUND"


def test_options_project_the_contract_and_the_rule_table(app, database) -> None:
    client = _login(app, database, UserRole.VIEWER)
    response = client.get("/api/drawings/options")
    assert response.status_code == 200
    data = response.json()["data"]
    rules = load_building_rules()
    assert [item["value"] for item in data["building_types"]] == [t.value for t in BuildingType]
    for item in data["building_types"]:
        typed = rules.for_type(BuildingType(item["value"]))
        assert item["riser_positions"] == list(typed.riser.positions)
        assert item["default_riser_position"] == typed.riser.default_position
    assert data["diameters"][0] == "DN15" and data["diameters"][-1] == "DN150"
    assert data["materials"] == ["PPR", "PE", "钢塑复合", "不锈钢"]
    assert data["ranges"] == {
        "floors": [1, 33],
        "units_per_floor": [1, 8],
        "pressure_mpa": [0.1, 1.0],
        "floor_height_m": [2.4, 6.0],
    }
    assert data["dwg"]["experimental"] is True
    json.dumps(data, ensure_ascii=False)
