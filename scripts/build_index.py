"""从受控语料构建 TriviumDB 索引（支持 256/512 双版本隔离）。

只读取 data/curated/clauses/ 中已通过审计的规范条款，
禁止本地 embedding 兜底，维度与 index_version 强绑定。
``--dry-run`` 只切块统计，不写库、不调云端，也不要求 DASHSCOPE_API_KEY。

``--text-only``（P7-S10）：纯关键词建库——不调 embedding、不要求任何 Key，向量写零向量（维度用 ``--dimensions``，
缺省 512），``index_meta.json`` 写 ``retrieval_modes: ["text"]``。只配了一个对话模型 Key 的部署也能新增 / 更新规范库；
这种索引在 ``auto`` 下永远走关键词检索，强制 ``hybrid`` 即 ``RETRIEVAL_EMBEDDING_MISMATCH``。它不是"本地 embedding
兜底"（那个仍然禁止），是不做 embedding。

P7-S10：embedding 端点 / 模型按 ``resolve_embedding_endpoint`` 解析（缺省百炼 qwen3.7-text-embedding）；建库时
把模型、端点主机名、维度与 index_version 写进 ``<db>.index_meta.json``，检索侧据此拒绝"换了模型还用旧向量"。
往已有索引里追加时，元数据（没有元数据的旧索引按百炼缺省模型对待）与本次的模型 / 维度不一致即拒绝——
一份索引里混两种模型的向量就是损坏。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from water_agent.embedding import DEFAULT_MODEL, build_embedding_provider
from water_agent.ingestion.pipeline import (
    IngestError,
    TextOnlyEmbedding,
    ingest_to_trivium,
    write_index_meta,
    write_text_only_index_meta,
)
from water_agent.retrieval.trivium import (
    MODE_HYBRID,
    MODE_TEXT,
    IndexMetaError,
    embedding_mismatch,
    read_index_meta,
)


class _DryRunProvider:
    """dry-run 占位：只提供元数据，绝不 embed（调用即抛错，防止误用）。"""

    def __init__(self, dimensions: int) -> None:
        self.config = type(
            "Cfg",
            (),
            {
                "model": DEFAULT_MODEL,
                "dimensions": dimensions,
                "index_version": f"{DEFAULT_MODEL}__{dimensions}__curated-v1",
                "fallback_enabled": False,
            },
        )()

    def embed(self, text: str) -> list[float]:
        raise RuntimeError("dry-run provider must not embed")


def _stats_dict(stats: object) -> dict:
    if isinstance(stats, dict):
        return stats
    as_dict = getattr(stats, "as_dict", None)
    return as_dict() if callable(as_dict) else {}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build TriviumDB index from curated corpus")
    p.add_argument("--curated-root", default="data/curated", help="受控语料根目录")
    p.add_argument("--db-path", required=True, help="输出的 TriviumDB 路径，如 data/indexes/qwen3.7_512.db")
    # 维度由 embedding 模型决定：缺省 qwen3.7 只收 256/512/768/1024，其余模型由 BailianEmbeddingConfig 校验
    p.add_argument("--dimensions", type=int, default=512)
    p.add_argument("--dry-run", action="store_true", help="只切块统计，不写库不调云端（不需要 Key）")
    p.add_argument(
        "--text-only",
        action="store_true",
        help="纯关键词建库：不调 embedding、不要求任何 Key，向量写零向量，检索只能走关键词模式",
    )
    args = p.parse_args(argv)

    curated = Path(args.curated_root)
    clause_dir = curated / "clauses"
    if not clause_dir.exists():
        print(json.dumps({"ok": False, "error": f"clause_dir not found: {clause_dir}"}, ensure_ascii=False))
        return 2

    # dry-run 不构造真实 provider（build_embedding_provider 构造即校验 Key）
    if args.text_only:
        try:
            provider = TextOnlyEmbedding(args.dimensions)
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 2
    elif args.dry_run:
        provider = _DryRunProvider(args.dimensions)
    else:
        provider = build_embedding_provider(dimensions=args.dimensions)

    # 维度自检：库已存在且维度与请求不一致 → 报错退出，防止沿用旧维度导致写入全失败
    if not args.dry_run and Path(args.db_path).exists():
        try:
            import triviumdb

            probe = triviumdb.TriviumDB(str(args.db_path), dim=args.dimensions)
            existing_dim = int(probe.dim())
            probe.close()
            if existing_dim != args.dimensions:
                print(json.dumps({
                    "ok": False,
                    "error": (
                        f"DB 已存在且维度不匹配：库维度 {existing_dim}，请求 {args.dimensions}。"
                        f"请先删除 {args.db_path} 再重建。"
                    ),
                }, ensure_ascii=False))
                return 2
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"DB 维度自检失败: {exc}"}, ensure_ascii=False))
            return 2
        try:
            existing = read_index_meta(args.db_path, dimensions=args.dimensions)
        except IndexMetaError as exc:
            print(json.dumps({"ok": False, "error": f"已有索引的元数据不可用：{exc}"}, ensure_ascii=False))
            return 2
        if args.text_only:
            # 往有向量的索引里写零向量就毁了它；纯关键词索引之间可以追加（维度上面已核对）
            mismatch = (
                "已有索引带向量（用 embedding 建的库），不能写入纯关键词条目"
                if existing.has_vectors
                else None
            )
        else:
            mismatch = embedding_mismatch(
                existing, model=provider.config.model, dimensions=provider.config.dimensions
            )
        if mismatch is not None:
            print(json.dumps({
                "ok": False,
                "error": f"不能往已有索引里追加：{mismatch}。请先删除 {args.db_path} 及其侧文件再重建。",
            }, ensure_ascii=False))
            return 2

    files = sorted(clause_dir.glob("*.jsonl"))
    if not files:
        print(json.dumps({"ok": False, "error": "no curated clause files"}, ensure_ascii=False))
        return 2

    if args.text_only and not args.dry_run:
        write_text_only_index_meta(args.db_path, dimensions=provider.config.dimensions)
    elif not args.dry_run:
        # 先写元数据再入库：中途失败的库同样记着它的向量是哪个模型产的，不会被当成旧索引按缺省模型对待
        write_index_meta(
            args.db_path,
            embedding_model=provider.config.model,
            embedding_base_url=getattr(provider.config, "base_url", None),
            dimensions=provider.config.dimensions,
            index_version=provider.config.index_version,
            retrieval_modes=(MODE_HYBRID, MODE_TEXT),
        )

    total = {"total_chunks": 0, "parent_chunks": 0, "child_payloads": 0, "inserted": 0, "skipped_existing": 0, "updated": 0, "failed": 0}
    per_file = []
    errors = []
    for f in files:
        try:
            stats = ingest_to_trivium(f, args.db_path, embedding_provider=provider, curated=True, dry_run=args.dry_run)
        except IngestError as exc:
            # 单文件部分失败不中断整体：记录错误，继续建后续文件，
            # 脚本结尾统一报告，失败项可借助 manifest 幂等在重跑时补齐。
            errors.append({"file": str(f), "issues": [issue.as_dict() for issue in exc.issues]})
            stats = _stats_dict(exc.stats)
        per_file.append({"file": str(f), "stats": stats})
        for k in total:
            total[k] += int(stats.get(k, 0) or 0)

    ok = not errors and total["failed"] == 0 and not args.dry_run
    out = {
        "ok": ok,
        "dry_run": args.dry_run,
        "text_only": args.text_only,
        "retrieval_modes": [MODE_TEXT] if args.text_only else [MODE_HYBRID, MODE_TEXT],
        "curated_root": str(curated),
        "db_path": args.db_path,
        "dimensions": provider.config.dimensions,
        "model": provider.config.model,
        "index_version": provider.config.index_version,
        "files": [str(f) for f in files],
        "per_file": per_file,
        "total": total,
        "errors": errors,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if ok or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
