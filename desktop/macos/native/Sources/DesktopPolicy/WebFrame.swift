import Foundation
import WebKit

/// Read the Objective-C getter before bridging: WebKit cold navigations can
/// supply nil despite WKFrameInfo.request's imported nonnull annotation.
@MainActor public func webFrameRequestURL(_ frame: WKFrameInfo?) -> URL? {
    guard let request = frame?.perform(#selector(getter: WKFrameInfo.request))?
        .takeUnretainedValue() as? NSURLRequest else { return nil }
    return request.url
}

@MainActor public func navigationSourceRequestURL(_ action: WKNavigationAction) -> URL? {
    let frame = action.perform(#selector(getter: WKNavigationAction.sourceFrame))?
        .takeUnretainedValue() as? WKFrameInfo
    return webFrameRequestURL(frame)
}
