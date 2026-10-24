import Foundation

/// Whole-client regression for the A1 canonical intake / M2 watchdog seam.
/// The LCK and audio boundaries are the explicit host facades in this suite.
@main struct WatchdogIntakeTests {
  @MainActor static func main() async throws {
    let policy = try AudioActivationPolicy(activationTimeout: 0.03, recoveryTimeout: 0.03)
    let client = try ConversationClient.forApplication(configuration: .init(), audioPolicy: policy)
    client.finishLaunch()
    let manager = ConversationManager.latest!
    client.setConversationHandler(.init(join: { _, _ in }, start: { _, _ in }, end: { _, _ in }))
    let payload = IncomingCallPayload(eventId: "watchdog-intake-event", serverCallId: "watchdog-intake-server",
      caller: .init(id: "host-watchdog-caller"))
    let id = try await client.reportIncomingConversation(payload)
    var endedReasons: [ConversationEndedReason] = []
    var watchdogEvents: [ConversationEventContext] = []
    let eventObserver = client.observeEvents { event in
      if event.type == .audioActivationMissing || event.type == .audioActivationFailed { watchdogEvents.append(event) }
    }
    defer { client.removeEventObserver(eventObserver) }
    let observer = client.observeState { update in
      if let reason = update.conversations.first(where: { $0.id == id })?.ended?.reason {
        endedReasons.append(reason)
      }
    }
    defer { client.removeStateObserver(observer) }
    try await client.joinConversation(id)
    let deadline = ContinuousClock.now + .seconds(2)
    while !manager.reports.contains(where: { pair in
      if case .conversationEnded(_, .failed) = pair.1 { return pair.0 == id }
      return false
    }) {
      precondition(ContinuousClock.now < deadline, "Host watchdog did not report its existing failure")
      try await Task.sleep(for: .milliseconds(1))
    }
    // Let the actual accepted terminal object publish, then inspect the retained
    // canonical tombstone as well as observer-visible metadata.
    await Task.yield()
    let terminalReports = manager.reports.filter { pair in
      if case .conversationEnded = pair.1 { return pair.0 == id }
      return false
    }
    precondition(terminalReports.count == 1, "Canonical intake must not send a second watchdog end report")
    precondition(endedReasons.contains(.failed), "Hydration erased the watchdog's failed terminal reason")
    precondition(client.incomingIntake.projection(id)?.terminal?.ended.reason == .failed,
      "The canonical incoming tombstone must retain watchdog failure")
    precondition(client.incomingIntake.takePendingTermination(id,
      generation: client.incomingIntake.projection(id)!.generation) == nil,
      "The direct watchdog report must not leave another intake end pending")
    precondition(watchdogEvents == [.init(type: .audioActivationMissing, conversationID: id),
                                   .init(type: .audioActivationFailed, conversationID: id)],
      "Actual watchdog events must preserve the affected conversation ID exactly once")
    print("{\"success\":true,\"groups\":1,\"actualClientIntakeAndWatchdog\":true,\"nativeFrameworkMeasured\":false}")
  }
}
