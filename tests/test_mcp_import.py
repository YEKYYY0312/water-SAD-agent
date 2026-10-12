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
    env 键变成 credential_env；旧 HTTP+SSE、含 / 的相对 command、归一不出 id 的名字逐条给原因；
    （P7-S8 起 url / type: http 条目生成 streamable_http 禁用提案，见 test_http_entries_become_disabled_proposals）；
    与现有 server_id 冲突改名并标出；超过 64 KiB 拒绝；全程不写文件、不启动子进程。"""

    claude = preview_import(_fixture("claude_code.mcp.json"), existing_ids={"cad"})
    assert claude.source_format == "mcpServers_json"
    by_name = {entry.name: entry for entry in claude.entries}
    assert {name: entry.status for name, entry in by_name.items()} == {
        "autocad-mcp": "proposed",
        "Project Library": "proposed",
        "remote-search": "proposed",
        "relative": "invalid",
        "123": "invalid",
    }
    assert by_name["autocad-mcp"].server_id == "autocad_mcp"
    assert by_name["autocad-mcp"].credential_env == ("AUTOCAD_API_TOKEN",)
    assert any("NODE_OPTIONS" in note for note in by_name["autocad-mcp"].notes), "危险变量名被去掉并说明"
    assert by_name["Project Library"].server_id == "project_library"
    assert by_name["remote-search"].server_id == "remote_search"
    assert any("command" in reason for reason in by_name["relative"].reasons)
    assert by_name["123"].server_id is None and by_name["123"].reasons

    proposal = json.loads(claude.proposal_text)
    assert proposal["version"] == MCP_CONFIG_VERSION
    assert [server["server_id"] for server in proposal["servers"]] == [
        "autocad_mcp", "project_library", "remote_search",
    ]
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
    assert next(e for e in cursor.entries if e.name == "real-sse").status == "unsupported", "type: sse（旧 HTTP+SSE）仍不支持"

    codex = preview_import(_fixture("codex.config.toml"), existing_ids=())
    assert codex.source_format == "codex_toml"
    docs = next(entry for entry in codex.entries if entry.name == "docs_server")
    assert docs.status == "proposed" and docs.credential_env == ("DOCS_TOKEN",)
    assert any("PYTHONPATH" in note for note in docs.notes)
    assert any("startup_timeout_sec" in note for note in docs.notes), "不支持的字段只列键名"
    figma = next(entry for entry in codex.entries if entry.name == "figma")
    assert figma.status == "proposed"
    codex_proposal = json.loads(codex.proposal_text)
    assert codex_proposal["servers"] == [
        {
            "server_id": "docs_server",
            "command": "/usr/local/bin/docs-mcp",
            "args": ["--readonly"],
            "cwd": "/srv/docs",
            "credential_env": ["DOCS_TOKEN"],
            "disabled": True,
        },
        {
            "server_id": "figma",
            "transport": "streamable_http",
            "url": "https://figma.example.invalid/mcp",
            "disabled": True,
        },
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


def test_http_entries_become_disabled_proposals(no_side_effects: None) -> None:
    """P7-S8-A10：url / type: http 条目生成 ``transport: streamable_http`` 的禁用提案（不写 allow）；请求头与令牌的值
    一律丢弃、只在备注里列名字，形似 Authorization 的提示改用 bearer_credential 槽位或 oauth；URL 的查询串丢弃；
    旧 HTTP+SSE（type: sse）与其他传输仍不支持；不合格的 URL（明文 http、userinfo）逐条给原因。"""

    payload = {
        "mcpServers": {
            "Remote Search": {
                "type": "http",
                "url": "https://mcp.example.invalid/search?api_key=query-fixture-secret-1&tenant=7",
                "headers": {"Authorization": "Bearer header-fixture-secret-2", "X-Tenant": "t-7"},
                "env": {"IGNORED": "x"},
            },
            "no-type": {"url": "https://mcp.example.invalid/mcp"},
            "old-sse": {"type": "sse", "url": "https://mcp.example.invalid/sse"},
            "socket": {"type": "websocket", "url": "wss://mcp.example.invalid/ws"},
            "plain": {"type": "http", "url": "http://mcp.example.invalid/mcp"},
            "userinfo": {"type": "http", "url": "https://u:p@mcp.example.invalid/mcp"},
            "no-url": {"type": "http"},
        }
    }
    preview = preview_import(json.dumps(payload), existing_ids={"remote_search"})
    by_name = {entry.name: entry for entry in preview.entries}
    assert {name: entry.status for name, entry in by_name.items()} == {
        "Remote Search": "proposed", "no-type": "proposed", "old-sse": "unsupported", "socket": "unsupported",
        "plain": "invalid", "userinfo": "invalid", "no-url": "invalid",
    }
    remote = by_name["Remote Search"]
    assert remote.server_id == "remote_search_2", "与现有 id 冲突改名"
    joined = " ".join(remote.notes)
    assert "Authorization" in joined and "X-Tenant" in joined and "bearer_credential" in joined and "oauth" in joined
    assert "api_key" in joined and "tenant" in joined and "查询串" in joined
    assert any("没有声明传输类型" in note for note in by_name["no-type"].notes)
    assert any("HTTP+SSE" in reason for reason in by_name["old-sse"].reasons)
    assert any("allow_private_network" in reason for reason in by_name["plain"].reasons)
    assert any("userinfo" in reason for reason in by_name["userinfo"].reasons)

    proposal = json.loads(preview.proposal_text)
    assert proposal["servers"] == [
        {"server_id": "remote_search_2", "transport": "streamable_http",
         "url": "https://mcp.example.invalid/search", "disabled": True},
        {"server_id": "no_type", "transport": "streamable_http", "url": "https://mcp.example.invalid/mcp",
         "disabled": True},
    ]
    for secret in ("query-fixture-secret-1", "header-fixture-secret-2", "Bearer ", "t-7"):
        assert secret not in preview.proposal_text
        assert not any(secret in note for entry in preview.entries for note in (*entry.notes, *entry.reasons))
    gateway = MCPGatewayConfig.model_validate(proposal)
    assert all(server.disabled and server.allow is None for server in gateway.servers)
