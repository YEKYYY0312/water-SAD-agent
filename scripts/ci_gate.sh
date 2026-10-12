#!/usr/bin/env bash
# PR 门禁（Step 10 + P2 + P2.5，只跑离线机制层）。
#
# 顺序：pytest → ruff → mypy → 机制层 runner 六套（rag/calc/docgen/material/governance/agentic）
#      → tool_exposure_p0 → skill_p0 → mcp_http_p0 → drawing_p0 → drawing_gen_p0 → multi_agent_p0 → 基线对比 → memory_p0。
# 任一项失败 exit 1；真实质量层（--variant real，需 Key）不在本门禁内，
# 由人工/定时任务触发，无 Key 时结构化 SKIP 且不参与基线比较。
#
# 判定只认退出码与入仓基线（review 2026-09-30）：
# - 机制层 runner 必须 exit 0（全过 + 无加载问题 + 至少 1 条用例）；此前各步
#   `2>/dev/null || true` 后只解析失败条数，数据集字段改名让用例静默变成加载问题、
#   条数变少，门禁照样绿。用例是否被删由各比较器的"基线用例缺失"检查兜底。
# - 基线文件缺失 = 失败。此前只打印"首次运行"就放行，删掉基线等于关掉回归判定；
#   首次接入新套件时显式设 CI_GATE_ALLOW_MISSING_BASELINE=1。
#
# SKIP 语义：本门禁只跑机制层，不会因缺 Key 产生 SKIP；若未来加入真实变体
# 分支，SKIP 用例须退出 0 且不参与基线比较（与 runner/compare_baseline 约定一致）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# 导入位置（P7-S0 H0）：worktree 里 _venv 的 editable install 指向主检出，不设 PYTHONPATH
# 时 pytest 与各 harness 子进程导入的都是主检出的代码——门禁照样全绿，验证的却是别的代码。
# 这里钉到本检出的 src/，并在下面核对实际导入位置。
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
# 虚拟环境：显式 VENV > 本机约定的 _venv > uv sync 默认产出的 .venv。
# 此前硬编码 _venv，而 GitHub runner 上 `uv sync` 建的是 .venv，p5-ci 的
# python-gate 因此必然 "No such file or directory"（review 09-28）。找不到就响亮
# 失败，不退回系统 python——那会用错依赖版本跑出一个假绿。
if [ -n "${VENV:-}" ]; then
    :
elif [ -x "_venv/bin/python" ]; then
    VENV="_venv"
elif [ -x ".venv/bin/python" ]; then
    VENV=".venv"
else
    echo "!! 找不到虚拟环境（_venv/ 或 .venv/）；先 uv sync --extra dev，或设置 VENV=路径" >&2
    exit 2
fi
PY="$VENV/bin/python"
IMPORTED_FROM="$("$PY" -c 'import pathlib, water_agent; print(pathlib.Path(water_agent.__file__).resolve().parent)')" || {
    echo "!! 无法用 $PY 导入 water_agent" >&2
    exit 2
}
if [ "$IMPORTED_FROM" != "$(cd "$ROOT/src/water_agent" && pwd -P)" ]; then
    echo "!! water_agent 导入自 $IMPORTED_FROM，不是本检出的 $ROOT/src/water_agent" >&2
    exit 2
fi
echo "water_agent 导入自：$IMPORTED_FROM"
FAILED=0

step() { echo "==> $1"; }
fail() { echo "!! $1"; FAILED=1; }

# 本次门禁专属的报告目录：compare_baseline 取"最新报告"只在这里找，data/eval/reports
# 里任意命名的旧拷贝不得压过本次结果（按文件名字典序取最新时曾实测判错）。
GATE_REPORT_DIR="data/eval/reports/ci_gate"
rm -rf "$GATE_REPORT_DIR"
mkdir -p "$GATE_REPORT_DIR"
# 诊断日志放每次门禁独有的临时目录：多个 worktree 并发跑门禁时不再互相覆盖。
LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ci_gate.XXXXXX")"
echo "门禁日志目录：$LOG_DIR"

missing_baseline() {
    # $1 = 套件名，$2 = 固化命令
    if [ "${CI_GATE_ALLOW_MISSING_BASELINE:-0}" = "1" ]; then
        echo "$1 无入仓基线（首次运行，CI_GATE_ALLOW_MISSING_BASELINE=1 显式放行）；固化：$2"
    else
        fail "$1 无入仓基线：没有基线就没有回归判定。固化：$2（首次接入可设 CI_GATE_ALLOW_MISSING_BASELINE=1）"
    fi
}

run_suite() {
    # 机制层 runner：exit 0 = 用例全过、无加载问题、至少 1 条用例；
    # 1 = 有用例失败；2 = 加载问题或空数据集；3 = 真实组件构建失败（见 runner 模块头）。
    local name="$1" jsonl="$2" rc=0
    local out="$LOG_DIR/$name.json"
    "$PY" -m water_agent.harness.runner "$jsonl" --report-dir "$GATE_REPORT_DIR" \
        > "$out" 2> "$LOG_DIR/$name.err" || rc=$?
    if [ "$rc" -ne 0 ]; then
        tail -60 "$out"
        tail -20 "$LOG_DIR/$name.err"
        fail "$name runner exit=$rc（0=全过；1=有用例失败；2=加载问题或空数据集；3=真实组件失败）"
        return 0
    fi
    "$PY" -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); print("%s: %d/%d 通过" % (sys.argv[2], d["passed"], d["total"]))' \
        "$out" "$name" || fail "$name runner 输出不是合法 JSON"
}

step "pytest（全量离线单测）"
# -rfE：失败/错误用例的摘要行；--tb=short：失败时打印精简 traceback。
# 此前只 tail -20，traceback 全被截掉——GitHub 上首次运行出现一条本机复现不了的
# 失败，日志里只剩一行 "AssertionError:"，无从判断原因。失败时打印 FAILURES 小节。
if ! "$VENV/bin/pytest" -q -rfE --tb=short > "$LOG_DIR/pytest.log" 2>&1; then
    awk '/^=+ (FAILURES|ERRORS) =+$/{show=1} show' "$LOG_DIR/pytest.log" | head -400
    tail -30 "$LOG_DIR/pytest.log"
    fail "pytest"
fi

step "ruff check src"
if ! "$VENV/bin/ruff" check src; then fail "ruff"; fi

step "mypy src/water_agent（Step 10 要求清零）"
if ! "$VENV/bin/mypy" src/water_agent > "$LOG_DIR/mypy.log" 2>&1; then
    tail -20 "$LOG_DIR/mypy.log"
    fail "mypy"
fi

# 陷阱用例 rag-p0-0011 按"校验器点名拒绝了伪造引用"判通过（expected_rejected_citations），
# 校验器失效时陷阱用例变红——正确实现下 12/12。
step "rag_p0 机制层 harness（期望 12/12，含 1 条 bad_citation 陷阱）"
run_suite rag_p0 data/eval/rag_p0.jsonl

step "calc_p0 harness（期望 28/28：计算子图与 run_calc 分发器两层）"
run_suite calc_p0 data/eval/calc_p0.jsonl

step "docgen_p0 harness（P2 文档生成，期望 11/11：结构化映射 + 渲染正文）"
run_suite docgen_p0 data/eval/docgen_p0.jsonl

step "material_p0 harness（P2 材料统计，期望 10/10）"
run_suite material_p0 data/eval/material_p0.jsonl

step "governance_p0 harness（P2.5 治理，期望 32/32）"
run_suite governance_p0 data/eval/governance_p0.jsonl

# agentic_p0：生产 AgenticSession 装配（真实 ToolRegistry / cite 可追溯检查 / 引用校验回流 /
# 生产 checkpoint serde）+ 逐步脚本模型 + fixture 语料；多轮每轮新 session、同一 thread，
# 与 Web RunManager 同构。此前 PR 门禁对生产 Web 走的 agentic 图没有 harness 级覆盖。
step "agentic_p0 harness（mock-agentic，生产装配 + 脚本模型，期望 21/21）"
run_suite agentic_p0 data/eval/agentic_p0.jsonl

# tool_exposure_p0（P7-S4）：按意图最小暴露。生产 AgenticSession + 记录型脚本模型，判定模型
# 每轮实际收到的工具与指引：该开的开了、写进了指引、没超 4500、无关工具没开、未开放的调用照常
# 执行并记 trace。⚠️ 合成用例、规则与标签同一作者——不证明真实模型的工具选择准确率（P7-S4-A13）。
step "tool_exposure_p0 按意图暴露机制层（36 条，硬指标全达标）"
TX_HEAD="data/eval/reports/tool_exposure_p0_head.json"
TX_MAIN="data/eval/tool_exposure_p0_main.json"
if ! $PY -m water_agent.harness.tool_exposure_eval data/eval/tool_exposure_p0.jsonl --report-path "$TX_HEAD" > "$LOG_DIR/tool_exposure.log" 2>&1; then
    tail -30 "$LOG_DIR/tool_exposure.log"
    fail "tool_exposure_p0 runner"
else
    head -8 "$LOG_DIR/tool_exposure.log"
    if [ -f "$TX_MAIN" ]; then
        if ! $PY scripts/compare_tool_exposure_p0.py --head "$TX_HEAD" --base "$TX_MAIN" > "$LOG_DIR/tool_exposure_cmp.log" 2>&1; then
            cat "$LOG_DIR/tool_exposure_cmp.log"
            fail "tool_exposure_p0 基线对比"
        else
            head -4 "$LOG_DIR/tool_exposure_cmp.log"
        fi
    else
        missing_baseline "tool_exposure_p0" "cp $TX_HEAD $TX_MAIN"
    fi
fi

# skill_p0（P7-S9）：Skill 库机制层。生产 AgenticSession + 记录型脚本模型，判定目录与暴露、
# 正文注入与来源、停用 / 不安全 / 遮蔽 / 删除不注入、加载规程后数值仍只来自计算工具且引用仍过校验器。
# ⚠️ 脚本模型——不证明真实模型会在该加载时加载、加载后会照做（P7-S9-A13）。
step "skill_p0 Skill 库机制层（26 条，硬指标全达标）"
SK_HEAD="data/eval/reports/skill_p0_head.json"
SK_MAIN="data/eval/skill_p0_main.json"
if ! $PY -m water_agent.harness.skill_eval data/eval/skill_p0.jsonl --report-path "$SK_HEAD" > "$LOG_DIR/skill.log" 2>&1; then
    tail -30 "$LOG_DIR/skill.log"
    fail "skill_p0 runner"
else
    head -4 "$LOG_DIR/skill.log"
    if [ -f "$SK_MAIN" ]; then
        if ! $PY scripts/compare_skill_p0.py --head "$SK_HEAD" --base "$SK_MAIN" > "$LOG_DIR/skill_cmp.log" 2>&1; then
            cat "$LOG_DIR/skill_cmp.log"
            fail "skill_p0 基线对比"
        else
            head -4 "$LOG_DIR/skill_cmp.log"
        fi
    else
        missing_baseline "skill_p0" "cp $SK_HEAD $SK_MAIN"
    fi
fi

# mcp_http_p0（P7-S8）：MCP Streamable HTTP 与 OAuth 2.1 机制层。会话 / Web 连接池 / runner 三条入口经
# **本地假 HTTP MCP server + 假授权服务器**（自签 TLS，子进程），判定暴露与跳过码、调用结果、应拦的目标零字节
# 发出、令牌零泄漏、工具调用零重放、每个出站请求都有 ProviderGateway 账。
# ⚠️ 没有真实 HTTP MCP server、真实授权服务器、企业代理 / 证书链，也没有浏览器里的授权跳转（P7-S8-A14 / A15）。
step "mcp_http_p0 MCP Streamable HTTP 与 OAuth 机制层（44 条，硬指标全达标）"
MH_HEAD="data/eval/reports/mcp_http_p0_head.json"
MH_MAIN="data/eval/mcp_http_p0_main.json"
if ! $PY -m water_agent.harness.mcp_http_eval data/eval/mcp_http_p0.jsonl --report-path "$MH_HEAD" > "$LOG_DIR/mcp_http.log" 2>&1; then
    tail -30 "$LOG_DIR/mcp_http.log"
    fail "mcp_http_p0 runner"
else
    head -4 "$LOG_DIR/mcp_http.log"
    if [ -f "$MH_MAIN" ]; then
        if ! $PY scripts/compare_mcp_http_p0.py --head "$MH_HEAD" --base "$MH_MAIN" > "$LOG_DIR/mcp_http_cmp.log" 2>&1; then
            cat "$LOG_DIR/mcp_http_cmp.log"
            fail "mcp_http_p0 基线对比"
        else
            head -4 "$LOG_DIR/mcp_http_cmp.log"
        fi
    else
        missing_baseline "mcp_http_p0" "cp $MH_HEAD $MH_MAIN"
    fi
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
if ! $PY -m water_agent.harness.drawing_eval tests/eval/drawing_p0 --report-path "$DRAWING_HEAD" > "$LOG_DIR/drawing.log" 2>&1; then
    tail -20 "$LOG_DIR/drawing.log"
    fail "drawing_p0 runner"
else
    tail -14 "$LOG_DIR/drawing.log"
    if [ -f "$DRAWING_MAIN" ]; then
        if ! $PY scripts/compare_drawing_p0.py --head "$DRAWING_HEAD" --base "$DRAWING_MAIN" > "$LOG_DIR/drawing_cmp.log" 2>&1; then
            cat "$LOG_DIR/drawing_cmp.log"
            fail "drawing_p0 基线对比"
        else
            cat "$LOG_DIR/drawing_cmp.log"
        fi
    else
        missing_baseline "drawing_p0" "见 scripts/compare_drawing_p0.py 文档头"
    fi
fi

# drawing_gen_p0（P6-S1）：参数化给水出图机制层。每条合法用例真跑一遍出图服务，
# 独立读回判分（图层 / 管段数 / 全部管径读回 / 每层一根立管 / S50 复审如实）。
# ⚠️ real_cad_verified=false：证明的是"本仓库读取器与审查器能正确读懂生成图"，
# 不是"可在 AutoCAD/天正中编辑"。DWG 状态依赖本机 LibreDWG，只作观测不参与判定。
step "drawing_gen_p0 参数化出图机制层（三类建筑 × 3 合法 + 5 非法）"
GEN_HEAD="data/eval/reports/drawing_gen_p0_head.json"
GEN_MAIN="data/eval/drawing_gen_p0_main.json"
if ! $PY -m water_agent.harness.drawing_gen_eval tests/eval/drawing_gen_p0 --report-path "$GEN_HEAD" > "$LOG_DIR/drawing_gen.log" 2>&1; then
    tail -30 "$LOG_DIR/drawing_gen.log"
    fail "drawing_gen_p0 runner"
else
    grep -E '"(total|passed|failed|dn_match_rate|readback_rate|review_honesty_rate)"' "$LOG_DIR/drawing_gen.log" || true
    if [ -f "$GEN_MAIN" ]; then
        if ! $PY scripts/compare_drawing_gen_p0.py --head "$GEN_HEAD" --base "$GEN_MAIN" > "$LOG_DIR/drawing_gen_cmp.log" 2>&1; then
            cat "$LOG_DIR/drawing_gen_cmp.log"
            fail "drawing_gen_p0 基线对比"
        else
            head -6 "$LOG_DIR/drawing_gen_cmp.log"
        fi
    else
        missing_baseline "drawing_gen_p0" "cp $GEN_HEAD $GEN_MAIN"
    fi
fi

step "multi_agent_p0 合成机制层（12 用例，8 硬指标全绿）"
MA_HEAD="data/eval/reports/multi_agent_p0_head.json"
MA_BASE="data/eval/multi_agent_p0_main.json"
if ! $PY -m water_agent.harness.multi_agent_eval tests/eval/multi_agent_p0 --out "$MA_HEAD" > "$LOG_DIR/multi.log" 2>&1; then
    tail -20 "$LOG_DIR/multi.log"
    fail "multi_agent_p0 harness"
else
    tail -8 "$LOG_DIR/multi.log"
    if [ -f "$MA_BASE" ]; then
        if ! $PY scripts/compare_multi_agent_p0.py --head "$MA_HEAD" --base "$MA_BASE" > "$LOG_DIR/multi_cmp.log" 2>&1; then
            cat "$LOG_DIR/multi_cmp.log"
            fail "multi_agent_p0 基线对比"
        else
            cat "$LOG_DIR/multi_cmp.log"
        fi
    else
        missing_baseline "multi_agent_p0" "cp $MA_HEAD $MA_BASE"
    fi
fi

step "基线对比（compare_baseline.py，阈值见 THRESHOLDS）"
if [ -f data/eval/baseline.json ]; then
    if ! $PY scripts/compare_baseline.py data/eval/baseline.json --report-dir "$GATE_REPORT_DIR" > "$LOG_DIR/baseline.log" 2>&1; then
        cat "$LOG_DIR/baseline.log"
        fail "compare_baseline"
    else
        cat "$LOG_DIR/baseline.log"
    fi
else
    missing_baseline "baseline.json" "$PY scripts/compare_baseline.py --init data/eval/baseline.json --report-dir $GATE_REPORT_DIR"
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
    > "$LOG_DIR/memory.log" 2>&1; then
    tail -40 "$LOG_DIR/memory.log"
    fail "memory_p0 harness"
else
    tail -20 "$LOG_DIR/memory.log"
    if [ -f "$MEMORY_BASE" ]; then
        if ! $PY scripts/compare_memory_p0.py --head "$MEMORY_HEAD" --base "$MEMORY_BASE" \
            > "$LOG_DIR/memory_cmp.log" 2>&1; then
            cat "$LOG_DIR/memory_cmp.log"
            fail "memory_p0 基线对比"
        else
            head -14 "$LOG_DIR/memory_cmp.log"
        fi
    else
        missing_baseline "memory_p0" "cp $MEMORY_HEAD $MEMORY_BASE"
    fi
fi

if [ "$FAILED" -ne 0 ]; then
    echo
    echo "CI 门禁失败：存在未通过项。"
    exit 1
fi

echo
echo "==> CI 门禁通过：pytest + ruff + mypy + rag_p0(12/12) + calc_p0(28/28) + docgen_p0(11/11) + material_p0(10/10) + governance_p0(32/32) + agentic_p0(21/21) + tool_exposure_p0(36/36) + skill_p0(26/26) + mcp_http_p0(44/44) + drawing_p0(合成 smoke 回归) + drawing_gen_p0(参数化出图) + multi_agent_p0 + memory_p0(§13.2 十二项硬指标) + 基线对比"
