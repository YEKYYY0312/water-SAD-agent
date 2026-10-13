"""对话 / embedding 端点解析（P7-S10-A01，``docs/design/16-p7-plugin-mcp.md`` §7 S10）。

全程离线：只构造对象、读解析结果，不发任何请求。
"""

from __future__ import annotations

import argparse
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
