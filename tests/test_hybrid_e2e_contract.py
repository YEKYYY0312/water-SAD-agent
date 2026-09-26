"""端到端：真实 triviumdb 写入 → 真库混合检索，验证维度一致与 search_hybrid。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from water_agent.ingestion.pipeline import ingest_to_trivium
from water_agent.retrieval.trivium import TriviumRetriever

MINI = [
    {"spec_id": "GB50013-2018", "article": "9.0.1", "title": "设计流量",
     "text": "给水管道设计秒流量应根据用水定额确定。", "fragment_id": "tiny-1", "source_line": 1},
    {"spec_id": "GB50014-2021", "article": "9.0.2", "title": "排水体制",
     "text": "排水体制分流制或合流制的选择应因地制宜。", "fragment_id": "tiny-2", "source_line": 2},
    {"spec_id": "GB50318-2017", "article": "9.0.3", "title": "规划",
     "text": "城市排水工程规划期限宜与城市总体规划一致。", "fragment_id": "tiny-3", "source_line": 3},
]


class StubProvider:
    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.config = type("Cfg", (), {
            "dimensions": dim, "model": "stub",
            "index_version": f"stub__{dim}__curated-v1", "fallback_enabled": False,
        })()

    def embed(self, text: str) -> list[float]:
        # 确定性伪向量，含文本哈希信号以便区分
        import hashlib
        d = hashlib.sha256(text.encode()).digest()
        return [((d[i % len(d)] / 255.0) - 0.5) for i in range(self.dim)]


def _build_and_retrieve(dim: int, query: str):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        clause = root / "clauses"
        clause.mkdir()
        by_spec = {}
        for r in MINI:
            by_spec.setdefault(r["spec_id"], []).append(r)
        for spec, rows in by_spec.items():
            (clause / f"{spec}.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
            )
        db = root / "index.db"
        provider = StubProvider(dim)
        for f in sorted(clause.glob("*.jsonl")):
            ingest_to_trivium(f, db, embedding_provider=provider, curated=True, dry_run=False)
        retriever = TriviumRetriever(db_path=db, embedding_fn=provider.embed, dim=dim)
        return retriever, retriever.retrieve(query, top_k=1)


_VALID = {"GB50013-2018 9.0.1", "GB50014-2021 9.0.2", "GB50318-2017 9.0.3"}


def test_hybrid_returns_from_real_db_512():
    retriever, hits = _build_and_retrieve(512, "给水管道设计秒流量")
    assert retriever.is_fallback is False, "应命中文档库而非 fallback"
    assert retriever.db_error is None, f"db_error={retriever.db_error}"
    assert len(hits) >= 1, "应该返回命中"
    assert hits[0].source_id in _VALID, hits[0].source_id


def test_hybrid_returns_from_real_db_256():
    retriever, hits = _build_and_retrieve(256, "排水体制分流制合流制的选择")
    assert retriever.is_fallback is False, "应命中文档库而非 fallback"
    assert retriever.db_error is None, f"db_error={retriever.db_error}"
    assert len(hits) >= 1
    assert hits[0].source_id in _VALID, hits[0].source_id


class _ZeroVectorProvider(StubProvider):
    """query 向量恒为 0：search_hybrid 的向量分量失效，命中只能来自文本索引。"""

    def embed(self, text: str) -> list[float]:
        return [0.0] * self.dim


def test_text_index_available_on_reopen_so_hybrid_is_not_pure_vector():
    """重开库后文本分量必须仍然有效，否则 hybrid 静默退化为纯向量。

    用零向量 + hybrid_alpha=0.0 强制只看文本分量：能按 query 关键词命中对应条款才算文本索引有效。
    这里不约束走哪条路（入库会 flush 出 ``.text`` 侧文件，因此实际走 persisted）；
    两条路各自的归属由下面两个测试分别钉住。
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        clause = root / "clauses"
        clause.mkdir()
        (clause / "mini.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in MINI), encoding="utf-8"
        )
        db = root / "index.db"
        ingest_to_trivium(clause / "mini.jsonl", db, embedding_provider=StubProvider(256), curated=True)

        # 模拟新进程：重新打开，且 query 向量恒为 0
        zero = _ZeroVectorProvider(256)
        retriever = TriviumRetriever(db_path=db, embedding_fn=zero.embed, dim=256, hybrid_alpha=0.0)
        assert retriever.text_index_ready is True
        assert retriever.db_error is None

        hits = retriever.retrieve("排水体制 分流制 合流制", top_k=1)
        assert hits and hits[0].source_id == "GB50014-2021 9.0.2", [h.source_id for h in hits]
        hits = retriever.retrieve("城市排水工程规划期限", top_k=1)
        assert hits and hits[0].source_id == "GB50318-2017 9.0.3", [h.source_id for h in hits]
        retriever.close()


def _ingest_mini(root: Path) -> Path:
    clause = root / "clauses"
    clause.mkdir()
    (clause / "mini.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in MINI), encoding="utf-8"
    )
    db = root / "index.db"
    ingest_to_trivium(clause / "mini.jsonl", db, embedding_provider=StubProvider(256), curated=True)
    return db


def test_missing_text_sidecar_falls_back_to_in_process_rebuild():
    """没有 ``.text`` 侧文件的库必须退回 read_write + 进程内重建，命中与 persisted 一致。

    只读模式下 ``index_text`` 会被 triviumdb 拒绝，所以这条路不能也用只读打开。
    旧库、以及增量重跑后丢掉侧文件的库都会走到这里。
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = _ingest_mini(Path(tmp))
        zero = _ZeroVectorProvider(256)
        query = "排水体制 分流制 合流制"

        sidecar = Path(str(db) + ".text")
        assert sidecar.is_file(), "入库时的 build_text_index + flush 应写出持久化文本索引"
        persisted = TriviumRetriever(db_path=db, embedding_fn=zero.embed, dim=256, hybrid_alpha=0.0)
        assert persisted.runtime_status["access_mode"] == "read_only"
        assert persisted.runtime_status["text_index_source"] == "persisted"
        expected = [h.source_id for h in persisted.retrieve(query, top_k=1)]
        persisted.close()
        assert expected == ["GB50014-2021 9.0.2"]

        sidecar.unlink()
        Path(str(db) + ".text.meta").unlink(missing_ok=True)
        rebuilt = TriviumRetriever(db_path=db, embedding_fn=zero.embed, dim=256, hybrid_alpha=0.0)
        assert rebuilt.runtime_status["access_mode"] == "read_write"
        assert rebuilt.runtime_status["text_index_source"] == "rebuilt"
        assert rebuilt.text_index_ready is True
        assert rebuilt.db_error is None
        assert [h.source_id for h in rebuilt.retrieve(query, top_k=1)] == expected
        rebuilt.close()


def test_retrieval_works_on_a_read_only_filesystem():
    """只读根文件系统（compose 的 ``read_only: true``）下必须仍能真库检索。

    triviumdb 默认的 ``access_mode="read_write"`` 在打开阶段就要求文件可写、``close()``
    还要求目录可写，容器里会直接失败并静默退成 fixture 兜底。这条测试钉住只读部署，
    是 B1 的验收条件，不能靠"本机目录恰好可写"蒙过去。
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db = _ingest_mini(root)
        index_dir = db.parent
        written = sorted(index_dir.glob(db.name + "*"))
        try:
            for path in written:
                path.chmod(0o444)
            index_dir.chmod(0o555)

            retriever = TriviumRetriever(
                db_path=db, embedding_fn=_ZeroVectorProvider(256).embed, dim=256, hybrid_alpha=0.0
            )
            assert retriever.runtime_status["access_mode"] == "read_only"
            assert retriever.is_fallback is False, retriever.db_error
            assert retriever.text_index_ready is True
            hits = retriever.retrieve("排水体制 分流制 合流制", top_k=1)
            assert hits and hits[0].source_id == "GB50014-2021 9.0.2"
            retriever.close()
        finally:
            # TemporaryDirectory 的清理需要写权限
            index_dir.chmod(0o755)
            for path in written:
                path.chmod(0o644)
