"""知识库入库管线：JSONL/markdown → 父子 chunks → TriviumDB 子块向量化。

幂等策略
--------
TriviumDB 0.8.2 没有按业务键查询 payload 的原生 API（`get(id)` 仅按数字 node_id），
因此用稳定 `chunk_id`（sha256(稳定字段+content_hash)[:16]）转成整数 node_id 作为幂等键：

- node_id 不存在 → `insert_with_id`，计入 `inserted`
- node_id 已存在 → `upsert_with_id` 覆盖 payload/向量并重建文本索引，计入 `updated`
- `index_text`/`flush` 失败 → 记入结构化 issues 并抛错，不计入成功计数

父块持久化
----------
父块（chapter/section/subsection）写入 `<db_path>.parents.json`，
`TriviumRetriever` 在未显式传入 `parent_store` 时自动加载，实现跨进程回填。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from water_agent.domain.models import Chunk
from water_agent.ingestion.chunker import ChunkConfig, Chunker, RawClause
from water_agent.retrieval.loader import load_clauses_jsonl


@dataclass(slots=True)
class IngestIssue:
    source_file: str
    chunk_id: str
    operation: str
    error: str

    def as_dict(self) -> dict[str, str]:
        return {
            "source_file": self.source_file,
            "chunk_id": self.chunk_id,
            "operation": self.operation,
            "error": self.error,
        }


@dataclass(slots=True)
class IngestStats:
    total_chunks: int = 0
    parent_chunks: int = 0
    child_payloads: int = 0
    inserted: int = 0
    skipped_existing: int = 0
    updated: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "total_chunks": self.total_chunks,
            "parent_chunks": self.parent_chunks,
            "child_payloads": self.child_payloads,
            "inserted": self.inserted,
            "skipped_existing": self.skipped_existing,
            "updated": self.updated,
            "failed": self.failed,
        }


class IngestError(RuntimeError):
    """入库失败：携带结构化 issues 与统计。"""

    def __init__(self, source_file: str, issues: list[IngestIssue], stats: IngestStats) -> None:
        self.source_file = source_file
        self.issues = issues
        self.stats = stats
        super().__init__(
            json.dumps(
                {
                    "source_file": source_file,
                    "issues": [issue.as_dict() for issue in issues],
                    "stats": stats.as_dict(),
                },
                ensure_ascii=False,
            )
        )


def load_raw_clauses_from_jsonl(path: str | Path) -> list[RawClause]:
    clauses = load_clauses_jsonl(path)
    return [
        RawClause(
            spec_id=c.spec_id,
            chapter=c.chapter,
            section=c.section,
            article=c.article,
            title=c.title,
            text=c.text,
            source_path=str(path),
            page=None,
        )
        for c in clauses
    ]


def chunks_to_trivium_payloads(chunks: list[Chunk]) -> list[dict]:
    """仅子块进库；空 text 子块跳过，不伪造正文。"""
    from water_agent.domain.models import CHILD_TYPES

    child_types = set(CHILD_TYPES) | {"paragraph", "text_window"}
    payloads: list[dict] = []
    for ch in chunks:
        if ch.chunk_type not in child_types:
            continue
        if not ch.text.strip():
            continue
        payloads.append(ch.model_dump(mode="json"))
    return payloads


def build_parent_store(chunks: list[Chunk]) -> dict[str, Chunk]:
    from water_agent.domain.models import PARENT_TYPES

    return {c.chunk_id: c for c in chunks if c.chunk_type in PARENT_TYPES}


def _parents_path(db_path: str | Path) -> Path:
    db = Path(db_path)
    return db.with_suffix(db.suffix + ".parents.json")


def _manifest_path(db_path: str | Path) -> Path:
    db = Path(db_path)
    return db.with_suffix(db.suffix + ".manifest.json")


def save_parent_store(parent_store: dict[str, Chunk], db_path: str | Path) -> Path:
    target = _parents_path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {chunk_id: chunk.model_dump(mode="json") for chunk_id, chunk in parent_store.items()}
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def load_parent_store(db_path: str | Path) -> dict[str, Chunk]:
    target = _parents_path(db_path)
    if not target.exists():
        return {}
    payload = json.loads(target.read_text(encoding="utf-8"))
    return {chunk_id: Chunk.model_validate(data) for chunk_id, data in payload.items()}


def save_manifest(db_path: str | Path, chunk_ids: set[int]) -> Path:
    target = _manifest_path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"chunk_ids": sorted(chunk_ids)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def load_manifest(db_path: str | Path) -> set[int]:
    target = _manifest_path(db_path)
    if not target.exists():
        return set()
    payload = json.loads(target.read_text(encoding="utf-8"))
    return {int(x) for x in payload.get("chunk_ids", [])}


def ingest_jsonl_to_chunks(
    jsonl_path: str | Path,
    *,
    config: ChunkConfig | None = None,
) -> tuple[list[Chunk], dict[str, Chunk]]:
    raw = load_raw_clauses_from_jsonl(jsonl_path)
    chunks = Chunker(config).chunk_clauses(raw)
    return chunks, build_parent_store(chunks)


def ingest_to_trivium(
    jsonl_path: str | Path,
    db_path: str | Path,
    *,
    config: ChunkConfig | None = None,
    embedding_fn: Callable[[str], list[float]] | None = None,
    embedding_provider: object | None = None,
    curated: bool = False,
    dry_run: bool = False,
) -> dict:
    """将 JSONL 切块后写入 TriviumDB（子块 only），返回统计。

    任何 insert/upsert/index_text/flush 失败都会以 :class:`IngestError` 抛出，
    错误信息包含 source_file / chunk_id / operation / 原始异常；
    失败条目不计入 inserted/updated。
    """
    from water_agent.retrieval.trivium import mock_embed

    if curated and embedding_provider is None and embedding_fn is None:
        raise ValueError("curated ingest requires an explicit embedding provider")
    if embedding_provider is not None:
        embed = embedding_provider.embed
    else:
        embed = embedding_fn or mock_embed
    chunks, parents = ingest_jsonl_to_chunks(jsonl_path, config=config)
    provider_config = getattr(embedding_provider, "config", None)
    embedding_metadata = {
        "model": getattr(provider_config, "model", "mock_embed"),
        "dimensions": getattr(provider_config, "dimensions", None),
        "index_version": getattr(provider_config, "index_version", "fixture-mock-v1"),
        "fallback_enabled": getattr(provider_config, "fallback_enabled", True),
    }
    if dry_run:
        return {
            **IngestStats(total_chunks=len(chunks), parent_chunks=len(parents), child_payloads=len(chunks_to_trivium_payloads(chunks))).as_dict(),
            "embedding": embedding_metadata,
            "db_path": str(db_path),
            "dry_run": True,
        }
    payloads = chunks_to_trivium_payloads(chunks)
    skipped_empty = sum(1 for ch in chunks if ch.chunk_type in {"paragraph", "text_window", "article", "clause", "item", "table"} and not ch.text.strip())
    source_file = str(jsonl_path)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    stats = IngestStats(
        total_chunks=len(chunks),
        parent_chunks=len(parents),
        child_payloads=len(payloads),
    )
    issues: list[IngestIssue] = []

    import triviumdb

    dim = int(getattr(provider_config, "dimensions", None) or 1536)
    db = triviumdb.TriviumDB(str(db_path), dim=dim)
    manifest_ids = load_manifest(db_path)
    try:
        try:
            existing_ids = {int(n) for n in db.all_node_ids()}
        except Exception as exc:
            raise IngestError(
                source_file,
                [IngestIssue(source_file, "*", "list_existing", str(exc))],
                stats,
            ) from exc
        # manifest 作为额外幂等层：
        # - manifest 中的 node_id → 说明是此前成功入库过的同批数据，记为 skipped_existing
        # - 仅存在于 DB、但不在 manifest 中 → 说明是外部预置或首次人工插入，记为 updated
        for payload in payloads:
            chunk_id = str(payload["chunk_id"])
            operation = "insert"
            try:
                node_id = int(chunk_id, 16)
            except ValueError as exc:
                issues.append(IngestIssue(source_file, chunk_id, "id-parse", str(exc)))
                stats.failed += 1
                continue
            text = payload.get("text", "")
            was_existing_manifest = node_id in manifest_ids
            was_existing_db = node_id in existing_ids
            if was_existing_manifest:
                stats.skipped_existing += 1
                continue
            try:
                vector = embed(text)
            except Exception as exc:  # noqa: BLE001 — 嵌入失败记录为 IngestIssue 后继续
                issues.append(IngestIssue(source_file, chunk_id, "embed", str(exc)))
                stats.failed += 1
                continue
            try:
                if was_existing_manifest or was_existing_db:
                    operation = "upsert"
                    db.upsert_with_id(node_id, vector, payload=payload)
                else:
                    db.insert_with_id(node_id, vector, payload=payload)
                    existing_ids.add(node_id)
            except Exception as exc:  # noqa: BLE001 — 写入失败记录为 IngestIssue 后继续
                issues.append(IngestIssue(source_file, chunk_id, operation, str(exc)))
                stats.failed += 1
                continue
            try:
                db.index_text(node_id, text)
            except Exception as exc:  # noqa: BLE001 — 索引失败记录为 IngestIssue 后继续
                issues.append(IngestIssue(source_file, chunk_id, "index_text", str(exc)))
                stats.failed += 1
                continue
            if was_existing_manifest:
                stats.skipped_existing += 1
            elif was_existing_db:
                stats.updated += 1
            else:
                stats.inserted += 1
        try:
            db.flush()
        except Exception as exc:  # noqa: BLE001 — flush 失败记录为 IngestIssue
            issues.append(IngestIssue(source_file, "*", "flush", str(exc)))
            stats.failed += 1
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001, S110 — 关闭失败不影响已入库数据
            pass

    parents_path = save_parent_store(parents, db_path)
    save_manifest(db_path, existing_ids)

    if issues:
        raise IngestError(source_file, issues, stats)
    return {
        **stats.as_dict(),
        "skipped_empty_text": skipped_empty,
        "db_path": str(db_path),
        "parents_path": str(parents_path),
        "manifest_path": str(_manifest_path(db_path)),
        "embedding": embedding_metadata,
    }
