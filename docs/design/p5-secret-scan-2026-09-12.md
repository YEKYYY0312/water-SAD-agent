# P5 Secret Scan 报告

> 扫描时间：2026-09-12（Asia/Shanghai）
> 仓库：`/Users/yaerken/Desktop/给排水agent`
> 原则：只记录脱敏位置、类型、状态和证据；不打印、复制、验证、调用或轮换 secret。

## 1. 扫描范围与方法

扫描了：

1. 当前工作树中的文本源文件、配置、脚本、JSON/Markdown 和显式 `.env*` 文件（排除 `.git`、虚拟环境、缓存、
   rebuildable index）；
2. 所有 reachable Git commits 的文本 blob，使用高置信 secret 形态匹配；输出只保留 commit 前缀和路径；
3. 敏感文件名（env、private key、数据库、trace、index 和生成报告）及 `.gitignore` 规则；
4. 当前环境中 `gitleaks`、`detect-secrets`、`trivy` 等工具是否存在；这些工具不存在时不伪造结果。

执行过的命令类型：

```text
rg -l --hidden ... <高置信 secret 形态> .
git rev-list --all + git grep -I -l ...
find ... <敏感文件扩展名>
git ls-files / git check-ignore
```

命令输出经过路径/类型脱敏；本报告不重复 secret 形态本身，避免把扫描规则变成示例载荷。

## 2. 结果

### 2.1 必须轮换的非空候选

`.env` 与 `.env.local` 中存在非空、非占位的 provider Key 候选，按真实秘密处理：

| 脱敏位置 | 类型 | 状态 | 动作 |
| --- | --- | --- | --- |
| `.env:3` | `DEEPSEEK_API_KEY` | nonempty secret candidate | **必须轮换**；不要提交、不要调用轮换 API |
| `.env:4` | `DASHSCOPE_API_KEY` | nonempty secret candidate | **必须轮换**；不要提交、不要调用轮换 API |
| `.env.local:1` | `DEEPSEEK_API_KEY` | nonempty secret candidate | **必须轮换**；不要提交、不要调用轮换 API |
| `.env.local:2` | `DASHSCOPE_API_KEY` | nonempty secret candidate | **必须轮换**；不要提交、不要调用轮换 API |

这里的“候选”表示本地扫描判定为真实形态，不表示本报告验证了 provider 接受该 Key。轮换由用户/部署者在
provider 控制台完成；本 Step 不读取值、不请求 provider、不尝试自动轮换。

### 2.2 合成测试载荷

高置信形态还命中以下已跟踪测试/场景文件：

- `src/water_agent/runtime/governance/scenarios.py`
- `tests/test_guardrails.py`
- `tests/test_redteam_dataset.py`

这些路径属于治理/红队合成数据上下文，命中用于测试阻断和脱敏，不作为真实 Key 证据；它们仍不得复制到
README、截图、公开样例或产品 trace。若未来把测试数据迁移到公共仓库，必须保留“合成”标识并二次扫描。

### 2.3 Reachable Git 历史

reachable Git history 的高置信扫描只发现上述合成治理/红队文件在当前提交中的匹配，没有发现非测试/非场景的
真实 secret 形态。该结果不覆盖不可达对象、开发者机器其他目录、provider 侧日志或未来 Docker 镜像层；发布前仍
必须在最终提交和镜像构建产物上重新扫描。

`.env`、`.env.local`、`data/traces/`、`data/eval/traces/`、`data/sessions/` 和 `data/indexes/` 未列为已跟踪
发布文件；但“未跟踪/被忽略”不等于文件已经从本机删除，也不等于历史/镜像绝对没有副本。

### 2.4 敏感生成物

当前工作区可见被忽略或生成的敏感类型包括：

- SQLite/session 数据库和 `*.db`/`*.wal`/`*.lock`；
- `data/traces/`、`data/eval/traces/` 中的运行 trace；
- `data/indexes/` 中可重建索引；
- 测试/评测报告和 judge cache。

它们禁止进入 Git、Docker build context、镜像层和公开压缩包；不在本 Step 做破坏性删除。

## 3. 保护规则和后续动作

1. 立即在 provider 侧轮换四个本地 Key 候选，并删除/重建本机 `.env*`；轮换完成前发布状态保持 BLOCKED。
2. 不在 issue、PR、trace、错误、SSE、memory、README、测试输出或截图中复制 Key；secret 值只允许由受控
   secret 文件/secret store 注入。
3. Step 36 形成 Dockerfile/Compose 后，构建上下文显式排除 `.env*`、数据库、trace、索引、用户文件和真实图纸，
   并在镜像层扫描；Step 45 把它接入发布门禁。
4. Step 37 之后对 session cookie、密码、CSRF token、重置 token、数据库 URL 和签名密钥增加类型化扫描；
   扫描输出仍只保留位置和类型。
5. 若任一 Key 已被 Git、镜像或外部日志暴露，删除文本不足以修复风险，必须先轮换，再清理历史/镜像并保留
   事件记录；本 Step 不执行历史重写或破坏性清理。

## 4. 工具限制和结论

本机没有 `gitleaks`、`detect-secrets` 或 `trivy`；没有 Dockerfile/Compose/镜像，因此镜像层扫描为
`blocked/not applicable yet`，不是通过。当前扫描是高置信正则和路径审计，不能证明没有新颖编码、二进制、
不可达 Git 对象或 provider 侧秘密。

**结论：secret scan 不通过公开发布门禁。** 公开发布前必须完成 Key 轮换、最终工作树/历史/镜像三层扫描，
并由人工确认扫描结果；本报告本身不包含真实值。
