"""从受控正式语料生成候选检索问题集。

只为人工审核做准备，不会自动把候选问题升级为正式 gold。
候选记录必须带 ``is_candidate=true``、``status="candidate"``，且不写
``gold_articles_any``，只写 ``candidate_gold_articles``。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from water_agent.domain.models import utc_now


def _candidate_id(record: dict[str, Any], index: int) -> str:
    raw = "|".join([
        str(record.get("spec_id", "")),
        str(record.get("article", "")),
        str(record.get("fragment_id", "")),
    ])
    return "cand-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]


def _candidate_query(record: dict[str, Any]) -> str:
    text = str(record.get("text", "")).strip()
    # 优先取正文里第一个像用户问题的句子：去掉纯列条号/序号行、表格分隔行。
    if text:
        for line in text.splitlines():
            cleaned = " ".join(line.split()).strip("|。；； ")  # noqa: B005 有意的分隔符集合裁剪
            if (
                len(cleaned) >= 12
                and not cleaned.startswith("|")
                and not cleaned.startswith(("1 ", "2 ", "3 ", "4 ", "5 ", "6 ", "7 ", "8 ", "9 ")) and not any(ch.isdigit() for ch in cleaned[:3])
                and "应对" not in cleaned[:0]
            ):
                return cleaned
    title = " ".join(str(record.get("title", "")).strip().split())
    if title and len(title) >= 6 and not title.startswith(("、", "|")):
        return title
    return f"请提供{record.get('spec_id','')} {record.get('article','')}的条款内容。"


def read_curated_records(curated_root: str | Path) -> list[dict[str, Any]]:
    root = Path(curated_root)
    rows: list[dict[str, Any]] = []
    for path in sorted((root / "clauses").glob("*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            rows.extend(_iter_jsonl(handle))
    return rows


def _iter_jsonl(handle) -> Iterable[dict[str, Any]]:
    # fallback in case of empty file
    for line in handle:
        if line.strip():
            yield json.loads(line)


def select_candidates(records: list[dict[str, Any]], *, count: int = 70) -> list[dict[str, Any]]:
    # 均匀覆盖三个规范：先按 spec 分桶，再从每个桶按步长采样不同 article。
    by_spec: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        spec = str(record.get("spec_id", ""))
        by_spec.setdefault(spec, []).append(record)

    picked: list[dict[str, Any]] = []
    # 每轮从每个 spec 取 1 条，进行 count 轮，覆盖三规范且不超总候选数。
    spec_names = sorted(by_spec)
    per_spec_seen: dict[str, set[str]] = {spec: set() for spec in spec_names}
    for round_index in range(count):
        filled = True
        for spec in spec_names:
            candidates_for_spec = by_spec[spec]
            chosen = None
            for record in candidates_for_spec:
                key = str(record.get("article", ""))
                if key not in per_spec_seen[spec]:
                    chosen = record
                    break
            if chosen is not None:
                per_spec_seen[spec].add(str(chosen.get("article", "")))
                picked.append(chosen)
                filled = False
        if filled:
            break
    return [
        {
            "id": _candidate_id(record, i),
            "status": "candidate",
            "is_candidate": True,
            "spec_id": record.get("spec_id", ""),
            "article": record.get("article", ""),
            "fragment_id": record.get("fragment_id", ""),
            "title": record.get("title", ""),
            "query": _candidate_query(record),
            "candidate_gold_articles": [f"{record.get('spec_id')} {record.get('article')}"],
            "audit_labels": record.get("audit_labels", []),
            "source_file": record.get("source_file", ""),
            "source_line": record.get("source_line"),
            "notes": "自动生成候选，待人工审核后固化。",
        }
        for i, record in enumerate(picked)
    ][:count]


def build_candidate_manifest(candidates: list[dict[str, Any]], *, curated_root: str | Path, output_path: str | Path) -> dict[str, Any]:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": utc_now().isoformat(),
        "type": "retrieval_candidates",
        "curated_root": str(curated_root),
        "total_candidates": len(candidates),
        "candidates": candidates,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload