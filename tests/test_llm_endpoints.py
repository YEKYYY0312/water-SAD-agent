"""对话 / embedding 端点解析（P7-S10-A01，``docs/design/16-p7-plugin-mcp.md`` §7 S10）。

全程离线：只构造对象、读解析结果，不发任何请求。
"""

from __future__ import annotations

import argparse
import importlib.util
from contextlib import ExitStack
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver

from water_agent.config.credentials import CREDENTIALS, forget_injected_credentials
from water_agent.generation import endpoints as ep
from water_agent.generation.chat_model import build_chat_model
from water_agent.generation.llm import build_llm_generator

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"

_ENDPOINT_ENV = (
    ep.CHAT_BASE_URL_ENV,
    ep.CHAT_MODEL_ENV,
    ep.EMBEDDING_BASE_URL_ENV,
    ep.EMBEDDING_MODEL_ENV,
    "WATER_AGENT_LLM_COMPATIBILITY_REPORT",
    "WATER_AGENT_LLM_TIMEOUT_SECONDS",
    "WATER_AGENT_LLM_PRICE",
    "WATER_AGENT_RETRIEVAL_MODE",
    "LOCAL_MODEL_ENABLED",
    "LOCAL_MODEL_BASE_URL",
    "LOCAL_MODEL_NAME",
    "LOCAL_MODEL_COMPATIBILITY_REPORT",
)


@pytest.fixture(autouse=True)
def _clean_endpoint_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in _ENDPOINT_ENV:
        monkeypatch.delenv(name, raising=False)
    for name in CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    forget_injected_credentials()


def _write_report(path: Path, *, endpoint: str, model: str) -> Path:
    spec = importlib.util.spec_from_file_location(
        "fake_openai_server",
        REPO_ROOT / "tests" / "fixtures" / "endpoint_compat" / "fake_openai_server.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.write_passing_report(path, endpoint=endpoint, model=model)


def _inner(model: object) -> object:
    return model.inner  # type: ignore[attr-defined]


def _session_args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "db_path": None,
        "dimensions": None,
        "rerank": "local",
        "rerank_model": None,
        "rerank_base_url": None,
        "rerank_api_key": None,
        "top_k": 5,
        "recall_top_k": 20,
        "hybrid_alpha": 0.7,
        "model": None,
        "base_url": None,
        "api_key_env": None,
        "api_key": None,
        "mock_generator": False,
        "session_dir": str(tmp_path / "sessions"),
        "trace_dir": str(tmp_path / "traces"),
        "stream": False,
        "drawing_root": "",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _session_model(tmp_path: Path, **overrides: object) -> object:
    from water_agent.retrieval.loader import load_clauses_jsonl
    from water_agent.retrieval.trivium import TriviumRetriever
    from water_agent.runtime.agentic import AgenticSession

    with ExitStack() as stack:
        session = AgenticSession(
            _session_args(tmp_path, **overrides),
            stack=stack,
            checkpointer=MemorySaver(),
            retriever=TriviumRetriever(db_path=None, fallback_clauses=load_clauses_jsonl(FIXTURE)),
            mcp_enabled=False,
        )
        return session.model


def test_defaults_are_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S10-A01 三项都没配 = 现状：对话 DeepSeek（deepseek-chat，``DEEPSEEK_API_KEY``），embedding
    百炼 qwen3.7-text-embedding（``DASHSCOPE_API_KEY``）；CLI / runner 参数缺省为"按部署配置"，解析结果
    与此前写死的值逐字相同；Web 会话工厂不再写死端点。"""

    chat = ep.resolve_chat_endpoint()
    assert (chat.base_url, chat.model, chat.api_key_name, chat.source) == (
        "https://api.deepseek.com",
        "deepseek-chat",
        "DEEPSEEK_API_KEY",
        "default",
    )
    assert chat.is_default
    embedding = ep.resolve_embedding_endpoint()
    assert (embedding.base_url, embedding.model, embedding.api_key_name, embedding.source) == (
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen3.7-text-embedding",
        "DASHSCOPE_API_KEY",
        "default",
    )
    assert embedding.is_default

    from water_agent.cli import build_parser
    from water_agent.harness.runner import build_parser as runner_parser

    cli = build_parser().parse_args(["问题"])
    assert (cli.model, cli.base_url, cli.api_key_env) == (None, None, None)
    runner_args = runner_parser().parse_args(["x.jsonl"])
    assert (runner_args.model, runner_args.base_url, runner_args.api_key_env) == (None, None, None)

    from water_agent.webapp.run_manager import WebAgenticSessionFactory

    web = WebAgenticSessionFactory(None, None, trace_dir=tmp_path, mock_generator=False)  # type: ignore[arg-type]
    web_args = web._args()
    assert (web_args.model, web_args.base_url, web_args.api_key_env, web_args.api_key) == (
        None,
        None,
        None,
        None,
    )

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-default-deepseek-key")
    model = build_chat_model()
    assert _inner(model).model_name == "deepseek-chat"  # type: ignore[attr-defined]
    assert _inner(model).openai_api_base == "https://api.deepseek.com"  # type: ignore[attr-defined]
    generator = build_llm_generator()
    assert (generator.config.model, generator.config.base_url) == (
        "deepseek-chat",
        "https://api.deepseek.com",
    )
    session_model = _session_model(tmp_path)
    assert _inner(session_model).model_name == "deepseek-chat"  # type: ignore[attr-defined]
    assert _inner(session_model).openai_api_base == "https://api.deepseek.com"  # type: ignore[attr-defined]


def test_key_is_never_sent_to_another_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S10-A01 端点改到别家而没配 ``WATER_AGENT_LLM_API_KEY`` 时不回落 ``DEEPSEEK_API_KEY``：解析出的
    Key 名是专用 Key、构造即响亮失败、预检写明缺哪一项（不含任何 Key 值）；端点仍是 DeepSeek（只换模型）
    时照旧回落；专用 Key 配了就用专用 Key。embedding 同一规则。"""

    from water_agent.runtime.agentic import chat_model_problem

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-only-for-deepseek")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope-only-for-bailian")
    monkeypatch.setenv(ep.CHAT_BASE_URL_ENV, "https://llm.other-provider.example/v1")
    monkeypatch.setenv(ep.CHAT_MODEL_ENV, "other-model")

    chat = ep.resolve_chat_endpoint()
    assert chat.api_key_name == "WATER_AGENT_LLM_API_KEY"
    assert chat.api_key() is None
    assert chat.missing_key() == "WATER_AGENT_LLM_API_KEY"
    with pytest.raises(ValueError, match="WATER_AGENT_LLM_API_KEY must not be empty"):
        build_chat_model()
    with pytest.raises(ValueError, match="api_key must not be empty"):
        build_llm_generator()
    code, detail = chat_model_problem() or ("", "")
    assert code == "WEB_MODEL_NOT_CONFIGURED"
    assert "WATER_AGENT_LLM_API_KEY" in detail and "llm.other-provider.example" in detail
    assert "sk-" not in detail

    monkeypatch.setenv("WATER_AGENT_LLM_API_KEY", "sk-the-other-provider-key")
    model = build_chat_model()
    assert _inner(model).openai_api_key.get_secret_value() == "sk-the-other-provider-key"  # type: ignore[attr-defined]
    assert _inner(model).openai_api_base == "https://llm.other-provider.example/v1"  # type: ignore[attr-defined]

    monkeypatch.delenv("WATER_AGENT_LLM_API_KEY")
    monkeypatch.delenv(ep.CHAT_BASE_URL_ENV)
    same_provider = ep.resolve_chat_endpoint()
    assert same_provider.model == "other-model" and same_provider.api_key_name == "DEEPSEEK_API_KEY"
    assert not same_provider.is_default

    monkeypatch.setenv(ep.EMBEDDING_BASE_URL_ENV, "https://embed.other-provider.example/v1")
    embedding = ep.resolve_embedding_endpoint()
    assert embedding.api_key_name == "WATER_AGENT_EMBEDDING_API_KEY"
    assert embedding.missing_key() == "WATER_AGENT_EMBEDDING_API_KEY"
    monkeypatch.setenv("WATER_AGENT_EMBEDDING_API_KEY", "sk-embedding-provider-key")
    assert ep.resolve_embedding_endpoint().api_key() == "sk-embedding-provider-key"


def test_all_entry_points_share_one_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P7-S10-A01 CLI 会话 / Web 会话 / runner / legacy 生成器 / docgen 润色 / judge 都经
    ``resolve_chat_endpoint``：同一份运维配置在每个入口得到同一个端点、模型与 Key。"""

    from water_agent.docgen.quality import RealPolishTransport
    from water_agent.harness.llm_judge import LLMJudge
    from water_agent.harness.runner import HarnessRunner

    monkeypatch.setenv(ep.CHAT_BASE_URL_ENV, "https://llm.other-provider.example/v1/")
    monkeypatch.setenv(ep.CHAT_MODEL_ENV, "other-model")
    monkeypatch.setenv("WATER_AGENT_LLM_API_KEY", "sk-the-other-provider-key")
    expected = ("https://llm.other-provider.example/v1", "other-model")
    # 非缺省端点要过兼容性门槛（A02，见 test_endpoint_compatibility.py）：这里给一份精确匹配的报告
    report = _write_report(tmp_path / "compat.json", endpoint=expected[0], model=expected[1])
    monkeypatch.setenv("WATER_AGENT_LLM_COMPATIBILITY_REPORT", str(report))

    def seen(model: object) -> tuple[str, str]:
        inner = _inner(model)
        return inner.openai_api_base, inner.model_name  # type: ignore[attr-defined]

    assert seen(build_chat_model()) == expected
    assert seen(_session_model(tmp_path)) == expected
    generator = build_llm_generator()
    assert (generator.config.base_url, generator.config.model) == expected
    assert generator.config.api_key == "sk-the-other-provider-key"
    assert seen(RealPolishTransport()._ensure_llm()) == expected
    judge = LLMJudge(cache_dir=tmp_path / "judge")
    assert (judge.judge_base_url, judge.judge_model) == expected
    assert judge._resolve_api_key() == "sk-the-other-provider-key"
    eval_path = tmp_path / "empty.jsonl"
    eval_path.write_text("", encoding="utf-8")
    runner = HarnessRunner(eval_path=eval_path, report_dir=tmp_path / "r", trace_dir=tmp_path / "t")
    assert (runner.base_url, runner.model, runner.api_key_env) == (
        *expected,
        "WATER_AGENT_LLM_API_KEY",
    )
    assert seen(runner._build_real_chat_model()) == expected


def test_endpoint_url_is_validated_and_local_model_is_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P7-S10-A01 base_url 只收 http(s)、拒绝 userinfo 与 fragment；``LOCAL_MODEL_ENABLED`` 与
    ``WATER_AGENT_LLM_*`` 同时启用即配置错误（Web 启动期就失败），只开本地模型时端点就是本地端点。"""

    from water_agent.webapp.app import create_app  # 模块导入即 create_app()：先在干净环境里导入

    for bad in (
        "ftp://x.example/v1",
        "https://user:pw@x.example/v1",
        "https://x.example/v1#frag",
        "x.example",
    ):
        monkeypatch.setenv(ep.CHAT_BASE_URL_ENV, bad)
        with pytest.raises(ep.EndpointConfigError):
            ep.resolve_chat_endpoint()
    monkeypatch.delenv(ep.CHAT_BASE_URL_ENV)

    monkeypatch.setenv("LOCAL_MODEL_ENABLED", "true")
    local = ep.resolve_chat_endpoint()
    assert (local.base_url, local.model, local.source) == (
        "http://local-model:11434/v1",
        "qwen3:8b",
        "local_model",
    )
    assert local.api_key() == "ollama" and not local.is_default

    monkeypatch.setenv(ep.CHAT_MODEL_ENV, "other-model")
    with pytest.raises(ep.EndpointConfigError, match="二选一"):
        ep.resolve_chat_endpoint()
    with pytest.raises(ep.EndpointConfigError):
        create_app(static_dir=Path("/nonexistent-static"))


def test_price_override_is_not_a_hard_cap(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P7-S10-A03 不新增 ``ProviderKind``、出站清单数量不变（换端点仍是 ``DEEPSEEK_CHAT`` 那四个调用点）；
    运维可覆盖对话端点超时（``WATER_AGENT_LLM_TIMEOUT_SECONDS``）与价格（``WATER_AGENT_LLM_PRICE``，挂在当前
    对话模型名上、``confirmed=False``）：价格只用于成本估算，成本远超任何额度也不拒绝；写错即响亮失败。
    回答缓存键带上对话端点摘要（缺省 DeepSeek 为空串）。"""

    from water_agent.runtime.agentic import _build_cache_service
    from water_agent.runtime.governance.binding import local_request_context
    from water_agent.runtime.governance.config import GovernanceConfig, ModelPrice
    from water_agent.runtime.governance.contracts import (
        CallCategory,
        Capability,
        ProviderCall,
        ProviderKind,
    )
    from water_agent.runtime.governance.gateway import ProviderGateway, ProviderUsage
    from water_agent.runtime.governance.inventory import PROVIDER_CALL_SITES

    assert [kind.value for kind in ProviderKind] == [
        "deepseek_chat",
        "bailian_embedding",
        "bailian_rerank",
        "bailian_vision",
        "mcp_remote",
    ]
    assert len(PROVIDER_CALL_SITES) == 8
    assert sorted(
        site.site_id for site in PROVIDER_CALL_SITES if site.provider is ProviderKind.DEEPSEEK_CHAT
    ) == [
        "deepseek_chat_agentic_loop",
        "deepseek_chat_docgen_polish",
        "deepseek_chat_judge",
        "deepseek_chat_legacy_rag",
    ]

    default = GovernanceConfig()
    assert default.profile_for(ProviderKind.DEEPSEEK_CHAT).timeout_seconds == 60.0
    assert default.price_for("other-model") is None
    assert _build_cache_service(_session_args(tmp_path))._model_endpoint_digest == ""

    monkeypatch.setenv(ep.CHAT_BASE_URL_ENV, "https://llm.other-provider.example/v1")
    monkeypatch.setenv(ep.CHAT_MODEL_ENV, "other-model")
    monkeypatch.setenv("WATER_AGENT_LLM_PRICE", "1.5, 3, 0.5")
    monkeypatch.setenv("WATER_AGENT_LLM_TIMEOUT_SECONDS", "90")
    config = GovernanceConfig()
    assert config.profile_for(ProviderKind.DEEPSEEK_CHAT).timeout_seconds == 90.0
    price = config.price_for("other-model")
    assert price == ModelPrice(
        input_per_1m=1.5, output_per_1m=3.0, cached_input_per_1m=0.5, confirmed=False
    )
    assert config.budget.daily_cost_cap_usd is None
    digest = _build_cache_service(_session_args(tmp_path))._model_endpoint_digest
    assert digest and digest == ep.resolve_chat_endpoint().cache_digest

    gateway = ProviderGateway(config=config)
    ctx = local_request_context(thread_id="price-override")
    call = ProviderCall(
        provider=ProviderKind.DEEPSEEK_CHAT,
        category=CallCategory.AGENTIC_LOOP,
        model="other-model",
        temperature=0.0,
        estimated_input_tokens=10,
        required_capability=Capability.LLM_INFERENCE,
    )
    for _ in range(3):
        result = gateway.call(
            ctx,
            call,
            lambda: ("ok", ProviderUsage.of(input_tokens=200_000, output_tokens=100_000)),
        )
        assert result.value == "ok"
        assert result.usage_record.cost_usd == price.estimate_usd(200_000, 100_000)

    for name, bad in (
        ("WATER_AGENT_LLM_PRICE", "1.5"),
        ("WATER_AGENT_LLM_PRICE", "a,b"),
        ("WATER_AGENT_LLM_PRICE", "-1,2"),
        ("WATER_AGENT_LLM_TIMEOUT_SECONDS", "0"),
        ("WATER_AGENT_LLM_TIMEOUT_SECONDS", "601"),
        ("WATER_AGENT_LLM_TIMEOUT_SECONDS", "slow"),
    ):
        monkeypatch.setenv(name, bad)
        with pytest.raises(ValueError):
            GovernanceConfig()
        monkeypatch.setenv("WATER_AGENT_LLM_PRICE", "1.5,3")
        monkeypatch.setenv("WATER_AGENT_LLM_TIMEOUT_SECONDS", "90")
