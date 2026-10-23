import Foundation

@main struct SessionTests {
  enum Failure: Error { case timedOut, streamEnded, expectedSetterError, expectedRevocation }
  static let builtIn = AVAudioSessionPortDescription("built-in", "Built-in microphone", "MicrophoneBuiltIn")
  static let receiver = AVAudioSessionPortDescription("receiver", "Actual receiver", "Receiver")

  @MainActor static func wait(_ condition: () -> Bool) async throws {
    let deadline = ContinuousClock.now + .seconds(3)
    while !condition() {
      guard ContinuousClock.now < deadline else { throw Failure.timedOut }
      try await Task.sleep(for: .milliseconds(1))
    }
  }
  static func entered(_ gate: RouteReadGate) async throws {
    try await withThrowingTaskGroup(of: Void.self) { group in
      group.addTask {
        var iterator = gate.entered.makeAsyncIterator()
        guard await iterator.next() != nil else { throw Failure.streamEnded }
      }
      group.addTask { try await Task.sleep(for: .seconds(3)); throw Failure.timedOut }
      defer { group.cancelAll() }
      _ = try await group.next()
    }
  }
  @MainActor static func makeOwner() async throws -> SystemAudioSession {
    AVAudioSession.fixture.configure(inputs: [builtIn], output: receiver)
    let owner = SystemAudioSession()
    try await wait { owner.routes.snapshot.current?.id == "output:receiver" }
    AVAudioSession.fixture.resetMetrics()
    return owner
  }

  @MainActor static func coalescedNotifications() async throws {
    let owner = try await makeOwner()
    let input = AVAudioSessionPortDescription("usb", "OS USB input", "USBAudio")
    let output = AVAudioSessionPortDescription("usb-out", "OS USB output", "USBAudio")
    AVAudioSession.fixture.configure(inputs: [input], output: output)
    NotificationCenter.default.post(name: AVAudioSession.routeChangeNotification, object: nil)
    NotificationCenter.default.post(name: AVAudioSession.availableInputsChangeNotification, object: nil)
    NotificationCenter.default.post(name: AVAudioSession.routeChangeNotification, object: nil)
    precondition(AVAudioSession.fixture.metrics().inputs == 0,
      "Notification delivery must schedule a delayed reread rather than perform native work inline")
    try await wait { owner.routes.snapshot.current?.id == "output:usb-out" }
    let reads = await AudioSessionQueue.shared.perform { AVAudioSession.fixture.metrics() }
    precondition(reads.inputs == 1 && reads.current == 1,
      "One synchronous notification burst must coalesce to one native route read")
    precondition(owner.routes.snapshot.routes.contains { $0.id == "input:usb" },
      "Available-input changes must update selection choices")
    // An input-inventory change can occur without any current-route change.
    // A route-only observer must fail this independent notification control.
    AVAudioSession.fixture.resetMetrics()
    AVAudioSession.fixture.configure(inputs: [.init("new-input", "New OS input", "USBAudio")], output: output)
    NotificationCenter.default.post(name: AVAudioSession.availableInputsChangeNotification, object: nil)
    try await wait { owner.routes.snapshot.routes.contains { $0.id == "input:new-input" } }
    let inputReads = await AudioSessionQueue.shared.perform { AVAudioSession.fixture.metrics() }
    precondition(inputReads.inputs == 1 && inputReads.current == 1 &&
      owner.routes.snapshot.current?.id == "output:usb-out")
  }

  @MainActor static func staleRead() async throws {
    let owner = try await makeOwner()
    var published: [AudioRoutesSnapshot] = []
    let token = owner.routes.observe { snapshot in
      precondition(Thread.isMainThread); published.append(snapshot)
    }
    defer { owner.routes.removeObserver(token) }
    let gate = RouteReadGate()
    defer { gate.unblock() }
    AVAudioSession.fixture.configure(inputs: [.init("stale", "Old input", "USBAudio")], output: receiver)
    AVAudioSession.fixture.blockNextInputRead(gate)
    owner.refreshRoutes()
    try await entered(gate)
    AVAudioSession.fixture.configure(inputs: [.init("fresh", "New input", "BluetoothHFP")],
      output: .init("fresh-out", "New output", "BluetoothHFP"))
    owner.refreshRoutes() // Invalidate an observation already blocked inside the native read.
    gate.unblock()
    try await wait { owner.routes.snapshot.current?.id == "output:fresh-out" }
    precondition(!published.contains { snapshot in snapshot.routes.contains { $0.id == "input:stale" } },
      "An older blocked read must not publish after a new probe generation")
  }

  @MainActor static func failedSelectionRereads() async throws {
    let owner = try await makeOwner()
    owner.setSystemAudioAuthorization(true)
    AVAudioSession.fixture.rejectNextOverride(observing: .init("external", "OS-selected output", "AirPlay"))
    do {
      try await owner.selectRoute("speaker", isActive: true)
      throw Failure.expectedSetterError
    } catch AVAudioSession.FixtureError.setterRejected {}
    precondition(owner.routes.snapshot.current?.id == "output:external" &&
      owner.routes.snapshot.current?.type == "AirPlay",
      "A rejected native selection must still reread actual OS output")
    let reads = AVAudioSession.fixture.metrics()
    precondition(reads.inputs == 1 && reads.current == 1 && reads.mutations == [.outputOverride(.speaker)])
    AVAudioSession.fixture.resetMetrics()
    try await owner.selectRoute("input:built-in", isActive: true)
    precondition(AVAudioSession.fixture.metrics().mutations == [.outputOverride(.none), .input("built-in")],
      "Input selection must clear speaker override and pass the actual available input UID")
    precondition(owner.routes.snapshot.current?.id == "output:external",
      "A successful input request must still preserve the OS-observed output")
    try await owner.selectRoute("speaker", isActive: true)
    precondition(owner.routes.snapshot.current?.id == "output:speaker-output" &&
      owner.routes.snapshot.current?.name == "Observed speaker" && owner.routes.snapshot.current?.type == "Speaker",
      "Successful speaker selection must publish the observed output UID/name/type, not the command target")
  }

  @MainActor static func revokedAdmissionCannotMutateNewActivation() async throws {
    let owner = try await makeOwner()
    owner.setSystemAudioAuthorization(true)
    let gate = RouteReadGate()
    defer { gate.unblock() }
    AVAudioSession.fixture.blockNextInputRead(gate)
    let selection = Task { @MainActor in try await owner.selectRoute("input:built-in", isActive: true) }
    try await entered(gate)
    owner.setSystemAudioAuthorization(false)
    owner.setSystemAudioAuthorization(true)
    gate.unblock()
    do {
      try await selection.value
      throw Failure.expectedRevocation
    } catch AudioRouteError.audioSessionInactive {}
    precondition(AVAudioSession.fixture.metrics().mutations.isEmpty,
      "An old admission must not set an override/input after false-to-true reactivation")
    precondition(owner.routes.snapshot.current?.id == "output:receiver")
  }

  @MainActor static func main() async throws {
    try await coalescedNotifications()
    try await staleRead()
    try await failedSelectionRereads()
    try await revokedAdmissionCannotMutateNewActivation()
    print("passed: 4 actual SystemAudioSession host route transport groups; inert AV facade only")
  }
}
