enum ExpoMediaSelection { static let name = "none" }
@MainActor func selectedMediaAdapter() -> any MediaAdapter { NoMediaAdapter() }
