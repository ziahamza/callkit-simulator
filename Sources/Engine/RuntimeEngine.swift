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
private enum IncomingFilterReason: String, CaseIterable, Sendable {
  case none
  case doNotDisturb = "do-not-disturb"
  case restrictedSharingMode = "restricted-sharing-mode"
  case callIsProtected = "call-is-protected"
  case sensitiveParticipants = "sensitive-participants"
  var code: Int {
    switch self {
    case .none: 0
    case .doNotDisturb: 3
    case .restrictedSharingMode: 5
    case .callIsProtected: 6
    case .sensitiveParticipants: 7
    }
  }
  func snapshot(policy: IncomingFilterPolicy?) -> [String: Any] {
    ["reason": rawValue, "code": code, "verification": policy?.verification ?? "not-applicable"]
  }
}
private struct IncomingFilterPolicy: Decodable, Sendable {
  let code: Int
  let delaySeconds: Double
  let verification: String
  let evidence: [String]
}
// Immutable per-report decision: later filter/fault commands cannot change an
// entered report. Duplicate UUID errors retain their existing code-2 priority.
private struct IncomingReportOutcome: Sendable {
  let code: Int
  let domain: String
  let filterReason: String
  let verification: String
  let delaySeconds: Double
  var error: NSError? { code == 0 ? nil : NSError(domain: domain, code: code) }
  init(reason: IncomingFilterReason, duplicate: Bool, policy: IncomingFilterPolicy?, normalDelaySeconds: Double, extraDelaySeconds: Double, incomingErrorCode: Int = 0) {
    if duplicate {
      code = 2; filterReason = "none"; verification = "not-applicable"
      delaySeconds = normalDelaySeconds + extraDelaySeconds
    } else if incomingErrorCode == 4 {
      code = 4; filterReason = "none"; verification = "injected-fault"
      delaySeconds = normalDelaySeconds + extraDelaySeconds
    } else if reason != .none, let policy {
      code = reason.code; filterReason = reason.rawValue; verification = policy.verification
      delaySeconds = policy.delaySeconds + extraDelaySeconds
    } else {
      code = 0; filterReason = "none"; verification = "not-applicable"
      delaySeconds = normalDelaySeconds + extraDelaySeconds
    }
    domain = code == 0 ? "" : "com.apple.CallKit.error.incomingcall"
  }
}
// iPhone 13 / iOS 27.0, #85. LCK exposes no group membership or transaction IDs.
private struct LCKMultiCallPolicy {
  enum Kind { case join, pause, resume, end, merge, other }
  enum Failure { case endWithCleanup, endRegardless, none }
  static func failure(_ kind: Kind) -> Failure {
    switch kind { case .join, .pause: .endWithCleanup; case .end: .endRegardless; default: .none }
  }
  static func choices(established: Int, held: Int, active: Int, maximumGroups: Int) -> [String] {
    if established == 0 { return ["answer", "decline"] }
    guard active == 1 else { return [] } // This configuration was not measured.
    var result = ["end-and-accept", "decline"]
    if maximumGroups >= 2 && established < maximumGroups { result.insert("hold-and-accept", at: 0) }
    if held > 0 { result.append("end-held-and-accept") }
    return result
  }
  static func canStart(established: Int, reservedStarts: Int, maximumGroups: Int) -> Bool {
    established + reservedStarts < maximumGroups
  }
  static func shouldEndActiveOnResume(maximumGroups: Int, maximumPerGroup: Int) -> Bool {
    maximumGroups == 2 && maximumPerGroup == 1
  }
  static func resumeEndDelay(nominal: Double, elapsedSinceIssued: Double) -> Double {
    max(0, nominal - elapsedSinceIssued)
  }
  static func usesCompositeBarrier(system: Bool, actionCount: Int, joiningCount: Int) -> Bool {
    system && actionCount > 1 && joiningCount > 0
  }
  struct Handoff {
    private var gapElapsed = false
    private var joined = false
    mutating func markGapElapsed() -> Bool { gapElapsed = true; return joined }
    mutating func markJoined() -> Bool { joined = true; return gapElapsed }
  }
  struct Barrier {
    private var remaining: Set<UUID>
    private let joining: [UUID: UUID]
    private var failed = Set<UUID>()
    init(actions: [UUID], joining: [UUID: UUID]) { remaining = Set(actions); self.joining = joining }
    mutating func settle(_ action: UUID, fulfilled: Bool) -> [UUID]? {
      guard remaining.remove(action) != nil else { return nil }
      if !fulfilled { failed.insert(action) }
      guard remaining.isEmpty else { return nil }
      return joining.filter { !failed.contains($0.key) }.map(\.value)
    }
  }
}
private struct LCKMultiCallProfile: Decodable {
  let pauseEndExpirySeconds: Double
  let joinExpirySeconds: Double
  let cleanupEndExpirySeconds: Double
  let pausedAfterDeliverySeconds: Double
  let joinedAfterSettlementSeconds: Double
  let audioHandoffGapSeconds: Double
  let audioDeactivationAfterDeliverySeconds: Double
  let appResumeEndDelaySeconds: Double
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
  let lckMultiCall: LCKMultiCallProfile
  let reportLatencySeconds: Double
  let pushReportDeadlineSeconds: Double
  let incomingFilterPolicies: [String: IncomingFilterPolicy]
  let muteEchoSeconds: Double
  let postEndUnmuteSeconds: Double
  let fulfilledPostEndUnmutePolicy: FulfilledPostEndUnmutePolicy

  func timing(api: ProviderAPIKind, answer: Bool, start: Bool, pauseOrEnd: Bool = false) -> (deadline: Double, expiry: Double) {
    if api == .livecommunicationkit {
      return (lckActionDeadlineSeconds, answer ? lckMultiCall.joinExpirySeconds :
        pauseOrEnd ? lckMultiCall.pauseEndExpirySeconds : lckActionDeadlineSeconds)
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
  var holdRevision: UInt64 = 0
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

private enum ActionOrigin: String { case application, system, muteEcho, postEndUnmute, failureCleanup }
private final class LCKRuntimeComposite: @unchecked Sendable {
  var barrier: LCKMultiCallPolicy.Barrier
  let joining: [UUID: CallState]
  let joiningRevisions: [UUID: UInt64]
  let systemComposite: Bool
  init(_ actions: [CXAction], calls: [UUID: CallState], origin: ActionOrigin) {
    var joiningActions: [UUID: UUID] = [:]
    var bindings: [UUID: CallState] = [:]
    for case let action as CXCallAction in actions where action is CXAnswerCallAction || (action as? CXSetHeldCallAction)?.isOnHold == false {
      if let call = calls[action.callUUID] { joiningActions[action.uuid] = call.id; bindings[call.id] = call }
    }
    barrier = LCKMultiCallPolicy.Barrier(actions: actions.map(\.uuid), joining: joiningActions)
    joining = bindings; joiningRevisions = bindings.mapValues(\.holdRevision)
    systemComposite = LCKMultiCallPolicy.usesCompositeBarrier(system: origin == .system,
      actionCount: actions.count, joiningCount: bindings.count)
  }
}
private final class RuntimePendingAction: @unchecked Sendable {
  let action: CXAction
  let provider: ProviderRegistration
  let transaction: CXTransaction
  let origin: ActionOrigin
  let apiKind: ProviderAPIKind
  let generation: UInt64
  let call: CallState?
  let groupTarget: CallState?
  let lckComposite: LCKRuntimeComposite?
  let issuedAt = Date()
  let issuedUptime = DispatchTime.now().uptimeNanoseconds
  var expired = false
  var settled = false
  init(_ action: CXAction, _ provider: ProviderRegistration, _ transaction: CXTransaction, _ origin: ActionOrigin, call: CallState?, groupTarget: CallState?, lckComposite: LCKRuntimeComposite?) {
    self.action = action; self.provider = provider; self.transaction = transaction; self.origin = origin
    self.apiKind = provider.apiKind
    self.generation = provider.generation; self.call = call; self.groupTarget = groupTarget
    self.lckComposite = lckComposite
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
  var staleAudio: Bool?
  var reportCallbackDelayMS: Int?
  var invalidateDuringHandoff: Bool?
  var incomingErrorCode: Int?
  mutating func apply(_ update: Faults) {
    if let value = update.staleAudio { staleAudio = value }
    if let value = update.reportCallbackDelayMS { reportCallbackDelayMS = value }
    if let value = update.invalidateDuringHandoff { invalidateDuringHandoff = value }
    if let code = update.incomingErrorCode { incomingErrorCode = code }
  }
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
  var heldCallUUID: UUID?
  var groupWithCallUUID: UUID?
  var applicationRequest: Bool?
  var held: Bool?
  var muted: Bool?
  var digits: String?
  var mustReport: Bool?
  var faults: Faults?
  var reason: String?
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
  private var lckHandoff = LCKMultiCallPolicy.Handoff()
  private var faults = Faults()
  private var incomingFilter: IncomingFilterReason = .none
  private let runID: String
  private let bundleID: String
  private let controlRoot: URL
  private var journal: FileHandle?
  private var sequence: UInt64 = 0
  private let epoch = DispatchTime.now().uptimeNanoseconds
  private var notificationToken: Int32 = 0
  private var notificationRegistered = false
  private var requestWatcher: (any DispatchSourceFileSystemObject)?
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
      profile.ordinaryDeadlineSeconds, profile.lckActionDeadlineSeconds, profile.pushReportDeadlineSeconds, profile.reportLatencySeconds, profile.muteEchoSeconds, profile.postEndUnmuteSeconds,
      profile.lckMultiCall.pauseEndExpirySeconds, profile.lckMultiCall.joinExpirySeconds,
      profile.lckMultiCall.cleanupEndExpirySeconds, profile.lckMultiCall.pausedAfterDeliverySeconds,
      profile.lckMultiCall.joinedAfterSettlementSeconds, profile.lckMultiCall.audioHandoffGapSeconds,
      profile.lckMultiCall.audioDeactivationAfterDeliverySeconds, profile.lckMultiCall.appResumeEndDelaySeconds]
    guard profile.schemaVersion == 1 && profile.id == profileID && durations.allSatisfy({ $0.isFinite && $0 > 0 }) else {
      throw BootstrapError.invalid("invalid behavior profile")
    }
    let filteredReasons = IncomingFilterReason.allCases.filter { $0 != .none }
    let filterPolicies = profile.incomingFilterPolicies
    guard Set(filterPolicies.keys) == Set(filteredReasons.map(\.rawValue)),
      filteredReasons.allSatisfy({ reason in
        guard let policy = filterPolicies[reason.rawValue] else { return false }
        return policy.code == reason.code && policy.delaySeconds.isFinite && policy.delaySeconds > 0 &&
          policy.verification == (reason == .doNotDisturb ? "verified" : "unverified") && !policy.evidence.isEmpty
      }) else { throw BootstrapError.invalid("invalid incoming filter profile") }
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
      // The host can publish requests even when CoreSimulator's spawn service
      // stalls. Observe the atomic rename instead of requiring a helper process
      // in the simulator to wake this queue for every command.
      let requests = controlRoot.appendingPathComponent("requests")
      let descriptor = open(requests.path, O_EVTONLY | O_CLOEXEC)
      guard descriptor >= 0 else { throw BootstrapError.invalid("cannot watch simulator mailbox") }
      let watcher = DispatchSource.makeFileSystemObjectSource(fileDescriptor: descriptor, eventMask: .write, queue: state)
      watcher.setEventHandler { [weak self] in self?.processCommands() }
      watcher.setCancelHandler { close(descriptor) }
      requestWatcher = watcher
      watcher.resume()
      let session: [String: Any] = ["schemaVersion": 1, "runID": runID, "pid": Int(getpid()),
        "bundleID": bundleID, "profile": profile.id, "notification": "dev.callkitsim.control.\(getpid())",
        "controlTransport": "directory-watch-v1"]
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
    "expiryPolicy": "The advertised LCK timeoutDate remains 30 seconds. #85 measured effective Pause/End expiry near 5.3 s and Join expiry near 64 s in system waiting-call composites; see lckMultiCall. Runtime expiry at 30 s for other LCK action kinds remains unverified.",
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
  "lckMultiCall": {
    "pauseEndExpirySeconds": 5.3,
    "joinExpirySeconds": 64,
    "cleanupEndExpirySeconds": 5.2,
    "pausedAfterDeliverySeconds": 0.11,
    "joinedAfterSettlementSeconds": 0.115,
    "audioHandoffGapSeconds": 0.213,
    "audioDeactivationAfterDeliverySeconds": 0.12,
    "appResumeEndDelaySeconds": 0.091,
    "apiKind": "livecommunicationkit",
    "verification": "measured",
    "reference": "#85; iPhone 13 / iOS 27.0 (24A5355q); October 24, 2026",
    "source": "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/README.md",
    "timingInterpretation": "Representative runtime samples and aggregate medians, not minimum delays or guaranteed bounds. Join median 115 ms (9\u2013239 ms); audio gap median 213 ms (151\u2013404 ms without delayed constituents). Pause 110 ms and deactivation 120 ms are representative delivery-relative samples; resume-generated End 91 ms is relative to Resume delivery.",
    "evidence": {
      "systemComposite": "Constituents of 28 system waiting-call composites arrived within 0\u20133 ms. Accepted Join followed every constituent terminal settlement; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/README.md; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-delay-pause.jsonl; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-reverse.jsonl.",
      "pauseBeforeFulfillment": "Pause state appeared about 110 ms after delivery while Pause remained unfulfilled for 2 s; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-delay-pause.jsonl; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-pending-pause.jsonl.",
      "effectiveExpiry": "timeoutDate advertised 30 s; Pause/End timedOut near 5.3 s, Join near 64 s; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-pending-pause.jsonl; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ea-pending-end.jsonl; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-pending-join.jsonl.",
      "failedPause": "Failed Pause ended A and delivered cleanup End A; B joined; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-fail-pause.jsonl.",
      "failedJoin": "Failed Join ended B and delivered cleanup End B while A stayed paused; End & Accept lost both calls; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-fail-join.jsonl; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ea-fail-join.jsonl.",
      "failedEnd": "Failed End was ignored: A left and B joined, without retry. Pending End expiry was terminal too; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ea-fail-end.jsonl; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ea-pending-end.jsonl.",
      "cleanupEnd": "Cleanup End arrived after the conversation had left. Immediate fulfillment still produced timedOut about 5.2 s later; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-fail-pause.jsonl; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-pending-join.jsonl.",
      "noAutomaticResume": "Ending B left A paused until the application acted; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/hold-accept.jsonl; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-fail-join.jsonl.",
      "choices": "Hold & Accept required maximumConversationGroups >= 2 and a free group. Full groups offered End & Accept, Decline and End Held & Accept; merging did not free a group. End Held & Accept delivered Pause B, End A and Join C; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/cap-1x1.jsonl; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/cap-1x2.jsonl; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/cap-2x2.jsonl; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/third-end-held-accept.jsonl.",
      "outgoingCapacity": "Outgoing Start at capacity returned com.apple.CallKit.error.requesttransaction 7 after 11\u2013206 ms; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/cap-1x1.jsonl; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/cap-1x2.jsonl.",
      "capabilities": "No capabilities, or removing pausing mid-call, gated neither waiting choices nor app Pause; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/caps-none.jsonl; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/caps-toggle.jsonl.",
      "mergeAndUnmerge": "App Merge/Unmerge were accepted and delivered without conversation state changes, including singleton Unmerge and Merge into a ringing target. LCK exposes no group membership; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/merge-b-into-a.jsonl; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/merge-a-into-b.jsonl; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/unmerge-singleton.jsonl; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/cap-2x1.jsonl.",
      "appResume": "With 2x1, app Resume A joined A, then a distinct generated End B arrived 91 ms after Resume delivery. Shared transaction composition was not observable; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/swap-app.jsonl.",
      "audioAndQueue": "Callbacks arrived on main; normal composites deactivated then activated audio with aggregate median 213 ms. The 120 ms deactivation sample is representative, not an aggregate median; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/README.md; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-delay-pause.jsonl; docs/research/callkit-simulator-probe/livecommunicationkit/multicall/hold-accept.jsonl."
    },
    "unverified": [
      "Genuine CallKit multi-call state, choices, capability gating, failures and capacity behavior; these LCK measurements must not replace CallKit policy.",
      "Concrete CXTransaction identifiers and action grouping; LCK exposes neither transaction identifiers nor group membership.",
      "Application-authored composite pause/resume transactions and system UI Swap.",
      "Per-group application-owned media/group membership effects, and capacity admission errors beyond the measured outgoing Start code 7.",
      "Expiry of LCK actions other than Pause, End and Join; 30 s remains a proposed expiry for those kinds.",
      "Delayed or failed app Resume ordering and generated-End timing beyond the immediately fulfilled 2x1 swap-app capture; runtime scheduling remains gated on successful settlement."
    ],
    "captureSHA256": {
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/hold-accept.jsonl": "4699c4f9140d9956bcaef84cb2f6075e4af7c6e643bc971f3a25551ecf01c802",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-delay-pause.jsonl": "a4395a1f5541f33b8d2b586cd59e93a5fbcdbe305a91c38d4b0ae7735758523b",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-reverse.jsonl": "e6f97e8e97fd397b265ec4c4bd59968904af61ac1b6c4b95b4bcad14d122ac61",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-fail-pause.jsonl": "31c9c2b054d8121c13488a50073988cd5a18de4a617751756ac8e46b97074a9e",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-fail-join.jsonl": "96836e3ab962169664179b7edc24855a4e79383e0ed1b97ddbe1858e69f70868",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-pending-pause.jsonl": "d30c7ae4c6c44b27c53fb640c22abbd61d8275417f9a5c0463a3b18393c2ada0",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ha-pending-join.jsonl": "97fb4cfb4fc42f80f4a452703ebffdb5b2fcb6a8ed41b2adbf88bf4b044bbb86",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ea-fail-end.jsonl": "f7a4a400406e8b7fcd34da575434856c6e8bd6e418e08232266fcc39a50a9344",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/ea-pending-end.jsonl": "5d7282b8e932fc2f74a8971eaf16c2f51a0117fdf79e67afe9ca4f83f49cea43",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/cap-1x1.jsonl": "6ea3e1ef43ea4d8e02f832004d4fd67915a9ec9f86f91b8c2e9a2c4083b6bcc9",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/cap-1x2.jsonl": "d96893e01c93b31f1a23c343d83b3f37ffb9839a367b55dc5b3f4f53bc5a2264",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/cap-2x2.jsonl": "e048fb999dddfd6bbd6898c0e4ebf140b8d3e34845f6de74ba43b92022f52159",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/caps-none.jsonl": "a7df6193aa821cfddecb01846f9b648d0328b251a65296f2a7610c6b81623902",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/caps-toggle.jsonl": "8a9777bf1fa181e1ca23f1c25cd97f8a6b91882a7175bc1b86cf8524042190fe",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/merge-b-into-a.jsonl": "92dfd7c2422e4ff546e094b1a314b67b75014d6f37267b50216867c98968cf4b",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/unmerge-singleton.jsonl": "9ebf6219bc1cddace70deac8d8bea0d55c7e54800e9a20909809dccb41afcd22",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/swap-app.jsonl": "512067c785291b5ac6215be88bc701a75242e0499c74947ee3ec0fc683547d7b",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/third-end-held-accept.jsonl": "c9f52d5d003d8f23ca7b96199adee56f720f46eb4a8d99413bf43aa4647d8eb6"
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
  },
  "incomingFilterPolicies": {
    "do-not-disturb": {
      "code": 3,
      "delaySeconds": 2.07,
      "verification": "verified",
      "evidence": [
        "docs/research/callkit-simulator-probe/livecommunicationkit/push/2026-10-23-iphone13-ios27-d3-cold-unlocked-entered.jsonl",
        "docs/research/callkit-simulator-probe/livecommunicationkit/push/2026-10-23-iphone13-ios27-d3-cold-unlocked-entered-repeat.jsonl",
        "docs/research/callkit-simulator-probe/livecommunicationkit/push/README.md"
      ],
      "scope": "D3 report errors measured at 2.081 s and 2.070 s. Same-number repeat at 69 s and process survival are documented in the device writeup; the two captures are separate cold launches."
    },
    "restricted-sharing-mode": {
      "code": 5,
      "delaySeconds": 2.07,
      "verification": "unverified",
      "evidence": [
        "docs/research/callkit-simulator-probe/livecommunicationkit/push/README.md"
      ],
      "scope": "Unverified on device. NSError code follows the public incoming-call error identity; 2.07 s is explicit simulator policy borrowed from measured DND timing, not a measurement for this reason."
    },
    "call-is-protected": {
      "code": 6,
      "delaySeconds": 2.07,
      "verification": "unverified",
      "evidence": [
        "docs/research/callkit-simulator-probe/livecommunicationkit/push/README.md"
      ],
      "scope": "Unverified on device. NSError code follows the public incoming-call error identity; 2.07 s is explicit simulator policy borrowed from measured DND timing, not a measurement for this reason."
    },
    "sensitive-participants": {
      "code": 7,
      "delaySeconds": 2.07,
      "verification": "unverified",
      "evidence": [
        "docs/research/callkit-simulator-probe/livecommunicationkit/push/README.md"
      ],
      "scope": "Unverified on device. NSError code follows the public incoming-call error identity; 2.07 s is explicit simulator policy borrowed from measured DND timing, not a measurement for this reason."
    }
  },
  "lckBlockedNumberEvidence": {
    "verification": "measured-negative",
    "sourceIssue": 85,
    "incomingErrorCode": 0,
    "repeatAfterSeconds": 66,
    "ringTimeoutSeconds": 60,
    "captures": {
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/filter-block.jsonl": "c52e5be2086fd2c14ec3ef4ae1d580ebd9594486d08aeba16d28be7d96229719",
      "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/filter-block-check.jsonl": "b41172c83eea3febb80c8d0f1172348fb4ad399aadda09a9342b2c4ed8c91828"
    },
    "scope": "The blocked number reported successfully and rang. Code 4 is not produced by iOS 27.0 for LCK in these measurements; runtime code 4 is an explicit injectable fault only."
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
      requestWatcher?.setEventHandler(handler: nil)
      requestWatcher?.cancel()
      requestWatcher = nil
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
      // Preparation publishes the mailbox before hook installation finishes.
      // An early directory event may have observed inactive hooks; drain again
      // once activation commits so that request cannot lose its only wakeup.
      if event == "runtime.installed" { processCommands() }
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
      record(event: "provider.created", fields: providerConfigurationJSON(entry))
    }
  }
  private func providerID(_ provider: CXProvider) -> String { String(describing: ObjectIdentifier(provider)) }
  private func providerConfigurationJSON(_ entry: ProviderRegistration) -> [String: Any] {
    let config = entry.provider.configuration
    return ["provider": providerID(entry.provider), "apiKind": entry.apiKind.rawValue,
      "selected": entry === selected, "valid": entry.valid,
      "maximumCallGroups": config.maximumCallGroups, "maximumCallsPerCallGroup": config.maximumCallsPerCallGroup]
  }
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
    if call.reporter.apiKind == .livecommunicationkit {
      for waiting in calls.values where waiting !== call && waiting.ringing && waiting.reporter === call.reporter {
        record(event: "call.changed", fields: callJSON(waiting))
      }
    }
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
    var fields: [String: Any] = ["uuid": call.id.uuidString, "apiKind": call.reporter.apiKind.rawValue,
     "outgoing": call.outgoing, "connected": call.connected,
     "held": call.held, "ended": call.ended, "muted": call.muted,
     "ringing": call.ringing, "callerName": call.update?.localizedCallerName ?? "",
     "handle": call.update?.remoteHandle?.value ?? ""]
    var capabilities: [String] = []
    if call.update?.supportsHolding == true { capabilities.append("pausing") }
    if call.update?.supportsGrouping == true { capabilities.append("merging") }
    if call.update?.supportsUngrouping == true { capabilities.append("unmerging") }
    fields["capabilities"] = capabilities
    if call.reporter.apiKind == .livecommunicationkit { fields["availableSystemChoices"] = availableChoices(call) }
    else { fields["group"] = call.group.uuidString }
    return fields
  }
  private func availableChoices(_ call: CallState) -> [String] {
    guard call.ringing, !call.ended else { return [] }
    let established = calls.values.filter { $0.reporter === call.reporter && $0.connected && !$0.ended }
    return LCKMultiCallPolicy.choices(established: established.count, held: established.filter(\.held).count,
      active: established.filter { !$0.held }.count, maximumGroups: Int(call.reporter.provider.configuration.maximumCallGroups))
  }
  private func actionTiming(_ action: CXAction, api: ProviderAPIKind, origin: ActionOrigin) -> (deadline: Double, expiry: Double) {
    let timing = profile.timing(api: api, answer: action is CXAnswerCallAction, start: action is CXStartCallAction,
      pauseOrEnd: action is CXSetHeldCallAction || action is CXEndCallAction)
    return (timing.deadline, api == .livecommunicationkit && origin == .failureCleanup ? profile.lckMultiCall.cleanupEndExpirySeconds : timing.expiry)
  }
  private func actionFields(_ item: RuntimePendingAction) -> [String: Any] {
    let timing = actionTiming(item.action, api: item.apiKind, origin: item.origin)
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
    var fields: [String: Any] = ["actionID": item.action.uuid.uuidString, "kind": kind,
      "callUUID": (item.action as? CXCallAction)?.callUUID.uuidString ?? "",
      "transactionID": item.transaction.uuid.uuidString, "origin": item.origin.rawValue,
      "provider": providerID(item.provider.provider), "apiKind": item.apiKind.rawValue,
      "issuedAtMS": Int64(item.issuedAt.timeIntervalSince1970 * 1000),
      "deadlineMS": Int64(item.issuedAt.addingTimeInterval(timing.deadline).timeIntervalSince1970 * 1000),
      "expiryMS": Int64(item.issuedAt.addingTimeInterval(timing.expiry).timeIntervalSince1970 * 1000),
      "deadlineSeconds": timing.deadline, "expirySeconds": timing.expiry,
      "latencyMS": Double(DispatchTime.now().uptimeNanoseconds - item.issuedUptime) / 1_000_000]
    if let hold = item.action as? CXSetHeldCallAction { fields["held"] = hold.isOnHold }
    if let group = item.action as? CXSetGroupCallAction { fields["groupTargetUUID"] = group.callUUIDToGroupWith?.uuidString ?? "" }
    if let tones = item.action as? CXPlayDTMFCallAction { fields["digits"] = tones.digits }
    return fields
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
        if let provider = selected, provider.apiKind == .livecommunicationkit {
          let established = calls.values.filter { $0.reporter === provider && !$0.ended && ($0.connected || $0.outgoing || !$0.ringing) }.count
          if !LCKMultiCallPolicy.canStart(established: established, reservedStarts: starts.count - 1,
            maximumGroups: Int(provider.provider.configuration.maximumCallGroups)) { return requestError(7) }
        }
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
  @discardableResult
  private func deliver(_ actions: [CXAction], _ provider: ProviderRegistration, origin: ActionOrigin, requestID: UUID? = nil, generation: UInt64? = nil) -> UUID? {
    guard provider.valid && (generation == nil || provider.generation == generation) else { return nil }
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
        if let group = action as? CXSetGroupCallAction { return ["groupTargetUUID": group.callUUIDToGroupWith?.uuidString ?? ""] }
        return [:]
      },
      "kinds": actions.map { NSStringFromClass(type(of: $0)) }])
    if provider.apiKind == .livecommunicationkit {
      for case let hold as CXSetHeldCallAction in actions { calls[hold.callUUID]?.holdRevision &+= 1 }
    }
    let composite = provider.apiKind == .livecommunicationkit ? LCKRuntimeComposite(actions, calls: calls, origin: origin) : nil
    for action in actions {
      let timing = actionTiming(action, api: provider.apiKind, origin: origin)
      let pending = RuntimePendingAction(action, provider, delivered, origin,
        call: (action as? CXCallAction).flatMap { calls[$0.callUUID] },
        groupTarget: (action as? CXSetGroupCallAction)?.callUUIDToGroupWith.flatMap { calls[$0] }, lckComposite: composite)
      let issued = pending.issuedAt
      let deadline = issued.addingTimeInterval(timing.deadline)
      deadlines.setObject(deadline as NSDate, forKey: action)
      record(event: "action.deadline", fields: actionFields(pending))
      if let answer = action as? CXAnswerCallAction, let call = calls[answer.callUUID] {
        call.ringing = false; changed(call, notifyObservers: false)
      }
      pendingActions[action.uuid] = pending
      if composite?.systemComposite == true, let hold = action as? CXSetHeldCallAction,
         hold.isOnHold, let call = pending.call {
        let revision = call.holdRevision
        after(profile.lckMultiCall.pausedAfterDeliverySeconds) {
          guard provider.valid, provider.generation == pending.generation, self.calls[call.id] === call,
                call.holdRevision == revision else { return }
          call.held = true; self.changed(call); self.reconcileAudio(provider: provider, delay: 0)
        }
      }
      after(max(0, issued.addingTimeInterval(timing.expiry).timeIntervalSinceNow)) { [weak self, weak pending] in
        guard let self, let pending, self.pendingActions[action.uuid] === pending, !pending.expired else { return }
        self.expire(pending)
      }
    }
    callback(provider) { CKSimPerform($0, provider.provider, delivered) }
    return delivered.uuid
  }
  private func settleLCK(_ item: RuntimePendingAction, fulfilled: Bool) {
    let action = item.action
    let kind: LCKMultiCallPolicy.Kind = action is CXAnswerCallAction ? .join : action is CXEndCallAction ? .end :
      (action as? CXSetHeldCallAction)?.isOnHold == true ? .pause : action is CXSetHeldCallAction ? .resume : .other
    let failure = LCKMultiCallPolicy.failure(kind)
    pendingActions.removeValue(forKey: action.uuid)
    if let call = item.call, calls[call.id] === call {
      if fulfilled && item.lckComposite?.systemComposite != true {
        switch kind {
        case .join:
          call.connected = true; call.held = false; call.ringing = false; changed(call)
          reconcileAudio(provider: item.provider, delay: 0.29)
        case .pause, .resume:
          call.held = kind == .pause; changed(call)
          reconcileAudio(provider: item.provider, delay: kind == .pause ? 0.09 : 0.18)
        default: break
        }
      }
      if (fulfilled && kind == .end) || (!fulfilled && failure == .endRegardless) { endCall(call.id, provider: item.provider) }
      else if !fulfilled && failure == .endWithCleanup {
        after(profile.lckMultiCall.joinedAfterSettlementSeconds) {
          guard item.provider.valid, item.provider.generation == item.generation, self.calls[call.id] === call else { return }
          self.endCall(call.id, provider: item.provider)
          self.deliver([CXEndCallAction(call: call.id)], item.provider, origin: .failureCleanup, generation: item.generation)
        }
      }
      if fulfilled, item.origin == .application, let resume = action as? CXSetHeldCallAction,
         !resume.isOnHold, item.transaction.actions.count == 1 {
        let config = item.provider.provider.configuration
        if LCKMultiCallPolicy.shouldEndActiveOnResume(maximumGroups: Int(config.maximumCallGroups), maximumPerGroup: Int(config.maximumCallsPerCallGroup)),
           let other = calls.values.first(where: { $0.reporter === call.reporter && $0.id != call.id && $0.connected && !$0.held && !$0.ended }) {
          let revision = call.holdRevision
          let elapsed = Double(DispatchTime.now().uptimeNanoseconds - item.issuedUptime) / 1_000_000_000
          after(LCKMultiCallPolicy.resumeEndDelay(nominal: profile.lckMultiCall.appResumeEndDelaySeconds, elapsedSinceIssued: elapsed)) {
            guard self.calls[call.id] === call, !call.held, call.holdRevision == revision,
                  self.calls[other.id] === other, !other.held else { return }
            self.deliver([CXEndCallAction(call: other.id)], item.provider, origin: .system, generation: item.generation)
          }
        }
      }
    }
    finishLCKBarrier(item, fulfilled: fulfilled)
  }
  private func finishLCKBarrier(_ item: RuntimePendingAction, fulfilled: Bool) {
    guard let composite = item.lckComposite, composite.systemComposite,
          let joining = composite.barrier.settle(item.action.uuid, fulfilled: fulfilled) else { return }
    let delay = composite.systemComposite ? profile.lckMultiCall.joinedAfterSettlementSeconds : 0
    after(delay) {
      guard item.provider.valid, item.provider.generation == item.generation else { return }
      for id in joining {
        guard let call = composite.joining[id], self.calls[id] === call,
              call.holdRevision == composite.joiningRevisions[id] else { continue }
        call.connected = true; call.held = false; call.ringing = false; self.changed(call)
      }
      if composite.systemComposite && self.handoffPending {
        if self.lckHandoff.markJoined() { self.handoffPending = false }
      }
      self.reconcileAudio(provider: item.provider, delay: 0)
    }
  }
  private func expire(_ item: RuntimePendingAction) {
    record(event: "action.expired", fields: actionFields(item))
    if item.apiKind == .livecommunicationkit && (item.action is CXAnswerCallAction || item.action is CXSetHeldCallAction || item.action is CXEndCallAction) {
      item.expired = true; item.settled = true
      record(event: "action.timeout", fields: actionFields(item))
      callback(item.provider) { $0.provider?(item.provider.provider, timedOutPerforming: item.action) }
      if item.origin == .failureCleanup { pendingActions.removeValue(forKey: item.action.uuid) }
      else { settleLCK(item, fulfilled: false) }
      return
    }
    if let answer = item.action as? CXAnswerCallAction {
      item.expired = true
      if let call = item.call, calls[answer.callUUID] === call {
        deliver([CXEndCallAction(call: answer.callUUID)], item.provider, origin: .system, generation: item.generation)
      }
      return
    }
    pendingActions.removeValue(forKey: item.action.uuid)
    if item.apiKind == .livecommunicationkit { finishLCKBarrier(item, fulfilled: false) }
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
      if item.apiKind == .livecommunicationkit && item.origin == .failureCleanup { return true }
      if item.apiKind == .livecommunicationkit && (action is CXAnswerCallAction || action is CXSetHeldCallAction || action is CXEndCallAction || action is CXSetGroupCallAction) {
        // Merge/Unmerge pass through without any state transition or capability gate.
        settleLCK(item, fulfilled: fulfilled)
        return true
      }
      if item.expired { pendingActions.removeValue(forKey: action.uuid); return true }
      guard let call = item.call, calls[call.id] === call else {
        // A system end can arrive after an answer failure already removed the
        // call. Fulfillment is ignored, and the action's own timer still fires.
        if item.apiKind == .livecommunicationkit {
          pendingActions.removeValue(forKey: action.uuid); finishLCKBarrier(item, fulfilled: false)
        }
        return true
      }
      pendingActions.removeValue(forKey: action.uuid)
      if item.apiKind == .livecommunicationkit { finishLCKBarrier(item, fulfilled: fulfilled) }
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
      let enteredAt = DispatchTime.now()
      let reportID = UUID().uuidString
      let outcome = IncomingReportOutcome(reason: incomingFilter, duplicate: calls[uuid] != nil,
        policy: profile.incomingFilterPolicies[incomingFilter.rawValue],
        normalDelaySeconds: ProcessInfo.processInfo.environment["CKSIM_LATENCY"] == "device" ? profile.reportLatencySeconds : 0.02,
        extraDelaySeconds: Double(faults.reportCallbackDelayMS ?? 0) / 1000, incomingErrorCode: faults.incomingErrorCode ?? 0)
      let error = outcome.error
      let fields: [String: Any] = ["reportID": reportID, "callUUID": uuid.uuidString, "pushID": pushID,
        "domain": outcome.domain, "code": outcome.code, "filterReason": outcome.filterReason,
        "verification": outcome.verification, "latencyMS": 0]
      record(event: "report.incoming", fields: fields)
      if error == nil {
        let call = CallState(uuid, owner); call.update = update.copy() as? CXCallUpdate; call.pushID = pushID
        calls[uuid] = call; changed(call)
        after(profile.ringTimeoutSeconds) {
          guard self.calls[uuid] === call, call.ringing else { return }
          self.deliver([CXEndCallAction(call: uuid)], self.selected ?? owner, origin: .system)
        }
      }
      after(outcome.delaySeconds) {
        (owner.queue ?? .main).async {
          guard self.withState({ !self.stopped }) else { return }
          self.record(event: "report.completed", fields: ["reportID": reportID, "callUUID": uuid.uuidString, "pushID": pushID,
            "domain": outcome.domain, "code": outcome.code, "error": outcome.code, "filterReason": outcome.filterReason,
            "verification": outcome.verification,
            "latencyMS": Double(DispatchTime.now().uptimeNanoseconds - enteredAt.uptimeNanoseconds) / 1_000_000])
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
  private func releaseHandoff(_ provider: ProviderRegistration, revision: UInt64) {
    if provider.apiKind == .livecommunicationkit {
      after(profile.lckMultiCall.audioHandoffGapSeconds) {
        guard self.audioRevision == revision, self.handoffPending else { return }
        if self.lckHandoff.markGapElapsed() {
          self.handoffPending = false; self.reconcileAudio(provider: provider, delay: 0)
        }
      }
    } else {
      handoffPending = false; reconcileAudio(provider: provider, delay: 0.15)
    }
  }
  private func changeAudio(_ desired: Bool, provider: ProviderRegistration, revision: UInt64) {
    guard audioRevision == revision else { return }
    guard desired != audioActive else {
      if !desired && handoffPending {
        releaseHandoff(provider, revision: revision)
      }
      return
    }
    if desired && faults.staleAudio == true { record(event: "fault.audioSuppressed", fields: [:]); return }
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
          self.releaseHandoff(provider, revision: revision)
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
    if command.applicationRequest == true {
      guard selected?.apiKind == .livecommunicationkit, ["hold", "merge", "unmerge"].contains(command.operation) else {
        throw ControlError.invalid("applicationRequest requires an LCK hold, merge or unmerge control")
      }
    }
    let origin: ActionOrigin = command.applicationRequest == true ? .application : .system
    switch command.operation {
    case "status": return ["calls": calls.values.map(callJSON), "pending": pendingActions.values.filter { !$0.settled }.count,
      "audioActive": audioActive, "sequence": sequence, "incomingErrorCode": faults.incomingErrorCode ?? 0,
      "incomingFilter": incomingFilter.snapshot(policy: profile.incomingFilterPolicies[incomingFilter.rawValue]),
      "providerConfigurations": providers.values.sorted { providerID($0.provider) < providerID($1.provider) }.map(providerConfigurationJSON)]
    case "events": return ["events": (try? String(contentsOf: controlRoot.appendingPathComponent("events.jsonl"), encoding: .utf8)) ?? ""]
    case "inspect-adapter":
      let name = "CallKitVoIPPushDelegate"
      let selector = NSSelectorFromString("pushRegistry:didReceiveIncomingVoIPPushWithPayload:metadata:withCompletionHandler:")
      guard let cls = NSClassFromString(name) else { throw ControlError.invalid("adapter class missing") }
      return ["metadataSelector": class_getInstanceMethod(cls, selector) != nil]
    case "incoming-filter":
      guard let rawReason = command.reason, let reason = IncomingFilterReason(rawValue: rawReason) else {
        throw ControlError.invalid("valid incoming filter reason required")
      }
      incomingFilter = reason
      let snapshot = reason.snapshot(policy: profile.incomingFilterPolicies[reason.rawValue])
      var fields = snapshot
      fields["requestID"] = command.id.uuidString
      record(event: "incomingFilter.configured", fields: fields)
      return ["incomingFilter": snapshot]
    case "faults":
      guard let value = command.faults, (0...120_000).contains(value.reportCallbackDelayMS ?? 0),
        [0, 4].contains(value.incomingErrorCode ?? 0) else { throw ControlError.invalid("invalid faults") }
      faults.apply(value)
      let code = faults.incomingErrorCode ?? 0
      record(event: "faults.configured", fields: ["requestID": command.id.uuidString, "incomingErrorCode": code])
      return ["configured": true, "incomingErrorCode": code]
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
      guard let provider = selected, provider.valid, let uuid = command.callUUID, let call = calls[uuid] else { throw ControlError.invalid("unknown call or provider") }
      let actions: [CXAction]
      switch command.operation {
      case "answer": actions = [CXAnswerCallAction(call: uuid)]
      case "decline", "end": actions = [CXEndCallAction(call: uuid)]
      case "hold":
        guard let held = command.held else { throw ControlError.invalid("held required") }
        actions = [CXSetHeldCallAction(call: uuid, onHold: held)]
      case "merge", "unmerge":
        guard provider.apiKind == .livecommunicationkit else { throw ControlError.invalid("unmeasured CallKit group control") }
        if command.operation == "merge" {
          guard let target = command.groupWithCallUUID else { throw ControlError.invalid("groupWithCallUUID required") }
          actions = [CXSetGroupCallAction(call: uuid, callUUIDToGroupWith: target)]
        } else { actions = [CXSetGroupCallAction(call: uuid, callUUIDToGroupWith: nil)] }
      case "mute":
        guard let muted = command.muted else { throw ControlError.invalid("muted required") }
        actions = [CXSetMutedCallAction(call: uuid, muted: muted)]
      case "tones":
        guard let digits = command.digits, (1...64).contains(digits.count),
              digits.allSatisfy({ "0123456789*#ABCDabcd".contains($0) }) else {
          throw ControlError.invalid("invalid DTMF digits")
        }
        actions = [CXPlayDTMFCallAction(call: uuid, digits: digits, type: .singleTone)]
      case "hold-and-accept", "end-and-accept", "end-held-and-accept":
        guard let active = command.activeCallUUID, active != uuid, calls[active]?.connected == true else { throw ControlError.invalid("activeCallUUID required") }
        if provider.apiKind == .livecommunicationkit,
           calls[active]?.reporter !== call.reporter || calls[active]?.held != false { throw ControlError.invalid("activeCallUUID must identify the joined call") }
        if provider.apiKind == .livecommunicationkit && !availableChoices(call).contains(command.operation) {
          throw ControlError.invalid("system choice unavailable")
        }
        if command.operation == "end-held-and-accept" {
          guard provider.apiKind == .livecommunicationkit, let held = command.heldCallUUID,
                held != active, held != uuid, let heldCall = calls[held], heldCall.connected,
                heldCall.held, heldCall.reporter === call.reporter else { throw ControlError.invalid("heldCallUUID required") }
          // #85 third-end-held-accept briefly reports the held conversation joined first.
          heldCall.held = false; changed(heldCall)
          actions = [CXSetHeldCallAction(call: active, onHold: true), CXEndCallAction(call: held), CXAnswerCallAction(call: uuid)]
        } else {
          actions = [command.operation == "hold-and-accept" ? CXSetHeldCallAction(call: active, onHold: true) : CXEndCallAction(call: active), CXAnswerCallAction(call: uuid)]
        }
      default: throw ControlError.invalid("unsupported operation")
      }
      if let error = validate(actions, origin: origin) { throw error }
      if command.operation == "hold-and-accept" || command.operation == "end-and-accept" || command.operation == "end-held-and-accept" {
        // The old call relinquishes audio before the new call acquires it.
        handoffPending = true
        lckHandoff = LCKMultiCallPolicy.Handoff()
        audioRevision += 1
        let revision = audioRevision
        let delay = provider.apiKind == .livecommunicationkit ? profile.lckMultiCall.audioDeactivationAfterDeliverySeconds : 0.12
        after(delay) { self.changeAudio(false, provider: provider, revision: revision) }
      }
      guard let transaction = deliver(actions, provider, origin: origin, requestID: command.id) else { throw ControlError.invalid("provider unavailable") }
      if actions.count > 1 && faults.invalidateDuringHandoff == true { provider.provider.invalidate() }
      return ["actionIDs": actions.map { $0.uuid.uuidString }, "transactionID": transaction.uuidString]
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
