#!/usr/bin/env bash
# PR 门禁（Step 10 + P2 + P2.5，只跑离线机制层）。
#
# 顺序：pytest → ruff → mypy → 机制层 runner 六套（rag/calc/docgen/material/governance/agentic）
#      → tool_exposure_p0 → skill_p0 → mcp_http_p0 → model_choice_p0 → external_content_p0 → web_search_p0 → skill_fetch_p0 → mcp_registry_p0 → retrieval_colloquial_p0 → drawing_p0 → drawing_gen_p0
#      → multi_agent_p0 → 基线对比 → memory_p0。
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

# model_choice_p0（P7-S15）：模型目录与按会话 / 按消息选择模型。生产 Web 层（create_app + 真实鉴权 + 真实 RunManager）
# + 记录型会话工厂 + 本地假端点，判定选择校验（403 先于 424）、不接受 base_url / api_key、不悄悄换模型、缺省行为逐字不变、
# 逐对兼容性验证互不串、成本按模型归因、本地模型 / env 锁定下一切覆盖被拒、Key 零泄漏。
# ⚠️ 脚本替身——不证明任何真实模型的作答质量，更不证明"换模型后质量不变"（P7-S15-A13，无 Key 即 blocked）。
step "model_choice_p0 模型目录与选择机制层（50 条，硬指标全达标）"
MC_HEAD="data/eval/reports/ci_gate/model_choice_p0_head.json"
MC_MAIN="data/eval/model_choice_p0_main.json"
mkdir -p "$(dirname "$MC_HEAD")"
if ! $PY -m water_agent.harness.model_choice_eval data/eval/model_choice_p0.jsonl --report-path "$MC_HEAD" > "$LOG_DIR/model_choice.log" 2>&1; then
    tail -30 "$LOG_DIR/model_choice.log"
    fail "model_choice_p0 runner"
else
    head -4 "$LOG_DIR/model_choice.log"
    if [ -f "$MC_MAIN" ]; then
        if ! $PY scripts/compare_model_choice_p0.py --head "$MC_HEAD" --base "$MC_MAIN" > "$LOG_DIR/model_choice_cmp.log" 2>&1; then
            cat "$LOG_DIR/model_choice_cmp.log"
            fail "model_choice_p0 基线对比"
        else
            head -4 "$LOG_DIR/model_choice_cmp.log"
        fi
    else
        missing_baseline "model_choice_p0" "cp $MC_HEAD $MC_MAIN"
    fi
fi

# external_content_p0（P7-S11）：外部内容底座。敌意语料（64 条）逐条验证信封 / 归一化，受治理 GET 内核对进程内假对端验证
# 禁止目的地零字节、3xx 不跟随、超限中止、错误不回显；生产图 + 脚本模型 + 夹具 web 工具验证回合污点、披露行、缓存拒绝、
# 终答网址追溯与"缺省路径逐字不变"。
# ⚠️ 归一化是确定性正则，必有语料之外的写法——指标 1.0 不证明能拦住任意注入；也不证明任何真实搜索服务可用（S12 起才有 provider）。
step "external_content_p0 外部内容底座机制层（44 条 + 敌意语料 64 条，硬指标全达标）"
EC_HEAD="data/eval/reports/ci_gate/external_content_p0_head.json"
EC_MAIN="data/eval/external_content_p0_main.json"
mkdir -p "$(dirname "$EC_HEAD")"
if ! $PY -m water_agent.harness.external_content_eval data/eval/external_content_p0.jsonl --report-path "$EC_HEAD" > "$LOG_DIR/external_content.log" 2>&1; then
    tail -30 "$LOG_DIR/external_content.log"
    fail "external_content_p0 runner"
else
    head -4 "$LOG_DIR/external_content.log"
    if [ -f "$EC_MAIN" ]; then
        if ! $PY scripts/compare_external_content_p0.py --head "$EC_HEAD" --base "$EC_MAIN" > "$LOG_DIR/external_content_cmp.log" 2>&1; then
            cat "$LOG_DIR/external_content_cmp.log"
            fail "external_content_p0 基线对比"
        else
            head -4 "$LOG_DIR/external_content_cmp.log"
        fi
    else
        missing_baseline "external_content_p0" "cp $EC_HEAD $EC_MAIN"
    fi
fi

# web_search_p0（P7-S12）：联网搜索。生产 AgenticSession + 脚本模型 + 进程内假搜索服务（线程，不起子进程）验证两道门
# （部署开关 / 逐条消息勾选）、request 组暴露与让位告警、目的地零字节、查询出口守卫、结果流水线、回答纪律 R1–R5、
# 失败模板与审计字段白名单；Web 层用 SQLite + 真实鉴权。
# ⚠️ 只对本地假服务验证：不证明任何真实搜索服务可用；查询守卫是确定性正则 / 子串，只能拦住标注的子串与正则可识别的泄露。
step "web_search_p0 联网搜索机制层（79 条，硬指标全达标）"
WS_HEAD="data/eval/reports/ci_gate/web_search_p0_head.json"
WS_MAIN="data/eval/web_search_p0_main.json"
mkdir -p "$(dirname "$WS_HEAD")"
if ! $PY -m water_agent.harness.web_search_eval data/eval/web_search_p0.jsonl --report-path "$WS_HEAD" > "$LOG_DIR/web_search.log" 2>&1; then
    tail -30 "$LOG_DIR/web_search.log"
    fail "web_search_p0 runner"
else
    head -4 "$LOG_DIR/web_search.log"
    if [ -f "$WS_MAIN" ]; then
        if ! $PY scripts/compare_web_search_p0.py --head "$WS_HEAD" --base "$WS_MAIN" > "$LOG_DIR/web_search_cmp.log" 2>&1; then
            cat "$LOG_DIR/web_search_cmp.log"
            fail "web_search_p0 基线对比"
        else
            head -4 "$LOG_DIR/web_search_cmp.log"
        fi
    else
        missing_baseline "web_search_p0" "cp $WS_HEAD $WS_MAIN"
    fi
fi

# skill_fetch_p0（P7-S13）：Skill 下载。经真实 skills.cli.main（进程内，注入 TTY 替身 / 固定时钟 / 独立网关）对进程内线程假注册表
# 验证"取回 -> 隔离 -> 人工批准 -> 安装 -> 加载期哈希重算"整条链路：敌意包（Tag 走私 / 同形字 / CRLF / NUL / YAML 炸弹……）一律被拒且
# 磁盘零写入、非 TTY 与键入名字错误被拒、摘要三方一致、三个崩溃点无半安装、降级与重放被拒、模型 / 面板 / 前端没有取回 / 批准入口。
# ⚠️ 只对本地假注册表验证：协议是我们自己定义的最小静态协议（unverified_against_real_registry），不证明任何真实注册表可用；
# 字符白名单 / 正则安检是确定性规则，只覆盖评测语料里的变体，不得据此声称"能防住恶意 skill"；批准人只是 OS 用户名，人审是唯一信任根。
step "skill_fetch_p0 Skill 下载机制层（111 条，硬指标全达标）"
SF_HEAD="data/eval/reports/ci_gate/skill_fetch_p0_head.json"
SF_MAIN="data/eval/skill_fetch_p0_main.json"
mkdir -p "$(dirname "$SF_HEAD")"
if ! $PY -m water_agent.harness.skill_fetch_eval data/eval/skill_fetch_p0.jsonl --report-path "$SF_HEAD" > "$LOG_DIR/skill_fetch.log" 2>&1; then
    tail -30 "$LOG_DIR/skill_fetch.log"
    fail "skill_fetch_p0 runner"
else
    head -4 "$LOG_DIR/skill_fetch.log"
    if [ -f "$SF_MAIN" ]; then
        if ! $PY scripts/compare_skill_fetch_p0.py --head "$SF_HEAD" --base "$SF_MAIN" > "$LOG_DIR/skill_fetch_cmp.log" 2>&1; then
            cat "$LOG_DIR/skill_fetch_cmp.log"
            fail "skill_fetch_p0 基线对比"
        else
            head -4 "$LOG_DIR/skill_fetch_cmp.log"
        fi
    else
        missing_baseline "skill_fetch_p0" "cp $SF_HEAD $SF_MAIN"
    fi
fi

# mcp_registry_p0（P7-S14）：MCP 目录发现。生产 RegistryCatalogClient / build_proposal + 进程内假目录（线程，不起子进程）
# + 条目地址探针（只数连接与字节）+ 网络间谍（记录每一次 DNS 解析与 TCP 连接尝试）验证目录协议的整页严格校验、条目不可信处理、
# 提案恒停用且全拒、搜索与提案全程对条目地址零字节零 DNS；Web 层用 SQLite + 真实鉴权。
# ⚠️ 只对本地假目录验证：目录协议 v1 是本项目自定义的最小协议，不是官方 MCP 注册表的格式（Step 0 取得的真实响应形态完全不同），
# 不证明兼容任何真实目录；字符白名单 / 注入检测是确定性规则，不证明条目与真实 server 一致，也不证明"目录搜索是安全的"。
step "mcp_registry_p0 MCP 目录发现机制层（64 条，硬指标全达标）"
MR_HEAD="data/eval/reports/ci_gate/mcp_registry_p0_head.json"
MR_MAIN="data/eval/mcp_registry_p0_main.json"
mkdir -p "$(dirname "$MR_HEAD")"
if ! $PY -m water_agent.harness.mcp_registry_eval data/eval/mcp_registry_p0.jsonl --report-path "$MR_HEAD" > "$LOG_DIR/mcp_registry.log" 2>&1; then
    tail -30 "$LOG_DIR/mcp_registry.log"
    fail "mcp_registry_p0 runner"
else
    head -4 "$LOG_DIR/mcp_registry.log"
    if [ -f "$MR_MAIN" ]; then
        if ! $PY scripts/compare_mcp_registry_p0.py --head "$MR_HEAD" --base "$MR_MAIN" > "$LOG_DIR/mcp_registry_cmp.log" 2>&1; then
            cat "$LOG_DIR/mcp_registry_cmp.log"
            fail "mcp_registry_p0 基线对比"
        else
            head -4 "$LOG_DIR/mcp_registry_cmp.log"
        fi
    else
        missing_baseline "mcp_registry_p0" "cp $MR_HEAD $MR_MAIN"
    fi
fi

# retrieval_colloquial_p0（P7-S10）：口语化 / 同义改写问法上的关键词检索（text 模式，BM25，不调用任何
# embedding、不要求任何 Key）。从 data/curated 现建纯关键词索引，CI 干净克隆里也能跑；与入仓基线比
# Recall@1/3/5、MRR、PASS_TO_PASS 与用例消失。门禁只守"不变差"，不设绝对阈值：只配对话 Key 时的口语化召回
# 本来就低（见 docs/design/p7-step-10-decision.md）。hybrid 模式需要百炼 Key，不进门禁。
step "retrieval_colloquial_p0 关键词检索（54 条口语化问法，不降）"
RC_HEAD="data/eval/reports/retrieval_colloquial_p0_head.json"
RC_MAIN="data/eval/retrieval_colloquial_p0_main.json"
if ! $PY -m water_agent.harness.retrieval_mode_eval data/eval/retrieval_colloquial_p0.jsonl --mode text --report-path "$RC_HEAD" > "$LOG_DIR/retrieval_colloquial.log" 2>&1; then
    tail -30 "$LOG_DIR/retrieval_colloquial.log"
    fail "retrieval_colloquial_p0 runner"
else
    head -3 "$LOG_DIR/retrieval_colloquial.log"
    if [ -f "$RC_MAIN" ]; then
        if ! $PY scripts/compare_retrieval_colloquial_p0.py --head "$RC_HEAD" --base "$RC_MAIN" > "$LOG_DIR/retrieval_colloquial_cmp.log" 2>&1; then
            cat "$LOG_DIR/retrieval_colloquial_cmp.log"
            fail "retrieval_colloquial_p0 基线对比"
        else
            head -4 "$LOG_DIR/retrieval_colloquial_cmp.log"
        fi
    else
        missing_baseline "retrieval_colloquial_p0" "cp $RC_HEAD $RC_MAIN"
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
echo "==> CI 门禁通过：pytest + ruff + mypy + rag_p0(12/12) + calc_p0(28/28) + docgen_p0(11/11) + material_p0(10/10) + governance_p0(32/32) + agentic_p0(21/21) + tool_exposure_p0(36/36) + skill_p0(26/26) + mcp_http_p0(44/44) + model_choice_p0(50/50) + external_content_p0(44/44) + web_search_p0(79/79) + skill_fetch_p0(111/111) + mcp_registry_p0(64/64) + retrieval_colloquial_p0(text 模式不降) + drawing_p0(合成 smoke 回归) + drawing_gen_p0(参数化出图) + multi_agent_p0 + memory_p0(§13.2 十二项硬指标) + 基线对比"
