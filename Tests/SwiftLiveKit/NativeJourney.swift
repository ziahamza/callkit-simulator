import AVFAudio
import CryptoKit
import Foundation
import LiveKit
import LiveKitConsumer
import SwiftUI
import VoIPSDK
import os

private enum JourneyError: Error { case failed(String) }

/// The ADM public running scalar can describe its device engine in manual mode.
/// Keep only a weak identity; callback methods never query AVAudioEngine state.
private final class ManualEngineObservation: AudioEngineObserver {
  static let shared = ManualEngineObservation()
  private struct State {
    weak var engine: AVAudioEngine?
    var next: (any AudioEngineObserver)?
  }
  private let state = OSAllocatedUnfairLock(initialState: State())
  var next: (any AudioEngineObserver)? {
    get { state.withLock { $0.next } }
    set { state.withLock { $0.next = newValue } }
  }
  func engineDidCreate(_ engine: AVAudioEngine) -> Int {
    let following = state.withLock { value in value.engine = engine; return value.next }
    return following?.engineDidCreate(engine) ?? 0
  }
  /// Called only inside the existing AudioSessionQueue operation, after the
  /// synchronous ADM setter has completed. No engine object leaves the lock.
  func read() -> (present: Bool, manual: Bool, running: Bool) {
    state.withLock { value in
      guard let engine = value.engine else { return (false, false, false) }
      return (true, engine.isInManualRenderingMode, engine.isRunning)
    }
  }
}

private struct EngineState: Sendable {
  let automatic, manual, input, output, running, publicRunning, enginePresent, engineManual: Bool
  var fields: [String: Bool] {
    ["automatic": automatic, "manual": manual, "input": input, "output": output, "running": running,
      "publicRunning": publicRunning, "enginePresent": enginePresent, "engineManual": engineManual]
  }
  static func read() async -> EngineState {
    await AudioSessionQueue.shared.perform {
      let manager = AudioManager.shared
      let availability = manager.engineAvailability
      let actual = ManualEngineObservation.shared.read()
      return EngineState(automatic: manager.audioSession.isAutomaticConfigurationEnabled,
        manual: manager.isManualRenderingMode, input: availability.isInputAvailable,
        output: availability.isOutputAvailable, running: actual.running,
        publicRunning: manager.isEngineRunning, enginePresent: actual.present, engineManual: actual.manual)
    }
  }
}

@MainActor private enum Trace {
  static let start = ProcessInfo.processInfo.systemUptime
  static var sequence = 0
  static func log(_ event: String, _ fields: [String: Any] = [:]) {
    sequence += 1
    let env = ProcessInfo.processInfo.environment
    var row = fields
    row.merge(["event": event, "sequence": sequence, "main": Thread.isMainThread,
      "t_ms": Int((ProcessInfo.processInfo.systemUptime - start) * 1000),
      "wallTimeMS": Int(Date().timeIntervalSince1970 * 1000), "pid": Int(getpid()),
      "runID": env["CKSIM_RUN_ID"] ?? "", "simulatorID": env["SIMULATOR_UDID"] ?? "",
      "bundleID": Bundle.main.bundleIdentifier ?? ""]) { _, actual in actual }
    if let bytes = try? JSONSerialization.data(withJSONObject: row, options: [.sortedKeys]) {
      FileHandle.standardError.write(Data("PROBE ".utf8) + bytes + Data("\n".utf8))
    }
  }
}

/// Observes real SDK delegate forwarding; this fixture never fabricates activation.
@MainActor private final class ObservedAdapter: MediaAdapter {
  let adapter = LiveKitMediaAdapter()
  private(set) var authorized = false
  private(set) var activations = 0
  private(set) var deactivations = 0
  private(set) var preparations = 0
  func setSystemAudioAuthorization(_ active: Bool) {
    authorized = active; adapter.setSystemAudioAuthorization(active)
    Trace.log("livekit.authorization", ["active": active])
  }
  func prepare() async throws {
    try await adapter.prepare(); preparations += 1
    Trace.log("livekit.prepared", (await EngineState.read()).fields)
  }
  func didActivate(_ session: AVAudioSession) async throws {
    try await adapter.didActivate(session); activations += 1
    Trace.log("livekit.lckDelegate", ["active": true])
  }
  func didDeactivate(_ session: AVAudioSession) async throws {
    try await adapter.didDeactivate(session); deactivations += 1
    Trace.log("livekit.lckDelegate", ["active": false])
  }
}

@MainActor private final class NativeJourney {
  static let shared = NativeJourney()
  private var started = false
  private var demandStarted = false
  private var events: [ConversationEventContext] = []

  private func expect(_ condition: Bool, _ message: String) throws {
    if !condition { throw JourneyError.failed(message) }
  }
  private func wait(_ condition: () -> Bool) async throws {
    let stop = ContinuousClock.now + .seconds(8)
    while !condition() {
      try expect(ContinuousClock.now < stop, "Call state did not settle")
      try await Task.sleep(for: .milliseconds(20))
    }
  }
  private func engine(_ phase: String, active: Bool) async throws {
    let stop = ContinuousClock.now + .seconds(8)
    while true {
      let value = await EngineState.read()
      if !value.automatic && value.manual && value.input == active && value.output == active && value.running == active &&
          (!active || value.enginePresent) && value.enginePresent == value.engineManual {
        Trace.log("livekit.engine", ["phase": phase, "observation": "weak-manual-AVAudioEngine", "state": value.fields]); return
      }
      if ContinuousClock.now >= stop {
        Trace.log("livekit.engineTimeout", ["phase": phase, "observation": "weak-manual-AVAudioEngine", "state": value.fields])
        throw JourneyError.failed("Real LiveKit engine did not reach " + phase)
      }
      try await Task.sleep(for: .milliseconds(20))
    }
  }
  private func startDemand(_ context: ConversationActionContext) async throws {
    try context.signal.throwIfCancelled()
    try await AudioSessionQueue.shared.perform {
      let manager = AudioManager.shared
      let availability = manager.engineAvailability
      guard !manager.audioSession.isAutomaticConfigurationEnabled,
            !availability.isInputAvailable, !availability.isOutputAvailable, !manager.isEngineRunning,
            !ManualEngineObservation.shared.read().running else {
        throw JourneyError.failed("Capture was not prepared and gated")
      }
      try manager.setManualRenderingMode(true)
      guard manager.isManualRenderingMode else { throw JourneyError.failed("No-device mode was not enabled") }
      try AudioSessionQueue.checkCancellation()
      try manager.startLocalRecording()
    }
    demandStarted = true
    try context.signal.throwIfCancelled()
    try await engine("pending-demand", active: false)
  }
  private func stopDemand() async throws {
    guard demandStarted else { return }
    try await AudioSessionQueue.shared.perform { try AudioManager.shared.stopLocalRecording() }
    demandStarted = false
  }

  private func waitForFault() async throws {
    Trace.log("livekit.readyForFault")
    let root = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
      .appendingPathComponent(".callkit-simulator")
      .appendingPathComponent(ProcessInfo.processInfo.environment["CKSIM_RUN_ID"] ?? "")
      .appendingPathComponent("responses")
    // Before any call/action/watchdog: allow the bounded 60s external notifier
    // plus its 10s ACK wait to finish on a cold simulator host.
    let stop = ContinuousClock.now + .seconds(75)
    while ContinuousClock.now < stop {
      for path in (try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? [] {
        guard path.pathExtension == "json", let data = try? Data(contentsOf: path),
              let row = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              row["success"] as? Bool == true, (row["result"] as? [String: Any])?["configured"] as? Bool == true,
              row["pid"] as? Int == Int(getpid()), row["runID"] as? String == ProcessInfo.processInfo.environment["CKSIM_RUN_ID"],
              row["bundleID"] as? String == Bundle.main.bundleIdentifier,
              let id = row["id"] as? String, UUID(uuidString: id) != nil else { continue }
        Trace.log("livekit.faultConfigured", ["commandID": id]); return
      }
      try await Task.sleep(for: .milliseconds(20))
    }
    throw JourneyError.failed("External stale-audio fault was not configured")
  }

  private func journey(_ mode: String) async throws {
    let stale = mode == "stale-audio"
    await AudioSessionQueue.shared.perform {
      let manager = AudioManager.shared
      // Preserve the exact default session/mixer objects and their order.
      manager.set(engineObservers: [manager.audioSession, manager.mixer, ManualEngineObservation.shared])
    }
    let adapter = ObservedAdapter()
    let policy: AudioActivationPolicy
    if stale { policy = try AudioActivationPolicy(activationTimeout: 1, recoveryTimeout: 1) }
    else { policy = .standard }
    let application = try VoIPApplication.start(mediaAdapter: adapter, audioPolicy: policy)
    let client = application.client
    Trace.log("livekit.owner", ["identity": String(describing: ObjectIdentifier(client)),
      "activationTimeout": policy.activationTimeout, "recoveryTimeout": policy.recoveryTimeout])
    let eventToken = client.observeEvents { [self] event in
      events.append(event)
      Trace.log("livekit.event", ["type": event.type.rawValue, "conversationID": event.conversationID?.uuidString ?? ""])
    }
    defer { client.removeEventObserver(eventToken) }
    client.setConversationHandler(.init(join: { [self] _, context in try await startDemand(context) },
      start: { _, _ in throw JourneyError.failed("Unexpected start action") },
      end: { [self] _, context in try context.signal.throwIfCancelled(); try await stopDemand() }, hold: { _, _ in }))
    if stale { try await waitForFault() }
    let id = try await client.reportIncomingConversation(.init(eventId: UUID().uuidString,
      serverCallId: UUID().uuidString, caller: .init(id: "swift-livekit-fixture")))
    Trace.log("livekit.incoming", ["conversationID": id.uuidString])
    try await client.joinConversation(id)
    if stale {
      try await wait { client.snapshots.isEmpty && events.contains { $0.type == .audioActivationFailed } }
      try expect(adapter.activations == 0 && !adapter.authorized && adapter.preparations == 2,
        "Stale activation must remain gated and recover once")
      try expect(events.filter { $0.type == .audioActivationMissing || $0.type == .audioActivationFailed }
        .map(\.conversationID) == [id, id], "Watchdog events must belong to the failed call")
      try await engine("stale-ended", active: false)
      try await stopDemand()
    } else {
      try await wait { adapter.authorized && adapter.activations == 1 }
      try await engine("joined", active: true)
      try await client.reportConversation(id, report: .connected)
      try await client.setHeld(id, true)
      try await wait { !adapter.authorized && adapter.deactivations == 1 }
      try await engine("held", active: false)
      try await client.setHeld(id, false)
      try await wait { adapter.authorized && adapter.activations == 2 }
      try await engine("resumed", active: true)
      try await client.endConversation(id)
      try await wait { client.snapshots.isEmpty && !adapter.authorized && adapter.deactivations == 2 }
      try await engine("ended", active: false)
    }
    Trace.log("livekit.completed", ["conversationID": id.uuidString, "empty": client.snapshots.isEmpty,
      "authorized": adapter.authorized, "demandStopped": !demandStarted, "preparations": adapter.preparations])
  }
  func run() async {
    guard !started else { return }; started = true
    let mode = ProcessInfo.processInfo.environment["SDK_SWIFT_LIVEKIT_CASE"] ?? "healthy"
    let executable = (try? Data(contentsOf: Bundle.main.executableURL!)) ?? Data()
    Trace.log("launch", ["traceVersion": 2, "mode": "sdk-swift-livekit", "case": mode,
      "rendering": "manual-no-device", "simulatorOS": UIDevice.current.systemVersion,
      "dataContainer": URL(fileURLWithPath: NSHomeDirectory()).resolvingSymlinksInPath().path,
      "runtimeInjectionPresent": (ProcessInfo.processInfo.environment["DYLD_INSERT_LIBRARIES"] ?? "")
        .split(separator: ":").contains { ["CKSimRuntime.dylib", "CKSimLoader.dylib"].contains(URL(fileURLWithPath: String($0)).lastPathComponent) },
      "executableSHA256": SHA256.hash(data: executable).map { String(format: "%02x", $0) }.joined()])
    do {
      try expect(["healthy", "stale-audio"].contains(mode), "Unknown native case")
      try await journey(mode)
      Trace.log("contract.result", ["success": true])
    } catch {
      try? await stopDemand()
      Trace.log("contract.error", ["error": String(describing: error)])
      Trace.log("contract.result", ["success": false])
    }
    Trace.log("done")
  }
}

@main private struct SDKSwiftLiveKitApp: App {
  init() { Trace.log("app.entry") }
  var body: some Scene {
    WindowGroup { Text("Silent Swift LiveKit contract").task { await NativeJourney.shared.run() } }
  }
}
