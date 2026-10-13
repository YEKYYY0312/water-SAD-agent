"""embedding 端点可配置与索引绑定（P7-S10-A04，``docs/design/16-p7-plugin-mcp.md`` §7 S10）。

全程离线：真实 embedding 请求经 monkeypatch 截获（只看发往哪里、带哪把 Key、请求什么模型），不出网。
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
from pathlib import Path

import pytest

from water_agent import embedding as embedding_module
from water_agent.config.credentials import CREDENTIALS, forget_injected_credentials
from water_agent.embedding import (
    BailianEmbeddingConfig,
    build_embedding_provider,
    index_version_for,
)
from water_agent.generation import endpoints as ep
from water_agent.ingestion.pipeline import ingest_to_trivium, write_index_meta
from water_agent.retrieval import trivium

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        ep.EMBEDDING_BASE_URL_ENV,
        ep.EMBEDDING_MODEL_ENV,
        ep.CHAT_BASE_URL_ENV,
        ep.CHAT_MODEL_ENV,
        "WATER_AGENT_RETRIEVAL_MODE",
        "WATER_AGENT_INDEX_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    forget_injected_credentials()


class _StubProvider:
    """建库用的确定性 provider：只提供元数据与固定向量，不出网。"""

    def __init__(self, model: str, dimensions: int) -> None:
        self.config = type(
            "Cfg",
            (),
            {
                "model": model,
                "dimensions": dimensions,
                "index_version": index_version_for(model, dimensions),
                "fallback_enabled": False,
                "base_url": "https://embed.example/v1",
            },
        )()
        self.calls = 0

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        return [((len(text) + i) % 7) / 7.0 for i in range(self.config.dimensions)]


def _index(
    tmp_path: Path,
    *,
    model: str = "qwen3.7-text-embedding",
    dimensions: int = 512,
    meta: bool = True,
) -> Path:
    db = tmp_path / "index" / "tiny.db"
    provider = _StubProvider(model, dimensions)
    ingest_to_trivium(FIXTURE, db, embedding_provider=provider, curated=True)
    if meta:
        write_index_meta(
            db,
            embedding_model=model,
            embedding_base_url="https://embed.example/v1",
            dimensions=dimensions,
            index_version=index_version_for(model, dimensions),
            retrieval_modes=("hybrid", "text"),
        )
    return db


def _args(db: Path) -> argparse.Namespace:
    return argparse.Namespace(
        db_path=str(db), dimensions=None, hybrid_alpha=0.7, mock_generator=False
    )


def _count_embedding_requests(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    seen: list[dict] = []

    def fake(self, text: str) -> list[float]:
        seen.append({"model": self.config.model, "text": text})
        return [0.0] * self.config.dimensions

    monkeypatch.setattr(embedding_module.BailianEmbeddingProvider, "_request_embedding", fake)
    return seen


def test_any_openai_compatible_embedding_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S10-A04 embedding 端点 / 模型 / Key 可配置：请求发往配置的 ``<base_url>/embeddings``、带专用 Key、
    请求配置的模型；``index_version`` 对任意模型都是 ``<model>__<dim>__curated-v1``；缺省仍是百炼 qwen3.7。"""

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope-key-0001")
    default = build_embedding_provider()
    assert (default.config.model, default.config.base_url, default.config.index_version) == (
        "qwen3.7-text-embedding",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "qwen3.7-text-embedding__512__curated-v1",
    )

    monkeypatch.setenv(ep.EMBEDDING_BASE_URL_ENV, "https://embed.other-provider.example/v1")
    monkeypatch.setenv(ep.EMBEDDING_MODEL_ENV, "bge-m3")
    with pytest.raises(ValueError, match="WATER_AGENT_EMBEDDING_API_KEY must not be empty"):
        build_embedding_provider()
    monkeypatch.setenv("WATER_AGENT_EMBEDDING_API_KEY", "sk-embedding-provider-0001")
    provider = build_embedding_provider(dimensions=1024)
    assert provider.config.index_version == "bge-m3__1024__curated-v1"

    captured: dict = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return io.BytesIO(json.dumps({"data": [{"embedding": [0.5] * 1024}]}).encode("utf-8"))

    monkeypatch.setattr(embedding_module.request, "urlopen", fake_urlopen)
    assert len(provider.embed("排水管道")) == 1024
    assert captured["url"] == "https://embed.other-provider.example/v1/embeddings"
    assert captured["auth"] == "Bearer sk-embedding-provider-0001"
    assert captured["body"]["model"] == "bge-m3" and captured["body"]["dimensions"] == 1024

    assert BailianEmbeddingConfig(
        api_key="k", model="bge-m3", dimensions=1536, index_version="bge-m3__1536__curated-v1"
    )
    with pytest.raises(ValueError, match="256, 512, 768, 1024"):
        BailianEmbeddingConfig(
            api_key="k", dimensions=1536, index_version="qwen3.7-text-embedding__1536__curated-v1"
        )


def test_legacy_index_without_meta(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """P7-S10-A04 没有 ``index_meta.json`` 的旧索引按百炼缺省模型（qwen3.7-text-embedding、库的实际维度）对待：
    缺省配置照常向量检索（向后兼容），运行状态写 ``index_meta=absent``；换了 embedding 模型即不匹配。"""

    db = _index(tmp_path, meta=False)
    meta = trivium.read_index_meta(db, dimensions=512)
    assert meta.status == trivium.INDEX_META_ABSENT
    assert (meta.embedding_model, meta.dimensions, meta.has_vectors) == (
        "qwen3.7-text-embedding",
        512,
        True,
    )
    assert trivium.embedding_mismatch(meta, model="qwen3.7-text-embedding", dimensions=512) is None
    assert trivium.embedding_mismatch(meta, model="bge-m3", dimensions=512)

    from water_agent.runtime.agentic import _build_retriever

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope-key-0001")
    seen = _count_embedding_requests(monkeypatch)
    retriever, _ = _build_retriever(_args(db))
    assert retriever.runtime_status["index_meta"] == "absent"
    assert retriever.retrieve("给水管道流速", top_k=3)
    assert seen and all(call["model"] == "qwen3.7-text-embedding" for call in seen)


def test_mismatched_embedding_never_searches_vectors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """P7-S10-A04 当前 embedding 模型或维度与 ``index_meta.json`` 不一致、索引是纯关键词索引、或元数据读不出：
    一次向量检索（一次 embedding 请求）都不做——``auto`` 降级到关键词检索并在运行状态里如实标出
    （``retrieval_mode=text``、``embedding_mismatch`` 与具体原因），强制 ``hybrid`` 即
    ``RETRIEVAL_EMBEDDING_MISMATCH``；会话与 runner 两条装配路径同一道检查；建库脚本拒绝往别的模型建的索引里
    追加。"""

    from water_agent.harness.runner import HarnessRunner
    from water_agent.runtime.agentic import _build_retriever

    db = _index(tmp_path, model="bge-m3", dimensions=512)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope-key-0001")
    seen = _count_embedding_requests(monkeypatch)
    eval_path = tmp_path / "empty.jsonl"
    eval_path.write_text("", encoding="utf-8")
    runner = HarnessRunner(
        eval_path=eval_path, report_dir=tmp_path / "r", trace_dir=tmp_path / "t", db_path=db
    )

    retriever, _ = _build_retriever(_args(db))
    status = retriever.runtime_status
    assert (status["retrieval_mode"], status["retrieval_mode_reason"]) == (
        "text",
        "embedding_mismatch",
    )
    assert (
        "bge-m3" in status["retrieval_mode_detail"]
        and "qwen3.7-text-embedding" in status["retrieval_mode_detail"]
    )
    assert retriever.retrieve("给水管道的设计秒流量", top_k=3), "关键词检索照常可用"
    assert runner._build_real_retriever().runtime_status["retrieval_mode"] == "text"
    assert seen == []

    monkeypatch.setenv("WATER_AGENT_RETRIEVAL_MODE", "hybrid")
    with pytest.raises(trivium.EmbeddingMismatchError) as caught:
        _build_retriever(_args(db))
    assert caught.value.code == "RETRIEVAL_EMBEDDING_MISMATCH" and "bge-m3" in caught.value.reason
    with pytest.raises(trivium.EmbeddingMismatchError):
        runner._build_real_retriever()

    write_index_meta(
        db,
        embedding_model="qwen3.7-text-embedding",
        embedding_base_url=None,
        dimensions=256,
        index_version="qwen3.7-text-embedding__256__curated-v1",
        retrieval_modes=("hybrid", "text"),
    )
    with pytest.raises(trivium.EmbeddingMismatchError, match="维度"):
        _build_retriever(_args(db))
    write_index_meta(
        db,
        embedding_model=None,
        embedding_base_url=None,
        dimensions=512,
        index_version="text-only__512__curated-v1",
        retrieval_modes=("text",),
    )
    with pytest.raises(trivium.EmbeddingMismatchError, match="纯关键词"):
        _build_retriever(_args(db))
    trivium.index_meta_path(db).write_text("{ 坏的", encoding="utf-8")
    with pytest.raises(trivium.EmbeddingMismatchError):
        _build_retriever(_args(db))
    monkeypatch.delenv("WATER_AGENT_RETRIEVAL_MODE")
    retriever, _ = _build_retriever(_args(db))
    assert retriever.runtime_status["retrieval_mode"] == "text"
    assert retriever.runtime_status["index_meta"] == "invalid"
    assert seen == []

    from scripts import build_index as build_index_script

    curated = tmp_path / "curated"
    (curated / "clauses").mkdir(parents=True)
    shutil.copy(FIXTURE, curated / "clauses" / "fixture.jsonl")
    target = tmp_path / "built" / "index.db"
    monkeypatch.setattr(
        build_index_script,
        "build_embedding_provider",
        lambda dimensions: _StubProvider("bge-m3", dimensions),
    )
    argv = ["--curated-root", str(curated), "--db-path", str(target)]
    assert build_index_script.main(argv) == 0
    written = json.loads(trivium.index_meta_path(target).read_text(encoding="utf-8"))
    assert written == {
        "format": 1,
        "embedding_model": "bge-m3",
        "embedding_host": "embed.example",
        "dimensions": 512,
        "index_version": "bge-m3__512__curated-v1",
        "retrieval_modes": ["hybrid", "text"],
    }
    monkeypatch.setattr(
        build_index_script,
        "build_embedding_provider",
        lambda dimensions: _StubProvider("qwen3.7-text-embedding", dimensions),
    )
    assert build_index_script.main(argv) == 2
    assert (
        json.loads(trivium.index_meta_path(target).read_text(encoding="utf-8"))["embedding_model"]
        == "bge-m3"
    )
