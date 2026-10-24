import Foundation

/// Proposed SDK watchdog budgets. Device activation measurements are separate
/// evidence; they do not define these configurable policy intervals.
public struct AudioActivationPolicy: Sendable, Equatable {
  public enum Error: Swift.Error, Sendable { case invalidTimeout }
  public let activationTimeout: TimeInterval
  public let recoveryTimeout: TimeInterval
  public static let standard = AudioActivationPolicy(activation: 10, recovery: 5)

  public init(activationTimeout: TimeInterval = 10, recoveryTimeout: TimeInterval = 5) throws {
    guard activationTimeout.isFinite, activationTimeout > 0,
          recoveryTimeout.isFinite, recoveryTimeout > 0 else { throw Error.invalidTimeout }
    self.init(activation: activationTimeout, recovery: recoveryTimeout)
  }
  private init(activation: TimeInterval, recovery: TimeInterval) {
    activationTimeout = activation; recoveryTimeout = recovery
  }
}

enum AudioActivationEvent: Equatable { case activated, deactivated, missing, failed }

/// One process-wide absence episode, independent of backend work or conversation count.
/// Only the genuine system delegate callbacks may call didActivate/didDeactivate.
@MainActor
final class AudioActivationCoordinator {
  typealias Schedule = @MainActor (TimeInterval, @escaping @MainActor () -> Void) -> @MainActor () -> Void
  private enum Phase { case waiting, recovering, failed }
  private let policy: AudioActivationPolicy
  private let now: @MainActor () -> TimeInterval
  private let schedule: Schedule
  /// Values identify native object lifetimes, independently of reusable call UUIDs.
  private var demand: [UUID: UUID] = [:]
  // Keep failed calls separate while their native end requests are in flight.
  // The episode identity also rejects a reentrant reset/replacement's old end.
  private struct FailedDemand {
    let nativeGeneration: UUID
    let episode: UUID
  }
  private var failedDemand: [UUID: FailedDemand] = [:]
  private var generation: UUID?
  private var phase: Phase?
  private var cancelTimer: (@MainActor () -> Void)?
  private var recoveryTask: Task<Void, Never>?
  private var lckActive = false
  private(set) var isActive = false
  var onEvent: (@MainActor (AudioActivationEvent, Set<UUID>) -> Void)?
  var recover: (@MainActor () async -> Void)?
  var endConversations: (@MainActor ([UUID: UUID]) -> Void)?

  init(policy: AudioActivationPolicy = .standard,
       now: @escaping @MainActor () -> TimeInterval = { ProcessInfo.processInfo.systemUptime },
       schedule: Schedule? = nil) {
    self.policy = policy; self.now = now
    self.schedule = schedule ?? { deadline, fire in
      let task = Task { @MainActor in
        // Chunk long caller-configured intervals to keep Duration conversion finite.
        while !Task.isCancelled {
          let remaining = deadline - ProcessInfo.processInfo.systemUptime
          if remaining <= 0 { fire(); return }
          do { try await Task.sleep(for: .seconds(min(remaining, 3_600))) }
          catch { return }
        }
      }
      return { task.cancel() }
    }
  }

  /// Supply only LCK joined, unheld conversations, never a still-pending app handler.
  func updateDemand(_ nativeGenerations: [UUID: UUID]) {
    let previous = unfailedDemand
    demand = nativeGenerations
    failedDemand = failedDemand.filter { demand[$0.key] == $0.value.nativeGeneration }
    if unfailedDemand.isEmpty { cancelEpisode() }
    else {
      // Keep a shared episode while an original demand survives. An object
      // replacement without such overlap gets a full fresh activation interval.
      let survives = previous.contains { demand[$0.key] == $0.value }
      if phase == .failed || (generation != nil && !survives) { cancelEpisode() }
      if !isActive && generation == nil { beginEpisode() }
    }
  }

  private var unfailedDemand: [UUID: UUID] {
    demand.filter { failedDemand[$0.key]?.nativeGeneration != $0.value }
  }

  func didActivate() {
    let changed = !isActive
    lckActive = true; isActive = true; failedDemand.removeAll(); cancelEpisode()
    if changed { onEvent?(.activated, []) }
  }
  func didDeactivate() {
    let changed = lckActive
    lckActive = false; isActive = false
    if changed { onEvent?(.deactivated, []) }
    if !isActive && !unfailedDemand.isEmpty && generation == nil { beginEpisode() }
  }

  /// A real inactive/interruption observation revokes authorization without
  /// inventing an LCK delegate callback. Only a later genuine callback restores it.
  func systemBecameInactive() {
    isActive = false
    if !unfailedDemand.isEmpty && generation == nil { beginEpisode() }
  }

  /// Reset invalidates authorization without fabricating a deactivation callback/event.
  func reset() {
    cancelEpisode(); demand.removeAll(); failedDemand.removeAll(); lckActive = false; isActive = false
  }

  /// Cancellation callbacks may reset, hold or replace the absence episode.
  func shouldEndConversation(_ id: UUID, generation: UUID) -> Bool {
    !isActive && demand[id] == generation && failedDemand[id]?.nativeGeneration == generation
  }

  private func beginEpisode() {
    let id = UUID(); generation = id; phase = .waiting
    cancelTimer = schedule(now() + policy.activationTimeout) { [weak self] in self?.missing(id) }
  }
  private func missing(_ id: UUID) {
    guard generation == id, phase == .waiting, !isActive, !unfailedDemand.isEmpty else { return }
    phase = .recovering
    cancelTimer?()
    // Arm the final deadline before any observer or async recovery can suspend.
    cancelTimer = schedule(now() + policy.recoveryTimeout) { [weak self] in self?.fail(id) }
    onEvent?(.missing, Set(unfailedDemand.keys))
    guard generation == id, phase == .recovering, !isActive, !unfailedDemand.isEmpty else { return }
    recoveryTask = Task { @MainActor [weak self] in
      guard self?.generation == id, self?.isActive == false, self?.unfailedDemand.isEmpty == false else { return }
      let operation = self?.recover
      // Do not retain this coordinator across an uncooperative recovery operation.
      await operation?()
      if self?.generation == id { self?.recoveryTask = nil }
    }
  }
  private func fail(_ id: UUID) {
    guard generation == id, phase == .recovering, !isActive, !unfailedDemand.isEmpty else { return }
    phase = .failed; cancelTimer?(); cancelTimer = nil
    recoveryTask?.cancel(); recoveryTask = nil
    let affected = unfailedDemand
    for (conversationID, nativeGeneration) in affected {
      failedDemand[conversationID] = FailedDemand(nativeGeneration: nativeGeneration, episode: id)
    }
    onEvent?(.failed, Set(affected.keys))
    // Observers can synchronously end, hold, reset or genuinely activate the session.
    guard !isActive else { return }
    let remaining = affected.filter {
      demand[$0.key] == $0.value && failedDemand[$0.key]?.episode == id
    }
    if !remaining.isEmpty { endConversations?(remaining) }
  }
  private func cancelEpisode() {
    generation = nil; phase = nil
    cancelTimer?(); cancelTimer = nil
    recoveryTask?.cancel(); recoveryTask = nil
  }
}
