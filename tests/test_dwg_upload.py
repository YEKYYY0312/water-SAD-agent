"""P5-S51：DWG 上传 → LibreDWG 转 DXF → S50 审查链。

守住：
- 转换是**入库侧受控子进程**：argv 固定、无 shell、用户文件名不进命令行、环境变量
  白名单、超时有界；模型侧能力不变（EXECUTE_SHELL 仍一律拒绝）；
- 转换器缺失、转换失败、超时 → 结构化 400，不入库；
- 转换成功 → 与 .dxf 同一份审查报告 + 转换来源说明；
- subprocess 只出现在登记过的模块里（静态断言）。

需要本机或镜像装有 dwg2dxf 的用例会在缺失时 skip，并写明原因。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from water_agent.documents import dwg_convert
from water_agent.documents.dwg_convert import check_dwg_magic, convert_dwg, dwg_converter_available
from water_agent.documents.errors import UploadError
from water_agent.documents.extractors import extract_text

FIXTURE = Path(__file__).parent / "fixtures" / "dwg" / "synthetic-P-01.dwg"
needs_converter = pytest.mark.skipif(
    not dwg_converter_available(), reason="dwg2dxf (LibreDWG) not installed on this host"
)


def test_non_dwg_bytes_are_rejected_before_any_process_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[object] = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: started.append(a))
    for body in (b"hello", b"  0\nSECTION\n", b"AC10" + b"\x00" * 10):
        with pytest.raises(UploadError) as info:
            convert_dwg(body)
        assert info.value.code == "UPLOAD_MAGIC_MISMATCH"
    assert started == []


def test_missing_converter_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dwg_convert, "converter_path", lambda: None)
    with pytest.raises(UploadError) as info:
        convert_dwg(FIXTURE.read_bytes())
    assert info.value.code == "UPLOAD_EXTRACTION_FAILED"
    assert "另存为 DXF" in str(info.value)


def test_subprocess_is_invoked_with_a_fixed_argv_and_an_env_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, b"dwg2dxf 0.14\n", b"")
        Path(argv[3]).write_bytes(b"  0\nSECTION\n  0\nEOF\n")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(dwg_convert, "converter_path", lambda: "/opt/libredwg/bin/dwg2dxf")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-must-not-leak")
    result = convert_dwg(FIXTURE.read_bytes())
    convert = calls[0]
    assert convert["argv"][:3] == ["/opt/libredwg/bin/dwg2dxf", "-y", "-o"]
    assert [Path(a).name for a in convert["argv"][3:]] == ["out.dxf", "in.dwg"], "用户文件名不得进入命令行"
    assert convert.get("shell") in (None, False)
    assert convert["timeout"] == dwg_convert.CONVERT_TIMEOUT_SECONDS
    assert "DEEPSEEK_API_KEY" not in convert["env"], "密钥不得传进转换子进程"
    assert set(convert["env"]) <= set(dwg_convert._ENV_ALLOWLIST)
    assert result.version == "0.14"
    assert not Path(convert["argv"][4]).parent.exists(), "临时目录用完即删"


def test_timeout_and_nonzero_exit_become_structured_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dwg_convert, "converter_path", lambda: "/opt/libredwg/bin/dwg2dxf")

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(UploadError, match="超时"):
        convert_dwg(FIXTURE.read_bytes())

    monkeypatch.setattr(
        subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 1, b"", b"ERROR: Invalid section\n")
    )
    with pytest.raises(UploadError) as info:
        convert_dwg(FIXTURE.read_bytes())
    assert info.value.code == "UPLOAD_EXTRACTION_FAILED"
    assert "Invalid section" in str(info.value)


@needs_converter
def test_real_conversion_feeds_the_same_review_chain() -> None:
    extracted = extract_text(FIXTURE.read_bytes(), ".dwg", display_name="给水平面图.dwg")
    assert extracted.engine.startswith("libredwg-")
    assert extracted.text.startswith("【DXF 程序化审查报告】已审查管段 ")
    assert "本图由 DWG 经 LibreDWG" in extracted.text
    assert "== 图纸审查报告：给水平面图.dwg ==" in extracted.text
    assert "【图面信息摘要】" in extracted.text


@needs_converter
def test_truncated_dwg_fails_without_leaving_files(tmp_path: Path) -> None:
    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("wa-dwg-*"))
    with pytest.raises(UploadError):
        convert_dwg(FIXTURE.read_bytes()[:400])
    assert set(Path(tempfile.gettempdir()).glob("wa-dwg-*")) == before


def test_zero_handle_repair_removes_only_the_invalid_pair() -> None:
    from water_agent.documents.dwg_convert import _repair_zero_handles

    dxf = b"  0\r\nENDBLK\r\n  5\r\n0\r\n330\r\n1F\r\n  0\r\nLINE\r\n  5\r\n20\r\n 10\r\n0\r\n"
    fixed, count = _repair_zero_handles(dxf)
    assert count == 1
    assert fixed == b"  0\r\nENDBLK\r\n330\r\n1F\r\n  0\r\nLINE\r\n  5\r\n20\r\n 10\r\n0\r\n", "坐标 10/0 与非零 handle 不得被动"


def test_dwg_magic_accepts_known_versions() -> None:
    check_dwg_magic(FIXTURE.read_bytes())


def test_subprocess_only_in_registered_modules() -> None:
    """新增子进程调用点必须显式登记（与出站网络扫描同一思路）。"""

    root = Path(__file__).resolve().parents[1] / "src" / "water_agent"
    registered = {
        "documents/dwg_convert.py",  # P5-S51 入库侧 DWG 转换
        "documents/pdf_parser.py",  # 安全修复：无凭据、有限资源的 PDF 文本解析。
        "mcp/client.py",  # P4 MCP stdio 客户端
        "webapp/smoke.py",  # 冒烟容器内的 CLI 探针
        "harness/memory_eval.py",  # 评测 harness
        "runtime/governance/rules.py",  # 规则文本（非调用）
    }
    pattern = re.compile(r"\bsubprocess\.(?:run|Popen|call|check_call|check_output)\s*\(")
    found = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts and pattern.search(p.read_text(encoding="utf-8"))
    }
    assert found <= registered, f"未登记的子进程调用点：{sorted(found - registered)}"


def test_model_side_capabilities_are_unchanged() -> None:
    from water_agent.runtime.governance.contracts import Capability, Principal

    with pytest.raises(ValueError):
        Principal.local("t", capabilities=frozenset({Capability.EXECUTE_SHELL}))


@pytest.mark.parametrize("available", [True, False])
def test_capability_note_follows_the_converter(monkeypatch: pytest.MonkeyPatch, available: bool) -> None:
    from water_agent.documents.attachments import AttachmentContext
    from water_agent.prompting import assembler
    from water_agent.prompting.assembler import PromptAssembler
    from water_agent.tools.registry import ToolRegistry

    monkeypatch.setattr(assembler, "dwg_converter_available", lambda: available)
    content = PromptAssembler(tool_registry=ToolRegistry()).assemble_prompt(
        {"messages": [], "attachment_context": AttachmentContext()}
    ).content
    if available:
        assert "DWG 也可上传" in content and "LibreDWG" in content
    else:
        assert "DWG 暂不支持（服务器未安装转换器）" in content
