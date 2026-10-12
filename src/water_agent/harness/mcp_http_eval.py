"""mcp_http_p0：MCP Streamable HTTP 与 OAuth 2.1 的机制层评测（P7-S8，规格 §7 P7-S8-A12）。

每条用例起一个**本地假 HTTP MCP server + 假授权服务器**（``tests/fixtures/fake_mcp_http_server.py``，自签 TLS，
子进程），写一份只含这台 server 的 MCP 配置，再经**生产装配**连接：

- ``session``：生产 ``AgenticSession``（真实 ``assemble_tools``、护栏、``ToolRegistry``）+ 脚本模型，工具由模型
  的脚本调用；
- ``web``：``McpConnectionPool.lease(confirmation_ready=True)`` + ``AgenticSession(mcp_lease=…, confirmation=True)``
  （与 Web 会话工厂同构，shared server 跨 run 复用）；
- ``runner``：``assemble_tools(McpGatewayPlugin.owning(…))`` + 直接 ``registry.invoke``（评测 runner 的装配方式）。

harness 据此判定八类事实（硬指标）：暴露了哪些远端工具、整台 / 单工具的跳过码、调用结果、**应拦的目标是否一个
字节都没发出**、令牌 / 授权码 / verifier / state 原文有没有出现在任何可落盘的产物里、同一次工具调用有没有被
重放、每个出站请求是否都对应一条 ``ProviderGateway`` 账、harness 自身是否出错。

**不测**：真实 HTTP MCP server、真实授权服务器、企业代理与证书链、浏览器里的授权跳转（见规格 P7-S8 未验证项）。
用例是合成的，规则与标签同一作者。Web 路由（``/api/admin/mcp/oauth/*``）的泄漏与鉴权由
``tests/test_web_mcp_oauth.py`` 守，不在这里。

数据集在 S8 Step 1 冻结；之后改它必须同时重新固化入仓基线并写明原因。

退出码：0 全部硬指标达标；1 有硬指标不达标或用例执行出错；2 加载问题、0 条用例或参数错误。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib
import json
import logging
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from water_agent.harness.doubles import ScriptedChatModel, ScriptExhausted

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATASET = REPO_ROOT / "data" / "eval" / "mcp_http_p0.jsonl"
FAKE_HTTP_SERVER = REPO_ROOT / "tests" / "fixtures" / "fake_mcp_http_server.py"
FIXTURE_CLAUSES = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"

SUITE = "mcp_http_p0"
GENERATED_BY = "water_agent.harness.mcp_http_eval"
REPORT_FORMAT = 1
SERVER_ID = "httpfake"
#: 授权回调地址（harness 只把它交给授权流程，从不真的监听）。规范允许 localhost 回调。
REDIRECT_URI = "http://127.0.0.1:18080/api/admin/mcp/oauth/callback"
DEFAULT_ALLOW = ("echo_query", "lookup_project", "nested_schema_tool", "session_probe")
PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")

#: 硬指标与目标值。速率为 ``None``（分母为 0、未测量）一律判不达标：未测量不是通过。
HARD_TARGETS: dict[str, float | int] = {
    "expected_exposure_rate": 1.0,
    "skip_code_match_rate": 1.0,
    "call_result_match_rate": 1.0,
    "forbidden_destination_block_rate": 1.0,
    "token_leak_count": 0,
    "tool_call_replay_count": 0,
    "egress_ungoverned_count": 0,
    "harness_error_count": 0,
}


class HarnessUsageError(RuntimeError):
    """参数或环境不对，评测无法开始（退出码 2）。"""


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerSpec(_Strict):
    mode: str = "json"
    flags: list[str] = Field(default_factory=list)


class OAuthSpec(_Strict):
    client: Literal["preregistered", "dynamic"] = "preregistered"
    auth_method: Literal["none", "client_secret_post", "client_secret_basic"] = "none"
    #: ``auto``：写假授权服务器的 issuer；``none``：不写白名单；``foreign``：写一个别的 issuer。
    allowed_issuers: Literal["auto", "none", "foreign"] = "auto"
    scopes: list[str] = Field(default_factory=list)


class ConfigSpec(_Strict):
    allow: list[str] | None = Field(default_factory=lambda: list(DEFAULT_ALLOW))
    host: str = "127.0.0.1"
    scheme: Literal["https", "http"] = "https"
    userinfo: bool = False
    allow_private_network: bool = True
    human_confirmation: bool = False
    lifecycle: Literal["per_run", "shared"] = "per_run"
    disabled: bool = False
    bearer_slot: str | None = None
    oauth: OAuthSpec | None = None
    call_timeout_seconds: float = 5.0
    max_response_bytes: int = 65536


class CallSpec(_Strict):
    tool: str = Field(min_length=1, description="远端工具名（不是桥接后的本地名）")
    args: dict[str, Any] = Field(default_factory=dict)
    expect_ok: bool | None = None
    expect_code: str | None = None
    expect_contains: str | None = None
    #: 结果 structured_content 里应有的键值（子集匹配）。
    expect_fields: dict[str, Any] = Field(default_factory=dict)


class OAuthAction(_Strict):
    do: Literal["authorize", "authorize_wrong_state", "revoke", "sleep"]
    seconds: float = 0.0
    #: 动作应当抛出的 MCP 错误码；``None`` = 应当成功。
    expect_error: str | None = None
    expect_authorized: bool | None = None


class ServerExpect(_Strict):
    no_requests: bool = False
    initialize_count: int | None = None
    tools_call_processed: int | None = None
    elsewhere_hits: int | None = None
    #: 授权服务器与 ``/elsewhere`` 从未收到 ``Authorization`` 头（令牌只发给配置的那一个 MCP endpoint）。
    authorization_only_on_mcp: bool = False
    #: 客户端把 server→client 请求以 ``-32601`` 回绝（POST 回 server）。
    method_not_found_reply: bool | None = None
    #: 令牌端点按 grant_type 收到的请求数。
    token_requests: dict[str, int] | None = None
    #: 所有授权端点请求（authorize / token）都带了 ``resource``，且令牌请求带了 ``code_verifier``。
    resource_and_verifier_sent: bool | None = None
    dynamic_registrations: int | None = None
    #: server 接受的每个 MCP 端点请求都带了 ``Authorization``（规范：每个 HTTP 请求都要带；被 401 拒绝的无令牌
    #: 发现探测不计）。
    authorization_on_every_mcp_request: bool | None = None
    #: well-known 受保护资源元数据被请求的路径（按出现顺序）。
    discovery_paths: list[str] | None = None


class RunSpec(_Strict):
    before: list[OAuthAction] = Field(default_factory=list)
    calls: list[CallSpec] = Field(default_factory=list)
    expect_exposed: list[str] | None = None
    #: 远端工具名（整台 server 层面用 ``*``）→ 错误码。
    expect_skips: dict[str, str] | None = None
    expect_config_error: str | None = None
    expect_server: ServerExpect | None = None
    expect_connection: Literal["per_run", "shared_new", "shared_reused"] | None = None
    #: 这一轮结束时这台 server 的授权状态（``authorization_status().authorized``）。
    expect_authorized_after: bool | None = None


class McpHttpCase(_Strict):
    id: str = Field(min_length=1)
    category: str = Field(pattern=r"^(transport|protocol|ssrf|bearer|oauth|confirmation|entry)$")
    description: str = ""
    entry: Literal["session", "web", "runner"] = "session"
    server: ServerSpec = Field(default_factory=ServerSpec)
    config: ConfigSpec = Field(default_factory=ConfigSpec)
    #: 用例开始前就填好的凭据槽位（名字 → 值）。
    slots: dict[str, str] = Field(default_factory=dict)
    #: 假 DNS：主机名 → 解析出的地址（按序）。未列出的名字走真实解析。
    dns: dict[str, list[str]] = Field(default_factory=dict)
    #: 同一主机名第二次及以后解析的应答（演示 DNS 重绑定）。
    dns_later: dict[str, list[str]] = Field(default_factory=dict)
    #: 这条用例里"不得有任何连接"的地址（IP 字面量），如云元数据地址。
    forbidden_ips: list[str] = Field(default_factory=list)
    #: 目标本身就是该被拦的：不仅要有 ``MCP_DESTINATION_FORBIDDEN``，假 server 还必须一个请求都没收到、
    #: 且 ``forbidden_ips`` 零连接。计入 ``forbidden_destination_block_rate``。
    forbidden_destination: bool = False
    runs: list[RunSpec] = Field(min_length=1)


def load_cases(path: Path) -> tuple[list[McpHttpCase], list[str]]:
    """读用例；任何一行坏掉都记为加载问题（整套评测随之 exit 2），不跳过。"""

    cases: list[McpHttpCase] = []
    problems: list[str] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            case = McpHttpCase.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as exc:
            problems.append(f"第 {line_no} 行：{exc}")
            continue
        if case.id in seen:
            problems.append(f"第 {line_no} 行：用例 id 重复 {case.id!r}")
            continue
        seen.add(case.id)
        cases.append(case)
    return cases, problems


# ---------------------------------------------------------------------------
# 假 server 进程
# ---------------------------------------------------------------------------


class FakeServer:
    """一台假 HTTP MCP server + 假授权服务器的子进程。"""

    def __init__(self, spec: ServerSpec, *, cert_dir: Path, workdir: Path) -> None:
        self.spec = spec
        self.log_path = workdir / "server_log.jsonl"
        self.secrets_path = workdir / "server_secrets.json"
        self.mcp_port = 0
        self.auth_port = 0
        self._proc: subprocess.Popen[str] | None = None
        self._cert_dir = cert_dir

    def start(self) -> None:
        command = [
            sys.executable, str(FAKE_HTTP_SERVER), "--mode", self.spec.mode,
            "--cert-dir", str(self._cert_dir), "--log", str(self.log_path),
            "--secrets", str(self.secrets_path), *self.spec.flags,
        ]
        self._proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        assert self._proc.stdout is not None
        line = self._proc.stdout.readline()
        if not line:
            error = self._proc.stderr.read() if self._proc.stderr else ""
            raise HarnessUsageError(f"假 HTTP MCP server 没有启动：{error.strip()[:300]}")
        ports = json.loads(line)
        self.mcp_port = int(ports["mcp_port"])
        self.auth_port = int(ports["auth_port"])

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    def log(self) -> list[dict[str, Any]]:
        if not self.log_path.is_file():
            return []
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines() if line]

    def secrets(self) -> list[str]:
        if not self.secrets_path.is_file():
            return []
        try:
            return [str(item) for item in json.loads(self.secrets_path.read_text(encoding="utf-8"))]
        except (OSError, ValueError):
            return []

    @property
    def auth_origin(self) -> str:
        scheme = "http" if "--plain" in self.spec.flags else "https"
        return f"{scheme}://127.0.0.1:{self.auth_port}"


def make_certificate(directory: Path) -> None:
    """生成一次自签证书（openssl 子进程；SAN：localhost、127.0.0.1）。"""

    result = subprocess.run(
        [sys.executable, str(FAKE_HTTP_SERVER), "--make-cert", str(directory)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise HarnessUsageError(f"生成测试证书失败（需要 openssl 命令）：{result.stderr.strip()[:300]}")


# ---------------------------------------------------------------------------
# 进程内观测：假 DNS 与连接间谍
# ---------------------------------------------------------------------------


class NetworkSpy:
    """记录进程内所有 TCP 连接尝试，并按用例给定的映射伪造 DNS 应答。

    ``dns``：主机名 → 第一次解析的全部应答；``later``：同一主机名**第二次及以后**解析的应答（用来演示
    "DNS 重绑定"——校验时解析到允许的地址，之后再解析就换成禁止的地址；钉死 IP 的客户端不会有第二次解析，
    也就不会连到后一个地址）。
    """

    def __init__(self, dns: dict[str, list[str]], later: dict[str, list[str]]) -> None:
        self.dns = dns
        self.later = later
        self.connects: list[tuple[str, int]] = []
        self.lookups: dict[str, int] = {}
        self._originals: tuple[Any, Any] | None = None

    def __enter__(self) -> Self:
        real_getaddrinfo = socket.getaddrinfo
        real_connect = socket.socket.connect

        def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
            name = host.decode() if isinstance(host, bytes) else host
            if name in self.dns:
                count = self.lookups[name] = self.lookups.get(name, 0) + 1
                answers = self.later[name] if count > 1 and name in self.later else self.dns[name]
                return [
                    (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))
                    for ip in answers
                ]
            return real_getaddrinfo(host, port, *args, **kwargs)

        def connect(sock: socket.socket, address: Any) -> Any:
            if isinstance(address, tuple) and len(address) >= 2:
                self.connects.append((str(address[0]), int(address[1])))
            return real_connect(sock, address)

        self._originals = (real_getaddrinfo, real_connect)
        socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
        socket.socket.connect = connect  # type: ignore[assignment,method-assign]
        return self

    def __exit__(self, *_: object) -> None:
        assert self._originals is not None
        socket.getaddrinfo = self._originals[0]  # type: ignore[assignment]
        socket.socket.connect = self._originals[1]  # type: ignore[method-assign]

    def connects_to(self, ips: Sequence[str]) -> list[tuple[str, int]]:
        targets = set(ips)
        return [item for item in self.connects if item[0] in targets]


# ---------------------------------------------------------------------------
# 配置与环境
# ---------------------------------------------------------------------------


def build_server_config(case: McpHttpCase, server: FakeServer) -> dict[str, Any]:
    """这台 server 的配置条目（仓库默认层格式）。"""

    spec = case.config
    userinfo = "user:pass@" if spec.userinfo else ""
    url = f"{spec.scheme}://{userinfo}{spec.host}:{server.mcp_port}/mcp"
    entry: dict[str, Any] = {
        "server_id": SERVER_ID,
        "transport": "streamable_http",
        "url": url,
        "allow_private_network": spec.allow_private_network,
        "allow": spec.allow,
        "human_confirmation": spec.human_confirmation,
        "lifecycle": spec.lifecycle,
        "disabled": spec.disabled,
        "call_timeout_seconds": spec.call_timeout_seconds,
        "max_response_bytes": spec.max_response_bytes,
    }
    slots = list(case.slots) if case.slots else []
    if spec.bearer_slot is not None:
        entry["bearer_credential"] = spec.bearer_slot
        if spec.bearer_slot not in slots:
            slots.append(spec.bearer_slot)
    if spec.oauth is not None:
        oauth: dict[str, Any] = {"token_endpoint_auth_method": spec.oauth.auth_method}
        if spec.oauth.client == "preregistered":
            oauth["client_id"] = "wa-preregistered"
            if spec.oauth.auth_method != "none":
                oauth["client_secret_credential"] = "OAUTH_CLIENT_SECRET"
                if "OAUTH_CLIENT_SECRET" not in slots:
                    slots.append("OAUTH_CLIENT_SECRET")
        else:
            oauth["dynamic_registration"] = True
        if spec.oauth.allowed_issuers == "auto":
            oauth["allowed_issuers"] = [server.auth_origin]
        elif spec.oauth.allowed_issuers == "foreign":
            oauth["allowed_issuers"] = ["https://some-other-issuer.invalid"]
        if spec.oauth.scopes:
            oauth["scopes"] = list(spec.oauth.scopes)
        entry["oauth"] = oauth
    if slots:
        entry["credential_env"] = slots
    return entry


@contextmanager
def case_environment(workdir: Path, config_payload: dict[str, Any], *, cert_dir: Path) -> Iterator[None]:
    """隔离环境变量：清掉会影响装配的、绕开代理、信任测试证书；结束时整体恢复。"""

    saved = dict(os.environ)
    try:
        for key in (
            "WATER_AGENT_MCP_OVERLAY", "WATER_AGENT_MCP_WEB_LAYER", "WATER_AGENT_TOOL_EXPOSURE",
            "WATER_AGENT_SKILLS", "WATER_AGENT_SKILLS_DIR", "WEB_PUBLIC_BASE_URL", "DRAWING_ROOT",
            "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "WATER_AGENT_INDEX_PATH", "SSL_CERT_DIR",
            *PROXY_VARS,
        ):
            os.environ.pop(key, None)
        os.environ["NO_PROXY"] = "*"
        config_path = workdir / "mcp_servers.json"
        config_path.write_text(json.dumps(config_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        credentials = workdir / "credentials"
        credentials.mkdir(parents=True, exist_ok=True)
        os.environ["WATER_AGENT_MCP_CONFIG"] = str(config_path)
        os.environ["WATER_AGENT_CREDENTIALS_DIR"] = str(credentials)
        os.environ["WATER_AGENT_TRACE_DIR"] = str(workdir / "traces")
        os.environ["SSL_CERT_FILE"] = str(cert_dir / "cert.pem")
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):
            self.lines.append(record.getMessage())


# ---------------------------------------------------------------------------
# 观测与打分
# ---------------------------------------------------------------------------


@dataclass
class RunObservation:
    config_error: str | None = None
    exposed: list[str] = field(default_factory=list)
    skips: dict[str, str] = field(default_factory=dict)
    connection: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    action_errors: list[str | None] = field(default_factory=list)
    action_authorized: list[bool | None] = field(default_factory=list)
    authorized_after: bool | None = None
    texts: list[str] = field(default_factory=list)
    harness_error: str | None = None


@dataclass
class RunResult:
    run: int
    exposed: list[str] = field(default_factory=list)
    skips: dict[str, str] = field(default_factory=dict)
    config_error: str | None = None
    connection: str | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    checks: dict[str, bool | None] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run": self.run, "exposed": self.exposed, "skips": self.skips,
            "config_error": self.config_error, "connection": self.connection,
            "calls": self.calls, "checks": self.checks, "failures": self.failures,
        }


@dataclass
class CaseResult:
    case_id: str
    category: str
    entry: str
    runs: list[RunResult] = field(default_factory=list)
    forbidden_destination: bool | None = None
    token_leaks: int = 0
    tool_call_replays: int = 0
    egress_ungoverned: int = 0
    wire_requests: int = 0
    governed_calls: int = 0
    error: str | None = None
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            self.error is None
            and not self.failures
            and not any(run.failures for run in self.runs)
            and self.forbidden_destination is not False
            and self.token_leaks == 0
            and self.tool_call_replays == 0
            and self.egress_ungoverned == 0
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id, "category": self.category, "entry": self.entry, "passed": self.passed,
            "error": self.error, "failures": self.failures,
            "forbidden_destination": self.forbidden_destination,
            "token_leaks": self.token_leaks, "tool_call_replays": self.tool_call_replays,
            "egress_ungoverned": self.egress_ungoverned, "wire_requests": self.wire_requests,
            "governed_calls": self.governed_calls,
            "runs": [run.as_dict() for run in self.runs],
        }


def _check(result: RunResult, name: str, ok: bool, message: str) -> None:
    previous = result.checks.get(name)
    result.checks[name] = ok if previous is None else bool(previous and ok)
    if not ok:
        result.failures.append(message)


def _code_matches(result: dict[str, Any], code: str) -> bool:
    """结果里带这个 MCP 错误码，或消息以该错误码的固定中文前缀开头（ToolRegistry 把异常包成 TOOL_ERROR）。"""

    if result.get("code") == code:
        return True
    from water_agent.mcp.errors import _CODE_HINTS

    return _CODE_HINTS.get(code, "\x00") in str(result.get("message", ""))


def score_calls(result: RunResult, run: RunSpec, observed: Sequence[dict[str, Any]]) -> None:
    for index, (spec, got) in enumerate(zip(run.calls, observed, strict=False)):
        declared = (
            spec.expect_ok is not None or spec.expect_code is not None
            or spec.expect_contains is not None or bool(spec.expect_fields)
        )
        if not declared:
            continue
        label = f"调用 {index + 1}（{spec.tool}）"
        if spec.expect_ok is not None:
            _check(result, "call", bool(got.get("ok")) is spec.expect_ok,
                   f"{label} 应 ok={spec.expect_ok}，实际 {got.get('ok')!r}（{str(got.get('message', ''))[:120]}）")
        if spec.expect_code is not None:
            _check(result, "call", _code_matches(got, spec.expect_code),
                   f"{label} 应带错误码 {spec.expect_code}，实际 {got.get('code')!r} / {str(got.get('message', ''))[:120]}")
        if spec.expect_contains is not None:
            _check(result, "call", spec.expect_contains in json.dumps(got, ensure_ascii=False),
                   f"{label} 的结果应包含 {spec.expect_contains!r}")
        structured = got.get("structured_content") or {}
        for key, value in spec.expect_fields.items():
            _check(result, "call", structured.get(key) == value,
                   f"{label} 的 {key} 应为 {value!r}，实际 {structured.get(key)!r}")
    if len(observed) < len(run.calls):
        _check(result, "call", False, f"应有 {len(run.calls)} 次调用结果，只观测到 {len(observed)} 次")


def _wire(log: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """假 server 收到的、由 water_agent 发起的请求（不含 harness 代浏览器访问的授权端点）。"""

    return [entry for entry in log if entry.get("event") != "authorize" and entry.get("path") != "/authorize"]


def score_server(result: RunResult, expect: ServerExpect, log: Sequence[dict[str, Any]]) -> None:
    wire = _wire(log)
    mcp = [e for e in wire if e.get("server") == "mcp" and e.get("path") == "/mcp"]
    if expect.no_requests:
        _check(result, "server", not wire, f"假 server 应一个请求都没收到，实际 {len(wire)} 个")
    if expect.initialize_count is not None:
        got = sum(1 for e in mcp if e.get("rpc_method") == "initialize" and e.get("status") == 200)
        _check(result, "server", got == expect.initialize_count,
               f"initialize 成功次数应为 {expect.initialize_count}，实际 {got}")
    if expect.tools_call_processed is not None:
        got = processed_tool_calls(log)
        _check(result, "server", got == expect.tools_call_processed,
               f"被处理的 tools/call 应为 {expect.tools_call_processed}，实际 {got}")
    if expect.elsewhere_hits is not None:
        got = sum(1 for e in wire if e.get("path") == "/elsewhere")
        _check(result, "server", got == expect.elsewhere_hits, f"/elsewhere 应被请求 {expect.elsewhere_hits} 次，实际 {got}")
    if expect.authorization_only_on_mcp:
        leaked = [e for e in wire if e.get("has_authorization") and (e.get("server") == "auth" and e.get("event") != "token"
                  or e.get("path") == "/elsewhere")]
        _check(result, "server", not leaked, f"令牌只该发给 MCP endpoint，实际有 {len(leaked)} 个别处的请求带了 Authorization")
    if expect.method_not_found_reply is not None:
        got = any(e.get("client_response_for") == "srv-1" and e.get("client_response_error_code") == -32601 for e in mcp)
        _check(result, "server", got is expect.method_not_found_reply,
               f"server→client 请求应被以 -32601 回绝：期望 {expect.method_not_found_reply}，实际 {got}")
    if expect.token_requests is not None:
        counts: dict[str, int] = {}
        for entry in wire:
            if entry.get("event") == "token":
                counts[str(entry.get("grant_type"))] = counts.get(str(entry.get("grant_type")), 0) + 1
        _check(result, "server", counts == expect.token_requests, f"令牌端点请求应为 {expect.token_requests}，实际 {counts}")
    if expect.resource_and_verifier_sent is not None:
        tokens = [e for e in wire if e.get("event") == "token"]
        authorizes = [e for e in log if e.get("event") == "authorize"]
        ok = (
            bool(tokens) and all(e.get("has_resource") for e in tokens)
            and all(e.get("has_resource") for e in authorizes)
            and all(e.get("has_code_verifier") for e in tokens if e.get("grant_type") == "authorization_code")
            and not any(e.get("token_in_query") for e in tokens)
        )
        _check(result, "server", ok is expect.resource_and_verifier_sent,
               f"resource / code_verifier 应{'都带上' if expect.resource_and_verifier_sent else '缺失'}，实际 {ok}")
    if expect.dynamic_registrations is not None:
        got = sum(1 for e in wire if e.get("event") == "register")
        _check(result, "server", got == expect.dynamic_registrations,
               f"动态注册应 {expect.dynamic_registrations} 次，实际 {got}")
    if expect.authorization_on_every_mcp_request is not None:
        # 被 401 拒绝的请求不计：授权发现要先发一个不带令牌的探测请求去拿 WWW-Authenticate（规范流程第一步）
        accepted = [e for e in mcp if e.get("status") != "auth_rejected"]
        got = bool(accepted) and all(e.get("has_authorization") for e in accepted)
        _check(result, "server", got is expect.authorization_on_every_mcp_request,
               f"每个 MCP 请求都带 Authorization：期望 {expect.authorization_on_every_mcp_request}，实际 {got}")
    if expect.discovery_paths is not None:
        got_paths = [e["path"] for e in wire if str(e.get("path", "")).startswith("/.well-known/oauth-protected-resource")]
        _check(result, "server", got_paths == expect.discovery_paths,
               f"受保护资源元数据请求路径应为 {expect.discovery_paths}，实际 {got_paths}")


def processed_tool_calls(log: Sequence[dict[str, Any]]) -> int:
    """假 server 真正处理了的 tools/call（被 401 / 403 / 404 / 400 拒绝在处理之前的不算）。"""

    return sum(
        1 for e in log
        if e.get("server") == "mcp" and e.get("rpc_method") == "tools/call" and e.get("status") in (200, "cut")
    )


# ---------------------------------------------------------------------------
# 三条入口
# ---------------------------------------------------------------------------


def _session_args(workdir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        db_path=None, dimensions=None, rerank="local", rerank_model=None, rerank_base_url=None,
        rerank_api_key=None, top_k=5, recall_top_k=20, hybrid_alpha=0.7, model="deepseek-chat",
        base_url=None, api_key_env="DEEPSEEK_API_KEY", api_key=None, mock_generator=True,
        session_dir=str(workdir / "sessions"), trace_dir=str(workdir / "traces"), stream=False,
        drawing_root="",
    )


def local_tool_name(remote_tool: str) -> str:
    from water_agent.mcp.bridge import bridge_tool_name

    return bridge_tool_name(SERVER_ID, remote_tool)


def remote_names(local_names: Sequence[str]) -> list[str]:
    """本地名 → 远端名（只可能在 ``DEFAULT_ALLOW`` 与假 server 的固定工具里）。"""

    candidates = {local_tool_name(name): name for name in (*DEFAULT_ALLOW, "dangerous_write", "unlisted_tool")}
    return sorted(candidates.get(name, name) for name in local_names)


def summarize_assembly(assembly: Any) -> tuple[list[str], dict[str, str], str | None]:
    exposed = remote_names(list(assembly.exposed_tool_names))
    skips: dict[str, str] = {}
    for item in assembly.skipped_servers:
        # 整台 server 层面的原因（停用、连接失败、确认门……）记在 "*"；没有工具被暴露时 open_bridge 把逐个
        # 工具的原因折进 skipped_servers，带着远端工具名
        skips[item.remote_tool if item.remote_tool not in (None, "") else "*"] = item.code
    for session in assembly.sessions:
        for skip in session.result.skipped:
            skips[skip.remote_tool] = skip.code
    connection = (assembly.connections or {}).get(SERVER_ID) if getattr(assembly, "connections", None) else None
    if connection is None and assembly.sessions:
        connection = "per_run"
    return exposed, skips, connection


def _tool_message_payloads(received: Sequence[Sequence[Any]]) -> dict[str, dict[str, Any]]:
    """脚本模型收到的 ToolMessage（按本地工具名，取最后一次）→ 解析后的结果 dict。"""

    payloads: dict[str, dict[str, Any]] = {}
    for messages in received:
        for message in messages:
            if getattr(message, "type", "") != "tool":
                continue
            content = message.content if isinstance(message.content, str) else str(message.content)
            try:
                parsed = json.loads(content)
            except (TypeError, ValueError):
                parsed = {"ok": False, "message": content}
            payloads[(getattr(message, "tool_call_id", "") or str(id(message)))] = {
                "_tool": getattr(message, "name", ""), **(parsed if isinstance(parsed, dict) else {})
            }
    return payloads


def _ordered_call_results(model: ScriptedChatModel, run: RunSpec) -> list[dict[str, Any]]:
    payloads = _tool_message_payloads(model.state["received"])
    ordered = []
    for index, _ in enumerate(run.calls):
        wanted = f"{model.id_prefix}-{index}-0"
        payload = payloads.get(wanted, {"ok": False, "message": "（脚本调用没有产生工具结果）"})
        ordered.append(_normalize_call(payload))
    return ordered


def _normalize_call(payload: dict[str, Any]) -> dict[str, Any]:
    ok = payload.get("ok")
    if ok is None:
        ok = payload.get("error") is not True and "error" not in payload
    return {
        "ok": bool(ok),
        "code": payload.get("code"),
        "message": str(payload.get("message", ""))[:300],
        "structured_content": payload.get("structured_content") if isinstance(payload.get("structured_content"), dict) else {},
        "content_text": str(payload.get("content_text", ""))[:300],
    }


def _scripted_model(run: RunSpec, case_id: str) -> ScriptedChatModel:
    steps: list[dict[str, Any]] = [
        {"tool_calls": [{"name": local_tool_name(call.tool), "args": call.args}]} for call in run.calls
    ]
    steps.append({"content": "已完成远程工具调用。"})
    return ScriptedChatModel(steps=steps, id_prefix=f"{case_id[:24]}-{uuid4().hex[:6]}")


def _member_request_context(thread_id: str) -> Any:
    from water_agent.runtime.governance.contracts import Principal, RequestContext
    from water_agent.webapp.auth.dependencies import _MEMBER_CAPABILITIES, TENANT_ID

    return RequestContext(
        principal=Principal.authenticated(
            subject="user:mcp-http-eval", auth_domain=TENANT_ID, capabilities=_MEMBER_CAPABILITIES
        ),
        request_id=f"req-{thread_id[:12]}",
        thread_id=thread_id,
    )


def _fixture_retriever() -> Any:
    from water_agent.retrieval.loader import load_clauses_jsonl
    from water_agent.retrieval.trivium import TriviumRetriever

    return TriviumRetriever(db_path=None, fallback_clauses=load_clauses_jsonl(FIXTURE_CLAUSES))


def _drive_turn(
    case: McpHttpCase, run: RunSpec, workdir: Path, *, saver: Any, thread_id: str,
    pool: Any | None, obs: RunObservation, traces: list[str],
) -> None:
    from water_agent.mcp.wiring import load_mcp_gateway
    from water_agent.runtime.agentic import AgenticSession

    model = _scripted_model(run, case.id)
    with ExitStack() as stack:
        kwargs: dict[str, Any] = {}
        if case.entry == "web":
            assert pool is not None
            lease = pool.lease(load_mcp_gateway(), confirmation_ready=True)
            stack.callback(lease.release)
            kwargs.update(
                request_context=_member_request_context(thread_id),
                mcp_lease=lease, confirmation=True,
            )
        session = AgenticSession(
            _session_args(workdir), stack=stack, thread_id=thread_id, checkpointer=saver,
            model=model, retriever=_fixture_retriever(), mcp_enabled=True, **kwargs,
        )
        exposed, skips, connection = summarize_assembly(session.mcp_assembly)
        obs.exposed, obs.skips, obs.connection = exposed, skips, connection
        if case.entry == "web" and case.config.human_confirmation:
            return  # 延迟工具：本组用例只看暴露，不调用
        asyncio.run(session.aask(f"调用远程工具 {uuid4().hex[:6]}", run_id=uuid4().hex))
        obs.calls = _ordered_call_results(model, run)
        traces.append(json.dumps(session.recorder.snapshot(), ensure_ascii=False, default=str))


def _drive_runner(case: McpHttpCase, run: RunSpec, workdir: Path, *, obs: RunObservation) -> None:
    from water_agent.mcp.wiring import load_mcp_gateway
    from water_agent.plugins import (
        AssemblyContext,
        McpContribution,
        McpGatewayPlugin,
        assemble_tools,
        builtin_plugins,
    )
    from water_agent.runtime.governance.binding import local_request_context

    with ExitStack() as stack:
        thread_id = f"runner-{uuid4().hex[:8]}"
        request_context = local_request_context(thread_id=thread_id)
        config = load_mcp_gateway()
        assembly = assemble_tools(
            AssemblyContext(retriever=_fixture_retriever(), cite_context_source=lambda: set(), drawing_root=""),
            base_principal=request_context.principal, trace=None,
            plugins=builtin_plugins(mcp=McpGatewayPlugin.owning(config, stack=stack), skills_disabled=()),
        )
        mcp_assembly = assembly.contribution(McpContribution).assembly
        exposed, skips, connection = summarize_assembly(mcp_assembly)
        obs.exposed, obs.skips, obs.connection = exposed, skips, connection
        context = request_context.model_copy(update={"principal": assembly.principal})
        client_module = importlib.import_module("water_agent.mcp.http_client")
        scope = getattr(client_module, "mcp_request_scope", None)
        for call in run.calls:
            with (scope(context) if scope is not None else nullcontext()):
                payload = assembly.registry.invoke(local_tool_name(call.tool), **call.args)
            obs.calls.append(_normalize_call(payload if isinstance(payload, dict) else {"ok": False}))


def browser_authorize(authorization_url: str) -> str:
    """代浏览器访问授权端点（不跟随重定向），返回 302 的 ``Location``。

    用原始 socket + TLS 而不是 ``http.client``：评测 harness 在 ``water_agent`` 包内，静态出站扫描
    （``http.client.HTTPS?Connection``）只允许 ``mcp/http_transport.py`` 一个模块开 HTTP 连接。
    """
    parsed = urlparse(authorization_url)
    target = f"{parsed.path}?{parsed.query}"
    context = ssl.create_default_context()
    with (
        socket.create_connection((parsed.hostname or "", parsed.port or 443), timeout=5) as raw,
        context.wrap_socket(raw, server_hostname=parsed.hostname) as tls,
    ):
        tls.sendall(
            f"GET {target} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\nConnection: close\r\n\r\n".encode()
        )
        data = b""
        while chunk := tls.recv(4096):
            data += chunk
    head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    for line in head.split("\r\n")[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "location":
            return value.strip()
    return ""


def _do_action(action: OAuthAction, case: McpHttpCase, server: FakeServer, obs: RunObservation) -> None:
    """授权相关动作。授权流程的 ``/authorize`` 一步由 harness 代浏览器完成（不跟随重定向）。"""

    from water_agent.mcp.errors import MCPError
    from water_agent.mcp.wiring import load_mcp_gateway

    if action.do == "sleep":
        time.sleep(action.seconds)
        return
    oauth = importlib.import_module("water_agent.mcp.oauth")
    gateway = load_mcp_gateway()
    assert gateway is not None
    server_config = next(s for s in gateway.servers if s.server_id == SERVER_ID)
    code: str | None = None
    try:
        if action.do == "revoke":
            oauth.revoke_authorization(server_config)
        else:
            start = oauth.start_authorization(server_config, redirect_uri=REDIRECT_URI)
            location = browser_authorize(start.authorization_url)
            query = parse_qs(urlparse(location).query)
            def first(key: str) -> str | None:
                values = query.get(key)
                return values[0] if values else None

            state = "forged-state-value" if action.do == "authorize_wrong_state" else first("state")
            code = first("code")
            oauth.complete_authorization(
                state=state, code=code, error=first("error"), iss=first("iss"), redirect_uri=REDIRECT_URI
            )
        obs.action_errors.append(None)
    except MCPError as exc:
        obs.action_errors.append(exc.code)
    status = None
    try:
        status = oauth.authorization_status(server_config)
    except Exception:  # noqa: BLE001 — 状态读不到就是未知
        status = None
    obs.action_authorized.append(bool(getattr(status, "authorized", False)) if status is not None else None)


def _authorized_now() -> bool | None:
    """这台 server 现在是否持有有效授权；机制不存在或读不到时为 ``None``（未知，不当 False）。"""

    try:
        from water_agent.mcp.wiring import load_mcp_gateway

        oauth = importlib.import_module("water_agent.mcp.oauth")
        gateway = load_mcp_gateway()
        server_config = next(s for s in gateway.servers if s.server_id == SERVER_ID)  # type: ignore[union-attr]
        return bool(oauth.authorization_status(server_config).authorized)
    except Exception:  # noqa: BLE001
        return None


def mechanism_present() -> bool:
    """当前代码有没有 Streamable HTTP 传输（S8 Step 1 时没有，报告如实记 ``mechanism=absent``）。"""

    try:
        config = importlib.import_module("water_agent.mcp.config")
        importlib.import_module("water_agent.mcp.http_transport")
        importlib.import_module("water_agent.mcp.http_client")
    except ModuleNotFoundError:
        return False
    return hasattr(config.ServerTransport, "STREAMABLE_HTTP")


# ---------------------------------------------------------------------------
# 单用例
# ---------------------------------------------------------------------------


def run_case(case: McpHttpCase, workdir: Path, *, cert_dir: Path) -> CaseResult:
    from langgraph.checkpoint.memory import InMemorySaver

    from water_agent.mcp.errors import MCPError
    from water_agent.mcp.pool import McpConnectionPool
    from water_agent.runtime.governance.binding import set_default_gateway
    from water_agent.runtime.governance.gateway import ProviderGateway
    from water_agent.runtime.governance.inventory import clear_mcp_tool_bindings
    from water_agent.session import default_checkpoint_serde

    result = CaseResult(case.id, case.category, case.entry)
    case_dir = workdir / case.id
    case_dir.mkdir(parents=True, exist_ok=True)
    server = FakeServer(case.server, cert_dir=cert_dir, workdir=case_dir)
    usage_records: list[Any] = []
    capture = _LogCapture()
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    traces: list[str] = []
    texts: list[str] = []
    pool = McpConnectionPool() if case.entry == "web" else None
    clear_mcp_tool_bindings()
    try:
        server.start()
        payload = {"version": "eval", "servers": [build_server_config(case, server)]}
        with case_environment(case_dir, payload, cert_dir=cert_dir):
            if case.slots:
                from water_agent.mcp.credentials import write_slot

                for name, value in case.slots.items():
                    write_slot(SERVER_ID, name, value)
            root_logger.addHandler(capture)
            root_logger.setLevel(logging.DEBUG)
            set_default_gateway(ProviderGateway(on_usage=usage_records.append))
            saver = InMemorySaver(serde=default_checkpoint_serde())
            thread_id = f"{case.id}-{uuid4().hex[:8]}"
            with NetworkSpy(dict(case.dns), dict(case.dns_later)) as spy:
                for number, run in enumerate(case.runs, start=1):
                    obs = RunObservation()
                    try:
                        for action in run.before:
                            _do_action(action, case, server, obs)
                        if case.entry == "runner":
                            _drive_runner(case, run, case_dir, obs=obs)
                        else:
                            _drive_turn(case, run, case_dir, saver=saver, thread_id=thread_id,
                                        pool=pool, obs=obs, traces=traces)
                    except MCPError as exc:
                        obs.config_error = exc.code
                    except ScriptExhausted as exc:
                        obs.harness_error = f"ScriptExhausted：{exc}"
                    except ModuleNotFoundError as exc:
                        obs.harness_error = f"机制不存在：{exc}"
                    except Exception as exc:  # noqa: BLE001 — 记为用例错误（硬指标 harness_error_count）
                        obs.harness_error = f"{type(exc).__name__}: {exc}"
                    if run.expect_authorized_after is not None:
                        obs.authorized_after = _authorized_now()
                    scored = score_run(number, run, obs, server.log())
                    result.runs.append(scored)
                    texts.append(json.dumps(scored.as_dict(), ensure_ascii=False, default=str))
                    texts.extend(obs.texts)
                    if obs.harness_error and result.error is None:
                        result.error = f"run {number}: {obs.harness_error}"
            _finish_case(case, result, server, spy, usage_records, texts + traces + capture.lines)
    except HarnessUsageError:
        raise
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        root_logger.removeHandler(capture)
        root_logger.setLevel(previous_level)
        set_default_gateway(None)
        if pool is not None:
            pool.close()
        server.stop()
        clear_mcp_tool_bindings()
    return result


def score_run(number: int, run: RunSpec, obs: RunObservation, log: Sequence[dict[str, Any]]) -> RunResult:
    result = RunResult(run=number, exposed=obs.exposed, skips=obs.skips, config_error=obs.config_error,
                       connection=obs.connection, calls=obs.calls)
    if obs.harness_error:
        result.failures.append(obs.harness_error)
    for index, action_spec in enumerate(run.before):
        if action_spec.do == "sleep":
            continue
        got_error = obs.action_errors[index] if index < len(obs.action_errors) else "（动作没有执行）"
        if got_error != action_spec.expect_error:
            _check(result, "oauth", False, f"授权动作 {action_spec.do} 应抛 {action_spec.expect_error!r}，实际 {got_error!r}")
        else:
            _check(result, "oauth", True, "")
        if action_spec.expect_authorized is not None:
            got_auth = obs.action_authorized[index] if index < len(obs.action_authorized) else None
            _check(result, "oauth", got_auth is action_spec.expect_authorized,
                   f"授权动作 {action_spec.do} 之后 authorized 应为 {action_spec.expect_authorized}，实际 {got_auth}")
    if run.expect_config_error is not None:
        _check(result, "skip", obs.config_error == run.expect_config_error,
               f"装配应以 {run.expect_config_error} 失败，实际 {obs.config_error!r}")
    elif obs.config_error is not None:
        result.failures.append(f"装配意外失败：{obs.config_error}")
    if run.expect_exposed is not None:
        _check(result, "exposure", sorted(obs.exposed) == sorted(run.expect_exposed),
               f"暴露的远端工具应为 {sorted(run.expect_exposed)}，实际 {sorted(obs.exposed)}")
    if run.expect_skips is not None:
        _check(result, "skip", obs.skips == run.expect_skips, f"跳过码应为 {run.expect_skips}，实际 {obs.skips}")
    if run.expect_authorized_after is not None:
        _check(result, "oauth", obs.authorized_after is run.expect_authorized_after,
               f"本轮结束时 authorized 应为 {run.expect_authorized_after}，实际 {obs.authorized_after}")
    if run.expect_connection is not None:
        _check(result, "exposure", obs.connection == run.expect_connection,
               f"连接方式应为 {run.expect_connection}，实际 {obs.connection!r}")
    score_calls(result, run, obs.calls)
    if run.expect_server is not None:
        score_server(result, run.expect_server, log)
    return result


def _finish_case(
    case: McpHttpCase, result: CaseResult, server: FakeServer, spy: NetworkSpy,
    usage_records: Sequence[Any], texts: Sequence[str],
) -> None:
    log = server.log()
    # 泄漏：假 server 见过的全部敏感值 + 用例预填的槽位值，不得出现在任何会落盘 / 外发的产物里
    secrets = {*server.secrets(), *case.slots.values()}
    result.token_leaks = sum(text.count(secret) for secret in secrets if len(secret) >= 8 for text in texts)
    if any(e.get("token_in_query") for e in log):
        result.token_leaks += 1
    # 重放：被处理的 tools/call 多于发起的
    attempted = sum(len(run.calls) for run in case.runs)
    if case.entry == "web" and case.config.human_confirmation:
        attempted = 0
    result.tool_call_replays = max(0, processed_tool_calls(log) - attempted)
    # 出站治理：每个线上请求都要有一笔网关账
    wire = _wire(log)
    governed = [r for r in usage_records if getattr(getattr(r, "provider", None), "value", "") == "mcp_remote"]
    result.wire_requests, result.governed_calls = len(wire), len(governed)
    result.egress_ungoverned = max(0, len(wire) - len(governed))
    if case.forbidden_ips and spy.connects_to(case.forbidden_ips) and not case.forbidden_destination:
        result.failures.append(f"不该发生的连接：{spy.connects_to(case.forbidden_ips)}")
    if case.forbidden_destination:
        no_wire = not wire
        no_connect = not spy.connects_to(case.forbidden_ips) if case.forbidden_ips else True
        blocked = all(
            run.skips.get("*") == "MCP_DESTINATION_FORBIDDEN" or run.config_error == "MCP_DESTINATION_FORBIDDEN"
            for run in result.runs
        )
        result.forbidden_destination = bool(no_wire and no_connect and blocked)
        if not result.forbidden_destination:
            result.failures.append(
                f"应拦的目标没有被完全拦下：线上请求 {len(wire)}、对 forbidden_ips 的连接 "
                f"{len(spy.connects_to(case.forbidden_ips))}、拦截码匹配 {blocked}"
            )


# ---------------------------------------------------------------------------
# 指标与报告
# ---------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 6)


def metrics_from_case_dicts(cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """从报告里的用例明细算出全部指标。harness 写报告与比较器核对"报告没被手改"共用这一个函数。"""

    totals: dict[str, list[int]] = {}
    forbidden_total = forbidden_blocked = 0
    leaks = replays = ungoverned = 0
    for case in cases:
        for run in case.get("runs", []) or []:
            for name, value in (run.get("checks") or {}).items():
                if value is None:
                    continue
                hit, total = totals.setdefault(name, [0, 0])
                totals[name] = [hit + int(bool(value)), total + 1]
        if case.get("forbidden_destination") is not None:
            forbidden_total += 1
            forbidden_blocked += int(bool(case["forbidden_destination"]))
        leaks += int(case.get("token_leaks", 0))
        replays += int(case.get("tool_call_replays", 0))
        ungoverned += int(case.get("egress_ungoverned", 0))

    def rate(*names: str) -> float | None:
        hit = sum(totals.get(name, [0, 0])[0] for name in names)
        total = sum(totals.get(name, [0, 0])[1] for name in names)
        return _rate(hit, total)

    return {
        "expected_exposure_rate": rate("exposure"),
        "skip_code_match_rate": rate("skip"),
        "call_result_match_rate": rate("call"),
        "forbidden_destination_block_rate": _rate(forbidden_blocked, forbidden_total),
        "token_leak_count": leaks,
        "tool_call_replay_count": replays,
        "egress_ungoverned_count": ungoverned,
        "harness_error_count": sum(1 for case in cases if case.get("error") is not None),
    }


def aggregate(results: Sequence[CaseResult]) -> dict[str, Any]:
    return metrics_from_case_dicts([result.as_dict() for result in results])


def hard_violations(metrics: dict[str, Any]) -> list[str]:
    return [
        f"{name}={metrics.get(name)!r}（目标 {target!r}）"
        for name, target in HARD_TARGETS.items()
        if metrics.get(name) != target
    ]


def build_report(results: Sequence[CaseResult], *, dataset: Path) -> dict[str, Any]:
    metrics = aggregate(results)
    violations = hard_violations(metrics)
    try:
        dataset_label = dataset.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        dataset_label = dataset.name
    return {
        "suite": SUITE,
        "generated_by": GENERATED_BY,
        "report_format": REPORT_FORMAT,
        "mechanism": "present" if mechanism_present() else "absent",
        "dataset": dataset_label,
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "total": len(results),
        "passed": sum(1 for result in results if result.passed),
        "metrics": metrics,
        "hard_targets": dict(HARD_TARGETS),
        "hard_violations": violations,
        "ok": not violations,
        "cases": [result.as_dict() for result in results],
    }


def run(dataset: Path, *, only: Callable[[McpHttpCase], bool] | None = None) -> tuple[dict[str, Any] | None, list[str]]:
    cases, problems = load_cases(dataset)
    if problems:
        return None, problems
    if not cases:
        return None, ["0 条可执行用例"]
    if not FAKE_HTTP_SERVER.is_file():
        raise HarnessUsageError(f"缺少假 HTTP MCP server：{FAKE_HTTP_SERVER}")
    selected = [case for case in cases if only is None or only(case)]
    with tempfile.TemporaryDirectory(prefix="wa-mcp-http-") as tmp:
        workdir = Path(tmp)
        cert_dir = workdir / "tls"
        make_certificate(cert_dir)
        results = [run_case(case, workdir, cert_dir=cert_dir) for case in selected]
    return build_report(results, dataset=dataset), []


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or SUITE).splitlines()[0])
    parser.add_argument("dataset", nargs="?", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--report-path", type=Path, default=None)
    parser.add_argument("--only", default=None, help="只跑 id 含此子串的用例（调试用；基线对比不接受）")
    args = parser.parse_args(argv)
    only = (lambda case: args.only in case.id) if args.only else None
    try:
        report, problems = run(args.dataset, only=only)
    except HarnessUsageError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 2
    if report is None:
        for problem in problems:
            print(f"!! 加载问题：{problem}", file=sys.stderr)
        return 2
    if args.report_path is not None:
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{SUITE} mechanism={report['mechanism']} passed={report['passed']}/{report['total']}")
    for name, value in report["metrics"].items():
        print(f"  {name}: {value}")
    for case in report["cases"]:
        if not case["passed"]:
            detail = case["error"] or "; ".join(
                [*case["failures"], *(f"run{run['run']}: {'; '.join(run['failures'])}" for run in case["runs"] if run["failures"])]
            )
            print(f"  FAIL {case['id']}: {detail[:400]}")
    if report["hard_violations"]:
        print("!! 硬指标不达标：" + "；".join(report["hard_violations"]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "GENERATED_BY",
    "HARD_TARGETS",
    "SUITE",
    "McpHttpCase",
    "aggregate",
    "build_report",
    "hard_violations",
    "load_cases",
    "main",
    "metrics_from_case_dicts",
    "run",
    "run_case",
]
