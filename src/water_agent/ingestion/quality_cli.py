"""DATA_ROOT 验证 CLI：路径解析 → 质量扫描 → 可读 JSON 报告与退出码。

退出码
-------
0  数据有效，或 report-only 模式完成
1  strict 模式数据质量失败
2  配置、路径或命令参数错误

本次只做数据接入契约与质量门禁，不执行真实索引；验证成功后仅输出
可执行的下一步 ingest 命令（用户明确确认后另行运行）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from water_agent.domain.models import utc_now
from water_agent.ingestion.data_root import resolve_data_root
from water_agent.ingestion.quality import (
    QualityReport,
    report_has_strict_blockers,
    scan_clause_files,
)

EXIT_OK = 0
EXIT_STRICT_FAILED = 1
EXIT_CONFIG_ERROR = 2

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_REPORT_DIR = REPO_ROOT / "data" / "quality" / "reports"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Validate DATA_ROOT clauses JSONL and produce a quality report")
    p.add_argument("--data-root", default=None, help="explicit DATA_ROOT path (overrides env)")
    p.add_argument("--strict", action="store_true", help="fail (exit 1) on invalid JSON / missing required / duplicate logic clauses")
    p.add_argument("--output", default=None, help="explicit report JSON path (default: data/quality/reports/<utc_timestamp>.json)")
    return p


def _write_report(report: QualityReport, output: str | None) -> Path:
    if output:
        target = Path(output)
    else:
        DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
        target = DEFAULT_REPORT_DIR / f"{utc_now().strftime('%Y%m%dT%H%M%S%f')}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    resolved = resolve_data_root(args.data_root)
    if resolved.status != "OK":
        payload = {
            "ok": False,
            "status": resolved.status,
            "data_root": resolved.data_root,
            "message": resolved.message,
            "next_step": None,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_CONFIG_ERROR

    files = resolved.clause_files
    report = scan_clause_files(files)
    report.generated_at = utc_now().isoformat()
    report.data_root = resolved.data_root or ""
    report.mode = "strict" if args.strict else "report-only"

    report_path = _write_report(report, args.output)

    if args.strict and report_has_strict_blockers(report):
        payload = {
            "ok": False,
            "status": "strict_failed",
            "data_root": resolved.data_root,
            "report_path": str(report_path),
            "summary": report.summary,
            "next_step": None,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_STRICT_FAILED

    next_step = (
        "_venv/bin/python scripts/ingest.py "
        + " ".join(f'"{p}"' for p in files)
        + " --db-path data/real.db"
    )
    payload = {
        "ok": True,
        "status": "ok",
        "data_root": resolved.data_root,
        "report_path": str(report_path),
        "summary": report.summary,
        "next_step": next_step,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
