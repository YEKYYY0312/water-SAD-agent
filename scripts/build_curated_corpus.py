"""根据完整语义审计报告构建受控正式语料候选集。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from water_agent.ingestion.curated_corpus import build_curated_corpus
from water_agent.ingestion.data_root import resolve_data_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a curated clause corpus from an audit report")
    parser.add_argument("--data-root", default=None, help="DATA_ROOT path; defaults to DATA_ROOT environment variable")
    parser.add_argument("--audit-report", required=True, help="complete semantic audit JSON report")
    parser.add_argument("--output-dir", default="data/curated", help="curated corpus output directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    resolved = resolve_data_root(args.data_root)
    if resolved.status != "OK":
        print(json.dumps({"ok": False, "status": resolved.status, "message": resolved.message}, ensure_ascii=False, indent=2))
        return 2
    try:
        result = build_curated_corpus(resolved.data_root or "", args.audit_report, args.output_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "status": "build_failed", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({
        "ok": True,
        "status": "ok",
        "output_dir": str(result.output_dir),
        "manifest_path": result.build_report["manifest_path"],
        "quarantine_path": result.build_report["quarantine_path"],
        "included_clause_paths": result.build_report["included_clause_paths"],
        "summary": {
            "records_scanned": result.manifest["records_scanned"],
            "records_included": result.manifest["records_included"],
            "records_quarantined": result.manifest["records_quarantined"],
            "included_specs": result.manifest["included_specs"],
            "excluded_specs": result.manifest["excluded_specs"],
            "duplicate_article_groups_preserved": result.manifest["duplicate_article_groups_preserved"],
            "included_by_label": result.manifest["included_by_label"],
            "quarantined_by_reason": result.manifest["quarantined_by_reason"],
        },
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
