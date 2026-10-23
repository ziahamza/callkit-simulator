import Foundation

/// Expo launch adapter selection. Native Swift apps use the dependency-free
/// core's explicit application-start adapter instead of this Expo-only wrapper.
@MainActor
public enum ExpoMediaSupport {
  public enum ConfigurationError: Error, Sendable, Equatable {
    case invalidMediaAdapter
    case nativeSelectionMismatch(requested: String, compiled: String)
  }
  private static let adapter = selectedMediaAdapter()

  @discardableResult
  public static func startFromBundle(_ bundle: Bundle = .main) throws -> VoIPApplication {
    let settings = bundle.object(forInfoDictionaryKey: "VoIPSDK") as? [String: Any]
    if let raw = settings?["mediaAdapter"], !(raw is String) {
      throw ConfigurationError.invalidMediaAdapter
    }
    let requested = settings?["mediaAdapter"] as? String ?? "webrtc"
    guard requested == ExpoMediaSelection.name else {
      throw ConfigurationError.nativeSelectionMismatch(requested: requested, compiled: ExpoMediaSelection.name)
    }
    // The application owner validates repeated starts. Reuse exactly this native
    // adapter instance; a failed start cannot silently select another backend.
    return try VoIPApplication.startFromBundle(bundle, mediaAdapter: adapter)
  }
}
