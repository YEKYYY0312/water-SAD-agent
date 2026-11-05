import Foundation
import Darwin

/// Tokens are received only through a fixed, private file from the App's supervisor.
/// Neither the HTTP server nor page JavaScript can request native authentication.
public func readLocalSession(file: URL, dataRoot: URL) throws -> [HTTPCookie] {
    let expected = dataRoot.appendingPathComponent("credentials/desktop-session.json").standardizedFileURL
    guard file.standardizedFileURL == expected else { throw DesktopPolicyError.invalidEvent }
    let descriptor = open(file.path, O_RDONLY | O_NOFOLLOW)
    guard descriptor >= 0 else { throw DesktopPolicyError.invalidEvent }
    defer { close(descriptor) }
    var attributes = stat()
    guard fstat(descriptor, &attributes) == 0, attributes.st_uid == geteuid(),
          attributes.st_mode & 0o077 == 0, attributes.st_mode & S_IFMT == S_IFREG,
          attributes.st_size > 0, attributes.st_size < 16_384 else { throw DesktopPolicyError.invalidEvent }
    var buffer = [UInt8](repeating: 0, count: Int(attributes.st_size))
    let count = buffer.withUnsafeMutableBytes { Darwin.read(descriptor, $0.baseAddress, $0.count) }
    guard count == buffer.count,
          let object = try JSONSerialization.jsonObject(with: Data(buffer)) as? [String: Any],
          let session = object["session_id"] as? String,
          let csrf = object["csrf_token"] as? String,
          let expires = object["expires_at"] as? Double,
          expires > Date().timeIntervalSince1970,
          expires <= Date().timeIntervalSince1970 + 86_400 else { throw DesktopPolicyError.invalidEvent }
    let token = "^[A-Za-z0-9_-]{32,128}$"
    guard session.range(of: token, options: .regularExpression) != nil,
          csrf.range(of: token, options: .regularExpression) != nil else { throw DesktopPolicyError.invalidEvent }
    var common: [HTTPCookiePropertyKey: Any] = [
        .domain: "127.0.0.1", .path: "/", .expires: Date(timeIntervalSince1970: expires),
        HTTPCookiePropertyKey("SameSite"): "Lax",
    ]
    common[.name] = "wa_session"; common[.value] = session
    common[HTTPCookiePropertyKey("HttpOnly")] = "TRUE"
    guard let authentication = HTTPCookie(properties: common), authentication.isHTTPOnly else {
        throw DesktopPolicyError.invalidEvent
    }
    common[.name] = "wa_csrf"; common[.value] = csrf
    common.removeValue(forKey: HTTPCookiePropertyKey("HttpOnly"))
    guard let csrfCookie = HTTPCookie(properties: common) else { throw DesktopPolicyError.invalidEvent }
    guard unlink(file.path) == 0 else { throw DesktopPolicyError.invalidEvent }
    return [authentication, csrfCookie]
}
