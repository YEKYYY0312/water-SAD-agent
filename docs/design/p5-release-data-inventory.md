# P5 发布数据清单（Step 35）

> 目的：在实现 Web/记忆前先划定“可以进入 GitHub/镜像”和“禁止发布”的数据边界。
> 状态：清单已定义；当前公开发布仍 BLOCKED。
> 依据：P5 任务书 §11、`docs/design/p2.5-threat-model.md` §5–§6、P4.5 §8/§12/§18，以及本工作树审计。

## 1. 发布分类

| 类别 | 可发布内容 | 条件/动作 |
| --- | --- | --- |
| A：默认可入库 | 项目源码、确定性测试、合成 eval 数据、脱敏设计文档、`pyproject.toml`、`uv.lock`、`.gitignore`、CI 脚本 | 不含 secret、用户数据、完整未授权规范/图纸；测试载荷保持合成标识 |
| B：核实后可入库 | `data/curated/` 中有明确来源/授权的最小必要条款片段、`knowledge/` 中版权明确的材料、第三方 API 文档、镜像基础文件 | 逐文件确认版权/许可证/来源；版本 unknown 或授权不明时不发布；只收录必要片段，不把全文当测试 fixture |
| C：必须重写后可入库 | `LICENSE`、`README.md`、`CONTRIBUTING.md`、`SECURITY.md`、`CODE_OF_CONDUCT.md`、`TROUBLESHOOTING.md`、第三方 notice | 当前 `LICENSE`/`CONTRIBUTING.md`/`TROUBLESHOOTING.md` 未跟踪且指向 IFC/Bonsai；版权主体、许可证选择和项目内容须人工确认后重写；当前不覆盖用户文件 |
| D：永不入库/不进镜像 | `.env`、`.env.local`、任何 API Key/password/token/private key/session secret、数据库密码和真实备份 | 通过 secret 文件/secret store 注入；即使值是“测试值”也不要形成真实凭据样式 |
| E：运行时敏感数据 | `data/sessions/`、SQLite/PostgreSQL 数据、LangGraph checkpoint、`data/traces/`、`data/eval/traces/`、judge cache、反馈、记忆正文、SSE 事件原文 | 只保存在受控 volume；日志/trace/SSE 只写安全摘要、ID/hash/计数；有 retention 和删除路径 |
| F：可重建但禁止发布 | `data/indexes/`、`*.db`、`*.db-*`、`*.wal`、`*.vec`、缓存、评测报告和本地导出文件 | 源数据可发布也不等于索引可发布；CI 在干净目录重建；保持 `.gitignore` 和镜像排除 |
| G：真实工程资料 | 真实设计院 DXF/DWG、真实项目坐标、业主/联系人/单位、用户上传、设计书、材料表、图纸预览和反馈 | 未经脱敏、授权、版权和隐私确认不得进入 Git、镜像、eval、issue、截图或示例；P3 真实样本当前 blocked |
| H：规范/版权高风险 | 未授权规范全文、扫描 PDF/OCR 全文、非正式摘要被描述为正式条文、旧版本全文和供应商内部资料 | 只发布有权威 source_id 和授权的最小必要片段；`data/versions.json` unknown 不得写成现行有效；不把占位条文作为产品证据 |
| I：外部/其他项目文件 | 当前未跟踪的 `blender_addon/`、`experiments/`、`docs/ifcopenshell_api_docs.txt`、`docs/LLM_ENV.md`、`docs/api-reference.md` 及相关说明 | 来源、许可证、是否含用户/供应商数据未完成核对前不纳入 P5 发布包；不得因文件在工作树就默认属于本项目 |

## 2. 当前工作树判定

审计得到的实际事实：

- `git status --short --branch` 显示用户已有修改：`data/mcp_servers.json`、P4.5 orchestrator/provenance 和两份测试；
  以及多个未跟踪文件/目录。Step 35 不覆盖、不暂存、不清理它们；
- `git ls-files` 只确认 `README.md` 和 `uv.lock` 等项目文件在历史中；根目录 `LICENSE`、`CONTRIBUTING.md`、
  `TROUBLESHOOTING.md` 没有可达 Git 历史，内容却自称 `IFC Bonsai MCP`/`blender_mcp`，必须人工核实来源；
- `SECURITY.md`、`CODE_OF_CONDUCT.md`、Dockerfile、Compose 和 `.github/` Actions 不存在；它们不是“默认安全”；
- `.gitignore` 已排除 env、trace、session、index 和报告，但当前磁盘仍可见这些生成物；忽略规则不替代删除、
  volume 权限、备份清理或镜像层检查；
- `web/index.html` 的文件头明确其为静态原型，含占位计算和占位条文；只能按设计稿归档，不能进入正式结果证据；
- `data/versions.json` 相关规范状态仍为 unknown；正式规范引用继续 blocked；
- 本次 secret scan 在 `.env`/`.env.local` 发现必须轮换的非空 Key 候选；公开发布保持 blocked。

## 3. 发布包最小 allowlist

最终发布包应从干净 checkout 生成，并只允许下列类别：

```text
src/
tests/                         # 仅合成/脱敏 fixture
data/eval/*.jsonl              # 仅无 PII、无真实图纸、无 secret 的评测源
data/curated/                  # 仅完成来源/授权核验的最小语料
docs/design/                   # 项目设计与审计文档，不含原始敏感 trace
pyproject.toml
uv.lock
.gitignore
README.md / LICENSE / CONTRIBUTING.md / SECURITY.md / CODE_OF_CONDUCT.md
scripts/                       # 不含 secret、个人路径或生产凭据
```

上述是类别 allowlist，不是对当前所有路径的自动批准；每个文件仍需 secret、PII、版权和生成物检查。特别是
`tests/`、`data/eval/` 和 `docs/` 中的示例不能包含真实业主、项目、API Key、完整系统提示、真实坐标或未授权
规范全文。

## 4. 发布前检查顺序

1. 清洁 checkout，确认 `git status` 只包含计划发布文件；保留用户工作树修改，不用破坏性 reset/checkout；
2. 轮换 `.env*` 中的 Key，确认新 secret 只由部署者在运行时提供；
3. 执行工作树、reachable Git history、构建上下文和最终镜像层 secret scan；
4. 从锁文件生成 SPDX/CycloneDX SBOM，核对 100% 依赖版本/hash/许可证/notice；
5. 逐文件核验许可证、版权主体、第三方文件来源、规范/图纸/用户数据授权；
6. 用干净目录执行离线测试、ruff、mypy、前端 typecheck/build、Compose smoke、备份恢复和基线对比；
7. 抽查 trace、SSE、错误响应、数据库 dump、备份和镜像，确认没有正文/Key/绝对路径/Principal subject；
8. 人工确认 P5 任务书的 owner、架构、CPU 支持矩阵、反向代理方案、PII 规则和公开许可证后再打 tag。

## 5. 删除、遗忘与发布的关系

“禁止发布”不等于“可以永久保留在本机”。后续实现必须给运行时数据设置 retention，并让 forget/delete 覆盖
活动数据库、FTS、embedding、cache、checkpoint 投影、导出和备份；审计事件只留无正文 tombstone/hash。历史
模型回答不追溯改写，但用户界面要明确其为历史输出。任何删除残留都阻塞 P5 发布，不可只隐藏 UI。

## 6. 未决责任

| 决策 | 当前状态 | 责任/解锁 |
| --- | --- | --- |
| MIT 或 Apache-2.0 与版权主体 | blocked | 用户/项目维护者确认后重写许可证和 README |
| GitHub owner、仓库名、镜像名 | unknown | 发布负责人确认 |
| 目标 CPU 架构 | unknown | 每个架构要有真实 CI/smoke 证据 |
| Caddy/Traefik/自有反向代理 | unknown | P5 部署文档确认；核心 app 保持代理无关 |
| PII 是否可存、地区/合规、retention | unknown | 用户/法务确认；确认前 P0 默认 block |
| 规范/知识/真实图纸版权授权 | blocked | 逐文件提供可复核来源和授权 |
| secret 轮换 | blocked | 部署者在 provider 控制台完成；本 Step 不执行 |
| 正式 SBOM/许可证扫描工具 | blocked | CI/发布环境提供 SPDX/CycloneDX 生成与许可证核验工具 |
