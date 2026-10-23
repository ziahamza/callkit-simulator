import Foundation

/// The shared identity and metadata ledger for foreground and PushKit reports.
/// The lock protects only value changes; platform calls and continuation resumes
/// always happen after unlocking. No executor, including MainActor, owns intake.
final class IncomingIntake: @unchecked Sendable {
  enum IntakeError: Error, Sendable { case managerReset }

  /// A failed platform result may precede another same-UUID attempt's success.
  /// Public tickets may wait; actual report calls and native completion never do.
  enum ReportOutcome: Sendable {
    case reported
    case alreadyReported(any Error)
    case failed(any Error)
  }

  final class Ticket: @unchecked Sendable {
    private let lock = NSLock()
    private var result: Result<UUID, any Error>?
    private var waiters: [CheckedContinuation<UUID, any Error>] = []

    var value: UUID {
      get async throws {
        try await withCheckedThrowingContinuation { continuation in
          lock.lock()
          let result = self.result
          if result == nil { waiters.append(continuation) }
          lock.unlock()
          if let result { continuation.resume(with: result) }
        }
      }
    }

    fileprivate func resolve(_ result: Result<UUID, any Error>) {
      lock.lock()
      guard self.result == nil else { lock.unlock(); return }
      self.result = result
      let waiters = self.waiters
      self.waiters.removeAll()
      lock.unlock()
      for waiter in waiters { waiter.resume(with: result) }
    }
  }

  struct Attempt: Sendable {
    let id: UUID
    let generation: UUID
    let attemptID: UUID
    let ticket: Ticket
  }

  enum Disposition: Sendable {
    case report(Attempt)
    case join(Ticket)
    case reported
  }

  struct Reservation: Sendable {
    let id: UUID
    let generation: UUID
    let payload: IncomingCallPayload
    let disposition: Disposition
  }

  struct Terminal: Sendable, Equatable {
    let revision: UUID
    let ended: ConversationEnded
  }

  struct Projection: Sendable {
    let id: UUID
    let generation: UUID
    let payload: IncomingCallPayload
    let terminal: Terminal?
  }

  private struct SettledFailure {
    let ticket: Ticket
    let error: any Error
  }
  private final class Entry {
    let generation = UUID()
    let payload: IncomingCallPayload
    // The first attempt's public result represents the canonical lifetime, not
    // whichever duplicate report happens to settle first.
    let canonicalTicket = Ticket()
    var attempts: [UUID: Attempt] = [:]
    // Reset fails public tickets, but it cannot cancel a framework async call.
    // Retain only its identity until actual settlement, without reviving it.
    var invalidatedAttempts: [UUID: UUID] = [:]
    var settledFailures: [SettledFailure] = []
    var everReported = false
    var everObserved = false
    var resetInvalidated = false
    var active = true
    var retainedUntil: Date
    var terminal: Terminal?
    var terminalPending = false
    init(payload: IncomingCallPayload, now: Date) {
      self.payload = payload
      retainedUntil = now.addingTimeInterval(120)
    }
    var hasPending: Bool { !attempts.isEmpty || !invalidatedAttempts.isEmpty || !settledFailures.isEmpty }
    var ownershipKnown: Bool { everReported || everObserved }
  }

  private let lock = NSLock()
  private var invitations = IncomingInvitationGate()
  private var entries: [UUID: Entry] = [:]
  private var endEventIDs: [String: Date] = [:]

  /// Reserve synchronously before scheduling reporting. PushKit never joins a
  /// predecessor: each delivery receives its own actual report attempt.
  func reserve(_ payload: IncomingCallPayload, source: IncomingReportSource,
               now: Date = Date()) throws -> Reservation {
    guard !payload.eventId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
          !payload.serverCallId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
          !payload.caller.id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
      throw ConversationClientError.invalidPayload
    }
    lock.lock()
    defer { lock.unlock() }
    entries = entries.filter { $0.value.active || $0.value.hasPending || $0.value.retainedUntil >= now }
    let registration = invitations.register(eventId: payload.eventId,
      serverCallId: payload.serverCallId, now: now)
    let existing = entries[registration.id]
    let entry = existing ?? Entry(payload: payload, now: now)
    entries[registration.id] = entry
    entry.retainedUntil = max(entry.retainedUntil, now.addingTimeInterval(120))

    let disposition: Disposition
    if source == .foreground, existing != nil {
      disposition = entry.ownershipKnown ? .reported : .join(entry.canonicalTicket)
    } else {
      let attempt = Attempt(id: registration.id, generation: entry.generation,
        attemptID: UUID(), ticket: existing == nil ? entry.canonicalTicket : Ticket())
      entry.attempts[attempt.attemptID] = attempt
      // An ended UUID can be accepted again by LCK. Its original termination
      // must also apply to the newly reported platform conversation.
      if entry.terminal != nil { entry.terminalPending = true }
      disposition = .report(attempt)
    }
    return Reservation(id: registration.id, generation: entry.generation,
      payload: entry.payload, disposition: disposition)
  }

  /// Commit the actual platform settlement before finishing native delivery.
  /// This method neither waits for MainActor nor invokes external code locked.
  func complete(_ attempt: Attempt, outcome: ReportOutcome, now: Date = Date()) {
    var resolutions: [(Ticket, Result<UUID, any Error>)] = []
    var retiredFailures: [SettledFailure] = []
    lock.lock()
    if let entry = entries[attempt.id], entry.invalidatedAttempts[attempt.attemptID] == attempt.generation {
      entry.invalidatedAttempts.removeValue(forKey: attempt.attemptID)
      // Late manager publication may follow this settlement. Keep its incoming
      // payload and failed terminal available for bounded reconciliation; this
      // old result grants no live ownership to the replacement generation.
      entry.retainedUntil = max(entry.retainedUntil, now.addingTimeInterval(120))
    } else if let entry = entries[attempt.id], entry.generation == attempt.generation,
              entry.attempts.removeValue(forKey: attempt.attemptID) != nil {
      switch outcome {
      case .reported:
        entry.resetInvalidated = false
        entry.everReported = true
        entry.active = true
        invitations.markActive(attempt.id, serverCallId: entry.payload.serverCallId, now: now)
        resolutions.append((attempt.ticket, .success(attempt.id)))
      case .alreadyReported(let error), .failed(let error):
        if entry.ownershipKnown { resolutions.append((attempt.ticket, .success(attempt.id))) }
        else { entry.settledFailures.append(SettledFailure(ticket: attempt.ticket, error: error)) }
      }
      resolveTickets(entry, id: attempt.id, into: &resolutions, retiring: &retiredFailures)
      if entry.attempts.isEmpty && entry.invalidatedAttempts.isEmpty && !entry.ownershipKnown {
        // Every actual attempt has failed. A real retry gets a fresh UUID;
        // failures cannot erase another pending attempt or successful lifetime.
        entries.removeValue(forKey: attempt.id)
        invitations.discardUnreported(attempt.id)
      }
    }
    lock.unlock()
    // An external Error can run arbitrary code from deinit. Even successful
    // normalization must retain discarded errors until after the intake unlocks.
    withExtendedLifetime((outcome, retiredFailures)) {}
    for (ticket, result) in resolutions { ticket.resolve(result) }
  }

  private func resolveTickets(_ entry: Entry, id: UUID,
                              into resolutions: inout [(Ticket, Result<UUID, any Error>)],
                              retiring retired: inout [SettledFailure]) {
    guard entry.ownershipKnown || entry.attempts.isEmpty else { return }
    if entry.ownershipKnown { resolutions.append((entry.canonicalTicket, .success(id))) }
    for failure in entry.settledFailures {
      resolutions.append((failure.ticket,
        entry.ownershipKnown ? .success(id) : .failure(failure.error)))
    }
    retired.append(contentsOf: entry.settledFailures)
    entry.settledFailures.removeAll()
  }

  func projection(_ id: UUID) -> Projection? {
    lock.lock()
    defer { lock.unlock() }
    guard let entry = entries[id] else { return nil }
    return Projection(id: id, generation: entry.generation, payload: entry.payload,
      terminal: entry.terminal)
  }

  func hasPending(_ id: UUID) -> Bool {
    lock.lock()
    defer { lock.unlock() }
    return entries[id]?.hasPending == true
  }

  /// An actual manager object is ownership evidence before async success unless
  /// reset invalidated its lifetime. Late invalidated objects remain projectable
  /// with their terminal marker, but must not reclaim discarded server aliases.
  func observeMembership(_ id: UUID, now: Date = Date()) {
    var resolutions: [(Ticket, Result<UUID, any Error>)] = []
    var retiredFailures: [SettledFailure] = []
    lock.lock()
    if let entry = entries[id], !entry.resetInvalidated {
      entry.everObserved = true
      entry.active = true
      invitations.markActive(id, serverCallId: entry.payload.serverCallId, now: now)
      resolveTickets(entry, id: id, into: &resolutions, retiring: &retiredFailures)
    }
    lock.unlock()
    withExtendedLifetime(retiredFailures) {}
    for (ticket, result) in resolutions { ticket.resolve(result) }
  }

  /// Preserve the existing callEnded wire/event dedup policy, including recording
  /// an unknown server event. A known in-flight invitation retains termination.
  @discardableResult
  func terminate(_ payload: CallEndedPayload, now: Date = Date()) -> Bool {
    // A transport notification cannot request a local system action. Reject it
    // before event dedup so a corrected remote notification remains eligible.
    guard payload.reason != .local else { return false }
    lock.lock()
    defer { lock.unlock() }
    endEventIDs = endEventIDs.filter { now.timeIntervalSince($0.value) <= 120 }
    guard endEventIDs[payload.eventId] == nil else { return false }
    endEventIDs[payload.eventId] = now
    guard let id = invitations.conversation(serverCallId: payload.serverCallId),
          let entry = entries[id] else { return false }
    entry.terminal = Terminal(revision: UUID(), ended: ConversationEnded(at: now, reason: payload.reason))
    entry.terminalPending = true
    return true
  }

  /// Record native/fallback termination without a server event. Already settled
  /// native ends set requiresReport=false to avoid a second platform end event.
  @discardableResult
  func terminate(_ id: UUID, generation: UUID, reason: ConversationEndedReason,
                 now: Date = Date(), requiresReport: Bool = true) -> Bool {
    lock.lock()
    defer { lock.unlock() }
    guard let entry = entries[id], entry.generation == generation else { return false }
    entry.terminal = Terminal(revision: UUID(), ended: ConversationEnded(at: now, reason: reason))
    entry.terminalPending = requiresReport
    return true
  }

  /// Claim only when the actor has an actual platform conversation to report on.
  /// Claiming does not erase the terminal marker used by state projection.
  func takePendingTermination(_ id: UUID, generation: UUID) -> Terminal? {
    lock.lock()
    defer { lock.unlock() }
    guard let entry = entries[id], entry.generation == generation,
          entry.terminalPending else { return nil }
    entry.terminalPending = false
    return entry.terminal
  }

  /// Retain canonical payload through the gate's event/server tombstone lifetime.
  /// A pending actual report must settle before its metadata can be retired.
  @discardableResult
  func removeConversation(_ id: UUID, generation: UUID? = nil, now: Date = Date()) -> Bool {
    lock.lock()
    defer { lock.unlock() }
    guard let entry = entries[id], generation == nil || generation == entry.generation,
          !entry.hasPending else { return false }
    entry.active = false
    invitations.removeConversation(id, now: now)
    if entry.retainedUntil < now { entries.removeValue(forKey: id) }
    return true
  }

  /// Only a real manager reset calls this. Invalidate pending generations while
  /// preserving known tombstones and any actual call that has yet to settle.
  /// Unreported gate aliases may roll back, but their old UUID retains incoming
  /// metadata for a late platform object. Old results cannot restore live state.
  func reset(now: Date = Date()) {
    var tickets: [Ticket] = []
    var retiredEntries: [Entry] = []
    lock.lock()
    retiredEntries = Array(entries.values)
    var retained: [UUID: Entry] = [:]
    for (id, entry) in entries {
      tickets.append(entry.canonicalTicket)
      tickets.append(contentsOf: entry.attempts.values.map(\.ticket))
      tickets.append(contentsOf: entry.settledFailures.map(\.ticket))
      var invalidated = entry.invalidatedAttempts
      for attempt in entry.attempts.values { invalidated[attempt.attemptID] = attempt.generation }
      if entry.ownershipKnown {
        invitations.removeConversation(id, now: now)
      } else {
        invitations.discardUnreported(id)
      }
      guard entry.ownershipKnown || !invalidated.isEmpty else { continue }
      let replacement = Entry(payload: entry.payload, now: now)
      replacement.invalidatedAttempts = invalidated
      replacement.resetInvalidated = true
      replacement.everReported = entry.everReported
      replacement.everObserved = entry.everObserved
      replacement.active = false
      replacement.retainedUntil = entry.retainedUntil
      replacement.terminal = entry.terminal ?? Terminal(revision: UUID(),
        ended: ConversationEnded(at: now, reason: .failed))
      // A still-running call may publish a residual object after reset. Its
      // terminal marker must be claimable when that actual object appears.
      replacement.terminalPending = !invalidated.isEmpty
      retained[id] = replacement
    }
    entries = retained
    lock.unlock()
    // Retired entries can own external delayed errors whose deinit reenters us.
    withExtendedLifetime(retiredEntries) {}
    for ticket in tickets { ticket.resolve(.failure(IntakeError.managerReset)) }
  }
}
