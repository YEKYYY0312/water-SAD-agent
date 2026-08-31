"""从受控语料构建 TriviumDB 索引（支持 256/512 双版本隔离）。

只读取 data/curated/clauses/ 中已通过审计的三规范条款，
禁止本地 embedding 兜底，维度与 index_version 强绑定。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from water_agent.embedding import build_embedding_provider
from water_agent.ingestion.pipeline import ingest_to_trivium


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Build TriviumDB index from curated corpus")
    p.add_argument("--curated-root", default="data/curated", help="受控语料根目录")
    p.add_argument("--db-path", required=True, help="输出的 TriviumDB 路径，如 data/indexes/qwen3.7_512.db")
    p.add_argument("--dimensions", type=int, default=512, choices=[256, 512, 768, 1024])
    p.add_argument("--dry-run", action="store_true", help="只切块统计，不写库不调云端")
    args = p.parse_args(argv)

    curated = Path(args.curated_root)
    clause_dir = curated / "clauses"
    if not clause_dir.exists():
        print(json.dumps({"ok": False, "error": f"clause_dir not found: {clause_dir}"}, ensure_ascii=False))
        return 2

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

    # 干跑时用一个假 transport，避免需要真实 Key 也能验证链路
    if args.dry_run:
        provider = provider  # dry_run 下 ingest_to_trivium 会提前返回，不会调 embed

    files = sorted(clause_dir.glob("*.jsonl"))
    if not files:
        print(json.dumps({"ok": False, "error": "no curated clause files"}, ensure_ascii=False))
        return 2

    total = {"total_chunks": 0, "parent_chunks": 0, "child_payloads": 0, "inserted": 0, "skipped_existing": 0, "updated": 0, "failed": 0}
    per_file = []
    errors = []
    for f in files:
        try:
            stats = ingest_to_trivium(f, args.db_path, embedding_provider=provider, curated=True, dry_run=args.dry_run)
        except IngestError as exc:
            # 单文件部分失败不中断整体：记录错误，继续建后续文件，
            # 脚本结尾统一报告，失败项可借助 manifest 幂等在重跑时补齐。
            errors.append({"file": str(f), "error": exc.issues if hasattr(exc, "issues") else str(exc)})
            stats = getattr(exc, "stats", {}) or {}
        per_file.append({"file": str(f), "stats": stats})
        for k in total:
            total[k] += stats.get(k, 0) if k in stats else 0

    ok = not errors and total["failed"] == 0 and not args.dry_run
    out = {
        "ok": ok,
        "dry_run": args.dry_run,
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
