"""MCP 长驻连接（P7-S5，``docs/design/16-p7-plugin-mcp.md`` §7 P7-S5）。

全部用 ``tests/fixtures/fake_mcp_server.py --mode stateful``：``whoami`` 回报 pid、``bump`` 是进程内
计数器、``slow_echo`` 睡指定秒数后回显 tag。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from water_agent.mcp.client import MCPStdioClient
from water_agent.mcp.config import MCPServerConfig

FAKE_SERVER = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
STATEFUL_ALLOW = ("whoami", "bump", "slow_echo")


def _server(server_id: str, **overrides: Any) -> MCPServerConfig:
    payload: dict[str, Any] = {
        "server_id": server_id,
        "command": sys.executable,
        "args": [str(FAKE_SERVER), "--mode", "stateful"],
        "allow": list(STATEFUL_ALLOW),
        "call_timeout_seconds": 3.0,
    }
    payload.update(overrides)
    return MCPServerConfig.model_validate(payload)


def test_concurrent_calls_on_one_client_get_their_own_responses() -> None:
    """P7-S5-A01：同一个客户端上并发调用，每个调用方都拿到**自己的**响应。

    修复前 ``_request`` 不是并发安全的：各请求共用一个 inbox，读到别人的响应时当成通知存起来
    继续等，真正的请求方只能超时（同一 run 里并行调用同一台 server 的两个工具就会触发）。
    """

    client = MCPStdioClient(_server("poolconc"))
    client.connect()
    try:
        for round_no in range(3):
            results: dict[str, Any] = {}

            def call(tag: str, seconds: float) -> None:
                try:
                    results[tag] = client.call_tool("slow_echo", {"tag": tag, "seconds": seconds})
                except Exception as exc:  # noqa: BLE001 — 记下来统一断言
                    results[tag] = exc

            # 先发的睡得久：server 按完成顺序应答，响应与等待顺序相反
            tags = [f"r{round_no}-t{index}" for index in range(4)]
            threads = [
                threading.Thread(target=call, args=(tag, 0.6 - 0.2 * index))
                for index, tag in enumerate(tags)
            ]
            for thread in threads:
                thread.start()
                threading.Event().wait(0.05)
            for thread in threads:
                thread.join(timeout=15)
            for tag in tags:
                outcome = results.get(tag)
                assert isinstance(outcome, dict), (tag, outcome)
                assert outcome["ok"] is True, (tag, outcome)
                assert outcome["structured_content"] == {"tag": tag}, (tag, outcome)
        assert client.alive is True
    finally:
        client.close()
    assert client.alive is False


def test_waiting_for_a_busy_server_counts_against_the_deadline() -> None:
    """P7-S5-A01：等锁的时间计入调用 deadline——前一个调用占着连接时，后一个调用按 deadline 超时，
    消息写明 server 正忙，而不是无限排队。"""

    from water_agent.mcp.errors import MCP_TIMEOUT, MCPError

    client = MCPStdioClient(_server("poolbusy"))
    client.connect()
    try:
        holder = threading.Thread(
            target=client.call_tool, args=("slow_echo", {"tag": "long", "seconds": 1.5})
        )
        holder.start()
        threading.Event().wait(0.2)
        with pytest.raises(MCPError) as exc:
            client.call_tool("slow_echo", {"tag": "short", "seconds": 0}, deadline_seconds=0.5)
        assert exc.value.code == MCP_TIMEOUT
        assert "正忙" in exc.value.message
        holder.join(timeout=10)
    finally:
        client.close()


def test_lifecycle_field_defaults_to_per_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S5-A02：``lifecycle`` 缺省 ``per_run``（与此前行为一致）；覆盖层可以把它改成 ``shared``；
    非法取值在加载期 ``MCP_CONFIG_INVALID``。"""

    import json

    from water_agent.mcp.config import (
        ENV_MCP_CONFIG,
        ENV_MCP_OVERLAY,
        MCP_CONFIG_VERSION,
        load_layered_gateway_config,
    )
    from water_agent.mcp.errors import MCP_CONFIG_INVALID, MCPError

    assert MCP_CONFIG_VERSION == "p7-s8-2026-10-12"
    assert _server("poollife").lifecycle == "per_run"

    base = tmp_path / "base.json"
    base.write_text(
        json.dumps(
            {
                "version": MCP_CONFIG_VERSION,
                "servers": [
                    {
                        "server_id": "poollife",
                        "command": sys.executable,
                        "args": [str(FAKE_SERVER), "--mode", "stateful"],
                        "allow": list(STATEFUL_ALLOW),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    overlay = tmp_path / "overlay.json"
    overlay.write_text(
        json.dumps({"servers": [{"server_id": "poollife", "lifecycle": "shared"}]}), encoding="utf-8"
    )
    monkeypatch.setenv(ENV_MCP_CONFIG, str(base))
    monkeypatch.delenv(ENV_MCP_OVERLAY, raising=False)
    plain = load_layered_gateway_config()
    assert plain is not None and plain.servers[0].lifecycle == "per_run"
    monkeypatch.setenv(ENV_MCP_OVERLAY, str(overlay))
    shared = load_layered_gateway_config()
    assert shared is not None and shared.servers[0].lifecycle == "shared"

    overlay.write_text(
        json.dumps({"servers": [{"server_id": "poollife", "lifecycle": "forever"}]}), encoding="utf-8"
    )
    with pytest.raises(MCPError) as exc:
        load_layered_gateway_config()
    assert exc.value.code == MCP_CONFIG_INVALID


# ---------------------------------------------------------------------------
# 连接池
# ---------------------------------------------------------------------------


def _gateway(*servers: dict[str, Any]) -> Any:
    from water_agent.mcp.config import MCP_CONFIG_VERSION, MCPGatewayConfig

    return MCPGatewayConfig.model_validate({"version": MCP_CONFIG_VERSION, "servers": list(servers)})


def _entry(server_id: str, *, lifecycle: str = "shared", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "server_id": server_id,
        "command": sys.executable,
        "args": [str(FAKE_SERVER), "--mode", "stateful"],
        "allow": list(STATEFUL_ALLOW),
        "call_timeout_seconds": 3.0,
        "lifecycle": lifecycle,
    }
    payload.update(overrides)
    return payload


def _session(lease: Any, server_id: str) -> Any:
    for session in lease.assembly.sessions:
        if session.config.server_id == server_id:
            return session
    raise LookupError(server_id)


def _call(lease: Any, server_id: str, tool: str, **args: Any) -> dict[str, Any]:
    result = _session(lease, server_id).client.call_tool(tool, args)
    assert result["ok"], result
    return result["structured_content"]


def _pid(lease: Any, server_id: str) -> int:
    return int(_call(lease, server_id, "whoami")["pid"])


def _gone(pid: int) -> bool:
    import os
    import time

    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def pool() -> Any:
    from water_agent.mcp.pool import McpConnectionPool

    instance = McpConnectionPool()
    yield instance
    instance.close()


def test_shared_server_is_reused_and_per_run_is_not(pool: Any) -> None:
    """P7-S5-A03：shared 的 server 连续两次租约是同一个进程；per_run 的每次都是新进程、释放即关闭。"""

    config = _gateway(_entry("poolshare"), _entry("poolrun", lifecycle="per_run"))
    first = pool.lease(config)
    shared_pid, run_pid = _pid(first, "poolshare"), _pid(first, "poolrun")
    assert first.assembly.connections == {"poolshare": "shared_new", "poolrun": "per_run"}
    first.release()
    assert _gone(run_pid)

    second = pool.lease(config)
    assert _pid(second, "poolshare") == shared_pid
    assert _pid(second, "poolrun") != run_pid
    assert second.assembly.connections == {"poolshare": "shared_reused", "poolrun": "per_run"}
    second.release()
    second.release()  # 幂等
    assert pool.snapshot()["active"]["poolshare"] == {"refs": 0, "alive": True}


def test_shared_server_state_is_visible_across_leases(pool: Any) -> None:
    """P7-S5-A03：共享即共享状态——两个租约（两个用户的 run）先后 bump，计数是连续的。
    这正是"声明 shared 即声明 server 不持有按用户区分的状态"这条警告的依据。"""

    config = _gateway(_entry("poolstate"))
    alice, bob = pool.lease(config), pool.lease(config)
    assert _call(alice, "poolstate", "bump")["counter"] == 1
    assert _call(bob, "poolstate", "bump")["counter"] == 2
    alice.release()
    bob.release()


def test_fingerprint_change_retires_the_old_process(pool: Any) -> None:
    """P7-S5-A04：配置（allow）或子进程 env 变了 → 下一次租约开新进程；旧进程在最后一个借用它的
    租约释放前仍可用、释放后关闭。删掉或停用的 server 同样退役。"""

    old = pool.lease(_gateway(_entry("poolfp")))
    old_pid = _pid(old, "poolfp")

    narrowed = pool.lease(_gateway(_entry("poolfp", allow=["whoami", "slow_echo"])))
    new_pid = _pid(narrowed, "poolfp")
    assert new_pid != old_pid
    assert _pid(old, "poolfp") == old_pid  # 旧租约在释放前照常可用
    old.release()
    assert _gone(old_pid)

    rotated = pool.lease(_gateway(_entry("poolfp", allow=["whoami", "slow_echo"], env={"TOKEN": "v2"})))
    rotated_pid = _pid(rotated, "poolfp")
    assert rotated_pid != new_pid
    narrowed.release()
    assert _gone(new_pid)
    rotated.release()

    disabled = pool.lease(_gateway(_entry("poolfp", disabled=True)))
    assert disabled.assembly.sessions == ()
    assert _gone(rotated_pid)
    disabled.release()
    assert "poolfp" not in pool.snapshot()["active"]


def test_dead_shared_process_is_reopened(pool: Any) -> None:
    """P7-S5-A05：共享进程死了，下一次租约重开；还拿着死连接的调用快速失败（结构化错误，不空等）。"""

    import os
    import signal
    import time

    from water_agent.mcp.errors import MCP_CONNECT_FAILED, MCPError

    config = _gateway(_entry("pooldead"))
    held = pool.lease(config)
    pid = _pid(held, "pooldead")
    os.kill(pid, signal.SIGKILL)
    client = _session(held, "pooldead").client
    for _ in range(50):  # 被杀的子进程在 poll() 回收前是僵尸，os.kill(pid, 0) 仍会成功
        if not client.alive:
            break
        time.sleep(0.05)
    assert client.alive is False

    started = time.monotonic()
    with pytest.raises(MCPError) as exc:
        for _ in range(2):  # 第一次取走 EOF 哨兵，第二次靠 reader_done 立即失败
            _session(held, "pooldead").client.call_tool("whoami", {})
    assert exc.value.code == MCP_CONNECT_FAILED
    assert time.monotonic() - started < 2.0

    fresh = pool.lease(config)
    assert fresh.assembly.connections == {"pooldead": "shared_new"}
    assert _pid(fresh, "pooldead") != pid
    held.release()
    fresh.release()


def test_pool_close_leaves_no_processes() -> None:
    """P7-S5-A06：池关闭后不留活进程；仍有未释放的租约照样终止并报告数量；关闭后不能再借用。"""

    from water_agent.mcp.pool import McpConnectionPool

    pool = McpConnectionPool()
    config = _gateway(_entry("poolclose"))
    released, outstanding = pool.lease(config), pool.lease(config)
    pid = _pid(outstanding, "poolclose")
    released.release()
    result = pool.close()
    assert result == {"closed_servers": 1, "outstanding_leases": 1}
    assert _gone(pid)
    with pytest.raises(RuntimeError, match="已关闭"):
        pool.lease(config)
    outstanding.release()  # 关闭后释放仍幂等、不抛


def test_concurrent_first_lease_spawns_once(pool: Any) -> None:
    """P7-S5-A06：多个 run 同时首次借用同一台 shared server，只启动一个进程。"""

    config = _gateway(_entry("poolrace"))
    leases: list[Any] = []
    lock = threading.Lock()

    def take() -> None:
        lease = pool.lease(config)
        with lock:
            leases.append(lease)

    threads = [threading.Thread(target=take) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert len(leases) == 4
    assert len({_pid(lease, "poolrace") for lease in leases}) == 1
    kinds = sorted(lease.assembly.connections["poolrace"] for lease in leases)
    assert kinds == ["shared_new", "shared_reused", "shared_reused", "shared_reused"]
    for lease in leases:
        lease.release()


def test_trace_summary_reports_connection_kind(pool: Any) -> None:
    """P7-S5-A08：trace 摘要每台 server 带 connection，且不含指纹、env 或命令行。"""

    import json

    config = _gateway(_entry("pooltrace", env={"SECRET_TOKEN": "do-not-log"}))
    first = pool.lease(config)
    second = pool.lease(config)
    summary = second.assembly.trace_summary()
    assert [item["connection"] for item in summary["mcp_servers"]] == ["shared_reused"]
    assert first.assembly.trace_summary()["mcp_servers"][0]["connection"] == "shared_new"
    rendered = json.dumps(summary, ensure_ascii=False)
    for leaked in ("do-not-log", "SECRET_TOKEN", "fingerprint", str(FAKE_SERVER)):
        assert leaked not in rendered
    first.release()
    second.release()
