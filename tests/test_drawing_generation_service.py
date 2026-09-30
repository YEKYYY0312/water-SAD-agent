"""P6-S1 Step 5：出图服务——校验 → 规划 → 渲染 → 读回核对 → S50 复审 → 落盘 + ZIP。

守住：
- 落盘在 ``export_root/drawings/{generation_id}/``，原子改名，``meta.json`` 记录生成者；
- ZIP = 三张 DXF + README.txt（勾选 DWG 且回读核对通过时另加实验性 DWG）；
- 复审报告**原样**附上：每张图"未检查"≥1，不出现"审查通过 / 0 缺陷 / 均未发现问题"；
- 输入不合法 → 结构化错误且不落任何文件；内部失败 → ``GEN_RENDER_FAILED``，**不回显异常文本**，
  不留半成品目录；导出目录不可写 → ``OSError``（路由据此报 503）；
- 实验性 DWG 的任何故障都不连累 DXF 交付；服务不 import 模型或 HTTP 客户端。
"""

from __future__ import annotations

import ast
import json
import zipfile
from pathlib import Path

import pytest

from water_agent.documents.dwg_convert import dwg_roundtrip_available
from water_agent.drawing import generation_service
from water_agent.drawing.building_rules import load_building_rules
from water_agent.drawing.generation_models import (
    GEN_INVALID_INPUT,
    GEN_RENDER_FAILED,
    GEN_UNSUPPORTED_SYSTEM,
    RENDER_FAILED_MESSAGE,
    GenerationError,
)
from water_agent.drawing.generation_service import (
    DRAFT_NOTICE,
    META_NAME,
    README_NAME,
    SHEET_FILENAMES,
    ZIP_NAME,
    find_generation,
    generate_drawing_set,
    plan_generation,
)
from water_agent.drawing.generator import DwgExport, ReadbackReport
from water_agent.drawing.layout_engine import DISCLAIMER

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_EXAMPLE = {
    "system_type": "给水",
    "building_type": "住宅",
    "floors": 6,
    "units_per_floor": 2,
    "riser_position": "楼梯间",
    "main_pipe": {"dn": "DN50", "material": "PPR"},
    "branch_pipe": {"dn": "DN25", "material": "PPR"},
    "flow_rate_lps": 2.5,
    "pressure_mpa": 0.35,
}
FORBIDDEN_CLAIMS = ("审查通过", "0 缺陷", "均未发现问题", "符合规范")


@pytest.fixture
def export_root(tmp_path: Path) -> Path:
    root = tmp_path / "exports"
    root.mkdir()
    return root


def _generate(export_root: Path, **overrides):
    return generate_drawing_set({**SPEC_EXAMPLE, **overrides}, export_root=export_root, owner_user_id="user-a")


class TestDeliverable:
    def test_files_zip_and_meta_land_under_the_generation_directory(self, export_root: Path) -> None:
        result = _generate(export_root)
        directory = export_root / "drawings" / result.generation_id
        assert result.directory == directory and directory.is_dir()
        for filename in SHEET_FILENAMES.values():
            assert (directory / filename).is_file()
        meta = json.loads((directory / META_NAME).read_text(encoding="utf-8"))
        assert meta["owner_user_id"] == "user-a"
        assert meta["generation_id"] == result.generation_id
        assert meta["request"]["building_type"] == "住宅"
        with zipfile.ZipFile(directory / ZIP_NAME) as archive:
            names = archive.namelist()
            assert names == [*SHEET_FILENAMES.values(), README_NAME]
            for filename in SHEET_FILENAMES.values():
                assert archive.read(filename) == (directory / filename).read_bytes()
        assert not list((export_root / "drawings").glob(".partial-*")), "改名后不留半成品目录"
        assert result.zip_name.endswith(".zip") and "住宅" in result.zip_name

    def test_readme_carries_parameters_calculations_reviews_and_the_notice(self, export_root: Path) -> None:
        result = _generate(export_root)
        with zipfile.ZipFile(result.zip_path) as archive:
            readme = archive.read(README_NAME).decode("utf-8-sig")
        assert DRAFT_NOTICE in readme and DISCLAIMER in readme
        assert "DN50 PPR" in readme and "2.50 L/s" in readme and "0.35 MPa" in readme
        assert "流速 1.2732 m/s" in readme, "计算结果来自 calc_velocity"
        assert "支管：未计算，缺输入 ['flow_lps']" in readme
        for sheet in result.sheets:
            assert sheet.review.text in readme, "复审报告必须原样附上"
        assert "未检查" in readme
        for claim in FORBIDDEN_CLAIMS:
            assert claim not in readme, claim

    def test_response_reports_both_pipe_counts_and_the_verbatim_review(self, export_root: Path) -> None:
        response = _generate(export_root).to_response()
        assert response["notice"] == DRAFT_NOTICE
        assert response["download"]["url"].endswith("/download")
        assert [s["sheet_id"] for s in response["sheets"]] == ["plan", "schematic", "detail"]
        for sheet in response["sheets"]:
            assert sheet["pipe_segments"] == sheet["pipe_segments_read_back"] == sheet["dn_matched"] > 0
            review = sheet["review"]
            assert review["not_checked"] >= 1 and review["not_checked_items"]
            assert review["pipes_reviewed"] >= sheet["pipe_segments"], "审查器把轴网等线段也算作管段"
            assert review["report_text"].startswith("【DXF 程序化审查报告】")
            assert "未对任何管段执行规则判定" in review["summary"]
            for claim in FORBIDDEN_CLAIMS:
                assert claim not in review["summary"] and claim not in review["report_text"], claim
        json.dumps(response, ensure_ascii=False)

    def test_velocity_warning_and_missing_branch_flow_are_surfaced(self, export_root: Path) -> None:
        response = _generate(export_root).to_response()
        assert any("超出建议区间" in w for w in response["warnings"])
        assert any("branch_flow_rate_lps" in m for m in response["missing_inputs"])
        given = _generate(export_root, branch_flow_rate_lps=0.3).to_response()
        assert given["missing_inputs"] == []
        branch = next(c for c in given["calculations"] if c["name"] == "branch_pipe_velocity")
        assert branch["ok"] is True and branch["values"]["velocity_mps"] > 0

    def test_same_request_plans_identically(self) -> None:
        rules = load_building_rules()
        request = generation_service.parse_generation_request(SPEC_EXAMPLE)
        assert plan_generation(request, rules).model_dump_json() == plan_generation(request, rules).model_dump_json()


class TestFailures:
    def test_invalid_input_writes_nothing(self, export_root: Path) -> None:
        for payload, code in (
            ({**SPEC_EXAMPLE, "floors": 0}, GEN_INVALID_INPUT),
            ({**SPEC_EXAMPLE, "system_type": "排水"}, GEN_UNSUPPORTED_SYSTEM),
            ({**SPEC_EXAMPLE, "riser_position": "核心筒"}, GEN_INVALID_INPUT),
        ):
            with pytest.raises(GenerationError) as info:
                generate_drawing_set(payload, export_root=export_root, owner_user_id="user-a")
            assert info.value.code == code
        assert not (export_root / "drawings").exists() or not list((export_root / "drawings").iterdir())

    def test_render_failure_does_not_leak_internals(self, export_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(*_args, **_kwargs):
            raise RuntimeError("/secret/internal/path exploded")

        monkeypatch.setattr(generation_service, "render_sheet", explode)
        with pytest.raises(GenerationError) as info:
            _generate(export_root)
        assert info.value.code == GEN_RENDER_FAILED
        assert info.value.status_code == 500
        assert info.value.message == RENDER_FAILED_MESSAGE
        assert "secret" not in json.dumps(info.value.to_dict(), ensure_ascii=False)
        assert not (export_root / "drawings").exists() or not list((export_root / "drawings").iterdir())

    def test_unverified_readback_is_never_delivered(self, export_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bad = ReadbackReport(
            ok=True, errors=(), truncated=False, entity_count=1, layers=(), pipe_segments=4,
            planned_segments=5, dn_matched=4, dn_mismatches=("plan.header.left",), suspicious_entities=0,
        )
        monkeypatch.setattr(generation_service, "verify_readback", lambda *_a, **_k: bad)
        with pytest.raises(GenerationError) as info:
            _generate(export_root)
        assert info.value.code == GEN_RENDER_FAILED

    def test_unwritable_export_root_raises_oserror_and_leaves_no_partial(self, tmp_path: Path) -> None:
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("x", encoding="utf-8")
        with pytest.raises(OSError):
            generate_drawing_set(SPEC_EXAMPLE, export_root=blocker, owner_user_id="user-a")


class TestFindGeneration:
    def test_only_the_owner_finds_it(self, export_root: Path) -> None:
        result = _generate(export_root)
        found = find_generation(export_root, result.generation_id, owner_user_id="user-a")
        assert found is not None and found.zip_path == result.zip_path and found.zip_name == result.zip_name
        assert find_generation(export_root, result.generation_id, owner_user_id="user-b") is None
        hyphenated = f"{result.generation_id[:8]}-{result.generation_id[8:12]}-{result.generation_id[12:16]}-{result.generation_id[16:20]}-{result.generation_id[20:]}"
        assert find_generation(export_root, hyphenated, owner_user_id="user-a") is not None

    @pytest.mark.parametrize("bad_id", ["../../etc/passwd", "", "not-a-uuid", "0" * 31, "g" * 32])
    def test_malformed_or_unknown_ids_are_not_found(self, export_root: Path, bad_id: str) -> None:
        _generate(export_root)
        assert find_generation(export_root, bad_id, owner_user_id="user-a") is None
        assert find_generation(export_root, "0" * 32, owner_user_id="user-a") is None


class TestExperimentalDwg:
    def test_not_requested_means_no_dwg_work(self, export_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(generation_service, "export_dwg", lambda *_: pytest.fail("未勾选 DWG 不得转换"))
        response = _generate(export_root).to_response()
        assert response["dwg_requested"] is False
        assert all(sheet["dwg"] is None for sheet in response["sheets"])

    def test_unavailable_or_crashing_writer_keeps_the_dxf_set(self, export_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(generation_service, "export_dwg", lambda *_: DwgExport("unavailable", "服务器未安装 LibreDWG，只交付 DXF"))
        response = _generate(export_root, include_dwg=True).to_response()
        assert all(sheet["dwg"]["status"] == "unavailable" for sheet in response["sheets"])
        assert any("DWG（实验性）未交付" in w for w in response["warnings"])

        def crash(*_args):
            raise RuntimeError("boom")

        monkeypatch.setattr(generation_service, "export_dwg", crash)
        result = _generate(export_root, include_dwg=True)
        assert all(sheet.dwg is not None and sheet.dwg.status == "failed" for sheet in result.sheets)
        with zipfile.ZipFile(result.zip_path) as archive:
            assert not [name for name in archive.namelist() if name.endswith(".dwg")]

    @pytest.mark.skipif(not dwg_roundtrip_available(), reason="LibreDWG (dxf2dwg + dwg2dxf) not installed on this host")
    def test_real_dwg_roundtrip_adds_experimental_files(self, export_root: Path) -> None:
        result = _generate(export_root, include_dwg=True, floors=2)
        with zipfile.ZipFile(result.zip_path) as archive:
            dwgs = [name for name in archive.namelist() if name.endswith(".dwg")]
            assert len(dwgs) == 3 and all("实验性" in name for name in dwgs)
            assert all(archive.read(name).startswith(b"AC1015") for name in dwgs)


def test_worst_case_generation_is_fast_enough_to_stay_synchronous(export_root: Path) -> None:
    """执行 prompt Step 6：实测超过 5 s 才改后台任务。这里给 CI 留出余量（本机 ~0.7 s）。"""

    result = _generate(
        export_root,
        floors=33,
        units_per_floor=8,
        riser_position="管道井",
        main_pipe={"dn": "DN100", "material": "钢塑复合"},
        branch_pipe={"dn": "DN32", "material": "钢塑复合"},
        flow_rate_lps=6.0,
    )
    assert result.timings_ms["total"] < 10_000
    assert result.sheets[1].readback.planned_segments == 1 + 33 + 33 + 33 * 8


def test_service_imports_no_model_or_http_client() -> None:
    source = (REPO_ROOT / "src" / "water_agent" / "drawing" / "generation_service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    roots = {name.split(".")[0] for name in imported}
    assert not roots & {"langchain", "langchain_core", "langchain_openai", "openai", "anthropic", "httpx", "requests"}
    assert not {name for name in imported if "generation.chat_model" in name or "llm_judge" in name}
    for marker in ("build_chat_model", "ChatOpenAI", "ProviderGateway"):
        assert marker not in source
