"""纯关键词建库 ``scripts/build_index.py --text-only``（P7-S10-A06，``docs/design/16-p7-plugin-mcp.md`` §7 S10）。

在进程内调用建库脚本的 ``main``（不起子进程），用 fixture 小语料现建，不依赖 ``data/indexes/``。
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pytest

from scripts import build_index as build_index_script
from water_agent import embedding as embedding_module
from water_agent.config.credentials import CREDENTIALS, forget_injected_credentials
from water_agent.retrieval import trivium

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        trivium.RETRIEVAL_MODE_ENV,
        "WATER_AGENT_EMBEDDING_BASE_URL",
        "WATER_AGENT_EMBEDDING_MODEL",
        "WATER_AGENT_INDEX_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in CREDENTIALS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    forget_injected_credentials()


def _curated(tmp_path: Path) -> Path:
    root = tmp_path / "curated"
    (root / "clauses").mkdir(parents=True)
    shutil.copy(FIXTURE, root / "clauses" / "fixture.jsonl")
    return root


def _args(db: Path) -> argparse.Namespace:
    return argparse.Namespace(
        db_path=str(db), dimensions=None, hybrid_alpha=0.7, mock_generator=False
    )


def test_text_only_index_needs_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """P7-S10-A06 ``--text-only`` 在没有任何 Key 的环境里建库成功、零 embedding 调用（不构造 provider），
    ``index_meta.json`` 写 ``retrieval_modes: ["text"]``；这份索引在 ``auto`` 下永远走关键词检索（没配
    embedding 是 ``embedding_not_configured``，配了也是 ``embedding_mismatch``）、强制 ``hybrid`` 即
    ``RETRIEVAL_EMBEDDING_MISMATCH``；纯关键词条目不能写进带向量的索引。"""

    constructed: list[int] = []
    original_init = embedding_module.BailianEmbeddingProvider.__init__

    def counting_init(self, *args, **kwargs) -> None:
        constructed.append(1)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(embedding_module.BailianEmbeddingProvider, "__init__", counting_init)
    db = tmp_path / "index" / "text-only.db"
    argv = ["--text-only", "--curated-root", str(_curated(tmp_path)), "--db-path", str(db)]
    assert build_index_script.main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["text_only"] is True
    assert report["retrieval_modes"] == ["text"] and report["model"] is None
    assert report["total"]["failed"] == 0 and report["total"]["inserted"] > 0
    assert json.loads(trivium.index_meta_path(db).read_text(encoding="utf-8")) == {
        "format": 1,
        "embedding_model": None,
        "embedding_host": None,
        "dimensions": 512,
        "index_version": "text-only__512__curated-v1",
        "retrieval_modes": ["text"],
    }
    assert constructed == []

    from water_agent.runtime.agentic import _build_retriever

    retriever, _ = _build_retriever(_args(db))
    status = retriever.runtime_status
    assert (status["retrieval_mode"], status["retrieval_mode_reason"], status["index_meta"]) == (
        "text",
        "embedding_not_configured",
        "present",
    )
    assert retriever.retrieve("给水管道的设计秒流量如何确定", top_k=3)

    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope-key-0001")
    retriever.close()
    retriever, _ = _build_retriever(_args(db))
    assert retriever.runtime_status["retrieval_mode"] == "text"
    assert retriever.runtime_status["retrieval_mode_reason"] == "embedding_mismatch"
    monkeypatch.setenv(trivium.RETRIEVAL_MODE_ENV, "hybrid")
    with pytest.raises(trivium.EmbeddingMismatchError, match="纯关键词"):
        _build_retriever(_args(db))
    assert constructed == []

    monkeypatch.delenv(trivium.RETRIEVAL_MODE_ENV)
    retriever.close()  # 检索器以只读方式占着库；追加要以读写方式打开
    assert build_index_script.main(argv) == 0, "纯关键词索引之间可以追加"
    capsys.readouterr()

    vector_db = tmp_path / "vector" / "index.db"
    trivium.index_meta_path(vector_db).parent.mkdir(parents=True)
    from water_agent.ingestion.pipeline import ingest_to_trivium, write_index_meta

    class _Provider:
        config = type(
            "Cfg",
            (),
            {
                "model": "qwen3.7-text-embedding",
                "dimensions": 512,
                "index_version": "qwen3.7-text-embedding__512__curated-v1",
                "fallback_enabled": False,
            },
        )()

        @staticmethod
        def embed(text: str) -> list[float]:
            return [0.5] * 512

    ingest_to_trivium(FIXTURE, vector_db, embedding_provider=_Provider(), curated=True)
    write_index_meta(
        vector_db,
        embedding_model="qwen3.7-text-embedding",
        embedding_base_url=None,
        dimensions=512,
        index_version="qwen3.7-text-embedding__512__curated-v1",
        retrieval_modes=("hybrid", "text"),
    )
    assert build_index_script.main([*argv[:3], "--db-path", str(vector_db)]) == 2
    assert "带向量" in capsys.readouterr().out
