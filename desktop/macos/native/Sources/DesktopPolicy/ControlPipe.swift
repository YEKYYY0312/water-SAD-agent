import Foundation
import Darwin

/// Read a single available pipe chunk rather than waiting for a full 4 KB.
/// The producer intentionally keeps stdout open for the lifetime of the App.
public func readControlChunk(from handle: FileHandle) throws -> Data? {
    var buffer = [UInt8](repeating: 0, count: 4096)
    while true {
        let count = buffer.withUnsafeMutableBytes {
            Darwin.read(handle.fileDescriptor, $0.baseAddress, $0.count)
        }
        if count > 0 { return Data(buffer.prefix(count)) }
        if count == 0 { return nil }
        if errno == EINTR { continue }
        throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
    }
}
