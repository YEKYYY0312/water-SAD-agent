import AppKit
import WebKit
import CryptoKit
import DesktopPolicy

final class DesktopApp: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate, NSMenuItemValidation {
    private var window: NSWindow!
    private let runtime = RuntimeController()
    private var dataDirectory: URL?
    private var resources: URL?
    private var origin: ServiceOrigin?
    private var webView: WKWebView?
    private var statusMessage: NSTextField?
    private var password: NSSecureTextField?
    private var repeatedPassword: NSSecureTextField?
    private var passwordMinimum = 12
    private var setupButton: NSButton?
    private var quitting = false
    private var busy = false
    private var lastBackupDestination: URL?
    private var downloads: [ObjectIdentifier: (download: WKDownload, staging: StagedDownload)] = [:]
    private var pendingCookies: [HTTPCookie]?
    private var sessionTimer: Timer?
    private var lastSession = Date.distantPast
    private var sessionRefreshPending = false
    private var reloadAfterSession = false
    private var initialized: Bool { resources != nil && dataDirectory != nil }

    func applicationDidFinishLaunching(_ notification: Notification) {
        makeMenus()
        makeWindow()
        do {
            dataDirectory = try DesktopArguments(arguments: Array(CommandLine.arguments.dropFirst())).dataDirectory
        } catch {
            showFailure("启动参数无效。仅支持使用 --data-dir 指定隔离验收的数据目录。", canRetry: false)
            return
        }
        guard let resourceURL = Bundle.main.resourceURL else {
            showFailure("应用资源不存在，请重新复制完整的给排水 Agent.app。", canRetry: false)
            return
        }
        resources = resourceURL
        runtime.onEvent = { [weak self] event in self?.receive(event) }
        runtime.onFailure = { [weak self] message in
            guard let self, !self.quitting else { return }
            self.showFailure(message)
        }
        // Launch Services normally reuses the app; direct duplicate executable launches also activate it.
        if let identifier = Bundle.main.bundleIdentifier, !CommandLine.arguments.contains("--data-dir"),
           let existing = NSRunningApplication.runningApplications(withBundleIdentifier: identifier)
            .first(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }) {
            existing.activate(options: [.activateAllWindows])
            NSApp.terminate(nil)
            return
        }
        sessionTimer = Timer.scheduledTimer(withTimeInterval: 3 * 3600, repeats: true) { [weak self] _ in
            self?.refreshLocalSession()
        }
        start()
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        if Date().timeIntervalSince(lastSession) > 300 { refreshLocalSession() }
    }

    private func refreshLocalSession(reload: Bool = false) {
        guard !busy, !quitting, webView != nil, runtime.isRunning, !sessionRefreshPending else { return }
        sessionRefreshPending = true
        reloadAfterSession = reload || Date().timeIntervalSince(lastSession) > 7 * 3600
        if !runtime.send(["command": "refresh_session"]) { sessionRefreshPending = false }
    }

    private func makeWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "给排水 Agent"
        window.minSize = NSSize(width: 760, height: 600)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func makeMenus() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu(title: "给排水 Agent")
        appMenu.addItem(withTitle: "关于给排水 Agent", action: #selector(about), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏给排水 Agent", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出给排水 Agent", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
        let edit = NSMenu(title: "编辑")
        for (title, action, key) in [("撤销", "undo:", "z"), ("剪切", "cut:", "x"), ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        editItem.submenu = edit; main.addItem(editItem)
        let serviceItem = NSMenuItem(title: "本地服务", action: nil, keyEquivalent: "")
        let service = NSMenu(title: "本地服务")
        for (title, action, key) in [("重新加载界面", #selector(reload), "r"), ("备份数据…", #selector(backup), ""), ("从备份恢复…", #selector(restore), ""), ("打开数据目录", #selector(openData), ""), ("打开日志目录", #selector(openLogs), "")] {
            let item = service.addItem(withTitle: title, action: action, keyEquivalent: key); item.target = self
        }
        serviceItem.submenu = service; main.addItem(serviceItem)
        let windowItem = NSMenuItem(title: "窗口", action: nil, keyEquivalent: "")
        let windowMenu = NSMenu(title: "窗口")
        let show = windowMenu.addItem(withTitle: "显示主窗口", action: #selector(showWindow), keyEquivalent: "0"); show.target = self
        windowItem.submenu = windowMenu; main.addItem(windowItem)
        NSApp.windowsMenu = windowMenu
        NSApp.mainMenu = main
        appMenu.items.first?.target = self
    }

    private func start(restore: URL? = nil) {
        guard !quitting, let resources, let dataDirectory else { return }
        origin = nil; webView = nil; busy = true; pendingCookies = nil; sessionRefreshPending = false
        showStatus(title: restore == nil ? "正在准备给排水 Agent" : "正在恢复备份", message: "数据保存在这台 Mac 的独立应用目录中。", spinning: true)
        runtime.start(resources: resources, dataDirectory: dataDirectory, restore: restore)
    }

    private func label(_ text: String, size: CGFloat = 14, bold: Bool = false) -> NSTextField {
        let label = NSTextField(wrappingLabelWithString: text)
        label.font = bold ? .boldSystemFont(ofSize: size) : .systemFont(ofSize: size)
        label.alignment = .center
        label.preferredMaxLayoutWidth = 480
        label.setContentCompressionResistancePriority(.required, for: .vertical)
        return label
    }

    @discardableResult
    private func showStatus(title: String, message: String, spinning: Bool) -> NSStackView {
        password = nil; repeatedPassword = nil; setupButton = nil
        let container = NSView()
        let stack = NSStackView()
        stack.orientation = .vertical; stack.alignment = .centerX; stack.spacing = 18
        stack.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: container.centerYAnchor),
            stack.widthAnchor.constraint(equalToConstant: 500),
        ])
        let icon = NSImageView(image: NSImage(systemSymbolName: "drop.fill", accessibilityDescription: "给排水 Agent") ?? NSImage())
        icon.contentTintColor = .systemTeal
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 54, weight: .regular)
        stack.addArrangedSubview(icon)
        stack.addArrangedSubview(label(title, size: 25, bold: true))
        let body = label(message)
        body.textColor = .secondaryLabelColor
        statusMessage = body; stack.addArrangedSubview(body)
        if spinning {
            let indicator = NSProgressIndicator(); indicator.style = .spinning
            indicator.setAccessibilityLabel("正在启动本地服务")
            indicator.startAnimation(nil); stack.addArrangedSubview(indicator)
        }
        window.contentView = container
        return stack
    }

    private func showFailure(_ message: String, canRetry: Bool = true) {
        busy = false
        webView?.stopLoading()
        origin = nil; webView = nil
        let stack = showStatus(title: "本地服务暂不可用", message: message, spinning: false)
        if canRetry && initialized {
            let button = NSButton(title: "重试启动", target: self, action: #selector(retry))
            button.bezelStyle = .rounded; stack.addArrangedSubview(button)
        }
        let logs = NSButton(title: "打开日志目录", target: self, action: #selector(openLogs))
        logs.bezelStyle = .rounded; logs.isEnabled = dataDirectory != nil; stack.addArrangedSubview(logs)
    }

    private func receive(_ event: RuntimeEvent) {
        switch event {
        case .stage(let message):
            if webView == nil && password == nil { statusMessage?.stringValue = message }
        case .setupRequired:
            showFailure("运行环境仍使用旧的登录版本，请重新复制完整的 App。")
        case .localSession(let path):
            guard let dataDirectory else { return }
            do {
                let cookies = try readLocalSession(file: URL(fileURLWithPath: path), dataRoot: dataDirectory)
                pendingCookies = cookies
                lastSession = Date(); sessionRefreshPending = false
                if let view = webView, let currentOrigin = origin, !busy {
                    installCookies(cookies, in: view, origin: currentOrigin, load: reloadAfterSession)
                    reloadAfterSession = false
                }
            } catch { showFailure("无法恢复本地个人会话，请重新打开 App。") }
        case .ready(let value):
            guard let origin = ServiceOrigin(readyURL: value) else { showFailure("本地服务地址不符合安全约束。"); return }
            self.origin = origin
            busy = false
            displayWeb(origin)
        case .error(let message, let code):
            busy = false
            if origin != nil { busy = false; lastBackupDestination = nil; alert("本地操作失败", text: "\(message)\n错误码：\(code)") }
            else { showFailure("\(message)\n错误码：\(code)") }
        case .backupComplete(let path):
            guard let destination = lastBackupDestination, destination.path == path else { return }
            busy = false
            lastBackupDestination = nil
            let alert = NSAlert(); alert.messageText = "备份已完成"; alert.informativeText = path
            alert.addButton(withTitle: "好"); alert.addButton(withTitle: "在 Finder 中显示")
            alert.beginSheetModal(for: window) { response in
                if response == .alertSecondButtonReturn { NSWorkspace.shared.activateFileViewerSelecting([destination]) }
            }
        }
    }

    @objc private func submitSetup() {
        guard let first = password, let second = repeatedPassword else { return }
        guard validInitialPassword(first.stringValue, repeated: second.stringValue, minimum: passwordMinimum) else {
            statusMessage?.stringValue = "密码须至少 \(passwordMinimum) 个字符，且两次输入一致。"
            window.makeFirstResponder(first)
            return
        }
        let value = first.stringValue
        first.stringValue = ""; second.stringValue = ""; setupButton?.isEnabled = false
        if runtime.send(["command": "setup", "password": value]) {
            showStatus(title: "正在启动本地服务", message: "正在完成管理员设置与数据库准备…", spinning: true)
        }
    }

    private func displayWeb(_ origin: ServiceOrigin) {
        guard let dataDirectory else { return }
        let configuration = WKWebViewConfiguration()
        configuration.applicationNameForUserAgent = "WaterAgentDesktop/0.1"
        // Stable, app-specific persistent store; QA data roots receive their own isolated cookies.
        let digest = Array(SHA256.hash(data: Data(dataDirectory.path.utf8)))
        let identifier = UUID(uuid: (digest[0], digest[1], digest[2], digest[3], digest[4], digest[5], digest[6], digest[7], digest[8], digest[9], digest[10], digest[11], digest[12], digest[13], digest[14], digest[15]))
        configuration.websiteDataStore = WKWebsiteDataStore(forIdentifier: identifier)
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = self; view.uiDelegate = self
        view.setAccessibilityLabel("给排水 Agent 工作区")
        view.allowsBackForwardNavigationGestures = false
        webView = view; window.contentView = view
        guard let cookies = pendingCookies else {
            showFailure("本地个人会话尚未准备完成，请重新打开 App。")
            return
        }
        pendingCookies = nil
        installCookies(cookies, in: view, origin: origin, load: true)
    }

    private func installCookies(_ cookies: [HTTPCookie], in view: WKWebView, origin: ServiceOrigin, load: Bool) {
        let finished = DispatchGroup()
        for cookie in cookies {
            finished.enter()
            view.configuration.websiteDataStore.httpCookieStore.setCookie(cookie) { finished.leave() }
        }
        finished.notify(queue: .main) { [weak self, weak view] in
            guard let self, let view, !self.quitting, self.webView === view, self.origin == origin else { return }
            if load { view.load(URLRequest(url: origin.url)) }
        }
    }

    private func alert(_ title: String, text: String) {
        let alert = NSAlert(); alert.messageText = title; alert.informativeText = text
        alert.addButton(withTitle: "好"); alert.beginSheetModal(for: window)
    }

    @objc private func retry() {
        guard initialized, !busy, !quitting else { return }; busy = true
        showStatus(title: "正在重新启动", message: "正在安全停止当前本地服务…", spinning: true)
        runtime.stop { [weak self] in self?.start() }
    }
    @objc private func reload() {
        guard initialized, !busy, !quitting else { return }
        if webView != nil { refreshLocalSession(reload: true) } else { retry() }
    }
    @objc private func showWindow() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    @objc private func about() { NSApp.orderFrontStandardAboutPanel(options: [.applicationName: "给排水 Agent", .applicationVersion: "本地独立版"]) }
    @objc private func openData() { if let dataDirectory { NSWorkspace.shared.open(dataDirectory) } }
    @objc private func openLogs() { if let dataDirectory { NSWorkspace.shared.open(dataDirectory.appendingPathComponent("logs", isDirectory: true)) } }

    @objc private func backup() {
        guard initialized, origin != nil, !busy, !quitting else { alert("暂不能备份", text: "请等待本地服务启动或当前操作完成。"); return }
        let panel = NSSavePanel(); panel.title = "保存给排水 Agent 备份"
        panel.nameFieldStringValue = "给排水Agent-备份-\(ISO8601DateFormatter().string(from: Date()).prefix(10)).tar.gz"
        panel.canCreateDirectories = true
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self, self.initialized, !self.busy, !self.quitting, response == .OK, let destination = panel.url else { return }
            guard !FileManager.default.fileExists(atPath: destination.path) else {
                self.alert("请选择新文件", text: "备份不会覆盖已有文件，请使用一个新的文件名。"); return
            }
            self.lastBackupDestination = destination; self.busy = true
            if !self.runtime.send(["command": "backup", "destination": destination.path]) {
                self.busy = false; self.lastBackupDestination = nil
            }
        }
    }

    @objc private func restore() {
        guard initialized, !busy, !quitting else { return }
        let panel = NSOpenPanel(); panel.title = "选择给排水 Agent 备份"
        panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self, self.initialized, !self.busy, !self.quitting, response == .OK, let backup = panel.url else { return }
            let confirm = NSAlert(); confirm.alertStyle = .warning
            confirm.messageText = "恢复将覆盖本 App 的当前数据"
            confirm.informativeText = "将先停止本地服务，再验证并恢复所选备份。请先保存当前工作并备份现有数据。\n\(backup.lastPathComponent)"
            confirm.addButton(withTitle: "取消"); confirm.addButton(withTitle: "停止服务并恢复")
            confirm.beginSheetModal(for: self.window) { result in
                guard result == .alertSecondButtonReturn, self.initialized, !self.busy, !self.quitting else { return }
                self.busy = true
                self.webView?.stopLoading(); self.webView = nil; self.origin = nil
                self.showStatus(title: "正在准备恢复", message: "正在停止本 App 的本地服务…", spinning: true)
                self.runtime.stop { [weak self] in self?.start(restore: backup) }
            }
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !quitting else { return .terminateLater }
        quitting = true
        cancelDownloads()
        guard runtime.isRunning else { return .terminateNow }
        showStatus(title: "正在退出", message: "正在安全关闭本地服务与数据库…", spinning: true)
        runtime.stop { NSApp.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { showWindow() }; return true
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        switch menuItem.action {
        case #selector(reload), #selector(restore): return initialized && !busy && !quitting
        case #selector(backup): return initialized && origin != nil && !busy && !quitting
        case #selector(openData), #selector(openLogs): return dataDirectory != nil
        default: return true
        }
    }

    // MARK: WebKit boundaries
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let origin, let url = action.request.url else { decisionHandler(.cancel); return }
        let fromApp = origin.contains(action.sourceFrame.request.url ?? origin.url)
        if action.shouldPerformDownload || url.scheme == "blob" {
            decisionHandler(fromApp && origin.allowsDownload(url) ? .download : .cancel)
            return
        }
        switch origin.navigation(url, userLink: action.navigationType == .linkActivated && fromApp) {
        case .allow:
            if action.targetFrame == nil { decisionHandler(.cancel); webView.load(action.request) }
            else { decisionHandler(.allow) }
        case .external:
            decisionHandler(.cancel)
            confirmExternal(url)
        case .deny: decisionHandler(.cancel)
        }
    }

    private func confirmExternal(_ url: URL) {
        let alert = NSAlert(); alert.messageText = "在浏览器中打开外部链接？"
        alert.informativeText = url.absoluteString
        alert.addButton(withTitle: "取消"); alert.addButton(withTitle: "打开浏览器")
        alert.beginSheetModal(for: window) { response in
            if response == .alertSecondButtonReturn { NSWorkspace.shared.open(url) }
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        guard let origin, let url = response.response.url, origin.allowsDownload(url) else { decisionHandler(.cancel); return }
        let disposition = (response.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        decisionHandler(!response.canShowMIMEType || disposition.lowercased().hasPrefix("attachment") ? .download : .allow)
    }
    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        guard let origin, let url = response.url, origin.allowsDownload(url) else { completionHandler(nil); return }
        let panel = NSSavePanel(); panel.title = "保存导出文件"
        panel.nameFieldStringValue = safeDownloadName(suggestedFilename)
        panel.canCreateDirectories = true
        panel.beginSheetModal(for: window) { [weak self] result in
            guard let self, result == .OK, let url = panel.url else { completionHandler(nil); return }
            do {
                let staging = try StagedDownload(destination: url)
                self.downloads[ObjectIdentifier(download)] = (download, staging)
                completionHandler(staging.fileURL)
            } catch {
                completionHandler(nil)
                self.alert("无法保存下载", text: "保存目录不可写，请选择其他位置后重试导出。")
            }
        }
    }
    func downloadDidFinish(_ download: WKDownload) {
        guard let active = downloads.removeValue(forKey: ObjectIdentifier(download)) else { return }
        do { try active.staging.commit() }
        catch { alert("无法完成保存", text: "原文件未被修改。请检查保存目录后重新导出。") }
    }
    private func cancelDownloads() {
        let active = Array(downloads.values)
        downloads.removeAll()
        for entry in active {
            entry.download.cancel { _ in }
            entry.staging.cancel()
        }
    }
    func download(_ download: WKDownload, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, decisionHandler: @escaping (WKDownload.RedirectPolicy) -> Void) {
        guard let origin, let target = request.url, origin.contains(target) else { decisionHandler(.cancel); return }
        decisionHandler(.allow)
    }
    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        downloads.removeValue(forKey: ObjectIdentifier(download))?.staging.cancel()
        guard !quitting, (error as NSError).code != NSURLErrorCancelled else { return }
        alert("下载未完成", text: "请重试导出，并检查保存目录是否可写。")
    }
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        guard let origin, let source = frame.request.url, origin.contains(source) else { completionHandler(nil); return }
        let panel = NSOpenPanel(); panel.title = "选择要上传的文件"
        panel.canChooseFiles = true; panel.canChooseDirectories = false
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: window) { result in completionHandler(result == .OK ? panel.urls : nil) }
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let failure = error as NSError
        // WebKit cancels provisional loads when a response becomes a download.
        guard failure.code != NSURLErrorCancelled,
              !(failure.domain == "WebKitErrorDomain" && failure.code == 102) else { return }
        showFailure("无法连接本地工作区，请重试启动或查看日志。")
    }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { showFailure("工作区页面进程已停止。请重试启动以恢复界面。") }
}
