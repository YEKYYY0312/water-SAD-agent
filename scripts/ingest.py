"""P1 ingestion 脚本：JSONL → 父子级切块 → TriviumDB（子块 only）。

用量
----
_venv/bin/python scripts/ingest.py knowledge/clauses/GB50014-2021.jsonl --db-path data/p1.db
（受控语料建索引请用 scripts/build_index.py，它走真实 embedding 且维度与 index_version 强绑定）
"""
from __future__ import annotations

import argparse
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Ingest JSONL to TriviumDB with parent-child chunking")
    p.add_argument("inputs", nargs="+", help="JSONL path(s) or glob, e.g. knowledge/clauses/*.jsonl")
    p.add_argument("--db-path", default=str(REPO_ROOT / "data" / "p1.db"), help="TriviumDB path")
    p.add_argument("--overwrite", action="store_true", help="Remove existing db before ingest")
    return p


def resolve_inputs(patterns: list[str]) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for pat in patterns:
        candidates = sorted(pat for pat in REPO_ROOT.glob(pat)) if ("*" in pat or "?" in pat) else [REPO_ROOT / pat]
        for c in candidates:
            if c.exists() and c.is_file():
                out.append(c)
    return sorted(set(out))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    sources = resolve_inputs(args.inputs)
    if not sources:
        print(f"no JSONL matched: {args.inputs}", file=sys.stderr)
        return 2
    db_path = pathlib.Path(args.db_path)
    if args.overwrite:
        # 必须连同伴生文件一起删（.vec/.wal/.lock/.parents.json/.manifest.json 等）：
        # 只删 .db 会留下旧 manifest，下次入库全部被判 skipped_existing，建出来的是空库。
        import shutil

        for sibling in sorted(db_path.parent.glob(db_path.name + "*")):
            if sibling.is_dir():
                shutil.rmtree(sibling, ignore_errors=True)
            else:
                sibling.unlink(missing_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    from water_agent.ingestion.pipeline import ingest_to_trivium

    total = {"total_chunks": 0, "parent_chunks": 0, "child_payloads": 0, "inserted": 0}
    for src in sources:
        stats = ingest_to_trivium(src, db_path)
        print(f"{src}: {stats}")
        for k in total:
            total[k] += stats.get(k, 0)
    total["db_path"] = str(db_path)
    total["sources"] = [str(s) for s in sources]
    print(f"done: {total}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
