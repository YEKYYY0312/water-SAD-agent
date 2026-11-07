# P5-S48 执行 Prompt：多模态文件输入与输出

> 当前工作区：`/Users/yaerken/Desktop/给排水agent`
> 参考文档：`docs/design/p5-step-48-multimodal-upload-export.md`（spec）、`docs/design/10-sdd-harness.md`（SDD 规范）、`AGENTS.md`（项目规则）

---

## 任务理解

你正在实现「多模态文件输入与输出」能力。这是一个 SDD（规格驱动开发）任务：

- **先读 spec**：`docs/design/p5-step-48-multimodal-upload-export.md`
- **再读 SDD 规范**：`docs/design/10-sdd-harness.md`（特别是规格模板和 Harness 六层）
- **遵守 AGENTS.md**：三条铁律、验证命令、改动范围纪律

**目标**：让 Web 工作台支持：
1. 用户上传 `.docx`/`.pdf`/`.txt` 文件，Agent 读取文本内容并纳入问答上下文
2. Agent 生成的回答可下载为 `.docx`（计算书/设计说明）或 `.xlsx`（材料清单）

**不做什么**（spec 里明确列出的）：
- 不做 PDF OCR（图片转文字）
- 不做 Excel/CSV 上传解析
- 不做全局向量存储（上传内容只入项目级索引）
- 不做图片上传（JPG/PNG）

## 用户已拍板的关键决策

1. **上传生命周期**：入库长期保存（`data/projects/{project_id}/uploads/`），可跨会话引用
2. **输出优先级**：Word（计算书/设计说明）优先，Excel 复用已有 `materials/export.py`
3. **检索集成**：上传文本入项目级索引（`data/indexes/project_{project_id}.db`），`source_type="upload"`

## 实施步骤（按依赖顺序）

### Step 1: 上传路由与文件解析

**文件**：
- 创建 `src/water_agent/webapp/upload_routes.py`
- 创建 `src/water_agent/documents/__init__.py`（新包：文档解析）
- 创建 `src/water_agent/documents/extractors.py`（docx/pdf/txt 提取器）
- 创建 `tests/test_upload.py`

**接口契约**：
```python
# POST /api/uploads
# Request: multipart/form-data, field name="file"
# Response 200:
{
  "ok": true,
  "data": {
    "upload_id": "uuid4",
    "filename": "原始文件名（脱敏后）",
    "size_bytes": 12345,
    "extracted_text_length": 5000,
    "extracted_text_preview": "前 200 字符...",
    "page_count": 3  # 仅 PDF
  }
}
# Errors: UPLOAD_TOO_LARGE / UPLOAD_TYPE_NOT_ALLOWED / UPLOAD_MAGIC_MISMATCH / UPLOAD_EXTRACTION_FAILED / UPLOAD_INJECTION_BLOCKED
```

**关键实现**：
- 大小上限：10 MB（`MAX_UPLOAD_BYTES = 10 * 1024 * 1024`）
- 魔数校验：`.docx` 必须是 ZIP（`PK\x03\x04`），`.pdf` 必须是 `%PDF-`
- 提取器：
  - `docx`：python-docx，提取段落 + 表格文本
  - `pdf`：pymupdf（fitz），提取文本层（保留页码标记 `\n--- Page {n} ---\n`）
  - `txt`/`md`：直读
- 护栏：提取文本过 `GuardrailPipeline`（`source=SourceTag.UNTRUSTED_MEDIA`），命中标记 `suspicious` 但不阻断（数据不是指令）
- 落盘：`data/projects/{project_id}/uploads/{upload_id}/{safe_filename}` + `extracted.txt`

### Step 2: 上传内容进检索索引

**文件**：
- 修改 `src/water_agent/ingestion/pipeline.py`：支持 `source_type="upload"` 的文本块入库
- 修改 `src/water_agent/retrieval/trivium.py`：支持按 `source_type` 过滤检索
- 创建 `tests/test_upload_retrieval.py`

**关键实现**：
- 项目级索引：`data/indexes/project_{project_id}.db`（与全局规范索引分开）
- 文本块 metadata：`source_type="upload"`、`upload_id`、`filename`、`page`（PDF 时）
- 检索时 `run_manager.py` 同时查全局规范索引 + 项目上传索引，结果合并去重

### Step 3: Word 输出生成

**文件**：
- 创建 `src/water_agent/docgen/docx_export.py`
- 创建 `tests/test_docx_export.py`

**接口契约**：
```python
def render_calculation_book_docx(
    title: str,
    input_params: dict[str, Any],
    calc_results: list[CalcResult],
    citations: list[CitationClaim],
    output_path: Path,
) -> Path: ...

def render_design_note_docx(
    title: str,
    sections: list[dict[str, str]],  # [{heading, content}]
    citations: list[CitationClaim],
    output_path: Path,
) -> Path: ...
```

**样式**（python-docx）：
- 页面：A4，上下左右边距 2.54cm
- 字体：正文 Calibri 11pt，标题 Calibri 14pt 加粗
- 行距：1.5 倍
- 页眉：项目名（左）+ 页码（右）
- 引用：文末自动汇总 `[1] GB50015-2019 3.2.1 ...`

### Step 4: 下载路由

**文件**：
- 创建 `src/water_agent/webapp/export_routes.py`
- 创建 `tests/test_export.py`

**接口契约**：
```python
# GET /api/runs/{run_id}/export?format=docx|xlsx
# Response 200: 文件流，Content-Disposition: attachment
# Response 404: run 不存在 / 无权限 / 无导出内容
# Response 400: format 不支持
```

**关键实现**：
- `format=docx`：从 run 的 `answer` + `calculations` + `citations` 生成
- `format=xlsx`：从 run 的 `calculations`（材料统计）复用 `materials/export.py`
- 权限：只有 run 的 owner 才能下载（`require_password_changed` + `get_visible_run`）

### Step 5: 前端集成

**文件**：
- 创建 `web/app/src/uploadApi.ts`
- 创建 `web/app/src/exportApi.ts`
- 修改 `web/app/src/Workbench.tsx`：输入框左侧加 📎 上传按钮
- 修改 `web/app/src/workspaceComponents.tsx`：消息卡片加下载按钮
- 创建 `web/app/tests/upload.test.mjs`、`web/app/tests/export.test.mjs`

**UI 行为**：
- 上传：点击 📎 → 文件选择器 → 上传中显示进度 → 成功后显示文件卡片（文件名 + 大小 + 预览）
- 下载：run 完成后，消息卡片底部显示「下载 Word」/「下载 Excel」按钮（按内容类型条件渲染）

## 验证命令（必须全部通过）

```bash
# 1. 后端测试
_venv/bin/pytest -q tests/test_upload.py tests/test_upload_retrieval.py tests/test_docx_export.py tests/test_export.py

# 2. 前端测试
cd web/app && npm run test && npm run typecheck

# 3. 完整门禁
bash scripts/ci_gate.sh

# 4. 冒烟（容器内）
docker compose up -d --build app
docker compose run --rm smoke
```

## 交付清单

- [ ] 上传路由：`.docx`/`.pdf`/`.txt` 上传、解析、护栏、落盘
- [ ] 检索集成：上传文本入项目级索引，可被 Agent 检索引用
- [ ] Word 输出：计算书/设计说明 .docx 生成
- [ ] 下载路由：`GET /api/runs/{run_id}/export`
- [ ] 前端：上传按钮 + 文件卡片 + 下载按钮
- [ ] 测试：后端 pytest + 前端 node --test + 冒烟
- [ ] 文档：`AGENTS.md` 补完成记录
- [ ] Commit：按「一步一 commit」纪律，每个 Step 一个 commit

## 开工前确认

- [ ] 我已阅读 `docs/design/p5-step-48-multimodal-upload-export.md` 完整 spec
- [ ] 我已阅读 `AGENTS.md` 的三条铁律和验证命令
- [ ] 我已阅读 `docs/design/10-sdd-harness.md` 的规格模板
- [ ] 我理解「不做 PDF OCR、不做全局向量存储、不做图片上传」的边界

**确认后，开始实现 Step 1。**
