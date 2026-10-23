import Foundation

/// All mutable bridge state belongs to main, alongside the SDK's native owner.
/// A JavaScript reload removes its handler without discarding native conversations.
@MainActor
final class ConversationBridge {
  typealias Emit = @MainActor (String, [String: Any]) -> Void
  private struct Delivery {
    let generation: Int
    let signal: ConversationCancellationSignal
    let observer: UUID
    let continuation: CheckedContinuation<Void, any Error>
  }

  private let client: ConversationClient
  private let pushService: PushService
  private let emit: Emit
  private var stateObserver: UUID?
  private var eventObserver: UUID?
  private var tokenObserver: UUID?
  private var dialObserver: UUID?
  private var dialGeneration = -1
  private var audioRoutesObserver: UUID?
  private var deliveries: [UUID: Delivery] = [:]
  private var generation = -1
  private var destroyed = false

  init(application: VoIPApplication, emit: @escaping Emit) {
    client = application.client
    pushService = application.pushService
    self.emit = emit
    stateObserver = client.observeState { [weak self] update in
      self?.emitValue("conversationsChanged", update)
    }
    eventObserver = client.observeEvents { [weak self] event in
      var payload: [String: String] = [:]
      if let id = event.conversationID { payload["conversationId"] = id.uuidString }
      self?.emit("conversationEvent", ["type": event.type.rawValue, "payload": payload])
    }
    tokenObserver = pushService.observeToken { [weak self] token in
      self?.emitValue("pushTokenUpdated", token)
    }
    audioRoutesObserver = client.observeAudioRoutes { [weak self] routes in
      self?.emitValue("audioRoutesChanged", routes)
    }
  }

  func attach(_ attached: Bool, generation: Int) {
    guard accepts(generation: generation) else { return }
    self.generation = generation
    cancelDeliveries(reason: "replaced")
    guard attached else { client.setConversationHandler(nil); return }
    func action(_ kind: ConversationActionKind) -> ConversationHandler.Action {
      { [weak self] conversation, context in
        guard let self else { throw CancellationError() }
        try await self.deliver(kind, conversation: conversation, context: context, generation: generation)
      }
    }
    client.setConversationHandler(.init(join: action(.join), start: action(.start), end: action(.end),
      hold: action(.hold), mute: action(.mute), tones: action(.tones)))
  }

  func accepts(generation: Int) -> Bool { !destroyed && generation >= 0 && generation > self.generation }

  func listenForDialRequests(attached: Bool, generation: Int) {
    guard !destroyed, generation >= 0, generation > dialGeneration else { return }
    dialGeneration = generation
    stopListeningForDialRequests()
    guard attached else { return }
    // Store identity before synchronous cold replay: teardown from an emitter
    // callback can then remove this exact consumer without detaching a newer one.
    let observer = UUID()
    dialObserver = observer
    DialRequestRouter.shared.observe(id: observer) { [weak self] request in
      guard let self, !self.destroyed else { return }
      self.emit("conversationEvent", ["type": "dialRequest",
        "payload": ["handle": request.handle, "video": request.video]])
    }
  }

  func stopListeningForDialRequests() {
    if let dialObserver { DialRequestRouter.shared.removeObserver(dialObserver) }
    dialObserver = nil
  }

  func settle(_ id: UUID, generation: Int, success: Bool, rejection: HandlerRejection = .init(nil)) {
    guard !destroyed, self.generation == generation,
          let delivery = deliveries[id], delivery.generation == generation else { return }
    deliveries.removeValue(forKey: id)
    delivery.signal.removeObserver(delivery.observer)
    if success { delivery.continuation.resume() }
    else { delivery.continuation.resume(throwing: rejection) }
  }

  func destroy(detachHandler: Bool) {
    guard !destroyed else { return }
    destroyed = true
    stopListeningForDialRequests()
    cancelDeliveries(reason: "cancelled")
    if detachHandler { client.setConversationHandler(nil) }
    if let stateObserver { client.removeStateObserver(stateObserver) }
    if let eventObserver { client.removeEventObserver(eventObserver) }
    if let tokenObserver { pushService.removeTokenObserver(tokenObserver) }
    if let audioRoutesObserver { client.removeAudioRoutesObserver(audioRoutesObserver) }
    stateObserver = nil; eventObserver = nil; tokenObserver = nil; audioRoutesObserver = nil
  }

  private func deliver(_ kind: ConversationActionKind, conversation: ConversationSnapshot,
                       context: ConversationActionContext, generation: Int) async throws {
    guard !destroyed, self.generation == generation else { throw CancellationError() }
    try context.signal.throwIfCancelled()
    let snapshot = try BridgeJSON.dictionary(conversation)
    try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, any Error>) in
      // Install cancellation before publishing the action. A settled/cancelled signal
      // can call its observer synchronously, so do not insert a stale continuation.
      var cancellation: ConversationCancellationSignal.Reason?
      let observer = context.signal.observe { [weak self] reason in
        cancellation = reason
        self?.cancel(context.actionID, generation: generation, reason: reason.rawValue)
      }
      if cancellation != nil {
        context.signal.removeObserver(observer)
        continuation.resume(throwing: CancellationError())
        return
      }
      deliveries[context.actionID] = Delivery(generation: generation, signal: context.signal,
        observer: observer, continuation: continuation)
      var body: [String: Any] = ["actionID": context.actionID.uuidString, "generation": generation,
        "kind": kind.rawValue, "conversation": snapshot,
        "deadline": BridgeJSON.date(context.deadline), "origin": context.origin.rawValue]
      if let held = context.held { body["held"] = held }
      if let muted = context.muted { body["muted"] = muted }
      if let digits = context.digits { body["digits"] = digits }
      emit("handlerAction", body)
    }
  }

  private func cancel(_ id: UUID, generation: Int, reason: String) {
    guard let delivery = deliveries[id], delivery.generation == generation else { return }
    deliveries.removeValue(forKey: id)
    delivery.signal.removeObserver(delivery.observer)
    emit("actionCancelled", ["actionID": id.uuidString, "generation": generation, "reason": reason])
    delivery.continuation.resume(throwing: CancellationError())
  }

  private func cancelDeliveries(reason: String) {
    for (id, delivery) in Array(deliveries) { cancel(id, generation: delivery.generation, reason: reason) }
  }

  private func emitValue<Value: Encodable>(_ event: String, _ value: Value) {
    do { emit(event, try BridgeJSON.dictionary(value)) }
    catch { NSLog("[VoIPSDK] Could not encode a native conversation event.") }
  }
}

enum BridgeError: Error { case invalidArguments, unavailable }

/// Preserve the JavaScript handler's failure at the native continuation boundary.
/// Its contents are not logged; application errors can include private data.
struct HandlerRejection: Error, Equatable, Sendable {
  let name: String
  let message: String

  init(_ reason: [String: String]?) {
    name = reason?["name"] ?? "Error"
    message = reason?["message"] ?? "The conversation handler rejected the action."
  }
}

enum BridgeJSON {
  static func date(_ value: Date) -> String {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.string(from: value)
  }

  static func dictionary<Value: Encodable>(_ value: Value) throws -> [String: Any] {
    let encoder = JSONEncoder()
    encoder.dateEncodingStrategy = .custom { value, encoder in
      var container = encoder.singleValueContainer()
      try container.encode(date(value))
    }
    let data = try encoder.encode(value)
    guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
      throw BridgeError.invalidArguments
    }
    return object
  }

  static func data(_ object: [String: Any]) throws -> Data {
    guard JSONSerialization.isValidJSONObject(object) else { throw BridgeError.invalidArguments }
    return try JSONSerialization.data(withJSONObject: object)
  }

  static func decode<Value: Decodable>(_ type: Value.Type, from data: Data) throws -> Value {
    do { return try JSONDecoder().decode(type, from: data) }
    catch { throw BridgeError.invalidArguments }
  }
}

/// Different module lifetimes may briefly overlap during a reload. Only the owner
/// that installed the current handler may detach it; old completions remain scoped.
@MainActor
enum ConversationBridges {
  private static var bridges: [UUID: ConversationBridge] = [:]
  private static var handlerOwner: UUID?
  private static var latestDialBridge: UUID?

  static func install(_ id: UUID, emit: @escaping ConversationBridge.Emit) throws {
    guard bridges[id] == nil else { return }
    bridges[id] = ConversationBridge(application: try ExpoMediaSupport.startFromBundle(), emit: emit)
    if let previous = latestDialBridge { bridges[previous]?.stopListeningForDialRequests() }
    latestDialBridge = id
  }
  static func listenForDialRequests(_ id: UUID, attached: Bool, generation: Int) {
    guard latestDialBridge == id else { return }
    bridges[id]?.listenForDialRequests(attached: attached, generation: generation)
  }
  static func attach(_ id: UUID, attached: Bool, generation: Int) {
    guard let bridge = bridges[id], bridge.accepts(generation: generation) else { return }
    if attached {
      if let previous = handlerOwner, previous != id { bridges[previous]?.attach(false, generation: Int.max) }
      handlerOwner = id
    }
    guard attached || handlerOwner == id else { return }
    bridge.attach(attached, generation: generation)
    if !attached { handlerOwner = nil }
  }
  static func settle(_ owner: UUID, id: UUID, generation: Int, success: Bool,
                     rejection: HandlerRejection = .init(nil)) {
    guard handlerOwner == owner else { return }
    bridges[owner]?.settle(id, generation: generation, success: success, rejection: rejection)
  }
  static func remove(_ id: UUID) {
    bridges.removeValue(forKey: id)?.destroy(detachHandler: handlerOwner == id)
    if handlerOwner == id { handlerOwner = nil }
    if latestDialBridge == id { latestDialBridge = nil }
  }
}
