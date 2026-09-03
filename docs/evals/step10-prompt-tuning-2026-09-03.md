# Step 10 Prompt Tuning 收口证据

> 记录时间：2026-09-03
>
> 本文档只记录**真实状态**，不把未验证项写成完成。区分三类变化：
> 1. prompt 改动；2. 协议/评测修复；3. 检索变化。
> 任何结论都必须能追溯到具体报告文件或命令输出。

## 1. 变更前后报告

### 1.1 修复前报告

- 报告文件：`data/eval/reports/rag_p1_20260903T115504Z.json`
- 结果：`55/58`，`pass_rate=0.9482758621`，`citation_ok_rate=1.0`
- 主要失败用例：
  - `rag-p1-0011`：真实链路失败（该条在当前记录中未收口，属于独立问题）；
  - `rag-p1-0049`：语料为“联网整理的设计要点综述，非正式规范条文”，旧 prompt 允许把它当正式条文引用，评审认为策略错误；
  - `rag-p1-0050`：旧评审修复把 `forbidden_articles` 从检索中剔除，属于 gold/标签泄漏。

### 1.2 协议/评测修复后的报告

- 报告文件：`data/eval/reports/rag_p1_20260903T124322Z.json`
- 结果：`57/58`，`pass_rate=0.9827586207`，`citation_ok_rate=1.0`
- 说明：该轮通过是**违规通过**，原因是：
  - runner 端把 `forbidden_articles` 做成了过滤器，改变了模型输入与检索指标；
  - prompt 仍保留“要点综述 / 非正式条文照常引用，标注不构成禁止引用”的旧规则。
- 结论：这份报告不能作为收口证据，仅作为“错误修复尝试”的历史记录。

### 1.3 本次修复后的真实运行结果

- 运行命令：

```bash
_venv/bin/python -m water_agent.harness.runner data/eval/rag_p1.jsonl --variant real --judge
```

- 结果：`data/eval/reports/rag_p1_20260903T133322Z.json`
- 结果摘要：`total=58, passed=0, failed=58, skipped=0, pass_rate=0.0, citation_ok_rate=1.0, quality_pass_rate=0.0`
- 失败原因：
  - 真实网络链路在当前 shell 通过代理返回 `OpenAIConnectionError: Connection error.`
  - 无法完成真实 LLM 回答与 judge 评分，因此 `quality_judged_count=0`
- 这说明：当前环境下**无法给出真实质量层通过结论**；本轮已如实记录为阻塞，不伪造结果。

## 2. 完整 prompt diff

### 2.1 规则源

- `src/water_agent/prompting/assembler.py::ROLE_AND_PRINCIPLES`
- `src/water_agent/graphs/agent_rag.py::DEFAULT_AGENTIC_PROMPT`

两者已改为复用同一公共静态提示定义，不再维护两套规则正文。

### 2.2 差异要点

- canonical 名称恢复为 `【三条铁律】`；
- “引用精准”从铁律中移出，作为普通执行规则；
- 删除“要点综述 / 非正式条文照常引用，标注不构成禁止引用”旧规则；
- 新增明确约束：
  - 带“要点综述 / 联网整理 / 非正式规范条文”标记的内容只能作为检索线索；
  - 回答必须明确标注其非正式性质；
  - 不得把它写成正式规范原文；
  - 不得据此给出未经正式来源核实的强制性结论或精确限值；
  - 需要正式依据时，应说明需核对正式出版标准。

## 3. 固定环境与参数

本次真实/离线评测使用的固定项：

- 模型：`deepseek-chat`
- 索引：`data/indexes/qwen3.7__256__curated.db` 与 `qwen3.7__512__curated.db`
- rerank：`none` / 本机默认配置（真实网络未通）
- `top_k=5`
- `recall_top_k=20`
- 评测集：`data/eval/rag_p1.jsonl`（58 条）
- judge：`--judge`（但本次 `quality_judged_count=0`）

## 4. 改进、回退、仍失败项

### 改进

- 零引用闭环已在机制层加固：有检索、有回答、无引用时回流一次，再次空引用结构化失败；
- `citation_ok_rate` 语义已与 expected_articles 对齐：零引用对需要引用的用例计失败；
- prompt 规则已收敛到单一来源，避免四条铁律漂移；
- 质量层判定已独立为 `quality_pass`，judge 低分不再与机制层 citation 判分混淆。

### 回退/保留

- `rag-p1-0049` 仍然依赖非正式语料：当前数据只能提供“要点综述 / 非正式规范条文”记录，**无法替代正式出版标准**。
- 这不是 prompt 能修复的问题，属于数据质量阻塞。

### 仍失败项

- 真实质量层 `rag_p1 --variant real --judge` 当前在本机环境无法完成（代理网络错误）；
- 真实检索矩阵重跑仍需可用网络与 Key；
- `rag-p1-0049` 的数据质量问题仍在：若后续真实链路恢复并严格执行新 prompt，该用例应如实失败，除非语料升级为正式规范来源。

## 5. 结论

- **prompt 改动已完成并通过离线机制层验证**；
- **协议/评测修复已完成并通过离线机制层验证**；
- **真实质量层证据不足，当前结论为 blocked，而非通过**。
