# 分层架构

```
┌────────────────────────────────────────────────────────┐
│  React 前端 (P5)                                        │
│  聊天 · 文件上传 · 工具调用可视化 · 图纸预览 · 反馈采集   │
└───────────────▲────────────────────────────────────────┘
                │ HTTP / SSE（流式）
┌───────────────┴────────────────────────────────────────┐
│  FastAPI 服务层 (water_agent.api)                       │
│  POST /chat · POST /upload · GET /tools · POST /feedback│
└───────────────▲────────────────────────────────────────┘
                │
┌───────────────┴────────────────────────────────────────┐
│  LangGraph 编排层 (graphs/)  —— Loop Engineering        │
│  Supervisor StateGraph                                  │
│    ├─ Router（意图分类）                                 │
│    ├─ RAG 子图                                          │
│    ├─ Calc 子图（水力计算）                              │
│    ├─ Drawing 子图（图纸审查）                           │
│    ├─ DocGen 子图（计算书/说明生成）                     │
│    └─ Reflection 自检环 + Human-in-loop                 │
└───────────────▲────────────────────────────────────────┘
                │
┌───────────────┴────────────────────────────────────────┐
│  Tool 层 (tools/ + mcp/)                               │
│  PDF/Word/Excel/DWG解析 · 水力计算库 · MCP Client Gateway│
│   ┌─ MCP server: 计算引擎 (自研)                        │
│   └─ MCP server: 规范库检索 (自研)                      │
└───────────────▲────────────────────────────────────────┘
                │
┌───────────────┴────────────────────────────────────────┐
│  数据/检索层                                            │
│  knowledge/*.md + clauses/*.jsonl                       │
│    → ingestion/chunker (结构化父子级切块，见 08-ingestion) │
│    → 父块(章/节) + 子块(条/款/项/表) + 递归兜底            │
│    → 子块向量+文本 → TriviumDB search_hybrid              │
│    → 检索命中子块 + parent_id 回填父块上下文              │
│  data/eval   金标准评测集                               │
└────────────────────────────────────────────────────────┘
        ║  贯穿全程
        ▼
  Harness 评测层 (harness/)  +  观测 (LangSmith/Langfuse)
```

## 模块职责

| 模块 | 职责 | 关键文件 |
|---|---|---|
| `graphs/` | 定义 LangGraph 图结构、节点、边、状态、checkpoint；P0 只实现最小 RAG 图 | `graphs/supervisor.py`, `graphs/rag.py` |
| `ingestion/` | 结构化父子级切块与入库：Chunk/Chunker/表格提取/TriviumDB 子块入库，父块回填 | `ingestion/chunker.py`, `ingestion/pipeline.py` |
| `retrieval/` | 检索适配器：InMemory / TriviumDB search_hybrid（子块 only）+ parent_store 回填 | `retrieval/trivium.py` |
| `tools/` | 工具函数：文件解析、水力计算、通用 LLM 工具 | `tools/hydraulics.py`, `tools/file_readers.py` |
| `mcp/` | MCP 客户端网关、把外部能力统一成工具 | `mcp/client.py`, `mcp/servers/` |
| `harness/` | 评测框架：数据集加载、指标计算、跑分、回归；P0 先实现确定性判分 | `harness/runner.py`, `harness/metrics.py` |
| `api/` | FastAPI 暴露给前端 | `api/routes.py` |
| `config/` | 模型、key、检索、容忍度等配置 | `config/settings.yaml` |

## 依赖方向

`api` → `graphs` → `tools`/`retrieval`/`mcp`；`harness` 只依赖公开接口与 fixture，用于测试，不被生产链路反向依赖。

P0 不实现完整 Supervisor、计算子图、DWG、文档生成、前端或真实 MCP server；只保留后续扩展边界。
