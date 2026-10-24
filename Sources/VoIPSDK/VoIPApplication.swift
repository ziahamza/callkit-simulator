#if canImport(LiveCommunicationKit) && (os(iOS) || os(watchOS))
import Foundation
import CoreFoundation
#if os(iOS)
import UIKit
#endif

/// Start in application launch, before a UI scene or JavaScript is required.
@MainActor
public final class VoIPApplication {
  public struct Configuration: Sendable, Equatable {
    public var client: ConversationClient.Configuration
    public var simulatorSupport: Bool
    public init(client: ConversationClient.Configuration = .init(), simulatorSupport: Bool = true) {
      self.client = client; self.simulatorSupport = simulatorSupport
    }

    /// Reads the config plugin's VoIPSDK Info.plist dictionary.
    public static func fromBundle(_ bundle: Bundle = .main) throws -> Configuration {
      let info: [String: Any]
      if let value = bundle.object(forInfoDictionaryKey: "VoIPSDK") {
        guard let dictionary = value as? [String: Any] else { throw ConversationClientError.invalidConfiguration }
        info = dictionary
      } else { info = [:] }
      func bool(_ key: String, default fallback: Bool) throws -> Bool {
        guard let value = info[key] else { return fallback }
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else {
          throw ConversationClientError.invalidConfiguration
        }
        return number.boolValue
      }
      func integer(_ key: String, default fallback: Int) throws -> Int {
        guard let value = info[key] else { return fallback }
        guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue.isFinite, number.doubleValue >= 1,
              number.doubleValue < Double(Int.max), number.doubleValue.rounded() == number.doubleValue else {
          throw ConversationClientError.invalidConfiguration
        }
        return number.intValue
      }
      func string(_ key: String) throws -> String? {
        guard let value = info[key] else { return nil }
        guard let string = value as? String else { throw ConversationClientError.invalidConfiguration }
        return string
      }
      let policy: UnhandledActionPolicy
      if let value = try string("unhandledAction") {
        guard let parsed = UnhandledActionPolicy(rawValue: value) else { throw ConversationClientError.invalidConfiguration }
        policy = parsed
      } else { policy = .fail }
      let ringtone = try string("ringtoneName")
      var iconData: Data?
      if let name = try string("iconTemplateImageName"), !name.isEmpty {
        #if os(iOS)
        iconData = UIImage(named: name, in: bundle, compatibleWith: nil)?.pngData()
        #else
        if let url = bundle.url(forResource: name, withExtension: nil) { iconData = try? Data(contentsOf: url) }
        #endif
        if iconData == nil { NSLog("VoIPSDK: configured icon could not be loaded; using the system icon.") }
      }
      return try Configuration(client: .init(
        ringtoneName: ringtone == "default" || ringtone == "" ? nil : ringtone,
        iconTemplateImageData: iconData,
        maximumConversationGroups: integer("maximumConversationGroups", default: 2),
        maximumConversationsPerGroup: integer("maximumConversationsPerConversationGroup", default: 2),
        includesConversationInRecents: bool("includesCallsInRecents", default: true),
        supportsVideo: bool("supportsVideo", default: true), unhandledAction: policy,
        providerOwnershipMonitoring: bool("providerOwnershipMonitoring", default: ConversationClient.Configuration.defaultProviderOwnershipMonitoring)),
        simulatorSupport: bool("simulatorSupport", default: true))
    }
  }

  public private(set) static var current: VoIPApplication?
  private static var starting = false
  public let client: ConversationClient
  public let pushService: PushService
  private let configuration: Configuration
  private init(client: ConversationClient, configuration: Configuration) {
    self.client = client; self.configuration = configuration
    pushService = PushService(client: client)
  }

  /// Idempotent for the same launch configuration. A failed registration can retry
  /// without creating another manager or receiver.
  @discardableResult
  public static func start(configuration: Configuration = .init(), mediaAdapter: (any MediaAdapter)? = nil,
                           audioPolicy: AudioActivationPolicy = .standard) throws -> VoIPApplication {
    try ProcessSDKOwnership.claim()
    if let current {
      guard current.configuration == configuration else { throw ConversationClientError.alreadyConfigured }
      _ = try ConversationClient.forApplication(configuration: configuration.client,
        mediaAdapter: mediaAdapter, audioPolicy: audioPolicy)
      current.client.finishLaunch()
      try current.pushService.start()
      return current
    }
    guard !starting else { throw ConversationClientError.alreadyConfigured }
    starting = true
    defer { starting = false }
    try SimulatorSupport.configure(enabled: configuration.simulatorSupport)
    let client = try ConversationClient.forApplication(configuration: configuration.client,
      mediaAdapter: mediaAdapter, audioPolicy: audioPolicy)
    let application = VoIPApplication(client: client, configuration: configuration)
    current = application
    client.finishLaunch()
    try application.pushService.start()
    return application
  }

  @discardableResult
  public static func startFromBundle(_ bundle: Bundle = .main, mediaAdapter: (any MediaAdapter)? = nil,
                                     audioPolicy: AudioActivationPolicy = .standard) throws -> VoIPApplication {
    try start(configuration: Configuration.fromBundle(bundle), mediaAdapter: mediaAdapter, audioPolicy: audioPolicy)
  }
}
#endif
