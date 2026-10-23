enum ExpoMediaSelection { static let name = "webrtc" }
@MainActor func selectedMediaAdapter() -> any MediaAdapter { QueuedMediaAdapter() }
