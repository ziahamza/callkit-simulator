#if os(iOS) && targetEnvironment(simulator)
import Foundation
import Darwin
#if SWIFT_PACKAGE
import CKSimRuntimeC
#endif
@preconcurrency import CallKit
@preconcurrency import AVFoundation

private enum ProviderAPIKind: String { case callkit, livecommunicationkit }
private struct FulfilledPostEndUnmutePolicy: Decodable {
  enum Settlement: String, Decodable { case timeout, complete }
  let callkit: Settlement
  let livecommunicationkit: Settlement
  func settlement(for api: ProviderAPIKind) -> Settlement {
    switch api { case .callkit: callkit; case .livecommunicationkit: livecommunicationkit }
  }
}
private struct BehaviorProfile: Decodable {
  let id: String
  let schemaVersion: Int
  let answerExpirySeconds: Double
  let ringTimeoutSeconds: Double
  let unknownProviderSeconds: Double
  let actionTimeoutSeconds: Double
  let startDeadlineSeconds: Double
  let answerDeadlineSeconds: Double
  let ordinaryDeadlineSeconds: Double
  let lckActionDeadlineSeconds: Double
  let reportLatencySeconds: Double
  let pushReportDeadlineSeconds: Double
  let muteEchoSeconds: Double
  let postEndUnmuteSeconds: Double
  let fulfilledPostEndUnmutePolicy: FulfilledPostEndUnmutePolicy

  func timing(api: ProviderAPIKind, answer: Bool, start: Bool) -> (deadline: Double, expiry: Double) {
    if api == .livecommunicationkit {
      return (lckActionDeadlineSeconds, lckActionDeadlineSeconds)
    }
    if answer { return (answerDeadlineSeconds, answerExpirySeconds) }
    if start { return (startDeadlineSeconds, startDeadlineSeconds) }
    return (ordinaryDeadlineSeconds, actionTimeoutSeconds)
  }
}

private final class ProviderRegistration: @unchecked Sendable {
  let provider: CXProvider
  weak var delegate: (any CXProviderDelegate)?
  var queue: DispatchQueue?
  var valid = true
  var generation: UInt64 = 0
  var apiKind: ProviderAPIKind = .callkit
  init(_ provider: CXProvider) { self.provider = provider }
}

private final class CallState: @unchecked Sendable {
  let id: UUID
  let reporter: ProviderRegistration
  var update: CXCallUpdate?
  var outgoing = false
  var connected = false
  var held = false
  var ended = false
  var muted = false
  var group: UUID
  var ringing = true
  var pushID = ""
  init(_ id: UUID, _ reporter: ProviderRegistration) {
    self.id = id; self.reporter = reporter; self.group = id
  }
  func snapshot() -> CXCall {
    CKSimMakeCall(id, outgoing, connected, held, ended)
  }
}

private enum ActionOrigin: String { case application, system, muteEcho, postEndUnmute }
private final class RuntimePendingAction: @unchecked Sendable {
  let action: CXAction
  let provider: ProviderRegistration
  let transaction: CXTransaction
  let origin: ActionOrigin
  let apiKind: ProviderAPIKind
  let generation: UInt64
  let call: CallState?
  let groupTarget: CallState?
  let issuedAt = Date()
  let issuedUptime = DispatchTime.now().uptimeNanoseconds
  var expired = false
  var settled = false
  init(_ action: CXAction, _ provider: ProviderRegistration, _ transaction: CXTransaction, _ origin: ActionOrigin, call: CallState?, groupTarget: CallState?) {
    self.action = action; self.provider = provider; self.transaction = transaction; self.origin = origin
    self.apiKind = provider.apiKind
    self.generation = provider.generation; self.call = call; self.groupTarget = groupTarget
  }
}

private final class ObserverRegistration: @unchecked Sendable {
  weak var observer: CXCallObserver?
  weak var delegate: (any CXCallObserverDelegate)?
  let queue: DispatchQueue
  init(_ observer: CXCallObserver, _ delegate: (any CXCallObserverDelegate)?, _ queue: DispatchQueue?) {
    self.observer = observer; self.delegate = delegate; self.queue = queue ?? .main
  }
}

private struct Faults: Codable {
  var staleAudio = false
  var reportCallbackDelayMS = 0
  var invalidateDuringHandoff: Bool?
}
// Objective-C clients supply blocks without Swift Sendable annotations. The
// immutable block is transferred once and invoked only on its designated queue.
private final class Completion: @unchecked Sendable {
  let invoke: (Error?) -> Void
  init(_ invoke: @escaping (Error?) -> Void) { self.invoke = invoke }
}
private struct Command: Decodable {
  let id: UUID
  let runID: String
  let pid: Int
  let bundleID: String
  let operation: String
  var callUUID: UUID?
  var activeCallUUID: UUID?
  var held: Bool?
  var muted: Bool?
  var digits: String?
  var mustReport: Bool?
  var faults: Faults?
}

// RuntimeEngine's serial state queue owns this value. An ambiguous same-UUID
// overlap remains unbound instead of assigning a different push's identifier.
private struct IncomingPushAssociations {
  enum Resolution: Equatable { case absent, associated(String), ambiguous }
  private struct Entry {
    var identifiers: Set<String>
    var ambiguous = false
  }
  private var pending: [UUID: Entry] = [:]
  mutating func associate(_ id: UUID, pushID: String) -> Bool {
    guard !pushID.isEmpty else { return false }
    var entry = pending[id] ?? Entry(identifiers: [])
    entry.identifiers.insert(pushID)
    entry.ambiguous = entry.ambiguous || entry.identifiers.count > 1
    pending[id] = entry
    return !entry.ambiguous
  }
  mutating func consume(_ id: UUID) -> Resolution {
    guard let entry = pending[id] else { return .absent }
    guard !entry.ambiguous, let identifier = entry.identifiers.first else { return .ambiguous }
    pending.removeValue(forKey: id)
    return .associated(identifier)
  }
  mutating func finish(_ id: UUID, pushID: String) {
    guard var entry = pending[id], entry.identifiers.remove(pushID) != nil else { return }
    pending[id] = entry.identifiers.isEmpty ? nil : entry
  }
  mutating func complete(pushID: String) {
    guard !pushID.isEmpty else { return }
    for id in Array(pending.keys) { finish(id, pushID: pushID) }
  }
}

// A report counts only when the interposed genuine provider report is entered.
// This is simulator policy, not a synthetic SDK "about to await" signal.
private struct PushReportingObligations {
  struct Observation {
    let required: Bool
    let reportEntered: Bool
    let violation: String?
  }
  private struct Delivery {
    let required: Bool
    let token = UUID()
    var reportEntered = false
    var returned = false
    var completed = false
  }
  private var deliveries: [String: Delivery] = [:]
  mutating func receive(_ id: String, required: Bool) -> Bool {
    guard !id.isEmpty, deliveries[id] == nil else { return false }
    deliveries[id] = Delivery(required: required)
    return true
  }
  mutating func entered(_ id: String) {
    guard !id.isEmpty else { return }
    deliveries[id]?.reportEntered = true
    if let delivery = deliveries[id], delivery.returned && delivery.completed { deliveries.removeValue(forKey: id) }
  }
  func deadlineToken(_ id: String) -> UUID? { deliveries[id]?.token }
  mutating func expire(_ id: String, token: UUID) -> Bool {
    guard let delivery = deliveries[id], delivery.token == token, delivery.required, !delivery.reportEntered else { return false }
    deliveries.removeValue(forKey: id)
    return true
  }
  mutating func returned(_ id: String) -> Observation {
    observe(id, completion: false)
  }
  mutating func completed(_ id: String) -> Observation {
    observe(id, completion: true)
  }
  private mutating func observe(_ id: String, completion: Bool) -> Observation {
    guard var delivery = deliveries[id] else {
      return Observation(required: false, reportEntered: false, violation: "unknown-delivery")
    }
    let repeated = completion ? delivery.completed : delivery.returned
    if completion { delivery.completed = true } else { delivery.returned = true }
    let violation = repeated ? "repeated-" + (completion ? "completion" : "callback-return") :
      delivery.required && !delivery.reportEntered ? "required-report-missing-at-" + (completion ? "completion" : "callback-return") : nil
    if delivery.completed && delivery.returned && (!delivery.required || delivery.reportEntered) { deliveries.removeValue(forKey: id) }
    else { deliveries[id] = delivery }
    return Observation(required: delivery.required, reportEntered: delivery.reportEntered, violation: violation)
  }
}

/// One private serial queue owns all state. App code always executes elsewhere.
@objc(CKSimulatorEngine)
final class RuntimeEngine: NSObject, CKRuntimeEngineProtocol, @unchecked Sendable {
  private static let storage = RuntimeStorage()

  static func prepare() throws {
    storage.lock.lock()
    defer { storage.lock.unlock() }
    if storage.engine == nil { storage.engine = try RuntimeEngine(prepared: ()) }
  }

  static func existing() -> RuntimeEngine? {
    storage.lock.lock()
    defer { storage.lock.unlock() }
    return storage.engine
  }

  static func releasePrepared() {
    storage.lock.lock()
    let engine = storage.engine
    storage.engine = nil
    storage.lock.unlock()
    engine?.shutdown()
  }
  private let state = DispatchQueue(label: "dev.callkitsim.engine", qos: .userInitiated)
  private let stateKey = DispatchSpecificKey<Bool>()
  private let profile: BehaviorProfile
  private var providers: [ObjectIdentifier: ProviderRegistration] = [:]
  private var selected: ProviderRegistration?
  private let controllerQueues = NSMapTable<CXCallController, DispatchQueue>.weakToStrongObjects()
  private var calls: [UUID: CallState] = [:]
  private var incomingPushAssociations = IncomingPushAssociations()
  private var pushReportingObligations = PushReportingObligations()
  private var pendingActions: [UUID: RuntimePendingAction] = [:]
  private let deadlines = NSMapTable<CXAction, NSDate>.weakToStrongObjects()
  private var observers: [ObjectIdentifier: ObserverRegistration] = [:]
  private var audioActive = false
  private var audioRevision: UInt64 = 0
  private var handoffPending = false
  private var faults = Faults()
  private let runID: String
  private let bundleID: String
  private let controlRoot: URL
  private var journal: FileHandle?
  private var sequence: UInt64 = 0
  private let epoch = DispatchTime.now().uptimeNanoseconds
  private var notificationToken: Int32 = 0
  private var notificationRegistered = false
  private var stopped = false
  private var commandResults: [UUID: [String: Any]] = [:]
  private var timers: [UUID: any DispatchSourceTimer] = [:]

  private init(prepared: Void) throws {
    let env = ProcessInfo.processInfo.environment
    let profileID = env["CKSIM_PROFILE"].flatMap { $0.isEmpty ? nil : $0 } ?? "ios-27.0"
    guard profileID == "ios-27.0" else { throw BootstrapError.invalid("unsupported behavior profile") }
    profile = try JSONDecoder().decode(BehaviorProfile.self, from: Self.profileData(profileID))
    let durations = [profile.answerExpirySeconds, profile.ringTimeoutSeconds, profile.unknownProviderSeconds,
      profile.actionTimeoutSeconds, profile.startDeadlineSeconds, profile.answerDeadlineSeconds,
      profile.ordinaryDeadlineSeconds, profile.lckActionDeadlineSeconds, profile.pushReportDeadlineSeconds, profile.reportLatencySeconds, profile.muteEchoSeconds, profile.postEndUnmuteSeconds]
    guard profile.schemaVersion == 1 && profile.id == profileID && durations.allSatisfy({ $0.isFinite && $0 > 0 }) else {
      throw BootstrapError.invalid("invalid behavior profile")
    }
    let requestedRun = env["CKSIM_RUN_ID"].flatMap { $0.isEmpty ? nil : $0 } ?? UUID().uuidString
    guard requestedRun.range(of: "^[A-Za-z0-9_-]{1,64}$", options: .regularExpression) != nil else {
      throw BootstrapError.invalid("invalid simulator run identifier")
    }
    runID = requestedRun
    bundleID = Bundle.main.bundleIdentifier ?? ProcessInfo.processInfo.processName
    guard let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first else {
      throw BootstrapError.invalid("application Documents directory is unavailable")
    }
    controlRoot = documents.appendingPathComponent(".callkit-simulator").appendingPathComponent(runID)
    super.init()
    state.setSpecific(key: stateKey, value: true)
    do {
      for folder in [controlRoot, controlRoot.appendingPathComponent("requests"), controlRoot.appendingPathComponent("responses")] {
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
      }
      let path = controlRoot.appendingPathComponent("events.jsonl")
      guard FileManager.default.createFile(atPath: path.path, contents: nil, attributes: [.posixPermissions: 0o600]) else {
        throw BootstrapError.invalid("cannot create simulator event journal")
      }
      journal = try FileHandle(forWritingTo: path)
      let status = notify_register_dispatch("dev.callkitsim.control.\(getpid())", &notificationToken, state) { [weak self] _ in
        self?.processCommands()
      }
      guard status == NOTIFY_STATUS_OK else { throw BootstrapError.invalid("cannot register simulator mailbox notification") }
      notificationRegistered = true
      let session: [String: Any] = ["schemaVersion": 1, "runID": runID, "pid": Int(getpid()),
        "bundleID": bundleID, "profile": profile.id, "notification": "dev.callkitsim.control.\(getpid())"]
      let data = try JSONSerialization.data(withJSONObject: session, options: [.sortedKeys])
      try data.write(to: documents.appendingPathComponent(".callkit-simulator/session.json"), options: .atomic)
    } catch {
      shutdown()
      throw error
    }
    state.async { self.processCommands() }
  }

  private static func profileData(_ profileID: String) throws -> Data {
    #if SWIFT_PACKAGE || CKSIM_SDK_SOURCE
    // SDK simulator builds embed the canonical profile under this file's simulator
    // guard. No SwiftPM/CocoaPods resource bundle reaches a device build. The host
    // exclusion control checks this JSON against the standalone profile source.
    return Data(embeddedProfileJSON.utf8)
    #else
    return try Data(contentsOf: profileURL(profileID))
    #endif
  }

  #if SWIFT_PACKAGE || CKSIM_SDK_SOURCE
  private static let embeddedProfileJSON = #"""
{
  "id": "ios-27.0",
  "schemaVersion": 1,
  "reference": "iPhone 13 / iOS 27.0 (24A5355q)",
  "answerExpirySeconds": 59.58,
  "ringTimeoutSeconds": 60.1,
  "unknownProviderSeconds": 10.6,
  "actionTimeoutSeconds": 5.2,
  "startDeadlineSeconds": 600,
  "answerDeadlineSeconds": 60,
  "ordinaryDeadlineSeconds": 5,
  "reportLatencySeconds": 1.1,
  "muteEchoSeconds": 0.015,
  "providerRouting": "newest-created",
  "waitingAnswer": "system-only",
  "transactionIdentity": "fresh-transaction-preserved-actions",
  "scope": "Measured iOS 27.0 behavior; not a guarantee for other iOS releases.",
  "postEndUnmuteSeconds": 0.08,
  "fulfilledPostEndUnmutePolicy": {
    "callkit": "timeout",
    "livecommunicationkit": "complete"
  },
  "fulfilledPostEndUnmuteEvidence": {
    "callkit": "Measured fulfilled post-end unmute still times out in 5.0\u20135.9 s.",
    "livecommunicationkit": "Measured no timeout through 5.542 s after fulfilled post-end unmute; terminal settlement follows public fulfill/complete semantics, not a measured infinite observation."
  },
  "lckActionDeadlineSeconds": 30,
  "lckActionDeadlineEvidence": {
    "measured": "24 lck.perform timeoutDate samples are 30 seconds. cx-after has no LCK deliveries; its later CX provider owns the actions.",
    "expiryPolicy": "Proposed runtime expiry at the advertised LCK deadline; unfulfilled LCK expiry timing was not measured by these captures.",
    "captures": {
      "docs/research/callkit-simulator-probe/livecommunicationkit/2026-10-21-iphone13-ios27-lck-basic.jsonl": {
        "sha256": "9fd5e48b62a826af7e62388de87119ab431fe2019a7d40445cdd9b528dde4bf7",
        "samples": 10
      },
      "docs/research/callkit-simulator-probe/livecommunicationkit/2026-10-21-iphone13-ios27-lck-cx-after.jsonl": {
        "sha256": "96d6e968c2cba8e098ec18872882f7a480476dd90611d5ae4cb5dc2dde78692b",
        "samples": 0
      },
      "docs/research/callkit-simulator-probe/livecommunicationkit/2026-10-21-iphone13-ios27-lck-lck-after.jsonl": {
        "sha256": "7b29c543492904cd1121c047d3fce125c4b0a16f1adc76240c80d6c091a15c89",
        "samples": 10
      },
      "docs/research/callkit-simulator-probe/livecommunicationkit/2026-10-21-iphone13-ios27-lck-two-managers.jsonl": {
        "sha256": "23ab443e1fd8623140e95cc7d34157d5fd8aa7d13b6321f90e1812825f65a042",
        "samples": 4
      }
    }
  },
  "pushReportDeadlineSeconds": 4.4,
  "pushReportDeadlineEvidence": {
    "measured": "Approximate 4.4 s termination and PKPushRegistry crash attribution from the device pass writeup; raw late-5s trace ends alive at4.200 s without a kill event.",
    "reference": "docs/research/callkit-simulator-probe/livecommunicationkit/push/README.md",
    "captures": {
      "docs/research/callkit-simulator-probe/livecommunicationkit/push/2026-10-23-iphone13-ios27-late-report-3s.jsonl": "1ed65280b43ebe27ab8b52535bb7b1df15b301e4eb9ab6d690cc9c8690ef36cf",
      "docs/research/callkit-simulator-probe/livecommunicationkit/push/2026-10-23-iphone13-ios27-late-report-5s.jsonl": "65aa587060ac3a2e5a898d97675e0c902dba70d7c8bf0b2d7dad0ab8ad29da2c"
    }
  }
}
"""#
  #endif

  private static func profileURL(_ profileID: String) throws -> URL {
    #if SWIFT_PACKAGE
    let resourceName = "VoIPSDK_CKSimRuntime"
    #else
    let resourceName = "IosVoipSDKRuntime"
    #endif
    // SwiftPM's generated Bundle.module accessor traps when its bundle is
    // absent. Resolve the same app/framework locations without that trap.
    var containers = [Bundle.main.resourceURL, Bundle(for: RuntimeEngine.self).resourceURL, Bundle.main.bundleURL]
    #if SWIFT_PACKAGE && DEBUG
    if let override = ProcessInfo.processInfo.environment["PACKAGE_RESOURCE_BUNDLE_PATH"] ??
        ProcessInfo.processInfo.environment["PACKAGE_RESOURCE_BUNDLE_URL"] {
      containers.insert(URL(fileURLWithPath: override), at: 0)
    }
    #endif
    for container in containers {
      if let resource = container?.appendingPathComponent(resourceName + ".bundle"),
         let resources = Bundle(url: resource), let url = resources.url(forResource: profileID, withExtension: "json") {
        return url
      }
    }
    #if SWIFT_PACKAGE
    throw BootstrapError.invalid("missing bundled behavior profile")
    #else
    // The standalone dylib keeps the historical runtime-dist layout.
    return URL(fileURLWithPath: CKSimResourceDirectory()).appendingPathComponent("profiles/\(profileID).json")
    #endif
  }

  private func shutdown() {
    withState {
      guard !stopped else { return }
      stopped = true
      audioRevision += 1
      for timer in timers.values { timer.setEventHandler(handler: nil); timer.cancel() }
      timers.removeAll()
      for provider in providers.values { provider.valid = false; provider.generation += 1 }
      observers.removeAll()
      pendingActions.removeAll()
      calls.removeAll()
      if notificationRegistered { notify_cancel(notificationToken); notificationRegistered = false }
      try? journal?.close()
      journal = nil
      let session = controlRoot.deletingLastPathComponent().appendingPathComponent("session.json")
      if let data = try? Data(contentsOf: session),
         let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
         object["runID"] as? String == runID, object["pid"] as? Int == Int(getpid()) {
        try? FileManager.default.removeItem(at: session)
      }
    }
  }

  private func withState<T>(_ body: () -> T) -> T {
    if DispatchQueue.getSpecific(key: stateKey) == true { return body() }
    return state.sync(execute: body)
  }
  private func after(_ seconds: Double, from start: DispatchTime = .now(), _ body: @escaping @Sendable () -> Void) {
    guard !stopped else { return }
    let id = UUID()
    let timer = DispatchSource.makeTimerSource(flags: .strict, queue: state)
    timers[id] = timer
    timer.schedule(deadline: start + seconds, leeway: .milliseconds(1))
    timer.setEventHandler { [weak self] in
      guard let self, !self.stopped else { return }
      let fired = self.timers.removeValue(forKey: id)
      fired?.setEventHandler(handler: nil)
      fired?.cancel()
      body()
    }
    timer.resume()
  }
  func record(event: String, fields: [String: Any]) {
    withState {
      guard !stopped else { return }
      let observed = DispatchTime.now()
      var row = fields
      var pushViolation: String?
      var pushDeadline: (String, UUID, DispatchTime)?
      if let pushID = fields["pushID"] as? String, !pushID.isEmpty {
        switch event {
        case "push.received":
          let required = fields["mustReport"] as? Bool ?? true
          if !pushReportingObligations.receive(pushID, required: required) {
            pushViolation = "repeated-delivery"
          } else if required, let token = pushReportingObligations.deadlineToken(pushID) {
            pushDeadline = (pushID, token, observed)
          }
        case "report.incoming": pushReportingObligations.entered(pushID)
        case "push.completed", "push.callbackReturned":
          let observation = event == "push.completed" ? pushReportingObligations.completed(pushID) : pushReportingObligations.returned(pushID)
          row["mustReport"] = observation.required
          row["reportEntered"] = observation.reportEntered
          pushViolation = observation.violation
          if event == "push.completed" { incomingPushAssociations.complete(pushID: pushID) }
        default: break
        }
      }
      sequence += 1
      row.merge(["schemaVersion": 1, "event": event, "sequence": sequence, "runID": runID, "pid": Int(getpid()),
                 "bundleID": bundleID, "profile": profile.id,
                 "wallTimeMS": Int64(Date().timeIntervalSince1970 * 1000),
                 "t_ms": (observed.uptimeNanoseconds - epoch) / 1_000_000]) { _, new in new }
      if let data = try? JSONSerialization.data(withJSONObject: row, options: [.sortedKeys]) {
        journal?.write(data + Data([10]))
      }
      if let (pushID, token, receivedAt) = pushDeadline {
        record(event: "push.reportDeadline", fields: ["pushID": pushID, "seconds": profile.pushReportDeadlineSeconds])
        after(profile.pushReportDeadlineSeconds, from: receivedAt) { [weak self] in
          guard let self, self.pushReportingObligations.expire(pushID, token: token) else { return }
          self.record(event: "diagnostic.pushReportExpired", fields: ["pushID": pushID, "severity": "error",
            "seconds": self.profile.pushReportDeadlineSeconds, "exitCode": 78])
          Darwin._exit(78)
        }
      }
      if let pushViolation {
        record(event: "diagnostic.pushReportingViolation", fields: ["severity": "error", "reason": pushViolation,
          "pushID": fields["pushID"] as? String ?? ""])
      }
    }
  }
  private func registration(_ provider: CXProvider) -> ProviderRegistration {
    let key = ObjectIdentifier(provider)
    if let value = providers[key] { return value }
    let value = ProviderRegistration(provider); providers[key] = value; return value
  }
  func register(provider: CXProvider) {
    withState {
      if selected != nil { record(event: "diagnostic.multipleProviders", fields: ["severity": "warning"]) }
      let entry = registration(provider)
      selected = entry
      let generation = entry.generation
      after(0.05) { if entry.valid && entry.generation == generation { self.callback(entry) { $0.providerDidBegin?(provider) } } }
      record(event: "provider.created", fields: ["provider": providerID(provider)])
    }
  }
  private func providerID(_ provider: CXProvider) -> String { String(describing: ObjectIdentifier(provider)) }
  private func callback(_ provider: ProviderRegistration, _ block: @escaping @Sendable (any CXProviderDelegate) -> Void) {
    guard let delegate = provider.delegate else { return }
    let generation = provider.generation
    (provider.queue ?? .main).async {
      guard self.withState({ !self.stopped && provider.valid && provider.generation == generation }) else { return }
      block(delegate)
    }
  }
  func set(provider: CXProvider, delegate: (any CXProviderDelegate)?, queue: DispatchQueue?) {
    withState {
      let value = registration(provider)
      value.delegate = delegate; value.queue = queue
      value.apiKind = CKSimDelegateUsesLiveCommunicationKit(delegate) ? .livecommunicationkit : .callkit
      record(event: "provider.apiKind", fields: ["provider": providerID(provider),
        "apiKind": value.apiKind.rawValue, "delegatePresent": delegate != nil])
    }
  }
  func register(controller: CXCallController, queue: DispatchQueue?) {
    withState { controllerQueues.setObject(queue ?? DispatchQueue(label: "dev.callkitsim.controller"), forKey: controller) }
  }
  func set(observer: CXCallObserver, delegate: (any CXCallObserverDelegate)?, queue: DispatchQueue?) {
    withState { observers[ObjectIdentifier(observer)] = ObserverRegistration(observer, delegate, queue) }
  }
  func liveCalls() -> [CXCall] { withState { calls.values.sorted { $0.id.uuidString < $1.id.uuidString }.map { $0.snapshot() } } }
  private func changed(_ call: CallState, notifyObservers: Bool = true) {
    let snapshot = call.snapshot()
    record(event: "call.changed", fields: callJSON(call))
    guard notifyObservers else { return }
    observers = observers.filter { $0.value.observer != nil }
    for entry in observers.values {
      guard let observer = entry.observer, let delegate = entry.delegate else { continue }
      entry.queue.async {
        guard self.withState({ !self.stopped }) else { return }
        delegate.callObserver(observer, callChanged: snapshot)
      }
    }
  }
  private func callJSON(_ call: CallState) -> [String: Any] {
    ["uuid": call.id.uuidString, "outgoing": call.outgoing, "connected": call.connected,
     "held": call.held, "ended": call.ended, "muted": call.muted, "group": call.group.uuidString,
     "ringing": call.ringing, "callerName": call.update?.localizedCallerName ?? "",
     "handle": call.update?.remoteHandle?.value ?? ""]
  }
  private func actionFields(_ item: RuntimePendingAction) -> [String: Any] {
    let timing = profile.timing(api: item.apiKind, answer: item.action is CXAnswerCallAction, start: item.action is CXStartCallAction)
    let kind: String
    switch item.action {
    case is CXAnswerCallAction: kind = "answer"
    case is CXStartCallAction: kind = "start"
    case is CXEndCallAction: kind = "end"
    case is CXSetHeldCallAction: kind = "hold"
    case is CXSetMutedCallAction: kind = "mute"
    case is CXPlayDTMFCallAction: kind = "tones"
    case is CXSetGroupCallAction: kind = "group"
    default: kind = NSStringFromClass(type(of: item.action))
    }
    return ["actionID": item.action.uuid.uuidString, "kind": kind,
      "callUUID": (item.action as? CXCallAction)?.callUUID.uuidString ?? "",
      "transactionID": item.transaction.uuid.uuidString, "origin": item.origin.rawValue,
      "provider": providerID(item.provider.provider), "apiKind": item.apiKind.rawValue,
      "issuedAtMS": Int64(item.issuedAt.timeIntervalSince1970 * 1000),
      "deadlineMS": Int64(item.issuedAt.addingTimeInterval(timing.deadline).timeIntervalSince1970 * 1000),
      "deadlineSeconds": timing.deadline, "expirySeconds": timing.expiry,
      "latencyMS": Double(DispatchTime.now().uptimeNanoseconds - item.issuedUptime) / 1_000_000]
  }
  private func requestError(_ code: Int) -> NSError {
    NSError(domain: "com.apple.CallKit.error.requesttransaction", code: code)
  }
  private func validate(_ actions: [CXAction], origin: ActionOrigin) -> NSError? {
    if actions.isEmpty { return requestError(1) }
    var starts = Set<UUID>()
    for action in actions {
      guard let action = action as? CXCallAction else { return requestError(6) }
      if action is CXStartCallAction {
        if calls[action.callUUID] != nil || !starts.insert(action.callUUID).inserted { return requestError(5) }
      } else {
        guard calls[action.callUUID] != nil else { return requestError(4) }
        if action is CXAnswerCallAction && origin == .application && calls.values.contains(where: { $0.id != action.callUUID && !$0.ended }) {
          return requestError(6)
        }
        if let group = action as? CXSetGroupCallAction, let other = group.callUUIDToGroupWith, calls[other] == nil {
          return requestError(4)
        }
      }
    }
    return nil
  }
  func request(transaction: CXTransaction, controller: CXCallController, completion: @escaping (Error?) -> Void) {
    let reply = Completion(completion)
    let actions = transaction.actions.map { $0.copy() as! CXAction }
    let requestID = transaction.uuid
    withState {
      let queue = controllerQueues.object(forKey: controller) ?? DispatchQueue.global(qos: .userInitiated)
      guard let provider = selected, provider.valid else {
        record(event: "request.accepted", fields: ["transactionID": requestID.uuidString,
          "actionIDs": actions.map { $0.uuid.uuidString }, "error": 2])
        after(profile.unknownProviderSeconds) { queue.async {
          guard self.withState({ !self.stopped }) else { return }
          reply.invoke(self.requestError(2))
        } }
        return
      }
      let generation = provider.generation
      let error = validate(actions, origin: .application)
      record(event: "request.accepted", fields: ["transactionID": requestID.uuidString,
        "actionIDs": actions.map { $0.uuid.uuidString }, "error": error?.code ?? 0])
      if error == nil { reserveStarts(actions, provider) }
      var requestedCalls: [UUID: CallState] = [:]
      for case let action as CXCallAction in actions {
        requestedCalls[action.callUUID] = calls[action.callUUID]
        if let group = action as? CXSetGroupCallAction, let target = group.callUUIDToGroupWith {
          requestedCalls[target] = calls[target]
        }
      }
      let bindings = requestedCalls
      // The continuation is scheduled only AFTER the client's completion returns.
      // No wall-clock timer, no app callback while executing the state queue.
      queue.async {
        guard self.withState({ !self.stopped }) else { return }
        reply.invoke(error)
        guard error == nil else { return }
        self.state.async {
          guard !self.stopped, bindings.allSatisfy({ self.calls[$0.key] === $0.value }) else { return }
          self.deliver(actions, provider, origin: .application, requestID: requestID, generation: generation)
        }
      }
    }
  }
  private func reserveStarts(_ actions: [CXAction], _ provider: ProviderRegistration) {
    for case let start as CXStartCallAction in actions {
      let call = CallState(start.callUUID, provider)
      call.outgoing = true; call.ringing = false; calls[call.id] = call; changed(call)
    }
  }
  private func deliver(_ actions: [CXAction], _ provider: ProviderRegistration, origin: ActionOrigin, requestID: UUID? = nil, generation: UInt64? = nil) {
    guard provider.valid && (generation == nil || provider.generation == generation) else { return }
    // Creation with the same concrete actions intentionally yields a fresh transaction UUID.
    let delivered = CXTransaction(actions: actions)
    record(event: "transaction.delivered", fields: ["transactionID": delivered.uuid.uuidString,
      "requestID": requestID?.uuidString ?? "system", "origin": origin.rawValue,
      "provider": providerID(provider.provider), "actionIDs": actions.map { $0.uuid.uuidString },
      "callUUIDs": actions.map { ($0 as? CXCallAction)?.callUUID.uuidString ?? "" },
      "parameters": actions.map { action -> [String: Any] in
        if let hold = action as? CXSetHeldCallAction { return ["held": hold.isOnHold] }
        if let mute = action as? CXSetMutedCallAction { return ["muted": mute.isMuted] }
        if let tones = action as? CXPlayDTMFCallAction { return ["digits": tones.digits] }
        return [:]
      },
      "kinds": actions.map { NSStringFromClass(type(of: $0)) }])
    for action in actions {
      let timing = profile.timing(api: provider.apiKind,
        answer: action is CXAnswerCallAction, start: action is CXStartCallAction)
      let pending = RuntimePendingAction(action, provider, delivered, origin,
        call: (action as? CXCallAction).flatMap { calls[$0.callUUID] },
        groupTarget: (action as? CXSetGroupCallAction)?.callUUIDToGroupWith.flatMap { calls[$0] })
      let issued = pending.issuedAt
      let deadline = issued.addingTimeInterval(timing.deadline)
      deadlines.setObject(deadline as NSDate, forKey: action)
      record(event: "action.deadline", fields: actionFields(pending))
      if let answer = action as? CXAnswerCallAction, let call = calls[answer.callUUID] {
        call.ringing = false; changed(call, notifyObservers: false)
      }
      pendingActions[action.uuid] = pending
      after(max(0, issued.addingTimeInterval(timing.expiry).timeIntervalSinceNow)) { [weak self, weak pending] in
        guard let self, let pending, self.pendingActions[action.uuid] === pending, !pending.expired else { return }
        self.expire(pending)
      }
    }
    callback(provider) { CKSimPerform($0, provider.provider, delivered) }
  }
  private func expire(_ item: RuntimePendingAction) {
    record(event: "action.expired", fields: actionFields(item))
    if let answer = item.action as? CXAnswerCallAction {
      item.expired = true
      if let call = item.call, calls[answer.callUUID] === call {
        deliver([CXEndCallAction(call: answer.callUUID)], item.provider, origin: .system, generation: item.generation)
      }
      return
    }
    pendingActions.removeValue(forKey: item.action.uuid)
    record(event: "action.timeout", fields: actionFields(item))
    callback(item.provider) { $0.provider?(item.provider.provider, timedOutPerforming: item.action) }
    if let call = item.call, calls[call.id] === call, item.action is CXEndCallAction {
      endCall(call.id, provider: item.provider)
    }
  }
  func complete(action: CXAction, fulfilled: Bool) -> Bool {
    withState {
      guard let item = pendingActions[action.uuid] else { return false }
      if item.settled { return true }
      item.settled = true
      var fields = actionFields(item)
      fields["fulfilled"] = fulfilled
      fields["expired"] = item.expired
      record(event: "action.settled", fields: fields)
      if item.origin == .postEndUnmute {
        // CallKit's fulfilled echo still times out on the reference device.
        // LCK reports successful completion; do not manufacture the CX timeout.
        if fulfilled && profile.fulfilledPostEndUnmutePolicy.settlement(for: item.apiKind) == .complete {
          pendingActions.removeValue(forKey: action.uuid)
        }
        return true
      }
      if item.expired { pendingActions.removeValue(forKey: action.uuid); return true }
      guard let call = item.call, calls[call.id] === call else {
        // A system end can arrive after an answer failure already removed the
        // call. Fulfillment is ignored, and the action's own timer still fires.
        return true
      }
      pendingActions.removeValue(forKey: action.uuid)
      if !fulfilled {
        if action is CXAnswerCallAction {
          after(0.29) {
            guard item.provider.valid, item.provider.generation == item.generation, self.calls[call.id] === call else { return }
            self.endCall(call.id, provider: item.provider)
          }
          after(0.3) {
            // The measured failed-answer end can arrive after removal, but it
            // must never act on a different call reusing the same UUID.
            guard self.calls[call.id] == nil || self.calls[call.id] === call else { return }
            self.deliver([CXEndCallAction(call: call.id)], item.provider, origin: .system, generation: item.generation)
          }
        } else if action is CXStartCallAction { endCall(call.id, provider: item.provider) }
        return true
      }
      switch action {
      case is CXAnswerCallAction:
        call.connected = true; call.ringing = false; changed(call); reconcileAudio(provider: item.provider, delay: 0.29)
      case is CXStartCallAction:
        reconcileAudio(provider: item.provider, delay: 0.23)
      case is CXEndCallAction:
        endCall(call.id, provider: item.provider)
      case let hold as CXSetHeldCallAction:
        call.held = hold.isOnHold; changed(call); reconcileAudio(provider: item.provider, delay: hold.isOnHold ? 0.09 : 0.18)
      case let mute as CXSetMutedCallAction:
        call.muted = mute.isMuted; changed(call, notifyObservers: false)
        if item.origin == .application {
          after(profile.muteEchoSeconds) {
            guard self.calls[call.id] === call else { return }
            self.deliver([CXSetMutedCallAction(call: call.id, muted: mute.isMuted)], item.provider, origin: .muteEcho, generation: item.generation)
          }
        }
      case let group as CXSetGroupCallAction:
        if let other = group.callUUIDToGroupWith {
          guard let target = item.groupTarget, calls[other] === target else { return true }
          call.group = target.group
        }
        else { call.group = call.id }
        record(event: "call.grouped", fields: callJSON(call))
      default: break
      }
      return true
    }
  }
  private func endCall(_ uuid: UUID, provider: ProviderRegistration) {
    guard let call = calls.removeValue(forKey: uuid) else { return }
    call.ended = true; call.ringing = false; changed(call)
    reconcileAudio(provider: provider, delay: calls.isEmpty ? 0.1 : 1.15)
    if call.muted && provider.valid {
      let generation = provider.generation
      after(profile.postEndUnmuteSeconds) {
        guard self.calls[uuid] == nil else { return }
        self.deliver([CXSetMutedCallAction(call: uuid, muted: false)], provider, origin: .postEndUnmute, generation: generation)
      }
    }
  }
  func incoming(provider: CXProvider, uuid: UUID, update: CXCallUpdate, completion: @escaping (Error?) -> Void) {
    let reply = Completion(completion)
    let synchronousPushID = CKSimCurrentPushID()
    withState {
      let pushID: String
      switch incomingPushAssociations.consume(uuid) {
      case .absent: pushID = synchronousPushID
      case .associated(let identifier): pushID = identifier
      case .ambiguous: pushID = ""
      }
      let owner = registration(provider)
      if !owner.valid {
        owner.generation += 1; owner.valid = true
        callback(owner) { $0.providerDidBegin?(provider) }
      }
      record(event: "report.incoming", fields: ["callUUID": uuid.uuidString, "pushID": pushID])
      let error: NSError? = calls[uuid] == nil ? nil : NSError(domain: "com.apple.CallKit.error.incomingcall", code: 2)
      if error == nil {
        let call = CallState(uuid, owner); call.update = update.copy() as? CXCallUpdate; call.pushID = pushID
        calls[uuid] = call; changed(call)
        after(profile.ringTimeoutSeconds) {
          guard self.calls[uuid] === call, call.ringing else { return }
          self.deliver([CXEndCallAction(call: uuid)], self.selected ?? owner, origin: .system)
        }
      }
      let delay = (ProcessInfo.processInfo.environment["CKSIM_LATENCY"] == "device" ? profile.reportLatencySeconds : 0.02)
        + Double(faults.reportCallbackDelayMS) / 1000
      after(delay) {
        (owner.queue ?? .main).async {
          guard self.withState({ !self.stopped }) else { return }
          self.record(event: "report.completed", fields: ["callUUID": uuid.uuidString, "pushID": pushID, "error": error?.code ?? 0])
          reply.invoke(error)
        }
      }
    }
  }
  func associateIncomingReport(_ id: UUID, pushID: String) {
    withState {
      guard !stopped, !pushID.isEmpty else { return }
      if !incomingPushAssociations.associate(id, pushID: pushID) {
        record(event: "diagnostic.ambiguousIncomingPush", fields: ["callUUID": id.uuidString])
      }
    }
  }
  func finishIncomingReport(_ id: UUID, pushID: String) {
    withState {
      guard !stopped else { return }
      incomingPushAssociations.finish(id, pushID: pushID)
    }
  }
  func updated(provider: CXProvider, uuid: UUID, update: CXCallUpdate) {
    withState {
      record(event: "report.updated", fields: ["callUUID": uuid.uuidString])
      if let call = calls[uuid] { call.update = update.copy() as? CXCallUpdate; changed(call, notifyObservers: false) }
    }
  }
  func ended(provider: CXProvider, uuid: UUID, reason: Int) {
    withState {
      record(event: "report.ended", fields: ["callUUID": uuid.uuidString, "reason": reason, "pushID": calls[uuid]?.pushID ?? ""])
      endCall(uuid, provider: selected ?? registration(provider))
    }
  }
  func connected(provider: CXProvider, uuid: UUID) {
    withState {
      record(event: "report.connected", fields: ["callUUID": uuid.uuidString])
      guard let call = calls[uuid] else { return }
      call.connected = true; changed(call)
    }
  }
  func pending(provider: CXProvider) -> [CXTransaction] {
    withState {
      var seen = Set<UUID>()
      return pendingActions.values.filter { !$0.settled && $0.provider.provider === provider && seen.insert($0.transaction.uuid).inserted }.map(\.transaction)
    }
  }
  func deadline(action: CXAction) -> Date? { withState { deadlines.object(forKey: action) as Date? } }
  func pendingActions(provider: CXProvider, actionClass: AnyClass, uuid: UUID) -> [CXCallAction] {
    withState {
      pendingActions.values.filter { !$0.settled && $0.provider.provider === provider }
        .compactMap { $0.action as? CXCallAction }.filter { $0.isKind(of: actionClass) && $0.callUUID == uuid }
    }
  }
  func invalidate(provider: CXProvider) {
    withState {
      let value = registration(provider); value.valid = false; value.generation += 1
      handoffPending = false
      audioRevision += 1
      for call in Array(calls.values) where call.reporter === value { endCall(call.id, provider: value) }
      for item in pendingActions.values where item.provider === value {
        record(event: "action.cancelled", fields: actionFields(item))
      }
      pendingActions = pendingActions.filter { $0.value.provider !== value }
      reconcileAudio(provider: value, delay: 0)
      record(event: "provider.invalidated", fields: ["provider": providerID(provider)])
    }
  }
  private func wantsAudio() -> Bool { calls.values.contains { !$0.held && ($0.connected || $0.outgoing) } }
  private func reconcileAudio(provider: ProviderRegistration, delay: Double) {
    if handoffPending { return }
    audioRevision += 1
    let revision = audioRevision
    let desired = wantsAudio()
    after(delay) {
      guard self.audioRevision == revision else { return }
      self.changeAudio(desired, provider: provider, revision: revision)
    }
  }
  private func changeAudio(_ desired: Bool, provider: ProviderRegistration, revision: UInt64) {
    guard audioRevision == revision else { return }
    guard desired != audioActive else {
      if !desired && handoffPending {
        handoffPending = false
        reconcileAudio(provider: provider, delay: 0.15)
      }
      return
    }
    if desired && faults.staleAudio { record(event: "fault.audioSuppressed", fields: [:]); return }
    DispatchQueue.main.async {
      guard self.withState({ !self.stopped && self.audioRevision == revision }) else { return }
      do {
        try AVAudioSession.sharedInstance().setActive(desired, options: desired ? [] : [.notifyOthersOnDeactivation])
      } catch {
        self.withState {
          self.record(event: "audio.failed", fields: ["activating": desired])
          self.handoffPending = false
        }
        return
      }
      self.withState {
        self.audioActive = desired
        guard self.audioRevision == revision else {
          self.reconcileAudio(provider: self.selected ?? provider, delay: 0)
          return
        }
        self.record(event: desired ? "audio.activated" : "audio.deactivated", fields: [:])
        self.callback(provider) {
          if desired { $0.provider?(provider.provider, didActivate: AVAudioSession.sharedInstance()) }
          else { $0.provider?(provider.provider, didDeactivate: AVAudioSession.sharedInstance()) }
        }
        if !desired && self.handoffPending {
          self.handoffPending = false
          self.reconcileAudio(provider: provider, delay: 0.15)
        }
      }
    }
  }
  private func processCommands() {
    guard !stopped && CKSimRuntimeIsActive() else { return }
    let folder = controlRoot.appendingPathComponent("requests")
    guard let files = try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil) else { return }
    for file in files.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) where file.pathExtension == "json" {
      var remove = true
      defer { if remove { try? FileManager.default.removeItem(at: file) } }
      guard let bytes = try? Data(contentsOf: file), bytes.count <= 65_536,
            let command = try? JSONDecoder().decode(Command.self, from: bytes),
            command.runID == runID, command.pid == Int(getpid()), command.bundleID == bundleID,
            UUID(uuidString: file.deletingPathExtension().lastPathComponent) == command.id else { continue }
      let responseURL = controlRoot.appendingPathComponent("responses/\(command.id.uuidString).json")
      if FileManager.default.fileExists(atPath: responseURL.path) { continue }
      var response: [String: Any] = ["id": command.id.uuidString, "runID": runID, "pid": Int(getpid()), "bundleID": bundleID]
      if let cached = commandResults[command.id] { response = cached }
      else { do {
        let object = try JSONSerialization.jsonObject(with: bytes) as? [String: Any] ?? [:]
        response["result"] = try execute(command, object)
        response["success"] = true
      } catch {
        response["success"] = false; response["error"] = String(describing: error)
        record(event: "control.rejected", fields: ["requestID": command.id.uuidString,
          "operation": command.operation, "error": String(describing: error)])
      }
      commandResults[command.id] = response
      }
      do {
        let data = try JSONSerialization.data(withJSONObject: response, options: [.sortedKeys])
        try data.write(to: responseURL, options: .atomic)
      } catch {
        remove = false
        record(event: "control.responseWriteFailed", fields: ["commandID": command.id.uuidString])
      }
    }
  }
  private func execute(_ command: Command, _ object: [String: Any]) throws -> [String: Any] {
    switch command.operation {
    case "status": return ["calls": calls.values.map(callJSON), "pending": pendingActions.values.filter { !$0.settled }.count, "audioActive": audioActive, "sequence": sequence]
    case "events": return ["events": (try? String(contentsOf: controlRoot.appendingPathComponent("events.jsonl"), encoding: .utf8)) ?? ""]
    case "inspect-adapter":
      let name = "CallKitVoIPPushDelegate"
      let selector = NSSelectorFromString("pushRegistry:didReceiveIncomingVoIPPushWithPayload:metadata:withCompletionHandler:")
      guard let cls = NSClassFromString(name) else { throw ControlError.invalid("adapter class missing") }
      return ["metadataSelector": class_getInstanceMethod(cls, selector) != nil]
    case "faults":
      guard let value = command.faults, (0...120_000).contains(value.reportCallbackDelayMS) else { throw ControlError.invalid("invalid faults") }
      faults = value; return ["configured": true]
    case "push":
      guard let payload = object["payload"] as? [String: Any], let mustReport = command.mustReport else { throw ControlError.invalid("payload and explicit mustReport required") }
      let bytes = try JSONSerialization.data(withJSONObject: payload)
      guard bytes.count <= 5120 else { throw ControlError.invalid("payload too large") }
      CKSimInjectPush(payload, mustReport, command.id.uuidString)
      return ["scheduled": true, "pushID": command.id.uuidString]
    case "reset":
      // Explicit selected-provider fault. This does not model invalidation of
      // an older provider (R3) or change provider-selection behavior.
      guard let provider = selected, provider.valid else { throw ControlError.invalid("no selected provider") }
      guard !pendingActions.values.contains(where: { $0.provider === provider }) else {
        throw ControlError.invalid("selected-provider reset requires no pending runtime actions")
      }
      provider.generation += 1
      handoffPending = false
      let owned = calls.values.filter { $0.reporter === provider }
      for call in owned {
        calls.removeValue(forKey: call.id)
        call.ended = true; call.ringing = false; changed(call)
      }
      record(event: "provider.reset", fields: ["provider": providerID(provider.provider),
        "requestID": command.id.uuidString, "callUUIDs": owned.map { $0.id.uuidString }, "cancelledActionIDs": [String]()])
      reconcileAudio(provider: provider, delay: 0)
      callback(provider) { $0.providerDidReset(provider.provider) }
      return ["reset": true, "provider": providerID(provider.provider)]
    default:
      guard let provider = selected, provider.valid, let uuid = command.callUUID, calls[uuid] != nil else { throw ControlError.invalid("unknown call or provider") }
      let actions: [CXAction]
      switch command.operation {
      case "answer": actions = [CXAnswerCallAction(call: uuid)]
      case "decline", "end": actions = [CXEndCallAction(call: uuid)]
      case "hold":
        guard let held = command.held else { throw ControlError.invalid("held required") }
        actions = [CXSetHeldCallAction(call: uuid, onHold: held)]
      case "mute":
        guard let muted = command.muted else { throw ControlError.invalid("muted required") }
        actions = [CXSetMutedCallAction(call: uuid, muted: muted)]
      case "tones":
        guard let digits = command.digits, (1...64).contains(digits.count),
              digits.allSatisfy({ "0123456789*#ABCDabcd".contains($0) }) else {
          throw ControlError.invalid("invalid DTMF digits")
        }
        actions = [CXPlayDTMFCallAction(call: uuid, digits: digits, type: .singleTone)]
      case "hold-and-accept", "end-and-accept":
        guard let active = command.activeCallUUID, active != uuid, calls[active]?.connected == true else { throw ControlError.invalid("activeCallUUID required") }
        actions = [command.operation == "hold-and-accept" ? CXSetHeldCallAction(call: active, onHold: true) : CXEndCallAction(call: active), CXAnswerCallAction(call: uuid)]
      default: throw ControlError.invalid("unsupported operation")
      }
      if let error = validate(actions, origin: .system) { throw error }
      if command.operation == "hold-and-accept" || command.operation == "end-and-accept" {
        // The old call relinquishes audio before the new call acquires it.
        handoffPending = true
        audioRevision += 1
        let revision = audioRevision
        after(0.12) { self.changeAudio(false, provider: provider, revision: revision) }
      }
      deliver(actions, provider, origin: .system, requestID: command.id)
      if actions.count == 2 && faults.invalidateDuringHandoff == true { provider.provider.invalidate() }
      return ["actionIDs": actions.map { $0.uuid.uuidString }]
    }
  }
}
private enum ControlError: Error { case invalid(String) }

@_cdecl("CKSimGetEngine")
func getRuntimeEngine() -> UnsafeMutableRawPointer? {
  RuntimeEngine.existing().map { Unmanaged.passRetained($0).toOpaque() }
}

@_cdecl("CKSimPrepareEngine")
func prepareRuntimeEngine() -> Bool {
  do { try RuntimeEngine.prepare(); return true }
  catch { NSLog("[ios-voip-sdk] simulator activation refused: %@", String(describing: error)); return false }
}

@_cdecl("CKSimReleaseEngine")
func releaseRuntimeEngine() { RuntimeEngine.releasePrepared() }

/// Shared bootstrap for the Swift package and the Expo pod's unified module.
@MainActor
public enum RuntimeBootstrap {
  @discardableResult
  public static func activate() -> Bool { CKSimActivateRuntime() }

  #if DEBUG || VSSDK_ENABLE_PROVIDER_DIAGNOSTICS
  /// SDK diagnostic integration; does not replace or relax verified runtime hooks.
  public static func installProviderCreationObserver(_ observer: @escaping (Any) -> Void) -> Bool {
    CKSimInstallProviderCreationObserver(observer)
  }
  #endif

  public nonisolated static var currentPushIdentifier: String? {
    guard CKSimRuntimeIsActive() else { return nil }
    let identifier = CKSimCurrentPushID()
    return identifier.isEmpty ? nil : identifier
  }

  public nonisolated static func associateIncomingReport(_ id: UUID, pushID: String) {
    guard CKSimRuntimeIsActive() else { return }
    RuntimeEngine.existing()?.associateIncomingReport(id, pushID: pushID)
  }

  public nonisolated static func finishIncomingReport(_ id: UUID, pushID: String) {
    guard CKSimRuntimeIsActive() else { return }
    RuntimeEngine.existing()?.finishIncomingReport(id, pushID: pushID)
  }
}

private final class RuntimeStorage: @unchecked Sendable {
  let lock = NSLock()
  var engine: RuntimeEngine?
}
private enum BootstrapError: Error { case invalid(String) }
#endif
