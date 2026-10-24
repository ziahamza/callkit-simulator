import Foundation

private enum TestFailure: Error, CustomStringConvertible {
  case failed(String)
  var description: String { switch self { case .failed(let message): message } }
}

@MainActor
private func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
  if !condition() { throw TestFailure.failed(message) }
}

@MainActor
private func eventually(_ message: String, _ predicate: () -> Bool) async throws {
  let deadline = ContinuousClock.now.advanced(by: .seconds(2))
  while !predicate() {
    guard ContinuousClock.now < deadline else { throw TestFailure.failed(message) }
    try await Task.sleep(for: .milliseconds(1))
  }
}

@MainActor
private final class Events {
  var rows: [(String, [String: Any])] = []
  var intercept: ((String, [String: Any]) -> Void)?
  func emit(_ name: String, _ body: [String: Any]) {
    rows.append((name, body)); intercept?(name, body)
  }
  func named(_ name: String) -> [[String: Any]] { rows.filter { $0.0 == name }.map(\.1) }
}

@MainActor
private final class Invocation {
  enum Outcome { case accepted, cancelled, rejected, encodingFailed, unexpected }
  private(set) var outcome: Outcome?
  private(set) var rejection: HandlerRejection?
  private(set) var completions = 0
  init(_ action: @escaping ConversationHandler.Action, snapshot: ConversationSnapshot,
       context: ConversationActionContext) {
    Task { @MainActor in
      do { try await action(snapshot, context); outcome = .accepted }
      catch is CancellationError { outcome = .cancelled }
      catch let error as HandlerRejection { rejection = error; outcome = .rejected }
      catch is EncodingError { outcome = .encodingFailed }
      catch { outcome = .unexpected }
      completions += 1
    }
  }
}

@MainActor
private final class WeakBridge {
  weak var value: ConversationBridge?
  init(_ value: ConversationBridge?) { self.value = value }
}

@MainActor
private func snapshot(_ id: UUID = UUID()) -> ConversationSnapshot {
  ConversationSnapshot(id: id, direction: .incoming, status: .ringing,
    members: [.init(id: "caller", displayName: "Fixture")], capabilities: .init(),
    isMuted: false, isVideo: false, connectedAt: nil, ended: nil, payload: nil, pending: nil)
}

@MainActor
private func context(id: UUID = UUID(), signal: ConversationCancellationSignal = .init()) -> ConversationActionContext {
  ConversationActionContext(actionID: id, deadline: Date(timeIntervalSince1970: 1_700_000_000.123),
    origin: .system, signal: signal, held: true, muted: false, digits: "12#")
}

@MainActor
private func invoke(_ application: VoIPApplication, kind: ConversationActionKind = .join,
                    conversation: ConversationSnapshot = snapshot(),
                    context: ConversationActionContext) throws -> Invocation {
  guard let action = application.client.handler?.implementation(for: kind) else {
    throw TestFailure.failed("The installed native handler must provide \(kind).")
  }
  return Invocation(action, snapshot: conversation, context: context)
}

@main
struct BridgeTests {
  @MainActor static func main() async {
    do { try await run() }
    catch {
      FileHandle.standardError.write(Data("Expo relay check failed: \(error)\n".utf8))
      exit(1)
    }
  }

  @MainActor private static func run() async throws {
    var checks: [String] = []
    try jsonBoundary(); checks.append("production-model-json-and-millisecond-dates")
    try await settlement(); checks.append("all-handler-kinds-success-rejection-and-duplicate-settlement")
    try await cancellation(); checks.append("native-cancellation-and-pre-cancelled-signal")
    try await replacement(); checks.append("handler-removal-replacement-and-old-generation-settlement")
    try await reentrantCancellation(); checks.append("cancellation-during-action-publication")
    try await ownership(); checks.append("overlapping-lifetimes-reload-and-stale-owner")
    try await destruction(); checks.append("destroyed-bridge-observers-continuations-and-release")
    try await invalidDelivery(); checks.append("invalid-model-encoding-rejects-handler-delivery")
    try observation(); checks.append("production-state-event-and-null-token-encoding")
    let result = try JSONSerialization.data(withJSONObject: ["success": true, "checks": checks], options: [.sortedKeys])
    print(String(decoding: result, as: UTF8.self))
  }

  @MainActor private static func jsonBoundary() throws {
    let precise = Date(timeIntervalSince1970: 1_700_000_000.123)
    let expected = "2023-11-14T22:13:20.123Z"
    try expect(BridgeJSON.date(precise) == expected, "Deadline encoding must retain milliseconds and UTC.")
    var conversation = snapshot()
    conversation.connectedAt = precise
    conversation.ended = .init(at: precise, reason: .remoteEnded)
    let body = try BridgeJSON.dictionary(conversation)
    try expect(body["connectedAt"] as? String == expected, "Snapshot dates must use the same ISO wire format.")
    try expect((body["ended"] as? [String: Any])?["at"] as? String == expected, "Ended dates must retain milliseconds.")
    let wire: [String: Any] = ["id": "person", "phoneNumber": "+15555550142"]
    let participant = try BridgeJSON.decode(Participant.self, from: BridgeJSON.data(wire))
    try expect(participant.id == "person", "Valid JSON must decode into the production model.")
    for invalid: [String: Any] in [["value": Double.nan], ["value": Date()], ["value": NSObject()]] {
      do { _ = try BridgeJSON.data(invalid); throw TestFailure.failed("Invalid JSON objects must be rejected.") }
      catch BridgeError.invalidArguments {}
    }
    var scalarRejected = false
    do { _ = try BridgeJSON.dictionary("scalar") } catch { scalarRejected = true }
    try expect(scalarRejected, "A scalar is not a bridge dictionary.")
    do { _ = try BridgeJSON.decode(Participant.self, from: Data("{}".utf8)); throw TestFailure.failed("Invalid model arguments must reject.") }
    catch BridgeError.invalidArguments {}
  }

  @MainActor private static func settlement() async throws {
    let application = VoIPApplication(), events = Events()
    let bridge = ConversationBridge(application: application, emit: events.emit)
    defer { bridge.destroy(detachHandler: true) }
    bridge.attach(true, generation: 0)
    for kind: ConversationActionKind in [.join, .start, .end, .hold, .mute, .tones] {
      let actionContext = context()
      let conversation = snapshot()
      let invocation = try invoke(application, kind: kind, conversation: conversation, context: actionContext)
      try await eventually("The native handler must emit an action.") {
        events.named("handlerAction").contains { $0["actionID"] as? String == actionContext.actionID.uuidString }
      }
      let body = try action(events, id: actionContext.actionID)
      try expect(body["kind"] as? String == kind.rawValue && body["generation"] as? Int == 0,
                 "Action kind and handler generation must survive the relay.")
      try expect((body["conversation"] as? [String: Any])?["id"] as? String == conversation.id.uuidString,
                 "The delivered conversation must retain its native identity.")
      try expect(body["origin"] as? String == "system" && body["deadline"] as? String == "2023-11-14T22:13:20.123Z",
                 "Handler context must retain origin and precise deadline.")
      try expect(body["held"] as? Bool == true && body["muted"] as? Bool == false && body["digits"] as? String == "12#",
                 "Optional action inputs must survive the relay.")
      let rejection = HandlerRejection(["name": "CallDeclined", "message": "The callee declined this invitation."])
      bridge.settle(actionContext.actionID, generation: 0, success: kind != .end, rejection: rejection)
      try await eventually("Settled handler continuation must complete.") { invocation.outcome != nil }
      try expect(invocation.outcome == (kind == .end ? .rejected : .accepted), "JS rejection must fail the native action.")
      try expect(invocation.rejection == (kind == .end ? rejection : nil),
                 "Only a rejected continuation must retain the exact JavaScript name and message.")
      bridge.settle(actionContext.actionID, generation: 0, success: true)
      actionContext.signal.cancel(.deadline)
      try expect(invocation.completions == 1, "Late settlement or cancellation must never resume twice.")
    }
    try expect(events.named("handlerAction").count == 6 && events.named("actionCancelled").isEmpty,
               "Completed deliveries must remove their cancellation observers.")
  }

  @MainActor private static func cancellation() async throws {
    let application = VoIPApplication(), events = Events()
    let bridge = ConversationBridge(application: application, emit: events.emit)
    defer { bridge.destroy(detachHandler: true) }
    bridge.attach(true, generation: 1)
    let actionContext = context()
    let invocation = try invoke(application, context: actionContext)
    try await eventually("Cancellation fixture must be in flight.") { !events.named("handlerAction").isEmpty }
    actionContext.signal.cancel(.deadline)
    try await eventually("Native cancellation must release the handler continuation.") { invocation.outcome != nil }
    bridge.settle(actionContext.actionID, generation: 1, success: true)
    actionContext.signal.cancel(.reset)
    try expect(invocation.outcome == .cancelled && invocation.completions == 1, "Native cancellation must win once.")
    let cancelled = events.named("actionCancelled")
    try expect(cancelled.count == 1 && cancelled[0]["reason"] as? String == "deadline", "Cancellation must carry its native reason once.")
    let preCancelled = context()
    preCancelled.signal.cancel(.reset)
    let previousCount = events.named("handlerAction").count
    let early = try invoke(application, context: preCancelled)
    try await eventually("An already-cancelled signal must complete without publishing.") { early.outcome != nil }
    try expect(early.outcome == .cancelled && events.named("handlerAction").count == previousCount,
               "A pre-cancelled action must not reach JS.")
  }

  @MainActor private static func replacement() async throws {
    let application = VoIPApplication(), events = Events()
    let bridge = ConversationBridge(application: application, emit: events.emit)
    defer { bridge.destroy(detachHandler: true) }
    bridge.attach(true, generation: 1)
    let oldContext = context()
    let old = try invoke(application, context: oldContext)
    try await eventually("The original handler must be pending.") { events.named("handlerAction").count == 1 }
    bridge.attach(false, generation: 2)
    try await eventually("Removing the handler must release the old continuation.") { old.outcome != nil }
    try expect(old.outcome == .cancelled && application.client.handler == nil, "Removal must detach the handler.")
    bridge.attach(true, generation: 3)
    let newContext = context(id: oldContext.actionID)
    let fresh = try invoke(application, context: newContext)
    try await eventually("The retained action must reach the replacement generation.") { events.named("handlerAction").count == 2 }
    bridge.settle(newContext.actionID, generation: 1, success: false)
    try expect(fresh.outcome == nil, "A previous generation must not settle a replacement delivery.")
    bridge.attach(false, generation: 2)
    bridge.attach(true, generation: 3)
    try expect(application.client.handler != nil, "Stale or repeated attachment must not replace the active handler.")
    bridge.settle(newContext.actionID, generation: 3, success: true)
    try await eventually("Replacement handler must settle independently.") { fresh.outcome != nil }
    try expect(fresh.outcome == .accepted && old.completions == 1 && fresh.completions == 1,
               "Replacement must retain exactly one completion per native invocation.")
    let priorContext = context()
    let prior = try invoke(application, context: priorContext)
    try await eventually("Same-bridge replacement must begin with an active delivery.") { events.named("handlerAction").count == 3 }
    bridge.attach(true, generation: 4)
    try await eventually("Replacing an attached handler must cancel the previous delivery.") { prior.outcome != nil }
    let nextContext = context(id: priorContext.actionID)
    let next = try invoke(application, context: nextContext)
    try await eventually("The replacement handler must accept retained delivery.") { events.named("handlerAction").count == 4 }
    bridge.settle(nextContext.actionID, generation: 3, success: false)
    try expect(next.outcome == nil && prior.outcome == .cancelled, "Old success must not settle the new attached handler.")
    bridge.settle(nextContext.actionID, generation: 4, success: true)
    try await eventually("A newly attached handler must complete its own delivery.") { next.outcome != nil }
    try expect(next.outcome == .accepted && next.completions == 1, "Attached replacement must settle exactly once.")
  }

  @MainActor private static func reentrantCancellation() async throws {
    let application = VoIPApplication(), events = Events()
    let bridge = ConversationBridge(application: application, emit: events.emit)
    defer { bridge.destroy(detachHandler: true) }
    bridge.attach(true, generation: 0)
    let actionContext = context()
    events.intercept = { name, _ in if name == "handlerAction" { actionContext.signal.cancel(.cancelled) } }
    let invocation = try invoke(application, context: actionContext)
    try await eventually("Cancellation during emit must not leak the newly installed continuation.") { invocation.outcome != nil }
    bridge.settle(actionContext.actionID, generation: 0, success: true)
    try expect(invocation.outcome == .cancelled && invocation.completions == 1 && events.named("actionCancelled").count == 1,
               "Synchronous cancellation must beat subsequent JS success.")
  }

  @MainActor private static func ownership() async throws {
    let application = VoIPApplication(), first = Events(), second = Events()
    VoIPApplication.fixture = application
    let a = UUID(), b = UUID()
    defer { ConversationBridges.remove(a); ConversationBridges.remove(b); VoIPApplication.fixture = nil }
    try ConversationBridges.install(a, emit: first.emit)
    try ConversationBridges.install(a, emit: first.emit)
    try ConversationBridges.install(b, emit: second.emit)
    ConversationBridges.attach(a, attached: true, generation: 0)
    let originalContext = context()
    let original = try invoke(application, context: originalContext)
    try await eventually("Owner A must receive its native action.") { first.named("handlerAction").count == 1 }
    ConversationBridges.remove(b)
    try expect(application.client.handler != nil && original.outcome == nil, "Removing a nonowner must preserve A's handler.")
    try ConversationBridges.install(b, emit: second.emit)
    ConversationBridges.attach(b, attached: true, generation: 0)
    try await eventually("Reload ownership transfer must release the previous continuation.") { original.outcome != nil }
    try expect(original.outcome == .cancelled, "The previous module's delivery must be cancelled on reload.")
    let newContext = context(id: originalContext.actionID)
    let replacement = try invoke(application, context: newContext)
    try await eventually("Owner B must receive the retained action.") { second.named("handlerAction").count == 1 }
    ConversationBridges.attach(a, attached: true, generation: 1)
    ConversationBridges.settle(a, id: newContext.actionID, generation: 0, success: false)
    ConversationBridges.remove(a)
    try expect(replacement.outcome == nil && application.client.handler != nil,
               "A stale module must neither reclaim, settle nor detach B's handler.")
    try expect(application.client.stateObserverCount == 1 && application.client.eventObserverCount == 1 && application.pushService.observerCount == 1,
               "Retiring A must remove its subscriptions while retaining B's.")
    ConversationBridges.settle(b, id: newContext.actionID, generation: 0, success: true)
    try await eventually("The owning module must settle its continuation.") { replacement.outcome != nil }
    try expect(replacement.outcome == .accepted && replacement.completions == 1, "Current-owner success must complete once.")
    ConversationBridges.remove(b)
    ConversationBridges.remove(b)
    try expect(application.client.handler == nil && application.client.stateObserverCount == 0 && application.pushService.observerCount == 0,
               "The last module's removal must detach its handler and observers.")
  }

  @MainActor private static func destruction() async throws {
    let application = VoIPApplication(), events = Events()
    var bridge: ConversationBridge? = ConversationBridge(application: application, emit: events.emit)
    let released = WeakBridge(bridge)
    bridge?.attach(true, generation: 0)
    let actionContext = context()
    let invocation = try invoke(application, context: actionContext)
    try await eventually("Destroy fixture must have a pending continuation.") { events.named("handlerAction").count == 1 }
    bridge?.destroy(detachHandler: true)
    bridge?.destroy(detachHandler: true)
    bridge?.attach(true, generation: 1)
    bridge?.settle(actionContext.actionID, generation: 0, success: true)
    bridge = nil
    try await eventually("Destroy must release the pending continuation and the bridge.") { invocation.outcome != nil && released.value == nil }
    try expect(invocation.outcome == .cancelled && invocation.completions == 1, "Destroyed deliveries must cancel once.")
    let count = events.rows.count
    application.client.publish(.providerReset)
    application.client.publish(.init(revision: 2, conversations: [snapshot()]))
    application.pushService.publish(Data([0x12, 0x34]))
    actionContext.signal.cancel(.deadline)
    try expect(events.rows.count == count && application.client.handler == nil && application.client.stateObserverCount == 0 && application.client.eventObserverCount == 0 && application.pushService.observerCount == 0,
               "A destroyed bridge must not receive later callbacks or retain subscriptions.")
  }

  @MainActor private static func invalidDelivery() async throws {
    let application = VoIPApplication(), events = Events()
    let bridge = ConversationBridge(application: application, emit: events.emit)
    defer { bridge.destroy(detachHandler: true) }
    bridge.attach(true, generation: 0)
    var invalid = snapshot()
    invalid.payload = .init(eventId: "event", serverCallId: "server", caller: .init(id: "caller"), metadata: ["bad": .number(.nan)])
    let invocation = try invoke(application, conversation: invalid, context: context())
    try await eventually("Encoding failure must reject without installing a continuation.") { invocation.outcome != nil }
    try expect(invocation.outcome == .encodingFailed && events.named("handlerAction").isEmpty,
               "Invalid JSON metadata must not silently become an empty JS action.")
  }

  @MainActor private static func observation() throws {
    let application = VoIPApplication(), events = Events()
    let bridge = ConversationBridge(application: application, emit: events.emit)
    defer { bridge.destroy(detachHandler: true) }
    let initialToken = events.named("pushTokenUpdated")
    try expect(initialToken.count == 1 && initialToken[0]["token"] is NSNull,
               "Token hydration must encode an explicit null before registration.")
    application.pushService.publish(Data([0x00, 0xab, 0xff]))
    let token = events.named("pushTokenUpdated").last?["token"] as? [String: Any]
    try expect(token?["type"] as? String == "apns-voip" && token?["value"] as? String == "00abff",
               "The real token model must retain its public wire fields.")
    application.pushService.publish(nil)
    try expect(events.named("pushTokenUpdated").last?["token"] is NSNull, "Invalidation must emit explicit null.")
    let conversation = snapshot()
    application.client.publish(.init(revision: 7, conversations: [conversation]))
    let state = events.named("conversationsChanged").last
    try expect(state?["revision"] as? Int == 7 && (state?["conversations"] as? [[String: Any]])?.first?["id"] as? String == conversation.id.uuidString,
               "Observed native state must preserve revision and conversation identity.")
    application.client.publish(.audioActivated)
    let event = events.named("conversationEvent").last
    try expect(event?["type"] as? String == "audioActivated" && (event?["payload"] as? [String: Any])?.isEmpty == true,
               "Native events must use the public event envelope.")
    application.client.publish(.providerOwnershipLost)
    let ownership = events.named("conversationEvent").last
    try expect(ownership?["type"] as? String == "providerOwnershipLost" &&
               (ownership?["payload"] as? [String: Any])?.isEmpty == true,
               "Process ownership loss must not invent a conversation identity.")
    application.client.publish(.audioActivationMissing, conversationID: conversation.id)
    let scoped = events.named("conversationEvent").last
    try expect(scoped?["type"] as? String == "audioActivationMissing" &&
               (scoped?["payload"] as? [String: Any])?["conversationId"] as? String == conversation.id.uuidString,
               "A watchdog event must preserve its affected conversation identity.")
  }

  @MainActor private static func action(_ events: Events, id: UUID) throws -> [String: Any] {
    guard let body = events.named("handlerAction").first(where: { $0["actionID"] as? String == id.uuidString }) else {
      throw TestFailure.failed("Missing action \(id).")
    }
    return body
  }
}
