# macOS 本地独立 App

本目录将现有给排水 Agent 封装为 Apple Silicon 原生窗口。安装包内含 CPython 3.12.14、PostgreSQL 18.2、LibreDWG 0.14、锁定的 Python 依赖、前端静态页面和指定的 3072 维索引。使用时不要求 Python、Node、Homebrew、Docker 或系统 PostgreSQL。远程模型仍需在设置页配置用户自己的 Key。

当前目标为本机使用的 arm64 App；ad-hoc 签名不等于 Apple 公证。最低编译目标为 macOS 14，较低系统版本、Intel 和另一台 Mac 的兼容性仍须实际验证。

## 安装与数据

将 `给排水 Agent.app` 从 DMG 复制到 Applications，再双击打开。本机个人版打开后直接进入工作台，无需设置或输入登录密码。后台使用私有会话及 CSRF 校验，不开放网页免鉴权接口；已有账户密码和数据保留，应用不会读取旧 Docker 的账号、数据库或模型密钥。

独立数据位于 `~/Library/Application Support/WaterAgent/`。数据库只监听私有 Unix socket，HTTP 只绑定 `127.0.0.1` 动态端口。替换 App 不删除用户数据。正常退出由 App 停止自己的后端和数据库，不停止其他项目服务。

备份使用应用菜单选择新文件，包含逻辑数据库导出和受支持用户文件；不包含模型 Key 与 trace 原文。恢复到已有目标需显式覆盖确认。不要在数据库运行时直接复制 PostgreSQL cluster。

### 真实模型配置

聊天地址和模型沿用设置页，保存到专用凭据目录的 `llm_endpoint.json`，非缺省组合必须运行原有兼容性检查。聊天配置不经桌面环境变量覆盖，因此设置页仍可修改。Key 使用原有凭据面板及生产写入器，文件为 0600、目录为 0700，不放进安装包。

embedding 尚无设置页，桌面适配仅将 `~/Library/Application Support/WaterAgent/config/embedding.json` 的三个非秘密字段转成既有生产环境输入，重启本 App 后生效：

```json
{
  "base_url": "https://model-endpoint.invalid/v1",
  "model": "MODEL_NAME",
  "retrieval_mode": "hybrid"
}
```

该文件不允许 Key、维度或其他字段；维度从随包索引读取。地址／模型解析、凭据优先级、索引模型绑定及失败语义全部由生产模块裁定，没有桌面备用解析器。文件不存在时保留既有默认行为，格式错误则停止启动并报告 `DESKTOP_EMBEDDING_CONFIG_INVALID`。启动器不继承宿主机模型／代理／数据库环境变量。Web 原有装配为 local rerank、recall_top_k=20、hybrid_alpha=0.7；模型实际 `retrieve_clauses.top_k` 参数仍由工具调用决定，不能宣称固定 top-5。

### 可选的运维凭据交接（A1）

普通App使用不需要Docker。已有本机Docker凭据卷需要显式迁移时，可在**退出App之后**运行 `Resources/runtime/credential_handoff.py`；它不是模型工具，也不会在启动时自动执行。只支持本机Unix socket、已有固定sha256的项目Python3.12 Linux运行镜像，以及两个专用槽位。交接不验证服务商鉴权、不轮换或撤销旧Key，不会更改兼容性报告。

运维须先准备本用户0600的非秘密绑定声明，字段恰好为：`version=1`、`volume`、`image=sha256:<64位小写hex>`、`chat={base_url,model}`、`embedding={base_url,model}`。声明不含Key，且必须与源聊天面板及目标实际配置一致；embedding归属是运维声明，不能当作已通过服务商认证。

```bash
"$HOME/Applications/给排水 Agent.app/Contents/Resources/python/bin/python3.12" -I -B \
  "$HOME/Applications/给排水 Agent.app/Contents/Resources/runtime/credential_handoff.py" \
  --resources "$HOME/Applications/给排水 Agent.app/Contents/Resources" \
  --data-dir "$HOME/Library/Application Support/WaterAgent" \
  --source-binding '/受保护目录/source-binding.json' \
  --socket "$HOME/.docker/run/docker.sock"
```

不要把Key粘贴到命令行、聊天或声明文件。源容器只读源卷、无网络、禁日志，producer先关闭标准输出；专用文件复制流只连接本机0600暂存FD，不进入控制台或PIPE日志捕获。完整验证并清理源与暂存后，目标经生产写入器更新；普通失败尝试恢复并回读原槽位，恢复/清理失败明确阻断。SIGKILL不保证两槽原子或自动清理，须核查受保护暂存与槽位后再继续。设计细节见 `docs/design/desktop-credential-handoff.md`。

开发验收仅使用新建canary卷和临时资料目录，不接受生产卷参数。先将输出目录加入本地Git排除，再运行（`--out`所在目录须不存在或为空，保留先前失败报告）：

```bash
PYTHONPATH=src _venv/bin/python desktop/macos/packaging/handoff_canary.py \
  --resources "$HOME/Applications/给排水 Agent.app/Contents/Resources" \
  --image sha256:ac9567ae2364d8dfa733926bd944092b843a64b50ec51275aaa36f6c9f763cf2 \
  --socket "$HOME/.docker/run/docker.sock" \
  --out "$PWD/work/credential-handoff-a1-2026-11-07/final-canary/report.json"
```

canary通过只说明机制可用，不能关闭此前真实Key日志审计缺口；旧Key轮换仍需独立协调所有受影响调用方。

## 构建

构建机需要 Apple command line tools（Swift、clang、make）、uv 和 Node/npm；仅构建期需要它们。工作目录为仓库根；原生依赖与安装产物只写本地忽略的 `work/macos-standalone/`。首次使用前将 `/work/macos-standalone/` 加入 `git rev-parse --git-path info/exclude` 返回的本地排除文件。

```bash
_venv/bin/python desktop/macos/packaging/build_dependencies.py
_venv/bin/python desktop/macos/packaging/build_app.py \
  --index '/明确指定的/text-embedding-3-large__3072__发布目录'
_venv/bin/python desktop/macos/packaging/verify_bundle.py \
  'work/macos-standalone/output/给排水 Agent.app'
PYTHONPATH=src _venv/bin/pytest -q tests/test_desktop_packaging.py
```

构建会执行 `npm ci --ignore-scripts`、前端测试/build、Swift release build。`--skip-frontend` 仅供复用本次已测试的 dist；`--no-dmg` 用于先验证 App，之后再生成 DMG。不得把未验证的旧 dist 当作本次前端测试证据。

所有网络下载是构建期操作。PostgreSQL 源码及其 SHA256 来自官方 PostgreSQL FTP；LibreDWG 的版本及摘要与项目 Dockerfile 对齐。Python 使用官方 python-build-standalone `20260825` 的 CPython 3.12.14 arm64 stripped install-only 归档，固定 SHA256。Python 业务依赖由 `uv.lock` 导出，使用 hash 校验非 editable 安装；不复制开发 `_venv`。上游安装器 pip 不进入运行包。

PostgreSQL 禁用 ICU/readline/zlib/lz4/zstd 和 NLS；默认不启用 TLS，仅用于本地私有 socket。LibreDWG 静态链接它自己的库，并作为独立受控工具运行。包内 `licenses/` 包含对应原生源码归档、原许可与构建说明；Python wheel 自带许可留在各 distribution metadata 中。

## 验证边界

构建清单记录源码提交、锁文件指纹、所有资源摘要、Python 包版本、原生依赖来源和索引摘要。扫描拒绝私有目录、开发虚拟环境、越界/绝对符号链接、外部 Homebrew 动态库、开发工具链 rpath 和私钥材料。公有证书仅精确允许 certifi 的 CA bundle。嵌套 Mach-O 逐个签名，再签 App，执行 `codesign --verify --deep --strict`。

`desktop/macos/packaging/smoke.py` 负责真实初始化、HTTP、持久化和备份恢复验证；以该脚本实际输出和评测报告为准。机制验证与真实模型问答质量、另一台 Mac 安装验收分开记录。缺 Key 不代表真实问答通过。
