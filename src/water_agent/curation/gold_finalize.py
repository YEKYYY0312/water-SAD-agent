"""人工审核 Markdown → 正式 Gold 固化。

规则：
- 只固化 approve / edit；reject 与未审核一律排除；
- edit 使用“修改后query / 修改后gold”，approve 保留原 query/gold；
- query 或 gold 为空、gold 不在受控语料、规范不在白名单 → 排除；
- 固化的正式 Gold 总数不足 50 → 抛 GoldFinalizeError，禁止自动补足。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ALLOWED_SPECS = frozenset({"GB50013-2018", "GB50014-2021", "GB50318-2017"})
MIN_GOLD = 50
REVIEW_ACTIONS = frozenset({"approve", "edit", "reject"})


class GoldFinalizeError(RuntimeError):
    """固化失败：不足 50 条等。"""


@dataclass(slots=True)
class ReviewDecision:
    candidate_id: str
    action: str
    edited_query: str = ""
    edited_gold: list[str] = field(default_factory=list)
    notes: str = ""


def _split_gold(value: str) -> list[str]:
    parts = re.split(r"[;；,，]", value)
    return [p.strip() for p in parts if p.strip()]


def parse_review_markdown(md_path: str | Path) -> dict[str, ReviewDecision]:
    """解析审核表：只保留 approve/edit/reject 三态，忽略未审核行。"""
    lines = Path(md_path).read_text(encoding="utf-8").splitlines()
    header = None
    reviews: dict[str, ReviewDecision] = {}
    for line in lines:
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and cells[0] == "id":
            header = cells
            continue
        if not header or len(cells) < 7:
            continue
        try:
            col = {name: i for i, name in enumerate(header)}
            cand_id = cells[col["id"]]
            action = cells[col["审核动作"]].strip().lower()
        except (KeyError, IndexError):
            continue
        if action not in REVIEW_ACTIONS:
            continue
        edited_query = cells[col["修改后query"]] if len(cells) > col.get("修改后query", 99) else ""
        edited_gold = cells[col["修改后gold"]] if len(cells) > col.get("修改后gold", 99) else ""
        notes = cells[col["备注"]] if len(cells) > col.get("备注", 99) else ""
        reviews[cand_id] = ReviewDecision(
            candidate_id=cand_id,
            action=action,
            edited_query=edited_query,
            edited_gold=_split_gold(edited_gold),
            notes=notes,
        )
    return reviews


def build_curated_article_index(curated_root: str | Path) -> set[tuple[str, str]]:
    """受控语料中允许的 (spec_id, article) 集合，仅白名单规范。"""
    base = Path(curated_root)
    index: set[tuple[str, str]] = set()
    if not (base / "clauses").exists():
        return index
    for path in sorted((base / "clauses").glob("*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                spec = str(record.get("spec_id", ""))
                article = str(record.get("article", ""))
                if spec in ALLOWED_SPECS and article:
                    index.add((spec, article))
    return index


def _load_candidates(path: str | Path) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                record = json.loads(line)
                candidates[str(record["id"])] = record
    return candidates


def _resolve_gold_articles(candidate: dict[str, Any], decision: ReviewDecision) -> list[str]:
    if decision.action == "edit" and decision.edited_gold:
        return decision.edited_gold
    return [str(g) for g in candidate.get("gold_articles_any", [])]


def _resolve_query(candidate: dict[str, Any], decision: ReviewDecision) -> str:
    if decision.action == "edit":
        return decision.edited_query.strip()
    return str(candidate.get("query", "")).strip()


def finalize_gold(
    *,
    candidates_path: str | Path,
    review_md_path: str | Path,
    curated_root: str | Path,
    gold_path: str | Path,
    manifest_path: str | Path,
    min_gold: int = MIN_GOLD,
) -> dict[str, Any]:
    reviews = parse_review_markdown(review_md_path)
    candidates = _load_candidates(candidates_path)
    curated_articles = build_curated_article_index(curated_root)

    approve_count = 0
    edit_count = 0
    rejected_count = 0
    skipped_blank_query = 0
    skipped_blank_gold = 0
    skipped_orphan = 0
    skipped_disallowed_spec = 0
    skipped_unknown_candidate = 0
    duplicate_query_ids: list[str] = []
    duplicate_id_ids: list[str] = []
    seen_query: dict[str, str] = {}
    seen_ids: set[str] = set()

    gold_rows: list[dict[str, Any]] = []
    for cand_id, decision in reviews.items():
        if decision.action == "reject":
            rejected_count += 1
            continue
        if cand_id in seen_ids:
            duplicate_id_ids.append(cand_id)
            continue
        seen_ids.add(cand_id)
        candidate = candidates.get(cand_id)
        if candidate is None:
            skipped_unknown_candidate += 1
            continue
        query = _resolve_query(candidate, decision)
        gold_articles = _resolve_gold_articles(candidate, decision)
        if not query:
            skipped_blank_query += 1
            continue
        if not gold_articles:
            skipped_blank_gold += 1
            continue
        if query in seen_query:
            duplicate_query_ids.append(cand_id)
            continue
        seen_query[query] = cand_id
        # 校验 gold：规范白名单 + 存在于受控语料
        valid_gold = []
        orphan = False
        disallowed = False
        for gold in gold_articles:
            parts = gold.split()
            if len(parts) < 2:
                orphan = True
                break
            spec, article = parts[0], parts[1]
            if spec not in ALLOWED_SPECS:
                disallowed = True
                break
            if (spec, article) not in curated_articles:
                orphan = True
                break
            valid_gold.append(gold)
        if disallowed:
            skipped_disallowed_spec += 1
            continue
        if orphan or not valid_gold:
            skipped_orphan += 1
            continue

        if decision.action == "edit":
            edit_count += 1
            review_status = "edited"
        else:
            approve_count += 1
            review_status = "approved"
        gold_rows.append({
            "id": cand_id,
            "query": query,
            "gold_articles_any": valid_gold,
            "gold_articles_all": [str(g) for g in candidate.get("gold_articles_all", [])],
            "forbidden_articles": [str(g) for g in candidate.get("forbidden_articles", [])],
            "category": candidate.get("category", ""),
            "needs_multiple_articles": bool(candidate.get("needs_multiple_articles", False)) or len(valid_gold) > 1,
            "is_gold": True,
            "review_status": review_status,
            "spec_id": candidate.get("spec_id", ""),
            "source_article": candidate.get("source_article", ""),
            "source_chunk_ids": [str(c) for c in candidate.get("source_chunk_ids", [])],
            "source_file": candidate.get("source_file", ""),
            "source_line": candidate.get("source_line"),
            "review_notes": decision.notes,
        })

    finalized_count = len(gold_rows)
    if finalized_count < min_gold:
        raise GoldFinalizeError(
            f"finalized {finalized_count} gold < {min_gold}; 禁止自动使用未审核候选补足，"
            f"需人工补充审核 {min_gold - finalized_count} 条候选。"
        )

    gold_path = Path(gold_path)
    gold_path.parent.mkdir(parents=True, exist_ok=True)
    if finalized_count > 0:
        gold_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in gold_rows),
            encoding="utf-8",
        )
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "total": finalized_count,
        "finalized_count": finalized_count,
        "approve_count": approve_count,
        "edit_count": edit_count,
        "rejected_count": rejected_count,
        "skipped_blank_query": skipped_blank_query,
        "skipped_blank_gold": skipped_blank_gold,
        "skipped_orphan": skipped_orphan,
        "skipped_disallowed_spec": skipped_disallowed_spec,
        "skipped_unknown_candidate": skipped_unknown_candidate,
        "duplicate_query_ids": duplicate_query_ids,
        "duplicate_id_ids": duplicate_id_ids,
        "by_spec": dict(sorted(Counter(row.get("spec_id", "?") for row in gold_rows).items())),
        "by_category": dict(sorted(Counter(row.get("category", "?") for row in gold_rows).items())),
        "multi_article_count": sum(1 for row in gold_rows if row["needs_multiple_articles"]),
        "gold_path": str(gold_path),
        "review_md_path": str(review_md_path),
        "curated_root": str(curated_root),
    }
    Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
    Path(manifest_path).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
