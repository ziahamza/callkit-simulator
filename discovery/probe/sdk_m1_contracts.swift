import CallKit
import AVFAudio
import CryptoKit
import Foundation
@preconcurrency import LiveCommunicationKit
import SwiftUI

@MainActor
private enum ContractTrace {
  static let start = ProcessInfo.processInfo.systemUptime
  static var sequence = 0
  #if DEBUG && targetEnvironment(simulator)
  static var platformDeadlines: [String: Int] = [:]
  #endif
  static func log(_ event: String, _ fields: [String: Any] = [:]) {
    sequence += 1
    let env = ProcessInfo.processInfo.environment
    var row = fields
    row.merge(["event": event, "sequence": sequence, "main": Thread.isMainThread,
      "t_ms": Int((ProcessInfo.processInfo.systemUptime - start) * 1000),
      "wallTimeMS": Int(Date().timeIntervalSince1970 * 1000), "pid": Int(getpid()),
      "runID": env["CKSIM_RUN_ID"] ?? "", "simulatorID": env["SIMULATOR_UDID"] ?? "",
      "bundleID": Bundle.main.bundleIdentifier ?? ""]) { _, actual in actual }
    if let data = try? JSONSerialization.data(withJSONObject: row, options: [.sortedKeys]) {
      FileHandle.standardError.write(Data("PROBE ".utf8) + data + Data("\n".utf8))
    }
  }
}

@MainActor
private final class ReentrantMediaAdapter: MediaAdapter {
  private(set) var observedClient: ConversationClient?
  private(set) var authorizationCalls = 0
  func setSystemAudioAuthorization(_ active: Bool) {
    authorizationCalls += 1
    observedClient = try? VoIPApplication.start(mediaAdapter: self).client
  }
  func prepare() async throws {}
  func didActivate(_ session: AVAudioSession) async throws {}
  func didDeactivate(_ session: AVAudioSession) async throws {}
}

@MainActor
private final class LifecycleContractAdapter: MediaAdapter {
  private(set) var authorized = false
  private(set) var activations = 0
  private(set) var deactivations = 0
  func setSystemAudioAuthorization(_ active: Bool) {
    authorized = active
    ContractTrace.log("lifecycle.adapterAuthorization", ["authorized": active,
      "phase": LifecycleContract.phase])
  }
  func prepare() async throws {}
  func didActivate(_ session: AVAudioSession) async throws {
    activations += 1
    ContractTrace.log("lifecycle.lckDelegate", ["active": true, "count": activations,
      "phase": LifecycleContract.phase])
  }
  func didDeactivate(_ session: AVAudioSession) async throws {
    deactivations += 1
    ContractTrace.log("lifecycle.lckDelegate", ["active": false, "count": deactivations,
      "phase": LifecycleContract.phase])
  }
}

@MainActor
private enum LifecycleContract {
  static var phase = "initial"
  static var observed: [(String, String)] = []
  static var consumed = 0
  static func kind(_ value: VSAudioSessionLifecycleValue) -> String {
    switch value.kind {
    case .active: "active"
    case .inactive: "inactive"
    case .resumptionRecommended: "recommended"
    case .mediaServicesReset: "reset"
    @unknown default: "unknown"
    }
  }
  static func wait(_ condition: () -> Bool) async -> Bool {
    let stop = Date().addingTimeInterval(5)
    while !condition(), Date() < stop { try? await Task.sleep(for: .milliseconds(20)) }
    return condition()
  }
  static func run() async throws -> Bool {
    let modern: Bool
    if #available(iOS 27.0, *) { modern = true } else { modern = false }
    let path = modern ? "modern" : "legacy"
    ContractTrace.log("lifecycle.path", ["path": path,
      "evidence": modern ? "native-lck-audio-authority-v1" : "native-synthetic-legacy-wiring"])
    #if DEBUG && targetEnvironment(simulator)
    VSAudioSessionSetLifecycleDiagnosticHandler { name, contextClass, main, value in
      MainActor.assumeIsolated {
        let label = kind(value)
        observed.append((phase, label))
        ContractTrace.log("lifecycle.observed", ["notificationName": name, "contextClass": contextClass,
          "observerMain": main, "kind": label, "hasSource": value.hasSource, "source": value.source,
          "hasInterruptionReason": value.hasInterruptionReason, "interruptionReason": value.interruptionReason,
          "shouldResume": value.shouldResume, "phase": phase])
      }
    }
    AudioLifecycleDiagnostics.didConsume = { event, before, after in
      consumed += 1
      let label: String
      var source: Int?
      var reason: UInt?
      var shouldResume = false
      switch event {
      case .active: label = "active"
      case .inactive(let value, let interruptionReason):
        label = "inactive"; source = value; reason = interruptionReason
      case .resumptionRecommended(let value): label = "recommended"; shouldResume = value
      case .mediaServicesReset: label = "reset"
      }
      ContractTrace.log("lifecycle.consumed", ["kind": label, "authorizedBefore": before,
        "authorizedAfter": after, "hasSource": source != nil, "source": source ?? 0,
        "hasInterruptionReason": reason != nil, "interruptionReason": reason ?? 0,
        "shouldResume": shouldResume, "phase": phase])
    }
    defer {
      VSAudioSessionSetLifecycleDiagnosticHandler(nil)
      AudioLifecycleDiagnostics.didConsume = nil
    }
    #endif
    let adapter = LifecycleContractAdapter()
    let application = try VoIPApplication.start(mediaAdapter: adapter)
    let client = application.client
    let repeated = try VoIPApplication.start(mediaAdapter: adapter)
    ContractTrace.log("contract.owner", ["identity": String(describing: ObjectIdentifier(client)),
      "sharedIdentity": String(describing: ObjectIdentifier(repeated.client)), "warnings": 0])
    client.setConversationHandler(.init(
      join: { _, _ in ContractTrace.log("lifecycle.handler", ["kind": "join", "phase": phase]) },
      start: { _, _ in ContractTrace.log("lifecycle.handler", ["kind": "start", "phase": phase]) },
      end: { _, _ in ContractTrace.log("lifecycle.handler", ["kind": "end", "phase": phase]) },
      hold: { _, _ in ContractTrace.log("lifecycle.handler", ["kind": "hold", "phase": phase]) }))
    let id = try await client.reportIncomingConversation(.init(eventId: UUID().uuidString,
      serverCallId: UUID().uuidString, caller: .init(id: "native-lifecycle-fixture")))
    phase = "answer"
    try await client.joinConversation(id)
    guard await wait({ adapter.authorized && adapter.activations == 1 }) else { return false }
    if modern {
      guard await wait({
        guard let call = client.snapshots.first(where: { $0.id == id }) else { return false }
        return call.status == .connecting && call.pending == nil && call.connectedAt == nil
      }) else { return false }
      ContractTrace.log("lifecycle.joinSettled", ["conversationID": id.uuidString,
        "pendingCleared": true, "connected": false, "phase": phase])
      ContractTrace.log("lifecycle.backendConnectedRequested", ["conversationID": id.uuidString, "phase": phase])
      try await client.reportConversation(id, report: .connected)
      guard await wait({
        guard let call = client.snapshots.first(where: { $0.id == id }) else { return false }
        return call.status == .connected && call.connectedAt != nil
      }), let connectedAt = client.snapshots.first(where: { $0.id == id })?.connectedAt else { return false }
      ContractTrace.log("lifecycle.backendConnected", ["conversationID": id.uuidString,
        "accepted": true, "connectedAtMS": Int(connectedAt.timeIntervalSince1970 * 1000), "phase": phase])
    } else {
      phase = "legacy-began"
      ContractTrace.log("lifecycle.syntheticPost", ["kind": "inactive", "phase": phase])
      NotificationCenter.default.post(name: AVAudioSession.interruptionNotification, object: AVAudioSession.sharedInstance(),
        userInfo: [AVAudioSessionInterruptionTypeKey: AVAudioSession.InterruptionType.began.rawValue,
          AVAudioSessionInterruptionReasonKey: AVAudioSession.InterruptionReason.default.rawValue])
      guard await wait({ !adapter.authorized }) else { return false }
      phase = "legacy-ended"
      ContractTrace.log("lifecycle.syntheticPost", ["kind": "recommended", "phase": phase])
      NotificationCenter.default.post(name: AVAudioSession.interruptionNotification, object: AVAudioSession.sharedInstance(),
        userInfo: [AVAudioSessionInterruptionTypeKey: AVAudioSession.InterruptionType.ended.rawValue,
          AVAudioSessionInterruptionOptionKey: AVAudioSession.InterruptionOptions.shouldResume.rawValue])
      guard await wait({ observed.contains { $0 == ("legacy-ended", "recommended") } }),
        !adapter.authorized else { return false }
    }
    phase = "hold"
    try await client.setHeld(id, true)
    guard await wait({ !adapter.authorized && adapter.deactivations == 1 }) else { return false }
    phase = "resume"
    try await client.setHeld(id, false)
    guard await wait({ adapter.authorized && adapter.activations == 2 }) else { return false }
    phase = "end"
    try await client.endConversation(id)
    guard await wait({ !adapter.authorized && adapter.deactivations == 2 && client.snapshots.isEmpty }) else { return false }
    if modern {
      ContractTrace.log("lifecycle.noConversations", ["count": client.snapshots.count, "phase": phase])
    }
    ContractTrace.log("lifecycle.completed", ["authorized": adapter.authorized,
      "activations": adapter.activations, "deactivations": adapter.deactivations,
      "observedCount": observed.count, "consumedCount": consumed, "phase": phase])
    // Authority is measured by genuine LCK callbacks above. Passive AV session
    // notifications may be absent on this OS; legacy wiring still requires them.
    return (modern || !observed.isEmpty) && observed.count == consumed
  }
}

@MainActor
private final class RouteContractAdapter: MediaAdapter {
  private(set) var authorized = false
  private(set) var activations = 0
  private(set) var deactivations = 0
  func setSystemAudioAuthorization(_ active: Bool) { authorized = active }
  func prepare() async throws {}
  func didActivate(_ session: AVAudioSession) async throws {
    activations += 1; ContractTrace.log("routes.lckDelegate", ["active": true])
  }
  func didDeactivate(_ session: AVAudioSession) async throws {
    deactivations += 1; ContractTrace.log("routes.lckDelegate", ["active": false])
  }
}

@MainActor
private enum RouteContract {
  static func capture(_ client: ConversationClient, phase: String) async throws -> Bool {
    let stop = Date().addingTimeInterval(5)
    repeat {
      let native = await AudioSessionQueue.shared.perform { () -> [[AudioRoute]] in
        let session = AVAudioSession.sharedInstance()
        func route(_ port: AVAudioSessionPortDescription) -> AudioRoute {
          AudioRoute(id: port.uid, name: port.portName, type: port.portType.rawValue)
        }
        return [(session.availableInputs ?? []).map(route), session.currentRoute.outputs.map(route)]
      }
      var expected: [AudioRoute] = []
      for port in native[0] where !expected.contains(where: { $0.id == "input:" + port.id }) {
        expected.append(.init(id: "input:" + port.id, name: port.name, type: port.type))
      }
      expected.append(.init(id: "speaker", name: "Speaker", type: "Speaker"))
      let current = native[1].first.map { AudioRoute(id: "output:" + $0.id, name: $0.name, type: $0.type) }
      if let current { expected.append(current) }
      let observed = client.audioRoutes
      // Notifications use a debounce. Wait for a coherent OS/cache checkpoint
      // rather than fail on two independently valid reads during a route change.
      if observed.routes == expected && observed.current == current {
        let snapshot = try JSONSerialization.jsonObject(with: JSONEncoder().encode(observed))
        func values(_ ports: [AudioRoute]) -> [[String: String]] {
          ports.map { ["uid": $0.id, "name": $0.name, "type": $0.type] }
        }
        ContractTrace.log("routes.observed", ["phase": phase, "snapshot": snapshot,
          "inputs": values(native[0]), "outputs": values(native[1])])
        return true
      }
      try await Task.sleep(for: .milliseconds(20))
    } while Date() < stop
    return false
  }
  static func run() async throws -> Bool {
    let adapter = RouteContractAdapter()
    let application = try VoIPApplication.start(mediaAdapter: adapter)
    let client = application.client
    let repeated = try VoIPApplication.start(mediaAdapter: adapter)
    ContractTrace.log("contract.owner", ["identity": String(describing: ObjectIdentifier(client)),
      "sharedIdentity": String(describing: ObjectIdentifier(repeated.client)), "warnings": 0])
    client.setConversationHandler(.init(join: { _, _ in }, start: { _, _ in }, end: { _, _ in }))
    let id = try await client.reportIncomingConversation(.init(eventId: UUID().uuidString,
      serverCallId: UUID().uuidString, caller: .init(id: "native-route-fixture")))
    try await client.joinConversation(id)
    guard await LifecycleContract.wait({ adapter.authorized && adapter.activations == 1 && client.audioRoutes.revision > 0 }) else { return false }
    ContractTrace.log("routes.authority", ["conversationID": id.uuidString, "authorized": true])
    do {
      try await client.selectAudioRoute("speaker")
      ContractTrace.log("routes.command", ["id": "speaker", "succeeded": true])
    } catch let error as AudioRouteError { throw error }
    catch {
      let native = error as NSError
      ContractTrace.log("routes.command", ["id": "speaker", "succeeded": false,
        "errorDomain": native.domain, "errorCode": native.code])
    }
    guard try await capture(client, phase: "after") else { return false }
    let before = client.audioRoutes
    if let current = before.current { try await client.selectAudioRoute(current.id) }
    ContractTrace.log("routes.currentNoOp", ["available": before.current != nil,
      "unchanged": client.audioRoutes == before])
    guard try await capture(client, phase: "unchanged") else { return false }
    try await client.endConversation(id)
    guard await LifecycleContract.wait({ !adapter.authorized && adapter.deactivations == 1 && client.snapshots.isEmpty }) else { return false }
    ContractTrace.log("routes.ended", ["conversationID": id.uuidString, "empty": true, "authorized": false])
    return true
  }
}

@MainActor
private final class SDKM1ContractsProbe {
  private var warnings = 0
  private var phase = "owned"
  private var extraProvider: CXProvider?
  private var extraManager: ConversationManager?

  private func wait(seconds: Double, until condition: () -> Bool) async -> Bool {
    let stop = Date().addingTimeInterval(max(0, seconds))
    while !condition(), Date() < stop { try? await Task.sleep(for: .milliseconds(20)) }
    return condition()
  }

  func run() async {
    #if DEBUG && targetEnvironment(simulator)
    // This fixture owns synchronous trace I/O; its overhead is part of the
    // measured Debug timeline. The SDK sink only passes scalar observations.
    ConversationActionCoordinator.diagnosticSink = { value in
      ContractTrace.platformDeadlines[value.actionID] = value.platformDeadlineMS
      ContractTrace.log("contract.actionDiagnostic", ["phase": value.phase,
        "actionID": value.actionID, "sdkWallTimeMS": value.wallTimeMS,
        "effectiveDeadlineMS": value.effectiveDeadlineMS,
        "platformDeadlineMS": value.platformDeadlineMS,
        "entryPending": value.entryPending, "platformPending": value.platformPending])
    }
    defer { ConversationActionCoordinator.diagnosticSink = nil }
    #endif
    let mode = ProcessInfo.processInfo.environment["SDK_M1_CONTRACT_CASE"] ?? ""
    let executable = (try? Data(contentsOf: Bundle.main.executableURL!)) ?? Data()
    let inserted = (ProcessInfo.processInfo.environment["DYLD_INSERT_LIBRARIES"] ?? "").split(separator: ":")
    let injected = inserted.contains { ["CKSimRuntime.dylib", "CKSimLoader.dylib"].contains(URL(fileURLWithPath: String($0)).lastPathComponent) }
    ContractTrace.log("launch", ["traceVersion": 2, "mode": "sdk-m1-contracts", "case": mode,
      "executableSHA256": SHA256.hash(data: executable).map { String(format: "%02x", $0) }.joined(),
      "runtimeInjectionPresent": injected, "simulatorOS": UIDevice.current.systemVersion])
    let active = SimulatorSupport.activateIfNeeded()
    ContractTrace.log("contract.activation", ["active": active])
    guard active else { finish(false); return }
    ProviderCreationMonitor.install { [self] message in
      warnings += 1
      ContractTrace.log("contract.warning", ["message": message, "phase": phase, "count": warnings])
    }
    do {
      if mode == "audio-lifecycle" { finish(try await LifecycleContract.run()); return }
      if mode == "audio-routes" { finish(try await RouteContract.run()); return }
      if mode == "media-reentrancy" {
        let adapter = ReentrantMediaAdapter()
        let application = try VoIPApplication.start(mediaAdapter: adapter)
        let client = application.client
        let repeated = try VoIPApplication.start(mediaAdapter: adapter)
        let sameOwner = adapter.observedClient === client && application === repeated
        ContractTrace.log("contract.owner", ["identity": String(describing: ObjectIdentifier(client)),
          "sharedIdentity": String(describing: ObjectIdentifier(repeated.client)), "warnings": warnings])
        ContractTrace.log("contract.mediaReentrancy", ["authorizationCalls": adapter.authorizationCalls,
          "sameOwner": sameOwner, "warnings": warnings])
        finish(sameOwner && adapter.authorizationCalls == 1 && warnings == 0)
        return
      }
      if mode == "duplicate-wrapper" {
        let configuration = VoIPApplication.Configuration(client: .init(includesConversationInRecents: false))
        let application = try VoIPApplication.start(configuration: configuration)
        let repeated = try VoIPApplication.start(configuration: configuration)
        ContractTrace.log("contract.owner", ["identity": String(describing: ObjectIdentifier(application.client)),
          "sharedIdentity": String(describing: ObjectIdentifier(repeated.client)), "warnings": warnings,
          "entryPoint": "VoIPApplication.start"])
        try await duplicateContract(application.client)
        return
      }
      if mode == "unhandled-accept" || mode == "unhandled-fail" {
        let policy: UnhandledActionPolicy = mode == "unhandled-accept" ? .accept : .fail
        ContractTrace.log("contract.configuration", ["policy": policy.rawValue, "handlerAttached": false])
      }
      let configuration = VoIPApplication.Configuration(client: .init(includesConversationInRecents: false,
        unhandledAction: mode == "unhandled-accept" ? .accept : .fail))
      let client = try VoIPApplication.start(configuration: configuration).client
      let repeated = try VoIPApplication.start(configuration: configuration).client
      ContractTrace.log("contract.owner", ["identity": String(describing: ObjectIdentifier(client)),
        "sharedIdentity": String(describing: ObjectIdentifier(repeated)), "warnings": warnings])
      if mode == "guard" { guardContract() }
      else if mode == "unhandled-accept" || mode == "unhandled-fail" {
        try await policyContract(client, accepting: mode == "unhandled-accept")
      }
      else { finish(false) }
    } catch {
      ContractTrace.log("contract.error", ["type": String(describing: type(of: error))])
      finish(false)
    }
  }

  private func guardContract() {
    phase = "extra-cx"
    ContractTrace.log("contract.creationBegin", ["phase": phase])
    extraProvider = CXProvider(configuration: CXProviderConfiguration())
    ContractTrace.log("contract.creationEnd", ["phase": phase, "warnings": warnings,
      "identity": String(describing: ObjectIdentifier(extraProvider!))])
    phase = "extra-lck"
    ContractTrace.log("contract.creationBegin", ["phase": phase])
    extraManager = ConversationManager(configuration: .init(ringtoneName: nil, iconTemplateImageData: nil,
      maximumConversationGroups: 2, maximumConversationsPerConversationGroup: 1,
      includesConversationInRecents: false, supportsVideo: false, supportedHandleTypes: [.generic]))
    ContractTrace.log("contract.creationEnd", ["phase": phase, "warnings": warnings,
      "identity": String(describing: ObjectIdentifier(extraManager!))])
    // Extra owners exist only in this diagnostic fixture; no SDK command uses them.
    finish(warnings == 2)
  }

  /// Exercise the SDK's public result through genuine LCK and its runtime-backed
  /// CX report completion. This observes compatibility, not NSError wrapper depth
  /// or physical-device behavior. The source tag is not an actual PushKit delivery.
  private func duplicateContract(_ client: ConversationClient) async throws {
    let payload = IncomingCallPayload(eventId: UUID().uuidString, serverCallId: UUID().uuidString,
      caller: .init(id: "native-duplicate-wrapper-fixture"))
    let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
    let payloadHash = SHA256.hash(data: try encoder.encode(payload)).map { String(format: "%02x", $0) }.joined()
    let observer = client.observeState { update in
      ContractTrace.log("contract.duplicateSnapshot", ["revision": update.revision,
        "conversations": update.conversations.map { call -> [String: Any] in
          var value: [String: Any] = ["id": call.id.uuidString, "status": call.status.rawValue,
            "hasConnectedAt": call.connectedAt != nil]
          if let pending = call.pending {
            value["pending"] = ["id": pending.id.uuidString, "kind": pending.kind.rawValue,
              "origin": pending.origin.rawValue]
          }
          return value
        }])
    }
    defer { client.removeStateObserver(observer); client.setConversationHandler(nil) }
    func report(_ ordinal: Int, _ source: IncomingReportSource, _ tag: String) async throws -> UUID {
      let fields: [String: Any] = ["ordinal": ordinal, "source": tag, "payloadSHA256": payloadHash]
      ContractTrace.log("contract.duplicateReportRequested", fields)
      CKSimEmit("contract.duplicateReportBoundary", fields.merging(["stage": "requested"]) { _, new in new })
      let id = try await client.reportIncomingConversation(payload, source: source)
      CKSimEmit("contract.duplicateReportBoundary", fields.merging(["stage": "returned", "callUUID": id.uuidString]) { _, new in new })
      ContractTrace.log("contract.duplicateReportResult", fields.merging([
        "conversationID": id.uuidString, "accepted": true,
        "conversations": client.snapshots.map { call -> [String: Any] in
          ["id": call.id.uuidString, "status": call.status.rawValue,
           "hasConnectedAt": call.connectedAt != nil, "hasPending": call.pending != nil]
        }
      ]) { _, new in new })
      return id
    }
    let first = try await report(1, .foreground, "foreground")
    let second = try await report(2, .pushKit, "pushKit")
    let oneLiveConversation = client.snapshots.count == 1 && client.snapshots.first?.id == first &&
      client.snapshots.first?.status == .ringing
    var endHandled = false
    client.setConversationHandler(.init(join: { _, _ in
      ContractTrace.log("contract.duplicateUnexpectedAction", ["kind": "join"])
      throw NSError(domain: "SDKM1DuplicateFixture", code: 1)
    }, start: { _, _ in
      ContractTrace.log("contract.duplicateUnexpectedAction", ["kind": "start"])
      throw NSError(domain: "SDKM1DuplicateFixture", code: 1)
    }, end: { call, context in
      ContractTrace.log("contract.duplicateEndHandler", ["conversationID": call.id.uuidString,
        "actionID": context.actionID.uuidString, "origin": context.origin.rawValue])
      endHandled = true
    }))
    ContractTrace.log("contract.duplicateEndRequested", ["conversationID": first.uuidString])
    CKSimEmit("contract.duplicateEndBoundary", ["stage": "requested", "callUUID": first.uuidString])
    // Exercise the local report's real End request using the same native action
    // oracle; the original A1 qualification used endConversation at this seam.
    try await client.reportConversation(first, report: .ended(.local))
    // LCK command completion acknowledges the request and may precede perform.
    // Observe actual handler/terminal cleanup separately before completing proof.
    CKSimEmit("contract.duplicateEndBoundary", ["stage": "command-returned", "callUUID": first.uuidString])
    ContractTrace.log("contract.duplicateEndCommandResult", ["conversationID": first.uuidString, "accepted": true])
    let empty = await wait(seconds: 5) { endHandled && client.snapshots.isEmpty }
    CKSimEmit("contract.duplicateEndBoundary", ["stage": "observed-complete", "callUUID": first.uuidString, "empty": empty])
    ContractTrace.log("contract.duplicateObservation", ["conversationID": first.uuidString, "empty": empty])
    finish(first == second && oneLiveConversation && empty && warnings == 0)
  }

  private func policyContract(_ client: ConversationClient, accepting: Bool) async throws {
    let token = client.observeState { update in
      let calls: [[String: Any]] = update.conversations.map { call in
        var value: [String: Any] = ["id": call.id.uuidString, "status": call.status.rawValue,
          "hasConnectedAt": call.connectedAt != nil]
        if let pending = call.pending {
          value["pending"] = ["id": pending.id.uuidString, "kind": pending.kind.rawValue,
            "origin": pending.origin.rawValue, "deadlineMS": Int(pending.deadline.timeIntervalSince1970 * 1000)]
        }
        return value
      }
      ContractTrace.log("contract.snapshot", ["revision": update.revision, "conversations": calls])
    }
    defer { client.removeStateObserver(token) }
    let id = try await client.reportIncomingConversation(.init(eventId: UUID().uuidString,
      serverCallId: UUID().uuidString, caller: .init(id: "native-policy-fixture")))
    ContractTrace.log("contract.incoming", ["conversationID": id.uuidString])
    let request = Task { @MainActor in
      do { try await client.joinConversation(id); ContractTrace.log("contract.joinResult", ["accepted": true]) }
      catch { ContractTrace.log("contract.joinResult", ["accepted": false]) }
    }
    defer { request.cancel() }
    _ = await wait(seconds: 5) { client.snapshots.first(where: { $0.id == id })?.pending != nil }
    guard let pending = client.snapshots.first(where: { $0.id == id })?.pending else {
      try? await client.reportConversation(id, report: .ended(.failed)); finish(false); return
    }
    let clockBefore = Int(Date().timeIntervalSince1970 * 1000)
    CKSimEmit("contract.pendingClock", ["actionID": pending.id.uuidString])
    ContractTrace.log("contract.pending", ["conversationID": id.uuidString, "actionID": pending.id.uuidString,
      "kind": pending.kind.rawValue, "origin": pending.origin.rawValue,
      "deadlineMS": Int(pending.deadline.timeIntervalSince1970 * 1000),
      "remainingMS": Int(pending.deadline.timeIntervalSinceNow * 1000), "handlerAttached": false,
      "clockBeforeMS": clockBefore])
    if !accepting {
      let cleared = await wait(seconds: pending.deadline.timeIntervalSinceNow + 3) {
        client.snapshots.first(where: { $0.id == id })?.pending?.id != pending.id
      }
      #if DEBUG && targetEnvironment(simulator)
      // Give any valid asynchronous LCK-to-CX forwarding its whole platform
      // window. Cleanup after expiry cannot create a pre-expiry native failure.
      guard let platformDeadline = ContractTrace.platformDeadlines[pending.id.uuidString],
            await wait(seconds: 3, until: { Int(Date().timeIntervalSince1970 * 1000) >= platformDeadline }) else {
        finish(false); return
      }
      #endif
      let call = client.snapshots.first(where: { $0.id == id })
      ContractTrace.log("contract.failureObservation", ["conversationID": id.uuidString,
        "pendingCleared": cleared, "conversationPresent": call != nil,
        "status": call?.status.rawValue ?? "absent"])
      // RFC policy fails the action; it does not promise that a failed join ends
      // its conversation. Explicit fixture cleanup is a separate observation.
      if call != nil {
        ContractTrace.log("contract.fixtureCleanupRequested", ["conversationID": id.uuidString, "reason": "failed"])
        try await client.reportConversation(id, report: .ended(.failed))
        ContractTrace.log("contract.fixtureCleanupReported", ["conversationID": id.uuidString, "reason": "failed"])
      } else {
        ContractTrace.log("contract.fixtureCleanupSkipped", ["conversationID": id.uuidString])
      }
      let empty = await wait(seconds: 3) { client.snapshots.isEmpty }
      finish(cleared && empty && warnings == 0)
      return
    }
    let joined = await wait(seconds: pending.deadline.timeIntervalSinceNow + 3) {
      guard let call = client.snapshots.first(where: { $0.id == id }) else { return false }
      return call.status == .connecting && call.pending == nil && call.connectedAt == nil
    }
    ContractTrace.log("contract.joinSettled", ["joined": joined, "conversationID": id.uuidString])
    var connected = false
    if joined {
      ContractTrace.log("contract.backendConnectedRequested", ["conversationID": id.uuidString])
      try await client.reportConversation(id, report: .connected)
      ContractTrace.log("contract.backendConnected", ["accepted": true, "conversationID": id.uuidString])
      connected = await wait(seconds: 3) {
        guard let call = client.snapshots.first(where: { $0.id == id }) else { return false }
        return call.status == .connected && call.connectedAt != nil
      }
    }
    ContractTrace.log("contract.observation", ["connected": connected, "conversationID": id.uuidString])
    if client.snapshots.contains(where: { $0.id == id }) {
      ContractTrace.log("contract.endReportRequested", ["conversationID": id.uuidString])
      try await client.reportConversation(id, report: .ended(.remoteEnded))
      ContractTrace.log("contract.reportEnded", ["conversationID": id.uuidString])
    }
    let empty = await wait(seconds: 3) { client.snapshots.isEmpty }
    finish(connected && empty && warnings == 0)
  }

  private func finish(_ success: Bool) { ContractTrace.log("contract.result", ["success": success]); ContractTrace.log("done") }
}

@main
private struct SDKM1ContractsApp: App {
  @State private var started = false
  init() { ContractTrace.log("app.entry") }
  var body: some Scene {
    WindowGroup { Text("M1 native contracts").task {
      guard !started else { return }; started = true
      await SDKM1ContractsProbe().run()
    } }
  }
}
