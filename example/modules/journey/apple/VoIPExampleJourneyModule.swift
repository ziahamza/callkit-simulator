import AVFoundation
import ExpoModulesCore
import IosVoipSDK

private enum JourneyConfiguration {
  static let environment = ProcessInfo.processInfo.environment
  static let runID = environment["SDK_SYSTEM_RUN_ID"] ?? environment["SDK_M1_RUN_ID"] ?? environment["SDK_M0_RUN_ID"] ?? UUID().uuidString
  static let m0 = environment["SDK_M0_ACCEPTANCE"] == "1"
  static let m1Case = environment["SDK_M1_CASE"] ?? ""
  static let systemCase = environment["SDK_SYSTEM_CASE"] ?? ""
}

/// Example-only instrumentation. Never records push tokens or arbitrary payloads.
@MainActor
private enum JourneyTrace {
  static let epoch = DispatchTime.now().uptimeNanoseconds
  static let environment = JourneyConfiguration.environment
  static let runID = JourneyConfiguration.runID
  static let m0 = JourneyConfiguration.m0
  static let m1Case = JourneyConfiguration.m1Case
  static var sequence = 0

  static func record(_ event: String, _ fields: [String: Any] = [:], observedAt: Date = Date()) {
    let start = epoch
    sequence += 1
    var row = fields
    row.merge([
      "event": event, "sequence": sequence, "runID": runID,
      "pid": Int(getpid()), "bundleID": Bundle.main.bundleIdentifier ?? "unknown",
      "simulatorID": environment["SIMULATOR_UDID"] ?? "device",
      "t_ms": (DispatchTime.now().uptimeNanoseconds - start) / 1_000_000,
      "wallTimeMS": Int64(observedAt.timeIntervalSince1970 * 1000),
      "main": Thread.isMainThread
    ]) { _, value in value }
    guard let bytes = try? JSONSerialization.data(withJSONObject: row, options: [.sortedKeys]) else { return }
    ProbeTraceWriter.emit(bytes)
  }

  private static let formatter: ISO8601DateFormatter = {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter
  }()
  static func iso(_ date: Date) -> String { formatter.string(from: date) }
}

public final class VoIPExampleAppDelegateSubscriber: ExpoAppDelegateSubscriber {
  public func appDelegateWillBeginInitialization() {
    JourneyTrace.record("app.entry")
    JourneyTrace.record("launch", ["traceVersion": 2,
      "mode": !JourneyConfiguration.systemCase.isEmpty ? "sdk-system" : JourneyTrace.m0 ? "sdk-m0" : "sdk-m1",
      "case": JourneyConfiguration.systemCase.isEmpty ? JourneyTrace.m1Case : JourneyConfiguration.systemCase,
      "bundlePath": Bundle.main.bundleURL.resolvingSymlinksInPath().path])
    if !JourneyConfiguration.systemCase.isEmpty { ExampleJourney.shared.startSystem() }
    if ["cold-launch", "unhandled-expiry"].contains(JourneyTrace.m1Case) {
      ExampleJourney.shared.startColdLaunch()
    }
  }
}

@MainActor
private final class ExampleJourney {
  static let shared = ExampleJourney()
  private var client: ConversationClient?
  private var stateObserver: UUID?
  private var previousStates: [UUID: ConversationStatus] = [:]
  private var coldTask: Task<[String: String], Error>?
  private var coldJoinTask: Task<Void, Never>?
  private var completed = false

  private init() {}

  private func bindClient() throws -> ConversationClient {
    if let client { return client }
    let application = try ExpoMediaSupport.startFromBundle()
    let active = SimulatorSupport.activateIfNeeded()
    JourneyTrace.record("sdk.activation", ["active": active])
    let client = application.client
    self.client = client
    // Legacy M0 marker: bind the SDK-owned manager through its public client.
    // No manager/provider is constructed by this example.
    JourneyTrace.record("sdk.managerCreated", ["owner": "VoIPApplication.start"])
    stateObserver = client.observeState { [weak self] update in self?.recordState(update) }
    return client
  }

  private func recordState(_ update: ConversationStateUpdate) {
    guard !completed else { return }
    let snapshots = update.conversations.map { conversation -> [String: Any] in
      var row: [String: Any] = ["id": conversation.id.uuidString,
        "status": conversation.status.rawValue, "direction": conversation.direction.rawValue,
        "isMuted": conversation.isMuted, "isVideo": conversation.isVideo,
        "hasConnectedAt": conversation.connectedAt != nil]
      if let pending = conversation.pending {
        row["pending"] = ["id": pending.id.uuidString, "kind": pending.kind.rawValue,
          "deadline": JourneyTrace.iso(pending.deadline), "origin": pending.origin.rawValue]
      }
      return row
    }
    JourneyTrace.record("sdk.snapshot", ["revision": update.revision, "conversations": snapshots])
    for conversation in update.conversations where previousStates[conversation.id] != conversation.status {
      let legacy: String
      switch conversation.status {
      case .ringing, .dialing: legacy = "idle"
      case .connecting: legacy = "joining"
      case .connected: legacy = "joined"
      case .held: legacy = "paused"
      case .ending: legacy = "leaving"
      case .ended: legacy = "left"
      }
      JourneyTrace.record("sdk.state", ["conversationID": conversation.id.uuidString,
        "state": legacy, "status": conversation.status.rawValue, "source": "clientProjection"])
      previousStates[conversation.id] = conversation.status
    }
    let live = Set(update.conversations.map(\.id))
    previousStates = previousStates.filter { live.contains($0.key) }
  }

  private func fixturePayload() -> IncomingCallPayload {
    IncomingCallPayload(eventId: "sdk-example-event-" + JourneyTrace.runID,
      serverCallId: "sdk-example-server-" + JourneyTrace.runID,
      caller: .init(id: "sdk-example-caller", displayName: "SDK example", phoneNumber: "+15555550142"),
      hasVideo: false)
  }

  func acceptance() async throws {
    guard JourneyTrace.m0 else { throw JourneyError.wrongMode }
    do {
      let client = try bindClient()
      guard client.snapshots.isEmpty else { throw JourneyError.alreadyCalling }
      client.setConversationHandler(.init(join: { conversation, _ in
        JourneyTrace.record("sdk.action", ["kind": "JoinConversationAction", "conversationID": conversation.id.uuidString])
        try AVAudioSession.sharedInstance().setCategory(.playAndRecord, mode: .voiceChat)
      }, start: { _, _ in throw JourneyError.wrongMode }, end: { conversation, _ in
        JourneyTrace.record("sdk.action", ["kind": "EndConversationAction", "conversationID": conversation.id.uuidString])
      }))
      let id = try await client.reportIncomingConversation(fixturePayload())
      JourneyTrace.record("sdk.incomingReported", ["conversationID": id.uuidString])
      // Acceptance interval only; the SDK still uses the platform action deadline.
      try await Task.sleep(for: .seconds(1))
      guard client.snapshots.first(where: { $0.id == id })?.status == .ringing else { throw JourneyError.notRinging }
      JourneyTrace.record("sdk.ringing", ["conversationID": id.uuidString])
      try await client.joinConversation(id)
      try await waitUntil {
        guard let call = client.snapshots.first(where: { $0.id == id }) else { return false }
        return call.status == .connecting && call.pending == nil && call.connectedAt == nil
      }
      JourneyTrace.record("sdk.joinSettled", ["conversationID": id.uuidString])
      JourneyTrace.record("sdk.backendConnectedRequested", ["conversationID": id.uuidString])
      try await client.reportConversation(id, report: .connected)
      try await waitUntil {
        guard let call = client.snapshots.first(where: { $0.id == id }) else { return false }
        return call.status == .connected && call.connectedAt != nil
      }
      JourneyTrace.record("sdk.joined", ["conversationID": id.uuidString])
      try await client.endConversation(id)
      try await waitUntil { client.snapshots.isEmpty }
      JourneyTrace.record("sdk.ended", ["conversationID": id.uuidString])
      finish(success: true)
    } catch {
      finish(success: false, errorType: String(reflecting: type(of: error)))
      throw error
    }
  }

  func simulatePush(_ payloadJSON: String, mustReport: Bool, label: String) throws -> String {
    guard ["primary", "cold"].contains(label) else { throw JourneyError.invalidTrace }
    _ = try bindClient()
    #if targetEnvironment(simulator)
    guard let data = payloadJSON.data(using: .utf8),
          let payload = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
      throw JourneyError.invalidPayload
    }
    let pushID = UUID().uuidString
    JourneyTrace.record("m1.pushInjected", ["pushID": pushID, "mustReport": mustReport, "label": label])
    CKSimInjectPush(payload, mustReport, pushID)
    return pushID
    #else
    throw JourneyError.simulatorOnly
    #endif
  }

  func startColdLaunch() {
    guard coldTask == nil else { return }
    coldTask = Task { @MainActor in
      let client = try bindClient()
      let bytes = try JSONEncoder().encode(["incomingCall": fixturePayload()])
      guard let json = String(data: bytes, encoding: .utf8) else { throw JourneyError.invalidPayload }
      _ = try simulatePush(json, mustReport: true, label: "cold")
      try await waitUntil { client.snapshots.contains { $0.status == .ringing } }
      guard let conversation = client.snapshots.first else { throw JourneyError.noIncomingConversation }
      let id = conversation.id
      JourneyTrace.record("m1.coldJoinRequested", ["conversationID": id.uuidString])
      coldJoinTask = Task { @MainActor in
        do {
          try await client.joinConversation(id)
          if !completed {
            JourneyTrace.record("m1.coldJoinRequestResult", ["conversationID": id.uuidString, "accepted": true])
          }
        } catch {
          if !completed {
            JourneyTrace.record("m1.coldJoinRequestResult", ["conversationID": id.uuidString, "accepted": false,
              "errorType": String(reflecting: type(of: error))])
          }
        }
      }
      try await waitUntil { client.snapshots.first(where: { $0.id == id })?.pending?.kind == .join }
      guard let pending = client.snapshots.first(where: { $0.id == id })?.pending else { throw JourneyError.noPendingAction }
      let clockBefore = Date()
      #if targetEnvironment(simulator)
      CKSimEmit("m1.coldPendingClock", ["conversationID": id.uuidString, "actionID": pending.id.uuidString])
      #endif
      let observedAt = Date()
      JourneyTrace.record("m1.coldPending", ["conversationID": id.uuidString, "actionID": pending.id.uuidString,
        "deadline": JourneyTrace.iso(pending.deadline), "remainingMS": pending.deadline.timeIntervalSince(observedAt) * 1000,
        "handlerAttached": false, "clockBeforeMS": Int64(clockBefore.timeIntervalSince1970 * 1000)], observedAt: observedAt)
      return ["conversationID": id.uuidString, "actionID": pending.id.uuidString,
        "deadline": JourneyTrace.iso(pending.deadline)]
    }
  }

  func waitForColdStart() async throws -> [String: String] {
    guard let coldTask else { throw JourneyError.wrongMode }
    return try await coldTask.value
  }

  /// Whitelisted test fields prevent accidental payload/token logging.
  func startSystem() {
    do { _ = try bindClient() }
    catch { finish(success: false, errorType: String(reflecting: type(of: error))) }
  }

  // Example-only backend/handler barriers; external cksim controls actual system actions and pushes.
  func nextSystemCommand() async throws -> [String: String] {
    guard ["answer-end", "decline", "hold-accept", "end-accept", "mute-echo", "provider-reset", "push-launch"]
      .contains(JourneyConfiguration.systemCase) else { throw JourneyError.wrongMode }
    let path = FileManager.default.temporaryDirectory.appendingPathComponent("sdk-system-control-\(JourneyTrace.runID).json")
    let deadline = ContinuousClock.now.advanced(by: .seconds(90))
    while !FileManager.default.fileExists(atPath: path.path) {
      guard ContinuousClock.now < deadline else { throw JourneyError.stateDeadline }
      try await Task.sleep(for: .milliseconds(25))
    }
    let bytes = try Data(contentsOf: path)
    try FileManager.default.removeItem(at: path)
    guard bytes.count <= 1024, let value = try JSONSerialization.jsonObject(with: bytes) as? [String: Any],
      Set(value.keys).isSubset(of: ["id", "runID", "case", "pid", "operation", "conversationID"]),
      value["runID"] as? String == JourneyTrace.runID, value["case"] as? String == JourneyConfiguration.systemCase,
      value["pid"] as? Int == Int(getpid()), let id = value["id"] as? String, UUID(uuidString: id) != nil,
      let operation = value["operation"] as? String,
      ["attach-handler", "backend-connected", "app-mute", "release-mute", "finish"].contains(operation) else {
      throw JourneyError.invalidTrace
    }
    var command = ["id": id, "operation": operation]
    if let conversation = value["conversationID"] as? String {
      guard UUID(uuidString: conversation) != nil else { throw JourneyError.invalidTrace }
      command["conversationID"] = conversation
    }
    JourneyTrace.record("system.commandReceived", command)
    return command
  }

  func trace(_ event: String, fieldsJSON: String) throws {
    guard !completed else { return }
    let events: Set<String> = ["m1.jsReady", "m1.subscriptionsStarting", "m1.handlerAttached", "m1.handler", "m1.cancelled",
      "m1.assertion", "m1.hookConversations", "m1.hookToken", "m1.event", "m1.expiryObserved", "m1.backendConnectedRequested",
      "system.ready", "system.handler", "system.handlerAttached", "system.hook", "system.event",
      "system.backendConnected", "system.appMuteRequested", "system.appMuteCompleted", "system.commandCompleted"]
    let fields: Set<String> = ["label", "passed", "conversationID", "kind", "origin", "deadline",
      "held", "muted", "attached", "available", "tokenType", "name", "count", "status",
      "pending", "hasConnectedAt", "reason", "id", "operation", "digits"]
    guard events.contains(event), let bytes = fieldsJSON.data(using: .utf8),
          let value = try JSONSerialization.jsonObject(with: bytes) as? [String: Any],
          Set(value.keys).isSubset(of: fields),
          value.values.allSatisfy({ $0 is String || $0 is NSNumber || $0 is NSNull }) else {
      throw JourneyError.invalidTrace
    }
    if event == "system.ready" {
      let before = Date()
      #if targetEnvironment(simulator)
      CKSimEmit("system.clock", [:])
      #endif
      var sample = value
      sample["clockBeforeMS"] = Int64(before.timeIntervalSince1970 * 1000)
      JourneyTrace.record(event, sample)
    } else {
      #if targetEnvironment(simulator)
      if event == "system.handler" {
        var marker = value
        marker["traceSequence"] = JourneyTrace.sequence + 1
        CKSimEmit("system.handlerEntered", marker)
      }
      #endif
      JourneyTrace.record(event, value)
    }
  }

  func finish(success: Bool, errorType: String? = nil) {
    guard !completed else { return }
    completed = true
    var fields: [String: Any] = ["success": success]
    if let errorType { fields["errorType"] = errorType }
    JourneyTrace.record("sdk.result", fields)
    JourneyTrace.record("done")
  }

  private func waitUntil(_ predicate: @MainActor () -> Bool) async throws {
    let deadline = ContinuousClock.now.advanced(by: .seconds(8))
    while !predicate() {
      guard ContinuousClock.now < deadline else { throw JourneyError.stateDeadline }
      try await Task.sleep(for: .milliseconds(25))
    }
  }
}

private enum JourneyError: Error {
  case alreadyCalling, noIncomingConversation, noPendingAction, notRinging, stateDeadline
  case wrongMode, simulatorOnly, invalidPayload, invalidTrace
}

/// Excluded from the distributed SDK. Synthetic push injection is simulator-only.
public final class VoIPExampleJourneyModule: Module {
  public func definition() -> ModuleDefinition {
    Name("VoIPExampleJourney")
    Constants(["acceptanceEnabled": JourneyConfiguration.m0, "m1Case": JourneyConfiguration.m1Case,
      "systemCase": JourneyConfiguration.systemCase, "runID": JourneyConfiguration.runID])
    AsyncFunction("nextSystemCommand") { (promise: Promise) in
      Task { @MainActor in
        do { promise.resolve(try await ExampleJourney.shared.nextSystemCommand()) }
        catch { promise.reject("journey", String(reflecting: type(of: error))) }
      }
    }
    AsyncFunction("acceptance") { (promise: Promise) in
      Task { @MainActor in
        do { try await ExampleJourney.shared.acceptance(); promise.resolve() }
        catch { promise.reject("journey", String(reflecting: type(of: error))) }
      }
    }
    AsyncFunction("simulatePush") { (payloadJSON: String, mustReport: Bool, label: String, promise: Promise) in
      Task { @MainActor in
        do { promise.resolve(try ExampleJourney.shared.simulatePush(payloadJSON, mustReport: mustReport, label: label)) }
        catch { promise.reject("journey", String(reflecting: type(of: error))) }
      }
    }
    AsyncFunction("waitForColdStart") { (promise: Promise) in
      Task { @MainActor in
        do { promise.resolve(try await ExampleJourney.shared.waitForColdStart()) }
        catch { promise.reject("journey", String(reflecting: type(of: error))) }
      }
    }
    AsyncFunction("trace") { (event: String, fieldsJSON: String, promise: Promise) in
      Task { @MainActor in
        do { try ExampleJourney.shared.trace(event, fieldsJSON: fieldsJSON); promise.resolve() }
        catch { promise.reject("journey", String(reflecting: type(of: error))) }
      }
    }
    AsyncFunction("finish") { (success: Bool, promise: Promise) in
      Task { @MainActor in ExampleJourney.shared.finish(success: success); promise.resolve() }
    }
  }
}
