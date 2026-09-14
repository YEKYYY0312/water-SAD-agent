# 给排水 Agent 项目 Review 报告

**Review 日期**: 2026-09-13  
**Review 范围**: P0–P5 全栈（Step -1 至 Step 46）  
**CI 状态**: ✅ 全部通过（2093 passed, 24 skipped）

---

## 一、总体评价

项目工程质量较高，已建立完善的双轨评测体系、治理机制和自托管能力。代码覆盖率充足，文档详实，有明确的验收标准和铁律约束。

**核心优势**：
- ✅ 评测先行：5 套机制层评测集（rag/calc/docgen/material/governance p0）+ 门禁基线对比
- ✅ 工程纪律：三条铁律（程序化计算/引用可追溯/评测先行）有代码强制
- ✅ 治理完备：限流/预算/熔断/护栏/检测器/trace 已接入 6/6 远程出口
- ✅ 安全意识：分层护栏、确定性校验、fail-closed 设计、威胁模型文档化
- ✅ 可观测性：JSON trace + 可选 OTel、8 个失败检测器、聚合指标

**当前阻塞（已知且已标记）**：
- ⚠️ **真实 RAG 质量 blocked**：rag_p1 机制 56/58、质量 36/58（62.07%），退出码 1
- ⚠️ **真实图纸未验证**：drawing_p0 是合成 smoke 集，不构成召回率验收证据
- ⚠️ **规范版本未核实**：4 本规范全为 unknown，缓存因此结构性命中率为 0
- ⚠️ **真实 MCP server 未验证**：只与本地 fake server 验证过往返
- ⚠️ **真实记忆质量 blocked**：memory_p0 用合成关键词 gold，不证明自然语言召回率

---

## 二、发现的问题（按严重程度排序）

### 🔴 严重问题（阻塞生产使用）

#### 2.1 真实 RAG 质量未达标
- **位置**: `data/eval/rag_p1.jsonl --variant real --judge`
- **现状**: 机制 56/58、独立质量 36/58（62.07%），退出码 1
- **影响**: 无法保证生产环境规范问答的准确性和引用质量
- **根因**: 
  - 检索 gold 可能存在泄漏（70 条 query 与条文原文高度同构）
  - 百炼 rerank 返回 403 `AccessDenied`
  - 22 条质量层失败案例未分析根因
- **建议**: 
  1. 重建无泄漏的 gold 集（真实用户问题 + 盲标注）
  2. 确认百炼 rerank 权限或切换 reranker
  3. 对失败用例做根因分类（检索失败/生成失败/引用失败）并针对性优化

#### 2.2 规范版本状态全部 unknown
- **位置**: `data/versions.json`
- **现状**: GB50015-2019、GB50013-2018、GB50014-2021、GB50318-2017 均为 status=unknown
- **影响**: 
  - 缓存写入资格恒为 false（原因 `spec_version_unknown`），结构性命中率为 0
  - 无法向用户保证引用的规范是现行有效版本
  - 版本降级警告会出现在每次回答的 citation 中
- **根因**: 人工核实工作未完成
- **建议**: 
  1. 到住建部/标准院官网查询 4 本规范的公告号、生效日期和现行状态
  2. 同步填写 `status`、`effective_date`、`source`（公告号/生效日期/查询出处）和 `verified_at`
  3. 更新 `tests/test_version_registry.py::test_status_from_prod_json`
  4. 核实后缓存即可解锁，届时接入 RAG 主路径

#### 2.3 真实图纸样本缺失
- **位置**: P3 图纸审查链路
- **现状**: 
  - 全仓 0 个真实设计院图纸样本
  - drawing_p0 是 ezdxf 程序化合成的 smoke 集（`real_samples=false`）
  - 不支持 INSERT 块参照、XREF 外部参照、非标图层、图纸空间视口
- **影响**: 
  - 不能声称能处理真实图纸
  - 不能声称缺陷召回率达标
  - ANNOTATION_MATCH_RADIUS 可能需按真实图纸重新标定
- **建议**: 
  1. 采集 10–20 张真实设计院 DXF（脱敏处理）
  2. 人工标注缺陷 ground truth
  3. 补充 INSERT/ARC 实体支持
  4. 建立 drawing_p1 真实评测集并达到召回率基线（如 ≥80%）

---

### 🟡 中等问题（影响功能完整性）

#### 2.4 LangGraph 弃用警告
- **位置**: `src/water_agent/graphs/agent_rag.py:745`
- **现状**: `create_react_agent` 从 LangGraph 移到了 langchain.agents（227 warnings）
- **影响**: 未来 LangGraph V2.0 会移除该 API
- **建议**: `from langchain.agents import create_react_agent` 替换导入

#### 2.5 护栏路径面不识别相对路径
- **位置**: `runtime/governance/rules.py::GuardrailPipeline._looks_like_path`
- **现状**: 
  - `plan1.dxf`、`drawings/a.dxf` 会被判定为 `allowed=True`
  - 刻意保守以免误伤 `DN200/DN150`
- **影响**: 相对路径形态的工具参数需要在工具层自行校验
- **建议**: 
  - 文档化约束：新增路径参数工具必须在工具层做绝对路径校验
  - read_drawing 已正确实现（resolve + relative_to + ToolPolicy）
  - 考虑在 ToolSpec 中增加 `path_params: list[str]` 声明，自动做绝对路径校验

#### 2.6 检测器未接进生产调用站点
- **位置**: `runtime/governance/observability.py`
- **现状**: 
  - 8 个失败检测器已实现
  - 但 CLI/runner 生产调用站点未传递 trace 参数
  - 检测器实际未运行
- **影响**: 无法在生产环境检测治理失败模式
- **建议**: 
  1. 在 `cli.py` 和 `harness/runner.py` 中传递 trace 参数
  2. 增加 e2e 测试验证检测器在真实链路触发

#### 2.7 材料统计 Excel 导出未接护栏
- **位置**: `materials/export.py`
- **现状**: 路径参数未经护栏路径校验
- **影响**: 可能写入预期外的路径
- **建议**: 
  - 在 `export_to_excel` / `export_to_csv` 中调用 `GuardrailPipeline` 的路径校验
  - 或限制输出目录白名单（如只允许 `./output/` 下）

#### 2.8 memory 待确认候选存进程内
- **位置**: `memory/service.py`
- **现状**: 候选表在进程内存（15 分钟 TTL）
- **影响**: 多 worker 部署时候选不共享
- **建议**: 
  - 持久化候选表到 PostgreSQL（新表 `pending_memory_candidates`）
  - 或在部署文档中明确标注"当前只支持单 worker"

---

### 🟢 轻微问题（改进建议）

#### 2.9 未提交的改动
- **位置**: `tests/test_read_drawing_tool.py`
- **现状**: 有未提交的改动（Step 42 注释更新）
- **建议**: `git add` 并 commit

#### 2.10 未追踪的文件
- **位置**: 根目录
- **现状**: 
  - `CONTRIBUTING.md`、`LICENSE`、`TROUBLESHOOTING.md`
  - `blender_addon/`、`deploy/`、`docs/LLM_ENV.md` 等
- **建议**: 
  - 确认是否应该入库
  - 不需要的话加入 `.gitignore`

#### 2.11 SQLAlchemy datetime adapter 弃用
- **位置**: Python 3.12+ 环境
- **现状**: SQLAlchemy 使用了 Python 3.12 弃用的 datetime adapter
- **影响**: 227 warnings
- **建议**: 等待 SQLAlchemy 上游修复，或按 sqlite3 文档实现 replacement recipe

#### 2.12 缓存未接进 RAG 主路径
- **位置**: `graphs/rag.py`、`run_once`
- **现状**: 虽然缓存机制已实现，但未接进主路径（因版本 unknown 导致命中率为 0）
- **建议**: 规范版本核实后立即接入

#### 2.13 DWG 支持 blocked
- **位置**: P3 图纸链路
- **现状**: 本机无 ODA File Converter / libredwg，且转换需 `EXECUTE_SHELL`
- **影响**: `.dwg` 恒返回 `DRAWING_UNSUPPORTED_FORMAT`
- **建议**: 
  - 文档化约束：只支持 DXF，DWG 需人工预先转换
  - 或提供独立的转换服务（隔离沙箱，不暴露给模型）

#### 2.14 Docker Compose build 在本机失败
- **位置**: Docker Desktop 29.7.2
- **现状**: `docker compose build` 因 buildx bake 的 gRPC 错误失败
- **影响**: 必须用 `docker build -t water-agent-app:local .`
- **建议**: 
  - 在 README 中补充说明
  - CI 不受影响（GitHub runner 正常）

---

## 三、代码质量检查

### 3.1 测试覆盖率
- ✅ 2093 个测试全部通过
- ✅ 24 个跳过测试均有前置条件说明（PostgreSQL/Docker/MCP server）
- ✅ 关键路径有回归保护（如引用校验、护栏、版本降级）

### 3.2 静态检查
- ✅ ruff check 通过
- ✅ mypy 通过（Step 10 要求清零）
- ✅ 有 AST 静态断言（如检测器不调用 LLM、治理不依赖 langgraph）

### 3.3 文档完整性
- ✅ README 详细且与代码一致
- ✅ AGENTS.md 是唯一权威规范
- ✅ 每个阶段有设计文档（`docs/design/`）和决策记录
- ✅ 评测证据文档化（`docs/evals/`）

### 3.4 安全性
- ✅ 威胁模型文档化（`docs/design/p5-threat-model.md`）
- ✅ 分层护栏（输入/工具/检索/输出/记忆）
- ✅ fail-closed 设计（未知即拒绝）
- ✅ 镜像安全检查（非 root/无密钥材料/无上下文泄漏）
- ⚠️ 护栏是确定性正则，必然存在未覆盖变体（已标明）

### 3.5 可维护性
- ✅ 单一真源原则（如 THRESHOLDS、inventory、错误码）
- ✅ 版本化配置（governance/mcp）
- ✅ 稳定错误码命名空间（MCP_*/DRAWING_*/WEB_*）
- ✅ 观测完备（trace/metrics/detectors）

---

## 四、架构健康度

### 4.1 依赖管理
- ✅ pyproject.toml 版本约束清晰
- ✅ uv.lock 锁定依赖
- ✅ 纯标准库实现 MCP 客户端（pypi.org 不可达时的 resilience）

### 4.2 模块划分
- ✅ 清晰的层次（domain/graphs/tools/runtime/harness）
- ✅ 治理层不依赖业务逻辑（governance 是独立包）
- ✅ 接口稳定（ToolSpec/ProviderCall/ContentEnvelope）

### 4.3 扩展性
- ✅ 工具注册表（新增工具只需注册）
- ✅ Provider 可替换（RateLimitStore/BudgetLedger/MemoryProvider）
- ✅ MCP 桥接机制（第三方工具无需改代码）

---

## 五、优先修复建议（按影响排序）

### 🔥 P0（立即修复）
1. **提交未 commit 的改动**：`tests/test_read_drawing_tool.py`
2. **核实规范版本状态**：解锁缓存（预计收益：减少 50%+ 模型调用成本）
3. **修复 LangGraph 弃用警告**：避免未来 breaking change

### 🔥 P1（本周完成）
4. **接入检测器到生产站点**：真正发挥失败检测价值
5. **材料导出接护栏**：补齐路径校验
6. **清理未追踪文件**：明确哪些入库、哪些 ignore

### 🔥 P2（规划中）
7. **采集真实图纸样本**：建立 drawing_p1 评测集
8. **重建无泄漏 RAG gold**：解除 rag_p1 blocked 状态
9. **持久化 memory 候选表**：支持多 worker 部署

---

## 六、总结

项目整体质量优秀，工程化程度高，有明确的完成定义和验收标准。当前的 blocked 状态都已明确标记且有缓解措施（如离线可用、结构化错误码）。

**最大风险**：真实场景质量未验证（RAG/图纸/记忆均用合成数据），需要尽快补充真实样本评测。

**最大收益**：核实规范版本状态即可解锁缓存，预计减少 50%+ 重复调用成本。

**建议下一步**：
1. 先修复 P0 问题（提交改动 + 核实版本 + 修复弃用）
2. 并行推进 P1（检测器接入 + 护栏补全）
3. 启动真实样本采集（图纸/RAG/记忆）并建立 p1 评测集
