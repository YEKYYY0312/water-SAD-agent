import XCTest
@testable import DesktopPolicy

final class PolicyTests: XCTestCase {
    func testReadyOnlyAcceptsExplicitLoopbackPortWithoutCredentials() {
        XCTAssertNotNil(ServiceOrigin(readyURL: "http://127.0.0.1:32145"))
        for value in ["https://127.0.0.1:32145", "http://localhost:32145", "http://127.0.0.1", "http://user@127.0.0.1:32145", "file:///tmp/x", "http://127.0.0.1:32145/other", "http://127.0.0.1:0", "http://127.0.0.1:65536"] {
            XCTAssertNil(ServiceOrigin(readyURL: value), value)
        }
    }

    func testNavigationIsSameOriginOrExplicitUserExternalLink() throws {
        let origin = try XCTUnwrap(ServiceOrigin(readyURL: "http://127.0.0.1:32145"))
        XCTAssertEqual(origin.navigation(URL(string: "http://127.0.0.1:32145/settings")!, userLink: false), .allow)
        XCTAssertEqual(origin.navigation(URL(string: "https://example.com/help")!, userLink: true), .external)
        XCTAssertEqual(origin.navigation(URL(string: "https://example.com/help")!, userLink: false), .deny)
        for value in ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,x", "http://127.0.0.1:8000", "http://127.0.0.1:32146", "http://evil@127.0.0.1:32145/x"] {
            XCTAssertEqual(origin.navigation(URL(string: value)!, userLink: false), .deny, value)
        }
        XCTAssertFalse(origin.contains(URL(string: "http://127.0.0.1.evil.test:32145")!))
    }

    func testBlobOriginAndDownloadNames() throws {
        let origin = try XCTUnwrap(ServiceOrigin(readyURL: "http://127.0.0.1:32145"))
        XCTAssertTrue(origin.allowsDownload(URL(string: "blob:http://127.0.0.1:32145/abcd")!))
        XCTAssertFalse(origin.allowsDownload(URL(string: "blob:https://example.com/abcd")!))
        XCTAssertEqual(safeDownloadName("../../图纸.dxf"), "图纸.dxf")
        XCTAssertEqual(safeDownloadName("C:\\目录\\材料.xlsx"), "材料.xlsx")
        XCTAssertEqual(safeDownloadName("\n..\u{0000}"), "下载文件")
        XCTAssertEqual(safeDownloadName("计算书.docx"), "计算书.docx")
        XCTAssertLessThanOrEqual(safeDownloadName(String(repeating: "中", count: 400)).utf8.count, 240)
    }

    func testProtocolRejectsMalformedOrInvalidEvents() throws {
        XCTAssertEqual(try RuntimeEvent.parse(Data(#"{"event":"stage","message":"正在启动"}"#.utf8)), .stage("正在启动"))
        XCTAssertEqual(try RuntimeEvent.parse(Data(#"{"event":"setup_required","username":"admin","minimum_password_length":12}"#.utf8)), .setupRequired(username: "admin", minimumLength: 12))
        XCTAssertEqual(try RuntimeEvent.parse(Data(#"{"event":"ready","url":"http://127.0.0.1:32145"}"#.utf8)), .ready("http://127.0.0.1:32145"))
        XCTAssertEqual(try RuntimeEvent.parse(Data(#"{"event":"error","message":"启动失败","code":"FAILED"}"#.utf8)), .error(message: "启动失败", code: "FAILED"))
        XCTAssertEqual(try RuntimeEvent.parse(Data(#"{"event":"backup_complete","path":"/tmp/备份.tar"}"#.utf8)), .backupComplete("/tmp/备份.tar"))
        for value in ["{}", "[]", "not JSON", #"{"event":"ready","url":"https://evil.test"}"#, #"{"event":"setup_required","minimum_password_length":1}"#, #"{"event":"unknown"}"#, #"{"event":"backup_complete","path":"relative"}"#] {
            XCTAssertThrowsError(try RuntimeEvent.parse(Data(value.utf8)), value)
        }
    }

    func testPasswordAndDataArgumentValidation() throws {
        XCTAssertFalse(validInitialPassword("12345678901", repeated: "12345678901", minimum: 12))
        XCTAssertFalse(validInitialPassword("123456789012", repeated: "123456789013", minimum: 12))
        XCTAssertTrue(validInitialPassword("123456789012", repeated: "123456789012", minimum: 12))
        XCTAssertThrowsError(try DesktopArguments(arguments: ["--url", "https://example.com"]))
        XCTAssertThrowsError(try DesktopArguments(arguments: ["--data-dir", "relative"]))
        XCTAssertThrowsError(try DesktopArguments(arguments: ["--data-dir"]))
        XCTAssertEqual(try DesktopArguments(arguments: ["--data-dir", "/tmp/隔离测试"]).dataDirectory.path, "/tmp/隔离测试")
    }
    func testCompletedDownloadReplacesOnlyAfterFinishAndFailurePreservesOriginal() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: root) }
        let destination = root.appendingPathComponent("计算书.docx")
        try Data("original".utf8).write(to: destination)
        let completed = try StagedDownload(destination: destination)
        XCTAssertFalse(FileManager.default.fileExists(atPath: completed.fileURL.path))
        try Data("complete".utf8).write(to: completed.fileURL)
        XCTAssertEqual(try Data(contentsOf: destination), Data("original".utf8))
        try completed.commit()
        XCTAssertEqual(try Data(contentsOf: destination), Data("complete".utf8))
        XCTAssertFalse(FileManager.default.fileExists(atPath: completed.fileURL.deletingLastPathComponent().path))
        let failed = try StagedDownload(destination: destination)
        try Data("incomplete".utf8).write(to: failed.fileURL)
        failed.cancel()
        XCTAssertEqual(try Data(contentsOf: destination), Data("complete".utf8))
        XCTAssertFalse(FileManager.default.fileExists(atPath: failed.fileURL.deletingLastPathComponent().path))
        let missing = try StagedDownload(destination: destination)
        XCTAssertThrowsError(try missing.commit())
        XCTAssertEqual(try Data(contentsOf: destination), Data("complete".utf8))
    }

    func testNewDownloadDoesNotOverwriteFileThatAppearsDuringDownload() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: root) }
        let destination = root.appendingPathComponent("材料.xlsx")
        let staged = try StagedDownload(destination: destination)
        try Data("download".utf8).write(to: staged.fileURL)
        try Data("other-save".utf8).write(to: destination)
        XCTAssertThrowsError(try staged.commit())
        XCTAssertEqual(try Data(contentsOf: destination), Data("other-save".utf8))
        XCTAssertFalse(FileManager.default.fileExists(atPath: staged.fileURL.deletingLastPathComponent().path))
    }

}
