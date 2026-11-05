import AppKit

let application = NSApplication.shared
let delegate = DesktopApp()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
