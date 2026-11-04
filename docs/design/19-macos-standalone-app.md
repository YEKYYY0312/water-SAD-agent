# macOS 独立桌面 App 设计

状态：Proposed，待用户审阅后进入实现。日期：2026-11-04。

起点：main `29db372`。工作分支：`codex/macos-standalone-app`。

## 1. 用户目标与交付边界

用户已选择 Mac 桌面 App，且要求完整独立安装：双击启动自己的后端和数据库，可在另一台 Mac 安装。目标机器不要求预装 Python、Node、PostgreSQL、Homebrew 或 Docker。首次启动不运行安装脚本、不联网下载运行时依赖。

交付为 `给排水 Agent.app` 与便于复制安装的 DMG；构建环境可以使用编译工具，使用环境不依赖这些工具。应用调用现有远程模型时仍需要网络及用户配置的 API Key，独立安装不等于离线模型推理。

首个构建目标为当前机器对应的 Apple Silicon / arm64。当前已观察的平台为 macOS 27.0.1，已有 Swift 命令行工具与可用的托管 Python 3.12.14。Intel Mac 需要单独的构建与验收，不把 arm64 包标为通用包。更低系统版本的兼容性须由实际测试建立，不凭编译目标版本宣布兼容。

现有 8000/8001 服务、Docker 卷、桌面原始检出用户改动保持不变。新 App 使用独立账号、数据库、凭据及用户文件；不自动导入旧服务的数据或密钥。

## 2. 现有代码事实

| 事实 | 源码依据 | 对设计的约束 |
|---|---|---|
| React 静态资源由 FastAPI 同源服务 | `webapp/app.py::create_app` / `DEFAULT_STATIC_DIR` | 复用界面和同源认证，不重写业务前端 |
| Web 会话检查点使用 PostgreSQL | `storage/checkpoint.py::postgres_checkpointer`，`webapp/run_manager.py` | 保留 PostgreSQL；不能仅换 SQLAlchemy URL 就宣称支持 SQLite 桌面版 |
| Alembic 是 schema 唯一管理入口 | `storage/checkpoint.py`、`storage/database.py` | 启动迁移复用现有迁移，不调用 PostgresSaver.setup 建另一套表 |
| 初始管理员函数可接受 secret_path | `webapp/auth/bootstrap.py::run_bootstrap` | 桌面入口可复用其授权语义；不改生产默认 secret 路径 |
| 凭据、索引与上传导出位置可配置 | `config/credentials.py`、`retrieval/trivium.py`、`documents/storage.py` | 可配置到 App 专用位置，不引用开发机路径 |
| DWG 转换依赖独立 LibreDWG 程序 | `documents/dwg_convert.py`、`Dockerfile` | 要保留该能力，须随包构建 macOS 版本，Linux 二进制不能直接复制使用 |
| 三条铁律与模型兼容性门槛已有实现 | `AGENTS.md`、`generation/compatibility.py` | 桌面只封装运行环境，不另造 Agent 或绕过校验 |

共享知识库相关搜索未找到既有桌面打包方案；以上判断来自本次检出的源码。

## 3. 方案比较与推荐

| 方案 | 优点 | 代价／适用性 |
|---|---|---|
| **原生窗口＋内置 Python＋内置 PostgreSQL（推荐）** | 满足无外部运行环境安装；复用当前数据库及业务链路 | 需要管理多进程、二进制搬移、签名与更新 |
| App 启动 Docker 服务 | 最大程度复用 Compose | 用户仍需 Docker/虚拟机，不满足本次独立安装目标 |
| Python 后端改为 SQLite | 数据库进程更少 | 需改检查点与相关持久化路径，属于独立存储改造，不纳入本轮包装 |

推荐保留业务内核，只新增桌面窗口、受控生命周期、构建与验收层。窗口使用 Swift / AppKit 与 WKWebView，加载应用自己启动的同源界面；不向页面暴露 shell、任意文件系统或模型凭据的原生桥。

## 4. 包结构与运行数据

拟新增目录：

```text
desktop/macos/
  native/              # 窗口、首次设置、进度／故障显示、下载与进程控制
  runtime/             # 启动监督、隔离路径、数据库初始化／迁移、服务装配
  packaging/           # 可重复构建、二进制搬移检查、签名与 DMG
  tests/               # 生命周期与安装包验收
```

安装包资源包含：

```text
给排水 Agent.app/Contents/
  MacOS/               # 原生可执行文件
  Resources/
    python/            # 可搬移的 CPython 3.12 与锁定依赖
    postgres/          # 原生 PostgreSQL server、initdb、pg_ctl、pg_dump/restore
    backend/           # 当前源码、迁移、前端 dist、必要只读数据
    libredwg/          # macOS 转换器及必要动态库
    licenses/          # 第三方许可、来源、构建说明与要求交付的源码材料
    build-manifest.json
```

用户数据根目录：`~/Library/Application Support/WaterAgent/`。数据库、上传、导出、trace、凭据、运维配置与备份各自分目录；目录权限 0700、凭据文件 0600。程序资源与可写用户数据分离，替换 `.app` 不删除会话和用户文件。

临时数据库 socket 放在本用户独占的短路径目录，避免 macOS Unix socket 路径长度限制。PostgreSQL 只开 Unix socket，不监听网络；FastAPI 只监听 `127.0.0.1`，由操作系统分配空闲端口，不使用固定 8000/8001。

CPython 基于托管的 python-build-standalone 分发，业务依赖按 `uv.lock` 安装为非 editable 资源。禁止带入 `_venv` 中指向桌面检出的链接。PostgreSQL 初始构建先对齐项目当前使用的 18.2；所有下载版本、来源与摘要写入构建清单。是否采用后续修订版须重新核对并验证，不在构建过程中静默漂移。

## 5. 首次启动与生命周期

1. 检查包完整性、数据目录权限与单实例锁，显示本地启动界面。
2. 首次使用由用户在 App 安全输入框设置管理员密码；不在聊天、命令行或日志中输入／输出密码。
3. 在新数据目录初始化专用 PostgreSQL cluster，生成数据库专用随机凭据；使用 SCRAM 认证及用户专属 socket 权限。
4. 启动数据库，确认就绪后执行现有 Alembic 迁移。迁移失败时停止启动，不悄悄切为内存数据或 fixture。
5. 经生产管理员创建函数完成初始账号设置；临时密码文件只在受保护目录存在，完成后清除。已有管理员时不重置密码。
6. 启动原有 FastAPI 生产装配，确认本次进程的 readiness 与预期迁移版本，然后加载同源页面。
7. 通过现有设置页配置模型凭据与兼容性验证；不随包携带当前 Docker 凭据卷、私有会话或个人文件。不自动发起计费探针。

启动失败应显示具体阶段和可导出的脱敏日志，并允许重试；不能只显示空白窗口。重复打开 App 只激活现有窗口。关闭窗口与退出应用区分：退出时先停止接收新任务，处理运行中任务，再停止后端和本 App 的数据库。

原生进程与监督进程用私有控制通道管理生命周期；父进程失联后执行有界清理。只根据本次专有数据目录、PID 和进程身份处理子进程，不用 killall、不按端口杀进程、不停止外部 Docker 服务。强制退出后下次启动验证数据库恢复与锁状态，不删除数据库以求启动成功。

## 6. 界面与业务兼容

- 复用登录、项目／会话、Agent 问答、SSE、记忆、文件上传、图纸审查、参数化出图、导出、设置与人工确认界面。
- 保留 cookie、CSRF、项目授权、工具治理、引用校验及程序化计算边界，桌面模式不提供免审特权。
- WKWebView 主窗口只加载本 App 的服务地址；外部网页由系统浏览器处理。拒绝页面导航到本地任意文件或其他协议。
- 验证文件选择、拖拽上传、blob 下载、Word／Excel／DXF／DWG 导出和中文文件名；下载目的地由用户选择，服务给出的文件名不是任意写路径。
- 服务断开或崩溃时回到明确的恢复界面，避免页面继续假装服务在线。

## 7. 索引、DWG 与数据搬移

默认随包携带精选规范及 `text-embedding-3-large / 3072` 索引的只读发布副本，核对既有逐文件摘要、模型维度元数据，并在 macOS 原生 TriviumDB 中验证打开与检索；不能把 Linux 实测当作 macOS 已验证。首次没有模型凭据时按现有规则显示未配置或关键词检索状态，不声称离线生成式回答可用。

LibreDWG 沿用当前项目 0.14 的版本与来源摘要，构建 macOS 原生二进制，保留受控子进程与资源上限语义。分发材料应包含许可清单、对应源码及构建说明，不只复制 Linux 的 `.so` 或宣称已通过法律审查。若 macOS 转换验证未通过，交付清单明确列为 blocked，不把 DWG 功能标为可用。

App 可在其他同架构 Mac 全新安装。已有个人数据通过显式“备份／恢复”流程搬移：使用随包 pg_dump/pg_restore 与用户文件清单，不在数据库运行时直接复制 cluster。备份默认排除 API Key 和 trace 原文；恢复目标检查、摘要校验和覆盖确认沿用现有安全原则。不得让用户为了换电脑自行定位 Docker 卷。

## 8. 更新与签名

第一版不实现后台自动更新。替换 App 前先保留可回滚程序包，对需要迁移的数据先备份；不承诺数据库降级可直接打开。恢复旧程序涉及 schema 不兼容时，必须从相应备份恢复到独立目标。

签名覆盖原生程序、Python 扩展与随包工具，输出签名验证及依赖检查结果。当前已检查到本机 Developer ID Application 身份数量为 0，因此可以先做本机开发构建与可搬移性测试，但正式签名／公证分发目前是缺少外部条件的 blocked 项。不得关闭 Gatekeeper 或把临时签名称为 Apple 公证通过。

## 9. 分阶段输入、输出与验收

| 阶段 | 输入与输出 | 验收条件 |
|---|---|---|
| A 可搬移运行时 | 锁定源码／依赖 → Python、PostgreSQL、LibreDWG、前端与索引资源 | 在含空格和中文的新目录运行；受控环境无 Homebrew/Python/Node/PostgreSQL/Docker 可执行路径；无开发机绝对依赖、无私有密钥 |
| B 生命周期与存储 | 运行时资源 → 独立初始化、迁移、ready、退出与恢复 | 单实例、空闲端口、失败回滚、正常退出、强制退出再启动、数据保留；不影响现有服务 |
| C Mac 窗口与设置 | 本地服务 → 可双击的 App | 首次设置、登录保持、SSE、上传下载、中文文件名、外部导航策略、断线恢复实测 |
| D 业务回归与打包 | App → 验证报告、DMG 与清单 | AGENTS.md 门禁及相关前后端测试；真实 PostgreSQL 下会话／记忆／授权／导出测试；离线机制探针与真实模型质量证据明确分开 |
| E 分发验收 | 构建包、签名身份、另一台 Mac → 分发证据 | Developer ID 签名、公证与另一台干净同架构 Mac 安装、启动、保存会话和备份恢复；缺前置条件保持 blocked |

仅在实际执行后记录通过；网络、权限、原生依赖或签名失败不得用假的成功状态替代。真实模型用例缺 Key 输出结构化 SKIP，不据此宣称真实问答验收通过。既有 RAG 质量、top_k 偏差与 S16 的 blocked 项不会因桌面打包解除。

## 10. 本轮不包含

- SQLite 存储迁移、跨平台 Windows／手机端、Intel 兼容声明。
- 模型能力或质量宣称升级、检索参数／Prompt／gold／阈值改动、思考回传与展示。
- 自动复制现有生产数据库、API Key、MCP 凭据或用户文件。
- App Store 上架、后台自动更新、多实例数据库共享。

## 11. 一手资料

- [Apple WKWebView](https://developer.apple.com/documentation/webkit/wkwebview)：原生窗口内的 Web 内容组件。
- [Apple WKDownload](https://developer.apple.com/documentation/webkit/wkdownload)：WebKit 下载接线参考。
- [Apple Developer ID](https://developer.apple.com/developer-id/) 与[公证说明](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)：正式分发与本机开发构建分开验收。
- [PostgreSQL 支持平台](https://www.postgresql.org/docs/current/supported-platforms.html)与[官方源码目录](https://www.postgresql.org/ftp/source/)：Mac 构建来源；平台支持不替代本 App 的集成验证。
- [uv 管理 Python](https://docs.astral.sh/uv/guides/install-python/) 与 [python-build-standalone](https://docs.astral.sh/python-build-standalone/)：可分发运行时来源；具体原生扩展和搬移仍需验收。

本文不声称安装包、原生 PostgreSQL、DWG 转换或签名已经构建／通过；当前交付是可审阅的打包设计。
