// swift-tools-version: 6.1
import PackageDescription

// Test-only consumer. Package.resolved is the prior genuine consumer's input
// pin, not evidence that this fixture has resolved, compiled or run.
let sdkRoot = "../.."
let package = Package(
  name: "LiveKitConsumer",
  platforms: [.iOS("17.4")],
  products: [.library(name: "LiveKitConsumer", targets: ["LiveKitConsumer"])],
  dependencies: [
    .package(name: "SDKUnderTest", path: sdkRoot),
    .package(url: "https://github.com/livekit/client-sdk-swift.git", exact: "2.18.0")
  ],
  targets: [.target(name: "LiveKitConsumer", dependencies: [
    .product(name: "VoIPSDK", package: "SDKUnderTest"),
    .product(name: "LiveKit", package: "client-sdk-swift")
  ])],
  swiftLanguageModes: [.v6]
)
