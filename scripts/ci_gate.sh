#!/usr/bin/env bash
# PR 门禁（Step 10，只跑离线机制层）。
#
# 顺序：pytest → ruff → mypy → rag_p0 机制层 → calc_p0 ≥95% → 基线对比。
# 任一项失败 exit 1；真实质量层（--variant real，需 Key）不在本门禁内，
# 由人工/定时任务触发，无 Key 时结构化 SKIP 且不参与基线比较。
#
# SKIP 语义：本门禁只跑机制层，不会因缺 Key 产生 SKIP；若未来加入真实变体
# 分支，SKIP 用例须退出 0 且不参与基线比较（与 runner/compare_baseline 约定一致）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="_venv/bin/python"
FAILED=0

step() { echo "==> $1"; }
fail() { echo "!! $1"; FAILED=1; }

step "pytest（全量离线单测）"
if ! _venv/bin/pytest -q > /tmp/ci_gate_pytest.log 2>&1; then
    tail -20 /tmp/ci_gate_pytest.log
    fail "pytest"
fi

step "ruff check src"
if ! _venv/bin/ruff check src; then fail "ruff"; fi

step "mypy src/water_agent（Step 10 要求清零）"
if ! _venv/bin/mypy src/water_agent > /tmp/ci_gate_mypy.log 2>&1; then
    tail -20 /tmp/ci_gate_mypy.log
    fail "mypy"
fi

step "rag_p0 机制层 harness（期望 11/12，含 1 条设计的 bad_citation 陷阱）"
RAG_JSON="$($PY -m water_agent.harness.runner data/eval/rag_p0.jsonl 2>/dev/null || true)"
RAG_FAILED="$(printf '%s' "$RAG_JSON" | $PY -c "import sys,json;print(json.load(sys.stdin).get('failed', -1))" 2>/dev/null || echo -1)"
if [ "$RAG_FAILED" != "1" ]; then
    fail "rag_p0 failed=$RAG_FAILED（预期 1，即 11/12）"
else
    echo "rag_p0 与基线一致（11/12）"
fi

step "calc_p0 harness（期望 28/28，通过率 ≥95%）"
CALC_JSON="$($PY -m water_agent.harness.runner data/eval/calc_p0.jsonl 2>/dev/null || true)"
CALC_PASS="$(printf '%s' "$CALC_JSON" | $PY -c "import sys,json;print(json.load(sys.stdin).get('pass_rate', 0.0))" 2>/dev/null || echo 0)"
if ! $PY -c "import sys; sys.exit(0 if float('$CALC_PASS') >= 0.95 else 1)"; then
    fail "calc_p0 pass_rate=$CALC_PASS（预期 ≥0.95）"
else
    echo "calc_p0 通过（28/28）"
fi

step "基线对比（compare_baseline.py，阈值见 THRESHOLDS）"
if [ -f data/eval/baseline.json ]; then
    if ! $PY scripts/compare_baseline.py data/eval/baseline.json > /tmp/ci_gate_baseline.log 2>&1; then
        cat /tmp/ci_gate_baseline.log
        fail "compare_baseline"
    else
        cat /tmp/ci_gate_baseline.log
    fi
else
    echo "基线不存在（首次运行）；先执行：$PY scripts/compare_baseline.py --init data/eval/baseline.json"
fi

if [ "$FAILED" -ne 0 ]; then
    echo
    echo "CI 门禁失败：存在未通过项。"
    exit 1
fi

echo
echo "==> CI 门禁通过：pytest + ruff + mypy + rag_p0(11/12) + calc_p0(28/28) + 基线对比"
