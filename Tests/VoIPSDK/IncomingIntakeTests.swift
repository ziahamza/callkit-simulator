import Foundation

enum TestError: Error, Equatable { case reportFailed, siblingFailed, duplicate }
enum RegressionFailure: Error {
  case resetIdentity, resetProjection, staleGeneration, errorNotReleased
  case foregroundJoinedRejected, foregroundOwnership
}

private final class DeinitCount: @unchecked Sendable {
  private let lock = NSLock()
  private var count = 0
  func increment() { lock.lock(); count += 1; lock.unlock() }
  var value: Int { lock.lock(); defer { lock.unlock() }; return count }
}

/// External errors can own arbitrary Sendable reference state. Deinitialization
/// must be allowed to query the real intake without entering its lock twice.
private final class ReentrantError: Error, @unchecked Sendable {
  private let onDeinit: @Sendable () -> Void
  init(_ onDeinit: @escaping @Sendable () -> Void) { self.onDeinit = onDeinit }
  deinit { onDeinit() }
}

@main
struct IncomingIntakeTests {
  static let now = Date(timeIntervalSince1970: 1_000)

  static func payload(_ event: String = "incoming", server: String = "server",
                      name: String = "First caller") -> IncomingCallPayload {
    IncomingCallPayload(eventId: event, serverCallId: server,
      caller: Participant(id: "caller", displayName: name),
      metadata: ["first": .string(name)])
  }

  static func attempt(_ reservation: IncomingIntake.Reservation) -> IncomingIntake.Attempt {
    guard case .report(let attempt) = reservation.disposition else {
      preconditionFailure("Expected an independent actual report attempt")
    }
    return attempt
  }

  static func fails(_ ticket: IncomingIntake.Ticket, with expected: TestError) async {
    do { _ = try await ticket.value; preconditionFailure("Expected a failed report") }
    catch { precondition(error as? TestError == expected) }
  }

  static func main() async throws {
    if let control = CommandLine.arguments.dropFirst().first {
      switch control {
      case "reset-retention": try await resetPreservesKnownIdentity()
      case "error-normalization": try reentrantErrorNormalization()
      case "error-reset": try reentrantErrorReset()
      case "reset-pending": try await resetRetainsUnsettledMetadata()
      case "foreground-ownership": try await foregroundJoinsCanonicalOutcome()
      case "reset-foreground-failure": try await resetParkedForegroundFailure()
      default: preconditionFailure("Unknown intake control")
      }
      print("Incoming intake focused control passed: \(control)")
      return
    }
    try await concurrentReservation()
    try await independentPushAndForegroundJoin()
    try await foregroundJoinsCanonicalOutcome()
    try await rollbackAndRetry()
    try await duplicateSettlementOrdering()
    try await observedMembershipWins()
    try await terminationAndTombstone()
    try await resetInvalidatesOldResults()
    try await expiredTombstoneAndLongActive()
    try validation()
    try await resetPreservesKnownIdentity()
    try reentrantErrorNormalization()
    try reentrantErrorReset()
    try await resetRetainsUnsettledMetadata()
    try await resetParkedForegroundFailure()
    print("Incoming intake: 15 host control groups passed (no framework/device claim)")
  }

  static func concurrentReservation() async throws {
    let intake = IncomingIntake()
    let reservations = try await withThrowingTaskGroup(of: IncomingIntake.Reservation.self) { group in
      for index in 0..<64 {
        group.addTask {
          try intake.reserve(payload("event-\(index)", name: "Caller \(index)"),
            source: .pushKit, now: now)
        }
      }
      var values: [IncomingIntake.Reservation] = []
      for try await value in group { values.append(value) }
      return values
    }
    let first = reservations[0]
    precondition(Set(reservations.map(\.id)).count == 1,
      "Concurrent push registrations must share the one gate's UUID")
    precondition(Set(reservations.map(\.generation)).count == 1)
    precondition(Set(reservations.map { attempt($0).attemptID }).count == 64,
      "Every required push receives its own report attempt")
    precondition(reservations.allSatisfy { $0.payload == first.payload },
      "Canonical first metadata is immutable across every same-call reservation")
    await withTaskGroup(of: Void.self) { group in
      for reservation in reservations {
        group.addTask { intake.complete(attempt(reservation), outcome: .reported, now: now) }
      }
    }
    for reservation in reservations {
      let result = try await attempt(reservation).ticket.value
      precondition(result == first.id)
    }
    precondition(!intake.hasPending(first.id))
  }

  static func independentPushAndForegroundJoin() async throws {
    let intake = IncomingIntake()
    let first = try intake.reserve(payload(), source: .foreground, now: now)
    let predecessor = attempt(first)
    let foreground = try intake.reserve(payload("second", name: "Changed caller"),
      source: .foreground, now: now)
    guard case .join(let joined) = foreground.disposition else {
      preconditionFailure("Foreground duplicates join the existing pending ticket")
    }
    precondition(joined === predecessor.ticket)
    let second = try intake.reserve(payload("third", name: "Changed again"), source: .pushKit, now: now)
    let successor = attempt(second)
    precondition(successor.attemptID != predecessor.attemptID && second.id == first.id,
      "A hung predecessor cannot prevent a required push from receiving a report attempt")
    precondition(second.payload == first.payload && foreground.payload == first.payload)
    intake.complete(successor, outcome: .reported, now: now)
    let successorResult = try await successor.ticket.value
    precondition(successorResult == first.id && intake.hasPending(first.id),
      "The second report settles independently while its predecessor remains hung")
    let joinedResult = try await joined.value
    precondition(joinedResult == first.id && intake.hasPending(first.id),
      "Foreground callers observe canonical ownership without waiting for the hung predecessor")
    let known = try intake.reserve(payload("known"), source: .foreground, now: now)
    guard case .reported = known.disposition else { throw RegressionFailure.foregroundOwnership }
    intake.complete(predecessor, outcome: .reported, now: now)
    precondition(!intake.hasPending(first.id))
  }

  static func rollbackAndRetry() async throws {
    let intake = IncomingIntake()
    let first = try intake.reserve(payload(), source: .pushKit, now: now)
    let second = try intake.reserve(payload("another"), source: .pushKit, now: now)
    intake.complete(attempt(first), outcome: .failed(TestError.reportFailed), now: now)
    precondition(intake.projection(first.id)?.payload == first.payload && intake.hasPending(first.id),
      "Earlier failure cannot erase the successor's metadata or invitation association")
    precondition(!intake.removeConversation(first.id, now: now),
      "Record cleanup cannot retire an in-flight report")
    intake.complete(attempt(second), outcome: .failed(TestError.siblingFailed), now: now)
    await fails(attempt(first).ticket, with: .reportFailed)
    await fails(attempt(second).ticket, with: .siblingFailed)
    precondition(intake.projection(first.id) == nil && !intake.hasPending(first.id))
    let retry = try intake.reserve(payload(), source: .foreground, now: now)
    precondition(retry.id != first.id && retry.generation != first.generation,
      "Only an entirely unreported failed reservation is discarded for a real retry")
    intake.complete(attempt(first), outcome: .reported, now: now)
    precondition(intake.projection(first.id) == nil && intake.hasPending(retry.id),
      "An old repeated settlement cannot affect its replacement")
    intake.complete(attempt(retry), outcome: .reported, now: now)
    _ = try await attempt(retry).ticket.value
  }

  static func foregroundJoinsCanonicalOutcome() async throws {
    for successFirst in [false, true] {
      let intake = IncomingIntake()
      let first = try intake.reserve(payload(), source: .foreground, now: now)
      let foreground = try intake.reserve(payload("foreground"), source: .foreground, now: now)
      guard case .join(let joined) = foreground.disposition else {
        preconditionFailure("A foreground duplicate must await the existing lifetime")
      }
      let second = try intake.reserve(payload("push"), source: .pushKit, now: now)
      if successFirst {
        intake.complete(attempt(second), outcome: .reported, now: now)
      }
      intake.complete(attempt(first), outcome: .failed(TestError.reportFailed), now: now)
      if !successFirst {
        intake.complete(attempt(second), outcome: .reported, now: now)
      }
      do {
        let result = try await joined.value
        precondition(result == first.id)
      } catch { throw RegressionFailure.foregroundJoinedRejected }
      let known = try intake.reserve(payload("known"), source: .foreground, now: now)
      guard case .reported = known.disposition else { throw RegressionFailure.foregroundOwnership }
    }
  }

  static func duplicateSettlementOrdering() async throws {
    let intake = IncomingIntake()
    let first = try intake.reserve(payload(), source: .pushKit, now: now)
    let duplicate = try intake.reserve(payload("duplicate"), source: .pushKit, now: now)
    intake.complete(attempt(duplicate), outcome: .alreadyReported(TestError.duplicate), now: now)
    precondition(intake.hasPending(first.id), "Duplicate ownership is not inferred from reservation alone")
    intake.complete(attempt(first), outcome: .reported, now: now)
    let result = try await attempt(duplicate).ticket.value
    precondition(result == first.id && !intake.hasPending(first.id),
      "Later real success normalizes the already-settled duplicate")
    let lateDuplicate = try intake.reserve(payload("late-duplicate"), source: .pushKit, now: now)
    intake.complete(attempt(lateDuplicate), outcome: .alreadyReported(TestError.duplicate), now: now)
    let lateResult = try await attempt(lateDuplicate).ticket.value
    precondition(lateResult == first.id)

    let failed = IncomingIntake()
    let primary = try failed.reserve(payload(), source: .pushKit, now: now)
    let other = try failed.reserve(payload("duplicate"), source: .pushKit, now: now)
    failed.complete(attempt(other), outcome: .alreadyReported(TestError.duplicate), now: now)
    failed.complete(attempt(primary), outcome: .failed(TestError.reportFailed), now: now)
    await fails(attempt(other).ticket, with: .duplicate)
    await fails(attempt(primary).ticket, with: .reportFailed)
    precondition(failed.projection(primary.id) == nil,
      "A duplicate error without actual ownership evidence is not converted to success")
  }

  static func observedMembershipWins() async throws {
    let intake = IncomingIntake()
    let first = try intake.reserve(payload(), source: .pushKit, now: now)
    let second = try intake.reserve(payload("duplicate"), source: .pushKit, now: now)
    intake.complete(attempt(second), outcome: .alreadyReported(TestError.duplicate), now: now)
    intake.observeMembership(first.id, now: now)
    let result = try await attempt(second).ticket.value
    precondition(result == first.id)
    intake.complete(attempt(first), outcome: .failed(TestError.reportFailed), now: now)
    let firstResult = try await attempt(first).ticket.value
    precondition(firstResult == first.id, "Observed ownership also settles the canonical foreground result")
    precondition(intake.projection(first.id)?.payload == first.payload && !intake.hasPending(first.id),
      "Actual observed membership prevents rollback even when report completion fails")
    let foreground = try intake.reserve(payload("foreground"), source: .foreground, now: now)
    guard case .reported = foreground.disposition else { preconditionFailure("Known lifetime remains coalesced") }
  }

  static func terminationAndTombstone() async throws {
    let intake = IncomingIntake()
    let first = try intake.reserve(payload(), source: .pushKit, now: now)
    let ended = CallEndedPayload(eventId: "ended", serverCallId: "server", reason: .remoteEnded)
    precondition(intake.terminate(ended, now: now.addingTimeInterval(1)))
    let terminal = intake.projection(first.id)?.terminal
    precondition(terminal?.ended.reason == .remoteEnded && terminal?.ended.at == now.addingTimeInterval(1))
    precondition(!intake.terminate(ended, now: now.addingTimeInterval(2)), "Termination event is deduplicated")
    intake.complete(attempt(first), outcome: .reported, now: now.addingTimeInterval(3))
    _ = try await attempt(first).ticket.value
    precondition(intake.projection(first.id)?.terminal == terminal,
      "Report settlement cannot resurrect an already terminated reservation")
    precondition(intake.takePendingTermination(first.id, generation: first.generation) == terminal)
    precondition(intake.takePendingTermination(first.id, generation: first.generation) == nil)
    precondition(intake.projection(first.id)?.terminal == terminal, "Claim preserves public terminal metadata")
    precondition(intake.removeConversation(first.id, now: now.addingTimeInterval(4)))
    let repeated = try intake.reserve(payload("repeat", name: "New metadata"),
      source: .pushKit, now: now.addingTimeInterval(5))
    precondition(repeated.id == first.id && repeated.payload == first.payload,
      "A tombstone re-report retains the canonical first payload and UUID")
    precondition(intake.takePendingTermination(first.id, generation: first.generation) == terminal,
      "A new actual report attempt re-arms its existing termination")
    intake.complete(attempt(repeated), outcome: .reported, now: now.addingTimeInterval(6))
    _ = try await attempt(repeated).ticket.value
    precondition(intake.terminate(first.id, generation: first.generation, reason: .failed,
      now: now.addingTimeInterval(7)))
    precondition(intake.projection(first.id)?.terminal?.ended.reason == .failed)
    _ = intake.takePendingTermination(first.id, generation: first.generation)
    precondition(intake.terminate(first.id, generation: first.generation, reason: .local,
      now: now.addingTimeInterval(8), requiresReport: false))
    precondition(intake.projection(first.id)?.terminal?.ended.reason == .local &&
      intake.takePendingTermination(first.id, generation: first.generation) == nil,
      "A settled native end preserves canonical terminal state without another LCK event")
    precondition(!intake.terminate(first.id, generation: UUID(), reason: .failed, now: now),
      "An obsolete fallback generation cannot overwrite termination")
  }

  static func resetInvalidatesOldResults() async throws {
    let intake = IncomingIntake()
    let first = try intake.reserve(payload(), source: .pushKit, now: now)
    let duplicate = try intake.reserve(payload("duplicate"), source: .pushKit, now: now)
    let end = CallEndedPayload(eventId: "ended", serverCallId: "server")
    precondition(intake.terminate(end, now: now))
    intake.complete(attempt(duplicate), outcome: .alreadyReported(TestError.duplicate), now: now)
    intake.reset()
    for ticket in [attempt(first).ticket, attempt(duplicate).ticket] {
      do { _ = try await ticket.value; preconditionFailure("Reset must settle pending public tickets") }
      catch { precondition(error as? IncomingIntake.IntakeError == .managerReset) }
    }
    let replacement = try intake.reserve(payload(), source: .foreground, now: now)
    precondition(replacement.id != first.id && replacement.generation != first.generation)
    let invalidated = intake.projection(first.id)
    intake.complete(attempt(first), outcome: .reported, now: now)
    intake.observeMembership(first.id, now: now)
    precondition(intake.projection(first.id)?.generation == invalidated?.generation &&
      intake.projection(first.id)?.terminal == invalidated?.terminal &&
      intake.projection(replacement.id)?.terminal == nil,
      "Late old membership keeps failed incoming metadata without changing the retry")
    precondition(!intake.terminate(end, now: now), "Reset preserves end-event dedup TTL")
    intake.complete(attempt(replacement), outcome: .reported, now: now)
    _ = try await attempt(replacement).ticket.value
  }

  static func expiredTombstoneAndLongActive() async throws {
    let intake = IncomingIntake()
    let first = try intake.reserve(payload(), source: .pushKit, now: now)
    intake.complete(attempt(first), outcome: .reported, now: now)
    _ = try await attempt(first).ticket.value
    let active = try intake.reserve(payload("after-ttl"), source: .pushKit, now: now.addingTimeInterval(300))
    precondition(active.id == first.id && active.payload == first.payload,
      "Actual active lifetime keeps its canonical identity beyond event TTL")
    intake.complete(attempt(active), outcome: .reported, now: now.addingTimeInterval(300))
    _ = try await attempt(active).ticket.value
    precondition(intake.removeConversation(first.id, now: now.addingTimeInterval(301)))
    let fresh = try intake.reserve(payload("fresh"), source: .foreground, now: now.addingTimeInterval(421))
    precondition(fresh.id != first.id && intake.projection(first.id) == nil,
      "Expired inactive metadata is pruned and the gate permits a new invitation")
    intake.complete(attempt(fresh), outcome: .reported, now: now.addingTimeInterval(421))
    _ = try await attempt(fresh).ticket.value
  }

  static func validation() throws {
    let intake = IncomingIntake()
    var invalid = payload()
    invalid.eventId = " \n"
    do { _ = try intake.reserve(invalid, source: .pushKit, now: now); preconditionFailure("Invalid reservation accepted") }
    catch { precondition(error as? ConversationClientError == .invalidPayload) }
    let good = try intake.reserve(payload(), source: .foreground, now: now)
    intake.complete(attempt(good), outcome: .reported, now: now)
  }

  static func resetPreservesKnownIdentity() async throws {
    let intake = IncomingIntake()
    let stamp = now
    let known = try intake.reserve(payload(), source: .pushKit, now: stamp)
    intake.complete(attempt(known), outcome: .reported, now: stamp)
    _ = try await attempt(known).ticket.value
    precondition(intake.terminate(known.id, generation: known.generation, reason: .local,
      now: stamp, requiresReport: false))
    let explicitTerminal = intake.projection(known.id)?.terminal
    let oldPending = try intake.reserve(payload("pending-before-reset"), source: .pushKit, now: stamp)
    intake.reset(now: stamp.addingTimeInterval(10))
    let foreground = try intake.reserve(payload(name: "Changed after reset"),
      source: .foreground, now: stamp.addingTimeInterval(10))
    guard foreground.id == known.id, foreground.payload == known.payload else {
      throw RegressionFailure.resetIdentity
    }
    guard foreground.generation != known.generation,
          intake.projection(known.id)?.terminal == explicitTerminal,
          intake.takePendingTermination(known.id, generation: foreground.generation) == explicitTerminal else {
      throw RegressionFailure.resetProjection
    }
    guard case .reported = foreground.disposition else { throw RegressionFailure.resetIdentity }
    do { _ = try await attempt(oldPending).ticket.value; preconditionFailure("Reset must fail pending tickets") }
    catch { precondition(error as? IncomingIntake.IntakeError == .managerReset) }
    intake.complete(attempt(oldPending), outcome: .reported, now: stamp)
    guard intake.projection(known.id)?.generation == foreground.generation,
          intake.projection(known.id)?.terminal == explicitTerminal else { throw RegressionFailure.staleGeneration }
    let repeated = try intake.reserve(payload("push-after-reset", name: "Changed again"),
      source: .pushKit, now: stamp)
    precondition(repeated.id == known.id && repeated.payload == known.payload)
    precondition(intake.takePendingTermination(known.id, generation: repeated.generation) == explicitTerminal,
      "A new real report re-arms the preserved explicit terminal reason")
    intake.complete(attempt(repeated), outcome: .reported, now: stamp)
    _ = try await attempt(repeated).ticket.value

    let observed = IncomingIntake()
    let live = try observed.reserve(payload(), source: .pushKit, now: stamp)
    observed.observeMembership(live.id, now: stamp)
    observed.reset(now: stamp.addingTimeInterval(10))
    let afterObservedReset = try observed.reserve(payload(name: "Changed"), source: .foreground, now: stamp)
    guard afterObservedReset.id == live.id, afterObservedReset.payload == live.payload,
          afterObservedReset.generation != live.generation,
          observed.projection(live.id)?.terminal?.ended.reason == .failed else {
      throw RegressionFailure.resetProjection
    }
    observed.complete(attempt(live), outcome: .reported, now: stamp)
    precondition(observed.projection(live.id)?.terminal?.ended.reason == .failed,
      "An old observed-lifetime success cannot clear the reset terminal marker")

    let bounded = IncomingIntake()
    let original = try bounded.reserve(payload(), source: .pushKit, now: stamp)
    bounded.complete(attempt(original), outcome: .reported, now: stamp)
    bounded.reset(now: stamp.addingTimeInterval(5))
    let inclusive = try bounded.reserve(payload(), source: .foreground, now: stamp.addingTimeInterval(120))
    precondition(inclusive.id == original.id && inclusive.payload == original.payload,
      "Reset preserves the existing inclusive 120-second identity window")
    let expired = try bounded.reserve(payload("new-event"), source: .foreground,
      now: stamp.addingTimeInterval(120.001))
    precondition(expired.id != original.id,
      "Reset does not extend the gate's original inactive server tombstone deadline")
    bounded.complete(attempt(expired), outcome: .reported, now: stamp.addingTimeInterval(120.001))
  }

  static func reentrantErrorNormalization() throws {
    for ordinary in [false, true] { try reentrantErrorNormalization(ordinary: ordinary) }
  }

  static func reentrantOutcome(_ ordinary: Bool, _ onDeinit: @escaping @Sendable () -> Void) -> IncomingIntake.ReportOutcome {
    let error = ReentrantError(onDeinit)
    return ordinary ? .failed(error) : .alreadyReported(error)
  }

  static func reentrantErrorNormalization(ordinary: Bool) throws {
    let intake = IncomingIntake()
    let counter = DeinitCount()
    let first = try intake.reserve(payload(), source: .pushKit, now: now)
    let duplicate = try intake.reserve(payload("duplicate"), source: .pushKit, now: now)
    intake.complete(attempt(duplicate), outcome: reentrantOutcome(ordinary) {
      _ = intake.projection(first.id)
      counter.increment()
    }, now: now)
    precondition(counter.value == 0, "A settled failure retains its external error until ownership is established")
    intake.complete(attempt(first), outcome: .reported, now: now)
    guard counter.value == 1 else { throw RegressionFailure.errorNotReleased }

    let observed = IncomingIntake()
    let observedCount = DeinitCount()
    let live = try observed.reserve(payload(), source: .pushKit, now: now)
    let repeated = try observed.reserve(payload("duplicate"), source: .pushKit, now: now)
    observed.complete(attempt(repeated), outcome: reentrantOutcome(ordinary) {
      _ = observed.projection(live.id)
      observedCount.increment()
    }, now: now)
    observed.observeMembership(live.id, now: now)
    guard observedCount.value == 1 else { throw RegressionFailure.errorNotReleased }
    observed.complete(attempt(live), outcome: .reported, now: now)
  }

  static func reentrantErrorReset() throws {
    for ordinary in [false, true] { try reentrantErrorReset(ordinary: ordinary) }
  }

  static func reentrantErrorReset(ordinary: Bool) throws {
    let intake = IncomingIntake()
    let counter = DeinitCount()
    _ = try intake.reserve(payload(), source: .pushKit, now: now)
    let duplicate = try intake.reserve(payload("duplicate"), source: .pushKit, now: now)
    intake.complete(attempt(duplicate), outcome: reentrantOutcome(ordinary) {
      _ = intake.hasPending(duplicate.id)
      counter.increment()
    }, now: now)
    precondition(counter.value == 0)
    intake.reset()
    guard counter.value == 1 else { throw RegressionFailure.errorNotReleased }
  }

  static func resetParkedForegroundFailure() async throws {
    let intake = IncomingIntake()
    let first = try intake.reserve(payload(), source: .foreground, now: now)
    let foreground = try intake.reserve(payload("foreground"), source: .foreground, now: now)
    guard case .join(let joined) = foreground.disposition else { throw RegressionFailure.foregroundOwnership }
    let second = try intake.reserve(payload("push"), source: .pushKit, now: now)
    intake.complete(attempt(first), outcome: .failed(TestError.reportFailed), now: now)
    intake.reset(now: now)
    for ticket in [joined, attempt(first).ticket, attempt(second).ticket] {
      do { _ = try await ticket.value; preconditionFailure("Reset must fail canonical and pending public results") }
      catch { precondition(error as? IncomingIntake.IntakeError == .managerReset) }
    }
    let retry = try intake.reserve(payload(), source: .foreground, now: now)
    precondition(retry.id != first.id && retry.generation != first.generation)
    intake.complete(attempt(second), outcome: .reported, now: now)
    intake.observeMembership(first.id, now: now)
    precondition(intake.projection(first.id)?.terminal?.ended.reason == .failed &&
                 intake.projection(retry.id)?.terminal == nil && !intake.hasPending(first.id),
                 "Late old success only drains reset work; it cannot own or replace the new invitation")
    intake.complete(attempt(retry), outcome: .reported, now: now)
    _ = try await attempt(retry).ticket.value
  }

  static func resetRetainsUnsettledMetadata() async throws {
    for known in [false, true] {
      let intake = IncomingIntake()
      var pending = try intake.reserve(payload(), source: .pushKit, now: now)
      if known {
        intake.complete(attempt(pending), outcome: .reported, now: now)
        pending = try intake.reserve(payload("pending-known"), source: .pushKit, now: now)
      }
      intake.reset(now: now.addingTimeInterval(1))
      intake.observeMembership(pending.id, now: now.addingTimeInterval(121))
      let retry = try intake.reserve(payload("late-retry"), source: .foreground,
        now: now.addingTimeInterval(121))
      precondition(retry.id != pending.id,
        "Late invalidated membership cannot restore discarded or expired gate aliases")
      intake.complete(attempt(pending), outcome: .reported, now: now.addingTimeInterval(122))
      intake.complete(attempt(retry), outcome: .reported, now: now.addingTimeInterval(122))
    }
    for (previouslyReported, membershipBeforeSettlement) in [
      (false, true), (false, false), (true, true), (true, false)
    ] {
      let intake = IncomingIntake()
      var pending = try intake.reserve(payload(), source: .pushKit, now: now)
      if previouslyReported {
        intake.complete(attempt(pending), outcome: .reported, now: now)
        _ = try await attempt(pending).ticket.value
        pending = try intake.reserve(payload("pending-known"), source: .pushKit, now: now)
      }
      intake.reset(now: now.addingTimeInterval(1))
      guard let invalidated = intake.projection(pending.id),
            invalidated.payload == pending.payload,
            invalidated.generation != pending.generation,
            invalidated.terminal?.ended.reason == .failed,
            intake.hasPending(pending.id) else { throw RegressionFailure.resetProjection }
      do { _ = try await attempt(pending).ticket.value; preconditionFailure("Reset must fail old public ticket") }
      catch { precondition(error as? IncomingIntake.IntakeError == .managerReset) }
      let retry = try intake.reserve(payload(name: "Retry caller"), source: .foreground,
        now: now.addingTimeInterval(2))
      precondition((retry.id == pending.id) == previouslyReported,
        "Only unreported aliases roll back; known gate identities remain deduplicated")
      if case .report(let retryAttempt) = retry.disposition {
        intake.complete(retryAttempt, outcome: .reported, now: now.addingTimeInterval(2))
        _ = try await retryAttempt.ticket.value
      }
      let unrelated = try intake.reserve(payload("unrelated", server: "other"),
        source: .foreground, now: now.addingTimeInterval(300))
      // A known tombstone has expired by this point; establish a real fresh
      // retry before late old membership, just as the never-owned case already did.
      let activeRetry: IncomingIntake.Reservation
      if previouslyReported {
        activeRetry = try intake.reserve(payload("fresh-retry", name: "Fresh caller"),
          source: .foreground, now: now.addingTimeInterval(300))
        precondition(activeRetry.id != pending.id)
        intake.complete(attempt(activeRetry), outcome: .reported, now: now.addingTimeInterval(300))
      } else { activeRetry = retry }
      precondition(intake.projection(pending.id)?.payload == pending.payload,
        "Actual unresolved reporting protects reset metadata beyond the ordinary TTL")
      if membershipBeforeSettlement { intake.observeMembership(pending.id, now: now.addingTimeInterval(300)) }
      intake.complete(attempt(pending), outcome: .reported, now: now.addingTimeInterval(301))
      precondition(!intake.hasPending(pending.id))
      if !membershipBeforeSettlement { intake.observeMembership(pending.id, now: now.addingTimeInterval(302)) }
      guard let late = intake.projection(pending.id),
            late.generation == invalidated.generation,
            late.payload == pending.payload,
            late.terminal == invalidated.terminal else { throw RegressionFailure.staleGeneration }
      precondition(intake.takePendingTermination(pending.id, generation: late.generation) == late.terminal,
        "Late actual membership retains incoming payload and failed termination for cleanup")
      let liveRetry = try intake.reserve(payload("retry-again"), source: .foreground,
        now: now.addingTimeInterval(302))
      precondition(liveRetry.id == activeRetry.id && liveRetry.payload == activeRetry.payload,
        "Old late membership cannot replace the retry's active gate association")
      precondition(intake.removeConversation(pending.id, generation: late.generation,
        now: now.addingTimeInterval(303)))
      intake.complete(attempt(unrelated), outcome: .failed(TestError.reportFailed), now: now.addingTimeInterval(303))
      let prune = try intake.reserve(payload("prune", server: "prune"), source: .foreground,
        now: now.addingTimeInterval(423))
      precondition(intake.projection(pending.id) == nil,
        "Settled invalidated metadata retires after the bounded tombstone window")
      intake.complete(attempt(prune), outcome: .reported, now: now.addingTimeInterval(423))
    }
  }
}
