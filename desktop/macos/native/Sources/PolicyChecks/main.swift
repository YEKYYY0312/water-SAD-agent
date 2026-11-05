// CLT-only environments lack XCTest. These are executable policy assertions,
// not a claim that the XCTest runner or native UI integration has passed.
import Foundation
import DesktopPolicy

var count = 0
func check(_ condition: @autoclosure () -> Bool, _ name: String) {
    count += 1
    guard condition() else {
        FileHandle.standardError.write(Data("FAIL: \(name)\n".utf8))
        exit(1)
    }
}
func rejects(_ name: String, _ body: () throws -> Void) {
    do { try body(); check(false, name) }
    catch { check(true, name) }
}

let origin = ServiceOrigin(readyURL: "http://127.0.0.1:32145")!
for value in ["https://127.0.0.1:32145", "http://localhost:32145", "http://127.0.0.1", "http://user@127.0.0.1:32145", "file:///tmp/x", "http://127.0.0.1:32145/other", "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:32145?x=1", "http://127.0.0.1:32145/#fragment"] {
    check(ServiceOrigin(readyURL: value) == nil, "reject invalid readiness origin: \(value)")
}
check(origin.navigation(URL(string: "http://127.0.0.1:32145/settings")!, userLink: false) == .allow, "same origin allowed")
check(origin.navigation(URL(string: "https://example.com/help")!, userLink: true) == .external, "external user link requires browser confirmation")
check(origin.navigation(URL(string: "https://example.com/help")!, userLink: false) == .deny, "background external navigation denied")
for value in ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,x", "http://127.0.0.1:8000", "http://127.0.0.1:32146", "http://evil@127.0.0.1:32145/x", "http://127.0.0.1.evil.test:32145"] {
    check(origin.navigation(URL(string: value)!, userLink: false) == .deny, "deny navigation: \(value)")
}
check(origin.allowsDownload(URL(string: "blob:http://127.0.0.1:32145/abcd")!), "same origin blob download")
check(!origin.allowsDownload(URL(string: "blob:https://example.com/abcd")!), "foreign blob denied")
check(!origin.allowsDownload(URL(string: "file:///tmp/data")!), "file download denied")
check(safeDownloadName("../../图纸.dxf") == "图纸.dxf", "strip forward paths")
check(safeDownloadName("C:\\目录\\材料.xlsx") == "材料.xlsx", "strip Windows paths")
check(safeDownloadName("\n..\u{0000}") == "下载文件", "strip controls and traversal")
check(safeDownloadName("计算书.docx") == "计算书.docx", "preserve Chinese filename")
check(safeDownloadName(String(repeating: "中", count: 400)).utf8.count <= 240, "UTF8-safe filename bound")
check(!validInitialPassword("12345678901", repeated: "12345678901", minimum: 12), "password length")
check(!validInitialPassword("123456789012", repeated: "123456789013", minimum: 12), "password confirmation")
check(validInitialPassword("123456789012", repeated: "123456789012", minimum: 12), "valid password")
check(!validInitialPassword("123456789012\u{0000}", repeated: "123456789012\u{0000}", minimum: 12), "null password denied")
for value in ["{}", "[]", "not JSON", #"{"event":"ready","url":"https://evil.test"}"#, #"{"event":"setup_required","minimum_password_length":1}"#, #"{"event":"unknown"}"#, #"{"event":"backup_complete","path":"relative"}"#] {
    rejects("invalid protocol event") { _ = try RuntimeEvent.parse(Data(value.utf8)) }
}
let events: [(String, RuntimeEvent)] = [
    (#"{"event":"stage","message":"正在启动"}"#, .stage("正在启动")),
    (#"{"event":"setup_required","username":"admin","minimum_password_length":12}"#, .setupRequired(username: "admin", minimumLength: 12)),
    (#"{"event":"ready","url":"http://127.0.0.1:32145"}"#, .ready("http://127.0.0.1:32145")),
    (#"{"event":"error","message":"启动失败","code":"FAILED"}"#, .error(message: "启动失败", code: "FAILED")),
    (#"{"event":"backup_complete","path":"/tmp/备份.tar"}"#, .backupComplete("/tmp/备份.tar")),
]
for (source, expected) in events { check(try! RuntimeEvent.parse(Data(source.utf8)) == expected, "valid protocol event") }
rejects("oversize protocol event") { _ = try RuntimeEvent.parse(Data(repeating: 32, count: 65_537)) }
rejects("arbitrary URL argument") { _ = try DesktopArguments(arguments: ["--url", "https://example.com"]) }
rejects("relative data directory") { _ = try DesktopArguments(arguments: ["--data-dir", "relative"]) }
rejects("missing data directory") { _ = try DesktopArguments(arguments: ["--data-dir"]) }
check(try! DesktopArguments(arguments: ["--data-dir", "/tmp/隔离测试"]).dataDirectory.path == "/tmp/隔离测试", "isolated QA directory")

let downloadChecksRoot = FileManager.default.temporaryDirectory.appendingPathComponent("water-agent-download-check-\(UUID().uuidString)", isDirectory: true)
try FileManager.default.createDirectory(at: downloadChecksRoot, withIntermediateDirectories: false)
defer { try? FileManager.default.removeItem(at: downloadChecksRoot) }
let chosenDestination = downloadChecksRoot.appendingPathComponent("已有计算书.docx")
try Data("old-content".utf8).write(to: chosenDestination)
let successfulDownload = try StagedDownload(destination: chosenDestination)
check(!FileManager.default.fileExists(atPath: successfulDownload.fileURL.path), "WebKit destination must not exist")
check(try! Data(contentsOf: chosenDestination) == Data("old-content".utf8), "choosing replacement preserves original")
try Data("new-content".utf8).write(to: successfulDownload.fileURL)
check(try! Data(contentsOf: chosenDestination) == Data("old-content".utf8), "in-progress replacement preserves original")
try successfulDownload.commit()
check(try! Data(contentsOf: chosenDestination) == Data("new-content".utf8), "finished download atomically replaces selected file")
check(!FileManager.default.fileExists(atPath: successfulDownload.fileURL.deletingLastPathComponent().path), "successful stage cleaned")
let failedDownload = try StagedDownload(destination: chosenDestination)
try Data("incomplete".utf8).write(to: failedDownload.fileURL)
failedDownload.cancel()
check(try! Data(contentsOf: chosenDestination) == Data("new-content".utf8), "failed download preserves original")
check(!FileManager.default.fileExists(atPath: failedDownload.fileURL.deletingLastPathComponent().path), "failed download stage cleaned")
let missingDownload = try StagedDownload(destination: chosenDestination)
rejects("missing completed download rejected") { try missingDownload.commit() }
check(try! Data(contentsOf: chosenDestination) == Data("new-content".utf8), "missing completed download preserves original")
let directoryTarget = downloadChecksRoot.appendingPathComponent("existing-directory", isDirectory: true)
try FileManager.default.createDirectory(at: directoryTarget, withIntermediateDirectories: false)
let renameFailure = try StagedDownload(destination: directoryTarget)
try Data("download".utf8).write(to: renameFailure.fileURL)
rejects("failed final rename rejected") { try renameFailure.commit() }
check(!FileManager.default.fileExists(atPath: renameFailure.fileURL.deletingLastPathComponent().path), "rename failure stage cleaned")
check(FileManager.default.fileExists(atPath: directoryTarget.path), "rename failure preserves original directory")
let newDestination = downloadChecksRoot.appendingPathComponent("新材料.xlsx")
let freshDownload = try StagedDownload(destination: newDestination)
try Data("complete".utf8).write(to: freshDownload.fileURL)
try freshDownload.commit()
check(try! Data(contentsOf: newDestination) == Data("complete".utf8), "new download published")
let racedDestination = downloadChecksRoot.appendingPathComponent("并发保存.xlsx")
let racedDownload = try StagedDownload(destination: racedDestination)
try Data("download".utf8).write(to: racedDownload.fileURL)
try Data("other-save".utf8).write(to: racedDestination)
rejects("new download cannot clobber a file created during download") { try racedDownload.commit() }
check(try! Data(contentsOf: racedDestination) == Data("other-save".utf8), "concurrent original preserved")
let symbolicDownload = try StagedDownload(destination: chosenDestination)
try FileManager.default.createSymbolicLink(at: symbolicDownload.fileURL, withDestinationURL: newDestination)
rejects("staged symlink cannot replace selected file") { try symbolicDownload.commit() }
check(try! Data(contentsOf: chosenDestination) == Data("new-content".utf8), "symlink stage preserves original")

// A short newline event must be delivered while the producer keeps the pipe open.
// Foundation.read(upToCount:4096) waits for a full buffer on this macOS host.
let controlPipe = Pipe()
let shortEvent = Data("{\"event\":\"setup_required\"}\n".utf8)
let shortDelivered = DispatchSemaphore(value: 0)
DispatchQueue.global().async {
    if let block = try? readControlChunk(from: controlPipe.fileHandleForReading), block == shortEvent {
        shortDelivered.signal()
    }
}
try controlPipe.fileHandleForWriting.write(contentsOf: shortEvent)
let deliveredBeforeEOF = shortDelivered.wait(timeout: .now() + 0.5) == .success
try controlPipe.fileHandleForWriting.close()
check(deliveredBeforeEOF, "short control event is delivered before writer closes")
let controlEOF = try readControlChunk(from: controlPipe.fileHandleForReading)
check(controlEOF == nil, "control EOF is explicit")

print("PolicyChecks final: \(count) assertions passed (XCTest runner not used)")
