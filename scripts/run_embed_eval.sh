#!/usr/bin/env bash
# 建 256/512 两套索引 + 用同一 gold 评测，重排序后的 Recall@5
set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
DB512="$ROOT/data/indexes/qwen3.7__512__curated.db"
DB256="$ROOT/data/indexes/qwen3.7__256__curated.db"
GOLD="$ROOT/data/eval/retrieval_gold.jsonl"

echo "==== 1/4 构建 512 维索引 ===="
_venv/bin/python scripts/build_index.py \
  --curated-root data/curated --db-path "$DB512" --dimensions 512

echo "==== 2/4 构建 256 维索引 ===="
_venv/bin/python scripts/build_index.py \
  --curated-root data/curated --db-path "$DB256" --dimensions 256

echo "==== 3/4 评测 512（Recall@5, rerank=local）===="
_venv/bin/python scripts/evaluate_retrieval.py \
  --db-path "$DB512" --gold "$GOLD" --dimensions 512 --top-k 5 --rerank local

echo "==== 4/4 评测 256（Recall@5, rerank=local）===="
_venv/bin/python scripts/evaluate_retrieval.py \
  --db-path "$DB256" --gold "$GOLD" --dimensions 256 --top-k 5 --rerank local

echo "==== 完成 ===="
