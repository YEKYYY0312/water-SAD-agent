# macOS 原生窗口

此 Swift Package 只负责本地 App 的窗口、WebKit、受限导航和监督进程控制，复用现有 React 工作区。最低编译目标 macOS 14、当前构建为 arm64；更低系统版本与其他机器仍须实际验收。

```bash
swift test --package-path desktop/macos/native
swift build --package-path desktop/macos/native -c release --product WaterAgentDesktop
swift run --package-path desktop/macos/native -c release PolicyChecks
```

`swift test` 需要包含 XCTest 的 Xcode 环境。本机只有 Command Line Tools，`import XCTest` 报 `no such module 'XCTest'`，因此不能声称 XCTest 通过；保留 `Tests/DesktopPolicyTests` 供有 XCTest 的环境运行。`PolicyChecks` 是无外部依赖的独立可执行断言，验证相同的 origin / 导航 / blob 下载 / 中文文件名 / 控制事件 / 密码及启动参数边界，输出数量和非零失败退出码，不替代真实 UI 验收。

构建产物为 `.build/release/WaterAgentDesktop`（arm64 实际目录 `.build/arm64-apple-macosx/release/WaterAgentDesktop`）。打包器将其复制到 `给排水 Agent.app/Contents/MacOS/`。资源只从 `Bundle.main.resourceURL` 读取，不支持远程 URL 覆盖，也不在页面注入原生权限桥。

运行参数缺省使用 `~/Library/Application Support/WaterAgent`；`--data-dir <绝对路径>` 仅供隔离验收。App 使用该数据根派生的固定 WebKit persistent store UUID，隔离 QA 数据与正常数据的 cookie。Launch Services 重开会激活已有窗口；普通关闭窗口不停止服务，Dock 可重开，⌘Q 才有界停止服务。

监督进程路径固定为 `Resources/python/bin/python3.12 -I -B Resources/runtime/launcher.py --resources Resources --data-dir <数据根>`。`-B` 防止导入时写入签名后的资源。密码只作为 JSON 写入私有 stdin，不进参数、stdout 或原生日志；stderr 不在窗口中回显，后端日志由 runtime 写入本应用日志目录。控制 stdout 逐行读取并限制单条消息 64 KiB。退出先发 stop、关闭 stdin；20 秒后只向本次直接子进程发 SIGTERM，再过 3 秒仍运行才对它发 SIGKILL。监督进程负责关闭自己的业务服务与数据库。

“本地服务”菜单提供重新加载、备份、恢复、数据目录与日志目录。备份目标必须是用户通过保存面板选择的新文件；恢复必须先选文件、确认覆盖警告，再停止监督进程，以 `--restore <文件> --force` 启动并继续正常启动链。文件上传使用系统选择器及原有 WebKit 拖放；下载经 WKDownload 和系统保存面板。下载名去除目录部分、控制字符并限制 UTF-8 长度；仅允许当前服务 origin 和其 blob。用户确认覆盖已有文件后，WebKit 仍只写入目标目录下权限 0700 的私有临时目录，成功完成才原子替换目标；失败或取消只清理临时目录，保留原文件。新文件名若在下载期间被其他保存操作占用，发布失败并保留先前文件。系统保存面板的覆盖确认由 macOS 管理。恢复与正常启动期间菜单重载/恢复/备份均禁用，直到 ready/error；初始化参数或资源缺失时也不能从菜单启动服务。

外部 http(s) 必须来自 App 页面的显式链接激活，再显示原生浏览器确认框；后台导航、file/data/javascript 及任意非当前 origin 页内导航被拒绝。登录、CSRF、授权、引用和计算验证都继续使用服务原有实现。

首次设置、真实登录、SSE、拖放、下载与恢复需要打包后的 App 做集成验收，纯策略断言与编译不能证明这些能力已通过。
