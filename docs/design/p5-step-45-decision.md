# P5 Step 45 决策记录：memory_p0、安全测试、CI 与生产 Compose

日期：2026-09-13
范围：P5 任务书 §12 Step 45、§13、§14。**本步不新增产品功能**，只做发布门禁。

## 0. 审计结论（Step 35–44 现状）

Step 45 要求的产出在开工时**全部不存在**：`data/eval/memory_p0.jsonl`、
`water_agent.harness.memory_eval`、memory 失败检测器、前后端/数据库集成测试、
生产 Compose overlay、`scripts/backup.sh`、`scripts/restore.sh`、compose smoke、
镜像安全检查、GitHub Actions 覆盖（当时只有 `.github/workflows/p5-postgres.yml` 一个
workflow，且不跑 `ci_gate.sh` / ruff / mypy / 前端 / compose）。已存在并复用：
非 root（`USER wateragent`）、read-only rootfs、两处 healthcheck、digest 固定的基础镜像、
`migrate`/`bootstrap-admin` 的 `service_completed_successfully` 顺序、`/health/ready`
对 DB + 两套迁移 head 的校验、`P5_TEST_DATABASE_URL` 注入约定与既有 skip reason 文本。

### D1（最重要）：Step 43 只交付了前端半边

`web/app/src/memoryApi.ts` 与 `web/app/tests/memory_workspace.test.mjs`（Step 43 commit
`25e10c5`）已经按 `/api/memories*` 契约编写，但**后端没有任何该路由**——`src/` 里只有 3 个
`APIRouter`，`grep -rn "api/memories" src/` 为空。也就是说「长期记忆」面板在当前 HEAD 上
只能 404，Step 43 的验收（用户能完成"记住→召回→更正→使用→忘记"）**并未真正成立**。

Step 45 的验收又要求「前后端和数据库集成测试」以及 memory_p0 覆盖「忘记后 … API 不可得」，
这两条都必须建立在真实 API 上。因此本步补齐**服务端适配层** `webapp/memory_routes.py`：

* 它是**薄适配器**，不定义任何治理、记忆策略、排序或 Agent 装配；每个请求都转交
  `MemoryService` / `ExplicitMemoryWriteService` / `MemoryRecallService`；
* 行为体只来自 `require_password_changed` 解析出的 `UserRecord`，tenant 来自服务端默认值；
  **请求体与查询串里不读任何身份字段**，响应里也不出现 `tenant_id` / `owner_user_id`
  （有测试断言 `extra="forbid"` 拒绝 `user_id`/`principal` 注入）；
* 契约与已提交的前端 `memoryApi.ts` 逐字段对齐（`POST /api/memories/candidates`、
  `POST /api/memories`、`GET/PATCH/DELETE /api/memories/{id}`、`/events`、`/usage`）。
* 已知 P0 限制（写进模块 docstring）：待确认候选存于**进程内**（15 分钟 TTL），
  多 worker 部署下"预览"与"确认"必须落到同一 worker；持久化候选表不在 P0 范围。

## 1. memory_p0 设计

* **数据集**：`data/eval/memory_p0.jsonl`，42 条，覆盖任务书 §13.1 的 16 项（映射为 23 个
  `area`）。零宽字符用例在生成时即断言码点存在（字面零宽字符会在编辑中静默消失）。
  所有"凭据"用例的值都是显式合成的占位串（含 `EXAMPLE` / `NOT-A-REAL`），
  有测试断言数据集里不含 `AKIA`/`LTAI`/`ghp_`/`-----BEGIN`。
* **执行**：`harness/memory_eval.py` 每条用例一个隔离 SQLite 库 + 真实服务/真实 FastAPI
  `TestClient`，只注入用户、项目、输入与故障；**不伪造**召回命中、墓碑、SSE 帧或指标。
* **指标**：`harness/memory_metrics.py` 的 12 项 §13.2 指标**只从用例上报的计数器聚合**，
  绝不用"用例自己 passed"代替。缺某个必需计数器 = `unmeasured_surface` 违规，而不是记 0。
  速率类指标未被执行时取 `None`（unknown）并按 `not_measured` 违规处理。
* **检测器**：10 个 memory 失败模式（跨域召回、凭据落盘、注入落盘、忘记残留、未确认写入、
  指令面泄漏、数值未重算、预算溢出、越权重放、静默降级），各带 PAGE/TICKET 严重度与运维
  runbook；有 AST 静态断言**不得引入任何模型/HTTP 客户端**。
  证据事件在写入报告前统一脱敏（不出现正文、凭据、query）。
* **门禁**：`scripts/compare_memory_p0.py` 拒绝人工编写的报告（schema/generator/计数器
  重算一致性），并以"速率不得低于入仓基线、缺陷计数必须为 0 且不得增长、用例级不得回退"
  判定；`scripts/ci_gate.sh` 末尾接入。
* **`--release-gate` 只属于真正具备 Docker + PostgreSQL 的环境**：入仓基线是在**没有**
  compose 栈时生成的（41 通过 + 1 条已声明 `requires: docker` 的环境性 SKIP），因此
  `mem-p0-0042` 在发布门禁环境（`p5-compose` workflow）**会真正执行并通过**，head 是基线的
  超集。没有该栈的 `p5-ci` memory-p0 job **不带** `--release-gate`，改为断言"skip 恰好 ≤1
  且必须带 `requires`"，避免把环境性 SKIP 误判成固定红灯；比较器同时新增
  `undeclared_skip:<case>` 违规——任何未声明前置条件的跳过都会红灯。
* **`mem-p0-0042` 不再是一个永远 SKIP 的用例**：有栈时它真实执行 `scripts/backup.sh`，
  用 `scripts/backup_manifest.py` 校验摘要/大小、并把备份记录的行数与一次独立的实时读取
  比对（含 `forgotten_with_body = 0` 不变量）。破坏性的恢复半程仍只由
  `tests/test_backup_restore.py`（需 `WATER_AGENT_COMPOSE_TEST=1`）与 `p5-compose`
  workflow 执行——harness 不碰使用者的卷。

### 本步由门禁发现的真实缺陷（已修）

1. **PostgreSQL 召回在"无项目"时整体不可用**：`_PG_RECALL_SQL` 里 `:project_id IS NOT NULL`
   让 psycopg 无法推断 NULL 绑定参数类型 → `could not determine data type of parameter $N`
   → 每次个人会话召回都返回 `MEMORY_STORE_UNAVAILABLE`。改用
   `CAST(:project_id AS text)` 后又暴露第二层 `character varying = uuid`（列是 varchar(36)），
   最终两处都做 text 转换。**离线 SQLite 替代实现永远发现不了这个 bug**，是 compose smoke
   打真实 PostgreSQL 时才炸出来的。修复后有静态回归断言
   （`tests/test_memory_recall.py::test_step45_postgres_recall_casts_the_project_parameter`）
   + 在真实 PostgreSQL 上跑通原有 PG-gated 测试。
2. **未授权请求先泄漏配置**：`POST /api/sessions/{thread_id}/messages` 原先在会话归属校验
   **之前**检查模型配置，于是猜中/伪造 thread_id 的越权请求收到 424 `WEB_MODEL_NOT_CONFIGURED`
   而不是 404；并且会先插一条 queued run 行再失败。已改为「归属 → 配置 → 建 run」，
   memory_p0 `mem-p0-0009` 锁死。
3. **镜像因文件权限不可读**：本会话创建的新模块权限是 `0600`，容器以 uid 10001 运行时
   `import water_agent.webapp.memory_routes` 直接 `PermissionError`。compose 实测发现后
   统一改为 `0644`/`0755`——这正是"生产 Compose 必须实测"的价值。
4. **空记忆段仍渲染标题**：`render_memory_context` 在"没有任何条目进预算"时仍返回前言标题，
   会让模型以为存在一段长期记忆却没有内容。已改为返回空串，与"无 items → 无段"一致。

## 2. 生产部署

* `compose.production.yml`：overlay 只做加固，**不重复定义** secrets / `DATABASE_URL` /
  `depends_on` / 镜像 digest（有测试断言 overlay 里不出现这些键）。加入 CPU/内存/pids 限制、
  `no-new-privileges`、`stop_signal`/`stop_grace_period`、日志轮转，并把 app 命令改为带
  `--timeout-graceful-shutdown 30` 的 uvicorn。
* 优雅关闭：`RunManager.close()` 原先是 `shutdown(wait=False)` 直接返回，SIGTERM 时在跑的 run
  会被悄悄丢掉。现改为「先请求取消所有活跃 run → 有界等待其写出终态 → 报告仍未完成的 run」，
  FastAPI shutdown hook 用 `asyncio.to_thread` 等待，结果存 `app.state.shutdown_drain` 供断言。
* `smoke` 服务放在**基础** compose 且**不加 profile**：任务书验收命令是不带 `-f` 的
  `docker compose run --rm smoke`，而 Compose 拒绝运行 profile 未激活的服务。它是一次性
  HTTP 检查（`python -m water_agent.webapp.smoke`），放在 `src/` 下而非 `scripts/`，
  因为 `.dockerignore` 默认拒绝一切、只重新包含 `src/`。
* 四个构建服务显式声明 `image: water-agent-*:local`，使 `scripts/check_image.sh` 与 CI
  有稳定的目标名（否则 compose 派生的镜像名不带 `:local`，检查会静默 SKIP 而不是检查）。
* `scripts/backup.sh`：`pg_dump --format=custom` + 应用卷 tar + `sha256sums.txt` +
  `manifest.json`（**只记计数与摘要**，不含正文/凭据）+ `db_counts` 行数快照。
* `scripts/restore.sh`：校验摘要 → 空目标或显式 `--force` → **先生成当前状态备份** →
  停 app → `pg_restore --clean --if-exists --exit-on-error` → 恢复卷 → 起 app →
  用 `scripts/backup_manifest.py` 比对 8 个行数与「忘记记忆不得带正文」不变量。
  校验与比对逻辑抽成可导入模块，因此**无需 Docker 也能单测**。
* `scripts/check_image.sh`：非 root、镜像内无 `.env`/`secrets`/`*.pem|*.key`/构建上下文泄漏、
  凭据形态字面量扫描、可选 trivy/grype/docker scout。只打印**命中路径与规则名**，绝不回显命中文本。

### D2：为什么 `secret_pattern_scan` 需要一条枚举式豁免

`src/water_agent/runtime/governance/scenarios.py` 里有一条**必须长得像真实 Key** 的红队夹具
`sk-abcdef0123456789abcdef0123456789`（改掉它会削弱那条治理测试的意图）。镜像扫描因此做了
唯一的、**按精确字面量**的豁免：只有当某文件里所有 `sk-` 形态命中**都恰好等于**该公开夹具串时
才判干净；任何其他同形状字面量仍然 FAIL。豁免不使用"含 EXAMPLE 就放过"这类可被绕过的启发式。

## 3. 本机环境的两处硬事实（不伪装）

1. **`docker compose build` 在本机不可用**：Docker Desktop 29.7.2 的 buildx bake 通道报
   `failed to dial gRPC … header key "x-docker-expose-session-sharedkey" contains value with
   non-printable ASCII characters`（`docker buildx bake -f <file>` 直接复现，与仓库无关）。
   等价路径 `docker build -t water-agent-app:local .` 成功，四个服务镜像由同一构建重新打标签，
   随后 `docker compose up -d` / `run --rm smoke` / `down` 全部按验收执行并通过。
   CI 在 GitHub runner 上不受此本机缺陷影响。
2. **本机镜像源只有 tag 通道可用**（daocloud 镜像），digest 直拉会挂；基础镜像均先按 tag 拉取，
   其 digest 与仓库固定的 digest 一致。

## 4. 验证证据（本步实测）

| 命令 | 结果 |
|---|---|
| `pytest -q tests/test_memory_web_api.py` | 15 passed |
| `pytest -q tests/test_memory_prompting.py` | 6 passed |
| `pytest -q tests/test_web_sse.py` | 12 passed |
| `pytest -q tests/test_web_authorization.py tests/test_web_sessions.py` | 36 passed, 1 skipped（PG-gated） |
| `pytest -q tests/test_memory_eval.py` | 18 passed |
| `pytest -q tests/test_backup_restore.py` | 15 passed, 1 skipped（compose-gated）；`WATER_AGENT_COMPOSE_TEST=1` 时 16 passed |
| `pytest -q tests/test_web_deployment_overlay.py` | 12 passed |
| `memory_eval`（无 compose 栈，本机默认门禁） | 42 条：通过 41、失败 0、SKIP 1（`mem-p0-0042` 声明 `requires: docker`）、12 项硬指标全达标 |
| `memory_eval --release-gate`（栈运行时） | 42 条：**通过 42、失败 0、SKIP 0**，退出 0 |
| `python scripts/compare_memory_p0.py` | 12/12 OK，退出 0；对篡改报告退出 2 |
| `bash scripts/ci_gate.sh` | 通过（含 memory_p0 与基线对比，既有套件无回退） |
| 前端 `typecheck` / `test` / `build` | 全部通过（11 个前端测试） |
| 真实 PostgreSQL（compose，`P5_TEST_DATABASE_URL`） | 89 passed，0 skipped |
| `scripts/backup.sh` → 全新 volume → `scripts/restore.sh` | 通过；8 项行数一致、`forgotten_with_body=0`；API 复核忘记后正文为空 |
| `scripts/check_image.sh water-agent-app:local` | `ok: true`（非 root、无密钥材料、无上下文泄漏、docker scout 跑通） |
| `docker compose up -d` → `run --rm smoke` → `down` | smoke `ok: true`，20/20 检查通过 |

## 5. 残余风险与不作为

* `gold_exact_recall_rate` 用合成关键词 gold，**不代表**自然语言召回质量；语义召回属 Step 46。
* 写入护栏是确定性正则，**必然存在未覆盖的改写变体**，不得声称"能拦住任意注入"。
* 待确认候选存于进程内：多 worker 部署需要持久化候选表（P0 未做，已在模块 docstring 记录）。
* `enabled: true`（重新启用已过期记忆）返回 409 `MEMORY_STATUS_TRANSITION_INVALID`：
  P0 状态机是前向的，不做静默复活。
* `PATCH /api/memories/{id}` 的更正把"用户点确认"表达为一次带 `expected_revision` 前置条件的
  显式请求，服务端仍走 candidate→confirm 两段；未确认的 candidate 从不落盘（有指标与测试锁死）。
* 未做（明确超出本步）：公网反向代理的真实证书签发、多架构镜像 smoke、真实模型下的端到端 run
  质量（`WEB_MODEL_NOT_CONFIGURED` 时 smoke 记录为 note 而非通过）。
