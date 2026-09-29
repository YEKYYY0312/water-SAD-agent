"""P6-S1 Step 4：规划 → DXF 渲染、读回核对与实验性 DWG 输出。

守住：
- R2010（AC1024）、``$INSUNITS=4``、``$DWGCODEPAGE=ANSI_936``；八个 ``WS-*`` 图层（轴网 CENTER）；
  文字样式 ``WS-FS`` = simfang.ttf + gbcbig.shx + 宽度 0.7 + 字体族 仿宋；块名 ``WS_`` 前缀、块内不用 0 层；
- 生成的 DXF 能被 ``ezdxf`` 严格读取（不走 recover）与本仓库 ``read_drawing`` 直接打开，
  **每条管段的 dn_mm 都能被 map_pipes 按 handle 匹配到**，内容护栏零命中；
- 渲染只做翻译：标注插入点即规划锚点；同一规划两次渲染的模型空间图元逐一相同；
- 生成器不 import 模型或 HTTP 客户端（AST）；
- DWG：受控子进程（固定 argv、无 shell、临时目录固定文件名、超时、环境变量白名单），
  回读核对不过就丢弃；本机装有 LibreDWG 时跑真实往返。
"""

from __future__ import annotations

import ast
import io
import subprocess
from pathlib import Path

import ezdxf
import pytest

from water_agent.documents import dwg_convert
from water_agent.documents.dwg_convert import (
    DwgWriteError,
    convert_dxf_to_dwg,
    dwg_roundtrip_available,
    dxf2dwg_path,
)
from water_agent.drawing import generator
from water_agent.drawing.blocks import BLOCK_LENGTH_MM, BLOCK_NAMES
from water_agent.drawing.building_rules import BuildingType, load_building_rules
from water_agent.drawing.detail_drawings import plan_details
from water_agent.drawing.generation_models import (
    PIPE_LAYERS,
    STANDARD_LAYERS,
    SheetPlan,
    parse_generation_request,
)
from water_agent.drawing.generator import (
    BIGFONT_FILE,
    FONT_FAMILY,
    FONT_FILE,
    TEXT_STYLE,
    export_dwg,
    read_back,
    render,
    render_sheet,
    verify_readback,
)
from water_agent.drawing.layout_engine import DISCLAIMER, SYMBOL_LENGTH_MM, WIDTH_FACTOR, plan_floor
from water_agent.drawing.mapping import map_pipes
from water_agent.drawing.system_schematic import plan_schematic

REPO_ROOT = Path(__file__).resolve().parents[1]
DRAWING_DIR = REPO_ROOT / "src" / "water_agent" / "drawing"
needs_libredwg = pytest.mark.skipif(
    not dwg_roundtrip_available(), reason="LibreDWG (dxf2dwg + dwg2dxf) not installed on this host"
)


def _request(building_type: BuildingType, **overrides):
    payload = {
        "building_type": building_type.value,
        "floors": 4,
        "units_per_floor": 3,
        "main_pipe": {"dn": "DN65", "material": "PPR"},
        "branch_pipe": {"dn": "DN25", "material": "PPR"},
        "flow_rate_lps": 2.5,
        "pressure_mpa": 0.35,
        **overrides,
    }
    return parse_generation_request(payload)


def _plans(building_type: BuildingType, **overrides) -> list[SheetPlan]:
    rules = load_building_rules()
    request = _request(building_type, **overrides)
    return [plan_floor(request, rules), plan_schematic(request, rules), *plan_details(request, rules)]


ALL_PLANS = [plan for building_type in BuildingType for plan in _plans(building_type)]


def _ids(plan: SheetPlan) -> str:
    return f"{plan.sheet_id}-{len(plan.pipes)}runs"


def _strict_read(data: bytes):
    """严格读取（ezdxf.read，不是 recover.read）：解析不了就抛异常。"""

    return ezdxf.read(io.StringIO(data.decode("utf-8")))


@pytest.mark.parametrize("plan", ALL_PLANS, ids=_ids)
class TestDocumentSetup:
    def test_format_units_and_codepage(self, plan: SheetPlan) -> None:
        doc = _strict_read(render(plan))
        assert doc.dxfversion == "AC1024"
        assert doc.header["$INSUNITS"] == 4
        assert doc.header["$MEASUREMENT"] == 1
        assert doc.header["$DWGCODEPAGE"] == "ANSI_936"
        assert doc.header["$LTSCALE"] == pytest.approx(plan.scale * generator.LTSCALE_FACTOR)

    def test_layers_and_center_linetype(self, plan: SheetPlan) -> None:
        doc = _strict_read(render(plan))
        for name in STANDARD_LAYERS:
            assert name in doc.layers, name
        assert doc.layers.get("WS-AXIS").dxf.linetype == "CENTER"
        assert "CENTER" in doc.linetypes and "DASHED" in doc.linetypes

    def test_text_style_names_font_and_bigfont(self, plan: SheetPlan) -> None:
        doc = _strict_read(render(plan))
        style = doc.styles.get(TEXT_STYLE)
        assert style.dxf.font == FONT_FILE == "simfang.ttf"
        assert style.dxf.bigfont == BIGFONT_FILE == "gbcbig.shx"
        assert style.dxf.width == pytest.approx(WIDTH_FACTOR)
        assert style.get_extended_font_data()[0] == FONT_FAMILY
        texts = [e for e in doc.modelspace() if e.dxftype() in ("TEXT", "MTEXT")]
        assert texts and all(e.dxf.style == TEXT_STYLE for e in texts)

    def test_blocks_are_prefixed_and_avoid_layer_zero(self, plan: SheetPlan) -> None:
        doc = _strict_read(render(plan))
        for name in BLOCK_NAMES.values():
            assert name.startswith("WS_")
            block = doc.blocks.get(name)
            assert block is not None, name
            assert all(entity.dxf.layer != "0" for entity in block), f"{name} 有 0 层图元，展开后系统归属未知"
        inserts = [e for e in doc.modelspace() if e.dxftype() == "INSERT"]
        assert inserts and all(e.dxf.name.startswith("WS_") for e in inserts)

    def test_disclaimer_is_on_every_sheet(self, plan: SheetPlan) -> None:
        extraction = read_back(render(plan))
        joined = "".join("".join(e.text for e in extraction.entities).split())
        assert "".join(DISCLAIMER.split()) in joined


@pytest.mark.parametrize("plan", ALL_PLANS, ids=_ids)
def test_read_drawing_reads_back_every_pipe_with_its_diameter(plan: SheetPlan) -> None:
    """执行 prompt §3：生成图读回后，每条管段的 dn_mm 都能被 map_pipes 匹配到。"""

    sheet = render_sheet(plan)
    extraction = read_back(sheet.data)
    assert extraction.ok, extraction.errors
    assert not extraction.truncated
    assert extraction.unit_known and not extraction.unit_suspicious
    assert not [e.handle for e in extraction.entities if e.suspicious], "生成图的文字不该命中内容护栏"
    mapped = {pipe.handle: pipe for pipe in map_pipes(extraction).pipes}
    for run in plan.pipes:
        pipe = mapped[sheet.pipe_handles[run.run_id]]
        assert pipe.dn_mm == run.dn.mm, run.run_id
        assert pipe.system == "water_supply", run.run_id
        assert pipe.layer in PIPE_LAYERS
    report = verify_readback(sheet, plan)
    assert report.verified
    assert report.pipe_segments == report.planned_segments == report.dn_matched == len(plan.pipes)


@pytest.mark.parametrize("plan", ALL_PLANS[:3], ids=_ids)
def test_rendering_only_translates(plan: SheetPlan) -> None:
    doc = _strict_read(render(plan))
    labels = {(label.text, label.anchor) for label in plan.labels}
    texts = [e for e in doc.modelspace() if e.dxftype() == "TEXT" and e.dxf.text.startswith("DN")]
    rendered = {(e.dxf.text, (round(e.dxf.insert[0], 3), round(e.dxf.insert[1], 3))) for e in texts}
    assert labels <= rendered, "管径标注的插入点必须就是规划锚点"
    polylines = [e for e in doc.modelspace() if e.dxftype() == "LWPOLYLINE" and e.dxf.layer in PIPE_LAYERS]
    assert len(polylines) == len(plan.pipes)


@pytest.mark.parametrize("plan", ALL_PLANS[:3], ids=_ids)
def test_two_renders_have_identical_modelspace(plan: SheetPlan) -> None:
    """头部的创建/更新时间与 GUID 每次不同（ezdxf 写入）；模型空间图元必须逐一相同。"""

    def dump(data: bytes) -> list[tuple]:
        doc = _strict_read(data)
        rows = []
        for entity in doc.modelspace():
            attribs = entity.dxfattribs()
            attribs.pop("owner", None)
            rows.append((entity.dxftype(), sorted((k, str(v)) for k, v in attribs.items())))
        return rows

    assert dump(render(plan)) == dump(render(plan))


def test_symbol_lengths_agree_between_blocks_and_planner() -> None:
    assert BLOCK_LENGTH_MM == SYMBOL_LENGTH_MM
    doc = _strict_read(render(ALL_PLANS[0]))
    bowtie = next(iter(doc.blocks.get(BLOCK_NAMES[next(iter(BLOCK_LENGTH_MM))])))
    xs = [point[0] for point in bowtie.get_points("xy")]
    assert max(xs) - min(xs) == pytest.approx(4.0)


def test_out_of_band_velocity_note_reaches_the_dxf() -> None:
    rules = load_building_rules()
    request = _request(BuildingType.RESIDENTIAL, main_pipe={"dn": "DN50", "material": "PPR"})
    extraction = read_back(render(plan_schematic(request, rules)))
    assert any(e.text == "主管流速 1.27 m/s，超出建议区间，需复核" for e in extraction.entities)


def test_r2000_source_writes_notes_as_single_line_text() -> None:
    plan = ALL_PLANS[2]
    data = render_sheet(plan, dxf_version="R2000").data
    doc = ezdxf.read(io.StringIO(data.decode("gbk")))
    assert doc.dxfversion == "AC1015"
    assert not [e for e in doc.modelspace() if e.dxftype() == "MTEXT"]
    lines = {e.dxf.text for e in doc.modelspace() if e.dxftype() == "TEXT"}
    assert all(line in lines for note in plan.notes for line in note.lines)


def test_generator_modules_import_no_model_or_http_client() -> None:
    """铁律①：生成器不 import 任何模型或 HTTP 客户端（写法同 memory_metrics 的检测器断言）。"""

    forbidden = {"langchain", "langchain_core", "langchain_openai", "langgraph", "openai", "anthropic", "httpx", "requests", "urllib", "http"}
    for name in ("generator.py", "blocks.py"):
        source = (DRAWING_DIR / name).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not imported & forbidden, name
        for marker in ("build_chat_model", "ChatOpenAI", "llm_judge", "generation.chat_model", "ProviderGateway"):
            assert marker not in source, name


# ---------------------------------------------------------------------------
# 实验性 DWG
# ---------------------------------------------------------------------------


class TestDxfToDwgSubprocess:
    def test_fixed_argv_env_allowlist_and_cleanup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict] = []

        def fake_run(argv, **kwargs):
            calls.append({"argv": list(argv), **kwargs})
            Path(argv[3]).write_bytes(b"AC1015" + b"\0" * 200)
            return subprocess.CompletedProcess(argv, 0, b"", b"Warning: Unknown DXF code 70 for MATERIAL\n")

        monkeypatch.setattr(dwg_convert, "dxf2dwg_path", lambda: "/opt/libredwg/bin/dxf2dwg")
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-must-not-leak")
        result = convert_dxf_to_dwg(b"  0\nSECTION\n  0\nEOF\n")
        (call,) = calls
        assert call["argv"][:3] == ["/opt/libredwg/bin/dxf2dwg", "-y", "-o"]
        assert [Path(a).name for a in call["argv"][3:]] == ["out.dwg", "in.dxf"]
        assert call.get("shell") in (None, False)
        assert call["timeout"] == dwg_convert.CONVERT_TIMEOUT_SECONDS
        assert "DEEPSEEK_API_KEY" not in call["env"]
        assert set(call["env"]) <= set(dwg_convert._ENV_ALLOWLIST)
        assert not Path(call["argv"][4]).parent.exists(), "临时目录用完即删"
        assert result.dwg.startswith(b"AC1015") and result.warnings == 1

    def test_missing_writer_timeout_and_failure_are_structured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dwg_convert, "dxf2dwg_path", lambda: None)
        with pytest.raises(DwgWriteError, match="dxf2dwg"):
            convert_dxf_to_dwg(b"x")
        monkeypatch.setattr(dwg_convert, "dxf2dwg_path", lambda: "/opt/libredwg/bin/dxf2dwg")

        def timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        monkeypatch.setattr(subprocess, "run", timeout)
        with pytest.raises(DwgWriteError, match="超时"):
            convert_dxf_to_dwg(b"x")
        monkeypatch.setattr(
            subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 1, b"", b"ERROR: bad DXF\n")
        )
        with pytest.raises(DwgWriteError, match="bad DXF"):
            convert_dxf_to_dwg(b"x")

    def test_writer_resolution_prefers_explicit_env_then_the_reader_sibling(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        reader, writer = tmp_path / "dwg2dxf", tmp_path / "dxf2dwg"
        for exe in (reader, writer):
            exe.write_text("#!/bin/sh\n", encoding="utf-8")
            exe.chmod(0o755)
        monkeypatch.setenv(dwg_convert.CONVERTER_ENV, str(reader))
        monkeypatch.delenv(dwg_convert.WRITER_ENV, raising=False)
        assert dxf2dwg_path() == str(writer)
        monkeypatch.setenv(dwg_convert.WRITER_ENV, str(tmp_path / "missing"))
        assert dxf2dwg_path() is None, "显式配置了却不可执行 → 不可用，不退回别处"


class TestExportDwg:
    def test_unavailable_converter_means_dxf_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dwg_convert, "dwg_roundtrip_available", lambda: False)
        result = export_dwg(ALL_PLANS[2])
        assert result.status == "unavailable" and result.data is None
        assert "只交付 DXF" in result.reason

    def test_readback_mismatch_discards_the_dwg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plan = ALL_PLANS[2]
        broken = generator.render_sheet(plan.model_copy(update={"pipes": plan.pipes[1:], "labels": plan.labels[1:]}))
        monkeypatch.setattr(dwg_convert, "dwg_roundtrip_available", lambda: True)
        monkeypatch.setattr(
            dwg_convert, "convert_dxf_to_dwg", lambda data: dwg_convert.DxfToDwg(dwg=b"AC1015", converter="x", warnings=0)
        )
        monkeypatch.setattr(
            dwg_convert,
            "convert_dwg",
            lambda data: dwg_convert.DwgConversion(dxf=broken.data, converter="x", version="0.14", warnings=0),
        )
        result = export_dwg(plan)
        assert result.status == "discarded" and result.data is None
        assert "管段数" in result.reason and "只交付 DXF" in result.reason

    @needs_libredwg
    @pytest.mark.parametrize("building_type", list(BuildingType))
    def test_real_roundtrip_keeps_pipes_diameters_and_chinese_text(self, building_type: BuildingType) -> None:
        for plan in _plans(building_type, floors=2, units_per_floor=2):
            result = export_dwg(plan)
            assert result.status == "ok", result.reason
            assert result.data is not None and result.data.startswith(b"AC1015")
            assert result.readback_pipe_segments == len(plan.pipes)
            assert "实验性" in result.reason and "未在 AutoCAD" in result.reason
