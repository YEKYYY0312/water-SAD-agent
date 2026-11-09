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

    assert MCP_CONFIG_VERSION == "p7-s8-security-wave1-2026-11-06"
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


# ---------------------------------------------------------------------------
# P7-S8：Streamable HTTP server 进连接池
# ---------------------------------------------------------------------------


def test_http_server_sessions_are_pooled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S8-A09：HTTP server 同样支持 per_run / shared。shared 复用的是 **MCP 会话**（不是 TCP 连接）：第二个
    租约拿到同一个 ``MCP-Session-Id``，server 只收到一次 initialize；per_run 每个租约现开、释放时 DELETE。
    指纹含静态 Bearer 的值与 OAuth 授权身份摘要——改值 / 重新授权换会话，令牌刷新不换；会话被 server 终止（404）时
    客户端透明重建一次，池里的条目不退役。"""

    import json
    import subprocess

    from water_agent.mcp.config import MCPGatewayConfig
    from water_agent.mcp.credentials import write_slot
    from water_agent.mcp.http_client import mcp_request_scope
    from water_agent.mcp.oauth_store import OAuthStore, StoredToken
    from water_agent.mcp.pool import (
        CONNECTION_PER_RUN,
        CONNECTION_SHARED_NEW,
        CONNECTION_SHARED_REUSED,
        McpConnectionPool,
        server_fingerprint,
    )
    from water_agent.runtime.governance.contracts import (
        DEFAULT_LOCAL_CAPABILITIES,
        Capability,
        Principal,
        RequestContext,
    )

    fake = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_http_server.py"
    cert_dir = tmp_path / "tls"
    subprocess.run([sys.executable, str(fake), "--make-cert", str(cert_dir)], check=True, capture_output=True)
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_dir / "cert.pem"))
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "SSL_CERT_DIR"):
        monkeypatch.delenv(key, raising=False)
    context = RequestContext(
        principal=Principal.local("pooltest", capabilities=DEFAULT_LOCAL_CAPABILITIES | {Capability.MCP_INVOKE}),
        request_id="req-pool",
    )

    def start(mode: str, name: str) -> tuple[subprocess.Popen[str], int, Path]:
        log = tmp_path / f"{name}.jsonl"
        proc = subprocess.Popen(
            [sys.executable, str(fake), "--mode", mode, "--cert-dir", str(cert_dir), "--log", str(log)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert proc.stdout is not None
        return proc, json.loads(proc.stdout.readline())["mcp_port"], log

    def log_of(path: Path) -> list[dict[str, Any]]:
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def http(server_id: str, port: int, **overrides: Any) -> MCPServerConfig:
        payload: dict[str, Any] = {
            "server_id": server_id, "transport": "streamable_http", "url": f"https://127.0.0.1:{port}/mcp",
            "allow_private_network": True, "allow": ["session_probe"], "call_timeout_seconds": 3.0,
        }
        payload.update(overrides)
        return MCPServerConfig.model_validate(payload)

    def probe(lease: Any, server_id: str) -> dict[str, Any]:
        client = next(s.client for s in lease.assembly.sessions if s.config.server_id == server_id)
        with mcp_request_scope(context):
            return client.call_tool("session_probe", {})["structured_content"]  # type: ignore[no-any-return]

    processes: list[subprocess.Popen[str]] = []
    pool = McpConnectionPool()
    try:
        proc, port, log = start("json", "pooled")
        processes.append(proc)
        shared = http("poolshared", port, lifecycle="shared")
        per_run = http("poolperrun", port)
        gateway = MCPGatewayConfig(servers=(shared, per_run))

        first = pool.lease(gateway)
        second = pool.lease(gateway)
        assert first.assembly.connections == {"poolshared": CONNECTION_SHARED_NEW, "poolperrun": CONNECTION_PER_RUN}
        assert second.assembly.connections == {"poolshared": CONNECTION_SHARED_REUSED, "poolperrun": CONNECTION_PER_RUN}
        a, b = probe(first, "poolshared"), probe(second, "poolshared")
        assert a["session"] == b["session"] and (a["calls_in_session"], b["calls_in_session"]) == (1, 2)
        assert probe(first, "poolperrun")["session"] != probe(second, "poolperrun")["session"], "per_run 各自独立会话"
        first.release()
        second.release()
        deletes = [e for e in log_of(log) if e["method"] == "DELETE"]
        assert len(deletes) == 2, "per_run 的两个会话在租约释放时 DELETE；shared 的还留在池里"
        third = pool.lease(gateway)
        assert probe(third, "poolshared")["calls_in_session"] == 3, "释放租约不关 shared 会话"
        third.release()
        assert pool.snapshot()["active"]["poolshared"] == {"refs": 0, "alive": True}
        pool.close()
        assert len([e for e in log_of(log) if e["method"] == "DELETE"]) == 4, "关池时 shared 会话也 DELETE"

        # 指纹：静态 Bearer 的值进摘要
        bearer = http("poolbearer", port, bearer_credential="POOL_KEY", credential_env=["POOL_KEY"])
        gw = MCPGatewayConfig(servers=(bearer,))
        write_slot("poolbearer", "POOL_KEY", "pool-bearer-value-0001")
        before = server_fingerprint(bearer, gw)
        assert server_fingerprint(bearer, gw) == before
        write_slot("poolbearer", "POOL_KEY", "pool-bearer-value-0002")
        assert server_fingerprint(bearer, gw) != before, "面板改值：shared 会话换新的"

        # 指纹：OAuth 授权身份摘要——刷新不换、重新授权换、清除换
        oauth_server = http("pooloauth", port, oauth={"client_id": "c"})
        gw2 = MCPGatewayConfig(servers=(oauth_server,))
        store = OAuthStore("pooloauth")
        token = StoredToken(
            authorization_id="auth-1", issuer="https://a.example", resource="https://127.0.0.1/mcp", client_id="c",
            client_auth="none", token_endpoint="https://a.example/token", revocation_endpoint=None,
            access_token="access-value-aaaa", refresh_token="refresh-value-aaaa", issued_at=1.0, expires_at=10.0, scope=None,
        )
        assert server_fingerprint(oauth_server, gw2) == server_fingerprint(oauth_server, gw2)
        none_fp = server_fingerprint(oauth_server, gw2)
        store.save(token)
        authorized_fp = server_fingerprint(oauth_server, gw2)
        assert authorized_fp != none_fp
        store.save(token.with_tokens(access_token="access-value-bbbb", refresh_token="refresh-value-bbbb",
                                     issued_at=2.0, expires_at=20.0, scope=None))
        assert server_fingerprint(oauth_server, gw2) == authorized_fp, "令牌刷新：同一份授权，不换指纹"
        store.save(StoredToken(**{**token.__dict__, "authorization_id": "auth-2"}))
        assert server_fingerprint(oauth_server, gw2) != authorized_fp, "重新授权换指纹"
        store.clear()
        assert server_fingerprint(oauth_server, gw2) == none_fp, "清除授权回到未授权的指纹"
    finally:
        pool.close()
        for proc in processes:
            proc.terminate()
            proc.wait(timeout=3)
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    stream.close()

    # 会话被 server 终止（404）：同一个租约内透明重建一次，条目不退役
    expire_proc, expire_port, expire_log = start("session_expire", "expire")
    pool = McpConnectionPool()
    try:
        expiring = http("poolexpire", expire_port, lifecycle="shared")
        gateway = MCPGatewayConfig(servers=(expiring,))
        lease = pool.lease(gateway)
        one = probe(lease, "poolexpire")
        two = probe(lease, "poolexpire")
        lease.release()
        client = lease.assembly.sessions[0].client
        assert client is not None and client.session_rebuilds == 1  # type: ignore[union-attr]
        assert one["session"] != two["session"] and two["initialize_count"] == 2
        assert pool.snapshot()["active"]["poolexpire"]["alive"] is True and pool.snapshot()["retired"] == 0
        later = pool.lease(gateway)
        assert later.assembly.connections == {"poolexpire": CONNECTION_SHARED_REUSED}
        later.release()
    finally:
        pool.close()
        expire_proc.terminate()
        expire_proc.wait(timeout=3)
        for stream in (expire_proc.stdout, expire_proc.stderr):
            if stream:
                stream.close()
