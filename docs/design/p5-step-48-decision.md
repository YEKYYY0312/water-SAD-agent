# Decision: P5-S48 多模态文件输入与输出（后端 Step 1–4）

> 状态：**后端 Step 1–4 已交付**（2026-09-28）；前端 Step 5（📎 上传按钮 + 文件卡片 + 下载按钮）**未做**，按用户拍板留到下一轮。
> 规格：`docs/design/p5-step-48-multimodal-upload-export.md`；执行 prompt：`docs/execution-prompts/P5-S48-multimodal-upload-export.md`。

## 用户拍板（本轮开工前）

| 问题 | 决定 |
|---|---|
| 上传内容进检索的实现 | **PostgreSQL 全文 + 字面匹配**，不做 Trivium 项目级向量索引 |
| PDF 提取库 | **pypdf 默认（BSD-3）**，pymupdf 作为 `pdf-enhanced` extra（AGPL-3.0，显式 opt-in） |
| 导出边界 | **只服务进程内 run**，不给 `runs` 表加结果列 |
| 交付范围 | 本轮只做后端 Step 1–4，前端下一轮 |

（spec 里更早拍板的两条仍然有效：上传入库长期保存、Word 输出优先。）

## 与执行 prompt 的偏离（按代码与容器现实修正）

1. **`SourceTag.UNTRUSTED_MEDIA` 不存在**。用 `SourceTag.RETRIEVED_FILE`（trust=10，不在 `INSTRUCTION_BEARING_SOURCES`），与 `read_drawing` 同一条隔离防线。新增标签会动 `SOURCE_TRUST_LEVEL`，没有必要。
2. **落盘位置不是 `data/projects/…`、索引不是 `data/indexes/project_*.db`**：镜像把 `/app/data` 打成只读（`chmod -R a-w`）。文件落 compose 卷 `app_uploads:/var/lib/water-agent/uploads`（spec D1 本身写的就是它），`documents/storage.py::resolve_upload_dir()` 照 `resolve_trace_dir()` 三层解析（显式 → `WATER_AGENT_UPLOAD_DIR` → 卷（存在且可写）→ `data/uploads`）。
3. **不改 `ingestion/pipeline.py` / `retrieval/trivium.py`**：Trivium 路径需要真实 embedding（无 Key 会退到 `mock_embed`，向量无意义但 `status` 仍 succeeded）且索引目录只读。改为 `uploads` + `upload_chunks` 两张表 + 迁移 `0005_step48_uploads`（PG-only GIN 索引，照 0003 形状）。
4. **上传内容走提示段，不做模型可调用工具**：工具 schema 预算 4370/4500，headroom 130，放不下 `read_upload`。召回结果进 `PromptAssembler` 新增的 `upload_context` 段。

## D1 上传：顺序即安全属性

`documents/service.py::ingest_upload` 独占这一顺序：

名称/扩展名白名单/魔数 → 提取 → **整篇 `sanitize_for_persistence` 一次** → 切块 → **逐块** `check_retrieved_content` → 落盘 → 一个事务写行 + 块。

- 整篇脱敏在切块**之前**：跨块边界的凭据才拦得住。
- 注入检测在**块**粒度：护栏 `MAX_CONTENT_BYTES`=200 KB，整篇过检会把正常的大设计文档整份拒掉（有测试锁死）。
- 注入命中**只标 `suspicious`，不阻断**（上传文本是数据）；`suspicious`（注入）与 `redacted`（PII/凭据被剥离）分开上报，处置不同。原始文件本身完整保留在卷上，脱敏只作用于提取文本与入库块。
- 413 在**读取过程中**产生（`_read_bounded`），不先缓冲整个 body；路径穿越**拒绝而非清洗**。
- 扫描件 PDF：空页不写页码标记，`extracted_text_length` 如实为 0（spec R1）。
- 实测缺陷：`UploadRepository.create` 必须 `flush()`——两表间无 `relationship()`，SQLAlchemy 按表名排序 INSERT，`upload_chunks` 排在 `uploads` 之前，FK 报错。
- `python-multipart` 是运行时必需依赖：缺它 FastAPI 在**请求期**才抛 RuntimeError。

路由：`POST /api/uploads`（CSRF，viewer 403，非成员项目 404 且不解析字节）、`GET /api/uploads`、`GET /api/projects/{id}/uploads`、`GET /api/uploads/{id}`、`DELETE /api/uploads/{id}`（owner-only，行与块先删、盘上目录后删）。

## D2 召回：确定性字面匹配，不是语义检索

- 匹配单元 = **中文字二元组 + 西文/数字词元**（`DN300`、`GB50015-2019` 保持完整）。命中比例 ≥0.3，且多词查询至少命中 2 个才算相关——宁漏召不塞无关上下文。
- **没有复用 `memory/recall.py::_literal_keyword_score`**（原计划复用）：实测它按空白切词，一句不带空格的中文问题整句成为一个词、必须原样出现在文档里才命中——对"上传文档后用自然语言问它"等于永不命中；PG 的 `'simple'` tsvector 也不切中文。memory 那条打分不改（有 `memory_p0` 入仓基线守着，且是另一个问题）。
- PG 路径：FTS 或 `unnest(:terms)` 字面命中做**预筛**，最终相关性与 SQLite 路径共用 Python 侧 `_upload_score`。`project_id` 两处 `CAST(... AS text)` 与 Step 45 同一教训（静态锁死 + PG 门控测试实跑）。
- 注入：`RunManager._execute` 在 memory recall 之后召回；只在有命中时才向 `aask` 传 `upload_context`（`AgentSessionFactory` 是协议，既有实现不认识该关键字）。SSE `upload_recall` 事件**只含计数与 upload_id**。召回失败 → `warning UPLOAD_RECALL_DEGRADED` 继续作答（上传资料不是跨租户面，降级即可）。特性开关 `UPLOAD_RECALL_ENABLED=false` 只关召回。
- 装配期只接受 `RETRIEVED_FILE` 信封；空召回不渲染孤立标题段；`PROMPT_TEMPLATE_VERSION` → `p5-step48-upload-context-2026-09-27`（缓存键失效因子）。

## D3 Word 输出

`docgen/docx_export.py` 是 `RenderedDoc` 的渲染器，不是第二个引擎：先 `render_docgen_doc()`，写盘前 `check_docgen_self_check()`（含 `validate_citation_policy`），**不通过不写文件**。多个 `CalcResult` 同名数值冲突 → 报错（不后者覆盖）。`CalcResult` 不带推导过程，故"计算过程"只列程序化结果与口径备注，**不生成推导步骤**。样式：A4 / 2.54 cm / Calibri 11pt + 显式 `w:eastAsia` / 标题 14pt 加粗 / 1.5 倍行距 / 页眉项目名 + PAGE 域 / 文末 `[n] source_id`。

## D4 下载

`GET /api/runs/{run_id}/export?format=docx|xlsx`：format（400）→ 可见性（404）→ 归属（项目成员非发起人 403）→ 进程内结果（404 `WEB_EXPORT_UNAVAILABLE`）。仅 succeeded 可导出。数据只取程序化工具产物：`calc_velocity` 结果 + 按 `tool_call_id` 配对的数值调用参数；`material_stats` 取**最后一张**表（累加即重复计量），任一行畸形整表丢弃。答案含未声明条款号 → 422 `WEB_EXPORT_SELF_CHECK_FAILED`。`Content-Disposition` 同带 ASCII `filename=` 与 RFC 5987 `filename*=`；暂存文件响应后删除。

## 验证证据

- `tests/test_upload.py` 26、`tests/test_upload_retrieval.py` 25、`tests/test_docx_export.py` 13、`tests/test_export.py` 13。
- 真实 PostgreSQL 18.2（compose 同 digest，临时容器）：迁移 0005 升级成功、`ix_upload_chunks_content_fts` 建出；`P5_TEST_DATABASE_URL` 门控的 7 个 PG 套件 151 passed / 0 skipped。
- `bash scripts/ci_gate.sh` 全通过（全量 pytest 2369 passed / 30 skipped + ruff + mypy + 8 套 harness + 基线对比）。
- 镜像内：`docker build` 后四个 service tag 指向同一新镜像，隔离 compose 项目 `smoke ok=true passed=30 failed=0`（含 4 项新检查与 `migration_head=0005_step48_uploads`）；`check_image.sh` PASS（漏洞扫描器无登录 SKIP）。

## 已知边界（不得越过的声明）

- **前端未做**：工作台里没有 📎 与下载按钮，"Web 端能上传/下载"目前只对 API 成立。
- **导出只服务进程内 run**：重启后 404，与 `/trace` 同限制。
- **召回是字面匹配**：SQLite 下只有字面分，PG 多一层 FTS 排序加成；**不得声称支持任意中文表达的召回**，也没有任何召回质量评测集（机制测试 ≠ 质量证据）。
- **Trivium/向量召回 blocked**：需 DASHSCOPE_API_KEY + 可写索引卷 + 与 curated 索引维度一致。
- 计算书只覆盖 `calc_velocity` 与 `material_stats` 两类程序化产物（`_calculation_payload` 的既有白名单）；其余工具的数值不进导出。
- 无病毒扫描（spec 风险自留）；uploads 卷无 retention，长期运行会无界增长。
- pymupdf 路径是 AGPL，启用即承担相应义务。
- spec R3 的"24 小时 TTL"与"入库长期保存"拍板冲突，**已作废**。
