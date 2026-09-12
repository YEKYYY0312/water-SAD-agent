# P5 依赖、许可证与 SBOM 预检报告

> 审计时间：2026-09-12（Asia/Shanghai）
> 仓库：`/Users/yaerken/Desktop/给排水agent`
> Git 基线：`726f7af`；工作树有用户已有修改，本报告不将其视为 Step 35 改动。
> 结论：依赖锁定和已知漏洞查询通过；许可证和正式 SBOM 预检未达到公开发布批准条件。

## 1. 安装边界和执行结果

当前唯一项目依赖声明是 `pyproject.toml`，唯一锁文件是 `uv.lock`；未发现 `package.json`、npm lockfile、
Poetry/Pipenv/requirements lockfile、Dockerfile、Compose 文件或 `.github/` Actions。当前 Python 约束为
`>=3.11,<3.13`。

实际命令结果：

| 命令 | 结果 | 解释 |
| --- | --- | --- |
| `uv lock --check` | PASS | 锁文件可解析且不会被当前项目元数据改写 |
| `uv tree --locked --no-dev` | PASS | 解析到 67 个 lock package（含 editable `water-agent`；66 个 registry package） |
| `uv pip check --python _venv/bin/python` | PASS | 当前 `_venv` 的 64 个已安装发行包依赖兼容 |
| `_venv/bin/python -m pip check` | BLOCKED | 当前 `_venv` 没有 `pip` 模块；已用 `uv pip check` 替代验证，不把该命令写成通过 |
| `uv audit --locked` | PASS | uv 报告 66 个 registry package “no known vulnerabilities and no adverse project statuses” |
| `pip-audit` / `pip-licenses` / `syft` / `trivy` | BLOCKED/ABSENT | 工具未安装；没有伪造其输出 |

`uv audit` 是当前 uv 提供的实验性审计命令，结果只能代表其漏洞服务查询时的已知项目，不证明依赖不存在恶意
包、不可达漏洞或许可证义务。没有 `pip-audit` 不构成漏洞“通过”；已使用当前包管理器的 `uv audit` 和锁文件校验。

## 2. 直接依赖对照

版本来自 `uv.lock` 的实际解析结果；约束来自 `pyproject.toml`，不是本报告推测。

| 类别 | 包 | 声明约束 | lock 版本 | 当前元数据许可证 | 预检结论 |
| --- | --- | --- | --- | --- | --- |
| runtime | `langgraph` | `>=1.2.0,<2` | 1.2.11 | 未从本地元数据识别 | 待逐项核对 |
| runtime | `langgraph-checkpoint-sqlite` | `>=3.0,<4` | 3.1.1 | 未从本地元数据识别 | 待逐项核对 |
| runtime | `triviumdb` | `>=0.8.0`（无上界） | 0.8.2 | Apache-2.0 | 需补上界/更新策略评审 |
| runtime | `pydantic` | `>=2.7` | 2.13.5 | 未从本地元数据识别 | 待逐项核对 |
| runtime | `langchain-openai` | `>=0.3` | 1.6.0 | MIT | 需保留上游 license notice |
| runtime | `openpyxl` | `>=3.1,<4` | 3.1.5 | MIT | 已有范围；仍需 notice 清单 |
| runtime | `ezdxf` | `>=1.4,<2` | 1.4.4 | MIT classifier | 需核对发布包 license 文件 |
| dev extra | `pytest` | `>=8.0` | 9.1.1 | 未从本地元数据识别 | 测试依赖也进入开发 SBOM |
| dev extra | `ruff` | `>=0.6` | 0.16.5 | 未从本地元数据识别 | 待逐项核对 |
| dev extra | `mypy` | `>=1.10` | 2.3.1 | 未从本地元数据识别 | 待逐项核对 |

特别风险：`triviumdb` 没有声明上界，当前锁定 0.8.2；任何升级都必须先看行为变更、许可证、漏洞和回归，
不得运行强制 audit remediation。

## 3. 锁文件和 SBOM 预检

通过标准库 `tomllib` 对 `uv.lock` 做了结构检查：

```text
lock_packages=67
registry_packages=66
hash_covered_registry_packages=66
registry_packages_without_hash=0
```

这证明 registry 分发记录均包含 sdist/wheel hash，可作为可复现安装的输入；它不是 SPDX/CycloneDX SBOM。
当前没有 `syft` 等生成器，也没有镜像可扫描，因此正式 SBOM 状态为 `blocked`。Step 36/45 发布门禁应生成
至少包含以下字段的 SPDX 或 CycloneDX 文档，并作为构建产物保存而不是手工复制：名称、版本、生态、来源、
分发 hash、直接/传递依赖关系、许可证表达式、版权/notice 来源和扫描时间。

SBOM 发布门禁：

1. 只从 `uv.lock` 和最终构建镜像生成，不从开发者全局环境猜依赖；
2. 运行时和 dev/build 依赖分别标注；前端引入 package manager 后必须增加唯一 JS lockfile 和对应审计；
3. 发现未锁定 URL/path/git 依赖、无 hash、许可证未知或镜像层额外文件时阻塞发布；
4. CI 使用锁定安装，禁止 `--force`/强制升级来“修复”漏洞；升级要读 changelog、跑现有门禁并审查 lockfile diff；
5. 每次新增依赖同时更新许可证清单、威胁模型、镜像排除规则和最小安装验证。

## 4. 许可证元数据扫描

本机用 `importlib.metadata` 检查了当前环境的发行包元数据：

```text
metadata_records=65
unique_distribution_names=64
duplicate_names=water-agentx2
unique_known=34
unique_unknown=30
conflicting_license_records=0
```

“known”仅表示 `License` 字段或 `License ::` classifier 非空，不等于已核对许可证全文、NOTICE、依赖树兼容性
或版权主体。30 个唯一发行包没有可直接使用的本地许可证元数据，名单如下：

```text
anyio, ast_serialize, idna, iniconfig, jiter, langgraph,
langgraph-checkpoint, langgraph-checkpoint-sqlite, langgraph-prebuilt,
langgraph-sdk, librt, mypy, mypy_extensions, numpy, packaging, pydantic,
pydantic_core, pygments, pyparsing, pytest, regex, ruff, truststore,
typing-inspection, typing_extensions, urllib3, uuid_utils, water-agent,
websockets, zstandard
```

已识别的元数据涉及 MIT、Apache-2.0、BSD、MPL-2.0 和多许可证表达式；没有把“未识别”默认为 MIT，也没有
据此作法律兼容性结论。当前根目录 `LICENSE` 是未跟踪文件，内容声称 MIT 但版权主体是其他项目/作者；它不能
作为本项目正式开源许可证。完整许可证批准必须在项目版权主体确认后完成，并为第三方依赖生成 notice 清单。

## 5. 发布判断和待办

| 项目 | 结论 |
| --- | --- |
| 锁文件存在且 hash 完整 | PASS（66/66 registry packages） |
| 当前环境依赖冲突 | PASS（`uv pip check`） |
| 已知漏洞查询 | PASS（`uv audit --locked` 当前查询无已知项） |
| 正式漏洞工具链 | BLOCKED（`pip-audit` 未安装；不要把缺工具写成无漏洞） |
| 许可证完整核对 | BLOCKED（30/64 本地元数据未知，根 LICENSE 来源/主体不明） |
| SPDX/CycloneDX SBOM | BLOCKED（无生成器、无 Docker 构建产物） |
| 前端供应链 | NOT_APPLICABLE_NOW（没有 `package.json`；Step 36 引入后必须新增门禁） |
| 发布批准 | BLOCKED，直到 secret 轮换、许可证主体确认、正式 SBOM 和镜像层扫描完成 |

## 6. 复现命令

```bash
uv lock --check
uv tree --locked --no-dev
uv pip check --python _venv/bin/python
uv audit --locked
uv export --locked --no-dev --no-hashes --format requirements.txt
```

本报告没有安装新依赖、没有修改锁文件、没有连接 provider，也没有输出任何 secret 值。
