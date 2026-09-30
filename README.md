# 给排水 Agent

面向给排水设计院的 AI Agent，覆盖 **规范问答、水力计算、计算书/设计说明生成、图纸审查、材料统计** 共五类能力。
当前已交付 **P1/Step 0–10 工程机制**、**P2 结构化底座**（文档模板、材料统计与导出、HITL、自检、专项评测）与 **P2.5 治理基础**（限流、预算、可靠性、护栏、trace、检测器、缓存）。**P3 图纸审查 DXF 解析已完成**（18/18 样本，0.4 秒平均延迟），**P4 召回率验收通过**（100% 召回率，2/2 缺陷检出），**P5 误报率验证未通过**（误报率 100%，需实施系统类型推断改进）。规格见 `docs/design/13-p3-drawing-review.md`、`docs/P4_COMPLETION_REPORT.md` 与 `docs/P5_COMPLETION_REPORT.md`。P2 的受限真实 LLM 润色已有 4 条样本证据；真实 RAG 质量、正式规范引用仍为 blocked，不能视为整体质量验收通过。

## 三条铁律

1. **计算类永远走程序化校验** —— 数值由真实水力计算工具算，LLM 只做参数理解和结果解释
2. **引用可追溯** —— 每个回答必须指回「规范号 + 条款号」，禁止编造条号
3. **评测先行** —— 每个阶段连同评测集一起交付，改动不得让关键指标下降

## 环境

- Python 3.12+（`requires-python >=3.11,<3.13`）
- `langgraph>=1.2.0,<2` / `triviumdb>=0.8.0` / `pydantic>=2.7` / `openpyxl>=3.1,<4`（P2 材料 Excel 导出）
- 安装：`uv sync` 或 `pip install -e .[dev]`

## 快速验证

```bash
# 1) 完整测试 + 静态检查
bash scripts/ci_gate.sh

# 2) 机制层 Harness 评测（离线、确定性，PR 门禁）
_venv/bin/python -m water_agent.harness.runner data/eval/rag_p0.jsonl          # 12/12
_venv/bin/python -m water_agent.harness.runner data/eval/calc_p0.jsonl          # 28/28
_venv/bin/python -m water_agent.harness.runner data/eval/docgen_p0.jsonl        # 11/11（P2 文档生成）
_venv/bin/python -m water_agent.harness.runner data/eval/material_p0.jsonl      # 10/10（P2 材料统计）
_venv/bin/python -m water_agent.harness.runner data/eval/governance_p0.jsonl    # 32/32（P2.5 治理）
_venv/bin/python -m water_agent.harness.runner data/eval/agentic_p0.jsonl      # 15/15（mock-agentic：生产 AgenticSession + 脚本模型）

# 3) P2 专项质量评测（不属于默认 baseline）
_venv/bin/python -m water_agent.harness.runner data/eval/docgen_quality.jsonl   # 离线 4/4
_venv/bin/python -m water_agent.harness.runner data/eval/material_quality.jsonl # 离线 7/7
_venv/bin/python -m water_agent.harness.runner data/eval/docgen_quality.jsonl --variant real
# 上一行只需 DEEPSEEK_API_KEY；无 Key 时 4 条结构化 SKIP。2026-09-04 证据为真实 LLM 4/4。

# 4) RAG 真实质量层（需 DEEPSEEK_API_KEY + DASHSCOPE_API_KEY）
#    默认走 agentic 图；--real-graph legacy 仅供改造前后对比
#    最新证据为机制 56/58、独立质量 36/58，退出码 1，当前 blocked
_venv/bin/python -m water_agent.harness.runner data/eval/rag_p1.jsonl --variant real --judge

# 5) CLI 离线自测：默认 auto（计算意图走 calc，其余走 legacy）
_venv/bin/python -m water_agent.cli --mock-generator "给水管道的设计秒流量如何确定？"
_venv/bin/python -m water_agent.cli --mode calc "DN150、Q=20L/s，求流速"

# 6) CLI 真实问答链路（需 DEEPSEEK_API_KEY + DASHSCOPE_API_KEY）
_venv/bin/python -m water_agent.cli --mode legacy "给水管道的设计秒流量如何确定？"
_venv/bin/python -m water_agent.cli --mode agentic "给水管道的设计秒流量如何确定？" --stream

# 7) 模型冒烟测试
bash scripts/smoke_agentic.sh

# 8) P5 长期记忆门禁（离线确定性；报告为可重建产物）
_venv/bin/python -m water_agent.harness.memory_eval data/eval/memory_p0.jsonl
_venv/bin/python scripts/compare_memory_p0.py --head data/eval/reports/memory_p0_head.json --base data/eval/memory_p0_main.json
# 发布门禁环境（CI）用 --release-gate：任何用例 SKIP 都算失败

# 9) P5 自托管部署（需 Docker；secrets/ 下先放 postgres_password 与 admin_password）
docker compose up -d
docker compose run --rm smoke          # 20 项 HTTP 检查，含记忆链路与越权拒绝
bash scripts/backup.sh --out backups/$(date -u +%Y%m%dT%H%M%SZ)
bash scripts/restore.sh --from <备份目录>   # 非空目标需显式 --force
bash scripts/check_image.sh water-agent-app:local
docker compose -f compose.yml -f compose.production.yml up -d   # 生产加固 overlay

# 10) 交互式多轮 / docgen HITL（EOF 挂起后按输出的 session_id 恢复）
_venv/bin/python -m water_agent.cli --interactive --mode agentic --mock-generator
_venv/bin/python -m water_agent.cli --flow docgen --doc-type calculation_book --mock-generator "生成 DN150 给水管道计算书"
_venv/bin/python -m water_agent.cli --flow docgen --doc-type design_note --mock-generator "某小区给水改造设计说明"
_venv/bin/python -m water_agent.cli --flow docgen --resume SESSION_ID --mock-generator
# 材料统计（Excel 导出见 water_agent.materials.export；agentic 会话内置 material_stats 工具）

# 11) P5 Step 46 可选本地模型（默认关闭；需先取得兼容性证据）
docker compose --profile local-model up -d local-model
docker compose exec local-model ollama pull MODEL_NAME
LOCAL_MODEL_ENABLED=true LOCAL_MODEL_BASE_URL=http://127.0.0.1:11434/v1 \
  _venv/bin/python -m water_agent.harness.local_model_compat \
  --out /path/visible-to-app/local-model-compatibility.json
# 只有 probe 的七项检查全为 passed 且 schema <4500 时，才通过部署覆盖文件将
# LOCAL_MODEL_ENABLED=true 与该报告路径传给 app；没有报告不会静默切换模型。
```

## 目录结构

```
src/water_agent/
  domain/         # models / state / citation / fixtures
  retrieval/      # base / in_memory / trivium (TriviumDB search_hybrid 适配器) / loader / rerank
  generation/     # base / mock / llm (DeepSeek) / chat_model (ChatOpenAI) / cost
  graphs/         # rag / calc / agent_rag (agentic 循环) / docgen (HITL) / chat
  prompting/      # assembler / runtime_context
  ingestion/      # chunker / pipeline / version_registry / quality / gold_set
  harness/        # loader / scorer / runner / doubles / retrieval_metrics / llm_judge / Step46 probe
  docgen/         # P2 文档模板 + 受限 LLM 润色 + 确定性质量自检
  materials/      # P2 材料统计：管段聚合（可追溯）+ .xlsx/CSV 导出
  memory/         # P5 长期记忆：原生 FTS + 可选 pgvector/MCP provider
  mcp/            # P4 网关 + P5 只读 Memory MCP adapter
  runtime/        # P2.5 治理：gateway / guardrails / cache / scenarios / detectors / observability
  observability/  # json_trace / otel (guarded OTel GenAI)
  cli.py          # 生产问答 CLI (--mode auto|calc|legacy|agentic, --flow qa|docgen, --doc-type, --interactive/--resume, --mock-generator, --stream)
  embedding.py    # 阿里云百炼 Embedding Provider
  session.py      # 多轮会话记忆 (checkpointer + sqlite)
  tools/          # hydraulics (水力计算工具族) / calc_velocity / material_stats 等工具规格
data/
  eval/           # 评测集 & 报告 & 基线
    rag_p0.jsonl          # P0 RAG 评测集（12 条有效 + 1 条非法）
    calc_p0.jsonl         # P0 计算评测集（28 条）
    docgen_p0.jsonl       # P2 文档生成机制层评测集（11 条，含 1 条非法输入）
    material_p0.jsonl     # P2 材料统计评测集（10 条：聚合一致/溯源，含 1 条非法输入）
    governance_p0.jsonl   # P2.5 治理评测集（32 条：限流/预算/可靠性/护栏/trace/检测器/缓存）
    docgen_quality.jsonl  # P2 受限 prose 润色质量集（4 条；支持 real 变体）
    material_quality.jsonl  # P2 材料鲁棒性质量集（7 条）
    rag_p1.jsonl          # P1 真实规范问答评测集（58 条，变体 real）
    baseline.json         # 五套机制层门禁基线（rag/calc/docgen/material/governance p0）
    reports/              # Harness 评测报告（JSON，被 .gitignore 忽略）
    traces/               # 过程 Trace（JSON，被 .gitignore 忽略）
    judge_cache/          # LLM-as-judge 结果缓存
  curated/clauses/  # 人工审校后的规范条款语料
  indexes/          # TriviumDB 向量索引（build_index.py 构建）
  fixtures/         # 测试用 fixture 条款
  knowledge/        # 原始知识库语料（40MB，221 个 md）
  versions.json     # 规范版本注册表
scripts/
  ci_gate.sh                  # PR 门禁（pytest + ruff + mypy + harness + 基线对比）
  compare_baseline.py          # 基线对比工具（阈值：通过率下降 ≤2pt，引用正确率下降 ≤3pt）
  build_index.py               # 构建 TriviumDB 索引
  smoke_agentic.sh             # 真实模型冒烟（3 问）
  run_real_qa.sh               # 真实问答验证
  verify.sh                    # 统一验证（旧版，由 ci_gate.sh 取代）
tests/              # 所有测试（pytest）
```

## 双轨评测

| 轨道 | 变体 | 检索器 | 生成器 | 依赖 | 用途 |
|------|------|--------|--------|------|------|
| 机制层 | `mock` / `programmatic` | fixture 或确定性引擎 | Mock/程序化工具 | 无 | PR 门禁，离线确定性 |
| docgen 质量层 | `programmatic` / `real` | 不依赖检索 | 脚本化润色 / DeepSeek | real 仅需 DEEPSEEK Key | 受限 prose 润色与守卫验证 |
| RAG 质量层 | `real` | TriviumDB + 真实 Embedding | DeepSeek + judge | DASHSCOPE + DEEPSEEK Key | 质量评估，人工/定时触发 |

**SKIP 语义**：无 Key 等前置条件缺失时输出结构化 `SKIP`（退出码 0，不参与基线比较）；已有 Key 但网络、权限或组件失败属于 blocked，退出码非 0。

## CI 门禁

`scripts/ci_gate.sh`（PR 门禁，只跑离线机制层）：

```bash
pytest → ruff → mypy → rag_p0 / calc_p0 / docgen_p0 / material_p0 / governance_p0 / agentic_p0（runner 须 exit 0）
      → drawing_p0 → drawing_gen_p0 → multi_agent_p0 → baseline 对比（含用例缺失检查）→ memory_p0
```

阈值集（`scripts/compare_baseline.py` 中 `THRESHOLDS`）：

| 指标 | 阈值（百分点） | 说明 |
|------|---------------|------|
| `pass_rate_decline_pt` | 2.0 | 通过率相对基线下降 ≤2pt |
| `citation_ok_decline_pt` | 3.0 | 引用正确率相对基线下降 ≤3pt |
| `quality_pass_rate_decline_pt` | 5.0 | 仅用于有独立真实质量基线且本次实际 judge 的 suite |
| `new_core_failures` | 0 | 无新增核心失败（PASS_TO_PASS 门禁） |

## 关键能力

- **规范问答 RAG**：TriviumDB 混合检索 + 父子块回填 + LocalReranker/BailianReranker + 引用校验 + 重试回流
- **Agentic RAG**：`--mode agentic` 由 `create_react_agent` 驱动的工具调用型多轮循环，引用声明工具 + 确定性校验
- **水力计算**：流速 / 沿程损失 / 管径 / 泵扬程 / 排水坡度等，全部程序化
- **文档生成（P2）**：计算书/设计说明结构化模板引擎（必需章节 + 数值/引用纪律，
  `validate_citation_policy` 防编造），docgen 图草稿/终稿同源渲染；受限 LLM 润色后再做结构、数值、引用确定性自检
- **材料统计（P2）**：管段表 → 材质×规格聚合（数量程序化、逐行来源可追溯）+ Excel(.xlsx)/CSV 导出
- **Human-in-the-Loop**：`--flow docgen` 文档生成审批节点（interrupt + Command resume，拒绝回流 ≤2 轮）
- **动态提示装配**：系统提示分段 + 运行时上下文投影 + 规范版本注册表
- **LLM-as-judge**：5 准则锚定刻度评分（忠实度/引用覆盖/完整性/清晰度/不编造），结果缓存防重复扣费
- **观测**：本地 JSON Trace + 可选 OTel GenAI 约定（guarded import）
- **自托管 Web + 长期记忆（P5）**：FastAPI + React 工作台，PostgreSQL 会话/迁移/checkpoint；
  账号与权限服务端校验（`Principal.authenticated`），SSE 可续传（`Last-Event-ID`）且逐事件复核授权；
  长期记忆按 user/project 硬隔离，写入需显式确认并通过 `MemoryWriteGuard`（凭据/PII/注入），
  召回结果以 `SESSION_MEMORY` 数据段有界注入（1200 字符），不能改变系统提示、工具集或能力授权
- **Step 46 可选增强**：默认仍使用 PostgreSQL FTS；pgvector 只作为显式注入的 provider，
  故障回退 FTS，forget 在同一事务内清理向量；第三方 Memory MCP 只提供受治理的 ID 排序提示，
  正文由原生 repository 再做权限过滤。`local-model` profile 默认关闭，当前本机没有本地 endpoint，
  因此尚未取得 compatible 证据，也不声称语义召回优于 FTS。

## 容量与基线

| 评测集 | 条数 | 通过率 | 说明 |
|--------|------|--------|------|
| rag_p0 | 12 | 12/12 (100%) | 含 1 条 bad_citation 陷阱：校验器点名拒绝伪造引用才算通过 |
| calc_p0 | 28 | 28/28 (100%) | 容差 rel=1e-4，4 条带越界断言 |
| docgen_p0 | 11 | 11/11 (100%) | P2：结构/数值/引用策略，含 1 条未知 doc_type 非法输入 |
| material_p0 | 10 | 10/10 (100%) | P2：聚合一致/全段溯源，含 1 条非法输入 |
| agentic_p0 | 15 | 15/15 (100%) | 生产 AgenticSession 装配 + 逐步脚本模型：引用回流、陷阱、多轮状态归零 |
| docgen_quality | 4 | 离线 4/4；real 4/4 | 2026-09-04 受限润色样本证据；不证明正式规范引用质量 |
| material_quality | 7 | 7/7 (100%) | 异常输入、round(3)、审计与溯源；xlsx 另由 pytest 覆盖 |
| memory_p0（P5 Step 45） | 42 | 41 通过 / 0 失败 / 1 SKIP | P5 长期记忆：§13.2 十二项硬指标全达标；**合成机制与隔离契约证据，非自然语言召回质量** |
| rag_p1 | 58 | 机制 56/58；质量 36/58 | 2026-09-04 real+judge，退出码 1，状态 blocked |

`data/eval/baseline.json` 于 2026-09-04 更新，只包含四套 PR 机制层 suite：`rag_p0`、`calc_p0`、`docgen_p0`、`material_p0`。`docgen_quality`、`material_quality` 与 `rag_p1` 不在默认 baseline；其中 `rag_p1` 需在真实质量达标后建立独立 baseline。

注意：baseline 中 `docgen_p0.citation_ok_rate=0.2727` 是 runner 的通用「引用是否属于检索上下文」指标；docgen 机制用例不建立 RAG context，因此它只作为固定回归快照，不能解释为引用策略仅通过 27.27%。docgen 的结构/数值/引用白名单验收体现在 11/11 的 suite pass rate。

## 已知约束

- 真实 RAG 质量层当前未达标：`rag_p1` 为机制 56/58、独立质量 36/58（62.07%），不能写成通过
- 真实检索矩阵虽已于 2026-09-04 重跑，但 70 条 query 存在 gold 泄漏；百炼 rerank 两组返回 403 `AccessDenied`，矩阵仍 blocked
- GB50015-2019 等现有语料含非正式摘要/要点，`data/versions.json` 四本规范状态仍为 unknown；正式引用需核对出版标准与版本来源
- P2 真实 docgen 4/4 只证明受限 prose 润色样本与确定性守卫；CLI docgen 的正式规范引用质量尚未验收
- 材料统计目前输入结构化管段表；从图纸自动抽取依赖 P3（2026-09-07 启动，Step 17 规格已交付，接缝是 `materials.PipeRun`）
- **P3 DWG 直读 blocked**：本机无 ODA File Converter / libredwg，且转换需 `EXECUTE_SHELL`（构造期即被拒绝的危险能力），故 P3 只支持 DXF，`.dwg` 恒返回结构化 `DRAWING_UNSUPPORTED_FORMAT`
- **P3 真实图纸未验证**：测试图纸由 ezdxf 程序化合成；真实设计院 DXF 含块参照/外部参照/非标图层，当前只证明解析管线与护栏正确
- 无 DEEPSEEK_API_KEY 时离线闭环（CLI `--mock-generator`；harness 默认机制层 variant）完全可用
- 语料扩容：`knowledge/` 下 221 个 md（40MB）已入库；`data/curated/clauses/` 含 1595 条审校条款；`data/indexes/` 向量库（21MB）可通过 `scripts/build_index.py` 从 curated 重建

### P5 自托管与长期记忆（Step 35–45）的口径

- **代码免费、远程模型不免费**：本项目代码可自托管且不收费，但真实问答/检索仍要调用 DeepSeek 与
  百炼，费用由使用者承担；未配置 Key 时 Web 端返回 424 `WEB_MODEL_NOT_CONFIGURED`，
  不假装成功（compose smoke 把这一条记为 note，而不是通过）。
- **`memory_p0` 是合成评测**：42 条离线确定性用例证明的是**隔离与治理机制**。
  `gold_exact_recall_rate = 1.00` 用合成关键词 gold，**不代表**任意中文表达的召回率；
  语义召回质量需在 Step 46 的独立无泄漏 gold 上另行验收。
- **护栏是确定性正则，不是分类器**：`blocked_injection_persisted_count = 0` 只说明
  "已拦下的变体没有落盘"，**必然存在未覆盖的改写变体**——不得声称能拦住任意注入。
- **真实记忆质量仍 blocked**：没有真实用户会话语料，也没有跨用户/跨项目的真实攻击样本；
  当前全部证据来自程序化合成。
- **单 worker 部署约束**：记忆候选表存于进程内（15 分钟 TTL，最多 256 条），设计面向单 worker 部署。多 worker 环境下候选不共享，需在负载均衡层启用会话亲和性（sticky session），或持久化候选表到 PostgreSQL（当前未实现）。
- **重新启用已过期记忆不受支持**：P0 状态机前向，`enabled: true` 返回 409，不静默复活。
- **本机 `docker compose build` 不可用**：Docker Desktop 的 buildx bake 通道报
  `x-docker-expose-session-sharedkey` gRPC 错误（与仓库无关）；等价命令
  `docker build -t water-agent-app:local .` 可用，`up/smoke/down` 与镜像检查均已实测通过。
