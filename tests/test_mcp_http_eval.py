"""P7-S8 ``mcp_http_p0`` 评测集与假 HTTP MCP server 的自测（规格 §7 P7-S8-A12）。

先验证**测试替身本身**：假 server 的 Streamable HTTP / OAuth 行为按 2025-11-25 规范原文写成，评测结果
只有在它正确时才有意义。再验证 harness 的指标算术与数据集形状（不依赖被测机制是否存在）。
"""
from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import json
import os
import ssl
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from water_agent.harness import mcp_http_eval as ev

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_http_server.py"
DATASET = Path(__file__).resolve().parents[1] / "data" / "eval" / "mcp_http_p0.jsonl"


# ---------------------------------------------------------------------------
# 假 server 本身
# ---------------------------------------------------------------------------


class Running:
    def __init__(self, mode: str, tmp: Path, *flags: str) -> None:
        self.tmp = tmp
        subprocess.run([sys.executable, str(FAKE), "--make-cert", str(tmp / "tls")], check=True, capture_output=True)
        self.proc = subprocess.Popen(
            [sys.executable, str(FAKE), "--mode", mode, "--cert-dir", str(tmp / "tls"),
             "--log", str(tmp / "log.jsonl"), "--secrets", str(tmp / "secrets.json"), *flags],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        ports = json.loads(self.proc.stdout.readline())  # type: ignore[union-attr]
        self.mcp, self.auth = ports["mcp_port"], ports["auth_port"]
        self.context = ssl.create_default_context(cafile=str(tmp / "tls" / "cert.pem"))

    def request(self, port: int, method: str, path: str, body: object | None = None,
                headers: dict[str, str] | None = None, raw: str | None = None) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPSConnection("127.0.0.1", port, context=self.context, timeout=5)
        try:
            payload = raw if raw is not None else (json.dumps(body) if body is not None else None)
            connection.request(method, path, body=payload, headers=headers or {})
            response = connection.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            connection.close()

    def rpc(self, message: dict, session: str | None = None, *, version: str | None = "2025-11-25",
            token: str | None = None) -> tuple[int, dict[str, str], bytes]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if session:
            headers["Mcp-Session-Id"] = session
        if version:
            headers["MCP-Protocol-Version"] = version
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.request(self.mcp, "POST", "/mcp", message, headers)

    def log(self) -> list[dict]:
        path = self.tmp / "log.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=3)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream:
                stream.close()


@pytest.fixture
def fake(tmp_path: Path) -> Iterator[callable]:
    started: list[Running] = []

    def start(mode: str, *flags: str) -> Running:
        server = Running(mode, tmp_path, *flags)
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}


def _initialized(server: Running, **kwargs: object) -> str:
    status, headers, _ = server.rpc(INIT, version=None, **kwargs)  # type: ignore[arg-type]
    assert status == 200
    return headers["mcp-session-id"]


def test_json_mode_follows_the_streamable_http_rules(fake: callable) -> None:
    """P7-S8-A12：假 server 的会话规则——initialize 发会话号；之后的请求缺会话号 400、缺协议版本头 400、
    通知回 202 无响应体；tools/call 回显参数。"""

    server = fake("json")
    session = _initialized(server)
    status, _, body = server.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    assert (status, body) == (202, b"")
    assert server.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[0] == 400
    assert server.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session, version=None)[0] == 400
    status, headers, body = server.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
    assert status == 200 and headers["content-type"] == "application/json"
    assert {tool["name"] for tool in json.loads(body)["result"]["tools"]} >= {"echo_query", "session_probe"}
    status, _, body = server.rpc(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo_query", "arguments": {"query": "管径"}}},
        session,
    )
    assert json.loads(body)["result"]["structuredContent"] == {"echo": {"query": "管径"}}


def test_sse_mode_primes_the_stream_and_ends_with_the_response(fake: callable) -> None:
    server = fake("sse")
    session = _initialized(server)
    status, headers, body = server.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
    assert status == 200 and headers["content-type"] == "text/event-stream"
    text = body.decode()
    assert text.startswith("id: 0\ndata:\n\n"), "先发一个带 id 的空 data 事件（规范 SHOULD）"
    assert '"tools"' in text


def test_session_expire_returns_404_for_the_old_session_only(fake: callable) -> None:
    server = fake("session_expire")
    session = _initialized(server)
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "session_probe", "arguments": {}}}
    assert server.rpc(call, session)[0] == 200
    assert server.rpc({**call, "id": 3}, session)[0] == 404
    fresh = _initialized(server)
    assert fresh != session and server.rpc({**call, "id": 4}, fresh)[0] == 200
    processed = [e for e in server.log() if e.get("rpc_method") == "tools/call" and e.get("status") == 200]
    assert len(processed) == 2, "404 的那次请求不算被处理"


def test_modern_only_rejects_initialize_with_a_400_header_mismatch(fake: callable) -> None:
    status, _, body = fake("modern_only").rpc(INIT, version=None)
    assert status == 400
    assert json.loads(body)["error"]["code"] == -32020


def test_redirect_mode_never_serves_the_redirect_target_by_itself(fake: callable) -> None:
    server = fake("redirect")
    status, headers, _ = server.rpc(INIT, version=None)
    assert status == 307 and headers["location"].endswith("/elsewhere")


def test_oauth_mode_challenges_with_resource_metadata_and_scope(fake: callable) -> None:
    server = fake("oauth")
    status, headers, _ = server.rpc(INIT, version=None)
    assert status == 401
    challenge = headers["www-authenticate"]
    assert challenge.startswith("Bearer ") and "resource_metadata=" in challenge and 'scope="mcp:read"' in challenge
    status, _, body = server.request(server.mcp, "GET", "/.well-known/oauth-protected-resource/mcp")
    metadata = json.loads(body)
    assert status == 200 and metadata["resource"].endswith("/mcp") and metadata["authorization_servers"]


def test_oauth_authorization_code_flow_with_pkce_and_resource(fake: callable) -> None:
    """规范逐条的服务端半边：授权端点要求 PKCE 与 resource；令牌端点校验 verifier 与 resource；访问令牌
    只对其 resource 有效；授权码只能用一次。"""

    server = fake("oauth")
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    resource = f"https://127.0.0.1:{server.mcp}/mcp"
    redirect = "http://127.0.0.1:18080/cb"
    query = urlencode({"response_type": "code", "client_id": "wa-preregistered", "redirect_uri": redirect,
                       "code_challenge": challenge, "code_challenge_method": "S256", "state": "st-1",
                       "resource": resource, "scope": "mcp:read"})
    assert server.request(server.auth, "GET", "/authorize?response_type=code&client_id=wa-preregistered")[0] == 400
    status, headers, _ = server.request(server.auth, "GET", f"/authorize?{query}")
    assert status == 302
    params = parse_qs(urlparse(headers["location"]).query)
    assert params["state"] == ["st-1"] and params["iss"] == [f"https://127.0.0.1:{server.auth}"]
    form = {"grant_type": "authorization_code", "code": params["code"][0], "code_verifier": verifier,
            "redirect_uri": redirect, "client_id": "wa-preregistered", "resource": resource}
    headers_form = {"Content-Type": "application/x-www-form-urlencoded"}
    bad = {**form, "code_verifier": "w" * 64}
    assert server.request(server.auth, "POST", "/token", headers=headers_form, raw=urlencode(bad))[0] == 400
    # 授权码在一次失败的兑换后也作废（一次性）
    assert server.request(server.auth, "POST", "/token", headers=headers_form, raw=urlencode(form))[0] == 400

    status, headers, _ = server.request(server.auth, "GET", f"/authorize?{query}")
    code = parse_qs(urlparse(headers["location"]).query)["code"][0]
    status, _, body = server.request(server.auth, "POST", "/token", headers=headers_form,
                                     raw=urlencode({**form, "code": code}))
    token = json.loads(body)
    assert status == 200 and token["token_type"] == "Bearer" and token["refresh_token"]
    session = _initialized(server, token=token["access_token"])
    assert server.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session, token=token["access_token"])[0] == 200
    assert server.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)[0] == 401
    secrets = json.loads((server.tmp / "secrets.json").read_text())
    assert token["access_token"] in secrets and token["refresh_token"] in secrets and "st-1" in secrets


def test_no_pkce_mode_omits_code_challenge_methods(fake: callable) -> None:
    server = fake("oauth_no_pkce")
    _, _, body = server.request(server.auth, "GET", "/.well-known/oauth-authorization-server")
    assert "code_challenge_methods_supported" not in json.loads(body)
    server2 = fake("oauth")
    _, _, body = server2.request(server2.auth, "GET", "/.well-known/oauth-authorization-server")
    assert json.loads(body)["code_challenge_methods_supported"] == ["S256"]


def test_insufficient_scope_is_a_403_on_tools_call_only(fake: callable) -> None:
    server = fake("insufficient_scope")
    verifier = "k" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    resource = f"https://127.0.0.1:{server.mcp}/mcp"
    redirect = "http://127.0.0.1:18080/cb"
    query = urlencode({"response_type": "code", "client_id": "wa-preregistered", "redirect_uri": redirect,
                       "code_challenge": challenge, "code_challenge_method": "S256", "state": "s",
                       "resource": resource})
    _, headers, _ = server.request(server.auth, "GET", f"/authorize?{query}")
    code = parse_qs(urlparse(headers["location"]).query)["code"][0]
    _, _, body = server.request(
        server.auth, "POST", "/token", headers={"Content-Type": "application/x-www-form-urlencoded"},
        raw=urlencode({"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                       "redirect_uri": redirect, "client_id": "wa-preregistered", "resource": resource}))
    token = json.loads(body)["access_token"]
    session = _initialized(server, token=token)
    status, headers, _ = server.rpc(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "echo_query", "arguments": {"query": "x"}}},
        session, token=token)
    assert status == 403 and 'error="insufficient_scope"' in headers["www-authenticate"]
    assert "mcp:write" in headers["www-authenticate"]


def test_request_log_never_contains_secret_values(fake: callable) -> None:
    server = fake("bearer")
    server.rpc(INIT, version=None, token="static-bearer-token-0001")
    text = (server.tmp / "log.jsonl").read_text()
    assert "static-bearer-token-0001" not in text
    assert json.loads((server.tmp / "secrets.json").read_text()).count("static-bearer-token-0001") == 1


# ---------------------------------------------------------------------------
# 数据集与指标算术
# ---------------------------------------------------------------------------


def test_dataset_loads_and_covers_the_required_surfaces() -> None:
    cases, problems = ev.load_cases(DATASET)
    assert problems == []
    assert len(cases) >= 24
    assert len({case.id for case in cases}) == len(cases)
    assert {case.entry for case in cases} == {"session", "web", "runner"}
    assert {case.category for case in cases} == {"transport", "protocol", "ssrf", "bearer", "oauth", "confirmation", "entry"}
    modes = {case.server.mode for case in cases}
    assert {"json", "sse", "session_expire", "bad_content_type", "flood", "redirect", "server_request", "modern_only",
            "oauth", "oauth_no_pkce", "oauth_bad_iss", "oauth_expire", "insufficient_scope"} <= modes
    assert sum(1 for case in cases if case.forbidden_destination) >= 4


def test_metrics_are_computed_only_from_the_report_detail() -> None:
    cases = [
        {"id": "a", "runs": [{"checks": {"exposure": True, "skip": True, "call": False}}], "forbidden_destination": True,
         "token_leaks": 0, "tool_call_replays": 0, "egress_ungoverned": 0, "error": None},
        {"id": "b", "runs": [{"checks": {"exposure": False}}], "forbidden_destination": False,
         "token_leaks": 2, "tool_call_replays": 1, "egress_ungoverned": 3, "error": "boom"},
    ]
    metrics = ev.metrics_from_case_dicts(cases)
    assert metrics == {
        "expected_exposure_rate": 0.5, "skip_code_match_rate": 1.0, "call_result_match_rate": 0.0,
        "forbidden_destination_block_rate": 0.5, "token_leak_count": 2, "tool_call_replay_count": 1,
        "egress_ungoverned_count": 3, "harness_error_count": 1,
    }


def test_unmeasured_rates_are_never_a_pass() -> None:
    metrics = ev.metrics_from_case_dicts([{"id": "a", "runs": [], "error": None}])
    assert metrics["expected_exposure_rate"] is None
    assert any("expected_exposure_rate" in item for item in ev.hard_violations(metrics))


def test_code_matching_accepts_the_registry_wrapped_form() -> None:
    from water_agent.mcp.errors import _CODE_HINTS, MCP_TIMEOUT

    assert ev._code_matches({"code": MCP_TIMEOUT}, MCP_TIMEOUT)
    assert ev._code_matches({"code": "TOOL_ERROR", "message": f"{_CODE_HINTS[MCP_TIMEOUT]}调用超过 1s"}, MCP_TIMEOUT)
    assert not ev._code_matches({"code": "TOOL_ERROR", "message": "别的错误"}, MCP_TIMEOUT)


def test_wire_requests_exclude_the_browsers_authorize_step() -> None:
    log = [
        {"server": "mcp", "path": "/mcp"},
        {"server": "auth", "event": "authorize", "path": "/authorize"},
        {"server": "auth", "event": "token", "path": "/token"},
    ]
    assert len(ev._wire(log)) == 2


def test_processed_tool_calls_ignore_requests_rejected_before_processing() -> None:
    log = [
        {"server": "mcp", "rpc_method": "tools/call", "status": 200},
        {"server": "mcp", "rpc_method": "tools/call", "status": 404},
        {"server": "mcp", "rpc_method": "tools/call", "status": "auth_rejected"},
        {"server": "mcp", "rpc_method": "tools/call", "status": "cut"},
    ]
    assert ev.processed_tool_calls(log) == 2


def test_network_spy_records_connects_and_fakes_dns() -> None:
    import socket

    with ev.NetworkSpy({"x.test": ["10.9.8.7"]}, {"x.test": ["169.254.169.254"]}) as spy:
        first = socket.getaddrinfo("x.test", 443)
        second = socket.getaddrinfo("x.test", 443)
    assert first[0][4][0] == "10.9.8.7" and second[0][4][0] == "169.254.169.254"
    assert spy.lookups["x.test"] == 2
    assert socket.getaddrinfo is not None  # 退出后已还原
    time.sleep(0)


# ---------------------------------------------------------------------------
# 完整评测与门禁对比（A12）
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPARE = REPO_ROOT / "scripts" / "compare_mcp_http_p0.py"
BASELINE = REPO_ROOT / "data" / "eval" / "mcp_http_p0_main.json"


@pytest.fixture(scope="module")
def head() -> dict[str, Any]:
    for key in ("WATER_AGENT_MCP_OVERLAY", "WATER_AGENT_MCP_CONFIG", "WATER_AGENT_MCP_WEB_LAYER",
                "WATER_AGENT_TOOL_EXPOSURE", "WATER_AGENT_SKILLS", "WATER_AGENT_SKILLS_DIR", "WEB_PUBLIC_BASE_URL"):
        assert key not in os.environ, f"{key} 会让 harness 读到宿主配置"
    report, problems = ev.run(DATASET)
    assert problems == [] and report is not None
    return report


def _compare(tmp_path: Path, head_report: dict[str, Any], base: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    head_path = tmp_path / "head.json"
    head_path.write_text(json.dumps(head_report, ensure_ascii=False), encoding="utf-8")
    argv = [sys.executable, str(COMPARE), "--head", str(head_path)]
    if base is not None:
        base_path = tmp_path / "base.json"
        base_path.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
        argv += ["--base", str(base_path)]
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    done = subprocess.run(argv, capture_output=True, text=True, check=False, env=env)
    return done.returncode, json.loads(done.stdout or done.stderr)


def test_mcp_http_p0_hard_metrics(head: dict[str, Any], tmp_path: Path) -> None:
    """P7-S8-A12：会话 / runner / Web 三条路径经本地假 HTTP server 与假授权服务器——44 条全过、八项硬指标全部达标
    （SSRF 拦截零字节、令牌零泄漏、工具调用零重放、每个出站请求都有网关账）；入仓基线是同一数据集上的 present 报告，
    逐字可复现；比较器拒绝手改指标、absent 机制、用例消失、PASS_TO_PASS 回退与数据集变化。"""

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert head["mechanism"] == "present"
    assert head["passed"] == head["total"] == 44
    assert head["hard_violations"] == [], head["hard_violations"]
    assert head["metrics"] == {
        "expected_exposure_rate": 1.0,
        "skip_code_match_rate": 1.0,
        "call_result_match_rate": 1.0,
        "forbidden_destination_block_rate": 1.0,
        "token_leak_count": 0,
        "tool_call_replay_count": 0,
        "egress_ungoverned_count": 0,
        "harness_error_count": 0,
    }
    assert {case["entry"] for case in head["cases"]} == {"session", "web", "runner"}
    assert ev.metrics_from_case_dicts(head["cases"]) == head["metrics"]
    assert baseline["generated_by"] == ev.GENERATED_BY and baseline["mechanism"] == "present"
    assert baseline["dataset_sha256"] == hashlib.sha256(DATASET.read_bytes()).hexdigest()
    assert baseline == head

    code, result = _compare(tmp_path, head, baseline)
    assert code == 0 and result["violations"] == [], result

    tampered = copy.deepcopy(baseline)
    tampered["metrics"]["token_leak_count"] = 0
    tampered["cases"][-1]["token_leaks"] = 3
    code, result = _compare(tmp_path, tampered, baseline)
    assert code == 1 and any("疑似手改" in item for item in result["violations"])

    absent = copy.deepcopy(baseline)
    absent["mechanism"] = "absent"
    code, result = _compare(tmp_path, absent, baseline)
    assert code == 1 and any("只认 present" in item for item in result["violations"])

    dropped = copy.deepcopy(baseline)
    dropped["cases"] = dropped["cases"][1:]
    dropped["total"] = len(dropped["cases"])
    dropped["passed"] = sum(1 for case in dropped["cases"] if case["passed"])
    dropped["metrics"] = ev.metrics_from_case_dicts(dropped["cases"])
    code, result = _compare(tmp_path, dropped, baseline)
    assert code == 1 and any("消失" in item for item in result["violations"])

    regressed = copy.deepcopy(baseline)
    victim = next(case for case in regressed["cases"] if case["category"] == "oauth")
    victim["passed"] = False
    victim["token_leaks"] = 2
    regressed["passed"] -= 1
    regressed["metrics"] = ev.metrics_from_case_dicts(regressed["cases"])
    code, result = _compare(tmp_path, regressed, baseline)
    assert code == 1
    assert any("PASS_TO_PASS" in item for item in result["violations"])
    assert any("token_leak_count" in item for item in result["violations"])

    moved = copy.deepcopy(baseline)
    moved["dataset_sha256"] = "0" * 64
    code, result = _compare(tmp_path, moved, baseline)
    assert code == 1 and any("数据集与基线不同" in item for item in result["violations"])
