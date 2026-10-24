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
    await Task.yield()
  }
}

/// Timers use injected absolute time. Cancellation does not erase callbacks: a
/// test may still deliver them to exercise work already queued by a scheduler.
@MainActor
private final class TestClock {
  private final class Entry {
    let id: Int
    let deadline: TimeInterval
    let fire: @MainActor () -> Void
    var cancelled = false
    var fired = false
    init(id: Int, deadline: TimeInterval, fire: @escaping @MainActor () -> Void) {
      self.id = id; self.deadline = deadline; self.fire = fire
    }
  }
  var now: TimeInterval = 100
  private var entries: [Int: Entry] = [:]
  private var nextID = 0

  func schedule(_ deadline: TimeInterval, _ fire: @escaping @MainActor () -> Void) -> @MainActor () -> Void {
    nextID += 1
    let id = nextID
    entries[id] = Entry(id: id, deadline: deadline, fire: fire)
    return { [weak self] in self?.entries[id]?.cancelled = true }
  }

  func advance(to target: TimeInterval) {
    while let entry = entries.values.filter({ !$0.fired && !$0.cancelled && $0.deadline <= target })
      .sorted(by: { ($0.deadline, $0.id) < ($1.deadline, $1.id) }).first {
      now = max(now, entry.deadline)
      entry.fired = true
      entry.fire()
    }
    now = target
  }

  func fireCancelledCallbacks() {
    for entry in entries.values.sorted(by: { $0.id < $1.id }) where entry.cancelled && !entry.fired {
      entry.fired = true
      entry.fire()
    }
  }

  func replayCallbacks() {
    for entry in entries.values.sorted(by: { $0.id < $1.id }) { entry.fire() }
  }
}

@MainActor
private final class SuspendedRecovery {
  private var continuation: CheckedContinuation<Void, Never>?
  private(set) var started = false
  private(set) var finished = false
  func run() async {
    started = true
    await withCheckedContinuation { continuation = $0 }
    finished = true
  }
  func release() {
    let pending = continuation
    continuation = nil
    pending?.resume()
  }
}

@MainActor
private final class Probe {
  let clock: TestClock
  let coordinator: AudioActivationCoordinator
  var events: [String] = []
  var endings: [Set<UUID>] = []
  var nativeGenerations: [UUID: UUID] = [:]
  var nativeEndings: [[UUID: UUID]] = []
  var recoveries = 0
  var recoveryCompletions = 0
  var recovery: (@MainActor () async -> Void)?
  var eventHook: (@MainActor (AudioActivationEvent) -> Void)?
  var endHook: (@MainActor (Set<UUID>) -> Void)?

  init(policy: AudioActivationPolicy = .standard) {
    let clock = TestClock()
    self.clock = clock
    coordinator = AudioActivationCoordinator(policy: policy, now: { clock.now }, schedule: clock.schedule)
    coordinator.onEvent = { [weak self] event, _ in
      guard let self else { return }
      switch event {
      case .activated: self.events.append("activated")
      case .deactivated: self.events.append("deactivated")
      case .missing: self.events.append("missing")
      case .failed: self.events.append("failed")
      }
      self.eventHook?(event)
    }
    coordinator.recover = { [weak self] in
      guard let self else { return }
      self.recoveries += 1
      if let recovery = self.recovery { await recovery() }
      self.recoveryCompletions += 1
    }
    coordinator.endConversations = { [weak self] targets in
      let ids = Set(targets.keys)
      self?.nativeEndings.append(targets)
      self?.endings.append(ids)
      self?.endHook?(ids)
    }
  }

  func updateDemand(_ ids: Set<UUID>) {
    for id in ids where nativeGenerations[id] == nil { nativeGenerations[id] = UUID() }
    coordinator.updateDemand(Dictionary(uniqueKeysWithValues: ids.map { ($0, nativeGenerations[$0]!) }))
  }

  func replaceNativeGeneration(_ id: UUID) {
    nativeGenerations[id] = UUID()
    updateDemand([id])
  }

  func shouldEndConversation(_ id: UUID) -> Bool {
    guard let generation = nativeGenerations[id] else { return false }
    return coordinator.shouldEndConversation(id, generation: generation)
  }
}

private let callA = UUID(uuidString: "00000000-0000-4000-8000-000000000001")!
private let callB = UUID(uuidString: "00000000-0000-4000-8000-000000000002")!
private let callC = UUID(uuidString: "00000000-0000-4000-8000-000000000003")!

@main
struct AudioTests {
  @MainActor static func main() async {
    do {
      var checks: [String] = []
      try policyValidation(); checks.append("finite-positive-policy")
      try await defaultDeadlines(); checks.append("default-activation-and-final-recovery-deadlines")
      try await customDeadlines(); checks.append("caller-configured-activation-and-recovery-intervals")
      try healthyActivation(); checks.append("healthy-activation-cancels-watchdog")
      try await heldAndResumed(); checks.append("hold-cancels-resume-starts-a-new-absence")
      try await demandChanges(); checks.append("multicall-demand-preserves-deadline-and-current-ids")
      try await suspendedRecovery(); checks.append("suspended-recovery-cannot-delay-clean-end")
      try await recoveredActivation(); checks.append("real-activation-invalidates-final-and-late-recovery")
      try await staleEpisode(); checks.append("stale-call-timers-cannot-end-a-new-call")
      try await sameUUIDReplacement(); checks.append("native-generation-replacement-invalidates-waiting-missing-and-failed-episodes")
      try await reset(); checks.append("reset-clears-demand-without-synthetic-audio-events")
      try await reentrantMissing(); checks.append("missing-observer-reentrancy-skips-stale-recovery")
      try await reentrantFailure(); checks.append("failed-observer-reentrancy-skips-stale-end")
      try await endReentrancy(); checks.append("end-callback-may-start-a-fresh-episode")
      try await replacingFailedDemand(); checks.append("replacement-demand-without-empty-gap-gets-new-watchdog")
      try await overlappingFailedDemand(); checks.append("fresh-demand-overlapping-failed-call-gets-scoped-watchdog")
      let bytes = try JSONSerialization.data(withJSONObject: ["success": true, "checks": checks], options: [.sortedKeys])
      print(String(decoding: bytes, as: UTF8.self))
    } catch {
      FileHandle.standardError.write(Data("Audio activation check failed: \(error)\n".utf8))
      exit(1)
    }
  }

  @MainActor private static func policyValidation() throws {
    for invalid in [0, -1, Double.nan, Double.infinity, -Double.infinity] {
      do {
        _ = try AudioActivationPolicy(activationTimeout: invalid, recoveryTimeout: 5)
        throw TestFailure.failed("Activation timeout must be finite and positive.")
      } catch is TestFailure { throw TestFailure.failed("Activation timeout must reject \(invalid).") }
      catch {}
      do {
        _ = try AudioActivationPolicy(activationTimeout: 10, recoveryTimeout: invalid)
        throw TestFailure.failed("Recovery timeout must be finite and positive.")
      } catch is TestFailure { throw TestFailure.failed("Recovery timeout must reject \(invalid).") }
      catch {}
    }
    _ = try AudioActivationPolicy(activationTimeout: 0.001, recoveryTimeout: 0.001)
  }

  @MainActor private static func defaultDeadlines() async throws {
    let probe = Probe()
    probe.updateDemand([callA])
    probe.clock.advance(to: 109.999)
    try expect(probe.events.isEmpty && probe.recoveries == 0 && probe.endings.isEmpty,
               "A joined call must receive the full default ten-second activation interval.")
    probe.clock.advance(to: 110)
    try expect(probe.events == ["missing"] && !probe.coordinator.isActive,
               "The first deadline must emit missing without fabricating activation.")
    try await eventually("Exactly one recovery must run after missing.") { probe.recoveryCompletions == 1 }
    probe.clock.advance(to: 114.999)
    try expect(probe.endings.isEmpty && probe.events == ["missing"], "Recovery gets its full five-second interval.")
    probe.clock.advance(to: 115)
    try expect(probe.events == ["missing", "failed"] && probe.endings == [[callA]] && probe.recoveries == 1,
               "Returning from recovery does not imply activation; the call must end once at the final deadline.")
    probe.clock.advance(to: 200)
    probe.clock.replayCallbacks()
    try expect(probe.endings.count == 1 && probe.recoveries == 1 && !probe.coordinator.isActive,
               "One absence episode must not repeat recovery or its final end.")
  }

  @MainActor private static func healthyActivation() throws {
    let probe = Probe()
    probe.updateDemand([callA])
    probe.clock.advance(to: 105)
    probe.coordinator.didActivate()
    try expect(probe.coordinator.isActive && probe.events == ["activated"], "Only a real callback marks audio active.")
    probe.clock.advance(to: 120)
    probe.clock.fireCancelledCallbacks()
    try expect(probe.events == ["activated"] && probe.recoveries == 0 && probe.endings.isEmpty,
               "Activation must invalidate a timer already queued for delivery.")
    probe.coordinator.didDeactivate()
    try expect(!probe.coordinator.isActive && probe.events == ["activated", "deactivated"], "Real deactivation must be observable.")
    probe.clock.advance(to: 129)
    probe.coordinator.didActivate()
    probe.clock.advance(to: 200)
    probe.clock.replayCallbacks()
    try expect(probe.events == ["activated", "deactivated", "activated"] && probe.endings.isEmpty,
               "Reactivation before the new deadline must keep the conversation alive.")
    let alreadyActive = Probe()
    alreadyActive.coordinator.didActivate()
    alreadyActive.updateDemand([callA, callB])
    alreadyActive.clock.advance(to: 200)
    try expect(alreadyActive.coordinator.isActive && alreadyActive.events == ["activated"] && alreadyActive.endings.isEmpty,
               "Conversations joining an already active session must not start an absence episode.")
    alreadyActive.coordinator.systemBecameInactive()
    try expect(!alreadyActive.coordinator.isActive && alreadyActive.events == ["activated"],
               "An AV interruption must revoke authorization without fabricating an LCK deactivation event.")
    alreadyActive.coordinator.didActivate()
    alreadyActive.clock.advance(to: 230)
    try expect(alreadyActive.coordinator.isActive && alreadyActive.events == ["activated", "activated"] && alreadyActive.endings.isEmpty,
               "Only a later genuine LCK activation restores interrupted audio and cancels its watchdog.")
  }

  @MainActor private static func customDeadlines() async throws {
    let probe = Probe(policy: try AudioActivationPolicy(activationTimeout: 2.5, recoveryTimeout: 1.25))
    probe.updateDemand([callA])
    probe.clock.advance(to: 102.499)
    try expect(probe.events.isEmpty, "A caller-configured activation interval must not expire early.")
    probe.clock.advance(to: 102.5)
    try await eventually("The configured activation deadline must replace the default.") { probe.recoveries == 1 }
    probe.clock.advance(to: 103.749)
    try expect(probe.endings.isEmpty, "The caller-configured recovery interval must not expire early.")
    probe.clock.advance(to: 103.75)
    try expect(probe.events == ["missing", "failed"] && probe.endings == [[callA]],
               "The configured recovery deadline must bound final termination independently.")
  }

  @MainActor private static func heldAndResumed() async throws {
    let probe = Probe()
    probe.updateDemand([])
    probe.clock.advance(to: 120)
    try expect(probe.events.isEmpty, "Ringing, held or ended calls without audio demand must not arm a watchdog.")
    probe.updateDemand([callA])
    probe.clock.advance(to: 125)
    probe.updateDemand([])
    probe.clock.advance(to: 130)
    probe.clock.fireCancelledCallbacks()
    try expect(probe.events.isEmpty && probe.endings.isEmpty, "Holding the last active call cancels the absence episode.")
    probe.updateDemand([callA])
    probe.clock.advance(to: 139.999)
    try expect(probe.events.isEmpty, "Resuming must receive a fresh activation interval.")
    probe.clock.advance(to: 140)
    try await eventually("Resumed demand must attempt recovery.") { probe.recoveries == 1 }
    probe.updateDemand([])
    probe.clock.advance(to: 150)
    probe.clock.replayCallbacks()
    try expect(probe.events == ["missing"] && probe.endings.isEmpty,
               "Holding or ending during recovery prevents stale failure and termination.")
  }

  @MainActor private static func demandChanges() async throws {
    let probe = Probe()
    probe.updateDemand([callA])
    probe.clock.advance(to: 109)
    probe.updateDemand([callA, callB])
    probe.clock.advance(to: 110)
    try await eventually("Adding a conversation must not postpone the first absence deadline.") { probe.recoveries == 1 }
    probe.clock.advance(to: 112)
    probe.updateDemand([callB])
    probe.clock.advance(to: 115)
    try expect(probe.events == ["missing", "failed"] && probe.endings == [[callB]],
               "The final deadline must end current unheld demand, excluding the call that already ended.")
  }

  @MainActor private static func suspendedRecovery() async throws {
    let probe = Probe(), recovery = SuspendedRecovery()
    probe.recovery = { await recovery.run() }
    defer { recovery.release() }
    probe.updateDemand([callA])
    probe.clock.advance(to: 110)
    try await eventually("Recovery must start and deliberately remain suspended.") { recovery.started }
    probe.clock.advance(to: 115)
    try expect(!recovery.finished && probe.events == ["missing", "failed"] && probe.endings == [[callA]],
               "An uncooperative adapter must not block the final deadline or clean end.")
    probe.clock.replayCallbacks()
    try expect(probe.recoveries == 1 && probe.endings.count == 1, "Suspended recovery must neither retry nor end twice.")
    recovery.release()
    try await eventually("The suspended fixture must release after the final deadline.") { recovery.finished }
    try expect(probe.events == ["missing", "failed"] && !probe.coordinator.isActive,
               "A late recovery return must not create an activation event.")
  }

  @MainActor private static func recoveredActivation() async throws {
    let probe = Probe(), recovery = SuspendedRecovery()
    probe.recovery = { await recovery.run() }
    defer { recovery.release() }
    probe.updateDemand([callA])
    probe.clock.advance(to: 110)
    try await eventually("Recovery must begin before the activation race.") { recovery.started }
    probe.clock.advance(to: 114.999)
    probe.coordinator.didActivate()
    probe.clock.advance(to: 115)
    probe.clock.fireCancelledCallbacks()
    recovery.release()
    try await eventually("The cancelled recovery fixture must finish.") { recovery.finished }
    probe.clock.replayCallbacks()
    try expect(probe.coordinator.isActive && probe.events == ["missing", "activated"] && probe.endings.isEmpty,
               "A genuine activation wins over queued final timeout and late adapter completion.")
  }

  @MainActor private static func staleEpisode() async throws {
    let probe = Probe()
    probe.updateDemand([callA])
    probe.clock.advance(to: 105)
    probe.updateDemand([])
    probe.clock.advance(to: 106)
    probe.updateDemand([callB])
    probe.clock.advance(to: 110)
    probe.clock.fireCancelledCallbacks()
    try expect(probe.events.isEmpty, "The ended call's queued timer must not report missing for a new call.")
    probe.clock.advance(to: 116)
    try await eventually("The new call must use its own first deadline.") { probe.recoveries == 1 }
    probe.clock.advance(to: 117)
    probe.updateDemand([])
    probe.clock.advance(to: 118)
    probe.updateDemand([callC])
    probe.clock.advance(to: 121)
    probe.clock.fireCancelledCallbacks()
    try expect(probe.endings.isEmpty && probe.events == ["missing"], "An old final timeout must not terminate a newer call.")
    probe.clock.advance(to: 128)
    try await eventually("The third call must get its own recovery attempt.") { probe.recoveries == 2 }
    probe.clock.advance(to: 133)
    try expect(probe.endings == [[callC]] && probe.events == ["missing", "missing", "failed"],
               "Only the third call's own absence episode may end it.")
  }

  @MainActor private static func sameUUIDReplacement() async throws {
    enum Phase { case waiting, missing, failed }
    for phase in [Phase.waiting, .missing, .failed] {
      let probe = Probe()
      probe.updateDemand([callA])
      let original = probe.nativeGenerations[callA]!
      var replaced = false
      probe.eventHook = { [weak probe] event in
        guard !replaced, (phase == .missing && event == .missing) || (phase == .failed && event == .failed) else { return }
        replaced = true
        probe?.replaceNativeGeneration(callA)
      }
      let replacementAt = phase == .waiting ? 105.0 : phase == .missing ? 110.0 : 115.0
      if phase == .waiting {
        probe.clock.advance(to: replacementAt)
        probe.replaceNativeGeneration(callA)
      } else {
        probe.clock.advance(to: 110)
        if phase == .failed {
          try await eventually("The original native generation must finish recovery before failure.") { probe.recoveryCompletions == 1 }
          probe.clock.advance(to: replacementAt)
        }
      }
      let replacement = probe.nativeGenerations[callA]!
      let priorEvents = phase == .waiting ? [] : phase == .missing ? ["missing"] : ["missing", "failed"]
      let priorRecoveries = phase == .failed ? 1 : 0
      try expect(replacement != original && !probe.shouldEndConversation(callA) && probe.endings.isEmpty,
                 "A replacement native object with the same UUID must not inherit the original failed marker or end request.")
      try expect(!probe.coordinator.shouldEndConversation(callA, generation: original),
                 "Replacing the native generation must revoke the original generation's failed-end authorization.")
      probe.clock.fireCancelledCallbacks()
      probe.clock.advance(to: replacementAt + 9.999)
      try expect(probe.events == priorEvents && probe.endings.isEmpty && probe.recoveries == priorRecoveries,
                 "Cancelled original timers must not recover or end a reused UUID before its fresh full activation interval.")
      probe.clock.advance(to: replacementAt + 10)
      try await eventually("The replacement native generation must receive its own recovery attempt.") {
        probe.recoveryCompletions == priorRecoveries + 1
      }
      probe.clock.advance(to: replacementAt + 14.999)
      try expect(probe.endings.isEmpty, "The replacement native generation must receive the full recovery interval.")
      probe.clock.advance(to: replacementAt + 15)
      probe.clock.replayCallbacks()
      try expect(probe.events == priorEvents + ["missing", "failed"] && probe.endings == [[callA]] &&
                 probe.nativeEndings == [[callA: replacement]] && probe.recoveries == priorRecoveries + 1,
                 "Only the replacement generation's own final deadline may end it once, even after old timers replay.")
      try expect(probe.shouldEndConversation(callA) && !probe.coordinator.shouldEndConversation(callA, generation: original),
                 "The replacement failure must authorize only its native generation, never the original one.")
    }
  }

  @MainActor private static func reset() async throws {
    let failed = Probe()
    failed.updateDemand([callA])
    failed.clock.advance(to: 110)
    try await eventually("Failure-current fixture must enter recovery.") { failed.recoveries == 1 }
    failed.clock.advance(to: 115)
    try expect(failed.shouldEndConversation(callA), "Only a current failed episode may end its demand.")
    failed.coordinator.reset()
    failed.updateDemand([callA])
    try expect(!failed.shouldEndConversation(callA),
               "A reset/new episode invalidates the old failure even when the conversation ID is reused.")
    let probe = Probe(), recovery = SuspendedRecovery()
    probe.recovery = { await recovery.run() }
    defer { recovery.release() }
    probe.updateDemand([callA])
    probe.clock.advance(to: 110)
    try await eventually("Reset fixture must have recovery in flight.") { recovery.started }
    probe.coordinator.reset()
    recovery.release()
    try await eventually("Reset fixture must release suspended application work.") { recovery.finished }
    probe.clock.advance(to: 200)
    probe.clock.replayCallbacks()
    try expect(probe.events == ["missing"] && probe.endings.isEmpty && !probe.coordinator.isActive,
               "Reset cancels demand and stale work without synthesizing activation or deactivation.")
    probe.coordinator.didActivate()
    probe.coordinator.reset()
    try expect(probe.events == ["missing", "activated"] && !probe.coordinator.isActive,
               "Reset of active audio must clear state without a synthetic deactivation callback.")
  }

  @MainActor private static func reentrantMissing() async throws {
    for activates in [true, false] {
      let probe = Probe()
      probe.eventHook = { [weak probe] event in
        guard case .missing = event else { return }
        if activates { probe?.coordinator.didActivate() }
        else { probe?.updateDemand([]) }
      }
      probe.updateDemand([callA])
      probe.clock.advance(to: 110)
      for _ in 0..<20 { await Task.yield() }
      probe.clock.advance(to: 130)
      probe.clock.replayCallbacks()
      try expect(probe.recoveries == 0 && probe.endings.isEmpty && probe.events == (activates ? ["missing", "activated"] : ["missing"]),
                 "An observer resolving missing audio must suppress obsolete recovery and failure.")
    }
  }

  @MainActor private static func reentrantFailure() async throws {
    for activates in [true, false] {
      let probe = Probe()
      probe.eventHook = { [weak probe] event in
        guard case .failed = event else { return }
        if activates { probe?.coordinator.didActivate() }
        else { probe?.updateDemand([]) }
      }
      probe.updateDemand([callA])
      probe.clock.advance(to: 110)
      try await eventually("Failure observer fixture must attempt recovery first.") { probe.recoveries == 1 }
      probe.clock.advance(to: 115)
      probe.clock.replayCallbacks()
      try expect(probe.endings.isEmpty && probe.events == (activates ? ["missing", "failed", "activated"] : ["missing", "failed"]),
                 "An observer resolving failure must prevent termination of now-valid or ended demand.")
    }
  }

  @MainActor private static func endReentrancy() async throws {
    let probe = Probe()
    probe.endHook = { [weak probe] _ in
      probe?.updateDemand([])
      probe?.updateDemand([callB])
    }
    probe.updateDemand([callA])
    probe.clock.advance(to: 110)
    try await eventually("End callback fixture must attempt recovery.") { probe.recoveries == 1 }
    probe.clock.advance(to: 115)
    try expect(probe.endings == [[callA]], "The failed call must end before the callback starts new demand.")
    probe.clock.advance(to: 124.999)
    try expect(probe.events == ["missing", "failed"] && probe.endings.count == 1,
               "A reentrant new call must get a full fresh activation interval.")
    probe.clock.advance(to: 125)
    try await eventually("Reentrant demand must retain its independently armed watchdog.") { probe.recoveries == 2 }
    probe.coordinator.didActivate()
    probe.clock.advance(to: 140)
    probe.clock.replayCallbacks()
    try expect(probe.endings == [[callA]] && probe.events == ["missing", "failed", "missing", "activated"],
               "Old completion must not discard the new episode or end a newly activated call.")
  }

  @MainActor private static func replacingFailedDemand() async throws {
    let probe = Probe()
    probe.eventHook = { [weak probe] event in
      guard case .failed = event, probe?.events.filter({ $0 == "failed" }).count == 1 else { return }
      probe?.updateDemand([callB])
    }
    probe.updateDemand([callA])
    probe.clock.advance(to: 110)
    try await eventually("Replacement fixture must start the original recovery.") { probe.recoveries == 1 }
    probe.clock.advance(to: 115)
    try expect(probe.endings.isEmpty, "The old failure must not terminate replacement demand.")
    probe.clock.advance(to: 125)
    try await eventually("New demand must receive a fresh watchdog without an empty intermediate snapshot.") { probe.recoveries == 2 }
    probe.clock.advance(to: 130)
    try expect(probe.endings == [[callB]], "Replacement demand must end once after its own recovery interval.")
  }

  @MainActor private static func overlappingFailedDemand() async throws {
    for reentrant in [false, true] {
      try await overlappingFailedDemand(reentrant: reentrant)
    }
  }

  @MainActor private static func overlappingFailedDemand(reentrant: Bool) async throws {
    let probe = Probe()
    if reentrant {
      probe.eventHook = { [weak probe] event in
        guard case .failed = event, probe?.events.filter({ $0 == "failed" }).count == 1 else { return }
        probe?.updateDemand([callA, callB])
      }
    }
    probe.updateDemand([callA])
    probe.clock.advance(to: 110)
    try await eventually("Overlap fixture must start the first recovery.") { probe.recoveries == 1 }
    probe.clock.advance(to: 115)
    try expect(probe.endings == [[callA]], "The first failed call must receive its own end request.")

    // LCK may keep the failed call joined while its end is still in flight.
    probe.updateDemand([callA, callB])
    try expect(probe.shouldEndConversation(callA) && !probe.shouldEndConversation(callB),
               "New demand must preserve only the old call's pending failed-end authorization.")
    probe.clock.advance(to: 124.999)
    try expect(probe.events == ["missing", "failed"] && probe.recoveries == 1,
               "Fresh overlapping demand must receive the full activation interval.")
    probe.clock.advance(to: 125)
    try await eventually("A fresh call needs a watchdog even while the failed call remains joined.") { probe.recoveries == 2 }
    probe.clock.advance(to: 130)
    probe.clock.replayCallbacks()
    try expect(probe.events == ["missing", "failed", "missing", "failed"] &&
               probe.endings == [[callA], [callB]] && probe.recoveries == 2,
               "The new absence must end only fresh demand, without repeating the old failed end.")
  }

}
