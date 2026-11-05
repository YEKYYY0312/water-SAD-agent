# macOS 本地独立 App

本目录将现有给排水 Agent 封装为 Apple Silicon 原生窗口。安装包内含 CPython 3.12.14、PostgreSQL 18.2、LibreDWG 0.14、锁定的 Python 依赖、前端静态页面和指定的 3072 维索引。使用时不要求 Python、Node、Homebrew、Docker 或系统 PostgreSQL。远程模型仍需在设置页配置用户自己的 Key。

当前目标为本机使用的 arm64 App；ad-hoc 签名不等于 Apple 公证。最低编译目标为 macOS 14，较低系统版本、Intel 和另一台 Mac 的兼容性仍须实际验证。

## 安装与数据

将 `给排水 Agent.app` 从 DMG 复制到 Applications，再双击打开。本机个人版打开后直接进入工作台，无需设置或输入登录密码。后台使用私有会话及 CSRF 校验，不开放网页免鉴权接口；已有账户密码和数据保留，应用不会读取旧 Docker 的账号、数据库或模型密钥。

独立数据位于 `~/Library/Application Support/WaterAgent/`。数据库只监听私有 Unix socket，HTTP 只绑定 `127.0.0.1` 动态端口。替换 App 不删除用户数据。正常退出由 App 停止自己的后端和数据库，不停止其他项目服务。

备份使用应用菜单选择新文件，包含逻辑数据库导出和受支持用户文件；不包含模型 Key 与 trace 原文。恢复到已有目标需显式覆盖确认。不要在数据库运行时直接复制 PostgreSQL cluster。

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
