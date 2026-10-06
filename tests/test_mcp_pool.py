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
