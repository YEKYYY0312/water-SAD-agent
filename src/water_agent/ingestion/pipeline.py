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

索引元数据（P7-S10）
--------------------
建库时把 embedding 模型、embedding 端点主机名、维度、``index_version`` 与可用的检索模式写进
`<db_path>.index_meta.json`（:func:`write_index_meta`，由 ``scripts/build_index.py`` 调用）。检索侧据此判定
当前 embedding 配置能不能对这份索引做向量检索（``retrieval/trivium.py``）；没有这个文件的旧索引按百炼缺省
模型对待。
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
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


def _text_sidecars(db_path: str | Path) -> tuple[Path, Path]:
    """持久化文本索引的一对侧文件：``.text`` 与 ``.text.meta``（缺一不可用）。"""
    base = str(db_path)
    return Path(base + ".text"), Path(base + ".text.meta")


def _has_complete_text_sidecars(db_path: str | Path) -> bool:
    """两个侧文件都在且 ``.text`` 非空。缺 ``.text.meta`` 时 ``load_text_index`` 用不了。"""
    text, meta = _text_sidecars(db_path)
    try:
        return text.is_file() and text.stat().st_size > 0 and meta.is_file()
    except OSError:
        return False


def _snapshot_text_sidecars(db_path: str | Path) -> bool:
    """入库前把已有文本索引侧文件复制成 ``.bak``，返回是否存在完整的一对。

    以 read_write 打开的 TriviumDB 在 ``close()`` 时会按**进程内**文本索引重写侧文件，
    进程内为空就把两个侧文件一起删掉（实测）。也就是说只要本次入库一条文本都没灌进
    索引（空语料、全部失败、或全被 manifest 跳过），关库就会毁掉上一次辛苦建好的索引。
    ``index_text`` 现在对 manifest 命中也照灌，正常路径已不会落空；这里是兜底，保证
    "一次没写入任何东西的入库" 绝不破坏既有索引。
    """
    if not _has_complete_text_sidecars(db_path):
        return False
    import shutil

    text, meta = _text_sidecars(db_path)

    shutil.copy2(text, text.with_suffix(text.suffix + ".bak"))
    shutil.copy2(meta, meta.with_suffix(meta.suffix + ".bak"))
    return True


def _settle_text_sidecars(db_path: str | Path, *, restore: bool) -> None:
    """按需还原 ``.bak`` 快照，随后无论如何都清掉快照文件。"""
    for path in _text_sidecars(db_path):
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.is_file():
            continue
        if restore:
            backup.replace(path)
        else:
            backup.unlink()


def _manifest_path(db_path: str | Path) -> Path:
    db = Path(db_path)
    return db.with_suffix(db.suffix + ".manifest.json")


def write_index_meta(
    db_path: str | Path,
    *,
    embedding_model: str | None,
    embedding_base_url: str | None,
    dimensions: int,
    index_version: str,
    retrieval_modes: Sequence[str],
) -> Path:
    """写 ``<db_path>.index_meta.json``（原子替换）。``embedding_model=None`` 表示纯关键词索引（没有向量）。

    只记主机名，不记完整 base_url：路径与查询串里可能有运维不想扩散的东西，判定也只用模型与维度。
    """

    from water_agent.generation.endpoints import endpoint_host
    from water_agent.retrieval.trivium import INDEX_META_FORMAT, index_meta_path

    target = index_meta_path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": INDEX_META_FORMAT,
        "embedding_model": embedding_model,
        "embedding_host": endpoint_host(embedding_base_url) if embedding_base_url else None,
        "dimensions": dimensions,
        "index_version": index_version,
        "retrieval_modes": list(retrieval_modes),
    }
    temporary = target.with_name(f"{target.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


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
        embed = embedding_provider.embed  # type: ignore[attr-defined]
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
    # 本次进程内成功灌进文本索引的节点数，决定能否安全 build+flush（见下方说明）。
    indexed_text_nodes = 0

    import triviumdb

    dim = int(getattr(provider_config, "dimensions", None) or 1536)
    had_text_sidecars = _snapshot_text_sidecars(db_path)
    # 必须把已有文本索引载进来：文本索引是**按进程**的，而入库是**按文件**的
    # （scripts/build_index.py 对 data/curated/clauses/*.jsonl 逐个调用本函数）。
    # 不加载就 build_text_index()，落盘的只有本文件这一批节点——实测 3313 个节点的
    # 3.5MB 索引，重跑一遍后只剩末位文件 81 个节点的 80KB。侧文件仍在、仍非空，
    # 检索侧照样以只读打开并信任它，于是 97% 的语料静默失去文本检索能力，
    # hybrid 退化成纯向量而没有任何一处报错。
    db = triviumdb.TriviumDB(str(db_path), dim=dim, load_text_index=had_text_sidecars)
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
                # manifest 命中只跳过**重新嵌入**（云调用，贵），文本索引必须照灌：
                # 它只存在于进程内，本次进程还没建过。漏掉这一步的后果不是"少建一个
                # 索引"，而是下面的 build_text_index() + flush() 会拿一个空索引覆盖
                # 已有的 .text 侧文件——实测直接删除。于是语料没变时重跑一次入库就把
                # 持久化文本索引清了：本机检索还能退回进程内重建所以看不出来，而容器
                # 以只读根文件系统运行时 read_write 打不开，会一路静默退到 fixture。
                try:
                    db.index_text(node_id, text)
                except Exception as exc:  # noqa: BLE001 — 与新增路径同语义：记录后继续
                    issues.append(IngestIssue(source_file, chunk_id, "index_text", str(exc)))
                    stats.failed += 1
                    continue
                indexed_text_nodes += 1
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
            indexed_text_nodes += 1
            # 只有写入 + 文本索引都成功才进 manifest；否则下次重跑会被当成"已入库"永不重试
            existing_ids.add(node_id)
            if was_existing_manifest:
                stats.skipped_existing += 1
            elif was_existing_db:
                stats.updated += 1
            else:
                stats.inserted += 1
        try:
            # build + flush 把文本索引落成 .text 侧文件，检索侧据此以只读方式打开库
            # （见 TriviumRetriever）。一条文本都没灌成功时跳过：空索引没有落盘价值，
            # 且下面的还原逻辑会把入库前的侧文件放回去。
            if indexed_text_nodes:
                db.build_text_index()
                db.flush()
        except Exception as exc:  # noqa: BLE001 — flush 失败记录为 IngestIssue
            issues.append(IngestIssue(source_file, "*", "flush", str(exc)))
            stats.failed += 1
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001, S110 — 关闭失败不影响已入库数据
            pass
        # 不变量：一次入库绝不能让持久化文本索引比入库前更糟。close() 未留下完整侧
        # 文件时（进程内索引为空、或 build/flush 失败）用入库前的快照还原。
        survived = _has_complete_text_sidecars(db_path)
        _settle_text_sidecars(db_path, restore=not survived)
        if not survived and payloads:
            # 有子块要入库却没落出文本索引：真实工作丢了，必须响。侧文件已还原，
            # 所以既有索引无损，但这次入库不能算成功。
            issues.append(
                IngestIssue(
                    source_file,
                    "*",
                    "text_index",
                    "本次入库未落出持久化文本索引"
                    + ("，已还原入库前的 .text 侧文件" if had_text_sidecars else ""),
                )
            )
            stats.failed += 1

    # 合并而不是覆盖：入库按文件逐个调用（scripts/build_index.py 对
    # data/curated/clauses/*.jsonl 各调一次），直接写本文件的父块会把此前文件的
    # 父块全部冲掉——与上面 .text 侧文件是同一类缺陷。实测已发布的 512 维索引
    # parents.json 只剩末位文件 GB50318 的 24 个父块（应为 147），检索命中另外三本
    # 规范时"父块上下文回填"静默失效（review 09-28 #5）。chunk_id 含 spec_id 与
    # 内容哈希，同一父块重入库得到同一个键，合并天然幂等。
    merged_parents = {**load_parent_store(db_path), **parents}
    parents_path = save_parent_store(merged_parents, db_path)
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
