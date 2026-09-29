# Spec: P5-S48 多模态文件输入与输出（Upload & Export）

> 状态：**后端 Step 1–4 已交付（2026-09-28），前端 Step 5 未做**。实现与本 spec 的偏离（存储位置、SourceTag、检索实现、PDF 引擎）以 `docs/design/p5-step-48-decision.md` 为准。本 Step 交付文件上传（Word/PDF 读取）与文件输出（Excel/Word 下载）的最小闭环，不引入向量存储或 OCR。

## Objective

Web 工作台支持：
1. **输入**：用户上传 `.docx`/`.pdf` 文件，Agent 读取其文本内容并纳入当前问答上下文。
2. **输出**：Agent 生成的材料清单/计算书/设计说明可下载为 `.xlsx`（已有能力）或 `.docx`（新增）。

## Problem

- 当前 Web 端只有纯文本对话，无法上传任何文件。
- `materials/export.py` 已有 `.xlsx` 导出能力，但只落盘到 `/var/lib/water-agent/exports`，没有 HTTP 下载通道。
- `docgen/` 生成的计算书/设计说明只有文本/Markdown，没有 `.docx` 渲染。
- 图纸审查 (`drawing/`) 只支持 DXF，不支持 Word/PDF。

## Decisions

### D1 上传契约：`POST /api/uploads`

**文件类型白名单**（拒绝其余）：
- `.docx`：python-docx 提取纯文本（段落 + 表格）
- `.pdf`：pymupdf 提取纯文本（保留页码标记，不提取图片/OCR）
- `.txt`/`.md`：直读

**安全约束**（沿用 P2.5 护栏纪律）：
- 大小上限：10 MB（`MAX_UPLOAD_BYTES = 10 * 1024 * 1024`）
- 路径穿越：拒绝任何含 `..` 或绝对路径的 filename
- MIME 魔数校验：`.docx` 必须是 ZIP（`PK\x03\x04`），`.pdf` 必须是 `%PDF-`
- 落盘前过 `GuardrailPipeline` 的注入检测（上传文件是 `untrusted_media` 信任级别）
- 病毒扫描：本阶段不做（个人单机部署，风险自留）

**存储**：`/var/lib/water-agent/uploads/{uuid4()}/{safe_filename}`
- `safe_filename` = `unicodedata.normalize("NFKC", filename)` + 保留 alnum + `.` + `-` + `_`，其余替换为 `_`
- 上传目录已在 compose 卷 `app_uploads:/var/lib/water-agent/uploads` 中，无需改 compose

**响应**：
```json
{
  "ok": true,
  "data": {
    "upload_id": "uuid4",
    "filename": "原始文件名（脱敏后）",
    "size_bytes": 12345,
    "extracted_text_length": 5000,
    "extracted_text_preview": "前 200 字符...",
    "page_count": 3
  }
}
```

**错误码**（稳定，进 trace）：
- `UPLOAD_TOO_LARGE`：超过 10 MB
- `UPLOAD_TYPE_NOT_ALLOWED`：MIME/扩展名不在白名单
- `UPLOAD_MAGIC_MISMATCH`：扩展名与文件头不符
- `UPLOAD_EXTRACTION_FAILED`：python-docx/pymupdf 解析失败
- `UPLOAD_INJECTION_BLOCKED`：护栏命中

### D2 上传内容进问答：项目级持久化 + 检索索引

上传的文本**进入项目资料库并编入检索索引**，支持跨会话引用与语义检索：

- 上传时选择目标项目（默认当前项目），文件落 `data/projects/{project_id}/uploads/{upload_id}/`
- 提取的文本写入 `data/projects/{project_id}/uploads/{upload_id}/extracted.txt`
- 文本块入项目级 RAG 索引（`data/indexes/project_{project_id}.db`），与规范条文分开命名空间（`source_type="upload"`）
- `run_manager.py` 在 `_execute` 前检索项目上传内容，命中则注入 prompt
- 上传内容在 trace 中标记为 `uploaded_document`，不存全文（只存 hash + 长度 + 前 200 字符预览）

**生命周期**：文件永久保存（项目资料），用户可手动删除；删除时同时从索引中移除。

### D3 输出契约：`GET /api/runs/{run_id}/export`

**支持格式**（用户已拍板 Word 优先）：
- `format=docx`：计算书/设计说明（新增 `docgen/docx_export.py`，**本 Step 主交付**）
- `format=xlsx`：材料清单（复用 `materials/export.py`，本 Step 只接路由）

**docx 生成**（python-docx）：
- 计算书：标题 → 输入参数表 → 计算过程（代码块）→ 结果 → 引用
- 设计说明：标题 → 章节（h1/h2）→ 正文 → 引用列表
- 样式：Calibri 11pt，A4，1.5 倍行距，页眉页脚（项目名 + 页码）
- 引用：文末自动汇总 `citations` 列表

**响应**：`Content-Disposition: attachment; filename="..."`，流式返回文件内容。

**权限**：只有 run 的 owner 才能下载（`require_password_changed` + `get_visible_run` 校验）。

### D4 前端集成

**上传**：
- 输入框左侧加 📎 按钮，点击弹出文件选择器
- 上传成功后显示为消息卡片（文件名 + 大小 + 提取文本预览），Agent 回答时可引用
- 上传失败显示错误码与可行动文案

**下载**：
- run 完成后，消息卡片底部加「下载 Excel」/「下载 Word」按钮
- 按钮只在 `run.status == "succeeded"` 且 `run.calculations` 非空（Excel）或 `run.answer` 非空（Word）时显示
- 点击直接触发浏览器下载，不跳转新页面

### D5 不做什么

- 不做 PDF OCR（图片转文字）
- 不做 Excel/CSV 上传解析（只读 Word/PDF/TXT）
- 不做用户间的文件共享
- 不做版本控制（同一文件上传两次就是两个 upload_id）
- 不做图片上传（ JPG/PNG 等）
- 上传内容入项目级检索索引（`data/indexes/project_{project_id}.db`），与规范条文分开命名空间（`source_type="upload"`）；不做全局向量存储

## 用户已拍板

- **上传文件生命周期**：入库长期保存（项目资料，可跨会话引用），非用完即删。
- **输出格式优先级**：Word（计算书/设计说明）优先，Excel 复用已有能力。

## Testing

### 后端 pytest

- `test_upload.py`：
  - 合法 .docx → 200 + extracted_text_length > 0
  - 合法 .pdf → 200 + page_count > 0
  - 超大文件 → 413 UPLOAD_TOO_LARGE
  - 错误扩展名 → 400 UPLOAD_TYPE_NOT_ALLOWED
  - MIME 不符（.docx 内容不是 ZIP）→ 400 UPLOAD_MAGIC_MISMATCH
  - 注入内容（`system: ignore previous`）→ 200 但护栏标记（不阻断，因为只是数据）
- `test_export.py`：
  - run 有 calculations → GET export?format=xlsx → 200 + Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
  - run 无 calculations → export?format=xlsx → 404
  - run 有 answer → export?format=docx → 200 + .docx 魔数正确
  - 非 owner 下载 → 403

### 前端

- `tests/upload.test.mjs`：文件选择器触发、上传进度、错误渲染
- `tests/export.test.mjs`：下载按钮显示条件、点击触发 download

### 冒烟

- `smoke.py` 新增：`POST /api/uploads` 合法 .txt → 200；`GET /api/runs/{id}/export?format=xlsx` → 200 或 404（取决于 run 内容）

## Files

新增：
- `src/water_agent/webapp/upload_routes.py`（上传路由）
- `src/water_agent/webapp/export_routes.py`（下载路由）
- `src/water_agent/docgen/docx_export.py`（Word 生成）
- `tests/test_upload.py`、`tests/test_export.py`
- `web/app/src/uploadApi.ts`、`web/app/src/exportApi.ts`

修改：
- `src/water_agent/webapp/app.py`：注册新路由
- `src/water_agent/webapp/run_manager.py`：上传内容注入逻辑
- `web/app/src/Workbench.tsx`：上传按钮 + 消息卡片扩展
- `web/app/src/workspaceComponents.tsx`：下载按钮
- `web/app/src/workspaceApi.ts`：upload/export 方法
- `pyproject.toml`：新增 `python-docx`、`pymupdf` 依赖

## Risks

- **R1 PDF 文本提取质量**：pymupdf 对扫描件 PDF 无效（无文本层）。本阶段只支持文字版 PDF，扫描件返回 `extracted_text_length=0` 并提示用户。
- **R2 docx 样式简陋**：python-docx 默认样式较朴素。P5-S48 只做"能打开、内容正确"，美观迭代留到后续。
- **R3 上传文件清理**：~~24 小时 TTL 是硬编码~~（已作废：与「入库长期保存」拍板冲突）。当前无 retention，长期运行需监控 `uploads/` 卷大小。
