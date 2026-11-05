# macOS 独立 App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 本机可双击启动、不依赖外装运行时的 Apple Silicon Mac App，内置后端和独立 PostgreSQL。
**Architecture:** Swift/AppKit/WKWebView → 私有 stdin/stdout JSON 控制通道 → Python supervisor → 原生 PostgreSQL Unix socket + 127.0.0.1 随机端口 FastAPI；业务内核复用。
**Tech Stack:** Swift 6 / macOS SDK / Python 3.12 / PostgreSQL 18.2 / React 构建产物。
**Spec:** `docs/design/19-macos-standalone-app.md`。

## Global Constraints

- 用户 2026-11-04 已批准设计并明确不需要上线。只交付本地 App/DMG，不做上架、公证、自动更新或生产部署；本地 ad-hoc 签名与原生依赖验证仍执行。
- 不修改现有 8000/8001 服务、生产数据、密钥或桌面检出。App 独立数据根 `~/Library/Application Support/WaterAgent/`。
- 不改 RAG 参数/Prompt/gold/阈值，不绕过认证/CSRF/授权/数值计算/引用门槛。无模型 Key 不能伪造真实问答验证。
- 包含 ARM64 可搬移 Python、PG、前端、只读索引；不依赖 Homebrew、Docker 或开发目录。
- 密码只经私有 stdin 管道与 0600 临时文件；永不放参数、日志、安装包或仓库。

## 2026-11-05 用户修订（覆盖下方旧登录步骤）

Mac 个人版取消初始密码设置和登录页。初始账号内部随机凭据，既有 admin 不改密码；私有 helper 创建标准服务端 session，通过固定 0600 文件交给原生 CookieStore，再直接进入工作台。令牌不出现在 stdout/参数/日志；无 HTTP 免登录接口。定期/唤醒续期，Web 登录与后端鉴权保留。

## 固定接口与文件边界

资源根为 `给排水 Agent.app/Contents/Resources`，包含 `python/bin/python3.12`、`postgres/bin/{postgres,initdb,pg_ctl,createdb,pg_dump,pg_restore}`、`backend/{src,data,alembic,alembic.ini,web/app/dist}`、`runtime/launcher.py` 及同目录辅助模块、可选 `libredwg/bin/{dwg2dxf,dxf2dwg}`。

Native 启动参数：`<Resources>/python/bin/python3.12 -I -B <Resources>/runtime/launcher.py --resources <Resources> --data-dir <absolute AppSupport root>`。
每行一个 JSON；stdout 只能输出控制事件，日志写本 App 数据目录。

```json
{"event":"stage","message":"正在启动数据库"}
{"event":"setup_required","username":"admin","minimum_password_length":12}
{"event":"ready","url":"http://127.0.0.1:12345"}
{"event":"error","message":"数据库启动失败","code":"DESKTOP_START_FAILED"}
{"event":"backup_complete","path":"/user/chosen/file"}
```

stdin：`{"command":"setup","password":"<用户输入>"}`、`{"command":"stop"}`、`{"command":"backup","destination":"<用户选择的绝对新文件>"}`。stdin EOF 等同有界退出。首次设置后继续启动，不要求重开 App。恢复由 launcher 独立 CLI `--restore <备份绝对路径>` 在服务未启动时处理，覆盖前要求显式 `--force`；Native 菜单若提供恢复入口须先停止本 App 服务。

### Task 1: 运行时监督与私有数据库

**Files:** `desktop/macos/runtime/{launcher.py,service.py,paths.py,backup.py}`；`tests/test_desktop_runtime.py`。
**Consumes:** 固定资源布局、生产 `run_bootstrap(secret_path=)` / Alembic / `create_app`。
**Produces:** 上述 JSON 控制协议，稳定错误码与本地日志；生命周期、路径和备份边界测试。

- [ ] RED：测试不安全路径、权限、单实例、环境隔离、备份越界与停止只针对自有进程；运行 `PYTHONPATH=src _venv/bin/pytest -q tests/test_desktop_runtime.py`，确认因模块不存在失败。
- [ ] GREEN：实现目录 0700 / 密钥 0600、PG Unix socket SCRAM、随机 HTTP 端口、独占锁、迁移和管理员设置，EOF/stop/SIGTERM 清理。`service.py` 在本进程绑定随机 socket 后交给 uvicorn，ready 文件含本次 nonce，supervisor 不接受旧 ready。
- [ ] 生产调用示例：`run_bootstrap(secret_path=private_password_file)`；`command.upgrade(Config(bundle_alembic_ini), 'head')`，script_location 指向绝对资源路径。
- [ ] 备份使用随包 pg_dump + uploads/exports/config 清单和 SHA256，不含凭据/trace；恢复先验证清单及安全 tar 路径，不覆盖已有数据而不确认。
- [ ] 验证 focused tests；保留真实 PG 的启动/重启/备份恢复验证供集成阶段。

### Task 2: 原生 App 窗口

**Files:** `desktop/macos/native/{Package.swift,Sources,Tests}`；原生构建文档。
**Consumes:** 固定资源路径与 JSON 事件协议。
**Produces:** Swift 可执行文件，首次密码界面、启动/故障页、WebView、文件上传下载、退出清理与用户菜单。

- [ ] RED：Swift XCTest 验证仅允许当前 localhost origin、拒绝 file/其他协议、下载名去路径与 JSON 事件解析；运行 `swift test --package-path desktop/macos/native` 得到明确失败。
- [ ] GREEN：AppKit 原生窗口，启动 supervisor、逐行解析事件，12 字符初始密码与重复输入；默认独立持久 WebKit cookie，保持既有登录流程。
- [ ] 使用 WKUIDelegate 文件选择与 WKDownloadDelegate/保存面板处理下载；外部 http(s) 用户链接交给系统浏览器；不提供 JS 原生权限桥。
- [ ] App 退出发 stop、关闭 stdin，超时只终止自有 supervisor；主窗口关闭可从 Dock 重开，不重复启动服务。支持 `--data-dir` 仅用于隔离验收，不接受远程服务地址覆盖。
- [ ] 验证 Swift 测试与 release 编译，记录警告；不修改 Python/前端业务源码。

### Task 3: 原生依赖与可搬移包

**Files:** `desktop/macos/packaging/{build_dependencies.py,build_app.py,verify_bundle.py}`、`desktop/macos/README.md`；`tests/test_desktop_packaging.py`。
**Consumes:** 生产锁文件/源码、Task1/Task2 输出、既有索引发布目录。
**Produces:** App、DMG、依赖/来源/许可/摘要清单；搬移检查及包内密钥排除检查。

- [ ] RED：包清单选择器必须拒绝 `.env`、secrets、_venv、.git、会话/trace、开发路径链接；输入的原生工具必须来自受控缓存，校验 SHA256。focused pytest 先失败。
- [ ] GREEN：下载官方 PostgreSQL 18.2 源码和 checksum（只保存受控缓存），编译 Mac ARM64 工具；使用独立 Python 3.12.14 分发和 uv.lock runtime 依赖。按实际 otool 输出处理 @loader_path/@rpath，拒绝外部 Homebrew 链接。
- [ ] 前端 `npm ci && npm test && npm run build`，打包非 editable 后端源码与必要数据。索引从明确传入的 3072 维发布目录复制并核对摘要，不随意取最新库。
- [ ] LibreDWG 0.14 根据 Dockerfile 已登记 sha256 构建 macOS 工具，附 GPL 源码/许可/构建说明；若编译或回读失败明确阻塞该能力，不静默承诺可用。
- [ ] 逐个 Mach-O ad-hoc 签名，生成 app icon、Info.plist 和 DMG。可用 Swift/AppKit SF Symbol 绘制应用图标，不新增 AI 图片依赖。
- [ ] 验证 focused tests、`codesign --verify --deep --strict` 和 verify_bundle；本机本地构建不宣称已公证或外部 Mac 已验收。

### Task 4: 集成、审查与交付

**Files:** `desktop/macos/packaging/smoke.py`、`docs/evals/macos-standalone-2026-11-04.md`、必要交叉链接。
**Consumes:** 完整 App，隔离临时数据目录。
**Produces:** 真实 PG + HTTP + 原生 UI + 搬移验证证据与本地安装产物。

- [ ] 从含空格/中文的新路径与裁剪环境启动 supervisor，首次密码由测试随机生成经 stdin 输入，不输出值。等待真实 ready，登录并验证数据库 migrations/checkpoint migrations、创建会话/重启后保留、上传下载与备份恢复。
- [ ] 关闭 stdin、正常退出与强制终止 supervisor 后验证无遗留自有 PG/服务进程；旧 8000/8001 容器身份/启动时间保持。
- [ ] 以 AppKit 打开 App 验证首次界面和窗口、登录、上传/下载/恢复界面；只对本 App 的测试账号与隔离数据操作。
- [ ] `COMPOSE_PROJECT_NAME=wa-macos-ci bash scripts/ci_gate.sh`、前端与桌面 focused tests、`git diff --check`；无 Key 真实 RAG 结构化 SKIP，不伪造费用或质量通过。
- [ ] 独立代码审查（生命周期、安全、包搬移和业务边界），修复阻塞问题；生成验收报告与本地 App/DMG 校验摘要。仅本地交付，不上线。
