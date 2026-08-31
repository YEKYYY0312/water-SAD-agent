"""检索评测矩阵：同一 gold × 多维度 × 多 rerank 策略 → 对比 Recall@k/MRR。

用途：给 P1 检索质量定一个可复现的基线（哪个维度 + 哪种 rerank 最优，以及准入门槛）。

对比纪律（保证可比性）：
- 同一 gold、同一 top-k / recall-top-k / hybrid-alpha
- 每个组合独立重建 retriever（按维度配 embedding）+ reranker（可选）
- bailian rerank 需要 DASHSCOPE_API_KEY（或 --api-key）；none/local 不需要云端 rerank，
  但**检索时仍需要真实 query embedding**，因此整套评测都需要 DASHSCOPE_API_KEY。

输出：data/eval/reports/retrieval_matrix_<UTC>.md（对比表 + 结论），并回显到 stdout。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

from water_agent.embedding import build_embedding_provider
from water_agent.harness.retrieval_metrics import evaluate_retrieval
from water_agent.retrieval.rerank import BailianReranker, LocalReranker, build_bailian_reranker
from water_agent.retrieval.trivium import TriviumRetriever

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GOLD = REPO_ROOT / "data" / "eval" / "retrieval_gold.jsonl"
DEFAULT_DB_256 = REPO_ROOT / "data" / "indexes" / "qwen3.7__256__curated.db"
DEFAULT_DB_512 = REPO_ROOT / "data" / "indexes" / "qwen3.7__512__curated.db"
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "eval" / "reports"


def _db_for(dim: int) -> Path:
    return REPO_ROOT / "data" / "indexes" / f"qwen3.7__{dim}__curated.db"


def _reranker(kind: str, api_key: str | None):
    if kind == "none":
        return None
    if kind == "local":
        return LocalReranker()
    return build_bailian_reranker(api_key=api_key, model="gte-rerank")


def _load_gold(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def run_case(
    *,
    dim: int,
    rerank: str,
    gold_path: Path,
    top_k: int,
    recall_top_k: int,
    hybrid_alpha: float,
    api_key: str | None,
) -> dict:
    provider = build_embedding_provider(api_key=api_key, dimensions=dim)
    retriever = TriviumRetriever(
        db_path=str(_db_for(dim)),
        embedding_fn=provider.embed,
        hybrid_alpha=hybrid_alpha,
        dim=dim,
    )
    reranker = _reranker(rerank, api_key)

    cases = []
    for row in _load_gold(gold_path):
        q = row.get("query", "")
        gold = row.get("gold_articles_any", [])
        hits = retriever.retrieve(q, top_k=recall_top_k)
        if reranker is not None:
            hits = reranker.rerank(q, hits, top_k=top_k)
        else:
            hits = hits[: top_k]
        cases.append({"retrieved": [h.source_id for h in hits], "gold": gold})

    result = evaluate_retrieval(cases)
    result |= {
        "dimensions": dim,
        "rerank": rerank,
        "top_k": top_k,
        "recall_top_k": recall_top_k,
        "hybrid_alpha": hybrid_alpha,
        "queries": len(cases),
    }
    return result


def _fmt(v: float) -> str:
    return f"{v:.4f}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="检索评测矩阵（维度 × rerank）")
    p.add_argument("--gold", default=str(DEFAULT_GOLD))
    p.add_argument("--dimensions", nargs="+", type=int, default=[256, 512])
    p.add_argument("--rerank", nargs="+", default=["none", "local", "bailian"])
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--recall-top-k", type=int, default=20)
    p.add_argument("--hybrid-alpha", type=float, default=0.7)
    p.add_argument("--api-key", default=None, help="DASHSCOPE_API_KEY（默认读环境变量）")
    p.add_argument("--out", default=None, help="报告输出路径（默认 data/eval/reports/retrieval_matrix_<UTC>.md）")
    args = p.parse_args(argv)

    api_key = args.api_key if args.api_key is not None else os.environ.get("DASHSCOPE_API_KEY", "")
    if not api_key.strip():
        print(json.dumps({"ok": False, "error": "缺少 DASHSCOPE_API_KEY（环境变量或 --api-key）"}, ensure_ascii=False))
        return 2

    for dim in args.dimensions:
        if not _db_for(dim).exists():
            print(json.dumps({"ok": False, "error": f"索引不存在: {_db_for(dim)}"}, ensure_ascii=False))
            return 2

    results = []
    for dim in args.dimensions:
        for kind in args.rerank:
            try:
                r = run_case(
                    dim=dim, rerank=kind, gold_path=Path(args.gold),
                    top_k=args.top_k, recall_top_k=args.recall_top_k,
                    hybrid_alpha=args.hybrid_alpha, api_key=api_key,
                )
                results.append(r)
                print(json.dumps({"ok": True, **r}, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001 — single combo failing shouldn't kill the matrix
                print(json.dumps({"ok": False, "dimensions": dim, "rerank": kind, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)

    # ---- 聚合对比表 ----
    header = "| dim | rerank | Recall@1 | Recall@5 | Recall@10 | MRR |\n|---|---|---|---|---|---|\n"
    rows = ""
    for r in sorted(results, key=lambda x: (x["dimensions"], x["rerank"])):
        rows += (
            f"| {r['dimensions']} | {r['rerank']} | {_fmt(r['recall_at_1'])} | "
            f"{_fmt(r['recall_at_5'])} | {_fmt(r['recall_at_10'])} | {_fmt(r['mrr'])} |\n"
        )

    # 泄漏自检：如果任意组合 Recall@1 都接近 1.0，gold 极可能由源条款自动派生（query≈条文文本），
    # 此时指标被高估、无法区分维度/rerank，不能当作有效基线。
    leaky = bool(results) and all(r["recall_at_1"] >= 0.95 for r in results)
    best_mrr = max(results, key=lambda x: x["mrr"]) if results else None
    gapless = [r for r in results if r["recall_at_5"] >= 0.7]
    summary = ""
    if results:
        summary += (
            f"**样本量**：{results[0]['queries']} 条 gold；"
            f"top_k={results[0]['top_k']}，recall_top_k={results[0]['recall_top_k']}，"
            f"hybrid_alpha={results[0]['hybrid_alpha']}（全部组合保持一致）。\n\n"
        )
        summary += f"**最优（按 MRR）**：dim={best_mrr['dimensions']} / rerank={best_mrr['rerank']}，MRR={_fmt(best_mrr['mrr'])}，Recall@5={_fmt(best_mrr['recall_at_5'])}。\n"
        gapless_str = ", ".join(
            f"dim={r['dimensions']}/rerank={r['rerank']}" for r in gapless
        ) or "无（未达到门槛）"
        summary += (
            f"**达标组合（Recall@5 ≥ 0.70）**：{gapless_str}。\n\n"
        )
        summary += (
            "**P1 质量基线建议**：以「规范问答检索 Recall@5 ≥ 0.70」为硬门槛；"
            "在达到门槛的组合里选 MRR 最高者作为默认配置。若 Recall@5 全低于门槛，"
            "说明需要扩充/清洗语料或调整 hybrid_alpha / 索引维度，而非只依赖 rerank。\n"
        )
        if leaky:
            summary += (
                "> ⚠️ **泄漏警告**：所有组合 Recall@1 均 ≥ 0.95。这通常是 gold 由源条款自动派生"
                "（query≈条文原文、gold=source_article）导致的——此时检索把源条款排在第 1 是必然的，"
                "**该指标不能反映真实检索质量，不能作为 P1 基线**。请改用独立构造、可人工验证、且含"
                "「语料内无答案」负例的 gold，再重新评测。\n\n"
            )
        if any(r.get("ok") is False for r in results):
            summary += (
                "> ⚠️ **含失败组合**：部分组合未产出指标（多为 bailian rerank 403 AccessDenied，"
                "通常是该 key 无 gte-rerank 模型访问权限，需在百炼/Model Studio 开通该模型）。\n\n"
            )
    else:
        summary = "⚠️ 所有组合均失败（缺少密钥或索引异常），未产生有效指标。参见上方逐条 stderr。\n"

    doc = (
        "# 检索评测矩阵报告\n\n"
        f"- 生成时间：`{_dt.datetime.now(_dt.timezone.utc).isoformat()}`\n"
        f"- gold：`{args.gold}`\n"
        "- 对比纪律：同一 gold / top_k / recall-top-k / hybrid-alpha\n\n"
        f"## 对比表\n\n{header}{rows}\n\n"
        f"## 结论\n\n{summary}"
    )

    out_path = Path(args.out) if args.out else DEFAULT_OUT_DIR / f"retrieval_matrix_{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    print(f"\n报告已写入：{out_path}")

    # 退出码语义：仅当「无泄漏 + 无失败 + 全部组合达标」才视为一次干净的基线跑分，否则非零提示需处理。
    clean = bool(results) and not leaky and all(r.get("ok") is not False for r in results)
    passed = clean and all(r.get("recall_at_5", 0) >= 0.7 for r in results)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
