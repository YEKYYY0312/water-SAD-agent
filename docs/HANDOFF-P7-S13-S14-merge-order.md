# P7 合入顺序与命令清单（S13 Skill 下载 / S14 MCP 发现）

> 写于 2026-10-22，供**没有 AI 助手参与**时按步骤执行。每条命令都在写本文时跑过（只读的直接跑，写操作只验证了语法与参数）。
> 状态是**快照**：动手前先跑 §1 的核对命令，和本文不一致就以命令输出为准。

## 0. 先说结论

1. GitHub Actions 被计费拦住了（2026-10-22 07:32Z 起）。**所有检查在 2 秒内失败、没有日志**，不是代码问题。恢复前，PR 上的红灯不代表代码有问题。
2. **合入顺序（必须）**：`#55` → S13 实现 PR → `#56` → S14 实现 PR。文档 PR（`#55` / `#56`）各只含一个 Prompt 文件，必须先于各自的实现 PR 合入，否则实现 PR 的 `p7-scope` 找不到 Prompt 会判失败。
3. 本地门禁 `bash scripts/ci_gate.sh` 就是 CI 的同一套检查。**CI 起不来的时候，本地门禁退出码 0 是你唯一的、也是足够强的证据**（S13 实现分支已经跑过：退出码 0，全量 4280 passed）。
4. 合入只有你能做（`gh pr merge` 以往会被 AI 的安全检查拦下，也不该由它代你按）。

## 1. 先核对现状（只读，随时可跑）

```bash
cd /Users/yaerken/Desktop/给排水agent            # 主检出；所有命令都在仓库根目录跑
git fetch origin
gh pr list --state open --json number,title,headRefName --jq '.[] | "#\(.number) \(.headRefName) | \(.title)"'
git ls-remote --heads origin 'YEK/p7-s13*' 'YEK/p7-s14*' 'YEK/p7-docs-s13*' 'YEK/p7-docs-s14*'
```

写本文时的预期：

| 东西 | 状态 |
|---|---|
| `#55` | OPEN，分支 `YEK/p7-docs-s13-prompt`，只含 `docs/execution-prompts/P7-S13-skill-fetch.md` |
| `#56` | OPEN，分支 `YEK/p7-docs-s14-prompt`，只含 `docs/execution-prompts/P7-S14-mcp-registry-discovery.md` |
| S13 实现 | 分支 `YEK/p7-s13-skill-fetch` 已推送，**还没开 PR**，8 个 commit |
| S14 实现 | 本地有分支，**还没推送、还没实现**（见 §6） |
| main | `00982cf`（S12 合入后） |

## 2. 恢复 CI（你要自己处理的一步）

报错原文（在任一失败检查的 annotation 里）：`The job was not started because recent account payments have failed or your spending limit needs to be increased`。

1. 浏览器打开 <https://github.com/settings/billing>，登录 `YEKYYY0312`。
2. 点 **Actions** 标签看分钟用量；再看 **Budgets and alerts** 有没有一条 Actions 预算、上限是不是 $0。
3. 账单周期显示 10 月 1 日 – 10 月 30 日，**免费额度预计 11 月 1 日左右重置**。重置后不用改任何设置，直接做 §3。
4. **重置后如果重跑仍是同样的报错**，说明原因不是额度，而是付款或预算（Budgets 上限为 $0、或付款方式失败），需要在账单页处理；这时再"等"没有用。
5. 不要开新号、不要转移仓库来绕额度：计费记在**仓库所有者**头上，不看谁触发，没用；还违反 GitHub 服务条款 B.3。

## 3. 重跑 CI（不要重新推送）

**只重跑 pull_request 事件的 run**。同一个提交在 `push` 和 `pull_request` 上各跑一遍，重跑两套是浪费分钟（`p5-ci` 一个 workflow 10 月就用了约 1055 墙钟分钟）。

```bash
# 列出 #55 的 PR 事件 run
gh run list --branch YEK/p7-docs-s13-prompt --event pull_request --json databaseId,name,conclusion \
  --jq '.[] | "\(.databaseId) \(.name) \(.conclusion)"'

# 逐个重跑（把 <run-id> 换成上面的 databaseId）
gh run rerun <run-id>
```

写本文时 #55 的 PR 事件 run：`39741337378`（p5-ci）、`39741337340`（p5-postgres）、`39741337139`（p7-scope）。#56：`39801570752`（p5-ci）、`39801570721`（p5-postgres）、`39801570713`（p7-scope）。这些 id 重跑后不会变，但**以 `gh run list` 的输出为准**。

注意：`gh pr checks <n>` 里同一个检查可能出现两行（`pull_request` 与 `push` 各一次），看汇总时 `| sort -u`。

```bash
gh pr checks 55 | sort -u
```

## 4. 合入顺序与每一步的命令

### 4.1 合 #55（S13 的 Prompt，只含一个文件）

```bash
gh pr checks 55 | sort -u          # 确认全部 pass（包括 step-scope）
gh pr merge 55 --merge             # 仓库里历史上都是 merge commit；squash / rebase 也允许，但保持一致
git fetch origin && git log --oneline origin/main -2
```

### 4.2 开 S13 实现 PR，并让它的检查跑起来

```bash
git fetch origin
# 实现分支基于 00982cf；#55 合入后 main 多了一个 merge commit，把它并进来再开 PR（分支内 Prompt 与 #55 逐字相同，不会冲突）
git switch YEK/p7-s13-skill-fetch
git merge origin/main
```

合并后立刻在本分支跑一次本地门禁（§5），通过再推送：

```bash
git push origin YEK/p7-s13-skill-fetch
gh pr create --base main --head YEK/p7-s13-skill-fetch --title "feat(p7-s13): Skill 下载" --fill
gh pr checks <新 PR 编号> | sort -u
```

`--fill` 用分支的 commit 信息做标题和正文，够开 PR。要写更完整的描述，用 `gh pr edit <编号> --body-file docs/design/p7-step-13-decision.md`（直接传文件路径，**不要用 `<(...)` 进程替换**——没实测过 `gh` 接不接受），建议贴「一句话结论」和「验证记录」两节。**不要在 PR 描述里加 AI 署名**（项目约定）。

### 4.3 合 S13 实现 PR

只在 `gh pr checks` 全部 pass 后合。若 CI 仍起不来：以你本地跑的 `bash scripts/ci_gate.sh` 退出码 0 + 全量 `pytest` 为准，**同时接受 `p7-scope` 没有在 PR 上跑过**（本地用 `check_step_scope.py` 补，见 §5）。

### 4.4 合 #56（S14 的 Prompt）

#56 的分支和 S13 完全独立，也只含一个文件，**任何时候都可以先合**，不必等 S13。

### 4.5 S14 实现（若已完成）与冲突

S14 与 S13 **都基于 `00982cf`，都把出站站点从 9 改成 10**。后合入者必须重算。完整清单见 `docs/execution-prompts/P7-S14-mcp-registry-discovery.md` 文末「并行冲突」，摘要：

| 冲突点 | 做法 |
|---|---|
| 5 处 `== 9` 断言（`test_vision_review.py`、`test_llm_endpoints.py`、`test_governance_contracts.py` ×2、`test_dxf_upload.py` 两个数） | 后合入者 `10` → `11`；`ProviderKind` 枚举断言末尾追加两项 |
| `GOVERNANCE_CONFIG_VERSION` | 取后合入者自己的、更新的版本串，不要两边各留一个 |
| `PINNED_ENV_KEYS` | 两边追加的键取**并集** |
| `compose.yml` | S13 加卷与 `skills-admin` 服务；S14 只加两个环境变量；取并集，不要覆盖 |
| `AGENTS.md`、`16` / `17` 号文档、`p2.5-*.md` | 各自追加自己的小节，取并集；`17-p7-external-capabilities.md` §0 那一行手工合成 `S13 9 → 10、S14 10 → 11` |
| `data/eval/tool_assembly_main.json` | S13 新增 `session_skills_installed`；S14 不新增 profile，不冲突 |

合并后必须重跑：`tool_assembly_snapshot --check`、`pytest tests/test_p7_traceability.py tests/test_check_step_scope.py`、两个套件各自的 runner 与比较器。

## 5. 本地门禁（没有 CI 时的替代）

```bash
export PYTHONPATH=src                       # worktree 里必须；不设则 conftest 守卫直接拦下（exit 4）
unset HTTPS_PROXY HTTP_PROXY WATER_AGENT_LLM_BASE_URL WATER_AGENT_LLM_MODEL DEEPSEEK_API_KEY DASHSCOPE_API_KEY LOCAL_MODEL_ENABLED
COMPOSE_PROJECT_NAME=p7s13 bash scripts/ci_gate.sh     # 约 5–6 分钟；退出码 0 才算过
```

要点（都踩过）：

- **`COMPOSE_PROJECT_NAME=<别名>` 必须设**：本机另一个 `water-agent` 栈在跑时，`memory_p0` 的备份用例会去备份它并误报 `MEMORY_FORGET_RESIDUAL`。
- `ci_gate.sh` 里的 pytest 是 `-qq`，**日志里没有汇总行**。要计数请另跑：`_venv/bin/pytest -q -o addopts= -p no:cacheprovider`（约 5.5 分钟）。
- 它会先核对 `water_agent` 导入自本检出，不一致 exit 2（worktree 里 `_venv` 的 editable install 指向主检出）。
- 范围守卫（PR 上由 `p7-scope` workflow 跑 `--auto`，本地没有 PR 时手工跑）：

```bash
# Prompt 已在 main 上：
_venv/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S13-skill-fetch.md --base origin/main
# Prompt 还不在 main 上（#55 未合）：用 --bootstrap 从工作区读范围块
_venv/bin/python scripts/check_step_scope.py docs/execution-prompts/P7-S13-skill-fetch.md --base origin/main --bootstrap
```

- 前端：`(cd web/app && npm test && npm run build)`。worktree 里没有 `node_modules` 时先 `npm ci`；直连不稳时 `HTTPS_PROXY=http://127.0.0.1:7892 npm ci`。
- 推送 / `gh` 直连不稳时：`HTTPS_PROXY=http://127.0.0.1:7892 git push ...`；推送失败先 `curl -m 10 -x http://127.0.0.1:7892 https://github.com` 判断是网络还是别的。**直连也可能是通的**，先试直连。

## 6. S14 的状态

- Prompt 已写完并通过范围守卫（`#56`）。
- 实现分支 `YEK/p7-s14-mcp-registry-discovery` 在本机 worktree `/private/tmp/s14-impl`（macOS 上 `/tmp` 的真实路径；**临时目录，重启可能丢**），**只有一份 Prompt，没有任何实现代码**，没有推送。
- 若 worktree 不在了：`git worktree add -b YEK/p7-s14-mcp-registry-discovery <新目录> origin/YEK/p7-docs-s14-prompt` 重建（在主检出里执行）。分支已存在时去掉 `-b`。
- 官方 MCP 注册表没有取得真实样例（`WebFetch` 只拿到页面标题，GitHub 在这里被拦）。**S14 只能做自定义最小目录协议 + 假目录，`unverified_against_real_registry`**，不要声称兼容任何真实目录。

## 7. 没做、也不能说做了的事

| 事项 | 状态 |
|---|---|
| 真实 skill 注册表 / 真实 MCP 目录 | 没有任何真实服务被验证；两个协议都是我们自己定义的 |
| 真 TTY 下的 `approve` 成功路径 | 没在容器里跑过（只有进程内测试 + 注入 TTY 替身） |
| 对抗性评审（让另一个人只读语料与机制各写一份"想绕过它"的清单） | 没做；S13 的评测集是机制之后才写的，同一作者 |
| `p5-compose` 冒烟与 `check_image.sh` | 本机 `docker compose build` 因 buildx bake 的 gRPC 错误不可用（`docker build` 可用），所以整栈冒烟没跑 |
| S16 思考型模型 | **被真实 Key 门槛卡住**：SDD 18 §10 的 D4–D8 必须先用 DeepSeek Key 实测并回填，Prompt 明确"未回填则停止，不得凭文档推测写策略表"。这台机器没有可用 Key |
| `mcp/schema.py::ensure_description_safe` 对 Unicode Tag 字符漏检 | S13 Step 0 发现，只记录，没修；与 skill 缺口同根（`rules.py::_INVISIBLE_CHARS` 只有 8 个） |

## 8. 出问题时

| 现象 | 怎么判断 / 怎么办 |
|---|---|
| 检查在 2 秒内失败、没有 step | 计费问题，见 §2。看 `gh api repos/YEKYYY0312/water-SAD-agent/check-runs/<id>/annotations` |
| `p7-scope` 报「找不到该步的执行 Prompt」 | 对应文档 PR 还没合入 main。先合 #55 / #56 |
| `p7-scope` 报越界文件 | 看输出里列的路径；只有在 Prompt 的 allow 里漏了它才是 Prompt 的问题（S8 D13、S12 都出现过），否则是改了不该改的 |
| `test_p7_traceability` 红 | `tests/` 里出现了规格没定义的 `P7-Sxx-Axx`，或 `delivered` 行引用了会被 skip 的测试；改规格或测试 docstring，**不要**放宽检查 |
| `tool_assembly_snapshot --check` 红 | 装配面变了。有意变更：`--diff` 看差异 → `--write --reason "..."`，且 surface 变了要递进版本号；无意变更就是回归 |
| 合并后 5 处 `== 10` / `== 11` 红 | 并行冲突的重算，见 §4.5 |
| `ci_gate.sh` 在 `memory_p0` 报 `MEMORY_FORGET_RESIDUAL` | 没设 `COMPOSE_PROJECT_NAME`，它备份了别的栈 |
| `git push` 超时 | 试 `HTTPS_PROXY=http://127.0.0.1:7892`；直连与代理哪个通是会变的 |

## 9. 项目约定（提交 / 分支）

- 分支名一律 `YEK/<name>`，不用 `claude/<name>`。
- commit、PR、文档**不加 AI 署名**（不写 `Co-Authored-By` / "Generated with …"）。
- 一个逻辑面一个 commit；测试没过就贴输出说没过，不含糊。
