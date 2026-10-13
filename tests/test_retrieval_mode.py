"""检索模式 ``WATER_AGENT_RETRIEVAL_MODE=auto|hybrid|text``（P7-S10-A05，``docs/design/16-p7-plugin-mcp.md`` §7 S10）。

真实 embedding 请求一律经 monkeypatch 截获计数，不出网；对话模型用离线脚本模型注入。
"""

from __future__ import annotations

import argparse
import json
from contextlib import ExitStack
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver

from water_agent import embedding as embedding_module
from water_agent.config.credentials import CREDENTIALS, forget_injected_credentials
from water_agent.generation import endpoints as ep
from water_agent.ingestion.pipeline import ingest_to_trivium
from water_agent.retrieval import trivium
from water_agent.runtime.agentic import (
    AgenticSession,
    _build_cache_service,
    _build_retriever,
    _ScriptedAgenticModel,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"
QUERY = "给水管道的设计秒流量如何确定"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        trivium.RETRIEVAL_MODE_ENV,
        ep.EMBEDDING_BASE_URL_ENV,
        ep.EMBEDDING_MODEL_ENV,
        "WATER_AGENT_INDEX_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    forget_injected_credentials()


class _Provider:
    def __init__(self, dimensions: int = 512) -> None:
        self.config = type(
            "Cfg",
            (),
            {
                "model": "qwen3.7-text-embedding",
                "dimensions": dimensions,
                "index_version": f"qwen3.7-text-embedding__{dimensions}__curated-v1",
                "fallback_enabled": False,
            },
        )()

    def embed(self, text: str) -> list[float]:
        return [((len(text) * 7 + i) % 11) / 11.0 for i in range(self.config.dimensions)]


@pytest.fixture
def index(tmp_path: Path) -> Path:
    db = tmp_path / "index" / "tiny.db"
    ingest_to_trivium(FIXTURE, db, embedding_provider=_Provider(), curated=True)
    return db


@pytest.fixture
def embedding_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """数真实 embedding provider 的构造与请求次数（请求被截获，返回固定向量）。"""

    counts = {"built": 0, "requests": 0}
    original_init = embedding_module.BailianEmbeddingProvider.__init__

    def counting_init(self, *args, **kwargs) -> None:
        counts["built"] += 1
        original_init(self, *args, **kwargs)

    def fake_request(self, text: str) -> list[float]:
        counts["requests"] += 1
        return [0.1] * self.config.dimensions

    monkeypatch.setattr(embedding_module.BailianEmbeddingProvider, "__init__", counting_init)
    monkeypatch.setattr(
        embedding_module.BailianEmbeddingProvider, "_request_embedding", fake_request
    )
    return counts


def _args(tmp_path: Path, db: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "db_path": str(db),
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


def _ask(tmp_path: Path, db: Path, **overrides: object) -> tuple[dict, dict, AgenticSession]:
    with ExitStack() as stack:
        session = AgenticSession(
            _args(tmp_path, db, **overrides),
            stack=stack,
            checkpointer=MemorySaver(),
            model=_ScriptedAgenticModel(),
            mcp_enabled=False,
        )
        payload = session.ask(QUERY)
    trace_files = sorted((tmp_path / "traces").glob("*.json"))
    trace = json.loads(trace_files[-1].read_text(encoding="utf-8")) if trace_files else {}
    return payload, trace, session


def _mode_events(trace: dict) -> list[dict]:
    return [
        event for event in trace.get("events", []) if event.get("event_type") == "retrieval_mode"
    ]


def test_auto_falls_back_to_text_visibly(
    tmp_path: Path, index: Path, embedding_calls: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S10-A05 ``auto``（缺省）下没配 embedding：真实索引上走关键词检索，``retrieval_mode=text`` /
    ``embedding_not_configured`` 进运行状态、会话结果与 trace，回答仍带引用；配了且与索引匹配走混合检索
    （``embedding_matched``）；``text`` / ``hybrid`` 可强制；取值不对响亮失败。关键词检索的回答缓存键与混合
    检索不同（索引版本因子记 ``text-only``）。"""

    payload, trace, session = _ask(tmp_path, index)
    status = payload["retriever_runtime_status"]
    assert (status["retrieval_mode"], status["retrieval_mode_reason"]) == (
        "text",
        "embedding_not_configured",
    )
    assert status["is_fallback"] is False and status["db_opened"] is True
    assert payload["status"] == "succeeded" and payload["citations"]
    events = _mode_events(trace)
    assert [event["data"]["retrieval_mode"] for event in events] == ["text"]
    assert events[0]["data"]["retrieval_mode_reason"] == "embedding_not_configured"
    assert session._keyword_index() is True
    assert (
        "text-only"
        in _build_cache_service(_args(tmp_path, index), keyword_index=True).corpus_version
    )
    assert embedding_calls == {"built": 0, "requests": 0}

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope-key-0001")
    payload, trace, session = _ask(tmp_path / "configured", index)
    status = payload["retriever_runtime_status"]
    assert (status["retrieval_mode"], status["retrieval_mode_reason"]) == (
        "hybrid",
        "embedding_matched",
    )
    assert status["index_meta"] == "absent"
    assert [event["data"]["retrieval_mode"] for event in _mode_events(trace)] == ["hybrid"]
    assert embedding_calls["requests"] > 0
    assert session._keyword_index() is False
    assert "text-only" not in _build_cache_service(_args(tmp_path, index)).corpus_version

    monkeypatch.setenv(trivium.RETRIEVAL_MODE_ENV, "text")
    payload, _, _ = _ask(tmp_path / "forced-text", index)
    assert payload["retriever_runtime_status"]["retrieval_mode"] == "text"
    assert payload["retriever_runtime_status"]["retrieval_mode_reason"] == "forced"

    monkeypatch.setenv(trivium.RETRIEVAL_MODE_ENV, "hybrid")
    monkeypatch.delenv("DASHSCOPE_API_KEY")
    with pytest.raises(ValueError, match="DASHSCOPE_API_KEY"):
        _build_retriever(_args(tmp_path, index))
    monkeypatch.setenv(trivium.RETRIEVAL_MODE_ENV, "semantic")
    with pytest.raises(ValueError, match="auto|hybrid|text"):
        _build_retriever(_args(tmp_path, index))

    monkeypatch.delenv(trivium.RETRIEVAL_MODE_ENV)
    fixture_only, _ = _build_retriever(_args(tmp_path, index, db_path=None, mock_generator=True))
    assert fixture_only.runtime_status["retrieval_mode"] == "text"
    assert fixture_only.runtime_status["retrieval_mode_reason"] == "fixture"


def test_text_mode_makes_no_embedding_calls(
    tmp_path: Path, index: Path, embedding_calls: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7-S10-A05 text 模式一次 embedding 都不调用（不构造 provider、不发请求、连维度探测都不碰向量函数），
    即使配了 embedding Key；排序与"零向量 + ``hybrid_alpha=0``"在未改动的检索器上逐条相同（Step 1 的
    "改之前"口径）。mock 生成器同一规则：判定为 text 时也不用伪向量。"""

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope-key-0001")
    monkeypatch.setenv(trivium.RETRIEVAL_MODE_ENV, "text")
    retriever, _ = _build_retriever(_args(tmp_path, index))
    hits = [hit.source_id for hit in retriever.retrieve(QUERY, top_k=5)]
    assert hits and embedding_calls == {"built": 0, "requests": 0}

    def must_not_embed(_text: str) -> list[float]:
        raise AssertionError("text 模式不得调用 embedding")

    direct = trivium.TriviumRetriever(
        db_path=index,
        embedding_fn=must_not_embed,
        retrieval_mode="text",
        retrieval_mode_reason="forced",
    )
    baseline = trivium.TriviumRetriever(
        db_path=index, embedding_fn=lambda _t: [0.0] * 512, hybrid_alpha=0.0, dim=512
    )
    # TriviumDB 对近乎同分的条目排序不固定（并行求和的浮点抖动，见决策记录），所以比全部命中的集合
    everything = 50
    assert sorted(hit.source_id for hit in direct.retrieve(QUERY, top_k=everything)) == sorted(
        hit.source_id for hit in baseline.retrieve(QUERY, top_k=everything)
    )
    assert set(hits) <= {hit.source_id for hit in direct.retrieve(QUERY, top_k=everything)}

    mock_text, _ = _build_retriever(_args(tmp_path, index, mock_generator=True))
    assert mock_text.runtime_status["retrieval_mode"] == "text"
    assert mock_text.runtime_status["fallback_kind"] == "none"
    monkeypatch.delenv(trivium.RETRIEVAL_MODE_ENV)
    mock_hybrid, _ = _build_retriever(_args(tmp_path, index, mock_generator=True))
    assert mock_hybrid.runtime_status["retrieval_mode"] == "hybrid"
    assert mock_hybrid.runtime_status["fallback_kind"] == "mock"
    assert embedding_calls == {"built": 0, "requests": 0}
