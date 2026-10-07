"""P7-S6：导入 ``mcpServers`` 的无状态预览（``docs/design/16-p7-plugin-mcp.md`` §7 S6 A03）。

夹具 ``tests/fixtures/mcp_import/`` 是合成的 Claude Code / Cursor JSON 与 Codex TOML，``env`` 里放了
形如密钥的值——它们一个都不能出现在输出里。
"""
from __future__ import annotations

import builtins
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from water_agent.mcp.config import MCP_CONFIG_VERSION, MCPGatewayConfig
from water_agent.mcp.importer import (
    MAX_IMPORT_BYTES,
    ImportRejected,
    normalize_server_id,
    preview_import,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "mcp_import"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _env_values(text: str) -> list[str]:
    """夹具里所有 env 值（以及 http headers 里的令牌）：输出里一个都不能出现。"""
    values = re.findall(r"[a-z-]*fixture[a-z-]*secret-[0-9a-f]+", text)
    assert values, "夹具里应当有形如密钥的值"
    return values


@pytest.fixture
def no_side_effects(monkeypatch: pytest.MonkeyPatch):
    """预览期间任何文件写入、子进程都直接失败。"""

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("导入预览不得写文件或启动子进程")

    real_open = builtins.open

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError(f"导入预览不得写文件：{file}")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(os, "system", refuse)
    monkeypatch.setattr(os, "replace", refuse)
    monkeypatch.setattr(os, "open", refuse)
    monkeypatch.setattr(Path, "write_text", refuse)
    monkeypatch.setattr(Path, "write_bytes", refuse)
    monkeypatch.setattr(Path, "mkdir", refuse)
    monkeypatch.setattr(builtins, "open", guarded_open)


def test_import_produces_disabled_proposals_only(no_side_effects: None) -> None:
    """P7-S6-A03 Claude Code / Cursor JSON 与 Codex TOML → 覆盖层格式提案：每台 disabled: true、不写 allow、
    env 键变成 credential_env；url / 非 stdio、含 / 的相对 command、归一不出 id 的名字逐条给原因；
    与现有 server_id 冲突改名并标出；超过 64 KiB 拒绝；全程不写文件、不启动子进程。"""

    claude = preview_import(_fixture("claude_code.mcp.json"), existing_ids={"cad"})
    assert claude.source_format == "mcpServers_json"
    by_name = {entry.name: entry for entry in claude.entries}
    assert {name: entry.status for name, entry in by_name.items()} == {
        "autocad-mcp": "proposed",
        "Project Library": "proposed",
        "remote-search": "unsupported",
        "relative": "invalid",
        "123": "invalid",
    }
    assert by_name["autocad-mcp"].server_id == "autocad_mcp"
    assert by_name["autocad-mcp"].credential_env == ("AUTOCAD_API_TOKEN",)
    assert any("NODE_OPTIONS" in note for note in by_name["autocad-mcp"].notes), "危险变量名被去掉并说明"
    assert by_name["Project Library"].server_id == "project_library"
    assert any("S8" in reason for reason in by_name["remote-search"].reasons)
    assert any("command" in reason for reason in by_name["relative"].reasons)
    assert by_name["123"].server_id is None and by_name["123"].reasons

    proposal = json.loads(claude.proposal_text)
    assert proposal["version"] == MCP_CONFIG_VERSION
    assert [server["server_id"] for server in proposal["servers"]] == ["autocad_mcp", "project_library"]
    for server in proposal["servers"]:
        assert server["disabled"] is True
        assert "allow" not in server, "不写 allow 即全拒"
        assert "env" not in server and "env_file" not in server
    assert proposal["servers"][0]["credential_env"] == ["AUTOCAD_API_TOKEN"]
    # 提案能被覆盖层直接接受（模型层校验，不查文件系统）
    MCPGatewayConfig.model_validate(proposal)

    cursor = preview_import(_fixture("cursor.mcp.json"), existing_ids={"cad"})
    cad = next(entry for entry in cursor.entries if entry.name == "cad")
    assert cad.status == "proposed" and cad.server_id == "cad_2"
    assert any("冲突" in note for note in cad.notes)
    assert next(e for e in cursor.entries if e.name == "legacy-sse").status == "unsupported"

    codex = preview_import(_fixture("codex.config.toml"), existing_ids=())
    assert codex.source_format == "codex_toml"
    docs = next(entry for entry in codex.entries if entry.name == "docs_server")
    assert docs.status == "proposed" and docs.credential_env == ("DOCS_TOKEN",)
    assert any("PYTHONPATH" in note for note in docs.notes)
    assert any("startup_timeout_sec" in note for note in docs.notes), "不支持的字段只列键名"
    figma = next(entry for entry in codex.entries if entry.name == "figma")
    assert figma.status == "unsupported"
    codex_proposal = json.loads(codex.proposal_text)
    assert codex_proposal["servers"] == [
        {
            "server_id": "docs_server",
            "command": "/usr/local/bin/docs-mcp",
            "args": ["--readonly"],
            "cwd": "/srv/docs",
            "credential_env": ["DOCS_TOKEN"],
            "disabled": True,
        }
    ]

    # 同批里两条归一成同一个 id：后者改名
    twins = preview_import(json.dumps({"mcpServers": {"My-Server": {"command": "a"},
                                                      "my_server": {"command": "b"}}}))
    assert [entry.server_id for entry in twins.entries] == ["my_server", "my_server_2"]

    for bad in ("", "   ", "[1, 2]", json.dumps({"servers": []}), "not = [valid", "title = 'x'",
                json.dumps({"mcpServers": {}})):
        with pytest.raises(ImportRejected):
            preview_import(bad)
    with pytest.raises(ImportRejected) as too_big:
        preview_import(json.dumps({"mcpServers": {"x": {"command": "a" * MAX_IMPORT_BYTES}}}))
    assert "KiB" in too_big.value.reason

    assert normalize_server_id("AutoCAD MCP (local)") == "autocad_mcp_local"
    assert normalize_server_id("中文名") is None


def test_import_never_echoes_env_values(no_side_effects: None) -> None:
    """P7-S6-A03 导入文件里的 env 值（常含真实密钥）与 http headers 一个都不出现在任何输出里：提案文本、
    逐条结果、拒绝原因都没有；env 的键只以 credential_env 槽位名出现。"""

    for name, existing in (
        ("claude_code.mcp.json", {"cad"}),
        ("cursor.mcp.json", {"cad"}),
        ("codex.config.toml", set()),
    ):
        text = _fixture(name)
        preview = preview_import(text, existing_ids=existing)
        rendered = json.dumps(preview.to_dict(), ensure_ascii=False)
        for secret in _env_values(text):
            assert secret not in rendered, (name, secret)
            assert secret not in preview.proposal_text, (name, secret)
        assert "Bearer" not in rendered and "/tmp/fixture-hook.js" not in rendered
        assert "/tmp/evil" not in rendered

    # 模型校验失败的条目，原因里同样没有值
    text = json.dumps({"mcpServers": {"broken": {"command": "./rel", "env": {"BROKEN_TOKEN": "zz-secret-value-123"}}}})
    rendered = json.dumps(preview_import(text).to_dict(), ensure_ascii=False)
    assert "zz-secret-value-123" not in rendered
