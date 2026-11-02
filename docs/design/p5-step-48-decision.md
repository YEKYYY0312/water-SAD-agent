# Decision: P5-S48 多模态文件输入与输出（后端 Step 1–4）

> 状态：**Step 1–5 全部交付**（2026-09-28）。前端 Step 5 同时按用户参考图把工作台整体改为浅色设计。
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
- 413 在**读取过程中**产生（`_read_bounded`），不先缓冲整个 body；路径穿越**拒绝而非清洗**。（2026-10-01 更正：这只对单文件精确上限成立，HTTP 层 FastAPI 在鉴权前已收完整个 multipart，见文末 D5。）
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

- 前端 Excel 按钮以「本次 `material_stats` 成功」为显示条件；后端若因材料表畸形丢弃整表，点击后返回 404 并原地提示。
- **导出只服务进程内 run**：重启后 404，与 `/trace` 同限制。
- **召回是字面匹配**：SQLite 下只有字面分，PG 多一层 FTS 排序加成；**不得声称支持任意中文表达的召回**，也没有任何召回质量评测集（机制测试 ≠ 质量证据）。
- **Trivium/向量召回 blocked**：需 DASHSCOPE_API_KEY + 可写索引卷 + 与 curated 索引维度一致。
- 计算书只覆盖 `calc_velocity` 与 `material_stats` 两类程序化产物（`_calculation_payload` 的既有白名单）；其余工具的数值不进导出。
- 无病毒扫描（spec 风险自留）；uploads 卷无 retention，长期运行会无界增长。
- pymupdf 路径是 AGPL，启用即承担相应义务。
- spec R3 的"24 小时 TTL"与"入库长期保存"拍板冲突，**已作废**。

## D5 请求体上限：在 FastAPI 之下、鉴权与读 body 之前（2026-10-01 补）

**缺陷（已复现）**：FastAPI 先 `await request.form()`（`fastapi/routing.py::get_request_handler`）再 `solve_dependencies`，而鉴权与 CSRF 都是依赖。所以 D1 的"413 在读取过程中产生"只对 `_read_bounded` 读已 spool 好的 `UploadFile` 成立：路由运行之前，Starlette 早已把整个 multipart 收完，文件部分写进 `SpooledTemporaryFile`（超过 1 MB 落 `/tmp`）。修复前实测（30 MiB 文件）：匿名、不带 cookie 与 CSRF → 403 `AUTH_CSRF_INVALID`，但已收 31457280 字节并写进临时文件；已登录 → `_read_bounded` 返回 413 时同样已收满。真实 uvicorn + curl 对照：修复前先回 `100 Continue`，客户端上传完 31457478 字节才拿到 403。真正起限流作用的只有 Caddy 站点级 `max_size 16MB`；基础 compose 直接暴露 app:8000，没有任何上限；容器 `/tmp` 是 tmpfs，推断计入容器内存限额，并发匿名上传可把 app 顶到 OOM。

**新的顺序**（越靠前越先拒）：

1. 反代（仅生产 profile）：`deploy/Caddyfile` 对 `/api/uploads`、`/api/uploads/*` 单设 `request_body max_size 10304KiB`（`caddy adapt` 实测解析为 10551296 字节，与应用层同值）；站点级 16MB 不变，两条都生效，上传路径取更严者。
2. `webapp/body_limit.py::UploadBodyLimitMiddleware`（2026-10-02 起兼管其余写请求，更名 `RequestBodyLimitMiddleware`，见 D6；纯 ASGI，`create_app` 注册，只作用于上述前缀；`POST /api/uploads` 是全应用唯一解析表单的路由，测试从 OpenAPI 读出并锁死）：
   - 声明的 `Content-Length` 超上限 → 读 body 之前直接 413：`receive` 一次都不调，服务器因此也不发 `100 Continue`（curl 实测上传 0 字节）；
   - 没有 `Content-Length` 或分块传输 → 包装 `receive` 累计已收字节，越过上限的那条消息即中止。应用对中断读取的反应（FastAPI 的 400 "error parsing the body"）被丢弃，改发同一个 413；Starlette 解析失败时会关闭已建的临时文件。若响应已经开始（上传路由不会），不再发第二个响应，异常上抛、由服务器断开连接；
   - 413 的响应体与路由层逐字节相同（信封、`UPLOAD_TOO_LARGE`、文案，有测试锁死），另带 `Connection: close`：剩余 body 不再读取，连接不能复用，也不让服务器替客户端无限排空。
3. 鉴权 / CSRF 依赖 → `_read_bounded`（单文件精确上限 `MAX_UPLOAD_BYTES`）→ `ingest_upload`（D1 顺序不变）。

**上限取值**：`UPLOAD_BODY_LIMIT_BYTES = MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES`（10 MiB + 64 KiB），不另起数值。余量只容纳 boundary、part 头（含文件名）与 `project_id` 字段，真实上传不到 2 KB；文件略超 10 MiB 而仍在余量内时，由 `_read_bounded` 返回同一个 413；正好 10 MiB、带长中文文件名的文件照常通过（有测试）。

**残余风险**：

- **反代仍是第一道防线**：连接数、请求速率、慢速发送都不归本中间件管；基础 compose 没有反代，只剩这一层。
- 上限以内的匿名请求仍在鉴权**之前**被完整解析与 spool（FastAPI 的顺序没变，只是现在有界）：每个请求最多约 10 MB 临时文件，并发数 × 10 MB 仍可能顶到内存限额。要消除需在 ASGI 层先做会话与 CSRF 校验，本次未做。（2026-10-02 已修：ASGI 层预检，见 D6。）
- 分块请求在越限之前已收的字节（不超过上限 + 一条 ASGI 消息）照样进了 spool，随后被关闭。（2026-10-02：现在只剩通过鉴权预检的请求会走到这一步，见 D6。）
- 其余 JSON 路由同样先 `await request.body()` 再鉴权，本次**未加**上限：不落盘，但整包进内存（curl 实测 30 MB 打 `/api/sessions`，收满后才回 422）。（2026-10-02 已加 1 MiB 上限，见 D6。）
- Caddy 越限时的响应由 Caddy 生成、不经过应用，不是本应用的 JSON 信封（具体状态码未实测）；前端已在本地按 10 MB 预检，正常界面操作到不了这里。
- 证据：`tests/test_upload_body_limit.py` 11 条（含参数化），修复前全部失败、修复后全部通过。

## D6 鉴权之前不读、少读 body：上传预检与通用请求体上限（2026-10-02 补）

**缺陷 1（已复现）**：D5 只给上传请求体封了顶，FastAPI「先 `request.form()`、后解依赖」的顺序没变。修复前在 ASGI 层计数实测：匿名 5 MiB 上传——不带 cookie 与 CSRF，或自造一对相同的 `wa_csrf` cookie 与 `X-CSRF-Token`——分别回 403 `AUTH_CSRF_INVALID` / 401 `AUTH_SESSION_INVALID`，但应用已从 `receive` 读完 5243036 字节，其中 5242880 字节写进滚到磁盘的临时文件（容器里是 tmpfs）。已登录却缺 CSRF 头、会话过期或不存在、账号停用、须改密的上传同样先收满再拒。每个匿名请求仍能让服务端收下并 spool 约 10 MB，并发时照样能顶到内存或磁盘限额。

**缺陷 2（已复现）**：JSON 路由同样先读后鉴权——FastAPI 在解依赖之前 `await request.body()`，整包拼进内存——而且没有任何上限。修复前实测（ASGI 层计数）：30 MiB 打 `POST /api/sessions`，Content-Length 与分块两种都读满 31457280 字节后才回 422 `WEB_INVALID_REQUEST`；`POST /api/auth/login`、`PUT /api/settings/credentials/{name}`、`PATCH`/`DELETE /api/memories/{id}` 同样读满再回 422；`POST /api/drawings/generate` 在鉴权之后自己 `await request.body()`，已登录的分块 30 MiB 读满后回 422 `GEN_INVALID_INPUT`。

**新的顺序**（越靠前越先拒，也越便宜）：

1. 反代（仅生产 profile）：同 D5；站点级 16MB 不变，非上传写请求在应用层先撞到下面的 1 MiB。
2. `body_limit.RequestBodyLimitMiddleware`（即 D5 的 `UploadBodyLimitMiddleware`，因不再只管上传而更名）：两个上限。
   - 上传前缀（任何方法）：`UPLOAD_BODY_LIMIT_BYTES`，413 `UPLOAD_TOO_LARGE`，同 D5。
   - 其余写方法（`WRITE_METHODS` = POST/PUT/PATCH/DELETE，含形似上传前缀的路径）：`REQUEST_BODY_LIMIT_BYTES` = 1 MiB，413 `WEB_REQUEST_TOO_LARGE`。此前没有 WEB 层的 413 码（`UPLOAD_TOO_LARGE` 属上传、文案写死 10 MB，不复用），新码的信封与其他 `WEB_*` 相同，文案 `Request body is too large`，带 `Connection: close`。机制与 D5 相同：声明长度超限读 body 之前即拒；分块按累计字节在越限那条消息截断，路由对中断读取的反应被丢弃——自己读 body 的出图路由同样被截断。
   - 读方法（GET/HEAD/OPTIONS）不受影响：没有任何读路由声明 body（测试从 OpenAPI 读出并锁死），FastAPI 也不会为没有 body 参数的路由读 body。
   - 只看一个请求头，不查库。
3. `body_limit.UploadPreauthMiddleware`（新增，注册在上一层之内）：只作用于 `POST /api/uploads`——全应用唯一在依赖之前解析 body 的路由。按 FastAPI 的求解顺序（路由级 `dependencies` 在前、参数依赖在后）直接调用路由声明的两个依赖函数 `validate_csrf` → `require_password_changed`（`UPLOAD_AUTH_DEPENDENCIES`；测试读路由的 dependant，锁死二者一致），header 与 cookie 用 Starlette 同一套解析，会话查询照 FastAPI 放进线程池。依赖抛出的结构化 `HTTPException` 按应用异常处理器的信封渲染：状态码、错误码、文案逐字节相同——8 种拒绝（缺 cookie 与 token、只有 cookie、token 不匹配、无会话、会话不存在、会话过期、账号停用、须改密）逐一与去掉预检的同一应用对照，响应体与响应头都相同，过期会话照样被删除。`receive` 一次不调，不解析、不 spool。
   - **只拒绝、不授权**：不拒绝的请求原样下传，不往 scope 写任何身份；路由的依赖照常再解析一次会话，仍是唯一权威（合法上传的会话查询由 1 次变 2 次，有测试）。viewer 403、项目成员资格仍由路由在读 body 之后判断。路由依赖被 `dependency_overrides` 替换时预检让路，不拿原函数去判。
   - `must_change_password`：上传路由挂的是 `require_password_changed`，预检照它回 403 `AUTH_MUST_CHANGE_PASSWORD`。
   - 会话库故障：依赖抛出的非 `HTTPException` 照常上抛，与在路由里一样落 500 `WEB_INTERNAL_ERROR`（fail closed，只是不再先解析 body，有测试）。
   - JSON 路由**不做**这种预检：各路由的依赖链不同（登录无依赖、改密用 `require_authenticated`、管理员用 `require_admin`……），逐条重放就是在 ASGI 层再写一遍授权；它们读到的字节已被第 2 步封顶在 1 MiB。
4. 分块计数截断（D5）→ 路由依赖 → `_read_bounded` → `ingest_upload`，均不变。

**请求体上限取值**：取当前最大的合法 JSON 请求体，再留至少 4 倍余量。按各路由请求模型的声明上限计算最坏编码（每个字符按 12 字节计：astral 字符被客户端转义成代理对 `\uXXXX\uXXXX` 时仍只算 1 个字符；测试从 OpenAPI 与 `GenerationRequest` 的 JSON Schema 重算并锁死）：

| 路由 | 请求模型 | 声明上限 | 最坏编码 |
|---|---|---|---|
| `POST /api/sessions/{id}/messages` | `MessageRequest` | message 16000 字符 + 附件 id ≤5 | 192227 B |
| `POST /api/memories/candidates` | `CandidateRequest` | content 4000 + semantic_key 255 | 51274 B |
| `PUT /api/settings/credentials/{name}` | `CredentialRequest` | value 4096 | 49165 B |
| `PATCH`/`DELETE /api/memories/{id}` | `PatchRequest` | content 4000 | 48072 B |
| `POST /api/runs/{id}/feedback` | `FeedbackRequest` | comment 4000 | 48037 B |
| `POST /api/memories` | `ConfirmRequest` | hash 128 + key 255 | 4809 B |
| `POST /api/sessions`、`/api/projects/{id}/sessions` | `SessionRequest` | title 240 | 2893 B |
| `POST /api/projects` | `ProjectRequest` | name 200 | 2412 B |
| `POST /api/drawings/generate`（路由自己读 body） | `GenerationRequest` | 枚举、数值、riser_position 16 | 700 B |
| 登录 / 改密 / 管理员建用户 / 重置密码 | `_LoginBody` 等 4 个 | 未声明（用户名列 varchar(128)、密码） | 无上限 |

最大的是聊天消息：最坏 192227 字节（约 188 KiB），浏览器发中文时约 48 KB（`JSON.stringify` 不转义 CJK，每字 3 字节）。1 MiB 是最坏情况的 5.4 倍、常见情况的 20 倍以上，也只有上传上限的十分之一。登录等 4 个 body 没有声明字段上限，1 MiB 现在就是它们的上限（Argon2 的耗时由 memory/time cost 决定，口令长度只进初始哈希）。

**连接是否保持**：所有 413 都带 `Connection: close`（剩余 body 无界，不替客户端排空）。预检拒绝同样不读 body，但声明了长度时剩余部分已被第 2 步封顶，这时**不带** `close`：由 uvicorn 把剩余字节读出丢弃、连接照常复用。依据是实测（真实 uvicorn，macOS 回环，匿名 5 MiB 上传）：httpx 各 20 次，拒绝带 `close` 时 7 次拿到的是 `ReadError`（连接被重置，响应丢失）而不是 403/401，不带时 20/20 正确；先写完再读的原始 socket，带 `close` 时写入报 `BrokenPipeError`/`ConnectionResetError`（macOS 上仍读得到 403），不带时 10/10 无错；curl 默认（`Expect: 100-continue`）上传 0 字节即拿到 403，去掉 `Expect` 后读到提前到达的响应即停止发送（约 128 KB）；拒绝之后同一连接能继续服务下一个请求。没有声明长度（分块）时剩余部分无界，拒绝仍带 `close`。

**残余风险**：

- JSON 路由在鉴权之前仍会把 body 读进内存，只是现在每请求不超过 1 MiB（匿名也一样）；并发数 × 1 MiB 仍归反代的连接与速率限制管。
- 预检拒绝后，声明长度以内的剩余字节仍由 uvicorn 从 socket 读出丢弃：不进内存、不落盘，但占带宽，每请求不超过上传上限。
- 会话查询仍对每个带会话 cookie 的匿名上传在读 body 之前发生一次（与修复前路由里那次相同；不带会话 cookie 的请求不查库）。
- 通过鉴权的上传（含 viewer）仍在路由运行前被完整解析与 spool——合法上传本来就要付这笔成本，上限见 D5。
- 所有 413 都依赖客户端在连接关闭后仍去读响应；httpx 这类先写完再读的客户端可能只看到连接重置（同上实测），反代后面由 Caddy 的连接处理决定，未实测。
- 上限是统一的，不按路由区分：将来哪个 JSON 请求体合法地超过 256 KiB，上限测试会变红，须重新评估这个数，而不是悄悄抬高。前端未对 16000 字的消息上限做本地校验，粘贴超过约 1 MiB 的文本时拿到的是 413 而不是 422，文案回落到服务端 message。
- `WEB_REQUEST_TOO_LARGE` 前端没有专门文案，回落到服务端 message；正常界面操作到不了这里。
- 证据：`tests/test_upload_body_limit.py`。缺陷 1 新增 20 条（含参数化），修复前全部失败、修复后全部通过；D5 的两条随之调整：匿名分块大请求现在在预检就被拒（spool 测试改为断言 403 且不 spool，修复前失败），分块截断测试只保留已登录一例。缺陷 2 新增 11 条（含参数化），D5 的「上传前缀以外不受影响」改写为「上传前缀以外用请求上限」，两条中间件单元测试按两种上限参数化（共 +3 条），修复前全部失败、修复后全部通过。
