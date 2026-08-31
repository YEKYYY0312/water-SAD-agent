#!/usr/bin/env bash
set -u
cd "$(dirname "$0")/.."
if [[ -z "${DASHSCOPE_API_KEY:-}" ]]; then echo "请先设置: export DASHSCOPE_API_KEY=你的百炼Key" >&2; exit 1; fi
echo "======== 1/3 百炼 gte-rerank 评测（512）========"
./_venv/bin/python scripts/evaluate_retrieval.py --db-path data/indexes/qwen3.7__512__curated.db --gold data/eval/retrieval_gold.jsonl --dimensions 512 --top-k 5 --rerank bailian --rerank-model gte-rerank --rerank-api-key "$DASHSCOPE_API_KEY"
echo "======== 2/3 百炼 gte-rerank 评测（256）========"
./_venv/bin/python scripts/evaluate_retrieval.py --db-path data/indexes/qwen3.7__256__curated.db --gold data/eval/retrieval_gold.jsonl --dimensions 256 --top-k 5 --rerank bailian --rerank-model gte-rerank --rerank-api-key "$DASHSCOPE_API_KEY"
echo "======== 3/3 真实问答（rerank+DeepSeek）========"
./_venv/bin/python -m water_agent.cli "给水管道设计秒流量如何确定？" --db-path data/indexes/qwen3.7__512__curated.db --dimensions 512 --rerank bailian --rerank-model gte-rerank
echo "======== 完成 ========"
