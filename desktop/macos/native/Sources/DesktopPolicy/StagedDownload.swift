import Foundation
import Darwin

/// WebKit requires a destination that does not exist. The user's selected file
/// is untouched until a completed download can be atomically renamed over it.
public final class StagedDownload {
    public let destination: URL
    public let fileURL: URL
    private let directory: URL
    private let replacementConfirmed: Bool
    private var active = true

    public init(destination: URL) throws {
        guard destination.isFileURL, destination.path.hasPrefix("/") else {
            throw DesktopPolicyError.invalidArguments
        }
        self.destination = destination
        var destinationMetadata = stat()
        replacementConfirmed = lstat(destination.path, &destinationMetadata) == 0
        let parent = destination.deletingLastPathComponent()
        var template = Array(parent.appendingPathComponent(".water-agent-download-XXXXXX").path.utf8CString)
        guard mkdtemp(&template) != nil else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        directory = URL(fileURLWithPath: String(cString: template), isDirectory: true)
        fileURL = directory.appendingPathComponent("download")
    }

    public func commit() throws {
        defer { cancel() }
        guard active else { throw DesktopPolicyError.invalidArguments }
        var metadata = stat()
        guard lstat(fileURL.path, &metadata) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        guard (metadata.st_mode & S_IFMT) == S_IFREG else {
            throw DesktopPolicyError.invalidArguments
        }
        guard chmod(fileURL.path, S_IRUSR | S_IWUSR) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        // Both paths are in the selected parent directory. Existing selections
        // were confirmed by NSSavePanel; new selections may not overwrite a
        // file that appeared while downloading. A hard link publishes complete
        // bytes atomically and fails with EEXIST instead of replacing that file.
        let result = replacementConfirmed
            ? rename(fileURL.path, destination.path)
            : link(fileURL.path, destination.path)
        guard result == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
    }

    public func cancel() {
        guard active else { return }
        active = false
        try? FileManager.default.removeItem(at: directory)
    }

    deinit { cancel() }
}
