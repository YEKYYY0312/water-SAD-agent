# macOS 本地独立 App 验收记录（2026-11-05）

交付源码：`e9232719c6b84e329b5296f0c7599c47ea9256ec`，分支 `codex/macos-standalone-app`。用户要求仅本地使用，并于 2026-11-05 明确取消桌面登录。当前已安装 App 为个人免登录版：启动后通过私有 IPC 建立正常服务端会话，原生 CookieStore 设置 HttpOnly session／CSRF cookie 后直接显示工作台；没有增加 HTTP 免登录接口。已有账户和密码不重置。

## 本地交付

- App：`/Users/yaerken/Applications/给排水 Agent.app`。
- DMG：`/Users/yaerken/Desktop/给排水Agent-独立版-arm64-2026-11-05.dmg`。
- DMG 大小：231971903 bytes；SHA256 `ceb7c8fb009279b1130a01ba989ffa749577bcf8c352947f977ce18e50df0604`。
- 内置 CPython 3.12.14、PostgreSQL 18.2、LibreDWG 0.14、锁定依赖、前端与 text-embedding-3-large／3072 索引。使用机不要求安装 Docker、Homebrew、Python 或 PostgreSQL。
- 用户数据：`~/Library/Application Support/WaterAgent/`；程序与数据分离。API Key 不随包复制，仍由用户在设置页配置。
- 当前目标 arm64；实际机器 macOS 27.0.1。安装包为本地 ad-hoc 签名，没有公证或上架。未把本机搬移实验声明为另一台 Mac／Intel／旧系统已经验证。

## 实测证据

- 桌面 Python 测试 80 项、原生可执行 PolicyChecks 76 条断言、前端 148 项测试与构建均通过。仅 CommandLineTools 的环境没有 XCTest，故未声称 XCTest 运行通过。
- `COMPOSE_PROJECT_NAME=wa-macos-personal-ci bash scripts/ci_gate.sh` exit 0，pytest／ruff／mypy／机制评测／入仓基线比较通过。
- 完整 App 复制到包含中文和空格的新路径，裁剪 PATH 后用独立数据目录运行：22 项验收通过，包括真实 PostgreSQL／迁移、个人会话、匿名请求拒绝、会话与上传重启保留、DXF 生成下载、真实只读索引＋脚本模型引用、无 HTTP 免登录接口、备份恢复、stdin EOF 和停止清理。
- 原生 CUA 实测：已安装的 `/Users/yaerken/Applications/给排水 Agent.app` 自动进入工作台，项目／会话／设置可见，没有登录表单。过程中未代填用户密码。
- 原生依赖从已校验的源码／归档构建；签名与包扫描通过，检查 arm64、相对加载路径、资源摘要和私密材料排除。扫描器的确定性 canary 与公有 CA 例外限制到具体内容及路径。
- 真实 PostgreSQL 的恢复中断测试覆盖 stop、EOF、SIGTERM、SIGKILL，保留已有数据库／上传／凭据；清理同时核对真实 executable 与确切数据目录。
- 原 8000/8001 容器身份、镜像、启动时间与重启数未变。没有复制生产数据库、API Key 或桌面原始检出的用户改动。

## 发现与修复

1. 版本索引位于子目录，启动器最初只查上一级；改为受控目录内唯一索引选择，缺失或歧义响亮失败。
2. Foundation `read(upToCount:4096)` 在实际 Mac pipe 上等待缓冲区填满，导致后端已准备而窗口停留启动页。改为 POSIX 单次短读取，新增保留写端开启的回归。
3. 已有下载目标改为私有暂存，成功后原子替换；恢复保持忙状态；参数错误菜单不解包 nil。恢复取消／目录切换中断由 journal 与 guardian 保证可回滚。
4. 用户取消登录后，改为桌面私有会话；令牌通过 0600 固定文件交接并立即删除，不写参数或日志。普通 Web 认证、会话授权与 CSRF 仍由生产实现裁定。桌面不再显示密码文件指引或退出登录入口。

原始证据在受忽略的 `work/macos-standalone/` 与本任务 `.superpowers/sdd/`。结构化验收及证据摘要见本地 `work/macos-standalone/acceptance-final.json`；包内 `build-manifest.json` 为逐资源清单。

真实 LLM 质量验证因未配置 Key 记为 SKIP；脚本模型的打包机制验证不代表真实模型回答质量。原 RAG 评测边界与 S16 blocked 项不因打包解除。部分 GUI 交互（如实际 SSE 计费回答、拖拽与多种导出）的完整用户流程未独立计分，不以编译／策略测试冒充已验收。
