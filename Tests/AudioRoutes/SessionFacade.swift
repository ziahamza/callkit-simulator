import Dispatch
import Foundation
import VoIPPlatform

/// An explicit inert AV boundary. No system audio session or activation API exists.
final class RouteReadGate: @unchecked Sendable {
  let entered: AsyncStream<Void>
  private let continuation: AsyncStream<Void>.Continuation
  private let release = DispatchSemaphore(value: 0)

  init() {
    let stream = AsyncStream<Void>.makeStream(bufferingPolicy: .bufferingNewest(1))
    entered = stream.stream; continuation = stream.continuation
  }
  func block() {
    continuation.yield(()); continuation.finish()
    precondition(release.wait(timeout: .now() + 4) == .success, "Host route read was never released")
  }
  func unblock() { release.signal() }
}

struct AVAudioSessionPortDescription: Sendable {
  struct Port: Sendable { let rawValue: String }
  let uid: String
  let portName: String
  let portType: Port
  init(_ uid: String, _ name: String, _ type: String) {
    self.uid = uid; portName = name; portType = Port(rawValue: type)
  }
}
struct AVAudioSessionRouteDescription: Sendable {
  let outputs: [AVAudioSessionPortDescription]
}

final class AVAudioSession: @unchecked Sendable {
  enum PortOverride: Equatable, Sendable { case none, speaker }
  enum FixtureError: Error, Equatable { case setterRejected }
  enum Mutation: Equatable, Sendable { case outputOverride(PortOverride), input(String) }
  struct Metrics: Sendable { let inputs: Int; let current: Int; let mutations: [Mutation] }
  private struct State {
    var inputs: [AVAudioSessionPortDescription] = []
    var outputs: [AVAudioSessionPortDescription] = []
    var inputReads = 0, currentReads = 0
    var mutations: [Mutation] = []
    var gate: RouteReadGate?
    var failedOverrideOutput: AVAudioSessionPortDescription?
  }
  static let fixture = AVAudioSession()
  static let routeChangeNotification = Notification.Name("HostRouteChange")
  static let availableInputsChangeNotification = Notification.Name("HostAvailableInputsChange")
  private let lock = NSLock()
  private var state = State()

  static func sharedInstance() -> AVAudioSession {
    requireQueue(); return fixture
  }
  private static func requireQueue() {
    precondition(VSAudioSessionIsOnQueue() && !Thread.isMainThread,
      "An actual SDK route read/setter escaped its shared native queue")
  }
  var availableInputs: [AVAudioSessionPortDescription]? {
    Self.requireQueue()
    let captured = lock.withLock { () -> ([AVAudioSessionPortDescription], RouteReadGate?) in
      state.inputReads += 1
      let gate = state.gate; state.gate = nil
      return (state.inputs, gate)
    }
    captured.1?.block()
    return captured.0
  }
  var currentRoute: AVAudioSessionRouteDescription {
    Self.requireQueue()
    return lock.withLock {
      state.currentReads += 1
      return AVAudioSessionRouteDescription(outputs: state.outputs)
    }
  }
  func overrideOutputAudioPort(_ value: PortOverride) throws {
    Self.requireQueue()
    let fails = lock.withLock {
      state.mutations.append(.outputOverride(value))
      if let output = state.failedOverrideOutput {
        state.outputs = [output]; state.failedOverrideOutput = nil; return true
      }
      if value == .speaker { state.outputs = [.init("speaker-output", "Observed speaker", "Speaker")] }
      return false
    }
    if fails { throw FixtureError.setterRejected }
  }
  func setPreferredInput(_ input: AVAudioSessionPortDescription?) throws {
    Self.requireQueue()
    lock.withLock { state.mutations.append(.input(input?.uid ?? "nil")) }
  }

  // Fixture setup/inspection is lock-protected and is separate from the AV API.
  func configure(inputs: [AVAudioSessionPortDescription], output: AVAudioSessionPortDescription) {
    lock.withLock { state.inputs = inputs; state.outputs = [output] }
  }
  func resetMetrics() { lock.withLock { state.inputReads = 0; state.currentReads = 0; state.mutations = [] } }
  func metrics() -> Metrics {
    lock.withLock { Metrics(inputs: state.inputReads, current: state.currentReads, mutations: state.mutations) }
  }
  func blockNextInputRead(_ gate: RouteReadGate) { lock.withLock { state.gate = gate } }
  func rejectNextOverride(observing output: AVAudioSessionPortDescription) {
    lock.withLock { state.failedOverrideOutput = output }
  }
}
