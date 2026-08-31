"""正式评测集固化：只把人工确认过的候选固化为 gold。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from water_agent.domain.models import utc_now


def _candidate_index(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {c.get("id"): c for c in candidates}


def finalize_gold_set(
    candidate_file: str | Path,
    *,
    human_confirmed_ids: list[str],
    gold_path: str | Path,
    manifest_path: str | Path,
) -> dict[str, Any]:
    """从候选集把人工确认的 id 固化为正式 gold 评测样例。

    只有 ``human_confirmed_ids`` 中显式列出的候选会被固化；
    其余候选保持 candidate 状态，绝不自动升级为 gold。
    """
    payload = json.loads(Path(candidate_file).read_text(encoding="utf-8"))
    candidates = payload.get("candidates", [])
    index = _candidate_index(candidates)
    gold_rows: list[dict[str, Any]] = []
    finalized_ids: list[str] = []
    skipped = 0
    for cand_id in human_confirmed_ids:
        candidate = index.get(cand_id)
        if candidate is None:
            skipped += 1
            continue
        gold_rows.append({
            "id": candidate.get("id"),
            "status": "gold",
            "is_gold": True,
            "query": candidate.get("query", ""),
            "gold_articles_any": candidate.get("candidate_gold_articles", []),
            "spec_id": candidate.get("spec_id", ""),
            "article": candidate.get("article", ""),
        })
        finalized_ids.append(cand_id)
    out = Path(gold_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in gold_rows),
        encoding="utf-8",
    )
    manifest = {
        "created_at": utc_now().isoformat(),
        "source_candidates": str(candidate_file),
        "finalized_count": len(gold_rows),
        "finalized_ids": finalized_ids,
        "requested_ids": human_confirmed_ids,
        "skipped_unmatched_ids": skipped,
        "gold_path": str(out),
    }
    Path(manifest_path).parent.mkdir(parents=True, exist_ok=True)
    Path(manifest_path).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest