"""mcp_registry_p0：MCP 目录发现的机制层评测（P7-S14，规格 ``docs/design/17-p7-external-capabilities-harness.md`` §6）。

证明"目录取回 → 条目处理 → 提案生成"全程**不连接条目地址**，提案恒停用且全拒：

- **registry**：生产 ``RegistryCatalogClient`` / ``build_proposal`` + 进程内假目录（``tests/fixtures/fake_mcp_registry.py``，
  **线程，不起子进程**）。条目地址指向 ``EntrySink``（只数连接与字节的 TCP 监听口）；``NetworkSpy`` 另外记录进程内**每一次
  DNS 解析与 TCP 连接尝试**——"条目主机零 DNS"和"禁止目的地零字节"靠它断言，不靠"假服务收到几个请求"（条目地址本来就不是假服务，
  拿掉保护也只是连不上，指标照样绿）；
- **api**：``create_app(database=sqlite)`` + 真实鉴权与 CSRF，路由经 ``app.state.mcp_registry_overrides`` 指向假目录；
- **invariant**：AST / 常量层面能判定的事实（P7-I13、能力面、站点登记）。

**不证明**：官方 MCP 注册表的真实字段与可用性（Step 0 取得的真实响应与本协议 v1 **形态完全不同**，本步不做适配器，
``unverified_against_real_registry``）；条目与真实 server 的一致性、条目描述是否如实、运维启用并放行后该 server 的实际行为
（任何确定性规则都检不出）；字符白名单 / 注入检测只覆盖语料里的写法，**不得据此声称"目录搜索是安全的"**。

指标只从用例上报的计数器聚合；缺计数器 = 未测量（``None``），判不达标，不记 0。数据集在 S14 Step 1 冻结；之后改它必须同时
重新固化入仓基线并写明原因。退出码：0 全部硬指标达标；1 有硬指标不达标或用例执行出错；2 加载问题、0 条用例或参数错误。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import io
import json
import socket
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATASET = REPO_ROOT / "data" / "eval" / "mcp_registry_p0.jsonl"
FAKE_REGISTRY = REPO_ROOT / "tests" / "fixtures" / "fake_mcp_registry.py"

SUITE = "mcp_registry_p0"
GENERATED_BY = "water_agent.harness.mcp_registry_eval"
REPORT_FORMAT = 1
PASSWORD = "McpRegistryEvalPass2026!"
REGISTRY_HOST = "registry.eval.test"
#: 当前用例"条目地址"探针的端口：只供 ``tests/test_mcp_registry_eval.py`` 的反证补丁使用（模拟有人加了探测）。
_ACTIVE_SINK_PORT = 0

#: 硬指标与目标值（harness 文档 §6）。速率为 ``None``（分母为 0、未测量）一律判不达标：未测量不是通过。
HARD_TARGETS: dict[str, float | int] = {
    "remote_only_proposal_rate": 1.0,
    "proposal_disabled_no_allow_rate": 1.0,
    "bytes_to_entry_urls": 0,
    "dns_lookups_for_entry_hosts": 0,
    "secret_drop_rate": 1.0,
    "unsafe_entry_proposed_count": 0,
    "proposal_hash_determinism_rate": 1.0,
    "registry_failure_fail_closed_rate": 1.0,
    "non_admin_access_count": 0,
    "benign_entry_proposed_rate": 1.0,
    "benign_text_false_reject_count": 0,
    "harness_error_count": 0,
}


class HarnessUsageError(RuntimeError):
    """参数或环境不对，评测无法开始（退出码 2）。"""


class _Loose(BaseModel):
    """数据集按冻结时的形状读；未知键直接报加载问题（字段改名不会让用例静默变空）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RegistryCase(_Loose):
    id: str = Field(min_length=1)
    suite: str
    category: str
    layer: str
    description: str = ""
    modes: str = "ok"
    allow_private: bool = True
    op: str
    expect: dict[str, Any] = Field(default_factory=dict)


def load_cases(path: Path) -> tuple[list[RegistryCase], list[str]]:
    cases: list[RegistryCase] = []
    problems: list[str] = []
    seen: set[str] = set()
    if not path.is_file():
        return [], [f"数据集不存在：{path}"]
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = RegistryCase.model_validate(json.loads(line))
        except (ValueError, ValidationError) as exc:
            problems.append(f"第 {number} 行不是合法用例：{str(exc).splitlines()[0]}")
            continue
        if case.id in seen:
            problems.append(f"用例 id 重复：{case.id}")
            continue
        seen.add(case.id)
        cases.append(case)
    return cases, problems


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    id: str
    category: str
    layer: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(check["ok"] for check in self.checks)

    def count(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + int(amount)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": "" if ok else detail})
        return bool(ok)

    def metric(self, name: str, ok: bool, detail: str = "") -> bool:
        """速率类计数：分母 ``<name>_total`` 加一，通过则分子 ``<name>_ok`` 加一，同时记一条检查。"""

        self.count(f"{name}_total")
        self.count(f"{name}_ok", int(bool(ok)))
        return self.check(name, ok, detail)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "layer": self.layer,
            "passed": self.passed,
            "error": self.error,
            "checks": self.checks,
            "counters": dict(sorted(self.counters.items())),
        }


# ---------------------------------------------------------------------------
# 假目录与观测
# ---------------------------------------------------------------------------


def fake_module() -> Any:
    spec = importlib.util.spec_from_file_location("fake_mcp_registry_eval", FAKE_REGISTRY)
    if spec is None or spec.loader is None:
        raise HarnessUsageError(f"缺少假目录：{FAKE_REGISTRY}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class NetworkSpy:
    """记录本进程里**每一次** DNS 解析与 TCP 连接尝试；非回环地址的连接一律拒绝（评测从不真的联网）。

    ``lookups``：被解析过的主机名（含 ``getaddrinfo``）；``connects``：被连接过的 (地址, 端口)。目录地址（回环假服务）会出现在
    这里——断言只看**条目主机**与**条目端口**有没有出现。注入的 ``resolver`` 不经 ``getaddrinfo``，所以对目录主机另有一个
    只认目录域名的解析器（``directory_resolver``）：条目主机若被解析，会走到它并被记录。
    """

    _LOOPBACK = frozenset({"127.0.0.1", "::1"})

    def __init__(self) -> None:
        self.lookups: list[str] = []
        self.connects: list[tuple[str, int]] = []
        self._real: tuple[Any, Any] | None = None

    def __enter__(self) -> Self:
        real_getaddrinfo = socket.getaddrinfo
        real_connect = socket.socket.connect
        spy = self

        def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
            spy.lookups.append(host.decode() if isinstance(host, bytes) else str(host))
            return real_getaddrinfo(host, port, *args, **kwargs)

        def connect(sock: Any, address: Any) -> Any:
            if isinstance(address, tuple) and len(address) >= 2:
                host = str(address[0])
                spy.connects.append((host, int(address[1])))
                if host not in spy._LOOPBACK:
                    raise ConnectionRefusedError("mcp_registry_eval：评测不连接非回环地址")
            return real_connect(sock, address)

        self._real = (real_getaddrinfo, real_connect)
        socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
        socket.socket.connect = connect  # type: ignore[method-assign]
        return self

    def __exit__(self, *_: object) -> None:
        assert self._real is not None
        socket.getaddrinfo = self._real[0]  # type: ignore[assignment]
        socket.socket.connect = self._real[1]  # type: ignore[method-assign]

    def connects_to_port(self, port: int) -> int:
        return sum(1 for _host, number in self.connects if number == port)


class Rig:
    """一个假目录 + 条目地址探针 + 网络间谍。"""

    def __init__(self, case: RegistryCase) -> None:
        from water_agent.mcp import registry as reg
        from water_agent.runtime.governance.gateway import ProviderGateway

        fake = fake_module()
        self.reg = reg
        self.fake = fake
        self.sink = fake.EntrySink().start()
        self.server = fake.FakeMcpRegistry(case.modes, sink_url=self.sink.url).start()
        global _ACTIVE_SINK_PORT
        _ACTIVE_SINK_PORT = self.sink.port
        self.spy = NetworkSpy()
        self.resolved_hosts: list[str] = []
        self.config = reg.RegistryConfig(base_url=self.server.base_url, allow_private=case.allow_private)
        self.gateway = ProviderGateway()

    def resolver(self, host: str, port: int) -> list[str]:
        """注入的解析器：记录主机名；目录主机回环，其余一律不应被解析（解析就记一次，并给出一个不可连接的地址）。"""

        self.resolved_hosts.append(host)
        return ["127.0.0.1"] if host == "127.0.0.1" else ["203.0.113.1"]

    def client(self, *, config: Any = None) -> Any:
        return self.reg.RegistryCatalogClient(
            config or self.config,
            self.reg.registry_context("eval-admin", request_id=f"eval-{uuid4().hex[:8]}"),
            resolver=self.resolver,
            gateway=self.gateway,
        )

    def entry_contacts(self) -> tuple[int, int]:
        return self.sink.contacts, self.sink.bytes_received

    def close(self) -> None:
        self.server.stop()
        self.sink.stop()


def _entry_hosts(entry_urls: Sequence[str]) -> set[str]:
    from urllib.parse import urlsplit

    hosts: set[str] = set()
    for url in entry_urls:
        with contextlib.suppress(ValueError):
            hosts.add(urlsplit(url).hostname or "")
    hosts.discard("")
    return hosts


def _account_no_connect(case: RegistryCase, rig: Rig, result: CaseResult, *, entry_urls: Sequence[str] = ()) -> None:
    """每条 registry 用例收口时都核对：条目地址零连接零字节；条目主机零 DNS。"""

    contacts, received = rig.entry_contacts()
    result.count("no_connect_checked")
    result.count("bytes_to_entry_urls", contacts + received)
    result.check("entry_address_untouched", contacts == 0 and received == 0, f"条目地址收到 {contacts} 次连接 / {received} 字节")
    hosts = _entry_hosts(entry_urls) - {"127.0.0.1"}
    hits = sorted(host for host in rig.resolved_hosts + rig.spy.lookups if host in hosts)
    result.count("dns_lookups_for_entry_hosts", len(hits))
    result.check("entry_hosts_not_resolved", not hits, f"条目主机被解析：{hits}")


# ---------------------------------------------------------------------------
# registry 层用例
# ---------------------------------------------------------------------------

_SECRETS = (
    "QUERY_SECRET_VALUE_9", "REMOTE_HDR_SECRET_2", "ENTRY_ENV_SECRET_3", "UNKNOWN_FIELD_SECRET_1", "hunter2-USERINFO-SECRET",
)
_UPSTREAM_MARKERS = ("registry-upstream", "SECRET-BODY", "upstream.internal", "sk-live-", "127.0.0.1", "://")


def _json_blob(*objects: Any) -> str:
    return "\n".join(json.dumps(item, ensure_ascii=False, default=str) for item in objects)


def run_search(case: RegistryCase, rig: Rig, result: CaseResult) -> list[Any]:
    """op=search：期望整次失败（fail closed）或成功读到若干页。"""

    expect = case.expect
    client = rig.client()
    try:
        search = client.search()
    except rig.reg.McpRegistryError as exc:
        if expect.get("fail_closed"):
            result.metric("registry_failure_fail_closed", exc.code == expect["code"], f"得到 {exc.code}，期望 {expect['code']}")
            if expect.get("no_echo"):
                text = str(exc) + exc.message
                leaked = [marker for marker in _UPSTREAM_MARKERS if marker in text]
                result.check("error_text_has_no_upstream", not leaked, f"错误文本含 {leaked}")
            if "max_requests" in expect:
                result.check("stopped_early", len(rig.server.requests) <= expect["max_requests"], f"发了 {len(rig.server.requests)} 个请求")
            if expect.get("zero_connect_to_directory"):
                result.check("no_connect_attempt", rig.spy.connects_to_port(rig.server.port) == 0, "仍然尝试连接了目录")
        else:
            result.check("unexpected_failure", False, f"意外失败 {exc.code}")
        return []
    if expect.get("fail_closed"):
        result.metric("registry_failure_fail_closed", False, "期望整次失败，实际读到了条目")
        return list(search.entries)
    if "pages" in expect:
        result.check("pages", search.pages == expect["pages"], f"读了 {search.pages} 页")
    if "truncated" in expect:
        result.check("truncated", search.truncated is expect["truncated"], f"truncated={search.truncated}")
    if "entries" in expect:
        result.check("entries", len(search.entries) == expect["entries"], f"{len(search.entries)} 条")
    return list(search.entries)


def run_entry(case: RegistryCase, rig: Rig, result: CaseResult) -> list[Any]:
    expect = case.expect
    client = rig.client()
    search = client.search()
    entries = [item for item in search.entries if item.name == expect["name"]]
    result.check("entry_present", bool(entries), f"目录结果里没有 {expect['name']}")
    if not entries:
        return list(search.entries)
    if "count" in expect:
        result.check("entry_count", len(entries) == expect["count"], f"{len(entries)} 条")
    for entry in entries:
        view = json.dumps(entry.to_dict(), ensure_ascii=False)
        if "status" in expect:
            result.check("status", entry.status == expect["status"], f"status={entry.status}")
        if "reason" in expect:
            result.check("unsafe_reason", entry.unsafe_reason == expect["reason"], f"reason={entry.unsafe_reason}")
        if "proposable" in expect:
            result.check("proposable", entry.proposable is expect["proposable"], f"proposable={entry.proposable}")
        if entry.status == "unsafe":
            hidden = entry.to_dict()
            result.check(
                "unsafe_hides_content",
                hidden["description"] == "" and hidden["remotes"] == [] and hidden["packages"] == [],
                "unsafe 条目仍展示了描述 / 地址 / 包",
            )
        if "dropped_fields" in expect:
            result.check("dropped_fields", entry.dropped_fields == expect["dropped_fields"], f"丢弃 {entry.dropped_fields}")
        if expect.get("secret_absent"):
            leaked = [secret for secret in _SECRETS if secret in view]
            result.metric("secret_drop", not leaked, f"视图里出现 {leaked}")
        if "description_max" in expect:
            result.check("description_truncated", len(entry.description) <= expect["description_max"], f"{len(entry.description)} 字符")
        if "display_url" in expect:
            shown = entry.to_dict()["remotes"][0]["url"]
            result.check("display_url", shown == expect["display_url"], f"展示为 {shown}")
    if "neighbor_ok" in expect:
        neighbor = next((item for item in search.entries if item.name == expect["neighbor_ok"]), None)
        result.check("benign_neighbor_untouched", neighbor is not None and neighbor.status == "ok", "同页良性条目被误伤")
    return list(search.entries)


def _propose(rig: Rig, name: str, version: str, existing: Sequence[str] = ()) -> tuple[Any | None, str | None, Any]:
    """服务端重取 + 生成提案。返回 (提案 | None, 错误码 | None, 条目)。"""

    client = rig.client()
    try:
        entry = client.find(name, version)
    except rig.reg.McpRegistryError as exc:
        return None, exc.code, None
    try:
        return rig.reg.build_proposal(entry, registry_host=REGISTRY_HOST, existing_ids=existing), None, entry
    except rig.reg.McpRegistryError as exc:
        return None, exc.code, entry


def run_propose(case: RegistryCase, rig: Rig, result: CaseResult) -> list[Any]:
    expect = case.expect
    proposal, code, entry = _propose(rig, expect["name"], expect["version"], expect.get("existing_ids", ()))
    entries = [entry] if entry is not None else []
    if expect["proposed"]:
        ok = proposal is not None
        result.metric("benign_entry_proposed", ok, f"期望能提案，实际 {code}")
        if proposal is None:
            return entries
        server = json.loads(proposal.text)["servers"][0]
        result.metric("proposal_disabled_no_allow", server.get("disabled") is True and "allow" not in server, f"提案内容 {sorted(server)}")
        if "keys" in expect:
            result.check("proposal_keys", sorted(server) == sorted(expect["keys"]), f"键 {sorted(server)}")
        result.metric("remote_only_proposal", server.get("transport") == "streamable_http" and str(server.get("url", "")).startswith("https://"),
                      f"transport={server.get('transport')} url={server.get('url')}")
        if "server_id" in expect:
            result.check("server_id", proposal.server_id == expect["server_id"], f"server_id={proposal.server_id}")
        if expect.get("secret_absent"):
            blob = _json_blob(proposal.to_dict(), proposal.text)
            leaked = [secret for secret in _SECRETS if secret in blob]
            result.metric("secret_drop", not leaked, f"提案里出现 {leaked}")
        if "secrets_dropped_min" in expect:
            result.check("secrets_counted", proposal.secret_dropped >= expect["secrets_dropped_min"], f"计数 {proposal.secret_dropped}")
        if "idna" in expect:
            result.check("idna_host", proposal.idna_host == expect["idna"], f"idna_host={proposal.idna_host}")
    else:
        result.metric("remote_only_proposal", proposal is None, f"{expect['name']} 竟然生成了提案")
        result.metric("registry_failure_fail_closed", code in ("MCP_REGISTRY_NOT_PROPOSABLE", "MCP_REGISTRY_ENTRY_UNSAFE"), f"拒绝码 {code}")
        # 「这条是敌意条目」由数据集标注（labelled_unsafe），不看被测代码有没有把它标成 unsafe——否则保护一拿掉，
        # 条目不再是 unsafe，评测就看不到它本该被拒（指标退化成未测量）
        if expect.get("labelled_unsafe") or (entry is not None and entry.status == "unsafe"):
            result.count("unsafe_entry_checked")
            result.count("unsafe_entry_proposed", int(proposal is not None))
    return entries


def run_hash(case: RegistryCase, rig: Rig, result: CaseResult) -> list[Any]:
    reg = rig.reg
    name, version = case.expect["name"], case.expect["version"]
    base_entry = rig.client().find(name, version)
    first = reg.build_proposal(base_entry, registry_host=REGISTRY_HOST).proposal_hash
    again = reg.build_proposal(rig.client().find(name, version), registry_host=REGISTRY_HOST).proposal_hash
    result.metric("proposal_hash_determinism", first == again, "同一条目两次取得的哈希不同")
    other_host = reg.build_proposal(base_entry, registry_host="other.example.test").proposal_hash
    result.metric("proposal_hash_determinism", other_host != first, "改目录主机哈希不变")
    renamed = reg.RegistryEntry(
        name="weather-station-2", version=base_entry.version, status=base_entry.status, description=base_entry.description,
        remotes=base_entry.remotes, packages=base_entry.packages,
    )
    result.metric("proposal_hash_determinism", reg.build_proposal(renamed, registry_host=REGISTRY_HOST).proposal_hash != first, "改名称哈希不变")
    rig.server.modes = frozenset({"ok", "weather_moved"})
    moved = reg.build_proposal(rig.client().find(name, version), registry_host=REGISTRY_HOST).proposal_hash
    result.metric("proposal_hash_determinism", moved != first, "改 URL 哈希不变")
    rig.server.modes = frozenset({"ok", "two_versions"})
    newer = reg.build_proposal(rig.client().find(name, "1.1.0"), registry_host=REGISTRY_HOST).proposal_hash
    result.metric("proposal_hash_determinism", newer != first, "改版本哈希不变")
    return [base_entry]


def run_full_flow(case: RegistryCase, rig: Rig, result: CaseResult) -> list[Any]:
    """搜索一次，再对每一条可提案条目生成提案——整条流程里条目地址零连接、条目主机零 DNS。"""

    client = rig.client()
    search = client.search()
    entries = list(search.entries)
    proposed = 0
    for entry in entries:
        if entry.proposable:
            with contextlib.suppress(rig.reg.McpRegistryError):
                rig.reg.build_proposal(entry, registry_host=REGISTRY_HOST)
                proposed += 1
    result.check("flow_ran", len(entries) >= 4 and proposed >= 2, f"条目 {len(entries)}，提案 {proposed}")
    return entries


def run_benign_text(case: RegistryCase, rig: Rig, result: CaseResult) -> list[Any]:
    reg = rig.reg
    for text in case.expect["texts"]:
        clean = reg.disallowed_codepoints(text) == [] and reg._text_is_clean(text) == (True, "")
        result.count("benign_text_checked")
        result.count("benign_text_false_reject", int(not clean))
        result.check("benign_text_accepted", clean, f"良性文本被拒：{text[:24]}")
    return []


def run_no_writes(case: RegistryCase, rig: Rig, result: CaseResult) -> list[Any]:
    import os

    with tempfile.TemporaryDirectory(prefix="wa-mr-nw-") as tmp:
        root = Path(tmp)
        (root / "overlay.json").write_text('{"servers": []}', encoding="utf-8")
        keys = ("WATER_AGENT_MCP_OVERLAY", "WATER_AGENT_MCP_WEB_LAYER", "WATER_AGENT_CREDENTIALS_DIR")
        saved = {key: os.environ.get(key) for key in keys}
        cwd = Path.cwd()
        os.environ["WATER_AGENT_MCP_OVERLAY"] = str(root / "overlay.json")
        os.environ["WATER_AGENT_MCP_WEB_LAYER"] = str(root / "web_layer.json")
        os.environ["WATER_AGENT_CREDENTIALS_DIR"] = str(root / "cred")
        os.chdir(root)
        try:
            before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            proposal, code, entry = _propose(rig, "weather-station", "1.0.0")
            after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        finally:
            os.chdir(cwd)
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        result.check("proposal_generated", proposal is not None, f"提案失败 {code}")
        result.check("nothing_written", before == after, "提案阶段改动了覆盖层 / 工作目录")
        result.check("web_layer_absent", not (root / "web_layer.json").exists(), "Web 管理层被创建")
    return [entry] if entry is not None else []


def run_usage_unknown(case: RegistryCase, rig: Rig, result: CaseResult) -> list[Any]:
    from water_agent.runtime.governance.contracts import ProviderKind
    from water_agent.runtime.governance.gateway import ProviderGateway

    records: list[Any] = []
    gateway = ProviderGateway(on_usage=records.append)
    client = rig.reg.RegistryCatalogClient(rig.config, rig.reg.registry_context("eval-admin", request_id="usage"), gateway=gateway)
    client.search()
    result.check("recorded", bool(records), "目录读取没有落账")
    result.check("usage_unknown", all(getattr(r, "usage_known", True) is False for r in records), "usage 不应记为已知")
    result.check("provider", {getattr(r, "provider", None) for r in records} == {ProviderKind.MCP_REGISTRY}, "provider 不是 MCP_REGISTRY")
    return []


_REGISTRY_OPS: dict[str, Callable[[RegistryCase, Rig, CaseResult], list[Any]]] = {
    "search": run_search,
    "entry": run_entry,
    "propose": run_propose,
    "hash": run_hash,
    "full_flow": run_full_flow,
    "benign_text": run_benign_text,
    "no_writes": run_no_writes,
    "usage_unknown": run_usage_unknown,
}


def run_registry_case(case: RegistryCase, case_dir: Path, result: CaseResult) -> None:
    del case_dir
    rig = Rig(case)
    try:
        handler = _REGISTRY_OPS.get(case.op)
        if handler is None:
            raise HarnessUsageError(f"未知 registry 用例类型：{case.op!r}")
        with rig.spy:
            entries = handler(case, rig, result)
        entry_urls = [remote.url for entry in entries for remote in getattr(entry, "remotes", ())]
        _account_no_connect(case, rig, result, entry_urls=entry_urls)
        if case.expect.get("zero_bytes_to_entry") or case.expect.get("zero_dns_for_entry_hosts"):
            contacts, received = rig.entry_contacts()
            result.check("explicit_zero_bytes", contacts == 0 and received == 0, "条目地址被连接")
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# invariant 层用例
# ---------------------------------------------------------------------------


def run_invariant_case(case: RegistryCase, case_dir: Path, result: CaseResult) -> None:
    del case_dir
    import ast

    from water_agent.runtime.governance import inventory
    from water_agent.runtime.governance.contracts import (
        DANGEROUS_CAPABILITIES,
        DEFAULT_LOCAL_CAPABILITIES,
        Capability,
    )

    src = REPO_ROOT / "src" / "water_agent"
    if case.op == "ast_imports":
        forbidden_names = {"MCPStdioClient", "MCPHttpClient", "open_bridge", "open_gateway_sessions", "open_exchange", "McpConnectionPool"}
        forbidden_modules = {"water_agent.mcp.client", "water_agent.mcp.http_client", "water_agent.mcp.bridge", "water_agent.mcp.pool",
                             "water_agent.mcp.wiring", "water_agent.mcp.oauth", "socket", "subprocess", "http.client", "urllib.request", "ssl"}
        found = 0
        for path in (src / "mcp" / "registry.py", src / "webapp" / "mcp_registry_routes.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    found += sum(1 for alias in node.names if alias.name in forbidden_modules)
                elif isinstance(node, ast.ImportFrom):
                    found += int((node.module or "") in forbidden_modules) + sum(1 for alias in node.names if alias.name in forbidden_names)
                elif isinstance(node, ast.Name) and node.id in forbidden_names or isinstance(node, ast.Attribute) and node.attr in forbidden_names:
                    found += 1
        result.count("client_import_violation", found)
        result.check("no_client_import", found == 0, f"发现 {found} 处禁用导入 / 名字")
    elif case.op == "capability_surface":
        from water_agent.plugins.base import GRANTABLE_CAPABILITIES
        from water_agent.webapp.auth.dependencies import _ADMIN_CAPABILITIES, _MEMBER_CAPABILITIES

        cap = Capability.MCP_REGISTRY_SEARCH
        result.check("not_default", cap not in DEFAULT_LOCAL_CAPABILITIES, "在默认能力集里")
        result.check("not_member", cap not in _MEMBER_CAPABILITIES and cap not in _ADMIN_CAPABILITIES, "在 Web 成员 / 管理员能力集里")
        result.check("not_grantable", cap not in GRANTABLE_CAPABILITIES, "在 GRANTABLE 里")
        result.check("network_fetch_still_dangerous", Capability.NETWORK_FETCH in DANGEROUS_CAPABILITIES, "NETWORK_FETCH 不在危险集")
        registered = {entry.tool_name for entry in (*inventory.TOOL_CAPABILITIES, *inventory.PLUGIN_GRANTED_TOOL_CAPABILITIES)}
        result.check("not_a_tool", not any("registry" in name for name in registered), "目录搜索被登记成了工具")
    elif case.op == "site_registered":
        site = next((s for s in inventory.PROVIDER_CALL_SITES if s.site_id == "mcp_registry"), None)
        result.check("site_present", site is not None, "站点未登记")
        result.check("site_governed", site is not None and site.governed, "站点未接入网关")
        result.check("site_count", len(inventory.PROVIDER_CALL_SITES) == 11, f"站点数 {len(inventory.PROVIDER_CALL_SITES)}")
        result.check("no_ungoverned_egress", inventory.ungoverned_egress_modules() == frozenset(), "存在未登记的出站模块")
    else:
        raise HarnessUsageError(f"未知 invariant 用例类型：{case.op!r}")


# ---------------------------------------------------------------------------
# api 层用例
# ---------------------------------------------------------------------------

SEARCH = "/api/admin/mcp/registry/search"
PROPOSAL = "/api/admin/mcp/registry/proposal"
STATUS = "/api/admin/mcp/registry/status"


class ApiRig:
    def __init__(self, case_dir: Path, modes: str = "ok") -> None:
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine, event

        from water_agent.mcp import registry as reg
        from water_agent.runtime.governance.gateway import ProviderGateway
        from water_agent.storage.database import Database
        from water_agent.storage.models import Base
        from water_agent.webapp.app import create_app

        fake = fake_module()
        self.reg = reg
        self.test_client = TestClient
        self.sink = fake.EntrySink().start()
        self.server = fake.FakeMcpRegistry(modes, sink_url=self.sink.url).start()
        engine = create_engine(f"sqlite:///{case_dir / 'eval.sqlite'}")

        @event.listens_for(engine, "connect")
        def _fk(dbapi_connection: object, _: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        Base.metadata.create_all(engine)
        self.database = Database(engine=engine)
        self.app = create_app(database=self.database, static_dir=case_dir)
        self.set_config(reg.RegistryConfig(base_url=self.server.base_url, allow_private=True), ProviderGateway())
        self.clients: list[Any] = []

    def set_config(self, config: Any, gateway: Any | None = None) -> None:
        from water_agent.runtime.governance.gateway import ProviderGateway

        self.app.state.mcp_registry_overrides = {"config": config, "gateway": gateway or ProviderGateway()}

    def login(self, role: str, *, must_change: bool = False) -> Any:
        from water_agent.webapp.auth.db_store import PostgresUserStore
        from water_agent.webapp.auth.models import UserRole

        username = f"{role}-{uuid4().hex[:8]}"
        PostgresUserStore(self.database).create_user(username, PASSWORD, UserRole(role), must_change_password=must_change)
        client = self.test_client(self.app, raise_server_exceptions=True)
        response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
        if response.status_code != 200:
            raise RuntimeError(f"登录失败 {response.status_code}")
        self.clients.append(client)
        return client

    @staticmethod
    def headers(client: Any) -> dict[str, str]:
        from water_agent.webapp.auth.csrf import CSRF_COOKIE_NAME

        return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME) or ""}

    def post(self, client: Any, url: str, payload: dict[str, Any]) -> Any:
        return client.post(url, json=payload, headers=self.headers(client))

    def close(self) -> None:
        for client in self.clients:
            client.close()
        self.database.dispose()
        self.server.stop()
        self.sink.stop()


def run_api_case(case: RegistryCase, case_dir: Path, result: CaseResult) -> None:
    from water_agent.webapp.auth import audit

    modes = case.modes
    rig = ApiRig(case_dir, modes)
    audit._clear_events()
    try:
        op = case.op
        if op == "authz":
            member = rig.login("member")
            temporary = rig.login("admin", must_change=True)
            anonymous = rig.test_client(rig.app)
            attempts = [
                anonymous.get(STATUS), anonymous.post(SEARCH, json={}), anonymous.post(PROPOSAL, json={}),
                member.get(STATUS), rig.post(member, SEARCH, {"query": ""}), rig.post(member, PROPOSAL, {"name": "weather-station", "version": "1.0.0"}),
                temporary.get(STATUS), rig.post(temporary, SEARCH, {"query": ""}),
            ]
            allowed = [response for response in attempts if response.status_code == 200]
            result.count("non_admin_access_measured")
            result.count("non_admin_access", len(allowed))
            result.check("all_refused", not allowed and all(r.status_code in (401, 403) for r in attempts), f"状态码 {[r.status_code for r in attempts]}")
            result.check("directory_untouched", rig.server.requests == [], f"目录收到 {len(rig.server.requests)} 个请求")
        elif op == "csrf":
            admin = rig.login("admin")
            responses = [admin.post(SEARCH, json={"query": ""}), admin.post(PROPOSAL, json={"name": "weather-station", "version": "1.0.0"})]
            result.check("csrf_refused", all(r.status_code == 403 and r.json()["error"]["code"] == "AUTH_CSRF_INVALID" for r in responses), "缺 CSRF 仍放行")
            result.check("directory_untouched", rig.server.requests == [], "目录收到请求")
        elif op == "body_whitelist":
            admin = rig.login("admin")
            bad_search: list[dict[str, Any]] = [{"query": "x", "limit": 100}, {"query": "x", "url": "https://evil.example/"}, {"query": "x" * 129},
                          {"query": "line1\nline2"}, {"cursor": "<script>"}, {"query": 5}]
            bad_proposal: list[dict[str, Any]] = [{"name": "weather-station"}, {"name": "weather-station", "version": "1.0.0", "url": "https://evil.example/mcp"},
                            {"name": "weather-station", "version": "1.0.0", "remotes": [{"type": "streamable-http", "url": "https://evil.example/mcp"}]},
                            {"name": "Weather Station", "version": "1.0.0"}, {"name": "weather-station", "version": "latest"}]
            for payload in bad_search:
                response = rig.post(admin, SEARCH, payload)
                result.check(f"search_422:{sorted(payload)}", response.status_code == 422 and "evil.example" not in response.text, f"HTTP {response.status_code}")
            for payload in bad_proposal:
                response = rig.post(admin, PROPOSAL, payload)
                result.check(f"proposal_422:{sorted(payload)}", response.status_code == 422 and "evil.example" not in response.text, f"HTTP {response.status_code}")
            result.check("directory_untouched", rig.server.requests == [], f"目录收到 {len(rig.server.requests)} 个请求")
        elif op == "refetch":
            admin = rig.login("admin")
            body = {"name": "weather-station", "version": "1.0.0"}
            first = rig.post(admin, PROPOSAL, body).json()["data"]["proposal"]
            result.check("from_directory", "mcp.weather-station.example" in first["proposal_text"], "提案不是来自目录当前条目")
            rig.server.modes = frozenset({"ok", "weather_moved"})
            second = rig.post(admin, PROPOSAL, body).json()["data"]["proposal"]
            result.check("follows_directory", "moved.example.com" in second["proposal_text"] and second["proposal_hash"] != first["proposal_hash"],
                         "目录改了地址，提案没有跟着变")
            forged = rig.post(admin, PROPOSAL, {**body, "url": "https://attacker.example/mcp"})
            result.check("forged_body_refused", forged.status_code == 422 and "attacker.example" not in forged.text, f"HTTP {forged.status_code}")
        elif op == "limits":
            admin = rig.login("admin")
            codes = [rig.post(admin, SEARCH, {"query": ""}).status_code for _ in range(11)]
            before = len(rig.server.requests)
            blocked = rig.post(admin, SEARCH, {"query": ""})
            result.check("eleventh_refused", codes[:10] == [200] * 10 and codes[10] == 429, f"状态码 {codes}")
            result.check("no_request_when_limited", len(rig.server.requests) == before and blocked.status_code == 429, "被限流后仍向目录发请求")
            other = rig.login("admin")
            result.check("other_admin_unaffected", rig.post(other, SEARCH, {"query": ""}).status_code == 200, "另一个管理员也被限流")
        elif op == "audit":
            admin = rig.login("admin")
            rig.post(admin, SEARCH, {"query": "my-secret-query-text"})
            rig.post(admin, PROPOSAL, {"name": "weather-station", "version": "1.0.0"})
            events = audit._get_events()
            blob = json.dumps(events, ensure_ascii=False)
            result.check("events_recorded", len([e for e in events if e["event"].startswith("mcp.registry_")]) == 2, f"事件数 {len(events)}")
            for forbidden in ("my-secret-query-text", "mcp.weather-station.example", "streamable_http", "https://"):
                result.check(f"audit_absent:{forbidden}", forbidden not in blob, f"审计里出现 {forbidden}")
            fields = {frozenset(e) for e in events if e["event"].startswith("mcp.registry_")}
            result.check("audit_fields", fields == {
                frozenset({"event", "ts", "actor_user_id", "registry_host", "result", "count", "pages"}),
                frozenset({"event", "ts", "actor_user_id", "registry_host", "name", "version", "result", "proposal_hash"}),
            }, f"字段 {sorted(map(sorted, fields))}")
        elif op == "not_configured":
            rig.set_config(rig.reg.RegistryConfig(base_url=""))
            admin = rig.login("admin")
            status = admin.get(STATUS).json()["data"]
            responses = [rig.post(admin, SEARCH, {"query": "x"}), rig.post(admin, PROPOSAL, {"name": "weather-station", "version": "1.0.0"})]
            result.check("status_unconfigured", status["configured"] is False and status["registry_host"] is None, f"状态 {status}")
            result.check("posts_409", all(r.status_code == 409 for r in responses), f"状态码 {[r.status_code for r in responses]}")
            result.check("directory_untouched", rig.server.requests == [], "目录收到请求")
        elif op == "failure_no_leak":
            admin = rig.login("admin")
            response_leak = False
            for mode in ("status_503", "status_401", "status_429", "junk"):
                rig.server.modes = frozenset({mode})
                response = rig.post(admin, SEARCH, {"query": ""})
                if any(marker in response.text for marker in _UPSTREAM_MARKERS):
                    response_leak = True
            result.check("response_has_no_upstream_content", not response_leak, "响应里出现上游正文 / 网址 / 密钥 / 回环地址")
            # 审计按设计记**目录主机名**（评测里的假目录就是 127.0.0.1），所以审计检查不含主机名这一项
            audit_markers = tuple(marker for marker in _UPSTREAM_MARKERS if marker != "127.0.0.1")
            blob = json.dumps(audit._get_events(), ensure_ascii=False)
            result.check("audit_has_no_upstream_content", not any(marker in blob for marker in audit_markers), "审计里出现上游正文 / 网址 / 密钥")
        else:
            raise HarnessUsageError(f"未知 api 用例类型：{op!r}")
        contacts, received = rig.sink.contacts, rig.sink.bytes_received
        result.count("no_connect_checked")
        result.count("bytes_to_entry_urls", contacts + received)
        result.check("entry_address_untouched", contacts == 0 and received == 0, f"条目地址收到 {contacts} 次连接 / {received} 字节")
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# 调度、指标、报告
# ---------------------------------------------------------------------------


def run_case(case: RegistryCase, workdir: Path) -> CaseResult:
    result = CaseResult(id=case.id, category=case.category, layer=case.layer)
    case_dir = workdir / case.id
    case_dir.mkdir(parents=True, exist_ok=True)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            if case.layer == "registry":
                run_registry_case(case, case_dir, result)
            elif case.layer == "invariant":
                run_invariant_case(case, case_dir, result)
            elif case.layer == "api":
                run_api_case(case, case_dir, result)
            else:
                raise HarnessUsageError(f"未知用例层：{case.layer!r}")
    except HarnessUsageError:
        raise
    except Exception as exc:  # noqa: BLE001 — 记为用例错误（硬指标 harness_error_count）
        result.error = f"{type(exc).__name__}: {exc}"
    if not result.checks and result.error is None:
        result.error = "用例没有产生任何检查（未测量不是通过）"
    return result


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 6)


def metrics_from_case_dicts(cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """从报告里的用例明细算出全部指标。harness 写报告与比较器核对"报告没被手改"共用这一个函数。"""

    totals: dict[str, int] = {}
    for case in cases:
        for name, value in (case.get("counters") or {}).items():
            totals[name] = totals.get(name, 0) + int(value)

    def total(name: str) -> int:
        return totals.get(name, 0)

    def counted(name: str, measured: str) -> int | None:
        """计数类指标：对应的"已测量"标记为 0 时记 None（未测量），不记 0。"""

        return total(name) if total(measured) > 0 else None

    return {
        "remote_only_proposal_rate": _rate(total("remote_only_proposal_ok"), total("remote_only_proposal_total")),
        "proposal_disabled_no_allow_rate": _rate(total("proposal_disabled_no_allow_ok"), total("proposal_disabled_no_allow_total")),
        "bytes_to_entry_urls": counted("bytes_to_entry_urls", "no_connect_checked"),
        "dns_lookups_for_entry_hosts": counted("dns_lookups_for_entry_hosts", "no_connect_checked"),
        "secret_drop_rate": _rate(total("secret_drop_ok"), total("secret_drop_total")),
        "unsafe_entry_proposed_count": counted("unsafe_entry_proposed", "unsafe_entry_checked"),
        "proposal_hash_determinism_rate": _rate(total("proposal_hash_determinism_ok"), total("proposal_hash_determinism_total")),
        "registry_failure_fail_closed_rate": _rate(total("registry_failure_fail_closed_ok"), total("registry_failure_fail_closed_total")),
        "non_admin_access_count": counted("non_admin_access", "non_admin_access_measured"),
        "benign_entry_proposed_rate": _rate(total("benign_entry_proposed_ok"), total("benign_entry_proposed_total")),
        "benign_text_false_reject_count": counted("benign_text_false_reject", "benign_text_checked"),
        "harness_error_count": sum(1 for case in cases if case.get("error") is not None),
    }


def hard_violations(metrics: dict[str, Any]) -> list[str]:
    return [
        f"{name}={metrics.get(name)!r}（目标 {target!r}）"
        for name, target in HARD_TARGETS.items()
        if metrics.get(name) != target
    ]


def mechanism_present() -> bool:
    try:
        importlib.import_module("water_agent.mcp.registry")
    except ImportError:
        return False
    return True


def build_report(results: Sequence[CaseResult], *, dataset: Path) -> dict[str, Any]:
    metrics = metrics_from_case_dicts([result.as_dict() for result in results])
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


def run(
    dataset: Path, *, only: Callable[[RegistryCase], bool] | None = None
) -> tuple[dict[str, Any] | None, list[str]]:
    cases, problems = load_cases(dataset)
    if problems:
        return None, problems
    if only is not None:
        cases = [case for case in cases if only(case)]
    if not cases:
        return None, ["0 条可执行用例"]
    if not FAKE_REGISTRY.is_file():
        raise HarnessUsageError(f"缺少假目录：{FAKE_REGISTRY}")
    from water_agent.harness.tool_assembly_snapshot import hermetic_environment

    with tempfile.TemporaryDirectory(prefix="wa-mcp-registry-") as tmp:
        workdir = Path(tmp)
        with hermetic_environment(workdir):
            results = [run_case(case, workdir) for case in cases]
    return build_report(results, dataset=dataset), []


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or SUITE).splitlines()[0])
    parser.add_argument("dataset", nargs="?", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--report-path", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        report, problems = run(args.dataset)
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
                f"{check['name']}: {check['detail']}" for check in case["checks"] if not check["ok"]
            )
            print(f"  FAIL {case['id']}: {detail}")
    if report["hard_violations"]:
        print("!! 硬指标不达标：" + "；".join(report["hard_violations"]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
