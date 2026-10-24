#if targetEnvironment(simulator)
import SwiftUI
@preconcurrency import LiveCommunicationKit
import AVFAudio

// One genuine LCK manager. App stimuli use a separate test mailbox; system
// stimuli use the runtime's shipping mailbox. Neither creates a CXProvider.
@MainActor
final class LCKFixture: NSObject, UIApplicationDelegate, @preconcurrency ConversationManagerDelegate {
  // LCK marks async entry points concurrent but omits Sendable. The fixture
  // owns one manager; delegate and mutable fixture state remain main-isolated.
  nonisolated(unsafe) var manager: ConversationManager!
  var ids: [String: UUID] = [:]
  var rules: [String: [String: Any]] = [:]
  var counts: [String: Int] = [:]
  var reversed: [ConversationAction] = []
  var timer: Timer?
  let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
  var mailbox: URL { documents.appendingPathComponent("lck-fixture-requests") }
  var capabilities: Conversation.Capabilities = [.pausing, .merging, .unmerging]
  func application(_ app: UIApplication, didFinishLaunchingWithOptions options: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
    guard RuntimeBootstrap.activate() else { fatalError("LCK fixture runtime refused") }
    let env = ProcessInfo.processInfo.environment
    if env["LCK_FIXTURE_CAPABILITIES"] == "none" { capabilities = [] }
    manager = ConversationManager(configuration: .init(ringtoneName: nil, iconTemplateImageData: nil,
      maximumConversationGroups: Int(env["LCK_FIXTURE_GROUPS"] ?? "2") ?? 2,
      maximumConversationsPerConversationGroup: Int(env["LCK_FIXTURE_PER_GROUP"] ?? "1") ?? 1,
      includesConversationInRecents: false, supportsVideo: false, supportedHandleTypes: [.generic, .phoneNumber]))
    manager.delegate = self
    try! FileManager.default.createDirectory(at: mailbox, withIntermediateDirectories: true)
    timer = Timer.scheduledTimer(withTimeInterval: 0.02, repeats: true) { [weak self] _ in
      MainActor.assumeIsolated { self?.drain() }
    }
    log("ready")
    return true
  }
  func log(_ event: String, _ fields: [String: Any] = [:]) {
    var value = fields
    value["main"] = Thread.isMainThread
    CKSimEmit("lck.fixture." + event, value)
  }
  func snapshot(_ label: String) {
    log("snapshot", ["label": label, "conversations": manager.conversations.map {
      ["callUUID": $0.uuid.uuidString, "state": String(describing: $0.state)]
    }])
  }
  func drain() {
    for file in (try? FileManager.default.contentsOfDirectory(at: mailbox, includingPropertiesForKeys: nil)) ?? [] where file.pathExtension == "json" {
      do {
        let data = try Data(contentsOf: file)
        try FileManager.default.removeItem(at: file)
        let value = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        Task { await self.command(value) }
      } catch { log("mailboxError", ["error": String(describing: error)]) }
    }
  }
  func command(_ value: [String: Any]) async {
    let requestID = value["id"] as? String ?? ""
    guard value["runID"] as? String == ProcessInfo.processInfo.environment["CKSIM_RUN_ID"] else {
      log("commandRejected", ["requestID": requestID, "reason": "session changed"]); return
    }
    let op = value["operation"] as? String ?? ""
    let name = value["call"] as? String ?? "A"
    let id = ids[name] ?? UUID()
    ids[name] = id
    do {
      var action: ConversationAction?
      switch op {
      case "policy":
        rules = value["rules"] as? [String: [String: Any]] ?? [:]
        counts = [:]
      case "incoming":
        let number = value["handle"] as? String
        let handle = Handle(type: number == nil ? .generic : .phoneNumber, value: number ?? name, displayName: "LCK " + name)
        try await manager.reportNewIncomingConversation(uuid: id, update: .init(members: [handle], capabilities: capabilities))
      case "start": action = StartConversationAction(conversationUUID: id, handles: [Handle(type: .generic, value: name)], isVideo: false)
      case "pause": action = PauseConversationAction(conversationUUID: id, isPaused: value["paused"] as? Bool ?? true)
      case "end": action = EndConversationAction(conversationUUID: id)
      case "merge":
        guard let other = ids[value["other"] as? String ?? ""] else { throw CocoaError(.validationMissingMandatoryProperty) }
        action = MergeConversationAction(conversationUUID: id, conversationUUIDToMergeWith: other)
      case "unmerge": action = UnmergeConversationAction(conversationUUID: id)
      case "snapshot": break
      default: throw CocoaError(.validationMissingMandatoryProperty)
      }
      if let action { try await manager.perform([action]) }
      log("command", ["requestID": requestID, "operation": op, "name": name, "callUUID": id.uuidString, "success": true])
    } catch {
      let error = error as NSError
      log("command", ["requestID": requestID, "operation": op, "name": name, "callUUID": id.uuidString,
                      "success": false, "errorDomain": error.domain, "errorCode": error.code])
    }
    snapshot(requestID)
  }
  func kind(_ action: ConversationAction) -> String {
    switch action {
    case is JoinConversationAction: "Join"
    case is StartConversationAction: "Start"
    case is PauseConversationAction: "Pause"
    case is EndConversationAction: "End"
    case is MergeConversationAction: "Merge"
    case is UnmergeConversationAction: "Unmerge"
    default: String(describing: type(of: action))
    }
  }
  func fields(_ action: ConversationAction) -> [String: Any] {
    var value: [String: Any] = ["actionID": action.uuid.uuidString, "callUUID": action.conversationUUID.uuidString,
      "kind": kind(action), "advertisedDeadlineMS": action.timeoutDate.timeIntervalSince1970 * 1000,
      "advertisedRemainingMS": action.timeoutDate.timeIntervalSinceNow * 1000]
    if let pause = action as? PauseConversationAction { value["held"] = pause.isPaused }
    return value
  }
  func settle(_ action: ConversationAction, failed: Bool, policy: String) {
    if failed { action.fail() } else {
      switch action {
      case let join as JoinConversationAction: join.fulfill(dateConnected: Date())
      case let start as StartConversationAction: start.fulfill(dateStarted: Date())
      case let end as EndConversationAction: end.fulfill(dateEnded: Date())
      default: action.fulfill()
      }
    }
    var value = fields(action)
    value["failed"] = failed; value["policy"] = policy
    log("settled", value)
    if !failed, action is StartConversationAction,
       let conversation = manager.conversations.first(where: { $0.uuid == action.conversationUUID }) {
      manager.reportConversationEvent(.conversationStartedConnecting(Date()), for: conversation)
      manager.reportConversationEvent(.conversationConnected(Date()), for: conversation)
    }
    snapshot("settled")
  }
  func conversationManager(_ manager: ConversationManager, perform action: ConversationAction) {
    let name = kind(action)
    counts[name, default: 0] += 1
    var rule = rules[name] ?? [:]
    if let nth = rule["nth"] as? Int, nth != counts[name] { rule = [:] }
    let policy = rule["mode"] as? String ?? "immediate"
    var value = fields(action)
    value["policy"] = policy; value["ordinal"] = counts[name]
    log("perform", value)
    if action is JoinConversationAction || action is StartConversationAction {
      try? AVAudioSession.sharedInstance().setCategory(.playAndRecord, mode: .voiceChat)
    }
    switch policy {
    case "pending": break
    case "delay":
      DispatchQueue.main.asyncAfter(deadline: .now() + Double(rule["ms"] as? Int ?? 2000) / 1000) {
        self.settle(action, failed: false, policy: policy)
      }
    case "reverse":
      reversed.append(action)
      if reversed.count == 1 {
        DispatchQueue.main.asyncAfter(deadline: .now() + Double(rule["ms"] as? Int ?? 630) / 1000) {
          let actions = self.reversed.reversed(); self.reversed = []
          for item in actions { self.settle(item, failed: false, policy: policy) }
        }
      }
    default: settle(action, failed: policy == "fail", policy: policy)
    }
  }
  func conversationManager(_ manager: ConversationManager, timedOutPerforming action: ConversationAction) {
    log("timedOut", fields(action)); snapshot("timedOut")
  }
  func conversationManager(_ manager: ConversationManager, conversationChanged conversation: Conversation) {
    log("changed", ["callUUID": conversation.uuid.uuidString, "state": String(describing: conversation.state)])
    snapshot("changed")
  }
  func conversationManagerDidBegin(_ manager: ConversationManager) { log("didBegin") }
  func conversationManagerDidReset(_ manager: ConversationManager) { log("didReset") }
  func conversationManager(_ manager: ConversationManager, didActivate audioSession: AVAudioSession) { log("didActivate") }
  func conversationManager(_ manager: ConversationManager, didDeactivate audioSession: AVAudioSession) { log("didDeactivate") }
}
@main
struct LCKFixtureApp: App {
  @UIApplicationDelegateAdaptor(LCKFixture.self) var fixture
  var body: some Scene { WindowGroup { Text("LCK native acceptance fixture") } }
}
#endif
