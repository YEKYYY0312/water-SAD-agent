"""P1 增量 3A 语义审计 CLI。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from water_agent.ingestion.data_root import resolve_data_root
from water_agent.ingestion.semantic_audit import SemanticAuditReport, audit_clause_files

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "quality" / "reports"


def _markdown(report: SemanticAuditReport) -> str:
    lines = [
        "# 条款语义审计报告",
        "",
        f"- 生成时间：`{report.generated_at}`",
        f"- DATA_ROOT：`{report.data_root}`",
        "",
        "## 摘要",
        "",
    ]
    for key, value in report.summary.items():
        lines.append(f"- **{key}**：`{json.dumps(value, ensure_ascii=False)}`")
    lines.extend(["", "## 各规范统计", "", "| spec_id | 总记录 | 正常 | 表格 | OCR 疑似 | 隔离候选 | 重复组 |", "|---|---:|---:|---:|---:|---:|---:|"])
    for spec_id, stats in report.per_spec_statistics.items():
        lines.append(
            f"| {spec_id} | {stats['records_total']} | {stats['normal_article']} | "
            f"{stats['table_fragment']} | {stats['ocr_suspect']} | {stats['quarantine_candidates']} | {stats['duplicate_groups']} |"
        )
    lines.extend(["", "## 正式入库策略", ""])
    for category, policy in report.recommended_ingestion_policy.items():
        lines.append(f"- `{category}`：**{policy['strategy']}** —— {policy['reason']}")
    lines.extend(["", "## 需要人工决策", ""])
    lines.extend(f"- {item}" for item in report.manual_decisions_required)
    lines.extend(["", "## 正常重复组样本", ""])
    for group in report.duplicate_group_samples.get("normal", []):
        lines.append(f"- `{group.spec_id} {group.article}`：{group.recommendation_reason}")
        for record in group.records:
            lines.append(f"  - {record.source_file}:{record.line_number} —— {record.text_preview}")
    lines.extend(["", "## 可疑重复组样本", ""])
    for group in report.duplicate_group_samples.get("suspicious", []):
        lines.append(f"- `{group.spec_id} {group.article}`：{group.recommendation_reason}")
        for record in group.records:
            lines.append(f"  - {record.source_file}:{record.line_number} —— {record.text_preview}")
    return "\n".join(lines) + "\n"


def _write_outputs(report: SemanticAuditReport, output_dir: str | Path) -> tuple[Path, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = report.generated_at.replace("+00:00", "").replace("-", "").replace(":", "").replace(".", "")
    json_path = directory / f"{stamp}_semantic_audit.json"
    markdown_path = directory / f"{stamp}_semantic_audit.md"
    json_path.write_text(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit duplicate article numbers and low-quality clause records")
    parser.add_argument("--data-root", default=None)
    parser.add_argument("--output-dir", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    resolved = resolve_data_root(args.data_root)
    if resolved.status != "OK":
        print(json.dumps({"ok": False, "status": resolved.status, "message": resolved.message, "report_path": None}, ensure_ascii=False, indent=2))
        return EXIT_CONFIG_ERROR
    report = audit_clause_files(resolved.clause_files, resolved.data_root or "")
    json_path, markdown_path = _write_outputs(report, args.output_dir or DEFAULT_OUTPUT_DIR)
    print(json.dumps({"ok": True, "status": "ok", "report_json": str(json_path), "report_markdown": str(markdown_path), "summary": report.summary}, ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
