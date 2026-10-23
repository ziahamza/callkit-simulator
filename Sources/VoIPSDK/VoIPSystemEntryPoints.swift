#if os(iOS)
import Intents
import UIKit

/// Forward system entry points here from custom application/scene delegates.
/// Expo's subscriber forwards the application-delegate URL and activity callbacks.
@MainActor
public enum VoIPSystemEntryPoints {
  @discardableResult
  public static func handle(url: URL) -> Bool {
    guard let request = DialRequest(telURL: url) else { return false }
    DialRequestRouter.shared.receive(request)
    return true
  }

  @discardableResult
  public static func handle(intent: INStartCallIntent) -> Bool {
    guard let request = DialRequest(intent: intent) else { return false }
    DialRequestRouter.shared.receive(request)
    return true
  }

  @discardableResult
  public static func handle(userActivity: NSUserActivity) -> Bool {
    guard let intent = userActivity.interaction?.intent as? INStartCallIntent else { return false }
    return handle(intent: intent)
  }

  /// Cold scene connection does not require a running JavaScript bridge.
  @discardableResult
  public static func handle(connectionOptions: UIScene.ConnectionOptions) -> Bool {
    var handled = false
    for context in connectionOptions.urlContexts {
      if handle(url: context.url) { handled = true }
    }
    for activity in connectionOptions.userActivities {
      if handle(userActivity: activity) { handled = true }
    }
    return handled
  }
}
#endif
