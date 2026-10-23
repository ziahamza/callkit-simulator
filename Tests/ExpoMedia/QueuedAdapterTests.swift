import AVFAudio
import Dispatch
import Foundation
import os
import VoIPPlatform
import VSMediaInterop

// The actual imported production interface supplies preparation's callback
// context. Only AVFAudio arguments remain inert Swift host facades.
typealias VSSelectedMediaBinding = VSMediaInterop.VSSelectedMediaBinding
extension VSSelectedMediaBinding {
  static var fixture: VSSelectedMediaBinding { shared() }
  var prepareCalls: Int { Int(VSHostMediaPreparationCount()) }
  func activate(_ session: AVFAudio.AVAudioSession, completion: @escaping @Sendable (Error?) -> Void) {
    VSAudioSessionGetQueue().async { completion(nil) }
  }
  func deactivate(_ session: AVFAudio.AVAudioSession, completion: @escaping @Sendable (Error?) -> Void) {
    VSAudioSessionGetQueue().async { completion(nil) }
  }
}
enum ExpoMediaSelection { static let name = "webrtc" }
private enum Failure: Error { case assertion(String) }

@main struct QueuedAdapterTests {
  @MainActor static func expect(_ value: @autoclosure () -> Bool, _ message: String) throws {
    if !value() { throw Failure.assertion(message) }
  }
  @MainActor static func main() async throws {
    guard CommandLine.arguments.count == 2 else { throw Failure.assertion("Expected one scenario") }
    let adapter = QueuedMediaAdapter()
    let audio = AVFAudio.AVAudioSession.fixture, binding = VSSelectedMediaBinding.fixture
    try expect(audio.configurationCalls == 0 && binding.prepareCalls == 0, "Construction must not prepare audio")
    try await adapter.prepare()
    try expect(audio.configurationCalls == 0 && binding.prepareCalls == 1,
      "Manual preparation before the handler must leave category and mode untouched")
    switch CommandLine.arguments[1] {
    case "phase-order":
      try await adapter.prepareForFulfillment()
      try expect(audio.configurationCalls == 1 && binding.prepareCalls == 2,
        "Fulfillment preparation must execute the actual session configuration closure")
      try await adapter.prepare()
      try expect(audio.configurationCalls == 1, "A later handler's manual preparation must not reconfigure")
      try await adapter.prepareForFulfillment()
      try expect(audio.configurationCalls == 2 && binding.prepareCalls == 4,
        "Each accepted action configures only during its fulfillment preparation")
    case "configuration-failure":
      audio.rejectNextConfiguration()
      var failure: NSError?
      do { try await adapter.prepareForFulfillment() } catch { failure = error as NSError }
      let expected = AVFAudio.AVAudioSession.FixtureError.configurationRejected as NSError
      try expect(failure?.domain == expected.domain && failure?.code == expected.code && audio.configurationCalls == 1,
        "Final configuration failure must cross the actual wrapper's completion boundary")
    case "queued-cancellation":
      let entered = AsyncStream<Void>.makeStream()
      let release = DispatchSemaphore(value: 0)
      defer { release.signal(); entered.continuation.finish() }
      VSAudioSessionGetQueue().async {
        entered.continuation.yield(())
        precondition(release.wait(timeout: .now() + 3) == .success)
      }
      for await _ in entered.stream { break }
      let task = Task { @MainActor in
        try await AudioPreparationScope.perform { try await adapter.prepareForFulfillment() }
      }
      let limit = ContinuousClock.now + .seconds(2)
      while binding.prepareCalls < 2 {
        precondition(ContinuousClock.now < limit, "Final configuration did not queue")
        await Task.yield()
      }
      task.cancel(); release.signal()
      var failed = false
      do { try await task.value } catch { failed = true }
      try expect(failed && audio.configurationCalls == 0,
        "An action cancelled while queued must never enter session configuration")
    default: throw Failure.assertion("Unknown scenario")
    }
    print("passed: \(CommandLine.arguments[1]); actual Swift QueuedMediaAdapter with inert binding/AV boundaries")
  }
}
