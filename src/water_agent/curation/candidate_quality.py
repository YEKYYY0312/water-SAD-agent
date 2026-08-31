"""候选检索集的质量校验：不允许自动固化为 gold。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ALLOWED_SPECS = frozenset({"GB50013-2018", "GB50014-2021", "GB50318-2017"})
_PIPE_RE = re.compile(r"(DN\s*\d+|DE\s*\d+|[0-9]+(?:\.[0-9]+)?\s*(?:m|mm|m/s|m³|m3|L/s|mg/L|MPa))", re.IGNORECASE)


def _load_curated_texts(curated_root: str | Path) -> dict[tuple[str, str], dict[str, list[str]]]:
    base = Path(curated_root)
    index: dict[tuple[str, str], dict[str, list[str]]] = {}
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
                if spec not in ALLOWED_SPECS:
                    continue
                article = str(record.get("article", ""))
                entry = index.setdefault((spec, article), {"titles": [], "texts": []})
                entry["titles"].append(str(record.get("title", "")))
                entry["texts"].append(str(record.get("text", "")))
    return index


def _conditions(text: str) -> set[str]:
    return set(_PIPE_RE.findall(text or ""))


def validate_candidate_set(
    candidates: list[dict[str, Any]],
    *,
    curated_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    source_index = _load_curated_texts(curated_root) if curated_root is not None else {}
    seen_query: set[str] = set()
    seen_source: set[tuple[str, str]] = set()
    for c in candidates:
        ident = c.get("id", "?")
        query = str(c.get("query", "")).strip()
        spec = str(c.get("spec_id", ""))
        article = str(c.get("source_article", ""))
        golds = [str(g) for g in c.get("gold_articles_any", [])]
        if not query:
            issues.append({"id": ident, "fatal": True, "message": "empty query"})
        if query and query in seen_query:
            issues.append({"id": ident, "fatal": True, "message": "duplicate query"})
        if query:
            seen_query.add(query)
        if spec not in ALLOWED_SPECS:
            issues.append({"id": ident, "fatal": True, "message": f"excluded_spec:{spec}"})
        else:
            entry = source_index.get((spec, article), {"titles": [], "texts": []})
            titles = entry.get("titles", [])
            texts = entry.get("texts", [])
            if not article:
                issues.append({"id": ident, "fatal": True, "message": "missing source_article"})
            elif titles and query in titles:
                issues.append({"id": ident, "fatal": True, "message": "query equals title"})
            elif (spec, article) in seen_source:
                issues.append({"id": ident, "fatal": True, "message": "duplicate source"})
            if texts:
                combined = " ".join(texts)
                extra = _conditions(query) - _conditions(combined)
                if extra:
                    issues.append({"id": ident, "fatal": True, "message": f"fabricated_condition:{sorted(extra)}"})
            seen_source.add((spec, article))
        if not golds:
            issues.append({"id": ident, "fatal": True, "message": "missing gold_articles_any"})
    return issues
