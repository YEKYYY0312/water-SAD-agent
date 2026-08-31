# 文件读取 与 MCP 架构

## 1. 文件读取能力矩阵

| 类型 | 解析方案 | 难点与处理 |
|---|---|---|
| **PDF**（规范/手册） | `pymupdf`/`pdfplumber` + 表格/公式处理 | 规范条文多为表格+条款结构 → 按「条号」做条款级 chunk，提取条文号、标题、正文、表格作为独立结构 |
| **Word**（计算书/报告） | `python-docx` | 保留章节结构、表格；提取章节 = 计算步骤 |
| **Excel**（计算表） | `openpyxl`/`pandas` | 读取公式结果 + 原始公式，便于「反算」核验 |
| **DWG**（图纸，P3） | ODA File Converter / libredwg → 图元；或 MCP 接专业工具 | **最大难点**：图层/图元语义映射到给排水对象（管线、立管、洁具、水表等）。单独一个阶段 |

### 读取后统一中间格式
所有文件解析后归一成结构化对象，进入 `tools/file_readers.py` 暴露的统一工具：
```python
read_file(path) -> {
    "kind": "pdf|docx|xlsx|dwg",
    "content": "...",           # 文本/结构化内容
    "chunks": [ {id, text, meta} ],
    "tables": [ ... ],
    "entities":[ ... ],        # DWG 才有的图元对象
}
```

## 2. MCP 架构（框架先行）

MCP 是**工具层标准化协议**。设计上在 `mcp/client.py` 做一个**客户端网关**，把任意 MCP server 的能力统一注入为 LangGraph 可用 tool：

```
LangGraph Tool 层
   │ (mcp.tool())
   ▼
MCP Client Gateway  (mcp/client.py)
   ├── mcp_server_hydraulics   # 水力计算引擎 (自研, P1)
   ├── mcp_server_specs        # 规范库检索 (自研, P1)
   ├── mcp_server_cad_bim      # CAD/BIM: 天正/Revit/中望 (P4 占位)
   └── mcp_server_enterprise   # 企业知识库/数据库 (P4 占位)
```

### 2.1 网关设计要点
- **统一封装**：把每个 MCP server 的 tools 列表 fetch 出来 → `@tool` 或 `langchain.tools` 包装，注册进 supervisor 的工具可见集。
- **可插拔**：MCP server 端点在 `config/` 配置，未配置的 server 自动跳过（对应「框架先行」）。
- **工具权限**：读/写分离；危险操作（如写文件）标记需人工确认。

### 2.2 自研 MCP server（给排水差异化核心，P1 先落）
1. **水力计算 server**（`mcp/servers/hydraulics_server.py`）
   - tools: `calculate_velocity`, `calculate_head_loss`, `select_diameter`, `calculate_pump_head`, `calculate_drainage_slope`
   - 每个 tool 输入明确参数 + 单位，输出数值 + 所用公式口径。
2. **规范库检索 server**（`mcp/servers/specs_server.py`）
   - tools: `query_spec(article/number)`, `get_article`, `get_current_version`
   - 保证「版本正确、条号可溯源」。

### 2.3 外部能力占位（P4）
- CAD/BIM：通过图元/API 打通（天正、Revit、中望）——先留接口。
- 企业库：接入设计院的既有知识库/项目库——先留接口。

## 3. 为什么先自研计算 + 规范库两个 server

这两者决定了给排水 Agent 的**可靠性**（数值准确 + 引用可溯），是 Agent 能在工程场景被信任的前提，也是它的差异化价值。CAD/BIM 和企业库是放大器，等核心稳定再接。
