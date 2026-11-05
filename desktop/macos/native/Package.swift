// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "WaterAgentDesktop",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "WaterAgentDesktop", targets: ["WaterAgentDesktop"]),
        .executable(name: "PolicyChecks", targets: ["PolicyChecks"]),
    ],
    targets: [
        .target(name: "DesktopPolicy"),
        .executableTarget(name: "WaterAgentDesktop", dependencies: ["DesktopPolicy"]),
        .executableTarget(name: "PolicyChecks", dependencies: ["DesktopPolicy"]),
        .testTarget(name: "DesktopPolicyTests", dependencies: ["DesktopPolicy"]),
    ],
    swiftLanguageModes: [.v5]
)
