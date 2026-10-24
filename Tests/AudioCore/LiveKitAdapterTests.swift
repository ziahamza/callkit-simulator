import AVFAudio
import Dispatch
import Foundation
import LiveKit
import VoIPPlatform
import VoIPSDK

private enum Failure: Error { case assertion(String) }

@main struct LiveKitAdapterTests {
  @MainActor static func main() async throws {
    guard CommandLine.arguments.count == 2 else { throw Failure.assertion("Expected one host scenario") }
    let scenario = CommandLine.arguments[1]
    switch scenario {
    case "call-time": try await callTime()
    case "active-reprepare": try await activeReprepare()
    case "configuration-failure": try await configurationFailure()
    case "preparation-failure": try await preparationFailure()
    case "activation-failure": try await activationFailure()
    case "queued-cancellation": try await queuedCancellation()
    case "queued-fulfillment-cancellation": try await queuedCancellation(fulfillment: true)
    case "preparation-cancellation": try await preparationCancellation()
    case "stale-deactivate": try await staleActivation(reset: false)
    case "stale-reset": try await staleActivation(reset: true)
    default: throw Failure.assertion("Unknown host scenario")
    }
    let events = AudioManager.shared.events
    for (index, event) in events.enumerated() {
      if case .attempt(_, let automatic) = event {
        try expect(!automatic && events[..<index].contains(.automatic(false)),
          "Automatic configuration must be off before every native backend availability call")
      }
    }
    print("passed: \(scenario); actual Swift adapter/queue/configuration with inert AV and LiveKit facades only")
  }

  @MainActor private static func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw Failure.assertion(message) }
  }
  @MainActor private static func eventually(_ predicate: () -> Bool) async throws {
    let deadline = ContinuousClock.now.advanced(by: .seconds(2))
    while !predicate() {
      guard ContinuousClock.now < deadline else { throw Failure.assertion("Host operation did not finish") }
      await Task.yield()
    }
  }
  @MainActor private static func callTime() async throws {
    let adapter = LiveKitMediaAdapter()
    _ = await AudioSessionQueue.shared.perform { true }
    try expect(AVAudioSession.fixture.configurationCalls == 0 && AudioManager.shared.events.isEmpty,
      "Constructing the adapter must do no idle configuration or backend work")
    try await adapter.prepare()
    try expect(AVAudioSession.fixture.configurationCalls == 0 && AudioManager.shared.calls == [.none],
      "Pre-handler preparation gates the backend without changing the session category")
    try await adapter.prepareForFulfillment()
    try expect(AVAudioSession.fixture.configurationCalls == 1 && AudioManager.shared.calls == [.none],
      "Only accepted-call fulfillment preparation configures the session without authorizing media")
  }
  @MainActor private static func activeReprepare() async throws {
    let adapter = LiveKitMediaAdapter(), backend = AudioManager.shared
    try await adapter.prepare()
    try await adapter.prepareForFulfillment()
    adapter.setSystemAudioAuthorization(true)
    try await adapter.didActivate(AVAudioSession())
    let before = backend.calls
    try expect(before == [.none, .default], "Genuine authorization must enable the prepared backend")
    try await adapter.prepare()
    try expect(AVAudioSession.fixture.configurationCalls == 1 && backend.calls == before,
      "Pre-handler preparation of a second call preserves the active call and category")
    try await adapter.prepareForFulfillment()
    try expect(AVAudioSession.fixture.configurationCalls == 2 && backend.calls == before,
      "Fulfillment preparation of a second call must preserve an already-authorized active call")
  }
  @MainActor private static func configurationFailure() async throws {
    let adapter = LiveKitMediaAdapter()
    try await adapter.prepare()
    try expect(AVAudioSession.fixture.configurationCalls == 0, "Manual preparation cannot configure the session")
    AVAudioSession.fixture.rejectNextConfiguration()
    do { try await adapter.prepareForFulfillment(); throw Failure.assertion("Fulfillment configuration failure must propagate") }
    catch AVAudioSession.FixtureError.configurationRejected {}
    try expect(AVAudioSession.fixture.configurationCalls == 1 && AudioManager.shared.calls == [.none],
      "Failed category configuration must not authorize media")
  }
  @MainActor private static func preparationFailure() async throws {
    let adapter = LiveKitMediaAdapter(), backend = AudioManager.shared
    backend.rejectNextAvailability(.none)
    do { try await adapter.prepare(); throw Failure.assertion("Backend preparation failure must propagate") }
    catch AudioFixtureError.availabilityRejected(.none) {}
    adapter.setSystemAudioAuthorization(true)
    try await adapter.didActivate(AVAudioSession())
    try expect(!backend.calls.contains(.default), "A failed availability gate must not publish prepared readiness")
  }
  @MainActor private static func activationFailure() async throws {
    let adapter = LiveKitMediaAdapter(), backend = AudioManager.shared
    try await adapter.prepare()
    try await adapter.prepareForFulfillment()
    adapter.setSystemAudioAuthorization(true)
    backend.rejectNextAvailability(.default)
    do { try await adapter.didActivate(AVAudioSession()); throw Failure.assertion("Activation failure must propagate") }
    catch AudioFixtureError.availabilityRejected(.default) {}
    try expect(backend.calls == [.none], "Rejected activation must not become a successful backend enable")
  }
  @MainActor private static func queuedCancellation(fulfillment: Bool = false) async throws {
    let adapter = LiveKitMediaAdapter()
    if fulfillment { try await adapter.prepare() }
    let before = AudioManager.shared.events
    let entered = AsyncStream<Void>.makeStream()
    let release = DispatchSemaphore(value: 0)
    defer { release.signal(); entered.continuation.finish() }
    VSAudioSessionGetQueue().async {
      entered.continuation.yield(())
      precondition(release.wait(timeout: .now() + 3) == .success)
    }
    for await _ in entered.stream { break }
    var started = false
    let task = Task { @MainActor in
      started = true
      if fulfillment { try await adapter.prepareForFulfillment() }
      else { try await adapter.prepare() }
    }
    try await eventually { started }
    task.cancel(); release.signal()
    do { try await task.value; throw Failure.assertion("Queued cancellation must propagate") }
    catch is CancellationError {}
    if !fulfillment {
      adapter.setSystemAudioAuthorization(true)
      try await adapter.didActivate(AVAudioSession())
    }
    try expect(AVAudioSession.fixture.configurationCalls == 0 && AudioManager.shared.events == before,
      "Cancelled queued preparation must not configure or authorize media")
  }
  @MainActor private static func preparationCancellation() async throws {
    let adapter = LiveKitMediaAdapter(), backend = AudioManager.shared
    backend.armSuspendedAvailability(.none)
    defer { backend.releaseAvailability() }
    let task = Task { @MainActor in try await adapter.prepare() }
    try await eventually { backend.availabilityStarted }
    task.cancel(); backend.releaseAvailability()
    do { try await task.value; throw Failure.assertion("Cancellation during native preparation must propagate") }
    catch is CancellationError {}
    adapter.setSystemAudioAuthorization(true)
    try await adapter.didActivate(AVAudioSession())
    try expect(!backend.calls.contains(.default), "A late cancelled preparation return cannot publish readiness")
  }
  @MainActor private static func staleActivation(reset: Bool) async throws {
    let adapter = LiveKitMediaAdapter(), backend = AudioManager.shared
    let operations = AudioAdapterOperations()
    var complete = false
    var errors: [String] = []
    operations.onError = { errors.append($0) }
    backend.armSuspendedAvailability(.none)
    defer { backend.releaseAvailability() }
    operations.enqueue { try await adapter.prepare() }
    try await eventually { backend.availabilityStarted }
    adapter.setSystemAudioAuthorization(true)
    operations.enqueue { try await adapter.didActivate(AVAudioSession()) }
    adapter.setSystemAudioAuthorization(false)
    if reset { operations.enqueue { try await adapter.prepare() } }
    else { operations.enqueue { try await adapter.didDeactivate(AVAudioSession()) } }
    operations.enqueue { complete = true }
    backend.releaseAvailability()
    try await eventually { complete }
    try expect(errors.isEmpty && !backend.calls.isEmpty && backend.calls.allSatisfy { $0 == .none },
      "Queued activation must remain disabled after genuine deactivation or reset")
  }
}
