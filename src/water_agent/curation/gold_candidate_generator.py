"""候选检索问题生成器：基于真实受控语料生成候选，永不固化为正式 gold。"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from water_agent.domain.models import utc_now

INCLUDED_SPECS = frozenset({"GB50013-2018", "GB50014-2021", "GB50318-2017"})
SPEC_TARGET = {"GB50013-2018": 30, "GB50014-2021": 30, "GB50318-2017": 10}
_CLEAN_RE = re.compile(r"\s+")
_QUANT_RE = re.compile(r"(DN\s*\d+|DE\s*\d+|[0-9]+(?:\.[0-9]+)?\s*(?:m|mm|m/s|m\u00b3|m3|L/s|mg/L|MPa))", re.IGNORECASE)

CATEGORY_RULES = [
    ("管径", re.compile(r"管径|公称直径|DN|DE|支管")),
    ("充满度", re.compile(r"充满度")),
    ("流量", re.compile(r"流量|秒流量|设计流量|最大时流量")),
    ("流速", re.compile(r"流速|流速范围|速度")),
    ("坡度", re.compile(r"坡度|坡降")),
    ("布置", re.compile(r"布置|位置|布置原则|走向|间距")),
    ("材料", re.compile(r"材料|材质|管材|铸铁|钢管|塑料|混凝土")),
    ("设施", re.compile(r"泵站|检查井|雨水口|调节池|水池|构筑物")),
    ("规划", re.compile(r"规划|总体|远期|近远期|管网系统|布局|排水体制")),
    ("规定要求", re.compile(r"应|须|不得|宜|符合|执行|规定")),
]


def _clean(text: str) -> str:
    return " ".join(_CLEAN_RE.split(str(text or ""))).strip("|,，。；; ")


def _article_text(record: dict[str, Any]) -> str:
    return _clean(record.get("text", ""))


_FRAGMENT_RE = re.compile(r"^\s*[、|]|、\s*\d+\.\s*\d+\.\s*\d+")


def _first_sentence(text: str) -> str:
    for sep in ("。", "；", ";", "\n"):
        if sep in text:
            lead = text.split(sep)[0].strip("| ，；;。")
            if len(lead) >= 10:
                return lead
    return text[:60].strip("| ，；;。") if text else ""


def _first_readable_sentence(record: dict[str, Any]) -> str:
    """返回首个可做问题的正文句；若开头是条文号列表/表格碎片，则返回空串。

    判据：去掉空白后不是“、开头”，也不含大量分散条文号（X. Y. Z）或表格分隔。
    """
    text = _clean(record.get("text", ""))
    for sep in ("。", "；", ";", "\n"):
        if sep not in text:
            continue
        lead = text.split(sep)[0].strip("| ，；;。")
        if len(lead) < 10:
            continue
        if _FRAGMENT_RE.match(lead):
            continue
        if "|" in lead or "---" in lead:
            continue
        # 分散条文号：多个“数字. 数字. 数字”且带空格的 OCR 样式
        scatter = re.findall(r"\d+\s*\.\s*\d+", lead)
        if len(scatter) >= 3:
            continue
        return lead
    lead = text[:60].strip("| ，；;。")
    if lead and not _FRAGMENT_RE.match(lead) and "|" not in lead and len(re.findall(r"\d+\s*\.\s*\d+", lead)) < 3:
        return lead
    return ""


def _quant_conditions_from(text: str) -> list[str]:
    return list(dict.fromkeys(tok for tok in _QUANT_RE.findall(text)))


def _category_for(record: dict[str, Any]) -> str:
    blob = f"{record.get('title')} {record.get('text')}"
    for name, pattern in CATEGORY_RULES:
        if pattern.search(blob):
            return name
    return "规定要求"


def _natural_query(record: dict[str, Any]) -> str:
    text = _article_text(record)
    title = _clean(record.get("title", "")).strip("，。;；")
    quantities = _quant_conditions_from(text)
    sentence = _first_readable_sentence(record)
    query_sentence = sentence or _first_readable_sentence(record)
    if not query_sentence:
        query_sentence = _first_sentence(text)
    if quantities and query_sentence:
        return f"在«{query_sentence}»的情况下，按条文的执行标准有什么要求？"
    if query_sentence:
        return f"根据条文，{query_sentence} 应如何执行？"
    if title and len(title) >= 8 and not _FRAGMENT_RE.match(title) and not re.match(r'^[\d|、]', title):
        return f"对“{title}”有哪些具体要求？请按条款说明。"
    return f"请提供{record.get('spec_id')} {record.get('article')}的条款内容。"


def _candidate_id(record: dict[str, Any], index: int) -> str:
    raw = "|".join([
        str(record.get("spec_id", "")),
        str(record.get("article", "")),
        str(record.get("fragment_id", "")),
        str(index),
    ])
    return "candidate-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]


def _iter_curated_records(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((root / "clauses").glob("*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    return rows


def generate_candidate_queries(
    root: str | Path,
    output: str | Path | None = None,
    *,
    target: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    base = Path(root)
    records = _iter_curated_records(base)
    targets = dict(target or dict(SPEC_TARGET))
    by_spec: dict[str, list[dict[str, Any]]] = {}
    seen_article: set[tuple[str, str]] = set()
    for record in records:
        spec = str(record.get("spec_id", ""))
        if spec not in INCLUDED_SPECS:
            continue
        article = str(record.get("article", ""))
        if (spec, article) in seen_article or not article or not _first_readable_sentence(record):
            continue
        seen_article.add((spec, article))
        by_spec.setdefault(spec, []).append(record)

    candidates: list[dict[str, Any]] = []
    global_index = 0
    consumed: Counter[str] = Counter()
    spec_order = [spec for spec in ("GB50013-2018", "GB50014-2021", "GB50318-2017") if spec in by_spec]
    while True:
        progressed = False
        for spec in spec_order:
            if consumed[spec] >= targets.get(spec, 0):
                continue
            pool = by_spec.get(spec, [])
            if consumed[spec] >= len(pool):
                continue
            record = pool[consumed[spec]]
            consumed[spec] += 1
            progressed = True
            global_index += 1
            article = str(record.get("article", ""))
            spec_id = str(record.get("spec_id", ""))
            query = _natural_query(record)
            category = _category_for(record)
            candidates.append({
                "id": _candidate_id(record, global_index),
                "query": query,
                "title": _clean(record.get("title", "")),
                "gold_articles_any": [f"{spec_id} {article}"],
                "gold_articles_all": [],
                "forbidden_articles": [],
                "category": category,
                "spec_id": spec_id,
                "source_article": article,
                "source_chunk_ids": [str(record.get("fragment_id", str(record.get("source_line", ""))))],
                "source_file": str(record.get("source_file", "")),
                "source_line": int(record.get("source_line", 0) or 0),
                "text_summary": _first_readable_sentence(record) or _first_sentence(_article_text(record)),
                "rationale": f"基于条款正文生成（{spec_id} {article}，类别：{category}），未引入正文之外的工程条件。",
                "needs_multiple_articles": False,
                "is_gold": False,
                "review_status": "pending",
                "is_candidate": True,
                "status": "candidate",
                "source": {"file": str(record.get("source_file", "")), "line": record.get("source_line"), "article": article},
            })

        if not progressed:
            break

    if output is not None:
        write_candidate_files(candidates, output, base=base)
    return candidates


def write_candidate_files(candidates: list[dict[str, Any]], jsonl_path: str | Path, *, base: Path) -> tuple[Path, Path]:
    jsonl = Path(jsonl_path)
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    jsonl.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in candidates), encoding="utf-8")
    md = jsonl.with_suffix(".md")
    lines = [
        "# 检索评测候选集（供人工审核）",
        "",
        f"- 生成时间：`{utc_now().isoformat()}`",
        f"- 候选数量：{len(candidates)}",
        f"- 来源语料：`{base}`",
        "- 这批候选不自动引入正式 gold；`is_gold=false`，需人工 approve/edit/reject。",
        "",
        "| id | query | gold_articles_any | category | source_article | 条款摘要 | 审核动作 | 修改后query | 修改后gold | 备注 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in candidates:
        summary = c.get("text_summary", "")[:40]
        lines.append(
            f"| {c['id']} | {c['query']} | {';'.join(c['gold_articles_any'])} | {c['category']} | "
            f"{c.get('source_article','')} | {summary} |  |  |  |  |"
        )
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jsonl, md
