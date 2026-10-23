enum ExpoMediaSelection { static let name = "livekit" }
@MainActor func selectedMediaAdapter() -> any MediaAdapter { QueuedMediaAdapter() }
