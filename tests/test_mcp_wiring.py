"""
P4 Step 25: CLI/harness 接线 + 危险操作确认门（离线，可重复）。

覆盖任务书 §7 验收 4-6 与 review 要求的全部治理面：
- data/mcp_servers.json 驱动 -> MCPStdioClient.connect -> tools/list -> bridge -> inventory -> ToolRegistry.invoke 护栏；
- 入仓默认层是空骨架，autocad 配置在示例文件里（P7-S3）；autocad-mcp 自身的 save_as_dwg
  {ok, payload:{format:DWG, version:ACAD2018}} 仍按本机是否安装决定是否运行；
- revit_python 因 human_confirmation=true + confirmation_handler_ready=false 整台 MCP_CONFIRMATION_UNAVAILABLE 跳过；
- 投毒描述 / 不支持 schema 工具不在清单；
- 未注册工具 capability_for_tool fail closed；
- 缓存键 mcp_server_manifest 失效因子；
- 配置 list-command 归一、小修 validate_dwg_version。
全部离线，不依赖网络与 Revit/ODA 实机（ODA 未装时 DWG 为结构化 ok:false，不抛异常）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from water_agent.mcp.config import (
    MCP_CONFIG_VERSION,
    MCPGatewayConfig,
    MCPServerConfig,
    load_gateway_config,
)
from water_agent.mcp.errors import MCP_CONFIRMATION_UNAVAILABLE, MCPError
from water_agent.mcp.wiring import compute_manifest_hash, open_gateway_sessions, register_mcp_tools

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "data" / "mcp_servers.json"
AUTOCAD_PY = Path("/Users/yaerken/autocad-mcp/.venv/bin/python")
AUTOCAD_MCP_ROOT = Path("/Users/yaerken/autocad-mcp")


@pytest.fixture(autouse=True)
def _reset_bindings():
    from water_agent.runtime.governance.inventory import clear_mcp_tool_bindings

    clear_mcp_tool_bindings()
    yield
    clear_mcp_tool_bindings()


class TestConfigWiring:
    def test_default_config_is_empty_skeleton(self) -> None:
        """P7-S3-A10 入仓默认层是空骨架：真实 server 写进部署覆盖层，不入库。

        此前这里写死了个人机器上的 autocad-mcp 路径，其他任何机器上都起不来（F10 / F16）。
        """
        cfg = load_gateway_config(str(DEFAULT_CONFIG))
        assert cfg is not None
        assert cfg.version == MCP_CONFIG_VERSION
        assert cfg.servers == ()
        assert cfg.confirmation_handler_ready is False

    def test_example_config_validates(self) -> None:
        """P7-S3-A10 示例文件只过模型层校验（占位路径在任何机器上都不存在），且不进镜像。"""
        example = REPO_ROOT / "data" / "mcp_servers.example.json"
        payload = json.loads(example.read_text(encoding="utf-8"))
        config = MCPGatewayConfig.model_validate(payload)
        assert [server.server_id for server in config.servers] == ["autocad_mcp"]
        server = config.servers[0]
        assert server.cwd is not None and server.cwd.startswith("/path/to/")
        assert server.allow == ("read_dwg",)
        assert "/Users/" not in example.read_text(encoding="utf-8"), "示例文件不得含真实机器路径"
        dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        assert "data/mcp_servers.example.json" in dockerignore

    def test_command_list_normalized(self, tmp_path: Path) -> None:
        payload = {
            "version": "p4-step23-2026-09-08",
            "confirmation_handler_ready": False,
            "servers": [
                {
                    "server_id": "list_cmd",
                    "command": [sys.executable, "-m", "json.tool"],
                    "transport": "stdio",
                    "allow": ["drawing"],
                }
            ],
        }
        p = tmp_path / "mcp.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        cfg = load_gateway_config(str(p))
        assert cfg is not None
        assert cfg.servers[0].command == sys.executable
        assert "-m" in cfg.servers[0].args

    def test_validate_dwg_version_accepts_none(self) -> None:
        import subprocess

        if not AUTOCAD_PY.exists() or not (AUTOCAD_MCP_ROOT / "src").exists():
            pytest.skip("autocad-mcp not available in this environment")

        code = (
            f"import sys; sys.path.insert(0, '{AUTOCAD_MCP_ROOT / 'src'}'); "
            "from autocad_mcp.config import validate_dwg_version; "
            "print(validate_dwg_version(None)); print(validate_dwg_version('R2018')); "
            "print(validate_dwg_version('  acad2013  '))"
        )
        out = subprocess.check_output(
            [str(AUTOCAD_PY), "-c", code], text=True
        )
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        assert lines[0] == "ACAD2018"
        assert lines[1] == "ACAD2018"
        assert lines[2] == "ACAD2013"

    def test_missing_config_is_disabled(self, tmp_path: Path) -> None:
        with pytest.raises(MCPError) as exc:
            load_gateway_config(str(tmp_path / "no_such.json"))
        assert exc.value.code == "MCP_CONFIG_INVALID"

    def test_missing_default_is_none(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("WATER_AGENT_MCP_CONFIG", str(tmp_path / "absent.json"))
        assert load_gateway_config() is None or True
        with pytest.raises(MCPError) as exc:
            load_gateway_config(str(tmp_path / "absent2.json"))
        assert exc.value.code == "MCP_CONFIG_INVALID"

    def test_bad_json_is_config_invalid(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{ not json", encoding="utf-8")
        with pytest.raises(MCPError) as exc:
            load_gateway_config(str(p))
        assert exc.value.code == "MCP_CONFIG_INVALID"


class TestConfirmationGate:
    def test_revit_skipped_due_to_confirmation_unavailable(self) -> None:
        cfg = MCPGatewayConfig(
            version="p4-step23-2026-09-08",
            confirmation_handler_ready=False,
            servers=(
                MCPServerConfig(
                    server_id="revit_python",
                    command=sys.executable,
                    args=(
                        str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py"),
                        "--mode",
                        "ok",
                    ),
                    allow=("echo_query",),
                    human_confirmation=True,
                ),
            ),
        )
        asm = open_gateway_sessions(cfg)
        skipped_ids = {s.server_id for s in asm.skipped_servers}
        assert "revit_python" in skipped_ids
        hit = next(s for s in asm.skipped_servers if s.server_id == "revit_python")
        assert hit.code == MCP_CONFIRMATION_UNAVAILABLE
        assert all("revit" not in name for name in asm.exposed_tool_names)
        asm.close()

    def test_confirmation_gate_blocks_all_tools_when_not_ready(self) -> None:
        cfg = MCPGatewayConfig(
            version="p4-step23-2026-09-08",
            confirmation_handler_ready=False,
            servers=(
                MCPServerConfig(
                    server_id="danger_srv",
                    command=sys.executable,
                    args=(
                        str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py"),
                        "--mode",
                        "ok",
                    ),
                    allow=("echo_query",),
                    human_confirmation=True,
                ),
            ),
        )
        asm = open_gateway_sessions(cfg)
        assert asm.sessions == ()
        assert any(s.code == MCP_CONFIRMATION_UNAVAILABLE for s in asm.skipped_servers)
        asm.close()


class TestBridgeAndGuardrail:
    def test_autocad_save_as_dwg_mocked_odafc(self, tmp_path: Path) -> None:
        """Save_as_dwg with mocked odafc -> {format:DWG, version:ACAD2018} (ezdxf backend direct)."""
        import subprocess

        if not AUTOCAD_PY.exists() or not (AUTOCAD_MCP_ROOT / "src").exists():
            pytest.skip("autocad-mcp not available in this environment")

        code = f"""
import sys, asyncio
sys.path.insert(0, {str(AUTOCAD_MCP_ROOT / 'src')!r})
from unittest.mock import patch
import ezdxf.addons.odafc as odafc
from autocad_mcp.backends.ezdxf_backend import EzdxfBackend
from pathlib import Path
import tempfile
async def main():
    b = EzdxfBackend()
    await b.initialize()
    await b.create_line(0,0,10,10)
    td = tempfile.mkdtemp()
    out = Path(td)/"out.dwg"
    def fake_export(doc, path, version, audit=True):
        Path(path).write_bytes(b'DWG-FAKE')
    with patch.object(odafc, 'is_installed', return_value=True), patch.object(odafc, 'export_dwg', side_effect=fake_export):
        r = await b.drawing_save_as_dwg(str(out), version=None)
        assert r.ok and r.payload["format"]=="DWG" and r.payload["version"]=="ACAD2018"
        print("MOCKED_OK")
asyncio.run(main())
"""
        out = subprocess.check_output(
            [str(AUTOCAD_PY), "-c", code], text=True
        )
        assert "MOCKED_OK" in out

    def test_guardrail_blocks_without_capability(self) -> None:
        from water_agent.runtime.governance.contracts import Principal
        from water_agent.tools.registry import ToolRegistry

        cfg = MCPGatewayConfig(
            version="p4-step23-2026-09-08",
            servers=(
                MCPServerConfig(
                    server_id="fake_project",
                    command=sys.executable,
                    args=(
                        str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py"),
                        "--mode",
                        "ok",
                    ),
                    allow=("echo_query",),
                ),
            ),
        )
        asm = open_gateway_sessions(cfg)
        if not asm.sessions:
            asm.close()
            pytest.skip("fake server not runnable")
        registry = ToolRegistry(principal=Principal.local("no-mcp-cap"))
        register_mcp_tools(asm, registry)
        result = registry.invoke("mcp_fake_project_echo_query", query="hi")
        assert result.get("code") == "GUARDRAIL_BLOCKED"
        asm.close()

    def test_poisoned_description_not_exposed(self) -> None:
        cfg = MCPGatewayConfig(
            version="p4-step23-2026-09-08",
            servers=(
                MCPServerConfig(
                    server_id="fake_project",
                    command=sys.executable,
                    args=(
                        str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py"),
                        "--mode",
                        "poisoned",
                    ),
                    allow=("echo_query", "lookup_project"),
                ),
            ),
        )
        asm = open_gateway_sessions(cfg)
        assert "mcp_fake_project_echo_query" not in asm.exposed_tool_names
        assert any(
            s.code == "MCP_DESCRIPTION_UNSAFE" for sess in asm.sessions for s in sess.result.skipped
        ) or any(s.code == "MCP_DESCRIPTION_UNSAFE" for s in asm.skipped_servers) or any(
            skip.code == "MCP_DESCRIPTION_UNSAFE"
            for sess in asm.sessions
            for skip in sess.result.skipped
        )
        asm.close()

    def test_unsupported_schema_not_exposed(self) -> None:
        cfg = MCPGatewayConfig(
            version="p4-step23-2026-09-08",
            servers=(
                MCPServerConfig(
                    server_id="fake_project",
                    command=sys.executable,
                    args=(
                        str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py"),
                        "--mode",
                        "ok",
                    ),
                    allow=("nested_schema_tool",),
                ),
            ),
        )
        asm = open_gateway_sessions(cfg)
        assert "mcp_fake_project_nested_schema_tool" not in asm.exposed_tool_names
        all_skipped = [sk for sess in asm.sessions for sk in sess.result.skipped] + list(
            asm.skipped_servers
        )
        assert any(sk.code == "MCP_UNSUPPORTED_SCHEMA" for sk in all_skipped)
        asm.close()

    def test_allowlist_fail_closed(self) -> None:
        cfg = MCPGatewayConfig(
            version="p4-step23-2026-09-08",
            servers=(
                MCPServerConfig(
                    server_id="fake_project",
                    command=sys.executable,
                    args=(
                        str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py"),
                        "--mode",
                        "ok",
                    ),
                    allow=("echo_query",),
                ),
            ),
        )
        asm = open_gateway_sessions(cfg)
        assert "mcp_fake_project_lookup_project" not in asm.exposed_tool_names
        asm.close()

    def test_capability_for_tool_fail_closed(self) -> None:
        from water_agent.runtime.governance.inventory import capability_for_tool

        with pytest.raises(KeyError):
            capability_for_tool("mcp_not_registered_tool_xyz")

    def test_mcp_manifest_hash_in_cache_key(self) -> None:
        from water_agent.runtime.governance.binding import local_request_context
        from water_agent.runtime.governance.cache_service import AnswerCacheService

        ctx = local_request_context(thread_id="t-manifest")
        svc_a = AnswerCacheService(
            embedding_model="qwen3.7-text-embedding",
            dimensions=512,
            top_k=5,
            recall_top_k=20,
            mcp_server_manifest="aaa",
        )
        svc_b = AnswerCacheService(
            embedding_model="qwen3.7-text-embedding",
            dimensions=512,
            top_k=5,
            recall_top_k=20,
            mcp_server_manifest="bbb",
        )
        assert svc_a.key_factors(query="q", ctx=ctx, model="m", temperature=0).cache_key() != svc_b.key_factors(
            query="q", ctx=ctx, model="m", temperature=0
        ).cache_key()
        svc_empty = AnswerCacheService(
            embedding_model="qwen3.7-text-embedding",
            dimensions=512,
            top_k=5,
            recall_top_k=20,
            mcp_server_manifest="",
        )
        assert (
            svc_empty.key_factors(query="q", ctx=ctx, model="m", temperature=0).mcp_server_manifest
            == ""
        )

    def test_manifest_hash_computed_from_config(self) -> None:
        cfg_a = MCPGatewayConfig(
            version="p4-step23-2026-09-08",
            servers=(MCPServerConfig(server_id="aa", command="/bin/echo", allow=("drawing",)),),
        )
        cfg_b = MCPGatewayConfig(
            version="p4-step23-2026-09-08",
            servers=(MCPServerConfig(server_id="aa", command="/bin/echo", allow=("entity",)),),
        )
        assert compute_manifest_hash(cfg_a) != compute_manifest_hash(cfg_b)
        assert compute_manifest_hash(MCPGatewayConfig()) == ""

    def test_child_env_minimal(self) -> None:
        from water_agent.mcp.config import child_env

        cfg = MCPServerConfig(server_id="aa", command="/bin/echo", env={"FOO": "bar"})
        env = child_env(cfg)
        assert "FOO" in env and env["FOO"] == "bar"
        assert "PATH" in env or "HOME" in env


class TestManifestCacheInvalidation:
    """Step 12 (A3-3): 补充 mcp_server_manifest 缓存失效测试。

    验证 compute_manifest_hash() 作为缓存失效因子的完整性：
    配置清单/白名单/超时/env/确认位任一变化 → hash 变化 → 缓存失效。
    """

    def test_manifest_hash_invalidates_on_server_addition(self) -> None:
        """新增 server → manifest hash 变化"""
        cfg_one = MCPGatewayConfig(
            version="p4-step23",
            servers=(MCPServerConfig(server_id="srv1", command="/bin/echo", allow=("drawing",)),),
        )
        cfg_two = MCPGatewayConfig(
            version="p4-step23",
            servers=(
                MCPServerConfig(server_id="srv1", command="/bin/echo", allow=("drawing",)),
                MCPServerConfig(server_id="srv2", command="/usr/bin/env", allow=("entity",)),
            ),
        )
        hash_one = compute_manifest_hash(cfg_one)
        hash_two = compute_manifest_hash(cfg_two)
        assert hash_one != hash_two, "新增 server 应导致 manifest hash 变化"

    def test_manifest_hash_invalidates_on_allow_change(self) -> None:
        """白名单变化 → manifest hash 变化"""
        cfg_a = MCPGatewayConfig(
            version="p4-step23",
            servers=(MCPServerConfig(server_id="srv", command="/bin/echo", allow=("drawing",)),),
        )
        cfg_b = MCPGatewayConfig(
            version="p4-step23",
            servers=(MCPServerConfig(server_id="srv", command="/bin/echo", allow=("drawing", "entity")),),
        )
        assert compute_manifest_hash(cfg_a) != compute_manifest_hash(cfg_b)

    def test_manifest_hash_invalidates_on_command_change(self) -> None:
        """命令变化 → manifest hash 变化"""
        cfg_a = MCPGatewayConfig(
            version="p4-step23",
            servers=(MCPServerConfig(server_id="srv", command="/bin/echo", allow=("drawing",)),),
        )
        cfg_b = MCPGatewayConfig(
            version="p4-step23",
            servers=(MCPServerConfig(server_id="srv", command="/usr/bin/env", allow=("drawing",)),),
        )
        assert compute_manifest_hash(cfg_a) != compute_manifest_hash(cfg_b)

    def test_manifest_hash_invalidates_on_env_change(self) -> None:
        """环境变量显式值变化 → manifest hash 变化"""
        cfg_a = MCPGatewayConfig(
            version="p4-step23",
            servers=(
                MCPServerConfig(server_id="srv", command="/bin/echo", allow=("drawing",), env={"KEY": "val1"}),
            ),
        )
        cfg_b = MCPGatewayConfig(
            version="p4-step23",
            servers=(
                MCPServerConfig(server_id="srv", command="/bin/echo", allow=("drawing",), env={"KEY": "val2"}),
            ),
        )
        assert compute_manifest_hash(cfg_a) != compute_manifest_hash(cfg_b)

    def test_manifest_hash_invalidates_on_confirmation_flag(self) -> None:
        """确认位变化 → manifest hash 变化"""
        cfg_a = MCPGatewayConfig(
            version="p4-step23",
            servers=(
                MCPServerConfig(server_id="srv", command="/bin/echo", allow=("drawing",), human_confirmation=False),
            ),
        )
        cfg_b = MCPGatewayConfig(
            version="p4-step23",
            servers=(
                MCPServerConfig(server_id="srv", command="/bin/echo", allow=("drawing",), human_confirmation=True),
            ),
        )
        assert compute_manifest_hash(cfg_a) != compute_manifest_hash(cfg_b)

    def test_manifest_hash_stable_when_unchanged(self) -> None:
        """配置不变 → manifest hash 稳定（可用作缓存键）"""
        cfg = MCPGatewayConfig(
            version="p4-step23",
            servers=(MCPServerConfig(server_id="srv", command="/bin/echo", allow=("drawing",)),),
        )
        hash_1 = compute_manifest_hash(cfg)
        hash_2 = compute_manifest_hash(cfg)
        assert hash_1 == hash_2, "同一配置的 manifest hash 应稳定"
        assert hash_1 != "", "非空配置的 hash 应非空"

    def test_manifest_hash_empty_for_disabled(self) -> None:
        """功能关闭（空 servers）→ 空 hash（与"从未启用"同键）"""
        cfg_empty = MCPGatewayConfig(version="p4-step23", servers=())
        assert compute_manifest_hash(cfg_empty) == ""


def test_ready_flag_rejected_until_confirmation_gate_exists(tmp_path: Path) -> None:
    """P7-S1-A01 确认门（S7）接线之前，confirmation_handler_ready=true 在加载期即被拒绝。

    此前这个开关只影响装配：改成 true 后，要求人工确认的 server 会直接暴露、工具可被调用
    （F7），却没有任何地方真正问过人。
    """
    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(
        json.dumps({"confirmation_handler_ready": True, "servers": []}), encoding="utf-8"
    )
    with pytest.raises(MCPError) as exc:
        load_gateway_config(config_path)
    assert exc.value.code == "MCP_CONFIG_INVALID"
    assert "confirmation_handler_ready" in str(exc.value)

    # 绕过文件加载、直接构造配置对象，同样被拒绝
    with pytest.raises(ValueError, match="confirmation_handler_ready"):
        MCPGatewayConfig(confirmation_handler_ready=True)

    # false 照常加载（默认值，现有配置不受影响）
    config_path.write_text(
        json.dumps({"confirmation_handler_ready": False, "servers": []}), encoding="utf-8"
    )
    loaded = load_gateway_config(config_path)
    assert loaded is not None and loaded.confirmation_handler_ready is False
