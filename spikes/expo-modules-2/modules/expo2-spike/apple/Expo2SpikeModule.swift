import ExpoModulesCore
import Foundation
import Dispatch

@ExpoModule("Expo2Spike", classes: [PingProbe.self])
public final class Expo2SpikeModule: Module {
  @JS func rawAsync(_ callback: @escaping @Sendable (Double) async throws -> Double) async throws -> Double {
    try await callback(handlerNow()) // Bounded resolve/throw probes only: never/reload use HandlerCell.
  }
  @JS func rawBlocking(_ callback: @escaping @Sendable (Double) throws -> Double) async throws -> ClosureProbe {
    let cell = HandlerCell<ClosureProbe>(), runtime = try appContext!.runtime
    DispatchQueue(label: "expo2.spike.blocking-callback").async {
      let before = captureThread("blocking-native-before", runtime: runtime), start = handlerNow()
      let jsEnd = (try? callback(start)) ?? -1
      cell.finish(ClosureProbe(times: [start, jsEnd, handlerNow()], threads: [before,
        captureThread("blocking-native-after", runtime: runtime)], counts: []))
    }
    var result = await cell.wait(); result.counts = cell.stats
    return result
  }

  @JS var startupReceipt: StartupReceipt { StartupLedger.shared.snapshot() }

  private let handlerRuntimeID = UUID().uuidString
  @Event var onHandler: (HandlerWire) -> Void
  @JS var handlerSnapshot: HandlerSnapshot { HandlerLedger.shared.snapshot(handlerRuntimeID) }
  @JS func handlerRegistered(_ enabled: Bool) { HandlerLedger.shared.register(handlerRuntimeID, enabled: enabled) }
  @JS func saveHandlerCheckpoint(_ json: String) { HandlerLedger.shared.checkpoint(json) }
  @JS func handlerClock() -> Double { handlerNow() }
  @JS func handlerMark(_ id: String, _ runtime: String, _ at: Double, _ abort: Bool) {
    HandlerLedger.shared.mark(id, runtime: runtime, at: at, abort: abort)
  }
  @JS func handlerSettle(_ id: String, _ runtime: String, _ success: Bool, _ at: Double) -> Bool {
    HandlerLedger.shared.finish(runtime + ":" + id, outcome: success ? "resolved" : "threw", settled: at)
  }
  @JS func handlerStart(_ id: String, _ timeout: Double) throws {
    guard timeout.isFinite && (1...10000).contains(timeout) else { throw NSError(domain: "Expo2Spike", code: 2) }
    let context = appContext!, identity = ObjectIdentifier(self), name = Self._jsName
    HandlerLedger.shared.start(id, runtime: handlerRuntimeID, timeout: timeout) { [weak context] event in
      guard let emitter = context?.moduleRegistry.get(moduleWithName: name) as? Expo2SpikeModule,
        ObjectIdentifier(emitter) == identity else { return }
      emitter.onHandler(event)
    }
  }
  @JavaScriptActor private var racyCount = 0
  @JavaScriptActor private var scheduledCount = 0

  @JS func resetHazard() { racyCount = 0; scheduledCount = 0 }
  @JS func hazardCount(_ scheduled: Bool) -> Int { scheduled ? scheduledCount : racyCount }
  @JS func hazardIncrement(_ scheduled: Bool) async throws -> ThreadSample {
    let runtime = try appContext!.runtime
    try await Task.sleep(for: .milliseconds(5))
    if scheduled {
      return await withCheckedContinuation { continuation in
        runtime.schedule {
          let before = self.scheduledCount
          let sample = captureThread("hazard-scheduled", runtime: runtime)
          self.scheduledCount = before + 1
          continuation.resume(returning: sample)
        }
      }
    }
    // Intentional read/capture/write race: this annotation provides no exclusion.
    let before = racyCount
    let sample = captureThread("hazard-racy", runtime: runtime)
    racyCount = before + 1
    return sample
  }

  @JavaScriptActor private var version = 0
  @JavaScriptActor private var fixtureSize = 2
  @JavaScriptActor private var cached: CachedSnapshot?
  @JavaScriptActor private var freezer: JavaScriptFunction.Ref?
  @JavaScriptActor private var flushScheduled = false

  @Event var onChange: (ChangeEvent) -> Void
  @Event var onBackground: (ThreadSample) -> Void

  @JS func threadSample(_ phase: String) throws -> ThreadSample {
    captureThread(phase, runtime: try appContext!.runtime)
  }

  @JS func defaultProbe() async throws -> [ThreadSample] {
    let runtime = try appContext!.runtime
    let before = captureThread("default-before-await", runtime: runtime)
    try await Task.sleep(for: .milliseconds(20))
    return [before, captureThread("default-after-await", runtime: runtime)]
  }

  // Only Sendable values cross into the SDK actor; the module stays in its binding context.
  @JS func sdkProbe() async throws -> SDKReply {
    let runtime = try appContext!.runtime
    return try await SDKCommands.probe(runtime)
  }

  @JS func emitFromBackground() throws {
    let context = appContext!
    let runtime = try context.runtime
    let name = Self._jsName
    let identity = ObjectIdentifier(self)
    DispatchQueue(label: "expo2.spike.background-event").async {
      guard let emitter = context.moduleRegistry.get(moduleWithName: name) as? Expo2SpikeModule,
        ObjectIdentifier(emitter) == identity else { return }
      emitter.onBackground(captureThread("background-emission", runtime: runtime))
    }
  }

  @JS func configureFixture(_ count: Int) throws {
    guard (2...4096).contains(count) else { throw NSError(domain: "Expo2Spike", code: 1) }
    fixtureSize = count
    try publish(version + 1)
  }

  @JS var snapshot: CachedSnapshot {
    get throws {
      if let cached { return cached }
      let initial = try makeSnapshot(0)
      cached = initial
      return initial
    }
  }

  @JS func publish(_ revision: Int) throws {
    guard revision > version else { return }
    let next = try makeSnapshot(revision)
    cached = next
    version = revision
    guard !flushScheduled else { return }
    flushScheduled = true
    let runtime = try appContext!.runtime
    runtime.schedule { [weak self] in
      guard let self, let cached = self.cached else { return }
      self.flushScheduled = false
      self.onChange(ChangeEvent(snapshot: cached, version: self.version))
    }
  }

  // Two scheduler turns drain the queued flush and its asynchronous event dispatch.
  @JS func drainChanges() async throws {
    let runtime = try appContext!.runtime
    await withCheckedContinuation { continuation in
      runtime.schedule { runtime.schedule { continuation.resume() } }
    }
  }

  @JS func writeResults(_ json: String) async throws {
    try await Task.detached {
      let directory = try FileManager.default.url(
        for: .documentDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
      try Data(json.utf8).write(to: directory.appendingPathComponent("expo2-results.json"), options: .atomic)
    }.value
  }

  @JavaScriptActor private func makeSnapshot(_ revision: Int) throws -> CachedSnapshot {
    let runtime = try appContext!.runtime
    precondition(runtime.isOnJavaScriptThread() && runtime.supportsAsyncScheduling)
    if freezer == nil {
      freezer = try runtime.eval("""
        (function freeze(value) {
          if (value && typeof value === 'object' && !Object.isFrozen(value)) {
            Object.values(value).forEach(freeze);
            Object.freeze(value);
          }
          return value;
        })
        """).asFunction().ref()
    }
    let conversations: [ConversationValue] = (0..<fixtureSize).map { index in
      if index.isMultiple(of: 2) {
        return .ringing(RingingConversation(id: "ring-\(index / 2 + 1)", status: .ringing, caller: "Ada"))
      }
      return .active(ActiveConversation(id: "active-\(index / 2 + 1)", status: .active, connectedAt: 123))
    }
    let record = SnapshotRecord(version: revision, conversations: conversations)
    let encoded = try SnapshotRecord.encode(record, in: runtime).ref()
    let frozen = try freezer!.withUnwrappedValue {
      try $0.call(arguments: JavaScriptValuesBuffer.copying(in: runtime, values: [encoded]))
    } ?? .undefined()
    return CachedSnapshot(value: frozen.ref())
  }
}
