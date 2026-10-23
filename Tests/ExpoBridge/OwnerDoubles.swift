import Foundation

// Only the platform-owning facades are replaced. Models, handler closures,
// cancellation signals, token encoding and the relay itself are production code.
@MainActor
final class ConversationClient {
  private(set) var handler: ConversationHandler?
  private var states: [UUID: @MainActor (ConversationStateUpdate) -> Void] = [:]
  private var events: [UUID: @MainActor (ConversationEventContext) -> Void] = [:]
  private var routes: [UUID: @MainActor (AudioRoutesSnapshot) -> Void] = [:]
  var state = ConversationStateUpdate(revision: 0, conversations: [])
  var audioRoutes = AudioRoutesSnapshot(revision: 0, routes: [], current: nil)
  var stateObserverCount: Int { states.count }
  var eventObserverCount: Int { events.count }
  var audioRoutesObserverCount: Int { routes.count }

  func setConversationHandler(_ handler: ConversationHandler?) { self.handler = handler }
  func observeState(_ callback: @escaping @MainActor (ConversationStateUpdate) -> Void) -> UUID {
    let id = UUID(); states[id] = callback; callback(state); return id
  }
  func removeStateObserver(_ id: UUID) { states.removeValue(forKey: id) }
  func observeEvents(_ callback: @escaping @MainActor (ConversationEventContext) -> Void) -> UUID {
    let id = UUID(); events[id] = callback; return id
  }
  func removeEventObserver(_ id: UUID) { events.removeValue(forKey: id) }
  func observeAudioRoutes(_ callback: @escaping @MainActor (AudioRoutesSnapshot) -> Void) -> UUID {
    let id = UUID(); routes[id] = callback; callback(audioRoutes); return id
  }
  func removeAudioRoutesObserver(_ id: UUID) { routes.removeValue(forKey: id) }
  func publish(_ update: AudioRoutesSnapshot) {
    audioRoutes = update
    for callback in Array(routes.values) { callback(update) }
  }
  func publish(_ update: ConversationStateUpdate) {
    state = update
    for callback in Array(states.values) { callback(update) }
  }
  func publish(_ event: ConversationEvent, conversationID: UUID? = nil) {
    let context = ConversationEventContext(type: event, conversationID: conversationID)
    for callback in Array(events.values) { callback(context) }
  }
}

@MainActor
final class PushService {
  private let tokens = PushTokenStore()
  private var observers: Set<UUID> = []
  var tokenState: PushTokenState { tokens.state }
  var observerCount: Int { observers.count }
  func observeToken(_ callback: @escaping @MainActor (PushTokenState) -> Void) -> UUID {
    let id = tokens.observe(callback); observers.insert(id); return id
  }
  func removeTokenObserver(_ id: UUID) { observers.remove(id); tokens.removeObserver(id) }
  func publish(_ data: Data?) { tokens.update(data) }
}

@MainActor
final class VoIPApplication {
  static var fixture: VoIPApplication?
  let client = ConversationClient()
  let pushService = PushService()
  static func startFromBundle() throws -> VoIPApplication {
    guard let fixture else { throw BridgeError.unavailable }
    return fixture
  }
}

@MainActor
enum ExpoMediaSupport {
  static func startFromBundle() throws -> VoIPApplication { try VoIPApplication.startFromBundle() }
}
