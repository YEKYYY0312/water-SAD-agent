"""P1 增量 3B：根据语义审计构建受控正式语料集。"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from water_agent.ingestion.data_root import resolve_data_root

INCLUDED_SPECS = frozenset({"GB50013-2018", "GB50014-2021", "GB50318-2017", "GB50015-2019"})
EXCLUDED_SPECS = frozenset({"GB18918-2002", "GB5749-2022", "GBT50104-2010"})
QUARANTINE_LABELS = frozenset({"ocr_suspect", "invalid_article_format", "empty_or_low_signal"})


@dataclass(frozen=True, slots=True)
class CuratedCorpusResult:
    output_dir: Path
    manifest: dict[str, Any]
    build_report: dict[str, Any]


def _utc_stamp() -> str:
    return datetime.now(UTC).isoformat()


def _record_hash(record: dict[str, Any]) -> str:
    canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fragment_id(record: dict[str, Any], source_file: str, source_line: int, source_record_hash: str) -> str:
    raw = "|".join([str(record.get("spec_id", "")), str(record.get("article", "")), source_file, str(source_line), source_record_hash])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _load_audit(path: str | Path) -> dict[tuple[str, int], dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    rows.extend(payload.get("records", []))
    for group in payload.get("duplicate_groups", []):
        rows.extend(group.get("records", []))
    index: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        source = str(row.get("source_file", ""))
        line = int(row.get("line_number", row.get("source_line", 0)))
        if source and line:
            index[(source, line)] = row
            index[(str(Path(source).resolve()), line)] = row
    return index


def _find_audit_row(audit: dict[tuple[str, int], dict[str, Any]], source: Path, line: int) -> dict[str, Any]:
    direct = audit.get((str(source), line))
    if direct is not None:
        return direct
    resolved = audit.get((str(source.resolve()), line))
    if resolved is not None:
        return resolved
    filename_matches = [row for (path, row_line), row in audit.items() if row_line == line and Path(path).name == source.name]
    return filename_matches[0] if len(filename_matches) == 1 else {}


def _labels(audit_row: dict[str, Any]) -> list[str]:
    return [str(label) for label in audit_row.get("categories", [])]


def _decision(record: dict[str, Any], audit_row: dict[str, Any]) -> tuple[str, str]:
    spec_id = str(record.get("spec_id", ""))
    labels = set(_labels(audit_row))
    article = str(record.get("article", "")).strip()
    if spec_id not in INCLUDED_SPECS:
        return "quarantine", f"excluded_spec:{spec_id}"
    if not article:
        return "quarantine", "missing_article"
    if labels & QUARANTINE_LABELS:
        return "quarantine", ";".join(sorted(labels & QUARANTINE_LABELS))
    if "table_fragment" in labels:
        return "include", "confirmed_table_fragment_with_valid_article"
    if "normal_article" in labels:
        return "include", "normal_article"
    return "quarantine", "unclassified_audit_record"


def _quarantine_record(record: dict[str, Any], source_file: str, source_line: int, audit_row: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "source_file": source_file,
        "source_line": source_line,
        "spec_id": record.get("spec_id", ""),
        "article": record.get("article", ""),
        "title": record.get("title", ""),
        "text_preview": " ".join(str(record.get("text", "")).split())[:180],
        "audit_labels": _labels(audit_row),
        "quarantine_reason": reason,
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    path.write_text(content, encoding="utf-8")


def build_curated_corpus(data_root: str | Path, audit_report: str | Path, output_dir: str | Path) -> CuratedCorpusResult:
    resolved = resolve_data_root(data_root)
    if resolved.status != "OK":
        raise ValueError(f"DATA_ROOT is not ready: {resolved.status} ({resolved.message})")
    audit = _load_audit(audit_report)
    source_root = Path(data_root).resolve()
    if not audit:
        raise ValueError("audit report contains no record-level audit rows")
    if not any(key[0] for key in audit):
        raise ValueError("audit report contains no source-linked record rows")
    output = Path(output_dir)
    clauses_output = output / "clauses"
    clauses_output.mkdir(parents=True, exist_ok=True)
    included: dict[str, list[dict[str, Any]]] = defaultdict(list)
    quarantined: list[dict[str, Any]] = []
    source_files: list[str] = []
    included_by_label: Counter[str] = Counter()
    quarantined_by_reason: Counter[str] = Counter()
    duplicate_groups: Counter[tuple[str, str]] = Counter()
    scanned = 0

    for source in resolved.clause_files:
        source_str = str(source)
        source_files.append(source_str)
        with source.open(encoding="utf-8") as handle:
            for source_line, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                scanned += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    quarantined.append(_quarantine_record({"text": line}, source_str, source_line, {}, "invalid_json"))
                    quarantined_by_reason["invalid_json"] += 1
                    continue
                if not isinstance(record, dict):
                    quarantined.append(_quarantine_record({"text": str(record)}, source_str, source_line, {}, "record_not_object"))
                    quarantined_by_reason["record_not_object"] += 1
                    continue
                audit_row = _find_audit_row(audit, source, source_line)
                if not audit_row:
                    audit_row = _find_audit_row(audit, source_root / "clauses" / source.name, source_line)
                if not audit_row:
                    audit_row = _find_audit_row(audit, Path(source_str), source_line)
                if not audit_row:
                    audit_row = _find_audit_row(audit, Path(audit_report).parent / "clauses" / source.name, source_line)
                decision, reason = _decision(record, audit_row)
                source_hash = _record_hash(record)
                if decision == "include":
                    enriched = dict(record)
                    enriched.update({
                        "fragment_id": _fragment_id(record, source_str, source_line, source_hash),
                        "source_file": source_str,
                        "source_line": source_line,
                        "source_record_hash": source_hash,
                        "ingestion_decision": "include",
                        "decision_reason": reason,
                        "audit_labels": _labels(audit_row),
                    })
                    included[str(record["spec_id"])].append(enriched)
                    primary = audit_row.get("primary_category", "normal_article")
                    included_by_label[str(primary)] += 1
                    if "duplicate_article_variant" in _labels(audit_row):
                        duplicate_groups[(str(record["spec_id"]), str(record["article"]))] += 1
                else:
                    quarantined.append(_quarantine_record(record, source_str, source_line, audit_row, reason))
                    quarantined_by_reason[reason] += 1

    for spec_id, rows in sorted(included.items()):
        _write_jsonl(clauses_output / f"{spec_id}.jsonl", rows)
    quarantine_path = output / "quarantine.jsonl"
    _write_jsonl(quarantine_path, quarantined)

    included_count = sum(len(rows) for rows in included.values())
    manifest = {
        "created_at": _utc_stamp(),
        "data_root": str(data_root),
        "audit_report_path": str(audit_report),
        "source_files": source_files,
        "included_specs": sorted(included),
        "excluded_specs": sorted(EXCLUDED_SPECS),
        "records_scanned": scanned,
        "records_included": included_count,
        "records_quarantined": len(quarantined),
        "included_by_label": dict(sorted(included_by_label.items())),
        "quarantined_by_reason": dict(sorted(quarantined_by_reason.items())),
        "duplicate_article_groups_preserved": sum(1 for count in duplicate_groups.values() if count > 1),
        "content_hash": "",
    }
    manifest_payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest["content_hash"] = hashlib.sha256(manifest_payload.encode("utf-8")).hexdigest()
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    build_report = {
        "status": "ok",
        "manifest_path": str(output / "manifest.json"),
        "quarantine_path": str(quarantine_path),
        "included_clause_paths": [str(clauses_output / f"{spec}.jsonl") for spec in sorted(included)],
        "policy": {
            "included_specs": sorted(INCLUDED_SPECS),
            "excluded_specs": sorted(EXCLUDED_SPECS),
            "quarantine_labels": sorted(QUARANTINE_LABELS),
        },
    }
    (output / "build_report.json").write_text(json.dumps(build_report, ensure_ascii=False, indent=2), encoding="utf-8")
    return CuratedCorpusResult(output, manifest, build_report)
