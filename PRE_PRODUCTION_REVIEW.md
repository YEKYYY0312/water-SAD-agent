# 给排水 Agent 上线前 Review 报告
**日期**: 2026-09-18  
**检查范围**: 全项目生产就绪度评估

---

## ✅ 已就绪的核心能力

### 1. 代码质量与测试
- ✅ **229 条单元测试全部通过**（9.92s）
- ✅ **门禁稳定**：`scripts/ci_gate.sh` 包含 pytest + ruff + mypy + 5 套评测 + baseline 对比
- ✅ **静态检查清零**：mypy 0 错误，ruff 0 警告
- ✅ **评测覆盖**：
  - `rag_p0`: 11/12 (91.7%)
  - `calc_p0`: 28/28 (100%)
  - `docgen_p0`: 11/11 (100%)
  - `material_p0`: 10/10 (100%)
  - `governance_p0`: 32/32 (100%)
  - `memory_p0`: 41/42 (97.6%)
  - `multi_agent_p0`: 12/12 (100%)

### 2. 部署基础设施
- ✅ **Docker Compose 配置完整**
  - `compose.yml`: 基础服务（postgres / migrate / app / smoke / reverse-proxy / local-model）
  - `compose.production.yml`: 生产加固（资源限制 / security_opt / 日志轮转）
- ✅ **Dockerfile 多阶段构建**
  - 前端：Node 26.8.1 + Vite 8
  - 后端：Python 3.12.14 + uv 0.12.6
  - 运行时：非 root 用户 (wateragent:10001)
- ✅ **反向代理**：Caddy 2.11.1 + CSP + HSTS + CORS
- ✅ **健康检查**：`/health/live` + `/health/ready`
- ✅ **Secrets 管理**：Docker secrets（`postgres_password` / `admin_password`）

### 3. 数据库与迁移
- ✅ **PostgreSQL 18.2** + psycopg3
- ✅ **Alembic 迁移**：4 个版本（0001 → 0004_step44_workspace）
- ✅ **迁移验证**：compose smoke 断言 `migration_head == 0004_step44_workspace`
- ✅ **备份恢复**：`scripts/backup.sh` + `scripts/restore.sh`

### 4. 治理与安全
- ✅ **护栏体系**：4 层（capability / 参数大小 / allowlist / 注入检测）
- ✅ **工具调用五层防御**：护栏 → schema → guard → execute → timeout
- ✅ **长期记忆隔离**：user/project 硬隔离 + `MemoryWriteGuard`
- ✅ **SSE 续传**：`Last-Event-ID` + 逐事件复核授权
- ✅ **越权防护**：32/32 治理评测通过

### 5. 运维工具
- ✅ **Smoke 测试**：20 项 HTTP 检查（含记忆链路 + 越权拒绝）
- ✅ **镜像检查**：`scripts/check_image.sh` 验证镜像安全（非 root / read-only）
- ✅ **备份脚本**：pg_dump + 元数据 JSON
- ✅ **恢复脚本**：带 `--force` 保护

---

## 🟡 待完善项（上线前必须处理）

### P0 - 阻塞上线

#### 1. **API 密钥泄漏风险** 🔴
**问题**：`.env.local` 和 `.env` 包含真实 API 密钥且可能已入库
```bash
# 当前状态
.env.local:1:export DEEPSEEK_API_KEY="sk-your-deepseek-key"
.env.local:2:export DASHSCOPE_API_KEY="sk-your-dashscope-key.EXRHIIM.R2tf..."
.env:3:DEEPSEEK_API_KEY=sk-your-deepseek-key
```

**风险**：
- 密钥可能已提交到 git 仓库
- 任何克隆仓库的人都能读取
- 密钥可能在公开仓库中泄漏

**修复**：
1. **立即轮换密钥**（在 DeepSeek / 百炼控制台生成新密钥）
2. 确认 `.env` 和 `.env.local` 在 `.gitignore` 中
3. 检查 git 历史：`git log -p -- .env .env.local`
4. 如已入库，执行 git 历史清理：
   ```bash
   git filter-branch --force --index-filter \
     'git rm --cached --ignore-unmatch .env .env.local' \
     --prune-empty --tag-name-filter cat -- --all
   ```
5. 创建 `.env.example` 模板（只含占位符）
6. 文档中说明密钥配置方式

#### 2. **部署文档缺失** 🟡
**问题**：无 `DEPLOY.md` / `INSTALL.md` / 运维手册

**缺失内容**：
- 生产环境最低配置要求（CPU / 内存 / 磁盘）
- 密钥配置步骤（DEEPSEEK / DASHSCOPE）
- 首次部署流程（secrets 生成 → compose up → smoke 验证）
- 域名与证书配置（WEB_DOMAIN / CADDY_EMAIL）
- 备份策略与恢复演练
- 日志查看与故障排查
- 监控指标接入（可选 OTel）
- 升级流程（迁移 → 重启 → smoke）

**修复**：创建 `docs/DEPLOYMENT.md`，包含上述章节

#### 3. **知识库数据缺失** 🟡
**问题**：README.md 声称 `data/knowledge/` 有 221 个 md（40MB），但目录不存在

**当前状态**：
```bash
$ ls -la data/knowledge/
ls: data/knowledge/: No such file or directory
```

**文档声称**：
```markdown
data/knowledge/        # 原始知识库语料（40MB，221 个 md）
语料扩容：`knowledge/` 下 221 个 md（40MB）尚未入库
```

**影响**：
- RAG 真实问答依赖 TriviumDB 索引（由 knowledge 构建）
- 当前只能用 fixture 数据（11/12 机制层）
- 真实质量层 (rag_p1) 无法验证

**修复**：
1. 确认知识库来源（是否需单独下载/采购）
2. 补充 `data/knowledge/` 或更新文档说明"需另行获取"
3. 补充 `scripts/build_index.py` 使用说明
4. 更新 README.md "已知约束" 章节

#### 4. **真实 RAG 质量未达标** 🟡
**问题**：`rag_p1` 真实质量层仅 36/58 (62.07%)，状态 blocked

**当前证据**：
- 机制层：56/58
- 独立质量：36/58（需 ≥80%）
- 退出码：1（失败）

**影响**：
- 真实用户问答质量无法保证
- 无法作为生产能力宣称

**修复**：
1. 分析 22 条失败用例（检索 miss / 生成错误 / judge 误判）
2. 优化检索策略（chunk 大小 / rerank 参数 / hybrid 权重）
3. 优化生成提示词（引用约束 / 格式要求）
4. 重跑 rag_p1 直至 ≥80%
5. 建立 rag_p1 baseline（当前未入 baseline.json）

---

### P1 - 上线前建议完成

#### 5. **监控与告警缺失**
**缺失**：
- Prometheus / Grafana 集成
- 关键指标 dashboard（请求量 / 延迟 / 错误率 / 成本）
- 告警规则（API 额度耗尽 / 数据库连接池满 / 磁盘空间不足）

**建议**：
- 接入已有 OTel trace（当前 guarded import）
- 添加 `/metrics` 端点（Prometheus 格式）
- 创建 Grafana dashboard 模板

#### 6. **CI/CD 流程缺失**
**缺失**：
- 无 `.github/workflows/` 或 `.gitlab-ci.yml`
- 无自动化测试 / 构建 / 部署流水线
- 无镜像推送到私有 registry

**建议**：
- 创建 `.github/workflows/ci.yml`：PR 触发 `scripts/ci_gate.sh`
- 创建 `.github/workflows/release.yml`：tag 触发构建镜像 + 推送
- 集成容器扫描（Trivy / Snyk）

#### 7. **规范版本验证未完成**
**问题**：`data/versions.json` 四本规范已有元数据，但 README 仍声称 "unknown"

**当前状态**：
```json
{
  "spec_id": "GB50015",
  "version": "2019",
  "status": "current",
  "verified_at": "2026-09-14"
}
```

**文档声称**：
```markdown
GB50015-2019 等现有语料含非正式摘要/要点，
`data/versions.json` 四本规范状态仍为 unknown
```

**建议**：
- 核对 `data/curated/clauses/` 语料是否为正式版本
- 更新 README.md 移除 "unknown" 说法
- 补充语料来源说明（出版社 / 官网 / 第三方）

#### 8. **图纸审查真实样本不足**
**问题**：P3 真实图纸仅 2 份（召回率 100%，但样本量不足统计意义）

**当前状态**：
- 18 份脱敏 DXF（`data/drawings/real_samples/`）
- 召回率验收：2/2 (100%)
- 误报率：11.1%（略高于 10% 目标）

**建议**：
- 补充至 ≥10 份真实设计院图纸
- 覆盖不同类型（给水 / 排水 / 消防）
- 补充复杂场景（多层嵌套块 / 外部参照）

---

### P2 - 上线后改进

#### 9. **多 Worker 部署约束**
**限制**：记忆候选表存进程内（15 分钟 TTL），多 worker 需 sticky session

**当前文档**：
> 单 worker 部署约束：记忆候选表存于进程内（15 分钟 TTL，最多 256 条）

**影响**：
- 无法水平扩展
- 单点故障风险

**改进**：
- 候选表持久化到 PostgreSQL（增加 `memory_candidates` 表）
- 或改用 Redis（TTL 原生支持）

#### 10. **本地模型未验证**
**状态**：`local-model` profile 存在但默认关闭，无兼容性证据

**当前文档**：
> 当前本机没有本地 endpoint，因此尚未取得 compatible 证据

**改进**：
- 补充 Ollama + qwen3:8b 的兼容性测试
- 生成 `local-model-compatibility.json`
- 文档说明本地模型 vs 远程模型的质量差异

#### 11. **日志聚合缺失**
**当前**：Docker json-file driver（本地磁盘，最多 3 个 10MB 文件）

**改进**：
- 接入 ELK / Loki / CloudWatch Logs
- 结构化日志（JSON 格式）
- 关联 trace_id（OTel）

#### 12. **成本监控缺失**
**当前**：`generation/cost.py` 记录 token 消耗，但无聚合报表

**改进**：
- 按 user / project / day 聚合成本
- 定期导出到 CSV / BI 系统
- 设置预算告警（月度/季度）

---

## 📋 上线检查清单

### 环境准备
- [ ] 生产服务器最低配置（建议 2C4G，磁盘 20GB+）
- [ ] Docker Engine 安装（≥24.0）
- [ ] Docker Compose V2 安装
- [ ] 域名 DNS 解析（A 记录指向服务器）
- [ ] 防火墙开放 80/443 端口

### 密钥与配置
- [ ] 轮换 DEEPSEEK_API_KEY（旧密钥已泄漏）
- [ ] 轮换 DASHSCOPE_API_KEY
- [ ] 生成 `secrets/postgres_password`（32 字节随机）
- [ ] 生成 `secrets/admin_password`（32 字节随机）
- [ ] 配置 WEB_DOMAIN 环境变量
- [ ] 配置 CADDY_EMAIL 环境变量（Let's Encrypt）
- [ ] 确认 .env* 在 .gitignore 中

### 数据准备
- [ ] 补充 `data/knowledge/` 语料（或文档说明缺失）
- [ ] 运行 `scripts/build_index.py` 构建 TriviumDB 索引
- [ ] 补充图纸审查真实样本至 ≥10 份

### 质量验收
- [ ] 本地运行 `bash scripts/ci_gate.sh` 全绿
- [ ] 真实 RAG 质量层 ≥80%（`rag_p1 --variant real --judge`）
- [ ] 建立 rag_p1 baseline

### 部署验证
- [ ] `docker compose up -d` 启动成功
- [ ] `docker compose run --rm smoke` 全部通过
- [ ] 浏览器访问 `https://<WEB_DOMAIN>` 正常
- [ ] 登录管理员账号（admin + admin_password）
- [ ] 提交一个真实问答，验证 RAG 链路
- [ ] 提交一个计算任务，验证 calc 链路
- [ ] 生成一份计算书，验证 docgen HITL
- [ ] 查看长期记忆面板，验证写入与召回

### 运维准备
- [ ] 创建 `docs/DEPLOYMENT.md`
- [ ] 设置定时备份任务（cron + `scripts/backup.sh`）
- [ ] 演练一次恢复流程（`scripts/restore.sh --from <backup>`）
- [ ] 配置监控告警（可选）
- [ ] 配置日志聚合（可选）

### 文档更新
- [ ] 更新 README.md 移除 blocked 状态说明
- [ ] 补充 API 密钥配置指南
- [ ] 补充知识库来源说明
- [ ] 补充规范语料版本说明

---

## 🎯 建议上线顺序

### 阶段 1：MVP 就绪（1-2 天）
1. **修复 P0 密钥泄漏**（立即）
2. **补充部署文档**（4 小时）
3. **说明知识库缺失**（文档更新，1 小时）
4. **内网试部署**（验证 smoke，2 小时）

**交付物**：可在内网单机部署，限定用户试用

### 阶段 2：质量达标（3-5 天）
1. **优化 RAG 质量至 ≥80%**（调参 + 重测）
2. **补充图纸真实样本至 ≥10 份**
3. **建立 rag_p1 baseline**
4. **补充规范版本验证**

**交付物**：真实问答质量可信，可对外宣称

### 阶段 3：生产加固（1 周）
1. **接入监控告警**（Prometheus + Grafana）
2. **设置 CI/CD**（GitHub Actions）
3. **配置定时备份**
4. **演练故障恢复**
5. **负载测试**（压测工具 + 资源监控）

**交付物**：可承载生产流量，运维体系完整

---

## ✅ 总结

**核心能力**：✅ 已就绪（代码质量 / 部署基础 / 治理安全）

**阻塞上线**：🔴 **4 项 P0 必须修复**
1. API 密钥泄漏（立即轮换）
2. 部署文档缺失
3. 知识库数据缺失
4. RAG 质量未达标

**建议完成**：🟡 **8 项 P1/P2 改进**（监控 / CI/CD / 多 worker / 本地模型）

**最快上线路径**：
- 修复 P0 → 内网试部署（2 天）
- 优化质量 → 对外发布（1 周）
- 生产加固 → 稳定运营（2 周）

**关键风险**：
- ⚠️ **密钥已泄漏**，必须立即轮换
- ⚠️ **RAG 质量不足**，不能作为生产能力宣称
- ⚠️ **知识库缺失**，真实问答无法工作

修复上述 P0 后，系统可进入小范围试用阶段。
