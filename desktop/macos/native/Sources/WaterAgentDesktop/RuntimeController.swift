import Foundation
import DesktopPolicy
import Darwin

/// The only control channel is a private pipe to the bundled launcher. No shell.
final class RuntimeController {
    private var process: Process?
    private var input: FileHandle?
    private var generation = UUID()
    private var stopping = false
    private var stopCompletions: [() -> Void] = []
    var onEvent: ((RuntimeEvent) -> Void)?
    var onFailure: ((String) -> Void)?

    var isRunning: Bool { process?.isRunning == true || stopping }

    func start(resources: URL, dataDirectory: URL, restore: URL? = nil) {
        guard process == nil, !stopping else { return }
        let child = Process()
        let stdinPipe = Pipe()
        let stdoutPipe = Pipe()
        let token = UUID()
        generation = token
        child.executableURL = resources.appendingPathComponent("python/bin/python3.12")
        child.arguments = ["-I", "-B", resources.appendingPathComponent("runtime/launcher.py").path,
                           "--resources", resources.path, "--data-dir", dataDirectory.path]
        if let restore { child.arguments! += ["--restore", restore.path, "--force"] }
        child.currentDirectoryURL = resources
        let inherited = ProcessInfo.processInfo.environment
        child.environment = ["HOME": FileManager.default.homeDirectoryForCurrentUser.path,
                             "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
                             "TMPDIR": inherited["TMPDIR"] ?? NSTemporaryDirectory()]
        child.standardInput = stdinPipe
        child.standardOutput = stdoutPipe
        child.standardError = FileHandle.nullDevice
        process = child
        input = stdinPipe.fileHandleForWriting
        do { try child.run() }
        catch {
            process = nil; input = nil
            onFailure?("无法启动随包运行环境，请检查应用是否完整。")
            return
        }
        // Blocking reads stay off the main thread; bounded buffers reject damaged protocols.
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var buffer = Data()
            do {
                while let block = try readControlChunk(from: stdoutPipe.fileHandleForReading), !block.isEmpty {
                    buffer.append(block)
                    while let end = buffer.firstIndex(of: 10) {
                        let line = Data(buffer[..<end]); buffer.removeSubrange(...end)
                        guard !line.isEmpty else { continue }
                        let event = try RuntimeEvent.parse(line)
                        DispatchQueue.main.async { [weak self] in
                            guard self?.generation == token else { return }
                            self?.onEvent?(event)
                        }
                    }
                    guard buffer.count <= 65_536 else { throw DesktopPolicyError.invalidEvent }
                }
                child.waitUntilExit()
                DispatchQueue.main.async { [weak self] in
                    guard let self, self.generation == token else { return }
                    self.input = nil; self.process = nil
                    self.onFailure?("本地服务已停止。可以重试启动，或打开日志查看原因。")
                }
            } catch {
                DispatchQueue.main.async { [weak self] in
                    guard self?.generation == token else { return }
                    self?.onFailure?("本地服务返回了无法识别的控制消息。请重试或检查日志。")
                }
            }
            try? stdoutPipe.fileHandleForReading.close()
        }
    }

    @discardableResult
    func send(_ command: [String: String]) -> Bool {
        guard let input, let data = try? JSONSerialization.data(withJSONObject: command) else { return false }
        do { try input.write(contentsOf: data + Data([10])); return true }
        catch { onFailure?("无法连接本地服务控制通道。"); return false }
    }

    func stop(completion: @escaping () -> Void) {
        stopCompletions.append(completion)
        guard !stopping else { return }
        generation = UUID() // Suppress late events from this exact process only.
        guard let child = process else {
            let callbacks = stopCompletions; stopCompletions.removeAll()
            callbacks.forEach { $0() }
            return
        }
        stopping = true
        _ = send(["command": "stop"])
        try? input?.close(); input = nil
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let deadline = Date().addingTimeInterval(20)
            while child.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
            if child.isRunning { child.terminate() }
            let terminateDeadline = Date().addingTimeInterval(3)
            while child.isRunning && Date() < terminateDeadline { Thread.sleep(forTimeInterval: 0.1) }
            // Process still owns its unreaped child; never find processes by name or port.
            if child.isRunning { kill(child.processIdentifier, SIGKILL) }
            child.waitUntilExit()
            DispatchQueue.main.async {
                self?.process = nil
                self?.stopping = false
                let callbacks = self?.stopCompletions ?? []
                self?.stopCompletions.removeAll()
                callbacks.forEach { $0() }
            }
        }
    }
}
