"""逐对兼容性验证（P7-S15-A02，``docs/design/18-p7-multi-model.md`` §3.3）。

探针对接进程内的假 OpenAI 兼容端点（``tests/fixtures/endpoint_compat/fake_openai_server.py``，明文回环），
不连真实服务商、不计费。
"""

from __future__ import annotations

import importlib.util
import stat
from pathlib import Path
from types import ModuleType

import pytest

from water_agent.config.credentials import CREDENTIALS, forget_injected_credentials
from water_agent.generation import compatibility as compat
from water_agent.generation import endpoints as ep
from water_agent.generation import model_catalog as mc
from water_agent.harness import endpoint_compat

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_SERVER = REPO_ROOT / "tests" / "fixtures" / "endpoint_compat" / "fake_openai_server.py"
URL = "https://llm.relay.example/v1"
KEY = "sk-relay-key-for-the-probe-0001"


def _fake() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fake_openai_server_ver", FAKE_SERVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE = _fake()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        ep.CHAT_BASE_URL_ENV,
        ep.CHAT_MODEL_ENV,
        compat.COMPATIBILITY_REPORT_ENV,
        "LOCAL_MODEL_ENABLED",
        "WATER_AGENT_LLM_PRICE",
        "WATER_AGENT_LLM_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    forget_injected_credentials()


def _report(model: str, url: str = URL, **overrides: object) -> compat.CompatibilityReport:
    fields: dict[str, object] = {
        "version": compat.ENDPOINT_PROBE_VERSION,
        "status": "compatible",
        "model": model,
        "endpoint": url,
        "checks": {name: "passed" for name in compat.ENDPOINT_COMPATIBILITY_CHECKS},
        "schema_tokens": 1200,
        "evidence": {"probe_version": compat.ENDPOINT_PROBE_VERSION},
    }
    fields.update(overrides)
    return compat.CompatibilityReport.model_validate(fields)


def _endpoint(monkeypatch: pytest.MonkeyPatch, model: str, url: str = URL) -> ep.ChatEndpoint:
    monkeypatch.setenv(ep.CHAT_BASE_URL_ENV, url)
    monkeypatch.setenv("WATER_AGENT_LLM_API_KEY", KEY)
    return ep.resolve_chat_endpoint(model=model)


def test_a_pair_report_verifies_exactly_its_own_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S15-A02 逐对报告只放行它自己的 (端点, 模型)：A 通过不代表 B 通过，也不代表另一个端点上的同名模型通过。"""

    compat.write_report(compat.pair_report_path(URL, "flash-a"), _report("flash-a"))
    assert compat.endpoint_verification(_endpoint(monkeypatch, "flash-a")).status == compat.VERIFICATION_VERIFIED
    assert compat.endpoint_verification(_endpoint(monkeypatch, "pro-b")).usable is False
    other = compat.endpoint_verification(_endpoint(monkeypatch, "flash-a", "https://llm.other.example/v1"))
    assert other.usable is False


def test_the_file_name_is_an_index_not_a_trust_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S15-A02 文件名只是索引：把 A 的报告拷到 B 的位置，读出后仍按 ``matches()`` 复核，B 不会因此通过。"""

    compat.write_report(compat.pair_report_path(URL, "pro-b"), _report("flash-a"))
    verdict = compat.endpoint_verification(_endpoint(monkeypatch, "pro-b"))
    assert verdict.usable is False
    assert "another endpoint or model" in verdict.reason


def test_stale_probe_version_and_failed_checks_do_not_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S15-A02 旧探针版本、有检查没通过的逐对报告都不是证据；原因优先说"针对这一对的那份报告"。"""

    endpoint = _endpoint(monkeypatch, "flash-a")
    path = compat.pair_report_path(URL, "flash-a")
    compat.write_report(path, _report("flash-a", version="p7-s9-old"))
    assert "outdated probe" in compat.endpoint_verification(endpoint).reason
    broken = {name: "passed" for name in compat.ENDPOINT_COMPATIBILITY_CHECKS} | {"streaming_usage": "blocked"}
    compat.write_report(path, _report("flash-a", status="blocked", checks=broken))
    verdict = compat.endpoint_verification(endpoint)
    assert verdict.usable is False and "not every compatibility check passed" in verdict.reason


def test_the_legacy_single_report_path_is_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P7-S15-A02 原单文件路径（env / 面板单文件）保持不变：没有逐对报告时照旧放行 / 拒绝，原因文案不变。"""

    endpoint = _endpoint(monkeypatch, "flash-a")
    assert compat.endpoint_verification(endpoint).reason == "no compatibility report"
    single = tmp_path / "credentials" / compat.PANEL_REPORT_FILE
    compat.write_report(single, _report("flash-a"))
    assert compat.endpoint_verification(endpoint).status == compat.VERIFICATION_VERIFIED
    compat.write_report(single, _report("pro-b"))
    assert compat.endpoint_verification(endpoint).reason == "compatibility report is for another endpoint or model"


def test_default_exemption_is_not_widened_by_the_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S15-A02/SDD18-I17 免检仍只属于缺省端点 + 字面 ``deepseek-chat``；目录里的模型（哪怕声明了 deepseek-chat
    之外的缺省端点模型）永远走逐对验证。"""

    mc.write_catalog(mc.parse_entries([{"id": "deepseek-flash", "label": "闪", "tier": "fast", "thinking": "off"}]))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-key-0001")
    default = ep.resolve_chat_endpoint()
    assert compat.endpoint_verification(default).status == compat.VERIFICATION_DEFAULT_EXEMPT
    flash = ep.resolve_chat_endpoint(model="deepseek-flash")
    assert flash.is_default is False
    assert compat.endpoint_verification(flash).usable is False


def test_probe_for_a_catalog_model_sends_the_production_extra_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P7-S15-A02/A03 探针按目录项的 ``thinking`` 配置构造被测模型：``thinking: off`` 的条目，探针的**每一个**请求体
    （含故意写错模型名那次）都带 ``thinking.type=disabled``——与生产装配同一份 ``extra_body``；报告写逐对位置
    （0600），证据里记 ``thinking_profile``；该对随之通过验证，全局模型不受影响。"""

    with FAKE.FakeOpenAIServer(api_key=KEY, models=["global-m", "flash-a"]) as server:
        ep.write_panel_endpoint(server.base_url, "global-m")
        monkeypatch.setenv("WATER_AGENT_LLM_API_KEY", KEY)
        mc.write_catalog(mc.parse_entries([{"id": "flash-a", "label": "快速", "tier": "fast", "thinking": "off"}]))

        report, skipped = endpoint_compat.run_endpoint_compatibility(model="flash-a")

        assert skipped is None and report is not None
        assert report.status == "compatible" and report.model == "flash-a"
        assert report.evidence["thinking_profile"] == "off"
        posts = [item for item in server.requests if item["path"].endswith("/chat/completions")]
        assert len(posts) >= 5
        assert all(item["thinking"] == {"type": "disabled"} for item in posts), [i["model"] for i in posts]
        path = compat.pair_report_path(server.base_url, "flash-a")
        assert path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600
        assert compat.endpoint_verification(ep.resolve_chat_endpoint(model="flash-a")).status == "verified"
        # 全局模型没有任何报告，仍未验证
        assert compat.endpoint_verification(ep.resolve_chat_endpoint()).usable is False


def test_probe_sends_nothing_extra_for_default_profile_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S15-A02/A03 ``thinking: default``（缺省）的条目、目录外的模型：探针请求体不带任何思考字段——不为没有思考
    开关的模型发会让严格服务商 400 的未知字段。"""

    with FAKE.FakeOpenAIServer(api_key=KEY, models=["global-m", "plain-a"]) as server:
        ep.write_panel_endpoint(server.base_url, "global-m")
        monkeypatch.setenv("WATER_AGENT_LLM_API_KEY", KEY)
        mc.write_catalog(mc.parse_entries([{"id": "plain-a", "label": "普通", "tier": "fast"}]))

        report, _ = endpoint_compat.run_endpoint_compatibility(model="plain-a")

        assert report is not None and report.status == "compatible"
        assert report.evidence["thinking_profile"] == "default"
        posts = [item for item in server.requests if item["path"].endswith("/chat/completions")]
        assert posts and all(item["thinking"] is None for item in posts)
