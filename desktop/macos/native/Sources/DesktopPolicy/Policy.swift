import Foundation

public enum NavigationDecision: Equatable { case allow, external, deny }

public struct ServiceOrigin: Equatable {
    public let url: URL
    public let port: Int

    public init?(readyURL: String) {
        guard let value = URL(string: readyURL), value.scheme == "http",
              value.host == "127.0.0.1", let port = value.port, (1...65535).contains(port),
              value.user == nil, value.password == nil,
              value.query == nil, value.fragment == nil,
              value.path.isEmpty || value.path == "/" else { return nil }
        self.url = value
        self.port = port
    }

    public func contains(_ target: URL) -> Bool {
        target.scheme == "http" && target.host == "127.0.0.1" && target.port == port &&
            target.user == nil && target.password == nil
    }

    public func allowsDownload(_ target: URL) -> Bool {
        if contains(target) { return true }
        guard target.scheme == "blob", let inner = URL(string: String(target.absoluteString.dropFirst(5))) else { return false }
        return contains(inner)
    }

    public func navigation(_ target: URL, userLink: Bool) -> NavigationDecision {
        if contains(target) { return .allow }
        guard userLink, ["http", "https"].contains(target.scheme ?? ""),
              target.host != nil, target.user == nil, target.password == nil else { return .deny }
        return .external
    }
}

public func safeDownloadName(_ suggested: String) -> String {
    let leaf = suggested.replacingOccurrences(of: "\\", with: "/").components(separatedBy: "/").last ?? ""
    let cleaned = String(leaf.unicodeScalars.filter { !CharacterSet.controlCharacters.contains($0) })
        .trimmingCharacters(in: .whitespacesAndNewlines)
        .replacingOccurrences(of: ":", with: "_")
    var result = ""
    for character in cleaned {
        if result.utf8.count + String(character).utf8.count > 240 { break }
        result.append(character)
    }
    return result.isEmpty || result.allSatisfy({ $0 == "." }) ? "下载文件" : result
}

public func validInitialPassword(_ password: String, repeated: String, minimum: Int) -> Bool {
    password.unicodeScalars.count >= max(12, minimum) && password == repeated && !password.contains("\u{0000}")
}

public enum DesktopPolicyError: Error { case invalidArguments, invalidEvent }

public struct DesktopArguments {
    public let dataDirectory: URL
    public init(arguments: [String]) throws {
        if arguments.isEmpty {
            dataDirectory = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Library/Application Support/WaterAgent", isDirectory: true)
        } else {
            guard arguments.count == 2, arguments[0] == "--data-dir", arguments[1].hasPrefix("/"),
                  !arguments[1].contains("\u{0000}") else { throw DesktopPolicyError.invalidArguments }
            dataDirectory = URL(fileURLWithPath: arguments[1], isDirectory: true).standardizedFileURL
        }
    }
}

public enum RuntimeEvent: Equatable {
    case stage(String)
    case setupRequired(username: String, minimumLength: Int)
    case ready(String)
    case error(message: String, code: String)
    case backupComplete(String)

    public static func parse(_ data: Data) throws -> RuntimeEvent {
        guard data.count <= 65_536,
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let kind = object["event"] as? String else { throw DesktopPolicyError.invalidEvent }
        switch kind {
        case "stage":
            guard let message = object["message"] as? String else { throw DesktopPolicyError.invalidEvent }
            return .stage(message)
        case "setup_required":
            guard let username = object["username"] as? String,
                  let minimum = object["minimum_password_length"] as? Int,
                  (12...1024).contains(minimum) else { throw DesktopPolicyError.invalidEvent }
            return .setupRequired(username: username, minimumLength: minimum)
        case "ready":
            guard let value = object["url"] as? String, ServiceOrigin(readyURL: value) != nil else { throw DesktopPolicyError.invalidEvent }
            return .ready(value)
        case "error":
            guard let message = object["message"] as? String, let code = object["code"] as? String else { throw DesktopPolicyError.invalidEvent }
            return .error(message: message, code: code)
        case "backup_complete":
            guard let path = object["path"] as? String, path.hasPrefix("/") else { throw DesktopPolicyError.invalidEvent }
            return .backupComplete(path)
        default: throw DesktopPolicyError.invalidEvent
        }
    }
}
