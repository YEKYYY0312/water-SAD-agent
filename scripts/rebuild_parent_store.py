"""重建已有索引的父块侧文件 ``<db>.parents.json``，不重新嵌入、不需要任何 Key。

背景（review 09-28 #5）：``ingest_to_trivium`` 曾按文件覆盖写父块，逐文件入库后
只剩最后一个文件的父块。已发布的 512 维索引实测只剩 GB50318 的 24 个父块，
而索引子块引用了 126 个不同的 parent_id——另外三本规范命中时父块上下文回填静默失效。

父块是**纯切块产物**（``Chunker`` 确定性输出，``chunk_id`` 由 spec_id/章节/条款/
内容哈希决定），与向量无关，所以可以只重跑切块来重建，不必再调一次百炼 embedding。

安全措施：
- 先校验：索引里每个子块引用的 parent_id 都必须能在重建结果里找到，否则拒绝写入
  （说明语料与索引已不一致，应当完整重建索引而不是修补）；
- 写入前把旧文件备份为 ``.parents.json.bak``；
- ``--dry-run`` 只报告不写。

用法::

    _venv/bin/python scripts/rebuild_parent_store.py data/indexes/qwen3.7__512__curated.db
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from water_agent.ingestion.pipeline import (
    ingest_jsonl_to_chunks,
    load_parent_store,
    save_parent_store,
)


def _referenced_parent_ids(db_path: Path, dim: int) -> set[str]:
    import triviumdb

    db = triviumdb.TriviumDB(str(db_path), dim=dim, access_mode="read_only", load_text_index=True)
    try:
        referenced: set[str] = set()
        for node_id in db.all_node_ids():
            parent_id = (db.get_payload(int(node_id)) or {}).get("parent_id")
            if parent_id:
                referenced.add(str(parent_id))
        return referenced
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db_path", type=Path)
    parser.add_argument("--clause-dir", type=Path, default=REPO_ROOT / "data" / "curated" / "clauses")
    parser.add_argument("--dim", type=int, default=512, help="索引向量维度（只读打开用，与内容无关）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not args.db_path.exists():
        print(json.dumps({"ok": False, "error": f"索引不存在：{args.db_path}"}, ensure_ascii=False))
        return 2
    files = sorted(args.clause_dir.glob("*.jsonl"))
    if not files:
        print(json.dumps({"ok": False, "error": f"{args.clause_dir} 下没有 *.jsonl"}, ensure_ascii=False))
        return 2

    rebuilt = {}
    for path in files:
        _, parents = ingest_jsonl_to_chunks(path)
        rebuilt.update(parents)

    referenced = _referenced_parent_ids(args.db_path, args.dim)
    current = load_parent_store(args.db_path)
    missing = sorted(referenced - set(rebuilt))
    report = {
        "db_path": str(args.db_path),
        "clause_files": [p.name for p in files],
        "referenced_parent_ids": len(referenced),
        "current_parents": len(current),
        "current_missing_referenced": len(referenced - set(current)),
        "rebuilt_parents": len(rebuilt),
        "rebuilt_missing_referenced": len(missing),
        "dry_run": args.dry_run,
    }
    if missing:
        report.update(ok=False, error="重建结果缺少索引引用的父块：语料与索引不一致，请用 build_index.py 完整重建")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    if not args.dry_run:
        target = Path(str(args.db_path) + ".parents.json")
        if target.exists():
            shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))
        save_parent_store(rebuilt, args.db_path)
    report["ok"] = True
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
