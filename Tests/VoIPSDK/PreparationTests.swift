import Foundation

@main
struct PreparationTests {
  @MainActor static func main() async {
    let start = Date(timeIntervalSince1970: 100)
    let conversationID = UUID()
    func snapshot() -> ConversationSnapshot {
      .init(id: conversationID, direction: .incoming, status: .ringing,
        members: [], capabilities: .init(), isMuted: false, isVideo: false)
    }
    for scenario in ["ready-handler", "ready-policy", "hung-policy", "failure", "cancel", "replacement", "platform-settled"] {
      let clock = PreparationClock(start)
      let coordinator = ConversationActionCoordinator(policy: .accept, now: { clock.now }, schedule: clock.schedule)
      coordinator.snapshot = { _ in snapshot() }
      let preparation = PreparationGate()
      var preparedCalls = 0
      var handlers: [String] = []
      var settlements: [Bool] = []
      var changes = 0
      coordinator.didChange = { changes += 1 }
      let platformPending = PreparationFlag()
      coordinator.prepareAction = { _, action in
        preparedCalls += 1
        precondition(action.deadline == start.addingTimeInterval(8.5))
        try await preparation.run()
      }
      precondition(preparedCalls == 0, "Installing the preparation hook cannot touch audio at launch")
      if scenario != "ready-policy" && scenario != "hung-policy" {
        coordinator.setHandler(.init(join: { _, context in
          precondition(context.deadline == start.addingTimeInterval(8.5))
          handlers.append("first")
        }, start: { _, _ in }, end: { _, _ in }))
      }
      let action = PendingAction(id: UUID(), kind: .join, deadline: start.addingTimeInterval(10), origin: .system)
      coordinator.enqueue(conversationID: conversationID, action: action,
        isPending: { platformPending.value }, settle: { settlements.append($0) })
      await eventually("Preparation must enter before the action can be delivered") { preparation.entered }
      precondition(preparedCalls == 1 && handlers.isEmpty && settlements.isEmpty)
      precondition(coordinator.pending(for: conversationID)?.deadline == start.addingTimeInterval(8.5))
      switch scenario {
      case "hung-policy":
        clock.advance(start.addingTimeInterval(8.5))
        precondition(settlements == [false], "Accept policy cannot accept unfinished mandatory preparation")
      case "cancel": coordinator.cancelAll(reason: .reset)
      case "replacement":
        coordinator.setHandler(.init(join: { _, _ in handlers.append("replacement") }, start: { _, _ in }, end: { _, _ in }))
      case "platform-settled": platformPending.value = false
      default: break
      }
      let changesBeforeCompletion = changes
      preparation.finish(failing: scenario == "failure")
      await eventually("Preparation completion must publish or remove the pending action") {
        preparation.returned && (scenario == "ready-policy"
          ? changes > changesBeforeCompletion && coordinator.pending(for: conversationID) != nil
          : coordinator.pending(for: conversationID) == nil)
      }
      switch scenario {
      case "ready-policy":
        precondition(handlers.isEmpty && settlements.isEmpty)
        clock.advance(start.addingTimeInterval(8.5))
        precondition(settlements == [true], "Prepared no-handler accept keeps the effective deadline")
      case "hung-policy", "cancel", "failure":
        precondition(handlers.isEmpty && settlements == [false])
      case "replacement": precondition(handlers == ["replacement"] && settlements == [true])
      case "platform-settled": precondition(handlers.isEmpty && settlements.isEmpty)
      default: precondition(handlers == ["first"] && settlements == [true])
      }
      precondition(coordinator.pending(for: conversationID) == nil)
    }
    let optional = ConversationActionCoordinator(policy: .accept)
    var preparations = 0
    optional.prepareAction = { _, _ in preparations += 1 }
    optional.snapshot = { _ in snapshot() }
    optional.setHandler(.init(join: { _, _ in }, start: { _, _ in }, end: { _, _ in }))
    var held: [Bool] = []
    optional.enqueue(conversationID: conversationID,
      action: .init(id: UUID(), kind: .hold, deadline: Date().addingTimeInterval(10), origin: .system, held: true),
      isPending: { true }, settle: { held.append($0) })
    precondition(held == [true] && preparations == 0, "Only real start/join actions begin preparation")
    await fulfillmentControls()
    print("Call-time preparation controls passed: eight prelude and fourteen fulfillment groups; ordering, policy margin, physical timeout and uncooperative completion races")
  }

  @MainActor private static func fulfillmentControls() async {
    let start = Date(timeIntervalSince1970: 100)
    let effective = Date(timeIntervalSince1970: 108.5)
    let physical = Date(timeIntervalSince1970: 110)
    let id = UUID()
    func snapshot() -> ConversationSnapshot {
      .init(id: id, direction: .incoming, status: .ringing,
        members: [], capabilities: .init(), isMuted: false, isVideo: false)
    }
    for kind in [ConversationActionKind.start, .join] {
      let clock = PreparationClock(start)
      let coordinator = ConversationActionCoordinator(policy: .accept, now: { clock.now }, schedule: clock.schedule)
      coordinator.snapshot = { _ in snapshot() }
      let final = PreparationGate(cooperative: false)
      var order: [String] = []
      var settlements: [Bool] = []
      coordinator.prepareAction = { _, action in
        precondition(action.kind == kind && action.deadline == effective)
        order.append("prelude")
      }
      let handler: ConversationHandler.Action = { _, context in
        precondition(context.deadline == effective)
        order.append("handler")
      }
      coordinator.setHandler(.init(join: handler, start: handler, end: { _, _ in }))
      coordinator.prepareForFulfillment = { _, action in
        precondition(action.kind == kind && action.deadline == effective)
        order.append("fulfillment")
        try await final.run()
      }
      precondition(order.isEmpty, "Installing hooks cannot perform launch-time preparation")
      coordinator.enqueue(conversationID: id,
        action: .init(id: UUID(), kind: kind, deadline: physical, origin: .system),
        isPending: { true }, settle: { settlements.append($0) })
      let staleEffective = clock.jobs.values.first { $0.0 == effective }!.1
      await eventually("A successful start/join must prepare again before fulfillment") { final.entered }
      precondition(order == ["prelude", "handler", "fulfillment"] && settlements.isEmpty)
      clock.advance(effective)
      staleEffective()
      precondition(settlements.isEmpty && coordinator.pending(for: id) != nil,
        "A stale effective timer cannot settle a fulfillment already in flight")
      clock.advance(Date(timeIntervalSince1970: 109.5))
      final.finish(failing: false)
      await eventually("Ready final preparation must fulfill within the physical margin") { coordinator.pending(for: id) == nil }
      precondition(final.returned && settlements == [true] && order.count == 3)
    }

    for scenario in ["policy-ready", "policy-late", "policy-hung", "failure", "reset", "platform-settled", "replacement", "stale-effective"] {
      let clock = PreparationClock(start)
      let coordinator = ConversationActionCoordinator(policy: .accept, now: { clock.now }, schedule: clock.schedule)
      coordinator.snapshot = { _ in snapshot() }
      let final = PreparationGate(cooperative: false)
      let platform = PreparationFlag()
      var handlerCalls = 0
      var replacementCalls = 0
      var finalCalls = 0
      var settlements: [Bool] = []
      var actionSignal: ConversationCancellationSignal?
      let policyOnly = scenario.hasPrefix("policy-")
      var changes = 0
      coordinator.didChange = { changes += 1 }
      coordinator.prepareAction = { _, _ in }
      coordinator.prepareForFulfillment = { _, _ in
        finalCalls += 1
        try await final.run()
      }
      if !policyOnly {
        coordinator.setHandler(.init(join: { _, context in
          handlerCalls += 1; actionSignal = context.signal
        }, start: { _, _ in }, end: { _, _ in }))
      }
      coordinator.enqueue(conversationID: id,
        action: .init(id: UUID(), kind: .join, deadline: physical, origin: .system),
        isPending: { platform.value }, settle: { settlements.append($0) })
      let staleEffective = clock.jobs.values.first { $0.0 == effective }!.1
      if policyOnly {
        await eventually("The initial preparation must publish readiness before no-handler policy") { changes >= 2 }
        clock.advance(effective)
      }
      await eventually("Accepted actions must enter final preparation") { final.entered }
      precondition(finalCalls == 1 && handlerCalls == (policyOnly ? 0 : 1) && settlements.isEmpty)
      switch scenario {
      case "policy-hung":
        clock.advance(physical)
        precondition(settlements == [false] && coordinator.pending(for: id) == nil,
          "Unfinished final preparation must fail at the physical deadline")
      case "policy-late":
        // Even when the timer callback is delayed, the physical bound wins.
        clock.now = physical
        precondition(settlements.isEmpty && coordinator.pending(for: id) != nil)
      case "reset":
        coordinator.cancelAll(reason: .reset)
        precondition(settlements == [false] && coordinator.pending(for: id) == nil)
      case "platform-settled": platform.value = false
      case "replacement":
        coordinator.setHandler(.init(join: { _, _ in replacementCalls += 1 }, start: { _, _ in }, end: { _, _ in }))
      case "stale-effective":
        clock.advance(effective)
        staleEffective()
        precondition(settlements.isEmpty && coordinator.pending(for: id) != nil)
      default: break
      }
      if scenario == "policy-ready" { clock.advance(Date(timeIntervalSince1970: 109.5)) }
      final.finish(failing: scenario == "failure")
      await eventually("An uncooperative final hook must return without reviving stale work") {
        final.returned && coordinator.pending(for: id) == nil
      }
      switch scenario {
      case "policy-hung", "policy-late", "failure", "reset": precondition(settlements == [false])
      case "platform-settled": precondition(settlements.isEmpty)
      default: precondition(settlements == [true])
      }
      if scenario == "failure" { precondition(actionSignal?.reason == .cancelled, "Failed final preparation must notify application media cleanup") }
      precondition(finalCalls == 1 && replacementCalls == 0,
        "Replacing a handler or resuming cancelled preparation cannot replay an accepted action")
      let terminalSettlements = settlements
      staleEffective()
      clock.advance(Date(timeIntervalSince1970: 111))
      precondition(settlements == terminalSettlements && finalCalls == 1 && replacementCalls == 0 && coordinator.pending(for: id) == nil)
    }

    for scenario in ["rejected-handler", "fail-policy", "optional-end", "optional-hold"] {
      let clock = PreparationClock(start)
      let policy: UnhandledActionPolicy = scenario == "fail-policy" ? .fail : .accept
      let coordinator = ConversationActionCoordinator(policy: policy, now: { clock.now }, schedule: clock.schedule)
      coordinator.snapshot = { _ in snapshot() }
      var finalCalls = 0
      var settlements: [Bool] = []
      coordinator.prepareForFulfillment = { _, _ in finalCalls += 1 }
      if scenario != "fail-policy" {
        coordinator.setHandler(.init(join: { _, _ in throw PreparationError.failed },
          start: { _, _ in }, end: { _, _ in }))
      }
      let kind: ConversationActionKind = scenario == "optional-end" ? .end : scenario == "optional-hold" ? .hold : .join
      coordinator.enqueue(conversationID: id,
        action: .init(id: UUID(), kind: kind, deadline: physical, origin: .system),
        isPending: { true }, settle: { settlements.append($0) })
      if scenario == "fail-policy" { clock.advance(effective) }
      await eventually("Rejected and non-start/join actions must settle without final preparation") { coordinator.pending(for: id) == nil }
      precondition(finalCalls == 0 && settlements == [scenario.hasPrefix("optional-")])
    }
  }
}

@MainActor private final class PreparationGate {
  private(set) var entered = false
  private(set) var returned = false
  private var result: CheckedContinuation<Void, any Error>?
  private let cooperative: Bool
  init(cooperative: Bool = true) { self.cooperative = cooperative }
  func run() async throws {
    defer { returned = true }
    try await withCheckedThrowingContinuation { continuation in
      result = continuation; entered = true
    }
    if cooperative { try Task.checkCancellation() }
  }
  func finish(failing: Bool) {
    if failing { result?.resume(throwing: PreparationError.failed) } else { result?.resume() }
    result = nil
  }
}
private enum PreparationError: Error { case failed }
@MainActor private final class PreparationFlag { var value = true }
@MainActor private final class PreparationClock {
  var now: Date
  var jobs: [UUID: (Date, @MainActor () -> Void)] = [:]
  init(_ now: Date) { self.now = now }
  func schedule(_ deadline: Date, _ fire: @escaping @MainActor () -> Void) -> @MainActor () -> Void {
    let id = UUID(); jobs[id] = (deadline, fire)
    return { self.jobs.removeValue(forKey: id) }
  }
  func advance(_ date: Date) {
    now = date
    for (id, job) in jobs.filter({ $0.value.0 <= date }) { jobs.removeValue(forKey: id); job.1() }
  }
}

@MainActor private func eventually(_ message: String, _ condition: () -> Bool) async {
  let deadline = ContinuousClock.now.advanced(by: .seconds(2))
  while !condition() {
    precondition(ContinuousClock.now < deadline, message)
    await Task.yield()
  }
}
