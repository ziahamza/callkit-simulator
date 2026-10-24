import Foundation

/// Whole-client authority and reentrancy controls. LCK/AV objects are explicit
/// host facades; these checks never activate audio or measure device delivery.
@MainActor private final class RecordingAudioAdapter: MediaAdapter {
  var authorized = false
  var callbacks: [Bool] = []
  var onAuthorization: (@MainActor (Bool) -> Void)?
  func setSystemAudioAuthorization(_ active: Bool) {
    authorized = active
    let callback = onAuthorization; onAuthorization = nil
    callback?(active)
  }
  func prepare() async throws {}
  func didActivate(_ session: AVAudioSession) async throws { callbacks.append(true) }
  func didDeactivate(_ session: AVAudioSession) async throws { callbacks.append(false) }
}

@main struct AudioAuthorityTests {
  @MainActor static func wait(_ condition: () -> Bool,
                             message: String = "Host audio authority boundary timed out") async throws {
    let limit = ContinuousClock.now + .seconds(2)
    while !condition() {
      precondition(ContinuousClock.now < limit, message)
      try await Task.sleep(for: .milliseconds(1))
    }
  }

  @MainActor static func main() async throws {
    let adapter = RecordingAudioAdapter()
    let policy = try AudioActivationPolicy(activationTimeout: 0.05, recoveryTimeout: 0.05)
    let client = try ConversationClient.forApplication(configuration: .init(), mediaAdapter: adapter, audioPolicy: policy)
    client.finishLaunch()
    let manager = ConversationManager.latest!, system = SystemAudioSession.latest!
    let session = AVAudioSession()
    var events: [ConversationEventContext] = []
    var onEvent: (@MainActor (ConversationEventContext) -> Void)?
    let observer = client.observeEvents { event in events.append(event); onEvent?(event) }
    defer { client.removeEventObserver(observer) }
    func activate() { client.conversationManager(manager, didActivate: session) }
    func deactivate() { client.conversationManager(manager, didDeactivate: session) }

    system.deliver(.active); system.deliver(.resumptionRecommended(true))
    precondition(!adapter.authorized && events.isEmpty, "Ordinary active/recommendation cannot authorize media")

    onEvent = { event in
      if event.type == .audioActivated {
        onEvent = nil
        system.deliver(.inactive(source: 1, interruptionReason: nil))
      }
    }
    activate()
    try await wait { adapter.callbacks.count == 1 }
    precondition(!adapter.authorized && !system.authorized,
      "An activation observer's synchronous inactive event must not be overwritten")

    onEvent = { event in
      if event.type == .audioActivated { onEvent = nil; client.conversationManagerDidReset(manager) }
    }
    activate()
    try await wait { adapter.callbacks.count == 2 }
    precondition(!adapter.authorized && !system.authorized && events.last?.type == .providerReset,
      "An activation observer's reset must remain revoked")

    activate()
    try await wait { adapter.callbacks.count == 3 }
    onEvent = { event in if event.type == .audioDeactivated { onEvent = nil; activate() } }
    deactivate()
    try await wait { adapter.callbacks.count == 5 }
    precondition(adapter.authorized && system.authorized && Array(adapter.callbacks.suffix(2)) == [false, true],
      "A deactivation observer's new activation must preserve native operation order and authorization")

    deactivate()
    try await wait { adapter.callbacks.count == 6 }
    adapter.onAuthorization = { active in
      precondition(active)
      system.deliver(.inactive(source: nil, interruptionReason: nil))
    }
    activate()
    // This queued deactivation is an ordered completion barrier: a stale outer
    // activation would appear before it, rather than hiding behind test timing.
    deactivate()
    try await wait { adapter.callbacks.count >= 7 }
    precondition(adapter.callbacks.count == 7 && adapter.callbacks.last == false && !adapter.authorized,
      "Reentrant application authorization must suppress stale queued activation")

    activate()
    try await wait { adapter.callbacks.count == 8 }
    adapter.onAuthorization = { active in precondition(!active); activate() }
    deactivate()
    try await wait { adapter.callbacks.count >= 9 }
    precondition(adapter.callbacks.count == 9 && adapter.callbacks.last == true && adapter.authorized,
      "Reentrant application activation must suppress stale queued deactivation")
    system.deliver(.mediaServicesReset)
    system.deliver(.active); system.deliver(.resumptionRecommended(true))
    precondition(!adapter.authorized && !system.authorized, "Reset revocation survives advisory notifications")
    precondition(events.allSatisfy { $0.conversationID == nil }, "Process-wide events cannot invent a conversation ID")

    func failedReportCount(_ id: UUID) -> Int {
      manager.reports.filter { pair in
        if case .conversationEnded(_, .failed) = pair.1 { return pair.0 == id }
        return false
      }.count
    }
    func watchdogEvents(_ id: UUID) -> [ConversationEvent] {
      events.filter { $0.conversationID == id &&
        ($0.type == .audioActivationMissing || $0.type == .audioActivationFailed) }.map(\.type)
    }

    // Replacing an object in either watchdog event starts a new absence for that
    // object. The old event/timer must neither end it nor suppress its watchdog.
    for trigger in [ConversationEvent.audioActivationMissing, .audioActivationFailed] {
      client.conversationManagerDidReset(manager)
      client.setConversationHandler(.init(join: { _, _ in }, start: { _, _ in }, end: { _, _ in }))
      activate()
      let id = try await client.reportIncomingConversation(.init(eventId: "observer-" + trigger.rawValue,
        serverCallId: "observer-" + trigger.rawValue, caller: .init(id: "host-audio-caller")))
      try await client.joinConversation(id)
      let original = manager.conversations.first { $0.uuid == id }!
      var replacement: Conversation?
      var replacedAt: ContinuousClock.Instant?
      var replacementMissing = false
      onEvent = { event in
        guard event.conversationID == id else { return }
        if replacement == nil && event.type == trigger {
          let current = Conversation(id, state: .joined)
          replacement = current; replacedAt = .now
          manager.conversations = [current]
          client.conversationManager(manager, conversationChanged: current)
        } else if replacement != nil && event.type == .audioActivationMissing {
          precondition(ContinuousClock.now - replacedAt! >= .milliseconds(50),
            "A replacement must receive its full activation interval")
          precondition(replacement?.state == .joined && failedReportCount(id) == 0 &&
            client.incomingIntake.projection(id)?.terminal == nil,
            "The original episode must leave the replacement and its intake live")
          replacementMissing = true
        } else if replacement != nil && event.type == .audioActivationFailed {
          precondition(replacementMissing,
            "A replacement must reach its own missing deadline before failure")
          precondition(ContinuousClock.now - replacedAt! >= .milliseconds(100),
            "A replacement must receive its full activation and recovery intervals")
        }
      }
      deactivate()
      try await wait({ replacement?.state == .left },
        message: "An observer's same-UUID replacement must finish its own bounded watchdog")
      onEvent = nil
      let expected: [ConversationEvent] = trigger == .audioActivationMissing
        ? [.audioActivationMissing, .audioActivationMissing, .audioActivationFailed]
        : [.audioActivationMissing, .audioActivationFailed, .audioActivationMissing, .audioActivationFailed]
      precondition(watchdogEvents(id) == expected && failedReportCount(id) == 1 && original.state == .joined,
        "Only the replacement's own episode may report its end; the original object stays untouched")
      precondition(client.incomingIntake.projection(id)?.terminal?.ended.reason == .failed,
        "The replacement's actual failed end must reach the canonical intake")
    }

    // Pending application cancellation can synchronously restore real LCK
    // authority or replace the current object. Neither permits a stale failed
    // end report or terminal metadata from the watchdog's older decision.
    for suffix in ["activation", "replacement"] {
      client.conversationManagerDidReset(manager)
      var pending: CheckedContinuation<Void, any Error>?
      var reentered = false
      var callID: UUID?
      var replacement: Conversation?
      client.setConversationHandler(.init(join: { _, _ in }, start: { _, _ in }, end: { _, _ in },
        mute: { _, context in
          context.signal.observe { _ in
            reentered = true
            if suffix == "replacement" {
              let current = Conversation(callID!, state: .joined)
              replacement = current
              manager.conversations = [current]
              client.conversationManager(manager, conversationChanged: current)
            } else { activate() }
          }
          try await withCheckedThrowingContinuation { pending = $0 }
        }))
      let id = try await client.reportIncomingConversation(.init(eventId: "reentrant-" + suffix,
        serverCallId: "reentrant-" + suffix, caller: .init(id: "host-audio-caller")))
      callID = id
      // Keep modeled LCK authority through setup, so scheduler delay cannot
      // race the watchdog before its cancellation observer exists.
      activate()
      try await client.joinConversation(id)
      let original = manager.conversations.first { $0.uuid == id }!
      let mute = MuteConversationAction(conversationUUID: id, isMuted: true)
      manager.deliver(mute)
      try await wait { pending != nil }
      deactivate() // Only now begin the absence episode under test.
      try await wait { reentered }
      precondition(!manager.reports.contains { pair in
        if case .conversationEnded(_, .failed) = pair.1 { return pair.0 == id }
        return false
      }, "A newer authority/object must prevent the stale native failed-end report")
      precondition(client.snapshots.first(where: { $0.id == id })?.ended == nil &&
        client.incomingIntake.projection(id)?.terminal == nil,
        "Skipping a stale native end must also preserve live metadata and the canonical intake")
      pending?.resume(throwing: CancellationError()); pending = nil
      if suffix == "replacement" {
        try await wait({ replacement?.state == .left },
          message: "A cancellation replacement must not inherit the original failed marker")
        precondition(watchdogEvents(id) == [.audioActivationMissing, .audioActivationFailed,
          .audioActivationMissing, .audioActivationFailed] && failedReportCount(id) == 1 && original.state == .joined,
          "A cancellation replacement must get its own watchdog and exactly one end for its current object")
        precondition(client.incomingIntake.projection(id)?.terminal?.ended.reason == .failed,
          "The cancellation replacement's bounded failure must update canonical intake")
      } else { try await client.reportConversation(id, report: .ended(.remoteEnded)) }
    }
    print("{\"success\":true,\"groups\":10,\"actualClientAuthorityAndWatchdog\":true,\"nativeFrameworkMeasured\":false}")
  }
}
