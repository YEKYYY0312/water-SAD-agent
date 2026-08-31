"""用同一份 gold 评测同一受控语料的索引，对比 256/512 的 Recall@k。

支持混合检索 + rerank：
- 混合检索先召回 ``--recall-top-k`` 候选；
- (可选) 用 ``--rerank`` 对候选按 query 重排；
- 最终截断到 ``--top-k`` 作为评测判定集合。
要求对比时保持 同一受控语料、同一 gold、同一 top-k / recall-top-k / hybrid-alpha。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from water_agent.embedding import build_embedding_provider
from water_agent.harness.retrieval_metrics import evaluate_retrieval
from water_agent.retrieval.rerank import BailianReranker, LocalReranker, build_bailian_reranker
from water_agent.retrieval.trivium import TriviumRetriever


def _build_reranker(args: argparse.Namespace):
    if args.rerank == "none":
        return None
    if args.rerank == "local":
        return LocalReranker()
    return build_bailian_reranker(
        api_key=args.rerank_api_key,
        model=args.rerank_model or BailianReranker.DEFAULT_MODEL,
        base_url=args.rerank_base_url or BailianReranker.DEFAULT_BASE_URL,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Evaluate Recall@k against a TriviumDB index")
    p.add_argument("--db-path", required=True)
    p.add_argument("--gold", required=True, help="gold jsonl，每行含 query + gold_articles_any")
    p.add_argument("--dimensions", type=int, default=512, choices=[256, 512, 768, 1024])
    p.add_argument("--top-k", type=int, default=10, help="最终截断条数（传入评测判定）")
    p.add_argument("--recall-top-k", type=int, default=20, help="混合检索召回的候选条数（先于此再 rerank）")
    p.add_argument("--rerank", choices=["none", "local", "bailian"], default="local",
                   help="rerank 策略：local=本地确定性重排；bailian=百炼 gte-rerank；none=保留原混合序")
    p.add_argument("--rerank-model", default=None, help="百炼 rerank 模型名（默认 gte-rerank）")
    p.add_argument("--rerank-base-url", default=None, help="百炼 rerank base_url（默认 compatible-mode）")
    p.add_argument("--rerank-api-key", default=None, help="百炼 rerank API Key（默认读 DASHSCOPE_API_KEY）")
    p.add_argument("--hybrid-alpha", type=float, default=0.7)
    p.add_argument("--report-dir", default="data/eval/reports", help="评测报告输出目录（默认 data/eval/reports）")
    args = p.parse_args(argv)

    provider = build_embedding_provider(dimensions=args.dimensions)
    retriever = TriviumRetriever(
        db_path=args.db_path,
        embedding_fn=provider.embed,
        hybrid_alpha=args.hybrid_alpha,
        dim=args.dimensions,
    )
    reranker = _build_reranker(args)

    cases = []
    for line in Path(args.gold).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        q = obj.get("query", "")
        gold = obj.get("gold_articles_any", [])
        hits = retriever.retrieve(q, top_k=args.recall_top_k)
        if reranker is not None:
            hits = reranker.rerank(q, hits, top_k=args.top_k)
        else:
            hits = hits[: args.top_k]
        retrieved = [h.source_id for h in hits]
        cases.append({"retrieved": retrieved, "gold": gold})

    result = evaluate_retrieval(cases)
    out = {
        "ok": True,
        "db_path": args.db_path,
        "gold": args.gold,
        "dimensions": args.dimensions,
        "top_k": args.top_k,
        "recall_top_k": args.recall_top_k,
        "rerank": args.rerank,
        "hybrid_alpha": args.hybrid_alpha,
        **result,
    }
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc)
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    report_path = report_dir / f"retrieval_{args.dimensions}_{args.rerank}_{stamp}.json"
    payload = {**out, "created_at": created_at.isoformat()}
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out["report_path"] = str(report_path)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
