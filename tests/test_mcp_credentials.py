"""P7-S6：MCP server 的凭据槽位（``docs/design/16-p7-plugin-mcp.md`` §7 S6 A02）。

名字由运维在默认层 / 覆盖层声明（``credential_env``），值由管理员经 Web 扩展面板写进凭据卷，连接时
注入子进程。fake server ``--mode stateful`` 的 ``env_present`` 只回报变量是否存在、从不回报值。
"""
from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from water_agent.mcp.config import (
    MCP_CONFIG_VERSION,
    MCPGatewayConfig,
    MCPServerConfig,
    child_env,
    load_layered_gateway_config,
)
from water_agent.mcp.credentials import (
    delete_slot,
    read_slot,
    slot_path,
    slot_status,
    write_slot,
)
from water_agent.mcp.errors import MCP_CREDENTIAL_MISSING, MCPError

FAKE_SERVER = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
SLOT_VALUE = "cad-token-value-0123456789"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from water_agent.runtime.governance.inventory import clear_mcp_tool_bindings

    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    for key in ("WATER_AGENT_MCP_OVERLAY", "WATER_AGENT_MCP_WEB_LAYER", "WATER_AGENT_TOOL_EXPOSURE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("CAD_API_TOKEN", raising=False)
    clear_mcp_tool_bindings()
    yield
    clear_mcp_tool_bindings()


def _entry(server_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "server_id": server_id,
        "command": sys.executable,
        "args": [str(FAKE_SERVER), "--mode", "stateful"],
        "allow": ["whoami", "env_present"],
        "call_timeout_seconds": 3.0,
        "credential_env": ["CAD_API_TOKEN"],
    }
    payload.update(overrides)
    return payload


def _gateway(*servers: dict[str, Any]) -> MCPGatewayConfig:
    return MCPGatewayConfig.model_validate({"version": MCP_CONFIG_VERSION, "servers": list(servers)})


def _write(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _call(lease_or_assembly: Any, server_id: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    assembly = getattr(lease_or_assembly, "assembly", lease_or_assembly)
    session = next(s for s in assembly.sessions if s.config.server_id == server_id)
    return session.client.call_tool(tool, args)["structured_content"]


def _gone(pid: int) -> bool:
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_credential_slots_are_declared_by_the_operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S6-A02 槽位名只能由运维在默认层 / 覆盖层声明：大写环境变量名；拒绝 PATH / HOME 与加载器、
    解释器会读来执行代码的变量；不与内联 env 重名、不重复；Web 管理层写不了 credential_env。"""

    assert MCPServerConfig.model_validate(_entry("credok")).credential_env == ("CAD_API_TOKEN",)
    assert MCPServerConfig.model_validate(_entry("credok", credential_env=[])).credential_env == ()

    for bad in (
        "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "PYTHONSTARTUP",
        "PYTHONHOME", "NODE_OPTIONS", "NODE_PATH", "PERL5OPT", "PERL5LIB", "RUBYOPT", "RUBYLIB",
        "PATH", "HOME", "BASH_ENV", "ENV", "JAVA_TOOL_OPTIONS", "cad_token", "Cad_Token",
        "1TOKEN", "TOKEN-1", "TOKEN NAME", "../TOKEN", "", "_TOKEN",
    ):
        with pytest.raises(ValueError):
            MCPServerConfig.model_validate(_entry("credbad", credential_env=[bad]))

    with pytest.raises(ValueError, match="重名"):
        MCPServerConfig.model_validate(_entry("credbad", env={"CAD_API_TOKEN": "inline"}))
    with pytest.raises(ValueError, match="重复"):
        MCPServerConfig.model_validate(_entry("credbad", credential_env=["CAD_API_TOKEN", "CAD_API_TOKEN"]))

    # 覆盖层能声明槽位（运维的文件）；Web 管理层不能
    base = _write(tmp_path / "repo" / "mcp_servers.json", {"servers": [_entry("credlayer", credential_env=[])]})
    overlay = _write(tmp_path / "deploy" / "overlay.json",
                     {"servers": [{"server_id": "credlayer", "credential_env": ["CAD_API_TOKEN"]}]})
    web = tmp_path / "mcp-admin" / "web_layer.json"
    monkeypatch.setenv("WATER_AGENT_MCP_CONFIG", str(base))
    monkeypatch.setenv("WATER_AGENT_MCP_OVERLAY", str(overlay))
    config = load_layered_gateway_config()
    assert config is not None and config.servers[0].credential_env == ("CAD_API_TOKEN",)

    monkeypatch.setenv("WATER_AGENT_MCP_WEB_LAYER", str(web))
    _write(web, {"servers": [{"server_id": "credlayer", "credential_env": ["OTHER_TOKEN"]}]})
    with pytest.raises(MCPError) as exc:
        load_layered_gateway_config()
    assert exc.value.code == "MCP_CONFIG_INVALID" and "credential_env" in str(exc.value)

    # 覆盖层里写危险名字同样加载期失败
    monkeypatch.delenv("WATER_AGENT_MCP_WEB_LAYER")
    _write(overlay, {"servers": [{"server_id": "credlayer", "credential_env": ["NODE_OPTIONS"]}]})
    with pytest.raises(MCPError) as exc:
        load_layered_gateway_config()
    assert exc.value.code == "MCP_CONFIG_INVALID"

    # 凭据文件路径只由形状合法的 server_id 与槽位名拼出
    for server_id, name in (("../etc", "CAD_API_TOKEN"), ("credok", "../../x"), ("credok", "LD_PRELOAD")):
        with pytest.raises(ValueError):
            slot_path(server_id, name)


def test_credential_values_are_write_only_and_reach_the_child(tmp_path: Path) -> None:
    """P7-S6-A02 值只写不读：状态里只有 configured / updated_at；文件 0600、目录 0700；连接时注入子进程
    （env_file < 槽位 < 内联 env）；改值后 shared server 的指纹变化、下一次租约换新进程。"""

    from water_agent.mcp.pool import McpConnectionPool, server_fingerprint

    status = slot_status("credval", "CAD_API_TOKEN")
    assert (status.configured, status.updated_at) == (False, None)

    write_slot("credval", "CAD_API_TOKEN", f"  {SLOT_VALUE}\n")
    status = slot_status("credval", "CAD_API_TOKEN")
    assert status.configured is True and status.updated_at
    assert SLOT_VALUE not in repr(status) and not hasattr(status, "value")
    path = slot_path("credval", "CAD_API_TOKEN")
    assert path == tmp_path / "credentials" / "mcp" / "credval" / "CAD_API_TOKEN"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert read_slot("credval", "CAD_API_TOKEN") == SLOT_VALUE

    # 值的校验与 LLM 凭据同一套
    from water_agent.config.credentials import InvalidCredentialError

    for bad in ("", "short", "has space inside"):
        with pytest.raises(InvalidCredentialError):
            write_slot("credval", "CAD_API_TOKEN", bad)
    assert read_slot("credval", "CAD_API_TOKEN") == SLOT_VALUE

    # 注入顺序：env_file < 槽位 < 内联 env（槽位与内联重名在加载期已拒绝，这里只验 env_file）
    env_file = tmp_path / "server.env"
    env_file.write_text("CAD_API_TOKEN=from-env-file\nOTHER=kept\n", encoding="utf-8")
    server = MCPServerConfig.model_validate(_entry("credval", env_file=str(env_file), env={"MODE": "x"}))
    built = child_env(server)
    assert built["CAD_API_TOKEN"] == SLOT_VALUE and built["OTHER"] == "kept" and built["MODE"] == "x"

    # 子进程真的拿到了（只回报存在与否）
    pool = McpConnectionPool()
    try:
        config = _gateway(_entry("credval", lifecycle="shared"))
        first = pool.lease(config)
        assert _call(first, "credval", "env_present", {"name": "CAD_API_TOKEN"}) == {
            "name": "CAD_API_TOKEN",
            "present": True,
        }
        assert _call(first, "credval", "env_present", {"name": "DEEPSEEK_API_KEY"})["present"] is False
        first_pid = _call(first, "credval", "whoami", {})["pid"]
        fingerprint = server_fingerprint(config.servers[0], config)
        first.release()

        write_slot("credval", "CAD_API_TOKEN", SLOT_VALUE + "-rotated")
        assert server_fingerprint(config.servers[0], config) != fingerprint
        second = pool.lease(config)
        assert second.assembly.connections == {"credval": "shared_new"}
        assert _call(second, "credval", "whoami", {})["pid"] != first_pid
        assert _gone(first_pid)
        second.release()
    finally:
        pool.close()

    delete_slot("credval", "CAD_API_TOKEN")
    delete_slot("credval", "CAD_API_TOKEN")  # 幂等
    assert slot_status("credval", "CAD_API_TOKEN").configured is False


def test_missing_credential_skips_the_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S6-A02 槽位没填值：连接前跳过（MCP_CREDENTIAL_MISSING）、不 spawn；停用的 server 不查凭据；
    跳过原因只有槽位名。子进程 env 在竞态下拿不到值同样报这个码，不带空 token 启动。"""

    import subprocess

    from water_agent.mcp.pool import McpConnectionPool
    from water_agent.mcp.wiring import open_gateway_sessions, pre_connection_skips

    spawned: list[object] = []
    real_popen = subprocess.Popen

    def counting_popen(*args: Any, **kwargs: Any) -> Any:
        spawned.append(args)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", counting_popen)

    config = _gateway(_entry("credmiss"), _entry("credoff", disabled=True))
    skips = pre_connection_skips(config)
    assert [(s.server_id, s.code) for s in skips] == [
        ("credoff", "MCP_SERVER_DISABLED"),
        ("credmiss", MCP_CREDENTIAL_MISSING),
    ]
    assert "CAD_API_TOKEN" in skips[1].detail

    assembly = open_gateway_sessions(config)
    assert assembly.sessions == ()
    assert [(s.server_id, s.code) for s in assembly.skipped_servers] == [
        ("credoff", "MCP_SERVER_DISABLED"),
        ("credmiss", MCP_CREDENTIAL_MISSING),
    ]
    pool = McpConnectionPool()
    try:
        lease = pool.lease(_gateway(_entry("credmiss", lifecycle="shared")), confirmation_ready=True)
        assert [(s.server_id, s.code) for s in lease.assembly.skipped_servers] == [
            ("credmiss", MCP_CREDENTIAL_MISSING)
        ]
        lease.release()
    finally:
        pool.close()
    assert spawned == [], "没填值的 server 不 spawn"

    # 竞态兜底：绕过连接前判定直接构造子进程 env
    with pytest.raises(MCPError) as exc:
        child_env(config.servers[0])
    assert exc.value.code == MCP_CREDENTIAL_MISSING

    # 填上就照常连接
    write_slot("credmiss", "CAD_API_TOKEN", SLOT_VALUE)
    assembly = open_gateway_sessions(_gateway(_entry("credmiss")))
    try:
        assert [s.config.server_id for s in assembly.sessions] == ["credmiss"]
        assert spawned, "填值后才 spawn"
    finally:
        assembly.close()

    # 跳过摘要里没有值
    assert SLOT_VALUE not in json.dumps(
        open_gateway_sessions(_gateway(_entry("credmiss2"))).summary(), ensure_ascii=False
    )
