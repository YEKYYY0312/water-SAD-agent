#!/usr/bin/env bash
# 统一验证入口（离线机制层质量门禁）。
# 任一项失败即 exit 1；真实质量层（需要 Key/网络）不在此门禁内，见 Step 10 ci_gate.sh。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="_venv/bin/python"
FAILED=0

step() { echo "==> $1"; }
fail() { echo "!! $1"; FAILED=1; }

step "pytest"
if ! _venv/bin/pytest -q; then fail "pytest"; fi

step "ruff check src"
if ! _venv/bin/ruff check src; then fail "ruff"; fi

step "rag_p0 harness（期望 11/12，含 1 条设计的 bad_citation 陷阱）"
RAG_JSON="$($PY -m water_agent.harness.runner data/eval/rag_p0.jsonl 2>/dev/null || true)"
RAG_FAILED="$(printf '%s' "$RAG_JSON" | $PY -c "import sys,json;print(json.load(sys.stdin).get('failed', -1))" 2>/dev/null || echo -1)"
if [ "$RAG_FAILED" != "1" ]; then
    fail "rag_p0 failed=$RAG_FAILED（预期 1，即 11/12）"
else
    echo "rag_p0 与基线一致（11/12）"
fi

step "calc_p0 harness（期望 28/28）"
CALC_JSON="$($PY -m water_agent.harness.runner data/eval/calc_p0.jsonl 2>/dev/null || true)"
CALC_PASS="$(printf '%s' "$CALC_JSON" | $PY -c "import sys,json;print(json.load(sys.stdin).get('pass_rate', 0.0))" 2>/dev/null || echo 0)"
if [ "$CALC_PASS" != "1.0" ]; then
    fail "calc_p0 pass_rate=$CALC_PASS（预期 1.0）"
else
    echo "calc_p0 通过（28/28）"
fi

if [ "$FAILED" -ne 0 ]; then
    echo
    echo "验证失败：存在未通过项。"
    exit 1
fi

echo
echo "==> 全部通过：pytest + ruff + rag_p0(11/12) + calc_p0(22/22)"
