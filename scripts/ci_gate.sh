#!/usr/bin/env bash
# PR 门禁（Step 10 + P2 + P2.5，只跑离线机制层）。
#
# 顺序：pytest → ruff → mypy → rag_p0 机制层 → calc_p0 ≥95%
#      → docgen_p0 ≥95%（P2 文档生成）→ material_p0 ≥95%（P2 材料统计）
#      → governance_p0 32/32（P2.5 治理）→ 基线对比。
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

step "docgen_p0 harness（P2 文档生成，期望 11/11，通过率 ≥95%）"
DOCGEN_JSON="$($PY -m water_agent.harness.runner data/eval/docgen_p0.jsonl 2>/dev/null || true)"
DOCGEN_PASS="$(printf '%s' "$DOCGEN_JSON" | $PY -c "import sys,json;print(json.load(sys.stdin).get('pass_rate', 0.0))" 2>/dev/null || echo 0)"
if ! $PY -c "import sys; sys.exit(0 if float('$DOCGEN_PASS') >= 0.95 else 1)"; then
    fail "docgen_p0 pass_rate=$DOCGEN_PASS（预期 ≥0.95）"
else
    echo "docgen_p0 通过（11/11）"
fi

step "material_p0 harness（P2 材料统计，期望 10/10，通过率 ≥95%）"
MATERIAL_JSON="$($PY -m water_agent.harness.runner data/eval/material_p0.jsonl 2>/dev/null || true)"
MATERIAL_PASS="$(printf '%s' "$MATERIAL_JSON" | $PY -c "import sys,json;print(json.load(sys.stdin).get('pass_rate', 0.0))" 2>/dev/null || echo 0)"
if ! $PY -c "import sys; sys.exit(0 if float('$MATERIAL_PASS') >= 0.95 else 1)"; then
    fail "material_p0 pass_rate=$MATERIAL_PASS（预期 ≥0.95）"
else
    echo "material_p0 通过（10/10）"
fi

step "governance_p0 harness（P2.5 治理，期望 32/32）"
GOV_JSON="$($PY -m water_agent.harness.runner data/eval/governance_p0.jsonl 2>/dev/null || true)"
GOV_FAILED="$(printf '%s' "$GOV_JSON" | $PY -c "import sys,json;print(json.load(sys.stdin).get('failed', -1))" 2>/dev/null || echo -1)"
if [ "$GOV_FAILED" != "0" ]; then
    fail "governance_p0 failed=$GOV_FAILED（预期 0，即 32/32）"
else
    echo "governance_p0 通过（32/32）"
fi

# drawing_p0（P3 Step 22）：合成图纸审查 smoke 集，守护审查子图链路回归。
# ⚠️ 该套件 real_samples=false / accepts_recall_gate=false —— 门禁对比的是
# HEAD 与 main 的用例级回归与 smoke 观测指标（阈值 5pt），**不是**真实缺陷
# 召回率验收；真实样本到手并重标定前，召回率数字不构成验收证据
# （P3 规格 §9.2、p3-step-21-decision.md 残余风险 1–3）。
step "drawing_p0 审查子图 smoke 集（合成图纸，链路回归守护）"
DRAWING_HEAD="data/eval/reports/drawing_p0_head.json"
# main 固化报告入仓（data/eval/drawing_p0_main.json）；HEAD 报告是可重建产物
# （data/eval/reports/ 不入仓）。
DRAWING_MAIN="data/eval/drawing_p0_main.json"
if ! $PY -m water_agent.harness.drawing_eval tests/eval/drawing_p0 --report-path "$DRAWING_HEAD" > /tmp/ci_gate_drawing.log 2>&1; then
    tail -20 /tmp/ci_gate_drawing.log
    fail "drawing_p0 runner"
else
    tail -14 /tmp/ci_gate_drawing.log
    if [ -f "$DRAWING_MAIN" ]; then
        if ! $PY scripts/compare_drawing_p0.py --head "$DRAWING_HEAD" --base "$DRAWING_MAIN" > /tmp/ci_gate_drawing_cmp.log 2>&1; then
            cat /tmp/ci_gate_drawing_cmp.log
            fail "drawing_p0 基线对比"
        else
            cat /tmp/ci_gate_drawing_cmp.log
        fi
    else
        echo "drawing_p0 无 main 基线报告（首次运行）；固化方法见 scripts/compare_drawing_p0.py 文档头"
    fi
fi

step "multi_agent_p0 合成机制层（12 用例，8 硬指标全绿）"
MA_HEAD="data/eval/reports/multi_agent_p0_head.json"
MA_BASE="data/eval/multi_agent_p0_main.json"
if ! $PY -m water_agent.harness.multi_agent_eval tests/eval/multi_agent_p0 --out "$MA_HEAD" > /tmp/ci_gate_multi.log 2>&1; then
    tail -20 /tmp/ci_gate_multi.log
    fail "multi_agent_p0 harness"
else
    tail -8 /tmp/ci_gate_multi.log
    if [ -f "$MA_BASE" ]; then
        if ! $PY scripts/compare_multi_agent_p0.py --head "$MA_HEAD" --base "$MA_BASE" > /tmp/ci_gate_multi_cmp.log 2>&1; then
            cat /tmp/ci_gate_multi_cmp.log
            fail "multi_agent_p0 基线对比"
        else
            cat /tmp/ci_gate_multi_cmp.log
        fi
    else
        echo "multi_agent_p0 无 main 基线（首次运行）；固化：cp $MA_HEAD $MA_BASE"
    fi
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

# memory_p0（P5 Step 45）：长期记忆隔离与安全硬指标。
# ⚠️ 该套件度量的是**合成机制与隔离契约**：gold_exact_recall_rate 用合成关键词 gold，
# 不代表任意中文表达的召回率；blocked_injection_persisted_count=0 只证明"已拦下的
# 变体没有落盘"，护栏是确定性正则，必然存在未覆盖改写。语义召回质量属 Step 46。
# docker/postgres 相关用例在本门禁里允许结构化 SKIP（--release-gate 关闭该行为，
# CI 的 memory-p0 job 用它；发布门禁环境不得 SKIP）。
step "memory_p0 长期记忆硬指标（§13.2 十二项，报告为可重建产物）"
MEMORY_HEAD="data/eval/reports/memory_p0_head.json"
MEMORY_BASE="data/eval/memory_p0_main.json"
if ! $PY -m water_agent.harness.memory_eval data/eval/memory_p0.jsonl --out "$MEMORY_HEAD" \
    > /tmp/ci_gate_memory.log 2>&1; then
    tail -40 /tmp/ci_gate_memory.log
    fail "memory_p0 harness"
else
    tail -20 /tmp/ci_gate_memory.log
    if [ -f "$MEMORY_BASE" ]; then
        if ! $PY scripts/compare_memory_p0.py --head "$MEMORY_HEAD" --base "$MEMORY_BASE" \
            > /tmp/ci_gate_memory_cmp.log 2>&1; then
            cat /tmp/ci_gate_memory_cmp.log
            fail "memory_p0 基线对比"
        else
            head -14 /tmp/ci_gate_memory_cmp.log
        fi
    else
        echo "memory_p0 无入仓基线（首次运行）；固化：cp $MEMORY_HEAD $MEMORY_BASE"
    fi
fi

if [ "$FAILED" -ne 0 ]; then
    echo
    echo "CI 门禁失败：存在未通过项。"
    exit 1
fi

echo
echo "==> CI 门禁通过：pytest + ruff + mypy + rag_p0(11/12) + calc_p0(28/28) + docgen_p0(11/11) + material_p0(10/10) + governance_p0(32/32) + drawing_p0(合成 smoke 回归) + multi_agent_p0 + memory_p0(§13.2 十二项硬指标) + 基线对比"
