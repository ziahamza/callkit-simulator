import Foundation

enum IncomingReportErrors {
  static func isAlreadyReported(_ error: any Error) -> Bool {
    var candidate = error as NSError
    for _ in 0..<4 {
      // Measured CallKit error wrapping remains meaningful through LCK without
      // importing or linking CallKit into the device SDK.
      if candidate.domain == "com.apple.CallKit.error.incomingcall" && candidate.code == 2 { return true }
      guard let underlying = candidate.userInfo[NSUnderlyingErrorKey] as? NSError else { return false }
      candidate = underlying
    }
    return false
  }
}

/// Carries an actual terminal delegate object through the current actor turn.
/// A new manager object wins immediately; generation checks protect UUID reuse.
struct TerminalConversationProjection<Object: AnyObject> {
  private struct Entry { let object: Object; let generation: UUID }
  private var entries: [UUID: Entry] = [:]
  @MainActor static func acceptingTerminal<Result>(_ object: Object, current: Object?,
                                                   body: @MainActor () -> Result) -> Result? {
    guard current == nil || current === object else { return nil }
    return body()
  }
  mutating func publishTerminal(_ object: Object, id: UUID) -> UUID {
    let generation = UUID()
    entries[id] = Entry(object: object, generation: generation)
    return generation
  }
  func object(for id: UUID) -> Object? { entries[id]?.object }
  mutating func project(current: [UUID: Object]) -> [UUID: Object] {
    for (id, entry) in entries {
      if let replacement = current[id], replacement !== entry.object { entries.removeValue(forKey: id) }
    }
    var projected = current
    for (id, entry) in entries where projected[id] == nil { projected[id] = entry.object }
    return projected
  }
  @discardableResult mutating func remove(_ id: UUID, generation: UUID) -> Bool {
    guard entries[id]?.generation == generation else { return false }
    entries.removeValue(forKey: id)
    return true
  }
}

#if canImport(LiveCommunicationKit) && (os(iOS) || os(watchOS))
import Observation
import AVFAudio
// Apple's LCK manager/actions and delegate lack concurrency annotations in Xcode 27.1.
// All manager access stays on MainActor except the narrow incoming-report transport.
@preconcurrency import LiveCommunicationKit
#if SWIFT_PACKAGE
import VoIPPlatform
#endif

/// The process's sole LCK owner. Calling code never receives the manager or a provider.
@MainActor
public final class ConversationClient: NSObject {
  public struct Configuration: Sendable, Equatable {
    public var ringtoneName: String?
    public var iconTemplateImageData: Data?
    public var maximumConversationGroups: Int
    public var maximumConversationsPerGroup: Int
    public var includesConversationInRecents: Bool
    public var supportsVideo: Bool
    /// Debug defaults on; Release requires explicit configuration and the
    /// ProviderOwnershipDiagnostics build option. Coverage is best effort.
    public var providerOwnershipMonitoring: Bool
    public static var defaultProviderOwnershipMonitoring: Bool {
      #if DEBUG
      true
      #else
      false
      #endif
    }
    public var unhandledAction: UnhandledActionPolicy
    public init(ringtoneName: String? = nil, iconTemplateImageData: Data? = nil,
                maximumConversationGroups: Int = 2, maximumConversationsPerGroup: Int = 2,
                includesConversationInRecents: Bool = true, supportsVideo: Bool = true,
                unhandledAction: UnhandledActionPolicy = .fail,
                providerOwnershipMonitoring: Bool = Self.defaultProviderOwnershipMonitoring) {
      self.ringtoneName = ringtoneName; self.iconTemplateImageData = iconTemplateImageData
      self.maximumConversationGroups = maximumConversationGroups
      self.maximumConversationsPerGroup = maximumConversationsPerGroup
      self.includesConversationInRecents = includesConversationInRecents
      self.supportsVideo = supportsVideo; self.unhandledAction = unhandledAction
      self.providerOwnershipMonitoring = providerOwnershipMonitoring
    }
  }

  private static var instance: ConversationClient?
  static func forApplication(configuration: Configuration, mediaAdapter: (any MediaAdapter)? = nil,
                             audioPolicy: AudioActivationPolicy = .standard) throws -> ConversationClient {
    try ProcessSDKOwnership.claim()
    if let instance {
      guard instance.configuration == configuration, instance.audioPolicy == audioPolicy,
            mediaAdapter == nil || instance.mediaAdapter === mediaAdapter else {
        throw ConversationClientError.alreadyConfigured
      }
      return instance
    }
    guard configuration.maximumConversationGroups > 0, configuration.maximumConversationsPerGroup > 0 else {
      throw ConversationClientError.invalidConfiguration
    }
    let client = ConversationClient(configuration: configuration,
      mediaAdapter: mediaAdapter ?? NoMediaAdapter(), audioPolicy: audioPolicy)
    instance = client
    return client
  }

  private typealias Record = ConversationRecord
  private let manager: ConversationManager
  private let coordinator: ConversationActionCoordinator
  private let configuration: Configuration
  private var launchFinished = false
  private let mediaAdapter: any MediaAdapter
  private let audioPolicy: AudioActivationPolicy
  private let audio: AudioActivationCoordinator
  // Retaining only currently joined objects prevents address reuse while their
  // value generation authorizes a watchdog end; platform objects stay on main.
  private var audioDemandObjects: [UUID: (object: Conversation, generation: UUID)] = [:]
  private let systemAudio = SystemAudioSession()
  private var audioAuthorityGeneration: UInt64 = 0
  private let audioOperations = AudioAdapterOperations()
  private var records: [UUID: Record] = [:]
  private var terminalProjection = TerminalConversationProjection<Conversation>()
  private var recordRemovalGenerations: [UUID: UUID] = [:]
  private var refreshSuspensionDepth = 0
  nonisolated let incomingIntake = IncomingIntake()
  nonisolated let incomingTransport: IncomingReportTransport
  private var appActions: [UUID: Date] = [:]
  private var stateObservers: [UUID: @MainActor (ConversationStateUpdate) -> Void] = [:]
  private var publishingState = false
  private var eventObservers: [UUID: @MainActor (ConversationEventContext) -> Void] = [:]
  public private(set) var state = ConversationStateUpdate(revision: 0, conversations: [])
  public var snapshots: [ConversationSnapshot] { state.conversations }

  private init(configuration: Configuration, mediaAdapter: any MediaAdapter, audioPolicy: AudioActivationPolicy) {
    self.configuration = configuration
    self.mediaAdapter = mediaAdapter; self.audioPolicy = audioPolicy
    audio = AudioActivationCoordinator(policy: audioPolicy)
    coordinator = ConversationActionCoordinator(policy: configuration.unhandledAction)
    SimulatorSupport.activateIfNeeded()
    #if DEBUG || VSSDK_ENABLE_PROVIDER_DIAGNOSTICS
    ProviderCreationMonitor.beginOwnedManagerCreation()
    #endif
    manager = ConversationManager(configuration: .init(ringtoneName: configuration.ringtoneName,
      iconTemplateImageData: configuration.iconTemplateImageData,
      maximumConversationGroups: configuration.maximumConversationGroups,
      maximumConversationsPerConversationGroup: configuration.maximumConversationsPerGroup,
      includesConversationInRecents: configuration.includesConversationInRecents,
      supportsVideo: configuration.supportsVideo,
      supportedHandleTypes: [.generic, .phoneNumber, .emailAddress]))
    incomingTransport = IncomingReportTransport(manager: manager)
    super.init()
    #if DEBUG || VSSDK_ENABLE_PROVIDER_DIAGNOSTICS
    ProviderCreationMonitor.endOwnedManagerCreation()
    #endif
    manager.delegate = self
    coordinator.snapshot = { [weak self] id in self?.snapshot(id) }
    coordinator.didChange = { [weak self] in self?.refresh() }
    coordinator.willSettle = { [weak self] id, action, accepted in
      guard let self, accepted else { return }
      if action.kind == .mute, let muted = action.muted { self.records[id]?.muted = muted }
      if action.kind == .end {
        let now = Date()
        self.records[id]?.ended = .init(at: now, reason: .local)
        if let incoming = self.incomingIntake.projection(id) {
          self.incomingIntake.terminate(id, generation: incoming.generation, reason: .local,
            now: now, requiresReport: false)
        }
      }
    }
    configureAudio()
  }

  /// Called only after VoIPApplication has published the complete process owner.
  /// Mark first: launch observation can reenter the published process owner.
  func finishLaunch() {
    guard !launchFinished else { return }
    launchFinished = true
    if configuration.providerOwnershipMonitoring {
      #if DEBUG || VSSDK_ENABLE_PROVIDER_DIAGNOSTICS
      // Install after the owned manager has exposed the system class. The
      // monitor never loads a framework or constructs a provider to find it.
      ProviderCreationMonitor.setOwnershipConflictHandler { [weak self] in self?.emit(.providerOwnershipLost) }
      ProviderCreationMonitor.install { message in NSLog("VoIPSDK provider ownership: %@", message) }
      #else
      NSLog("VoIPSDK: provider ownership diagnostics were not compiled in; enable the build option.")
      #endif
    }
    trackManager()
    mediaAdapter.setSystemAudioAuthorization(false)
  }

  private func configureAudio() {
    audioOperations.onError = { message in NSLog("VoIPSDK media adapter: %@", message) }
    audio.onEvent = { [weak self] event, affected in
      switch event {
      case .activated: self?.emit(.audioActivated)
      case .deactivated: self?.emit(.audioDeactivated)
      case .missing:
        for id in affected.sorted(by: { $0.uuidString < $1.uuidString }) { self?.emit(.audioActivationMissing, conversationID: id) }
      case .failed:
        for id in affected.sorted(by: { $0.uuidString < $1.uuidString }) { self?.emit(.audioActivationFailed, conversationID: id) }
      }
    }
    coordinator.prepareAction = { [weak self] _, _ in
      guard let self else { throw CancellationError() }
      try await AudioPreparationScope.perform { [audioOperations = self.audioOperations, mediaAdapter = self.mediaAdapter] in
        try await audioOperations.perform {
          try AudioSessionQueue.checkCancellation()
          try await mediaAdapter.prepare()
        }
      }
    }
    coordinator.prepareForFulfillment = { [weak self] _, _ in
      guard let self else { throw CancellationError() }
      try await AudioPreparationScope.perform { [audioOperations = self.audioOperations, mediaAdapter = self.mediaAdapter] in
        try await audioOperations.perform {
          try AudioSessionQueue.checkCancellation()
          try await mediaAdapter.prepareForFulfillment()
        }
      }
    }
    audio.recover = { [weak self] in
      guard let self else { return }
      do {
        try await AudioPreparationScope.perform { [audioOperations = self.audioOperations, mediaAdapter = self.mediaAdapter] in
          try await audioOperations.perform {
            try AudioSessionQueue.checkCancellation()
            try await mediaAdapter.prepare()
          }
        }
      }
      catch { NSLog("VoIPSDK audio recovery configuration failed: %@", String(describing: error)) }
    }
    audio.endConversations = { [weak self] ids in self?.endForMissingAudio(ids) }
    systemAudio.onLifecycle = { [weak self] event in
      guard let self else { return }
      switch event {
      case .inactive, .mediaServicesReset:
        // Revoke before external adapter code can reenter. These are actual AV
        // observations, never fabricated LCK activation/deactivation callbacks.
        self.audioAuthorityGeneration &+= 1
        self.systemAudio.setSystemAudioAuthorization(false)
        self.audio.systemBecameInactive()
        self.mediaAdapter.setSystemAudioAuthorization(false)
      case .active, .resumptionRecommended:
        // LCK owns activation. A recommendation cannot authorize media.
        break
      }
    }
  }

  private func endForMissingAudio(_ nativeGenerations: [UUID: UUID]) {
    for id in nativeGenerations.keys.sorted(by: { $0.uuidString < $1.uuidString }) {
      guard let generation = nativeGenerations[id], audio.shouldEndConversation(id, generation: generation),
            let tracked = audioDemandObjects[id], tracked.generation == generation else { continue }
      let conversation = tracked.object
      guard manager.conversations.first(where: { $0.uuid == id }) === conversation,
            conversation.state == .joined else { continue }
      // Cancelling application work may synchronously activate audio, reset the
      // provider or replace the native object. Commit terminal metadata only if
      // this same joined conversation still needs the failed-end report.
      coordinator.cancelConversation(id)
      guard audio.shouldEndConversation(id, generation: generation),
            audioDemandObjects[id]?.generation == generation, conversation.state == .joined,
            manager.conversations.first(where: { $0.uuid == id }) === conversation else { continue }
      let ended = ConversationEnded(at: Date(), reason: .failed)
      records[id]?.ended = ended
      if let incoming = incomingIntake.projection(id) {
        incomingIntake.terminate(id, generation: incoming.generation, reason: .failed,
          now: ended.at, requiresReport: false)
      }
      manager.reportConversationEvent(.conversationEnded(ended.at, .failed), for: conversation)
    }
    refresh()
  }

  private func refreshAudioDemand() {
    var joined: [UUID: (object: Conversation, generation: UUID)] = [:]
    for conversation in manager.conversations where conversation.state == .joined {
      let generation: UUID
      if let previous = audioDemandObjects[conversation.uuid], previous.object === conversation {
        generation = previous.generation
      } else { generation = UUID() }
      joined[conversation.uuid] = (conversation, generation)
    }
    audioDemandObjects = joined
    audio.updateDemand(joined.mapValues(\.generation))
  }

  @discardableResult
  public func observeState(_ callback: @escaping @MainActor (ConversationStateUpdate) -> Void) -> UUID {
    let token = UUID(); stateObservers[token] = callback; callback(state); return token
  }
  public func removeStateObserver(_ token: UUID) { stateObservers.removeValue(forKey: token) }
  @discardableResult
  public func observeEvents(_ callback: @escaping @MainActor (ConversationEventContext) -> Void) -> UUID {
    let token = UUID(); eventObservers[token] = callback; return token
  }
  public func removeEventObserver(_ token: UUID) { eventObservers.removeValue(forKey: token) }
  public func setConversationHandler(_ handler: ConversationHandler?) { coordinator.setHandler(handler) }

  public func startConversation(to participant: Participant, video: Bool = false,
                                metadata: [String: JSONValue]? = nil) async throws -> UUID {
    guard !participant.id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
      throw ConversationClientError.invalidPayload
    }
    let id = UUID()
    // Metadata exists synchronously before perform can deliver its delegate action.
    records[id] = makeRecord(direction: .outgoing, members: [participant], video: video)
    // Outgoing metadata has no incoming payload/server identity; keep it in a separate ledger.
    outgoingMetadata[id] = metadata
    do { try await perform(.start(id, participant, video)); refresh(); return id }
    catch { if records[id]?.seenInManager != true {
      records.removeValue(forKey: id); outgoingMetadata.removeValue(forKey: id)
    }; refresh(); throw error }
  }
  private var outgoingMetadata: [UUID: [String: JSONValue]] = [:]
  private struct IncomingLocalEndClaim {
    let generation: UUID
    weak var object: Conversation?
  }
  private var incomingLocalEndClaims: [UUID: [IncomingLocalEndClaim]] = [:]
  private func incomingLocalEndTarget(_ id: UUID) -> IncomingLocalEndClaim? {
    guard let incoming = incomingIntake.projection(id),
          let object = manager.conversations.first(where: { $0.uuid == id }) else { return nil }
    return IncomingLocalEndClaim(generation: incoming.generation, object: object)
  }
  private func hasIncomingLocalEndClaim(_ id: UUID, generation: UUID, object: Conversation) -> Bool {
    incomingLocalEndClaims[id, default: []].contains { $0.generation == generation && $0.object === object }
  }
  @discardableResult
  private func claimIncomingLocalEnd(_ id: UUID, generation: UUID, object: Conversation) -> Bool {
    guard !hasIncomingLocalEndClaim(id, generation: generation, object: object) else { return false }
    incomingLocalEndClaims[id, default: []].append(IncomingLocalEndClaim(generation: generation, object: object))
    return true
  }
  private func pruneIncomingLocalEndClaims() {
    for (id, claims) in incomingLocalEndClaims {
      let generation = incomingIntake.projection(id)?.generation
      let live = claims.filter { $0.generation == generation && $0.object != nil }
      if live.isEmpty { incomingLocalEndClaims.removeValue(forKey: id) }
      else { incomingLocalEndClaims[id] = live }
    }
  }
  public func metadata(for id: UUID) -> [String: JSONValue]? {
    if let incoming = incomingIntake.projection(id) { return incoming.payload.metadata }
    return records[id]?.payload?.metadata ?? outgoingMetadata[id]
  }

  public func joinConversation(_ id: UUID) async throws {
    try requireConversation(id); try await perform(.join(id))
  }
  public func endConversation(_ id: UUID) async throws {
    try requireConversation(id); try await perform(.end(id))
  }
  public func setMuted(_ id: UUID, _ muted: Bool) async throws {
    try requireConversation(id); try await perform(.mute(id, muted))
  }
  public func setHeld(_ id: UUID, _ held: Bool) async throws {
    try requireConversation(id); try await perform(.hold(id, held))
  }
  public func playTones(_ id: UUID, _ digits: String) async throws {
    try requireConversation(id)
    guard !digits.isEmpty, digits.allSatisfy({ "0123456789*#ABCDabcd".contains($0) }) else {
      throw ConversationClientError.invalidDigits
    }
    try await perform(.tones(id, digits))
  }
  public func mergeConversations(_ first: UUID, _ second: UUID) async throws {
    try requireConversation(first); try requireConversation(second)
    guard first != second else { throw ConversationClientError.unsupported }
    try await perform(.merge(first, second))
  }

  public func reportIncomingConversation(_ payload: IncomingCallPayload,
                                         source: IncomingReportSource = .foreground) async throws -> UUID {
    let reservation = try incomingIntake.reserve(payload, source: source)
    hydrateIncoming(reservation.id)
    switch reservation.disposition {
    case .reported:
      refresh()
      return reservation.id
    case .join(let ticket):
      defer { refresh() }
      return try await ticket.value
    case .report(let attempt):
      let outcome = await incomingTransport.report(reservation)
      incomingIntake.complete(attempt, outcome: outcome)
      refresh()
      return try await attempt.ticket.value
    }
  }

  /// Reporting can finish while MainActor is busy. Its one canonical intake is
  /// already registered; actor state is a projection, never a second gate.
  func incomingIntakeDidChange() { refresh() }

  @discardableResult
  private func hydrateIncoming(_ id: UUID) -> IncomingIntake.Projection? {
    guard let projection = incomingIntake.projection(id) else { return nil }
    if records[id] == nil || records[id]?.metadataIsPlaceholder == true {
      var record = makeRecord(direction: .incoming, members: [projection.payload.caller],
        video: projection.payload.hasVideo)
      record.payload = projection.payload
      records[id] = record
    }
    // Actor records cache the canonical payload/terminal marker. Explicit app
    // member/video updates remain actor state; intake generations never replace
    // the canonical caller payload of a retained UUID.
    records[id]?.payload = projection.payload
    records[id]?.ended = projection.terminal?.ended
    recordRemovalGenerations.removeValue(forKey: id)
    return projection
  }

  @discardableResult
  public func receiveCallEnded(_ payload: CallEndedPayload) -> Bool {
    let accepted = incomingIntake.terminate(payload)
    if accepted { refresh() }
    return accepted
  }

  /// Local termination submits the ordinary End action. The await confirms request
  /// acceptance; the handler and terminal snapshot may arrive afterward.
  public func reportConversation(_ id: UUID, report: ConversationReport) async throws {
    if case .ended(.local) = report {
      try await endConversation(id)
      return
    }
    guard let conversation = manager.conversations.first(where: { $0.uuid == id }), records[id] != nil else {
      throw ConversationClientError.unknownConversation
    }
    let now = Date()
    switch report {
    case .connecting:
      records[id]?.connecting = true
      manager.reportConversationEvent(.conversationStartedConnecting(now), for: conversation)
    case .connected:
      records[id]?.connecting = false; records[id]?.connectedAt = now
      manager.reportConversationEvent(.conversationConnected(now), for: conversation)
    case .updated(let members, let video):
      if let members, members.contains(where: { $0.id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) {
        throw ConversationClientError.invalidPayload
      }
      if let members { records[id]?.members = members }
      if let video { records[id]?.video = video }
      if let record = records[id] { manager.reportConversationEvent(.conversationUpdated(update(for: record)), for: conversation) }
    case .ended(let reason):
      guard let nativeReason = endedReason(reason) else { throw ConversationClientError.unsupported }
      records[id]?.ended = .init(at: now, reason: reason)
      if let incoming = incomingIntake.projection(id) {
        incomingIntake.terminate(id, generation: incoming.generation, reason: reason,
          now: now, requiresReport: false)
      }
      manager.reportConversationEvent(.conversationEnded(now, nativeReason), for: conversation)
    }
    refresh()
  }

  private enum Command: Sendable {
    case start(UUID, Participant, Bool), join(UUID), end(UUID), mute(UUID, Bool)
    case hold(UUID, Bool), tones(UUID, String), merge(UUID, UUID)
  }
  private func perform(_ command: Command) async throws {
    // Fresh action objects stay local to this submission. Passing a Swift-owned,
    // actor-isolated action array between methods would require an unsafe transfer.
    let action: ConversationAction
    switch command {
    case .start(let id, let participant, let video):
      action = StartConversationAction(conversationUUID: id, handles: [handle(participant)], isVideo: video)
    case .join(let id): action = JoinConversationAction(conversationUUID: id)
    case .end(let id): action = EndConversationAction(conversationUUID: id)
    case .mute(let id, let muted): action = MuteConversationAction(conversationUUID: id, isMuted: muted)
    case .hold(let id, let held): action = PauseConversationAction(conversationUUID: id, isPaused: held)
    case .tones(let id, let digits): action = PlayToneAction(conversationUUID: id, digits: digits, tone: .single)
    case .merge(let first, let second):
      action = MergeConversationAction(conversationUUID: first, conversationUUIDToMergeWith: second)
    }
    let id = action.uuid
    if case .end(let conversationID) = command,
       let target = incomingLocalEndTarget(conversationID), let object = target.object {
      // Account for the object targeted at submission, never a later replacement
      // that happens to be current when the handler completes.
      claimIncomingLocalEnd(conversationID, generation: target.generation, object: object)
    }
    appActions = appActions.filter { $0.value > Date() }
    appActions[id] = action.timeoutDate
    do { try await manager.perform([action]) }
    catch { appActions.removeValue(forKey: id); throw error }
  }
  private func requireConversation(_ id: UUID) throws {
    guard manager.conversations.contains(where: { $0.uuid == id && $0.state != .left }) else {
      throw ConversationClientError.unknownConversation
    }
  }
  private func makeRecord(direction: ConversationDirection, members: [Participant], video: Bool) -> Record {
    Record(direction: direction, members: members,
      capabilities: .init(video: configuration.supportsVideo), video: video)
  }
  private func handle(_ participant: Participant) -> Handle {
    if let phone = participant.phoneNumber { return Handle(type: .phoneNumber, value: phone, displayName: participant.displayName) }
    if let email = participant.email { return Handle(type: .emailAddress, value: email, displayName: participant.displayName) }
    return Handle(type: .generic, value: participant.id, displayName: participant.displayName)
  }
  private func update(for record: Record) -> Conversation.Update {
    var capabilities: Conversation.Capabilities = []
    if record.capabilities.hold { capabilities.insert(.pausing) }
    if record.capabilities.merge { capabilities.formUnion([.merging, .unmerging]) }
    if record.capabilities.tones { capabilities.insert(.playingTones) }
    // LCK's video flag describes this conversation, not the app's ability to upgrade.
    if record.video { capabilities.insert(.video) }
    return .init(members: Set(record.members.map(handle)), capabilities: capabilities)
  }
  private func endedReason(_ reason: ConversationEndedReason) -> Conversation.EndedReason? {
    switch reason {
    case .local: nil
    case .remoteEnded: .remoteEnded
    case .failed: .failed
    case .unanswered: .unanswered
    case .answeredElsewhere: .joinedElsewhere
    case .declinedElsewhere: .declinedElsewhere
    }
  }
  private func snapshot(_ id: UUID) -> ConversationSnapshot? {
    guard let conversation = manager.conversations.first(where: { $0.uuid == id }) ?? terminalProjection.object(for: id),
          let record = records[id] else { return nil }
    let direction = record.direction
    let status: ConversationStatus
    switch conversation.state {
    case .idle: status = record.connecting ? .connecting : direction == .incoming ? .ringing : .dialing
    case .joining: status = .connecting
    case .joined: status = record.connectedAt == nil ? .connecting : .connected
    case .paused: status = .held
    case .leaving: status = .ending
    case .left: status = .ended
    @unknown default: status = .connecting
    }
    return ConversationSnapshot(id: id, direction: direction, status: status, members: record.members,
      capabilities: record.capabilities, isMuted: record.muted, isVideo: record.video,
      connectedAt: record.connectedAt, ended: record.ended, payload: record.payload,
      pending: coordinator.pending(for: id))
  }
  private func refresh() {
    guard refreshSuspensionDepth == 0 else { return }
    pruneIncomingLocalEndClaims()
    let current = manager.conversations
    let projectedObjects = terminalProjection.project(current: Dictionary(uniqueKeysWithValues: current.map { ($0.uuid, $0) }))
    let ids = Set(projectedObjects.keys)
    for conversation in current {
      recordRemovalGenerations.removeValue(forKey: conversation.uuid)
      incomingIntake.observeMembership(conversation.uuid)
      let incoming = hydrateIncoming(conversation.uuid)
      if records[conversation.uuid] == nil {
        // LCK only exposes localMember; it must never be mislabeled as a remote party.
        var placeholder = makeRecord(direction: .outgoing, members: [], video: false)
        placeholder.metadataIsPlaceholder = true
        records[conversation.uuid] = placeholder
      }
      records[conversation.uuid]?.seenInManager = true
      if let incoming, conversation.state != .left {
        let pending = incomingIntake.takePendingTermination(conversation.uuid, generation: incoming.generation)
        if let terminal = pending ?? incoming.terminal, terminal.ended.reason == .local {
          // Canonical local termination applies to each observed platform object,
          // including replacements published after an earlier task was skipped.
          guard !hasIncomingLocalEndClaim(conversation.uuid, generation: incoming.generation,
            object: conversation) else { continue }
          Task { @MainActor [weak self] in
            guard let self, let latest = self.incomingIntake.projection(conversation.uuid),
                  latest.generation == incoming.generation, latest.terminal?.revision == terminal.revision,
                  self.manager.conversations.first(where: { $0.uuid == conversation.uuid }) === conversation,
                  conversation.state != .left else { return }
            // Scheduling alone consumes no object. Mark immediately before the
            // actual request, then retain the claim on either acceptance or error.
            guard self.claimIncomingLocalEnd(conversation.uuid, generation: incoming.generation,
              object: conversation) else { return }
            do { try await self.perform(.end(conversation.uuid)) }
            catch { NSLog("VoIPSDK: retained local termination request failed.") }
          }
        } else if let pending, let reason = endedReason(pending.ended.reason) {
          manager.reportConversationEvent(.conversationEnded(pending.ended.at, reason), for: conversation)
        }
      }
    }
    for (id, record) in records {
      if record.seenInManager && !ids.contains(id) { scheduleRecordRemoval(id) }
      else if record.payload != nil && !record.seenInManager && !ids.contains(id) &&
                incomingIntake.projection(id) == nil { records.removeValue(forKey: id) }
    }
    let projected = projectedObjects.keys.compactMap(snapshot).sorted { $0.id.uuidString < $1.id.uuidString }
    // Pending signaling is not media demand. Only actually joined, unheld LCK
    // conversations start an absence episode, independently of backend reports.
    refreshAudioDemand()
    guard projected != state.conversations else { return }
    state = ConversationStateUpdate(revision: state.revision &+ 1, conversations: projected)
    guard !publishingState else { return }
    publishingState = true
    var update = state
    while true {
      for callback in Array(stateObservers.values) { callback(update) }
      guard update.revision != state.revision else { break }
      update = state
    }
    publishingState = false
  }
  /// Keep metadata until queued terminal delegates have had a chance to publish it.
  private func scheduleRecordRemoval(_ id: UUID) {
    guard recordRemovalGenerations[id] == nil else { return }
    let generation = UUID()
    recordRemovalGenerations[id] = generation
    Task { @MainActor [weak self] in
      guard let self, self.recordRemovalGenerations[id] == generation else { return }
      self.recordRemovalGenerations.removeValue(forKey: id)
      guard self.terminalProjection.object(for: id) == nil,
            !self.manager.conversations.contains(where: { $0.uuid == id }),
            !self.incomingIntake.hasPending(id) else { return }
      self.removeRecord(id)
      self.refresh()
    }
  }
  private func removeRecord(_ id: UUID) {
    recordRemovalGenerations.removeValue(forKey: id)
    // A failed/cancelled platform action may synchronously deliver its terminal
    // delegate. Keep metadata until that callback has buffered and published it.
    refreshSuspensionDepth += 1
    coordinator.cancelConversation(id)
    refreshSuspensionDepth -= 1
    guard terminalProjection.object(for: id) == nil else { return }
    records.removeValue(forKey: id); outgoingMetadata.removeValue(forKey: id)
    incomingIntake.removeConversation(id)
  }
  private func publishTerminal(_ conversation: Conversation) {
    let id = conversation.uuid
    let current = manager.conversations.first { $0.uuid == id }
    let generation = TerminalConversationProjection<Conversation>.acceptingTerminal(conversation, current: current) {
      let generation = terminalProjection.publishTerminal(conversation, id: id)
      incomingIntake.observeMembership(id)
      hydrateIncoming(id)
      if records[id] == nil {
        var placeholder = makeRecord(direction: .outgoing, members: [], video: false)
        placeholder.metadataIsPlaceholder = true; placeholder.seenInManager = true
        records[id] = placeholder
      }
      // The accepted terminal delegate is itself a real manager object, even
      // when it disappeared before observation or incoming report settlement.
      records[id]?.seenInManager = true
      recordRemovalGenerations.removeValue(forKey: id)
      // Cancel only after accepting this object identity and buffering it. A
      // stale terminal delegate must not cancel a reused UUID's pending actions.
      refreshSuspensionDepth += 1
      coordinator.cancelConversation(id)
      refreshSuspensionDepth -= 1
      return generation
    }
    guard let generation else { refresh(); return }
    refresh()
    Task { @MainActor [weak self] in
      guard let self, self.terminalProjection.remove(id, generation: generation) else { return }
      if !self.manager.conversations.contains(where: { $0.uuid == id }), !self.incomingIntake.hasPending(id) {
        self.removeRecord(id)
      }
      self.refresh()
    }
  }

  private func trackManager() {
    withObservationTracking { refresh(); _ = manager.conversations } onChange: { [weak self] in
      Task { @MainActor in self?.trackManager() }
    }
  }
  private func emit(_ event: ConversationEvent, conversationID: UUID? = nil) {
    let context = ConversationEventContext(type: event, conversationID: conversationID)
    for callback in Array(eventObservers.values) { callback(context) }
  }
}

// Device traces prove all LCK delegate callbacks arrive on main. @preconcurrency limits
// the missing SDK isolation annotation to these witnesses; it adds a runtime actor check.
extension ConversationClient: @preconcurrency ConversationManagerDelegate {
  public func conversationManager(_ manager: ConversationManager, conversationChanged conversation: Conversation) {
    if conversation.state == .left { publishTerminal(conversation) }
    else { refresh() }
  }
  public func conversationManagerDidBegin(_ manager: ConversationManager) { refresh() }
  public func conversationManagerDidReset(_ manager: ConversationManager) {
    audioAuthorityGeneration &+= 1
    systemAudio.setSystemAudioAuthorization(false)
    audioDemandObjects.removeAll()
    audio.reset()
    mediaAdapter.setSystemAudioAuthorization(false)
    incomingIntake.reset()
    coordinator.cancelAll(reason: .reset); appActions.removeAll(); refresh(); emit(.providerReset)
  }
  public func conversationManager(_ manager: ConversationManager, perform action: ConversationAction) {
    switch action.state { case .idle, .running: break; default: return }
    let id = action.conversationUUID
    let origin: ConversationActionOrigin = appActions.removeValue(forKey: action.uuid) == nil ? .system : .app
    hydrateIncoming(id)
    if action is EndConversationAction, origin == .system,
       let target = incomingLocalEndTarget(id), let object = target.object {
      // Delivery is already an actual attempt. Claim its original object before
      // refresh can schedule retained cleanup, including when the handler fails.
      claimIncomingLocalEnd(id, generation: target.generation, object: object)
    }
    if let start = action as? StartConversationAction, records[id]?.metadataIsPlaceholder != false {
      let members = start.handles.map { handle -> Participant in
        Participant(id: handle.value, displayName: handle.displayName,
          phoneNumber: handle.type == .phoneNumber ? handle.value : nil,
          email: handle.type == .emailAddress ? handle.value : nil)
      }
      if records[id] == nil { records[id] = makeRecord(direction: .outgoing, members: members, video: start.isVideo) }
      else { records[id]?.enrichSystemStart(members: members, video: start.isVideo) }
    }
    refresh()
    let kind: ConversationActionKind
    var held: Bool?; var muted: Bool?; var digits: String?
    switch action {
    case is JoinConversationAction: kind = .join
    case is StartConversationAction: kind = .start
    case is EndConversationAction: kind = .end
    case let pause as PauseConversationAction:
      kind = .hold; held = pause.isPaused
      if !coordinator.hasPendingAction(for: id, kind: .hold),
         let snapshot = snapshot(id), (snapshot.status == .held) == pause.isPaused { action.fulfill(); return }
    case let mute as MuteConversationAction:
      kind = .mute; muted = mute.isMuted
      let matchesPendingAppMute = origin == .system && coordinator.hasPendingAppMute(for: id, value: mute.isMuted)
      if snapshot(id) == nil || snapshot(id)?.status == .ended || matchesPendingAppMute ||
          (!coordinator.hasPendingAction(for: id, kind: .mute) && records[id]?.muted == mute.isMuted) {
        action.fulfill(); return
      }
    case let tone as PlayToneAction: kind = .tones; digits = tone.digits
    case is MergeConversationAction, is UnmergeConversationAction:
      // RFC has no merge handler; LCK owns topology. No transport/media work is fabricated.
      action.fulfill(); return
    default: action.fail(); return
    }
    let pending = PendingAction(id: action.uuid, kind: kind, deadline: action.timeoutDate,
      origin: origin, held: held, muted: muted, digits: digits)
    coordinator.enqueue(conversationID: id, action: pending, isPending: {
      switch action.state { case .idle, .running: true; default: false }
    }, settle: { accepted in if accepted { action.fulfill() } else { action.fail() } })
  }
  public func conversationManager(_ manager: ConversationManager, timedOutPerforming action: ConversationAction) {
    coordinator.cancel(action.uuid, reason: .platformTimeout)
  }
  public func conversationManager(_ manager: ConversationManager, didActivate audioSession: AVAudioSession) {
    audioAuthorityGeneration &+= 1
    let generation = audioAuthorityGeneration
    systemAudio.setSystemAudioAuthorization(true)
    mediaAdapter.setSystemAudioAuthorization(true)
    guard audioAuthorityGeneration == generation else { return }
    audioOperations.enqueue { [mediaAdapter] in try await mediaAdapter.didActivate(audioSession) }
    // Publish only after the authorization and ordered native work are installed;
    // event observers may synchronously reset or deliver an opposite callback.
    audio.didActivate()
  }
  public func conversationManager(_ manager: ConversationManager, didDeactivate audioSession: AVAudioSession) {
    audioAuthorityGeneration &+= 1
    let generation = audioAuthorityGeneration
    systemAudio.setSystemAudioAuthorization(false)
    mediaAdapter.setSystemAudioAuthorization(false)
    guard audioAuthorityGeneration == generation else { return }
    audioOperations.enqueue { [mediaAdapter] in try await mediaAdapter.didDeactivate(audioSession) }
    // Publish only after the authorization and ordered native work are installed;
    // event observers may synchronously reset or deliver an opposite callback.
    audio.didDeactivate()
  }
}
#endif
