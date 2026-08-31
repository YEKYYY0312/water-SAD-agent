# Loop Engineering —— LangGraph 循环设计

这是编排层的核心。采用 **Supervisor(监督者) + 子图** 架构，共享单一 `WaterAgentState`。

## 1. 状态定义

```python
from typing import TypedDict, Annotated

class WaterAgentState(TypedDict):
    messages: list           # 对话历史
    intent: str              # router 输出: rag | calc | drawing | docgen | material
    query: str               # 当前用户问题（可能被改写）
    context: list            # 检索到的规范条款 [{title, article, content}]
    calc_result: dict | None # 水力计算结果 + 校验状态
    drawings: list           # 图纸解析结果
    doc_draft: str | None    # 文档草稿
    citations: list          # 引用的规范条款（可追溯）
    approval: bool | None    # human-in-loop 是否已确认
    errors: list
    run_id: str
    trace_id: str
    retry_count: int
    step_count: int
    status: str               # running | waiting | succeeded | failed
    missing_inputs: list
    assumptions: list
```

## 2. 主图（Supervisor）

```
 start ─► router_node ─┬─► rag_subgraph ─┐
                       ├─► calc_subgraph ─┤
                       ├─► drawing_subgraph ─┤
                       ├─► docgen_subgraph ─┤
                       └─► material_subgraph ─┘
                                    │
                          reflection_node  ◄──┐
                                    │        │ (不合格, 带反馈回流)
                          ┌─ approval? ──┘
                                    │
                    yes ──► 输出 / 终结
                    no  ──► interrupt (等待人工确认)
```

## 3. 各子图 Loop 细节

### 3.1 RAG 子图（规范问答）
```
query_rewrite → hybrid_retrieve(BM25+vector) → rerank → generate → citation_check
        ▲                                                  │
        └──────── self_check 失败(引用错误/资料不足) ────────┘
```
- **Retry 逻辑**：`citation_check` 验证引用的「规范编号 + 条号」是否与检索结果一致。版本有效性必须依赖版本注册表，不能仅凭条款文本推断。失败→改写 query 重检索（max 2 次）。
- **防护**：检索结果为空或置信度低时，明确告知「无法在语料中定位该条款」，**禁止编造条号**。

### 3.2 计算子图（水力计算）
```
parse_params(从对话或文件) → choose_formula → call_hydraulic_tool → numeric_validate
        ▲                                                        │
        └── 结果越界/单位错误/参数缺失 ──► 带反馈重新解析 ──────────┘
```
- **程序化数值校验**：调用真实水力计算函数（如 Darcy-Weisbach / Hazen-Williams、曼宁公式），LLM 只负责**参数理解与结果解释**，绝不参与数值运算。
- **回环**：若结果超出约束，生成候选方案并说明变化；不得静默修改用户输入。P1 默认要求用户确认后再重算。

### 3.3 图纸子图（DWG 审查，P3）
```
load_dwg → extract_entities → map_to_schema(给排水图层/图元) → rule_check(对照规范) → report_defects
```
- 规则引擎（规范条文 → 可执行检查规则）+ 可能的 vision 模型辅助图面理解。

### 3.4 文档生成子图（计算书/设计说明）
```
gather_inputs(计算结果+citations) → draft → format_template → self_check → approval(interrupt)
```
- **Human-in-the-loop**：生成正式文档**前**停在 `approval` 节点，人工确认后产出。

## 4. Human-in-the-loop

用 LangGraph 的 `interrupt` / `interrupt_before/after` 在 `approval` 节点暂停，恢复时携带确认结果。用 `checkpointer`（如 SqliteSaver/PostgresSaver）支持暂停/恢复。所有循环必须有 `max_steps`、节点级 `max_retries`、超时和终止状态，禁止无限循环。

## 5. LangGraph 关键选型

| 能力 | 用 LangGraph 的哪个特性 |
|---|---|
| 循环/重试 | `StateGraph` + 在节点内部或边决策 |
| 状态共享 | 单一 `WaterAgentState`，`Annotated` reducer |
| 人机暂停 | `interrupt()` + checkpointing |
| 条件分支 | 节点返回的 `intent` 驱动的 conditional edges |
| 并发/子图 | 用 `StateGraph` 组合成子图，主图调用子图 node |

## 6. 反射（Reflection）自检清单

每个子图输出后统一过 `reflection_node`，检查：
1. **依据规范是否现行有效**（条号存在、版本更新）
2. **引用是否精确**（编号 + 条号匹配）
3. **计算数值是否物理/规范合理**（流速、坡度、充满度范围）
4. **口径是否统一**（单位、设计秒流量、重现期）
5. **是否给出可执行结论**（而非罗列）

不合格 → 收集到 `errors`，带反馈回流到对应子图。
