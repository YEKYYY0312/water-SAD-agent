"""P7-S13-A01：注册表协议与客户端（``docs/design/17-p7-external-capabilities.md`` §6.2 / §6.3）。

进程内线程假注册表（不起子进程，S8 D13）。这里只测客户端自身的协议行为；整条 fetch → approve 链路与敌意包在
``skill_fetch_p0`` 里（``tests/test_skill_fetch_eval.py``）。
"""
from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self

import pytest

from water_agent.runtime.governance.contracts import Capability, Principal, RequestContext
from water_agent.runtime.governance.gateway import ProviderGateway
from water_agent.skills.errors import (
    SKILL_NOT_IN_REGISTRY,
    SKILL_REGISTRY_BAD_RESPONSE,
    SKILL_REGISTRY_MISMATCH,
    SKILL_REGISTRY_UNAVAILABLE,
)
from water_agent.skills.remote.client import (
    IndexVersion,
    RegistryClient,
    RegistryConfig,
    SkillRemoteError,
    cli_principal,
    parse_index,
    provider_call_for,
    sha256_hex,
)

UPSTREAM_SECRET = "upstream-body-SECRET-TOKEN https://upstream.internal/leak?key=sk-live-abcdef0123456789"
NAME, VERSION = "pump-room-notes", "1.0.0"
RAW = f"---\nname: {NAME}\ndescription: x。\nversion: {VERSION}\n---\nbody\n".encode()


def _index(raw: bytes = RAW, *, name: str = NAME, version: str = VERSION, **overrides: Any) -> dict[str, Any]:
    entry = {"version": version, "sha256": sha256_hex(raw), "size": len(raw), **overrides}
    return {"version": 1, "skills": [{"name": name, "versions": [entry]}]}


class _Registry:
    def __init__(self, handler: Any) -> None:
        self.requests: list[str] = []
        registry = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                return

            def do_GET(self) -> None:
                registry.requests.append(self.path)
                status, ctype, body, extra = handler(self.path)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for key, value in extra.items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/reg-PATHSENTINEL"


def _serve(index: Any = None, file: bytes = RAW, *, index_ctype: str = "application/json", file_ctype: str = "text/markdown",
           status: int = 200) -> Any:
    def handler(path: str) -> tuple[int, str, bytes, dict[str, str]]:
        if status != 200:
            return status, "text/plain", UPSTREAM_SECRET.encode(), {}
        if path.endswith("/index.json"):
            body = index if isinstance(index, bytes) else json.dumps(index if index is not None else _index()).encode()
            return 200, index_ctype, body, {}
        if path.endswith("/SKILL.md"):
            return 200, file_ctype, file, {}
        return 404, "text/plain", b"nope", {}

    return handler


def _client(registry: _Registry, **kwargs: Any) -> RegistryClient:
    config = RegistryConfig(base_url=registry.base_url, allow_private=kwargs.pop("allow_private", True))
    return RegistryClient(config, gateway=ProviderGateway(sleep=lambda seconds: None), **kwargs)


def _declared(raw: bytes = RAW) -> IndexVersion:
    return IndexVersion(version=VERSION, sha256=sha256_hex(raw), size=len(raw))


def _code(call: Any) -> str:
    with pytest.raises(SkillRemoteError) as exc:
        call()
    return exc.value.code


def test_index_and_file_round_trip() -> None:
    """P7-S13-A01 index 严格解析后能找到 name@version；按声明的 size 与 sha256 取回 SKILL.md，请求路径只含校验过的名字与版本。"""

    with _Registry(_serve()) as registry:
        client = _client(registry)
        index = client.fetch_index()
        declared = index.find(NAME, VERSION)
        assert declared is not None and declared.size == len(RAW)
        fetched = client.fetch_skill_file(NAME, VERSION, declared=declared)
        assert fetched.raw == RAW and fetched.sha256 == hashlib.sha256(RAW).hexdigest()
        assert registry.requests == [
            "/reg-PATHSENTINEL/index.json",
            f"/reg-PATHSENTINEL/skills/{NAME}/{VERSION}/SKILL.md",
        ]
        assert client.request_count == 2


def test_not_configured_and_forbidden_destinations_are_unavailable_with_zero_requests() -> None:
    """P7-S13-A01 非 https（回环未放行）→ ``SKILL_REGISTRY_UNAVAILABLE``，**零请求**发出；放行回环后才连得上。"""

    with _Registry(_serve()) as registry:
        client = _client(registry, allow_private=False)
        assert _code(client.fetch_index) == SKILL_REGISTRY_UNAVAILABLE
        assert registry.requests == []


def test_bad_statuses_and_media_types_are_refused_without_echoing_the_upstream_body() -> None:
    """P7-S13-A01 404 / 5xx / 401 / 403 / 429 → 稳定码，错误对象的文本不含上游响应体、网址或路径哨兵；媒体类型不对 → ``BAD_RESPONSE``。"""

    for status, expected in ((404, SKILL_REGISTRY_BAD_RESPONSE), (500, SKILL_REGISTRY_UNAVAILABLE),
                             (503, SKILL_REGISTRY_UNAVAILABLE), (401, SKILL_REGISTRY_UNAVAILABLE),
                             (403, SKILL_REGISTRY_UNAVAILABLE), (429, SKILL_REGISTRY_UNAVAILABLE)):
        with _Registry(_serve(status=status)) as registry:
            with pytest.raises(SkillRemoteError) as exc:
                _client(registry).fetch_index()
            assert exc.value.code == expected, status
            text = f"{exc.value} {exc.value.message}"
            for leaked in ("SECRET-TOKEN", "upstream.internal", "sk-live", "PATHSENTINEL", "127.0.0.1"):
                assert leaked not in text, (status, leaked)
    with _Registry(_serve(index_ctype="text/html")) as registry:
        assert _code(_client(registry).fetch_index) == SKILL_REGISTRY_BAD_RESPONSE
    with _Registry(_serve(file_ctype="text/html")) as registry:
        assert _code(lambda: _client(registry).fetch_skill_file(NAME, VERSION, declared=_declared())) == SKILL_REGISTRY_BAD_RESPONSE


def test_redirects_are_not_followed() -> None:
    """P7-S13-A01 3xx 不跟随：注册表不能把请求引到别的地址（出口只由运维写死的基址决定）。"""

    def handler(path: str) -> tuple[int, str, bytes, dict[str, str]]:
        return 302, "text/plain", b"", {"Location": "http://127.0.0.1:1/elsewhere"}

    with _Registry(handler) as registry:
        assert _code(_client(registry).fetch_index) == SKILL_REGISTRY_BAD_RESPONSE
        assert len(registry.requests) == 1


def test_oversize_responses_are_refused() -> None:
    """P7-S13-A01 index 超过 256 KiB、SKILL.md 超过其声明的 size（上限 64 KiB）→ ``BAD_RESPONSE``。"""

    with _Registry(_serve(index=b" " * (256 * 1024 + 10))) as registry:
        assert _code(_client(registry).fetch_index) == SKILL_REGISTRY_BAD_RESPONSE
    with _Registry(_serve(file=RAW + b"x" * 1000)) as registry:
        assert _code(lambda: _client(registry).fetch_skill_file(NAME, VERSION, declared=_declared())) == SKILL_REGISTRY_BAD_RESPONSE


def test_size_and_sha_mismatches_are_registry_mismatch() -> None:
    """P7-S13-A01 文件的字节数或 sha256 与 index 声明不一致 → ``SKILL_REGISTRY_MISMATCH``（只防传输错误，不是信任根）。"""

    with _Registry(_serve()) as registry:
        client = _client(registry)
        wrong_sha = IndexVersion(version=VERSION, sha256="0" * 64, size=len(RAW))
        assert _code(lambda: client.fetch_skill_file(NAME, VERSION, declared=wrong_sha)) == SKILL_REGISTRY_MISMATCH
        wrong_size = IndexVersion(version=VERSION, sha256=sha256_hex(RAW), size=len(RAW) - 1)
        assert _code(lambda: client.fetch_skill_file(NAME, VERSION, declared=wrong_size)) in {
            SKILL_REGISTRY_MISMATCH, SKILL_REGISTRY_BAD_RESPONSE,
        }


@pytest.mark.parametrize(
    ("label", "document"),
    [
        ("not-json", b"{ nope"),
        ("not-an-object", b"[]"),
        ("wrong-version", json.dumps({"version": 2, "skills": []}).encode()),
        ("extra-top-key", json.dumps({"version": 1, "skills": [], "latest": "1.0.0"}).encode()),
        ("skills-not-list", json.dumps({"version": 1, "skills": {}}).encode()),
        ("traversal-name", json.dumps(_index(name="../etc/passwd")).encode()),
        ("uppercase-name", json.dumps(_index(name="Pump-Room")).encode()),
        ("bad-version", json.dumps(_index(version="latest")).encode()),
        ("short-sha", json.dumps(_index(sha256="abc")).encode()),
        ("uppercase-sha", json.dumps(_index(sha256="A" * 64)).encode()),
        ("zero-size", json.dumps(_index(size=0)).encode()),
        ("huge-size", json.dumps(_index(size=10**9)).encode()),
        ("bool-size", json.dumps(_index(size=True)).encode()),
        ("extra-entry-key", json.dumps(_index(url="https://x.example")).encode()),
    ],
)
def test_index_is_validated_as_a_whole(label: str, document: bytes) -> None:
    """P7-S13-A01 index 严格整份校验：不是 JSON / 形状不是协议 v1 / 名字或版本不合法（含 ``../``、大写、``latest``）/ sha256 不是 64 位
    小写十六进制 / size 越界 / 多余键 → ``BAD_RESPONSE``。**任何一个条目不合法，整份 index 被拒**。"""

    assert _code(lambda: parse_index(document)) == SKILL_REGISTRY_BAD_RESPONSE, label


def test_duplicate_names_and_versions_reject_the_whole_index() -> None:
    """P7-S13-A01 同一 name 或同一 version 在 index 里出现两次 → 整份拒绝（不是"取后者"）。"""

    entry = {"version": VERSION, "sha256": "a" * 64, "size": 10}
    duplicate_name = {"version": 1, "skills": [{"name": NAME, "versions": [entry]}, {"name": NAME, "versions": [entry]}]}
    duplicate_version = {"version": 1, "skills": [{"name": NAME, "versions": [entry, dict(entry)]}]}
    assert _code(lambda: parse_index(json.dumps(duplicate_name).encode())) == SKILL_REGISTRY_BAD_RESPONSE
    assert _code(lambda: parse_index(json.dumps(duplicate_version).encode())) == SKILL_REGISTRY_BAD_RESPONSE


def test_request_arguments_are_validated_before_they_reach_a_path() -> None:
    """P7-S13-A01 ``fetch_skill_file`` 在拼路径之前再核一次 name / version：``../``、换行、大写、非三段版本一律不发请求。"""

    with _Registry(_serve()) as registry:
        client = _client(registry)
        for name, version in (("../etc", "1.0.0"), ("ab-cd\n", "1.0.0"), ("Ab-cd", "1.0.0"), ("ab-cd", "latest"), ("ab-cd", "1.0")):
            assert _code(lambda n=name, v=version: client.fetch_skill_file(n, v, declared=_declared())) == SKILL_REGISTRY_BAD_RESPONSE
        assert registry.requests == []


def test_lookup_distinguishes_missing_versions() -> None:
    """P7-S13-A01 ``find`` 精确匹配：不在 index 里的 name 或 version 返回 ``None``（CLI 把它报成 ``SKILL_NOT_IN_REGISTRY``）。"""

    index = parse_index(json.dumps(_index()).encode())
    assert index.find(NAME, VERSION) is not None
    assert index.find(NAME, "2.0.0") is None and index.find("other-skill", VERSION) is None
    assert SKILL_NOT_IN_REGISTRY == "SKILL_NOT_IN_REGISTRY"


def test_every_request_goes_through_the_governed_gateway_with_the_cli_capability() -> None:
    """P7-S13-A09 每个出站请求都经 ``ProviderGateway.call``（网关计数 +1）；CLI 主体**只**持有 ``SKILL_FETCH``；没有该能力的主体被网关拒绝，
    且一个请求都不发出；调用意图的熔断键按注册表主机分桶。"""

    with _Registry(_serve()) as registry:
        gateway = ProviderGateway(sleep=lambda seconds: None)
        client = RegistryClient(RegistryConfig(base_url=registry.base_url, allow_private=True), gateway=gateway)
        client.fetch_index()
        assert gateway.provider_call_count == 1

        weak = Principal.local("weak", capabilities=frozenset({Capability.RETRIEVE_CLAUSES}))
        denied = RegistryClient(
            RegistryConfig(base_url=registry.base_url, allow_private=True),
            gateway=ProviderGateway(sleep=lambda seconds: None),
            principal=weak,
        )
        before = len(registry.requests)
        assert _code(denied.fetch_index) == SKILL_REGISTRY_UNAVAILABLE
        assert len(registry.requests) == before, "能力校验在发送前拒绝"

    assert cli_principal().capabilities == frozenset({Capability.SKILL_FETCH})
    call_a = provider_call_for(RegistryConfig(base_url="https://a.example/x"))
    call_b = provider_call_for(RegistryConfig(base_url="https://b.example/x"))
    assert ProviderGateway.breaker_key(call_a) != ProviderGateway.breaker_key(call_b)
    assert call_a.required_capability is Capability.SKILL_FETCH
    assert isinstance(RequestContext, type)


def test_config_repr_never_shows_the_full_url() -> None:
    """P7-S13-A01 ``RegistryConfig`` 的 ``repr`` 只含主机名与私网标志，不含完整 URL（路径可能带部署信息）。"""

    config = RegistryConfig(base_url="https://registry.example.com/team-secret-path", allow_private=False)
    assert "team-secret-path" not in repr(config) and "registry.example.com" in repr(config)
    with pytest.raises(ValueError):
        RegistryConfig(base_url="")
