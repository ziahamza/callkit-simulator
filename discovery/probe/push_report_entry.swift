import CryptoKit
import Foundation
@preconcurrency import LiveCommunicationKit
@preconcurrency import PushKit
import SwiftUI

@MainActor
private enum EntryTrace {
  static var sequence = 0
  static let epoch = ProcessInfo.processInfo.systemUptime
  static func log(_ event: String, _ fields: [String: Any] = [:]) {
    sequence += 1
    var row = fields
    row.merge(["event": event, "sequence": sequence, "pid": Int(getpid()), "main": Thread.isMainThread,
      "runID": ProcessInfo.processInfo.environment["CKSIM_RUN_ID"] ?? "",
      "simulatorID": ProcessInfo.processInfo.environment["SIMULATOR_UDID"] ?? "",
      "bundleID": Bundle.main.bundleIdentifier ?? "", "t_ms": Int((ProcessInfo.processInfo.systemUptime - epoch) * 1000)]) { _, value in value }
    if let data = try? JSONSerialization.data(withJSONObject: row, options: [.sortedKeys]) {
      FileHandle.standardError.write(Data("PROBE ".utf8) + data + Data([10]))
    }
  }
}

/// These negative delegates deliberately reproduce the old deferred/missing-report
/// behavior. They never stand in for the SDK's genuine reporting implementation.
@MainActor
private final class GenericEntryDelegate: NSObject, @preconcurrency PKPushRegistryDelegate {
  let mode: String
  let manager: ConversationManager
  init(mode: String) {
    self.mode = mode
    manager = ConversationManager(configuration: .init(ringtoneName: nil, iconTemplateImageData: nil,
      maximumConversationGroups: 2, maximumConversationsPerConversationGroup: 2,
      includesConversationInRecents: false, supportsVideo: false, supportedHandleTypes: [.generic]))
  }
  func pushRegistry(_ registry: PKPushRegistry, didUpdate credentials: PKPushCredentials, for type: PKPushType) {}
  func pushRegistry(_ registry: PKPushRegistry, didInvalidatePushTokenFor type: PKPushType) {}
  @available(iOS 26.4, *)
  func pushRegistry(_ registry: PKPushRegistry, didReceiveIncomingVoIPPushWith payload: PKPushPayload,
                    metadata: PKVoIPPushMetadata, withCompletionHandler completion: @escaping () -> Void) {
    receive(completion)
  }
  func pushRegistry(_ registry: PKPushRegistry, didReceiveIncomingPushWith payload: PKPushPayload,
                    for type: PKPushType, completion: @escaping () -> Void) { receive(completion) }
  private func receive(_ completion: @escaping () -> Void) {
    let late = mode == "late-report"
    guard mode == "deferred" || late else { completion(); return }
    let push = SimulatorSupport.currentPushIdentifier
    Task { @MainActor in
      try? await Task.sleep(for: .milliseconds(late ? 5_000 : 100))
      await SimulatorPushContext.$pushIdentifier.withValue(push) {
        let id = UUID()
        SimulatorSupport.associateIncomingReport(id)
        defer { SimulatorSupport.finishIncomingReport(id) }
        try? await manager.reportNewIncomingConversation(uuid: id, update: .init(
          members: [Handle(type: .generic, value: "entry-negative")], capabilities: []))
      }
      if !late { completion() }
    }
    // Returning and completing must not cancel the independent report deadline.
    if late { completion() }
  }
}

@MainActor
private final class EntryProbe {
  private var delegate: GenericEntryDelegate?
  private var registry: PKPushRegistry?
  func run() async {
    let env = ProcessInfo.processInfo.environment
    let mode = env["SDK_PUSH_ENTRY_CASE"] ?? ""
    let executable = Bundle.main.executableURL.flatMap { try? Data(contentsOf: $0) } ?? Data()
    EntryTrace.log("launch", ["traceVersion": 1, "case": mode, "osVersion": UIDevice.current.systemVersion,
      "executableSHA256": SHA256.hash(data: executable).map { String(format: "%02x", $0) }.joined()])
    guard SimulatorSupport.activateIfNeeded() else { finish(false); return }
    do {
      if mode == "sdk-main-stall" {
        let application = try VoIPApplication.start(configuration: .init(client: .init(includesConversationInRecents: false)))
        try await runMainStall(application)
        return
      }
      if mode.hasPrefix("sdk-") { _ = try VoIPApplication.start(configuration: .init(client: .init(includesConversationInRecents: false))) }
      else {
        let delegate = GenericEntryDelegate(mode: mode)
        let registry = PKPushRegistry(queue: .main)
        registry.delegate = delegate
        self.delegate = delegate; self.registry = registry
        registry.desiredPushTypes = [.voIP]
      }
      let count = mode == "sdk-duplicate" ? 2 : 1
      let required = mode != "optional"
      let run = env["CKSIM_RUN_ID"] ?? ""
      let payload: [AnyHashable: Any] = ["incomingCall": ["eventId": "entry-event", "serverCallId": "entry-server",
        "caller": ["id": "entry-caller", "displayName": "Entry fixture"], "hasVideo": false]]
      for index in 0..<count {
        let pushID = run + "-" + String(index)
        EntryTrace.log("entry.inject", ["pushID": pushID, "mustReport": required])
        CKSimInjectPush(payload, required, pushID)
      }
      if mode == "late-report" { try await Task.sleep(for: .seconds(6)) }
      let deadline = ContinuousClock.now.advanced(by: .seconds(7))
      var completed = false
      while ContinuousClock.now < deadline {
        if let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first,
           let bytes = try? Data(contentsOf: documents.appendingPathComponent(".callkit-simulator/" + run + "/events.jsonl")),
           let text = String(data: bytes, encoding: .utf8) {
          let rows = text.split(separator: "\n").compactMap { line in
            try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any]
          }
          if rows.filter({ $0["event"] as? String == "push.completed" }).count == count,
             rows.filter({ $0["event"] as? String == "push.callbackReturned" }).count == count { completed = true; break }
        }
        try await Task.sleep(for: .milliseconds(20))
      }
      // This marker proves capture liveness only. The strict external validator
      // must reject required missing/deferred cases despite their terminal marker.
      finish(completed)
    } catch { finish(false) }
  }
  private func runMainStall(_ application: VoIPApplication) async throws {
    let owner = try PushRegistryOwner.shared(receiver: application.pushService)
    let payload: [AnyHashable: Any] = ["incomingCall": ["eventId": "main-stall-event", "serverCallId": "main-stall-server",
      "caller": ["id": "main-stall-caller", "displayName": "Main stall fixture"], "hasVideo": false]]
    // This helper invokes the linked native callback body on a worker after
    // main has begun its controlled stall. It never marks framework entry.
    CKProbeRunIncomingMainStall(owner, payload)
    let deadline = ContinuousClock.now.advanced(by: .seconds(4))
    while ContinuousClock.now < deadline {
      if CKProbeMainStallCompletionCount() == 1 && CKProbeMainStallCallbackReturned() &&
          application.client.snapshots.count == 1 { break }
      try await Task.sleep(for: .milliseconds(20))
    }
    let snapshots = application.client.snapshots
    let conversations: [[String: Any]] = snapshots.map { snapshot in
      var row: [String: Any] = ["id": snapshot.id.uuidString, "status": snapshot.status.rawValue,
        "direction": snapshot.direction.rawValue]
      if let payload = snapshot.payload, let data = try? JSONEncoder().encode(payload),
         let value = try? JSONSerialization.jsonObject(with: data) { row["payload"] = value }
      return row
    }
    EntryTrace.log("entry.mainStallObservation", ["owner": CKProbeMainStallOwnerIdentity(),
      "fixtureID": (ProcessInfo.processInfo.environment["CKSIM_RUN_ID"] ?? "") + "-native-main-stall",
      "conversationID": snapshots.first?.id.uuidString ?? "", "conversations": conversations,
      "completionCount": CKProbeMainStallCompletionCount(), "callbackReturned": CKProbeMainStallCallbackReturned()])
    finish(CKProbeMainStallCompletionCount() == 1 && CKProbeMainStallCallbackReturned() && snapshots.count == 1)
  }
  private func finish(_ complete: Bool) {
    EntryTrace.log("entry.capture", ["boundariesObserved": complete])
    EntryTrace.log("done")
  }
}

@main
private struct PushEntryApp: App {
  @State private var started = false
  private let probe = EntryProbe()
  init() { EntryTrace.log("app.entry") }
  var body: some Scene {
    WindowGroup { Text("Push report entry fixture").task {
      guard !started else { return }; started = true
      await probe.run()
    } }
  }
}
