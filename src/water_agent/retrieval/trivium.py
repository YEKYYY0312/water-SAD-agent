"""TriviumDB 适配器：子块检索 + parent_id 回填父块上下文。

P0 阶段不要求真实向量库已就绪：当 ``db_path`` 不存在或打不开时自动回落到
fixture 检索，保证无真实数据也能完整跑通 RAG / Harness / CLI。
真实库就绪后会走 ``TriviumDB.search_hybrid`` 路径；一旦真实库检索链路失败，
不得静默退回 fixture，而应抛出结构化错误，由上层图/CLI 记录失败。
真实库 0 命中是合法结果（返回空列表，不回落、不抛错）。

文本索引：triviumdb 的文本索引不落盘，打开库时由 ``_rebuild_text_index`` 用 payload
重灌并 ``build_text_index``，否则 ``search_hybrid`` 退化为纯向量（见该方法说明）。

P1 父子块语义
--------------
- 入库：仅子块（article/clause/item/table/paragraph/text_window）进向量+文本索引
- 检索：命中子块后按 ``parent_id`` 回填父块（chapter/section/subsection）上下文
- 返回文本：``子块.text + 父块上下文``，引用绑定子块 ``article``（如 GB50014-2021 4.4.2）
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path

from water_agent.domain.models import Chunk, RetrievedClause
from water_agent.retrieval.base import Retriever
from water_agent.retrieval.in_memory import InMemoryRetriever
from water_agent.retrieval.loader import load_clauses_jsonl

TRIVIUM_DIM = 1536
DEFAULT_HYBRID_ALPHA = 0.7
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FIXTURE_PATH = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"


class TriviumRetrievalError(RuntimeError):
    """真实 TriviumDB 检索失败时抛出。"""


def mock_embed(text: str, dim: int = TRIVIUM_DIM) -> list[float]:
    """确定性伪嵌入：仅用于 P0/P1 离线验证与适配器自检，不替代真实嵌入模型。"""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    out: list[float] = []
    for i in range(dim):
        out.append((digest[i % len(digest)] / 255.0 - 0.5) * 2.0)
    return out


def _default_fixture_clauses() -> list[RetrievedClause]:
    clauses = []
    for c in load_clauses_jsonl(DEFAULT_FIXTURE_PATH):
        clauses.append(
            RetrievedClause(
                spec_id=c.spec_id,
                article=c.article,
                chapter=c.chapter,
                section=c.section,
                title=c.title,
                text=c.text,
            )
        )
    return clauses


def _parents_path(db_path: str | Path) -> Path:
    return Path(str(db_path) + ".parents.json")


def _friendly_parent_label(chunk: Chunk) -> str:
    if chunk.chunk_type == "chapter" and chunk.chapter:
        if chunk.title and not chunk.title.startswith("第"):
            return chunk.title
        return f"第{chunk.chapter}章"
    if chunk.chunk_type in {"section", "subsection"}:
        # 标题型无条号父块：chapter/section 常为哈希，不应直接展示给用户
        if chunk.chapter and re.fullmatch(r"[0-9a-f]{8}", chunk.chapter) and chunk.section and re.fullmatch(r"[0-9a-f]{16}", chunk.section):
            if chunk.title:
                plain_title = re.sub(r"^第[一二三四五六七八九十百千0-9]+章\s*", "", chunk.title).strip()
                return f"标题块 / {plain_title or chunk.title} / {chunk.chapter}"
            return f"标题块 / {chunk.chapter}"
        if chunk.title and not re.fullmatch(r"[\d.]+", chunk.title.strip()):
            return chunk.title
        if chunk.chapter and chunk.section:
            return f"{chunk.chapter} / {chunk.section}"
    return chunk.title or chunk.section or chunk.chapter or "标题块"


class TriviumRetriever(Retriever):
    """基于 TriviumDB 的混合检索适配器，失败时回落到 fixture 或抛错。"""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        fallback_clauses: list[RetrievedClause] | None = None,
        parent_store: dict[str, Chunk] | None = None,
        embedding_fn: Callable[[str], list[float]] | None = None,
        hybrid_alpha: float = DEFAULT_HYBRID_ALPHA,
        dim: int | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else None
        self._embedding_fn = embedding_fn or mock_embed
        self._hybrid_alpha = hybrid_alpha
        self._fallback = InMemoryRetriever(fallback_clauses or _default_fixture_clauses())
        self._parent_store: dict[str, Chunk] = parent_store or self._load_parent_store()
        self._db = None
        self._db_error: str | None = None
        self._db_opened = False
        self._used_fallback = self.db_path is None or (self.db_path is not None and not self.db_path.exists())
        if self.db_path is not None and self.db_path.exists():
            try:
                import triviumdb

                open_dim = dim
                # 未显式指定时，用 query 向量实际维度推断（写读一致）
                if open_dim is None:
                    try:
                        probe_len = len(self._embedding_fn("维度探测"))
                    except Exception:  # noqa: BLE001 — 探测失败回退默认维度
                        probe_len = 0
                    open_dim = probe_len or 1536
                self._db = triviumdb.TriviumDB(str(self.db_path), dim=open_dim)
                self._db_opened = True
                self._used_fallback = False
            except Exception as exc:  # noqa: BLE001 — pragma: no cover — triviumdb 打开失败降级 fixture
                self._db_error = str(exc)
                self._db = None
                self._used_fallback = True
        self._text_index_ready = False
        self._text_indexed_nodes = 0
        if self._db is not None:
            self._rebuild_text_index()

    def _rebuild_text_index(self) -> None:
        """打开库后重建文本索引（triviumdb 0.8.2 的文本索引只存在于进程内）。

        实测：``index_text`` + ``build_text_index`` 的结果不随 ``flush``/``close`` 持久化，
        重开后 ``search_hybrid`` 的文本分量恒为空——``hybrid_alpha`` 形同虚设、退化为纯向量。
        因此每次打开都用 payload 里的 ``text`` 重灌一遍（3k 节点 <0.1s）。失败不静默：
        记入 ``db_error`` 并让 ``retrieve`` 抛 ``TriviumRetrievalError``。
        """
        assert self._db is not None
        try:
            count = 0
            for node_id in self._db.all_node_ids():
                payload = self._db.get_payload(int(node_id)) or {}
                text = payload.get("text") or ""
                if text:
                    self._db.index_text(int(node_id), text)
                    count += 1
            self._db.build_text_index()
            self._text_index_ready = True
            self._text_indexed_nodes = count
        except Exception as exc:  # noqa: BLE001 — 文本索引失败必须可观测，不能退化为纯向量
            self._db_error = f"text index rebuild failed: {exc}"
            self._text_index_ready = False

    @property
    def text_index_ready(self) -> bool:
        """真实库的文本索引是否已在本进程重建（False 时 retrieve 会拒绝服务）。"""
        return self._text_index_ready

    @property
    def parent_store(self) -> dict[str, Chunk]:
        return self._parent_store

    def _load_parent_store(self) -> dict[str, Chunk]:
        if self.db_path is None:
            return {}
        path = _parents_path(self.db_path)
        if not path.exists():
            self._db_error = f"parent_store missing: {path}"
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return {chunk_id: Chunk.model_validate(data) for chunk_id, data in payload.items()}
        except Exception as exc:  # noqa: BLE001 — parent_store 损坏时降级为空映射
            self._db_error = f"parent_store load failed: {exc}"
            return {}

    @property
    def is_fallback(self) -> bool:
        return self._used_fallback and self._db is None

    @property
    def db_error(self) -> str | None:
        return self._db_error

    def _enrich_with_parent(self, clause: RetrievedClause, payload: dict) -> RetrievedClause:
        parent_id = payload.get("parent_id")
        if not parent_id:
            return clause
        parent = self._parent_store.get(str(parent_id))
        if parent is None:
            return clause
        if parent.chunk_type in {"section", "subsection"} and parent.chapter and parent.section:
            if re.fullmatch(r"[0-9a-f]{8}", parent.chapter) and re.fullmatch(r"[0-9a-f]{16}", parent.section):
                prefix = _friendly_parent_label(parent)
            else:
                prefix_parts = [p for p in [parent.chapter, parent.section, _friendly_parent_label(parent)] if p]
                prefix = " / ".join(dict.fromkeys(prefix_parts))
        else:
            prefix = " / ".join([p for p in [parent.chapter, parent.section, _friendly_parent_label(parent)] if p])
        if prefix:
            clause.text = f"【{prefix}】\n{clause.text}"
            if not clause.chapter and parent.chapter and not re.fullmatch(r"[0-9a-f]{8}", parent.chapter):
                clause.chapter = parent.chapter
            if not clause.section and parent.section and not re.fullmatch(r"[0-9a-f]{16}", parent.section):
                clause.section = parent.section
        return clause

    def retrieve(self, query: str, top_k: int = 5) -> list[RetrievedClause]:
        if not query or not query.strip():
            return []
        if self._db is None:
            return self._fallback.retrieve(query, top_k=top_k)
        if not self._text_index_ready:
            raise TriviumRetrievalError(
                f"text index not ready for db={self.db_path}: {self._db_error or 'unknown'}"
            )
        try:
            qvec = self._embedding_fn(query)
        except Exception as exc:
            self._db_error = str(exc)
            raise TriviumRetrievalError(f"embedding failed for query={query!r}: {exc}") from exc

        try:
            hits = self._db.search_hybrid(qvec, query, top_k=top_k, hybrid_alpha=self._hybrid_alpha)
        except Exception as exc:
            self._db_error = str(exc)
            raise TriviumRetrievalError(f"search_hybrid failed for query={query!r}: {exc}") from exc

        out: list[RetrievedClause] = []
        for hit in hits:
            payload = getattr(hit, "payload", {}) or {}
            chunk_type = payload.get("chunk_type")
            try:
                if chunk_type:
                    chunk = Chunk.model_validate(payload)
                    clause = RetrievedClause(
                        spec_id=chunk.spec_id,
                        article=chunk.article or "",
                        chapter=chunk.chapter,
                        section=chunk.section,
                        title=chunk.title,
                        text=chunk.text,
                    )
                else:
                    clause = RetrievedClause.model_validate(payload)
            except Exception as exc:
                self._db_error = str(exc)
                raise TriviumRetrievalError(f"payload parse failed for query={query!r}: {exc}") from exc
            clause.score = float(getattr(hit, "score", 0.0) or 0.0)
            clause = self._enrich_with_parent(clause, payload)
            out.append(clause)
        # 0 命中是合法结果（语料里没有相关条款），交给上层报"检索无结果"；
        # 这里绝不回落 fixture（is_fallback 保持 False），也不当成链路故障抛错。
        return out

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:  # noqa: BLE001, S110 — 关闭失败无需上报
                pass
