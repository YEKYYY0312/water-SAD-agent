# 给排水 Agent 改造任务书（v1.1 修订版）

> **修订说明：** 本版根据当前代码、数据资产、测试与 `docs/09-next-steps.md` 的实际状态修订。它是可执行计划，不把尚未验证的 API、指标或业务公式写成既定事实。
>
> **给 Cursor Agent 的执行说明：** 本任务书每步都是独立的最小闭环，按顺序执行，一步一提交。
> 每步开头必须**先朗读"使用技能"列出的文件**，严格按其中方法执行；完成时必须以**新鲜验证证据**（实际命令输出）作为完成门槛，禁止空口宣称完成。
> 执行前请先读：`~/Desktop/给排水agent/docs/design/00-overview-and-decisions.md`、`01-architecture.md`、`02-loop-engineering.md`、`03-harness-engineering.md` 与 `~/Desktop/给排水agent/docs/09-next-steps.md`。

**目标：** 把给排水 Agent 从"单次 RAG 流水线"改造为"工具调用型多轮 Agent"，同时把评测链与校验链接回"真实模型与真实检索"，使每一次改动都有可度量的闭环。

**架构：** 保留现有 LangGraph 图作为 legacy 路径与确定性子图（计算永远程序化）；新增"工具注册表 + agentic 循环（retrieve → cite → answer）+ 多轮会话记忆（checkpointer）+ 引用校验器 + 双轨评测（机制层 mock / 质量层真实）"。旧图与新循环用 `--mode` 特性开关共存，逐步切换。

**技术栈：** Python 3.12（`_venv`）｜langgraph 1.2.11（已装）｜langchain_core 1.6.0（已装）｜pydantic 2.13.4（已装）｜triviumdb 0.8.2（已装）｜langgraph-checkpoint-sqlite（Step 3 视可用性引入）｜langchain-openai（Step 7 引入）｜pytest

**规格来源：** `~/Desktop/给排水agent/docs/design/*.md`（9 份设计文档 + 09-next-steps.md 现状盘点）

---

## 目录

**第 0 阶段 现状止损与工程基线**
- Step -1 现状基线：测试、检索、数据质量与真实链路状态
- Step 0 版本控制、依赖锁与资产策略

**第 1 阶段 P1 核心业务闭环**
- Step 1 水力计算内核、计算工具族与程序化评测
- Step 2 水力计算 LangGraph 子图与意图接入

**第 2 阶段 会话与工具化改造**
- Step 3 多轮会话记忆：checkpointer + thread_id
- Step 4 工具抽象层：ToolSpec + ToolRegistry
- Step 5 引用声明工具与确定性校验器
- Step 6 Agentic RAG 外层循环与 ToolNode
- Step 7 真实模型接入：DeepSeek function calling + 流式

**第 3 阶段 生产化能力与质量收口**
- Step 8 系统提示装配 + 运行时上下文投影 + 规范版本注册表
- Step 9 Human-in-the-Loop：interrupt 审批节点
- Step 10 双轨评测深化、观测、CI 门禁与文档收口

**附录**：A 技能资产索引｜B 环境事实与已验证 API｜C 术语对照｜D 执行守则

---

## 全局约束（每步隐含包含，禁止违反）

1. **三条铁律**（来自 `docs/CURSOR_BUILD_BRIEF.md`）：① 计算类永远走程序化校验，LLM 不参与数值运算；② 引用可追溯，禁止编造条款；③ 评测先行，改动不得让关键指标下降。
2. **底线：全量测试不得回退**。每步开始前记录当前测试数量与通过状态；结束时要求 `pytest -q` 退出码为 0，且既有测试不得因本步改动而失败。
3. **TDD 纪律**：先写失败测试 → 确认失败 → 最小实现 → 确认通过 → 再扩展。禁止先写实现再补测试。
4. **一次一步**：每步只改一个逻辑面；未完成的功能用 `--mode` / 特性开关护住，不删旧代码。
5. **提交纪律**：每步一次 commit（`feat/test/fix/docs:` 前缀 + 一句 why）；禁止把两步并成一个 commit。
6. **回滚纪律**：每步必须独立可回滚（`git revert <该步 commit>` 即可回到上一步，数据类变更必须配套说明如何重建）。
7. **不伪造**：真实 API（DeepSeek / 百炼）无 Key 时，测试与闭环验证一律走 mock / fake；任何“已验证”结论必须附新鲜命令输出。
8. **环境事实**（以项目实际为准，若发生变化以命令输出为准）：`_venv` 可用；`langgraph==1.2.11`、`langchain_core==1.6.0`、`pydantic==2.13.4`、`triviumdb==0.8.2` 已装；`langgraph-checkpoint-sqlite` 与 `langchain-openai` 需要在对应步骤显式安装或说明跳过条件。
9. **已验证可用的关键 API**（写代码按实际安装版本验证结果为准）：
   - `from langgraph.graph import START, END, StateGraph`
   - `from langgraph.graph.message import add_messages`
   - `from langgraph.types import interrupt, Command`
   - `from langgraph.checkpoint.memory import MemorySaver`
   - `from langchain_core.tools import BaseTool, tool, StructuredTool`
   - `from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage`
   - 假模型：`from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel, GenericFakeChatModel`

### 本版执行覆盖规则

下方保留了原任务书的详细设计素材，但其中旧的 Step 编号仅作为主题参考。实际执行以本版目录为准：原 Step 6 拆为新 Step 1–2；原 Step 1 为新 Step 3；原 Step 2–5 依次为新 Step 4–7；原 Step 7–9 依次为新 Step 8–10。若详细块与本节冲突，以本版的前置条件、文件清单、接口契约和验收规则为准。

- 测试数量是参考值，不是验收条件；验收以退出码为 0、既有测试不回退和新增行为断言为准。
- 真实模型、百炼 embedding/rerank 属于可选质量链路；无 Key 时必须输出结构化 `SKIP`，不将 `SKIP` 当作通过或失败。
- 生产 CI 默认只跑离线机制层；真实质量层由人工或定时任务触发。
- 任何水力公式、规范有效性和工程阈值都必须有来源或人工确认，不能由文件名、年份或 LLM 自行推断。

---

# 第 0 阶段：现状止损与工程基线（Step -1–0）

---

## Step -1｜现状基线与止损（约 1–2 小时 ★★☆）

**本步目标：** 先把当前项目已经完成的工作变成可重复、可比较、可回归的证据，再开始架构改造。

**当前事实：** 项目已有 214 个测试、4 本精选规范、两套 TriviumDB 索引、13 条 `rag_p0` 评测集，但检索基线和真实问答链路仍需独立确认。不得把历史报告或任务书中的数字当作本次验证结果。

**做什么：**

- [ ] 运行 `_venv/bin/pytest -q`，记录退出码、通过数和失败数
- [ ] 运行 `rag_p0`，保存本次报告；确认 13 条中 12 条有效用例与 1 条异常覆盖用例的判分口径
- [ ] 为现有检索评测补充报告落盘（只改评测输出，不改判分口径）
- [ ] 对两套现有索引分别记录无 rerank / local rerank 的结果；百炼 rerank 无 Key 时标记 `SKIP`，不伪造结果
- [ ] 修复 `semantic_audit.py` 中已知的中文乱码，并只重跑受影响的质检报告
- [ ] 输出 `data/eval/baseline_preflight.json`，记录代码版本、数据 manifest、索引 manifest、测试与评测结果

**禁止：** 本步不引入 Agent 循环、不改 legacy RAG 判分逻辑、不创建规范版本结论、不把真实 API 失败写成测试失败。

**验收：** 所有离线命令有新鲜输出；基线文件可读取；真实服务缺 Key 时明确为 `SKIP`；当前 legacy 路径行为未改变。

**提交：** `docs: 建立现状基线与止损记录`

---

## Step 0｜工程基建：git + 锁 + 静态检查基线（约 30 分钟 ★☆☆）

**📍 本步解决什么：** 项目无版本历史，"评测先行、防退化"无从谈起；无 lint/类型检查，改动质量无法门禁；依赖未锁定。

**🎓 学习目标：** 建立可回滚、可对比、可审查的工程基线；理解"没有 git 就没有回归对比"。

**前置条件：** 无（从零开始）。

**做什么：**

- [ ] 在项目根执行 `git init`，先提交 `.gitignore`、`AGENTS.md`、`pyproject.toml` 与必要的说明文档
- [ ] 明确资产策略：源码、设计文档、评测集、精选语料与 manifest 入库；TriviumDB `.db/.vec/.wal/.lock` 默认不入库，保留可重建说明
- [ ] 用 `uv lock` 生成依赖锁文件 `uv.lock`；若当前环境无 `uv` 或锁生成失败，记录原因并作为后续环境任务处理
- [ ] `pyproject.toml` 增加 `[tool.ruff]`（line-length 100、target-version py312）与 `[tool.mypy]`（python_version 3.12、check_untyped_defs、no_implicit_optional），并增加开发依赖组；如项目已采用不同打包方式，以现状为准补齐
- [ ] 在项目根创建 `AGENTS.md`（≤80 行）：指向本任务书、三条铁律、验证命令（`pytest -q` / harness 命令）、改动范围纪律——为 Cursor 每次会话提供路由与边界
- [ ] 首次 commit 只提交已确认的可重建资产；大体量索引与中间产物仅在“可重建说明”明确后再决定是否入库或使用 LFS
- [ ] 全量回归 `pytest -q` 确认全绿，并记录本步前后的测试状态变化

**怎么做：** 关键 `.gitignore` 内容（追加/修改现有）：

```gitignore
_venv/
.uv-cache/
.pytest_cache/
__pycache__/
*.pyc
.DS_Store
data/traces/
data/eval/traces/
data/eval/reports/
data/sessions/
:memory:.lock
:memory:.wal
knowledge的替身*
*.zip
```

（注意：`data/fixtures/`、`data/eval/*.jsonl`（评测集）、`data/curated/manifest.json`、`data/indexes/*.manifest.json` 属**必须入库**的资产，不要忽略。）

**动哪些文件：**

- Modify：`~/Desktop/给排水agent/.gitignore`、`~/Desktop/给排水agent/pyproject.toml`
- Create：`~/Desktop/给排水agent/uv.lock`、`~/Desktop/给排水agent/.editorconfig`（可选）、`~/Desktop/给排水agent/AGENTS.md`
- 其他：`git init`、`git add`、首 commit

**接口契约：**

- Consumes：无
- Produces：`uv.lock`（后续所有步骤安装依赖以它为准）；静态检查命令 `ruff check src`、`mypy src/water_agent`（初版允许 mypy 存在告警，但 **Step 10 必须清零**）

**🧭 使用技能（先读后做）：**

- `~/Desktop/obsidian knowledge/AI应用开发者精选 Skills/Skills/git-workflow-and-versioning.md`——提交规范、原子提交、pre-commit 检查
- `~/Desktop/obsidian knowledge/AI应用开发者精选 Skills/Skills/verification-before-completion.md`——完成门槛必须附新鲜命令输出
- `~/Desktop/obsidian knowledge/AI应用开发者精选 Skills/Skills/ci-cd-and-automation.md`——门禁管线（lint→类型→单测）后续 CI 化
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/03 Agent Engineering/Skills/skill-minimal-workbench.md`——极简工作台（AGENTS.md 路由 + 持久状态 + 任务板），按它的"AGENTS.md≤80 行/指向验证命令"约束写本步的 AGENTS.md

**✅ 最小闭环验证：**

```bash
cd ~/Desktop/给排水agent
_venv/bin/pytest -q                       # 预期：退出码 0，与基线（Step -1 记录）一致或更高
git log --oneline                         # 预期：1 条 commit（"chore: 工程基线"）
ruff check src                            # 预期：无 error（warning 允许）
_venv/bin/python -m water_agent.harness.runner data/eval/rag_p0.jsonl   # 预期：与 Step -1 基线一致
```

**验收标准：** 以上 4 条命令实际输出与预期一致；`git status` 干净；`rag_p0`、测试与索引 manifest 的基线记录可追溯。

**自检问题：** ① 为什么"没有 git"是评测工程的硬伤？② 数据资产与可再生成产物如何区分入库？③ 若 Step 0 之后想整体回滚，命令是什么？

**提交：** `chore: 工程基线（git/锁/静态检查）`　**回滚：** `git reset --hard HEAD~1`（首步，无风险）

---

# 第 1 阶段：P1 核心业务闭环（Step 1–2）

---

## Step 1｜水力计算内核与程序化评测（P1 核心，约 1 天 ★★★）

**📍 本步解决什么：** P1 验收项"水力计算"完全缺失；计算是给排水 Agent 可靠性底线（铁律①），也是 function calling 最有价值的落点。

**🎓 学习目标：** "LLM 只做参数理解，计算全程序化"的标准实现；公式口径显式化（ADR-01）；计算类评测集与程序化判分（误差 ≤1%）；`missing_inputs/assumptions` 结构化状态。

**前置条件：** Step 0（不依赖会话与工具层，可先于 Agent 改造完成）。

**关键业务约束（本步开工前必须补齐，专业口径由人确认）：**

- 新增一份公式口径表（建议 `docs/design/09-hydraulics-formulas.md`），对每个计算项写清：适用场景、输入单位、输出单位、公式、系数来源、适用范围、默认值是否允许、缺参策略、越界策略
- `calc_velocity`：明确给水与排水场景分别适用的流速约束；"0.6–1.5 m/s"只是给水常用区间之一，禁止作为全局硬编码
- `calc_head_loss`：`darcy_weisbach` 必须指定摩阻系数来源（或固定取值方式）；`hazen_williams` 必须写明适用单位制与 C 值来源；`shevelev` 必须写明具体公式与适用管径范围
- `calc_slope` 的输入口径（充满度、流量、管径与坡度的关系）必须由专业口径表定义，禁止仅凭参数名推断
- 泵扬程计算（`calc_pump_head`）在 `09-next-steps.md` 中明确为 P1 能力之一；若口径表包含，则本步一并实现，否则在任务书与 README 中明确标注"未含"
- **公式由输入显式指定，禁止静默切换**；`HydraulicsFormula = Literal[...]` 只列出口径表中确认存在的公式

**做什么：**

- [ ] 产出并评审公式口径表（上述关键业务约束的落地产物，先于代码）
- [ ] `src/water_agent/tools/hydraulics.py`：按口径表实现计算工具，包括 `calc_velocity`（复用 Step 4 注册表）、`calc_head_loss`、`calc_diameter`、`calc_slope`、按需 `calc_pump_head`——所有公式由输入显式指定；单位/范围校验
- [ ] `data/eval/calc_p0.jsonl`：**≥20 条** gold（每条显式 `formula` + `input` + `gold` + `tolerances: {relative: 0.01}`；覆盖缺参/越界/单位错误陷阱用例；每条 gold 的数值来自口径表或人工核验的参考计算，**不允许只和实现同源生成**）
- [ ] harness 扩展：`domain/models.py` 新增 `CalcCase`（extra="forbid"）；`harness/loader.py` 按 `type` 路由；`harness/scorer.py` 新增数值比对（相对误差+绝对误差双阈值）；`harness/runner.py` 支持 `type=="calc"` 变体 `variant=="programmatic"`（计算图走真工具、生成用 mock，保证判分确定性）
- [ ] 测试：`tests/test_hydraulics.py`（每个公式 ≥2 组独立来源的已知答案，误差断言）、`tests/test_calc_harness.py`（5 条 mini 评测集跑通）

**怎么做：** 关键接口：

```python
# data/eval/calc_p0.jsonl 每条（与 03-harness-engineering.md §3 格式一致）
{"id": "calc-0001", "type": "calc", "variant": "programmatic",
 "query": "DN150、Q=20L/s，求流速", "formula": "velocity",
 "input": {"diameter_mm": 150, "flow_lps": 20}, "gold": {"velocity_mps": 1.132},
 "tolerances": {"relative": 0.01, "absolute": 0.001}}

# scorer 数值比对（示意）
def score_calc(case: CalcCase, actual: dict, gold: dict, tol) -> bool:
    for k, gv in gold.items():
        av = actual.get(k)
        if av is None or abs(av - gv) > max(tol.absolute, tol.relative * abs(gv)):
            return False
    return True
```

**动哪些文件：**

- Create：`~/Desktop/给排水agent/docs/design/09-hydraulics-formulas.md`、`~/Desktop/给排水agent/src/water_agent/tools/hydraulics.py`、`~/Desktop/给排水agent/data/eval/calc_p0.jsonl`、`tests/test_hydraulics.py`、`tests/test_calc_harness.py`
- Modify：`~/Desktop/给排水agent/src/water_agent/domain/models.py`（CalcCase/CalcGold/CalcTolerances）、`~/Desktop/给排水agent/src/water_agent/harness/loader.py`、`harness/scorer.py`、`harness/runner.py`（type 路由与变体）

**接口契约：**

- Consumes：`ToolRegistry`（Step 4，纯函数可先在注册表外独立实现，再于 Step 4 后注册）
- Produces：计算工具族（每个工具的函数签名与口径表一致）；`CalcCase` JSON 格式；harness `type=="calc"` 跑分能力

**🧭 使用技能（先读后做）：**

- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-function-calling-patterns.md`——**enum 约束公式**（防模型改公式口径）、重试策略
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-eval-patterns.md` + `Prompts/prompt-eval-designer.md`——为计算判分设计"口径固定 + 双阈值"评测集
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/03 Agent Engineering/Skills/skill-hybrid-planner.md`——确定性子图（计算）与 LLM 编排边界
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Prompts/prompt-rag-architect.md`（参考混合流程）
- 设计约束：`~/Desktop/给排水agent/docs/design/00-overview-and-decisions.md` ADR-01（公式口径显式指定，禁止静默切换）

**✅ 最小闭环验证：**

```bash
_venv/bin/pytest tests/test_hydraulics.py tests/test_calc_harness.py -v   # 预期：全绿
_venv/bin/python -m water_agent.harness.runner data/eval/calc_p0.jsonl
# 预期：通过率 ≥95%（20 条中 ≤1 条失败），报告含数值误差；exit 0
_venv/bin/pytest -q                                          # 预期：退出码 0，既有用例不回退
```

**验收标准：** 公式口径表先于代码落地；每条 gold 的 `formula` 与计算结果对得上，且 gold 数值有独立来源（口径表/人工核验）；缺参用例返回结构化 `missing_inputs`（而不是瞎猜）；判分与基线记录（`data/eval/reports/`）可追溯。

**自检问题：** ① 为什么"公式由 LLM 选"是绝对禁止的？如何用 schema 约束？② 计算判分为什么用双阈值（相对+绝对）？③ 缺参时返回 missing_inputs 与"让模型猜"的差别是什么？④ 为什么 gold 数值不允许只与实现同源生成？

**提交：** `feat: 水力计算内核与程序化评测（P1 核心闭环）`　**回滚：** `git revert HEAD`（删除本次新增的 calc 评测集与模块即可）

---

## Step 2｜水力计算子图与意图接入（约半天 ★★☆）

**📍 本步解决什么：** Step 1 产出了纯计算工具与评测，但用户话术到结构化参数的转换、越界时的候选方案生成、以及"计算意图"的识别还未接进系统。

**🎓 学习目标：** 确定性子图（计算）与 LLM 编排的边界；`missing_inputs/assumptions` 结构化状态；意图路由的最小闭环。

**前置条件：** Step 1（工具已注册到 `ToolRegistry`）。

**做什么：**

- [ ] `src/water_agent/graphs/calc.py`：`parse_params_node`（LLM 读用户话 → 结构化参数，缺参输出 `missing_inputs` 清单）→ `compute_node`（程序化调用 Step 1 工具）→ `validate_node`（按口径表做物理/范围校验，越界标注 `assumptions` 并生成候选方案）→ `finish_node`
- [ ] 缺参/越界行为明确：`missing_inputs` 是"要求澄清"的结构化输出，**禁止让模型猜测参数**；`assumptions` 必须随结果一并返回并在回答中明示
- [ ] CLI/harness 接入：`--mode` 下识别计算意图并路由到 calc 子图；`harness/runner.py` 支持 `type=="calc"` 的 `variant=="programmatic"` 端到端跑分（计算图走真工具、生成用 mock，保证判分确定性）
- [ ] 测试：`tests/test_calc_graph.py`（缺参→missing_inputs；越界→assumptions；正常→succeeded）

**动哪些文件：**

- Create：`~/Desktop/给排水agent/src/water_agent/graphs/calc.py`、`tests/test_calc_graph.py`
- Modify：`~/Desktop/给排水agent/src/water_agent/cli.py`（意图路由，默认仍走 legacy）、`harness/runner.py`（calc 端到端变体）

**接口契约：**

- Consumes：Step 1 计算工具族、`ToolRegistry`（Step 4）
- Produces：`build_calc_graph() -> CompiledStateGraph`；CLI 计算意图路径；harness `type=="calc"` 端到端跑分能力

**🧭 使用技能（先读后做）：**

- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/03 Agent Engineering/Skills/skill-hybrid-planner.md`——确定性子图（计算）与 LLM 编排边界
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-rag-pipeline.md`——检索与参数理解的边界
- 设计约束：`docs/design/00-overview-and-decisions.md` ADR-01（公式口径显式指定，禁止静默切换）

**✅ 最小闭环验证：**

```bash
_venv/bin/pytest tests/test_calc_graph.py -v                        # 预期：全绿
_venv/bin/python -m water_agent.harness.runner data/eval/calc_p0.jsonl   # 预期：通过率 ≥95%，exit 0
_venv/bin/pytest -q                                                 # 预期：退出码 0，既有用例不回退
_venv/bin/python -m water_agent.cli --mock-generator "DN150、Q=20L/s，管道流速是多少？"
# 预期：走 calc 意图路径，返回 velocity_mps=1.132 与公式口径记录
```

**验收标准：** 缺参用例返回结构化 `missing_inputs`（而不是瞎猜）；越界返回 `assumptions` 与候选方案；计算路径的数值与 Step 1 评测一致；legacy 路径不受影响。

**自检问题：** ① 计算子图为什么必须是确定性节点，LLM 只负责参数解析与结果解释？② 缺参时"要求澄清"与"让模型猜"的工程差别是什么？③ 越界标注 `assumptions` 后，为什么必须在回答中明示而不是静默修正？

**提交：** `feat: 水力计算子图与意图接入`　**回滚：** `git revert HEAD`

---

# 第 2 阶段：会话与工具化改造（Step 3–7）

---

## Step 3｜多轮会话记忆：checkpointer + thread_id（约 2–3 小时 ★★☆）

**📍 本步解决什么：** `WaterAgentState.messages` 是死字段，`--interactive` 每问一句都是全新会话——不能追问、不能澄清，一个"问答助手"却没有对话。

**🎓 学习目标：** LangGraph 的 `add_messages` reducer 与 checkpointer；thread_id 会话隔离；"对话记忆"如何落到持久磁盘。

**前置条件：** Step 0 与 Step 2（如果先做水力计算，则至少要保证 CLI 与状态契约不冲突）。

**做什么：**

- [ ] `pyproject.toml` 增加依赖 `langgraph-checkpoint-sqlite`（以实际安装验证结果为准；若无该包或安装失败，则先使用 `MemorySaver` 完成本步并在记录中说明持久化缺口）
- [ ] 新建 `src/water_agent/graphs/chat.py`：`ChatState`（`messages: Annotated[list[BaseMessage], add_messages]` + `query/answer/citations/status`）；`build_chat_app()` 用 `StateGraph` 实现最小"echo/记忆"闭环——**本步不做 agentic**，只做消息记录与多轮可达
- [ ] `src/water_agent/session.py`：`new_session_id()` / `session_db_path()` / `save_session_meta()`；**会话存储统一为单库**：`data/sessions/sessions.sqlite`，用 `thread_id` 做会话隔离；本步实现时可先用 `MemorySaver`，磁盘化只在 sqlite 依赖可用时启用
- [ ] `cli.py --interactive`：改为单 `invoke` + `config={"configurable":{"thread_id": session_id}}` 多轮复用；启动时打印会话 ID；`--resume <thread_id>` 恢复历史会话
- [ ] 测试：`tests/test_chat_memory.py`（`MemorySaver` + `FakeMessagesListChatModel`，断言：① 第二次提问时 `messages` 含第一条记录 ② 不同 thread_id 互不串扰 ③ sqlite 可用时落盘后可恢复，不可用时该用例 `skip`）

**怎么做：** 核心接口示意（以已装 API 为准）：

```python
# src/water_agent/graphs/chat.py
from typing import Annotated, Any, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage

class ChatState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]  # 必须用 add_messages，否则第二次写入覆盖第一次
    query: str
    answer: str
    citations: list[dict[str, Any]]
    status: str

def answer_node(state: ChatState) -> dict:
    # 本步：调用现有 MockGenerator 语义（占位），把 answer 写回；真实 LLM 在 Step 7 接入
    return {"answer": f"已收到第 {len(state['messages'])}-1 条消息", "status": "succeeded"}
```

```python
# cli.py --interactive 关键改动（sqlite 与 MemorySaver 二选一，均为 config 注入）
# 单库：data/sessions/sessions.sqlite
config = {"configurable": {"thread_id": session_id}}
result = app.invoke({"messages": [HumanMessage(content=line)]}, config=config)
```

**动哪些文件：**

- Modify：`~/Desktop/给排水agent/pyproject.toml`（依赖）、`~/Desktop/给排水agent/src/water_agent/cli.py`
- Create：`~/Desktop/给排水agent/src/water_agent/graphs/chat.py`、`~/Desktop/给排水agent/src/water_agent/session.py`、`~/Desktop/给排水agent/tests/test_chat_memory.py`
- 不动的：`domain/state.py`、`graphs/rag.py`（legacy 图保持原样，Step 6 才接新循环）

**接口契约：**

- Consumes：无（独立新模块）
- Produces：`build_chat_app(*, checkpointer=None) -> CompiledStateGraph`；`session.new_session_id() -> str`；`cli.text`：`--session-dir`（默认 `data/sessions`，单库 `sessions.sqlite`）、`--resume <thread_id>`

**✅ 最小闭环验证：**

```bash
_venv/bin/pytest tests/test_chat_memory.py -v          # 预期：3 passed（sqlite 不可用时 1 条 skip）
_venv/bin/pytest -q                                    # 预期：全绿（新增用例通过，既有用例不回退）
_venv/bin/python -m water_agent.cli --interactive      # 输入两句，第二句断言能引用第一句；exit 退出
```

**验收标准：** 交互演示中第二次提问的 answer 能看到"第 N 条消息"计数递增（证明 messages 累积）；sqlite 依赖可用时，重启进程后 `--resume` 仍可续聊（证明磁盘持久化）；依赖不可用时明确记录为已知缺口，不阻塞本步完成。

**自检问题：** ① 为什么 messages 必须 `Annotated[list, add_messages]` 而不是普通 list？② thread_id 应当按"会话"还是按"用户+会话"设计，为什么？③ MemorySaver 与 sqlite 持久化各自适用何时？

**提交：** `feat: 多轮会话记忆（checkpointer + thread_id）`　**回滚：** `git revert HEAD`（仅新增文件与 cli.py 一处改动，回滚干净）

---

## Step 4｜工具抽象层：ToolSpec + ToolRegistry（约 3–4 小时 ★★☆）

**📍 本步解决什么：** 项目无工具层，retriever/generator 是图内函数注入；后续"计算、引用、文件读取、多轮澄清"全都需要统一的"模型可调用"边界。

**🎓 学习目标：** function calling 的工具设计四原则（一工具一动作、描述即提示词、enum 约束、必填才必填）；输入 schema 用 pydantic 建模；注册表式扩展点。

**前置条件：** Step 0 与 Step 3（本步纯 Python，可与会话记忆并行，但入库接口要在合并前对齐）。

**做什么：**

- [ ] `src/water_agent/tools/base.py`：`ToolSpec`（普通类或 `pydantic.BaseModel` + `arbitrary_types_allowed=True`，以实际 pydantic 版本验证为准）：`name`（snake_case 唯一）、`description`（写给模型看的一级提示词）、`args_model`（pydantic schema）、`execute(args) -> dict`、`max_retries=3`、`timeout_seconds=30`、`guard`（可选安全检查钩子）
- [ ] `src/water_agent/tools/registry.py`：`ToolRegistry`——`register(spec)`（重名/非法名报错）、`invoke(name, **args)`（先校验 schema 再执行；异常转成结构化 `{"error": true, "code": ..., "message": ..., "suggestions": [...]}` 返回，**绝不向循环抛裸异常**）、`list_tools() -> list[dict]`（供模型工具定义）
- [ ] **执行路径唯一化**：所有工具执行统一走 `ToolRegistry.invoke`；LangChain 适配器（Step 6 引入）必须代理到 Registry，不得复制校验与错误转换逻辑
- [ ] 首批两个工具：`calc_velocity(diameter_mm: float, flow_lps: float) -> {velocity_mps}`（程序化：v = Q/A，含单位与范围校验）；`retrieve_clauses(query: str, top_k: int = 5)`（包装现有 `Retriever.retrieve`，返回截断后的 JSON——**截断 10KB**，防上下文填充）
- [ ] 测试 `tests/test_tools.py`：schema 校验拒绝缺参/类型错；重名注册拒绝；execute 异常转结构化错误；`list_tools()` 字段完整

**怎么做：** 关键接口：

```python
# src/water_agent/tools/base.py
from pydantic import BaseModel

class ToolSpec(BaseModel):
    model_config = {"arbitrary_types_allowed": True}   # 以实际 pydantic 版本验证为准
    name: str                      # 正则 ^[a-z][a-z0-9_]{1,40}$
    description: str               # 写"做什么+返回什么+何时用"，像给初级工程师下指令
    args_model: type[BaseModel]    # pydantic 模型；枚举字段必须用 Literal/Enum 约束
    execute: Callable[[Any], dict] # 返回 JSON 可序列化 dict
    max_retries: int = 3
    timeout_seconds: float = 30.0
    guard: Callable[[Any], None] | None = None   # 可选：安全钩子（白名单/路径校验）
```

```python
# 示例工具（参数用 enum 防模型自己编枚举值 + 校验范围）
class VelocityArgs(BaseModel):
    diameter_mm: float = Field(gt=0, le=3000)
    flow_lps: float = Field(gt=0, le=5000)

def calc_velocity(args: VelocityArgs) -> dict:
    area = 3.1415926 * (args.diameter_mm / 1000 / 2) ** 2
    v = (args.flow_lps / 1000) / area                      # 程序化计算，LLM 永不参与
    return {"velocity_mps": round(v, 3)}
```

**动哪些文件：**

- Create：`~/Desktop/给排水agent/src/water_agent/tools/__init__.py`、`base.py`、`registry.py`、`calc_velocity.py`、`retrieve_clauses.py`
- Create：`~/Desktop/给排水agent/tests/test_tools.py`
- 不动的：现有 retrieval/generation 全部（只被包装，不改）

**接口契约：**

- Consumes：`water_agent.retrieval.base.Retriever.retrieve(query: str, top_k: int = 5) -> list[RetrievedClause]`（已存在）
- Produces：`ToolSpec` / `ToolRegistry.register` / `ToolRegistry.invoke(name, **args) -> dict` / `ToolRegistry.list_tools()`；Step 6 将把任意 ToolSpec 适配为 `langchain_core BaseTool`，且适配层只做参数与结果转换，不复制校验与错误语义

**🧭 使用技能（先读后做）：**

- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-function-calling-patterns.md`——工具设计四原则 + 结构化错误 + 重试策略 + **安全清单（白名单/参数校验/结果过滤/限流/日志/结果≤10KB）**，逐条对照本步实现
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Prompts/prompt-tool-designer.md`——用它为 `calc_velocity` 生成"标准工具定义草稿"，再对照 skill 检查
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/02 MCP Protocol/Skills/skill-tool-schema-linter.md`——**静态审计工具注册表**：名称 snake_case+动宾+命名空间前缀、描述 40-1024 字符含 "Use when/Do not for"、schema `additionalProperties:false`、超 3 个动作建议拆分；本步所有 ToolSpec 按此自检
- `~/Desktop/obsidian knowledge/AI应用开发者精选 Skills/Skills/api-and-interface-design.md`——契约先行、边界校验、错误语义统一

**✅ 最小闭环验证：**

```bash
_venv/bin/pytest tests/test_tools.py -v        # 预期：≥6 passed
_venv/bin/python -c "
from water_agent.tools.registry import ToolRegistry
r = ToolRegistry(); r.register(calc_velocity)
print(r.invoke('calc_velocity', diameter_mm=150, flow_lps=20))   # 预期 {'velocity_mps': 1.132}
print(r.invoke('calc_velocity', diameter_mm=-1, flow_lps=20))    # 预期结构化 {'error': True, ...}，不抛异常
"
_venv/bin/pytest -q                           # 预期：退出码 0，既有用例不回退
```

**验收标准：** 结构化错误含 code/suggestions；`list_tools()` 的 schema 是标准 JSON Schema（可被 DeepSeek/OpenAI 直接消费）；Registry 是唯一执行入口，LangChain 适配层不复制逻辑。

**自检问题：** ① 为什么工具异常要转成"结构化错误字符串"而不是抛给循环？（对照 function-calling-patterns 错误处理）② `retrieve_clauses` 结果截断的阈值为什么重要？③ enum 约束为什么能防幻觉参数？

**提交：** `feat: 工具抽象层（ToolSpec + Registry + 首批工具）`　**回滚：** `git revert HEAD`（纯新增）

---

## Step 5｜引用声明工具 cite + 确定性引用校验器（激活"防答非所引"，约 2–3 小时 ★★☆）

**📍 本步解决什么：** 现状 citations 由生成器程序化绑定 context 前 5 条，校验链在真实链路缺少"模型声明 → 程序化校验"的闭环；模型正文引用 A、引用列表标 B 的情况完全无法发现。

**🎓 学习目标：** 把引用从"系统代填"改为"模型声明 + 程序化校验"；校验失败的结构化反馈回流（observation 不吞错）；保持现有评测契约不破坏。

**前置条件：** Step 4。

**做什么：**

- [ ] 先定义标准引用声明模型 `CitationClaim`（`source_id` / `spec_version` / `declared_by` / `message_id`），再实现 `src/water_agent/tools/cite.py`：`cite_clauses(article_ids: list[str]) -> {"cited": [...], "registered": bool}`——模型用条款 id 源码（如 `GB50015-2019 5.4.2`）声明引用；**语义**：本工具只是模型声明载体，真正判分在校验器
- [ ] `src/water_agent/domain/citation.py`：`validate_citations(cited_ids, context_ids, *, allowed_beyond_context=False) -> CitationCheckResult{ok, invalid, missing_version, errors}`——纯函数、确定性；本步对 `missing_version` 仅做标记，不创建版本结论
- [ ] 把 `graphs/rag.py` 的 `citation_check_node` 改为调用 `validate_citations`（行为保持：仍用 source_id 集合比较；**本步不改评测契约**），并新增：invalid 时把"错误文本"作为结构化 observation 记录进 `errors`，供 Step 6 回流给模型
- [ ] 明确 legacy 路径与 agentic 路径的引用来源：legacy 继续用 `GeneratedAnswer.citations`；agentic 用 `cite_clauses` 声明写入 state，两套在 state 层统一为 `citations: list[CitationClaim]`，Step 6 消费同一结构
- [ ] 测试 `tests/test_citation_check.py`：引用 ∈ context 通过；引用 ∉ context 拦截；`validate_citations(["FAKE 0.0.0"], ["FIX 1.1.1"])` 返回 `ok=False` 且 `errors` 含可行动建议文本；版本未知仅标记不判废止

**怎么做：** 关键接口：

```python
# src/water_agent/domain/citation.py
@dataclass(frozen=True)
class CitationCheckResult:
    ok: bool
    invalid: list[str]                       # 不在本次检索结果的引用
    missing_version: list[str]               # 无法确认现行有效的引用
    errors: list[str]                        # 给模型看的可行动错误文本（"did you mean X?" 风格）

def validate_citations(cited_ids: Iterable[str], context_ids: Iterable[str],
                       version_registry=None) -> CitationCheckResult:
    ...  # 纯确定性；错误文本包含建议，供模型自纠
```

**动哪些文件：**

- Create：`~/Desktop/给排水agent/src/water_agent/tools/cite.py`、`~/Desktop/给排水agent/src/water_agent/domain/citation.py`、`~/Desktop/给排水agent/tests/test_citation_check.py`
- Modify：`~/Desktop/给排水agent/src/water_agent/graphs/rag.py`（citation_check_node 内部改用校验器，对外行为不变）
- 不动的：`harness/*`（scorer 判分口径不变）

**接口契约：**

- Consumes：`ToolSpec`（Step 4）
- Produces：`cite_clauses` 工具；`validate_citations(...) -> CitationCheckResult`；Step 6 的 agentic 循环直接消费

**🧭 使用技能（先读后做）：**

- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-function-calling-patterns.md`——错误处理（结构化错误 + 最多 3 次重试 + 每次错误都要让模型看到失败文本）
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-guardrail-patterns.md`——引用是"不可信输出"，校验层思想
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-eval-patterns.md` + `Prompts/prompt-eval-designer.md`——为"引用正确率"设计判分准则（先设计，Step 10 落地 judge）
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/03 Agent Engineering/Skills/skill-injection-defense.md`——来源标记（用户/工具/检索）+ 按来源信任等级放行：本步起所有检索文本与工具结果一律视作**不可信数据**，校验器即为该信任等级的 Validator

**✅ 最小闭环验证：**

```bash
_venv/bin/pytest tests/test_citation_check.py -v   # 预期：≥4 passed（含拦截∉context用例）
_venv/bin/pytest -q                                # 预期：退出码 0，既有用例不回退
_venv/bin/python -m water_agent.harness.runner data/eval/rag_p0.jsonl
# 预期：仍 11/12（评测契约未变）；bad_citation 变体仍被判失败（retry 逻辑健在）
```

**验收标准：** 新增一条单测：`validate_citations(["FAKE 0.0.0"], ["FIX 1.1.1"])` 返回 `ok=False` 且 `errors` 内包含可行动建议文本（而非空字符串）。

**自检问题：** ① 为什么"把引用交给模型声明"比"系统代填"更真实？代价是什么？② invalid 引用回流给模型时，为什么必须给完整错误文本？③ 本步为什么刻意不动 harness 判分口径？

**提交：** `feat: 引用声明工具与确定性校验器`　**回滚：** `git revert HEAD`

---

## Step 6｜Agentic RAG 外层循环与 ToolNode（约 4–6 小时 ★★★）

**📍 本步解决什么：** 现在是一条"注定单次"的直线图；改造为"模型驱动"的循环：模型决定 `retrieve → cite → answer` 的顺序与重试，工具结果作为下轮上下文，循环有硬上限。

**🎓 学习目标：** `create_react_agent` 作为 agent 子图；ToolSpec → BaseTool 适配（统一代理到 Registry）；外层图负责"最终引用校验 + 回流重试 + 预算收口"；停止条件与护栏；假模型驱动的端到端测试；特性开关共存旧图。

**关键架构决定：** `create_react_agent` 的标准行为是"模型 → 工具 → 模型……直到模型不再调用工具"，它**没有**"最终回答后再插入引用校验并回流"的阶段。因此引用校验不能依赖 `create_react_agent` 内置，而必须由外层图承担：

```text
START → agent_loop(create_react_agent 子图)
             ↓
        citation_validator
          ├─ 通过 → finalize（成功）
          ├─ 失败且有预算 → 注入带错误文本的 ToolMessage → 回 agent_loop
          └─ 失败且无预算 → finalize（失败，errors 可见）
END
```

**前置条件：** Step 3、4、5。

**做什么：**

- [ ] `src/water_agent/tools/ds_adapters.py`：`tool_spec_to_base_tool(spec) -> BaseTool`（用 `StructuredTool.from_function`，`args_schema=spec.args_model`，`description=spec.description`，**内部必须代理到 `ToolRegistry.invoke`**，不复制校验与错误转换）
- [ ] `src/water_agent/graphs/agent_rag.py`：`build_agentic_app(...)` 构建外层图——`agent_loop` 节点内 `create_react_agent(model=..., tools=[retrieve_clauses, cite_clauses, calc_velocity], state_schema=ChatState, checkpointer=...)`；`citation_validator` 节点调用 `validate_citations` 并把失败错误写成结构化 `ToolMessage` 回流
- [ ] 循环收口：外层图 `recursion_limit`≤20；引用校验失败最多回流重试 2 次；`agent_loop` 内模型工具调用轮数有独立预算；超限统一返回结构化错误而非挂死
- [ ] `cli.py` 增 `--mode legacy|agentic`（**默认 legacy**——特性开关，旧的跑通再生产）；agentic 模式的模型先由 CLI 注入，未接真实模型前用 fake 顶替（Step 7 接真实）
- [ ] `scripts/run_agentic_smoke.py`：假模型端到端脚本（`FakeMessagesListChatModel` 预置两轮：第一轮 `tool_calls=[retrieve_clauses]`，第二轮 final answer），断言消息轨迹
- [ ] 测试 `tests/test_agent_rag.py`：① 假模型触发 ToolNode 执行并回填 ToolMessage ② 引用校验失败时外层图注入回流消息、重试 ≤2 后正确收口 ③ 无 checkpointer 时拒绝构建（护住会话纪律）④ 预算超限返回结构化错误

**怎么做：** 关键片段：

```python
# 假模型驱动 agent 子图（离线、无 Key）
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

model = FakeMessagesListChatModel(responses=[
    AIMessage(content="", tool_calls=[{"name": "retrieve_clauses", "args": {"query": "设计秒流量", "top_k": 3}, "id": "c1"}]),
    AIMessage(content="根据 GB50015……（引用 FIX 1.1.1）"),
])
app = build_agentic_app(model=model, tools=[retrieve_clauses_tool, cite_clauses_tool],
                        checkpointer=MemorySaver())
out = app.invoke({"messages": [HumanMessage(content="设计秒流量如何确定？")]},
                 config={"configurable": {"thread_id": "t1"}})
assert any(m.type == "tool" for m in out["messages"])        # 工具被执行
assert out["messages"][-1].content                            # 最终回答
```

**动哪些文件：**

- Create：`~/Desktop/给排水agent/src/water_agent/tools/ds_adapters.py`、`~/Desktop/给排水agent/src/water_agent/graphs/agent_rag.py`、`~/Desktop/给排水agent/scripts/run_agentic_smoke.py`、`~/Desktop/给排水agent/tests/test_agent_rag.py`
- Modify：`~/Desktop/给排水agent/src/water_agent/cli.py`（`--mode`；`--mode agentic` 走新图，模型占位为 fake/真实由 Step 7 决定）
- 不动的：`graphs/rag.py`（legacy）、`harness/`（Step 10 才接真实链评测）

**接口契约：**

- Consumes：`ToolRegistry`（Step 4）、`validate_citations`（Step 5）、`ChatState`（Step 3）
- Produces：`build_agentic_app(model, tools, *, checkpointer, prompt=None) -> CompiledStateGraph`；`cli --mode`

**🧭 使用技能（先读后做）：**

- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/03 Agent Engineering/Skills/skill-agent-loop.md`——**核心**：停止条件（选一个主停止，其余是安全带）、轮数预算（问答类 10）、工具错误→observation 不得吞、不可信工具输出
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-stategraph-designer.md`——节点单职责、checkpointer 必配、interrupt 位置
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/03 Agent Engineering/Skills/{skill-runtime-shape, skill-actor-runtime}.md`——循环运行时形状与执行模型
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/02 MCP Protocol/Skills/skill-parallel-call-safety-check.md`——工具并行安全审计：`retrieve_clauses` 纯读可并行（parallel_safe），`calc_*` 依赖参数解析需标注顺序依赖（ordering_deps）；不得全标安全、写路径不并行
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/03 Agent Engineering/Skills/skill-scope-contract.md`——本步改动范围契约：只允许增 `tools/`、`graphs/agent_rag.py`、`cli.py` 中 `--mode` 分支；禁止触碰 harness 判分口径（越界即 CI 非零）
- `~/Desktop/obsidian knowledge/AI应用开发者精选 Skills/Skills/incremental-implementation.md`——特性开关（`--mode`）护住未完成功能

**✅ 最小闭环验证：**

```bash
_venv/bin/pytest tests/test_agent_rag.py -v          # 预期：≥4 passed（全程离线）
_venv/bin/pytest -q                                   # 预期：退出码 0，既有用例不回退
_venv/bin/python scripts/run_agentic_smoke.py         # 预期：打印消息轨迹，含 tool 消息 + 最终回答，exit 0
_venv/bin/python -m water_agent.cli --mode legacy "给水管道的设计秒流量如何确定" --mock-generator
# 预期：legacy 路径不受影响（回归证明）
```

**验收标准：** 假模型闭环展示"模型发起工具调用 → 工具执行 → 结果回填 → 模型继续"完整循环；引用校验失败时外层图注入带错误文本的 `ToolMessage` 回流，且重试有上限；`recursion_limit` 超限时返回结构化错误而非挂死。

**自检问题：** ① 循环的"主停止条件"是什么？还有哪几条安全带？② 为什么工具输出一律视为不可信输入？③ 为什么最终引用校验必须由外层图承担，而不能只依赖 `create_react_agent`？④ `--mode` 特性开关为什么放在 CLI 而不是删掉旧图？

**提交：** `feat: agentic RAG 外层循环（引用校验回流，--mode 双轨）`　**回滚：** `git revert HEAD`

---

## Step 7｜真实模型接入：DeepSeek function calling + 流式（约 3–4 小时 ★★☆）

**📍 本步解决什么：** 现有 `LLMGenerator` 是 urllib 一次性请求，不支持工具调用/流式；Agentic 循环需要真正的 function-calling 模型。

**🎓 学习目标：** OpenAI 兼容 function calling 的接入方式（DeepSeek 官方 `https://api.deepseek.com`）；`ChatOpenAI` 参数（base_url/model/api_key/temperature）；无 Key 时的可验证闭环（结构化跳过）。

**前置条件：** Step 6。

**做什么：**

- [ ] `pyproject.toml` 增加 `langchain-openai`（以实际安装验证为准）
- [ ] `src/water_agent/generation/chat_model.py`：`build_chat_model(*, model="MODEL_NAME", base_url="https://api.deepseek.com", api_key_env="DEEPSEEK_API_KEY", temperature=0.0) -> BaseChatModel`（api_key 缺失时 raise 明确错误）；**本步只实现 DeepSeek 的 OpenAI 兼容路径**，暂不引入多 provider 抽象
- [ ] `graphs/agent_rag.py`：model 参数由 CLI 注入；系统提示改用 `prompt=`（Step 8 再升级为可调用装配器）
- [ ] `cli.py`：`--model/--base-url/--api-key-env`（默认 `DEEPSEEK_API_KEY`）；`--mode agentic` 无 Key 时报错但仍输出结构化指引（复用现有错误风格：`{"ok": false, "error": "..."}`）
- [ ] 流式：CLI `--stream` 时用 `app.astream(..., stream_mode="messages")` 打印 token（可选增强，若复杂可降级到 Step 10）
- [ ] 测试 `tests/test_chat_model.py`：`build_chat_model` 无 key 抛错；参数透传（用 `FakeMessagesListChatModel` 顶替验证调用形状）；`scripts/smoke_agentic.sh`：检测 `DEEPSEEK_API_KEY`，无则打印 SKIP exit 0，有则连跑 3 问 exit 0/1

**怎么做：** 关键片段：

```python
# src/water_agent/generation/chat_model.py
from langchain_openai import ChatOpenAI

def build_chat_model(*, model="MODEL_NAME",
                     base_url="https://api.deepseek.com",
                     api_key_env="DEEPSEEK_API_KEY",
                     temperature: float = 0.0) -> BaseChatModel:
    key = os.environ.get(api_key_env, "")
    if not key.strip():
        raise ValueError(f"{api_key_env} must not be empty")   # 与现有失败风格一致：失败要响亮
    return ChatOpenAI(model=model, base_url=base_url, api_key=key, temperature=temperature)
```

**动哪些文件：**

- Modify：`~/Desktop/给排水agent/pyproject.toml`、`~/Desktop/给排水agent/src/water_agent/graphs/agent_rag.py`、`~/Desktop/给排水agent/src/water_agent/cli.py`
- Create：`~/Desktop/给排水agent/src/water_agent/generation/chat_model.py`、`~/Desktop/给排水agent/scripts/smoke_agentic.sh`、`~/Desktop/给排水agent/tests/test_chat_model.py`

**接口契约：**

- Consumes：Step 6 的 `build_agentic_app(model, ...)`
- Produces：`build_chat_model(...) -> BaseChatModel`；CLI `--model/--base-url/--api-key-env/--stream`

**🧭 使用技能（先读后做）：**

- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-function-calling-patterns.md`——Provider 差异（OpenAI 风格 `tools=[...]`、tool_choice 语义）、并行调用、tool_call id 回传
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-structured-outputs.md` / `Prompts/prompt-structured-extractor.md`——回答与引用结构化输出
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-rag-pipeline.md`——事实问答 temperature=0、检索参数（top_k 5-10）
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-production-checklist.md`——可观测/失败模式对照

**✅ 最小闭环验证：**

```bash
_venv/bin/pytest tests/test_chat_model.py -v            # 预期：≥2 passed
bash scripts/smoke_agentic.sh                           # 无 Key → 打印 SKIP 并 exit 0（这是合法验证！）
export DEEPSEEK_API_KEY=***  # 有 Key 时：
_venv/bin/python -m water_agent.cli --mode agentic "给水管道的设计秒流量如何确定"
# 预期：真实模型完成 retrieve→cite→answer，citations 全部 ∈ 检索结果；exit 0
_venv/bin/pytest -q                                      # 预期：退出码 0，既有用例不回退
```

**验收标准：** 无 Key 时全工程（含本步验证）依然可完整离线跑通（mock/fake 替代）；有 Key 时代理路径返回结构化"引用 ∈ context"的结果；`--mode legacy` 行为与 Step -1 基线一致。

**自检问题：** ① DeepSeek 官方接口与 Anthropic 的 tool 格式差异，接错了会怎样？（对照 skill 的 provider patterns）② 为什么 temperature=0 对规范问答是硬约束？③ 无 Key 为什么也算"验证证据"？

**提交：** `feat: 真实模型接入（function calling + 流式）`　**回滚：** `git revert HEAD`

---

# 第 3 阶段：生产化能力与质量收口（Step 8–10）

---

## Step 8｜系统提示装配 + 运行时上下文投影 + 规范版本注册表（约 3–4 小时 ★★☆）

**📍 本步解决什么：** 系统提示是死字符串；规范版本（现行/废止）无法验证；上下文无预算、无动态性——多轮后 token 迅速膨胀。

**🎓 学习目标：** 动态上下文装配（按需组装、段级预算、变化才注入）；版本注册表（设计文档明确要求）；Citation 带版本。

**前置条件：** Step 3、5、7（版本注册表与装配器可先于 agentic 完成，但接进 `graphs/agent_rag.py` 需在 Step 7 之后）。

**做什么：**

- [ ] `src/water_agent/ingestion/version_registry.py` + `data/versions.json`：`get_spec_status(spec_id) -> {status: current|superseded|deprecated|unknown, effective_date, successor, source}`——**只录入有来源或经人工确认的事实**；未知状态必须返回 `unknown`，禁止根据文件名年份或索引年份推断"现行/废止"
- [ ] 版本注册表的数据条目必须带 `source`（如"人工确认 2026-08"）与录入时间；`data/versions.json` 初始内容只包含当前项目实际拥有的四本规范中确有依据的条目，其余为 `unknown`
- [ ] `src/water_agent/prompting/assembler.py`：`assemble_prompt(state) -> SystemMessage`——分段：角色与铁律（静态）/ 版本注册表快照（动态）/ 公式口径（动态）/ 工具使用指南（随工具注册表生成）；每段带 token 预算（估算函数）
- [ ] `src/water_agent/prompting/runtime_context.py`：`RuntimeContextProjection`——渲染"当前 cwd/版本状态/时间/会话引用"快照，**与上次相同则跳过注入**（省 token），变化才作为一条用户消息注入
- [ ] `graphs/agent_rag.py`：`prompt=assemble_prompt`（callable）；`llm.py` legacy 路径的 system prompt 也换成装配器（行为兼容）
- [ ] `domain/models.py`：`Citation` 增加 `spec_version: str | None`；`validate_citations` 接入 `version_registry`（Step 5 的 `missing_version` 正式生效；未知版本只标记不判废止）
- [ ] 测试：`tests/test_assembler.py`（段级预算、无 Key 离线可跑）、`tests/test_runtime_context.py`（不变不注入/变化注入一次）、`tests/test_version_registry.py`（未知条目返回 `unknown`；仅录入事实可判定）

**怎么做：** 关键接口：

```python
# prompting/runtime_context.py —— 变化才注入（对照 DSH runtime-context projection）
class RuntimeContextProjection:
    def __init__(self, registry): self._last: str | None = None
    def project(self, current: str) -> str | None:
        if self._last == current: return None      # 无变化 → 不注入
        self._last = current
        return current
```

```python
# data/versions.json 条目形态（source 与 verified_at 必填，不推断）
{"spec_id": "GB50015", "version": "2019", "status": "current",
 "effective_date": "2020-03-01", "successor": null,
 "source": "人工确认", "verified_at": "2026-08-31"}
```

**动哪些文件：**

- Create：`~/Desktop/给排水agent/src/water_agent/prompting/__init__.py`、`assembler.py`、`runtime_context.py`、`~/Desktop/给排水agent/src/water_agent/ingestion/version_registry.py`、`~/Desktop/给排水agent/data/versions.json`、`tests/test_assembler.py`、`tests/test_runtime_context.py`、`tests/test_version_registry.py`
- Modify：`~/Desktop/给排水agent/src/water_agent/domain/models.py`（Citation.spec_version）、`~/Desktop/给排水agent/src/water_agent/domain/citation.py`（接版本注册表）、`~/Desktop/给排水agent/src/water_agent/graphs/agent_rag.py`、`llm.py`

**接口契约：**

- Consumes：Step 5 校验器、Step 7 模型注入点
- Produces：`assemble_prompt(state) -> SystemMessage`；`RuntimeContextProjection.project(current) -> str | None`；`version_registry.get_spec_status(spec_id)`

**🧭 使用技能（先读后做）：**

- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-context-engineering.md`——按查询动态组装、各组件预算、工具按意图裁剪（省 60-80% token）
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Prompts/prompt-context-optimizer.md`——审查上下文组装策略
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-prompt-caching-planner.md`——稳定/易变区切分（缓存友好：静态段在前）
- `~/Desktop/obsidian knowledge/AI应用开发者精选 Skills/Skills/context-engineering.md`——上下层级与选择性包含

**✅ 最小闭环验证：**

```bash
_venv/bin/pytest tests/test_assembler.py tests/test_runtime_context.py tests/test_version_registry.py -v  # 预期：全绿
_venv/bin/pytest -q                              # 预期：退出码 0，既有用例不回退
_venv/bin/python scripts/run_agentic_smoke.py    # 预期：假模型闭环消息里，上下文段仅在变化时出现一次
```

**验收标准：** 相同状态下连续两次 `project()` 第二次返回 None；版本注册表对未录入条目返回 `unknown`，对已录入事实（含来源）可判定状态；agentic 假模型轨迹中动态段只注入一次。

**自检问题：** ① "变化才注入"为什么能省 token 又不失真？② 版本注册表为什么必须"只录入已知事实"，且未知必须显式返回 `unknown`？③ 缓存友好的提示布局原则是什么（稳定区/易变区顺序）？

**提交：** `feat: 动态提示装配 + 运行时上下文投影 + 版本注册表`　**回滚：** `git revert HEAD`

---

## Step 9｜Human-in-the-Loop：interrupt 审批节点（约 2–3 小时 ★★☆）

**📍 本步解决什么：** `approval` 字段是死的；无暂停/恢复；正式文档/成品输出前没有人审（设计文档明确：生成正式文档前必须人工确认）。

**🎓 学习目标：** LangGraph `interrupt()` + `Command(resume=...)`；checkpointer 对中断/恢复的支持；interrupt 放置原则（副作用前，不是后）。

**前置条件：** Step 3、8（docgen 依赖计算子图结果与装配器输出）。

**做什么：**

- [ ] `src/water_agent/graphs/docgen.py`：`gather_inputs`（汇集计算结果+citations）→ `draft`（模板化，`DocumentTemplate` 工具占位）→ `approval`（`interrupt({draft_preview, changes})` 挂起）→ `finalize`（只有确认后才产出）
- [ ] `cli.py`：`--flow docgen`；交互：中断后显示预览，输入 `y/n` → `Command(resume={"approved": True})`；拒绝分支与"回到 draft 带反馈"的语义要一致（见下方关键片段修正），并明确最大轮数上限
- [ ] **拒绝分支语义统一**：若采用"拒绝 → 带反馈回到 draft（最多 2 轮）"，则状态机必须有 `retry_count` 与条件路由（失败且未超上限 → draft，超上限 → finalize/failed）；若采用"拒绝 → 直接失败"，则删除"回到 draft"的表述。二者择一，不得在任务描述与代码示例中冲突
- [ ] 测试 `tests/test_docgen_hitl.py`：`app.invoke` 触发中断后状态快照正确；`Command(resume=...)` 后流程走完且状态含审批记录；拒绝分支带反馈回流（或按选定语义）；**无 checkpointer 构建时报错**（护住纪律）

**怎么做：** 关键片段（拒绝分支按"回到 draft 带反馈，最多 2 轮"示例）：

```python
from langgraph.types import interrupt, Command

MAX_DRAFT_ROUNDS = 2

def approval_node(state: DocGenState) -> Command | dict:
    decision = interrupt({"draft_preview": state["doc_draft"], "approval_required": True})
    if decision.get("approved") is False:
        rounds = state.get("draft_rounds", 0) + 1
        if rounds < MAX_DRAFT_ROUNDS:
            # 带用户反馈回到 draft，重新生成草稿
            return {"draft_rounds": rounds, "user_feedback": decision.get("reason", ""), "status": "rejected"}
        return {"errors": state["errors"] + [f"用户拒绝：{decision.get('reason','')}"], "status": "failed"}
    return {"approval": True, "status": "running"}
# 条件路由：status=="rejected" → draft；否则 → finalize
# 图配置：interrupt_before=["finalize"] 或本节点内 interrupt；checkpointer 必须为磁盘持久化（真持久化）
```

**动哪些文件：**

- Create：`~/Desktop/给排水agent/src/water_agent/graphs/docgen.py`、`~/Desktop/给排水agent/tests/test_docgen_hitl.py`
- Modify：`~/Desktop/给排水agent/src/water_agent/cli.py`（`--flow docgen`）、`domain/state.py`（approval 语义注释与默认值修正——仅注释与校验，不改结构）

**接口契约：**

- Consumes：Step 2 计算结果、Step 8 装配器
- Produces：`build_docgen_graph(*, checkpointer) -> CompiledStateGraph`；CLI `--flow docgen`

**🧭 使用技能（先读后做）：**

- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-stategraph-designer.md`——**interrupt 放置**（不可逆副作用前 interrupt_before；拒绝 interrupt_after 副作用节点）
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-production-checklist.md`——人审阈值；`skill-guardrail-patterns.md`——破坏性操作人审
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/03 Agent Engineering/Skills/skill-hybrid-planner.md`——LLM 编排与人工确认的边界

**✅ 最小闭环验证：**

```bash
_venv/bin/pytest tests/test_docgen_hitl.py -v    # 预期：≥4 passed（含拒绝分支）
_venv/bin/pytest -q                              # 预期：退出码 0，既有用例不回退
_venv/bin/python -m water_agent.cli --flow docgen --mock-generator "生成 DN150 给水管道计算书"
# 预期：打印预览后等待输入，y 后生成、n 后带反馈回流（或按选定语义）
```

**验收标准：** 中断后进程可恢复（重启 + `--resume` 续跑，若 CLI 支持；否则至少证明 checkpointer 落盘）；审批记录写入 trace；拒绝分支语义与代码一致。

**自检问题：** ① 为什么审批节点要放在"副作用发生前"？② interrupt 与普通"等用户输入"的区别？③ 拒绝后"带反馈回流"与直接失败哪个更符合工程场景？选定后如何在状态机中落地？

**提交：** `feat: Human-in-the-loop 审批（interrupt）`　**回滚：** `git revert HEAD`

---

## Step 10｜双轨评测深化 + 观测 + CI 门禁 + 文档收口（约 1 天 ★★★）

**📍 本步解决什么：** 现状 harness 只测替身（MockGenerator/InMemoryRetriever），真实模型与真实检索"零评测"；无基线对比；无成本/观测；README 与代码脱节。

**🎓 学习目标：** 评测双层化（机制层稳定 / 质量层真实）；检索指标进评测（recall@k/mrr）；LLM-as-judge 的 rubric 设计；基线门槛与 CI 门禁；观测与成本；doc/工程收口。

**前置条件：** Step 1、2、5、7、8、9 全部。

**做什么：**

- [ ] `harness/runner.py`：`variant=="real"`（真实 TriviumRetriever + 真实 ChatOpenAI + 真实 rerank；无 Key 时该变体**结构化 SKIP**，不算失败也不算通过）；`EvalResult` 增加 `cost_usd/tokens/retrieval_metrics`
- [ ] `harness/retrieval_metrics.py` 接入：对 rag 用例计算 `recall@k`、`mrr`（gold_articles_any 命中）；`scorer.py` 引用判分保持确定性
- [ ] `harness/llm_judge.py`：回答质量 judge（rubric 3-5 准则：忠实度/引用覆盖/完整性/清晰度/不编造；judge 模型可配置；逐条输出 1-5 分 + 理由；结果缓存 jsonl 防重复扣费）
- [ ] `data/eval/rag_p1.jsonl`：**≥50 条**真实规范问答 gold（source_id 与 `data/curated/clauses/` 对齐，含 `spec_version`）；评测集扩充后与 P0 集并存
- [ ] 基线：`data/eval/baseline.json` + `scripts/ci_gate.sh` + `scripts/compare_baseline.py`。**门禁分两层**：PR 门禁只跑离线机制层（`pytest` → `rag_p0` 机制层 → `calc_p0 ≥95%` → `ruff/mypy`），违反 exit 1；真实质量层由人工或定时任务触发，结果写报告与基线对比
- [ ] **阈值统一**：以 `compare_baseline.py` 中单一、显式定义的阈值集为准（如 pass_rate 相对下降 ≤2pt、引用正确率相对下降 ≤3pt、无新增核心失败），不再混用"≥5% 回归拦截"等多套口径；任何阈值调整必须修改同一处常量并更新文档
- [ ] **SKIP 语义**：真实变体无 Key 时输出结构化 `SKIP` 且不参与基线比较，退出码仍为 0；真实评测失败（有 Key 但调用出错）与 `SKIP` 的退出码必须不同
- [ ] 观测：`observability/json_trace.py` 增加 `llm_call` 事件（prompt/response 摘要/token/cost/延迟）；可选 `observability/otel.py`（OTel GenAI spans，**guarded import**，未装 SDK 不影响运行——沿用"不阻塞"原则）
- [ ] `prompt` 打磨：用真实评测结果跑 `prompt-advanced-rag-debugger` / `prompt-prompt-optimizer` 轮询改 1 次 prompt，改后必须重新跑质量层并记录 diff
- [ ] README.md **重写**（对齐代码现状：双轨评测、5 类能力当前进度、`--mode`、数据资产与重建说明），更新 `docs/09-next-steps.md`；mypy 清零
- [ ] 最终验收：`pytest` + 全部机制层 harness + `ci_gate.sh` + 生产清单对照（skill-production-checklist 阈值项）

**怎么做：** 关键接口：

```bash
# scripts/ci_gate.sh（示意，PR 门禁 = 离线机制层）
set -e
_venv/bin/pytest -q > /tmp/pytest.log
_venv/bin/python -m water_agent.harness.runner data/eval/rag_p0.jsonl  || exit 1
_venv/bin/python -m water_agent.harness.runner data/eval/calc_p0.jsonl || exit 1
ruff check src && mypy src/water_agent

# 质量层（人工/定时，不在 PR 门禁内）
_venv/bin/python -m water_agent.harness.runner data/eval/rag_p1.jsonl --variant real   # 无Key则SKIP
_venv/bin/python scripts/compare_baseline.py data/eval/baseline.json || exit 1
```
_venv/bin/python -m water_agent.harness.runner data/eval/rag_p1.jsonl --variant real   # 无Key则SKIP
_venv/bin/python scripts/compare_baseline.py data/eval/baseline.json || exit 1
```

**动哪些文件：**

- Modify：`~/Desktop/给排水agent/src/water_agent/harness/{runner,scorer,loader}.py`、`domain/models.py`（EvalResult + cost/tokens）、`observability/json_trace.py`、`README.md`、`docs/09-next-steps.md`、`pyproject.toml`（mypy 严格化）
- Create：`~/Desktop/给排水agent/src/water_agent/harness/llm_judge.py`、`observability/otel.py`、`~/Desktop/给排水agent/data/eval/rag_p1.jsonl`、`data/eval/baseline.json`、`scripts/ci_gate.sh`、`scripts/compare_baseline.py`、`tests/test_llm_judge.py`、`tests/test_ci_gate.py`（无 Key 下 ci_gate 的 SKIP 分支）

**接口契约：**

- Consumes：全部前序步骤产物
- Produces：双轨评测命令 `python -m water_agent.harness.runner <jsonl> [--variant real]`；`ci_gate.sh`；基线文件与门槛约定

**🧭 使用技能（先读后做）：**

- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/03 Agent Engineering/Skills/skill-benchmark-harness.md`——**FTP/PTP 双门禁、逐任务报告 + P50/P95、污染检查、≥5% 回归拦截**（本步核心）
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-eval-patterns.md`——LLM-judge rubric（锚定 rubric 降 30-40% 分歧）、评测规模（PR 100-200 条级）
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Prompts/prompt-eval-designer.md` / `prompt-advanced-rag-debugger.md`——judge 设计与 RAG 根因诊断
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/01 LLM Engineering/Skills/skill-production-checklist.md`——上线阈值验收；`skill-cost-patterns.md`——成本记账
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/03 Agent Engineering/Skills/skill-otel-genai.md`（可选接 OTel）；`Skills/skill-feedback-runner.md`——线上反馈回流候选评测集
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/03 Agent Engineering/Skills/skill-failure-detector.md`——六类通用失败探测器（幻觉引用/范围蔓延等）+ 阈值告警（≥5% 触发），用模式匹配而非 LLM 逐条打分
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/03 Agent Engineering/Skills/skill-rule-set-builder.md`——把三条铁律转成确定性 check（引用校验器/数值校验器已具备，串成 `rule_checker` 套件）
- `~/Desktop/obsidian knowledge/AI Engineering from Scratch/03 Agent Engineering/Skills/skill-workbench-benchmark.md`——改造前后对比：同任务跑"旧流水线 vs 新 agentic 管线"产出 comparison 报告（量化改造收益）
- `~/Desktop/obsidian knowledge/AI应用开发者精选 Skills/Skills/verification-before-completion.md`、`handoff.md`（步骤交接文档）、`ci-cd-and-automation.md`

**✅ 最小闭环验证：**

```bash
bash scripts/ci_gate.sh                 # 无 Key：预期 exit 0（真实变体 SKIP 不计入基线比较）
_venv/bin/pytest -q                     # 预期：退出码 0，既有用例不回退
_venv/bin/python -m water_agent.harness.runner data/eval/rag_p1.jsonl --variant real
# 有 Key：预期报告含 recall@k、mrr、judge 分数、cost_usd、逐条 trace_id；exit 0/1 依门槛
ruff check src && mypy src/water_agent # 预期：0 error
```

**验收标准：** 基准报告 `baseline.json` 首次生成并入库；第二次起任何改动跑 `compare_baseline.py` 均与基线对比；`ci_gate.sh`（PR 门禁）只依赖离线机制层；README 与代码一致；三项铁律均有评测覆盖（计算=程序化判分、引用=校验器+回归、评测=双轨）。

**自检问题：** ① 为什么"机制层必须全绿"与"质量层允许波动"要分开？② FTP/PTP 门禁如何翻译成本项目的"引用正确率/无新增核心失败"？③ 无 Key 时如何保证 CI 仍有意义的 SKIP，且 SKIP 与真实失败退出码不同？

**提交：** `feat: 双轨评测 + CI 门禁 + 生产化收口`　**回滚：** `git revert HEAD`（基线文件保留）

---

# 附录 A｜技能与 Prompt 资产索引（obsidian knowledge → 改造步骤映射）

> 全部资产在 `~/Desktop/obsidian knowledge/`，共 159 份 md。下表按"用在哪一步"索引，执行每步前先读对应文件。

| 步骤 | 必须读取的资产（绝对路径 → 在 `~/Desktop/obsidian knowledge/` 下） | 用途 |
|---|---|---|
| Step -1 | `AI应用开发者精选 Skills/Skills/verification-before-completion.md`；`03 Agent Engineering/Skills/skill-benchmark-harness.md`（基线方法部分） | 现状盘点/新鲜证据/基线落盘 |
| Step 0 | `AI应用开发者精选 Skills/Skills/{git-workflow-and-versioning, verification-before-completion, ci-cd-and-automation}.md`；`03 Agent Engineering/Skills/skill-minimal-workbench.md` | 提交规范/验证纪律/门禁/工作台(AGENTS.md) |
| Step 1 | `01 LLM Engineering/Skills/{skill-function-calling-patterns, skill-eval-patterns}.md` + `Prompts/prompt-eval-designer.md`；`03 Agent Engineering/Skills/skill-hybrid-planner.md`；`docs/design/00-overview-and-decisions.md`（ADR-01） | enum 约束/判分/确定性子图边界/公式口径 |
| Step 2 | `03 Agent Engineering/Skills/skill-hybrid-planner.md`；`01 LLM Engineering/Skills/skill-rag-pipeline.md`；`docs/design/00-overview-and-decisions.md`（ADR-01） | 计算子图/意图路由/missing_inputs 语义 |
| Step 3 | `AI Engineering from Scratch/01 LLM Engineering/Skills/skill-stategraph-designer.md`；`03 Agent Engineering/Skills/{skill-state-schema, skill-state-graph}.md`；`AI应用开发者精选 Skills/Skills/tdd.md` | checkpointer/add_messages/线程策略 |
| Step 4 | `AI Engineering from Scratch/01 LLM Engineering/Skills/{skill-function-calling-patterns, skill-framework-picker}.md` + `Prompts/prompt-tool-designer.md`；`02 MCP Protocol/Skills/skill-tool-schema-linter.md`；`AI应用开发者精选 Skills/Skills/api-and-interface-design.md` | 工具设计四原则/schema 审计/契约 |
| Step 5 | `01 LLM Engineering/Skills/{skill-function-calling-patterns, skill-guardrail-patterns, skill-eval-patterns}.md` + `Prompts/prompt-eval-designer.md`；`03 Agent Engineering/Skills/skill-injection-defense.md` | 错误处理/护栏/不可信数据/判分设计 |
| Step 6 | `03 Agent Engineering/Skills/{skill-agent-loop, skill-runtime-shape, skill-actor-runtime, skill-scope-contract}.md`；`02 MCP Protocol/Skills/skill-parallel-call-safety-check.md`；`01 LLM Engineering/Skills/skill-stategraph-designer.md`；`AI应用开发者精选 Skills/Skills/{incremental-implementation, writing-plans}.md` | 循环收口/停止条件/并行安全/范围契约/特性开关 |
| Step 7 | `01 LLM Engineering/Skills/{skill-function-calling-patterns, skill-structured-outputs, skill-rag-pipeline}.md` + `Prompts/prompt-structured-extractor.md`；`skill-production-checklist.md` | provider 差异/结构化输出/温度 |
| Step 8 | `01 LLM Engineering/Skills/{skill-context-engineering, skill-prompt-caching-planner}.md` + `Prompts/prompt-context-optimizer.md`；`AI应用开发者精选 Skills/Skills/context-engineering.md` | 动态装配/缓存友好/投影/版本状态 |
| Step 9 | `01 LLM Engineering/Skills/{skill-stategraph-designer, skill-production-checklist}.md`；`03 Agent Engineering/Skills/skill-hybrid-planner.md` | interrupt 放置/人审阈值 |
| Step 10 | `03 Agent Engineering/Skills/{skill-benchmark-harness, skill-feedback-runner, skill-otel-genai, skill-failure-detector, skill-rule-set-builder, skill-workbench-benchmark}.md`；`01 LLM Engineering/Skills/{skill-eval-patterns, skill-production-checklist, skill-cost-patterns}.md` + `Prompts/{prompt-eval-designer, prompt-advanced-rag-debugger, prompt-architecture-reviewer}.md`；`AI应用开发者精选 Skills/Skills/{verification-before-completion, handoff, ci-cd-and-automation, source-driven-development}.md` | FTP/PTP 门禁/judge/失败探测器/规则检查器/成本/收口 |

**其余技能备用索引（按需取用）：**

- RAG 组件：`01 LLM Engineering/Skills/{skill-rag-pipeline, skill-advanced-rag, skill-embedding-patterns}.md` + `Prompts/{prompt-rag-architect, prompt-advanced-rag-debugger, prompt-embedding-advisor}.md`（检索质量诊断/混合检索/重排/嵌入选型）
- 提示词：`01 LLM Engineering/Skills/{skill-prompt-patterns, skill-cot-patterns}.md` + `Prompts/{prompt-prompt-optimizer, prompt-reasoning-chain}.md`（Step 10 prompt 打磨轮询）
- 成本：`01 LLM Engineering/Skills/skill-cost-patterns.md` + `Prompts/prompt-cost-optimizer.md`（缓存/路由/预算，P2+）
- 安全：`01 LLM Engineering/Skills/skill-guardrail-patterns.md` + `Prompts/prompt-safety-auditor.md`；`03 Agent Engineering/Skills/{skill-injection-defense, skill-computer-use-safety}.md`；`AI应用开发者精选 Skills/Skills/security-and-hardening.md`
- MCP（P2+，把工具暴露为 server 时用）：`02 MCP Protocol/Skills/{skill-mcp-server-scaffolder, skill-mcp-client-harness, skill-gateway-bootstrap, skill-release-gate, skill-tool-schema-linter, skill-contract-reviewer, skill-parallel-call-safety-check, skill-mcp-auth, skill-sampling-loop-designer}.md`
- 评测与观测：`03 Agent Engineering/Skills/{skill-benchmark-harness, skill-workbench-benchmark, skill-minimal-workbench, skill-failure-detector, skill-feedback-runner}.md`；`AI应用开发者精选 Skills/Skills/observability-and-instrumentation.md`
- 规则与边界：`03 Agent Engineering/Skills/{skill-rule-set-builder, skill-scope-contract, skill-injection-defense}.md`（铁律转确定性检查、改动范围契约、不可信输入）
- Agent 架构补充：`03 Agent Engineering/Skills/{skill-crew-or-flow, skill-reflexion-buffer, skill-memory-blocks, skill-virtual-memory, skill-reviewer-agent, skill-debate}.md`（算力扩展/反思/记忆/评审员，P2+）
- 流程与质量：`AI应用开发者精选 Skills/Skills/{spec-driven-development, planning-and-task-breakdown, executing-plans, subagent-driven-development, test-driven-development, systematic-debugging, debugging-and-error-recovery, code-review-and-quality, requesting-code-review, receiving-code-review}.md`（每步执行与评审用）

# 附录 B｜环境事实与已验证 API（勘察记录，勿改）

| 项 | 事实 |
|---|---|
| Python | `_venv` 3.12.14（README 与 pyproject 要求 ≥3.11,<3.13） |
| 关键依赖版本 | langgraph 1.2.11 / langgraph_prebuilt 1.1.0 / langgraph_checkpoint 4.2.0（base） / langchain_core 1.6.0 / pydantic 2.13.4 / triviumdb 0.8.2 |
| 缺依赖 | `langgraph-checkpoint-sqlite`（SqliteSaver 不可用）、`langchain-openai`（ChatOpenAI 不可用）——分别 Step 3、Step 7 安装 |
| 已验证 API | `create_react_agent`（prebuilt，支持 tools/state_schema/checkpointer/interrupt/prompt/version='v2'）、`add_messages`（langgraph.graph.message）、`interrupt`/`Command`（langgraph.types）、`MemorySaver`（可用）、假模型 `langchain_core.language_models.fake_chat_models`（FakeMessagesListChatModel/GenericFakeChatModel） |
| 未装 | `langchain-openai`、`openai`（Step 7 引入）；`langgraph-checkpoint-sqlite`（Step 3 引入） |
| API Key | 当前 shell 未设置 `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY`；真实链路失败信息：`DASHSCOPE_API_KEY must not be empty`（设计行为） |
| 现有测试 | 214 个全绿（`_venv/bin/pytest -q`）；harness rag_p0：11/12（1 条为设计的 bad_citation 陷阱用例） |
| 数据资产 | knowledge/ 40MB 221 md；clauses/ 1992 条；curated 1595 条；子块 3313；索引 `qwen3.7__{256,512}__curated.db` |

# 附录 C｜术语对照

| 术语 | 本任务书含义 |
|---|---|
| 机制层评测 | 用 MockGenerator/InMemoryRetriever 验证图机制与判分逻辑（离线、确定性） |
| 质量层评测 | 真实 LLM + 真实检索 + judge/rubric，衡量真实回答质量（需 Key，可 SKIP） |
| 最小闭环 | 一个步骤结束即可独立测试、验证、提交、回滚，且有新鲜验证证据 |
| 特性开关 | `--mode legacy|agentic`、`--variant mock|real` 等，护住未完成功能 |
| observation | 工具执行结果/错误，作为消息回填给模型；失败文本必须可见 |
| FTP/PTP | FAIL_TO_PASS/PAST_TO_PASS 双门禁：新增能力必须通过 + 既有能力不得回退 |

# 附录 D｜给 Cursor Agent 的执行守则（每步开工前读一遍）

1. 读本步"使用技能"文件 → 按其中方法产出设计/判分/清单（**先产出该步骤要求的 artifact，再动代码**）。
2. TDD：先写失败测试（确认失败）→ 最小实现（确认通过）→ 全量回归 → 提交。
3. 不碰本步文件清单之外的文件；不改"不动的"文件。
4. 完成 = 本步"最小闭环验证"的命令全部跑出预期输出，且 `pytest -q` 全绿；把命令输出粘贴进 commit message 或 PR 描述。
5. 每步一次 commit；中途发现问题，先 `git status` 检查是否污染了上一步，污染立即 rebase/回滚修正。
6. 任何"以后再说"进入下一步前必须写进 `docs/09-next-steps.md` 更新版。
7. 大量文件操作后跑 `ruff check src && mypy src/water_agent`（Step 10 前仅要求 ruff 无 error；mypy Step 10 清零）。
8. 步骤间交接：按 `handoff` skill 产出一份交接说明（含下一步所需接口签名），保持"接口契约"块同步。
