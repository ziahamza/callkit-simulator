#if canImport(AVFAudio) && (os(iOS) || os(watchOS))
import Foundation
import AVFAudio
import os
#if canImport(VoIPPlatform)
import VoIPPlatform
#endif


enum SystemAudioLifecycleEvent: Sendable, Equatable {
  case active
  case inactive(source: Int?, interruptionReason: UInt?)
  case resumptionRecommended(Bool)
  case mediaServicesReset
}

#if DEBUG && targetEnvironment(simulator)
@MainActor
enum AudioLifecycleDiagnostics {
  /// Internal scalar-only acceptance observation, compiled out of device and Release builds.
  static var didConsume: (@MainActor (SystemAudioLifecycleEvent, Bool, Bool) -> Void)?
}
#endif

private struct ObservedAudioRoutes: Sendable {
  let inputs: [AudioRoutePort]
  let outputs: [AudioRoutePort]
}

private struct AudioRouteAuthorization {
  var active = false
  var generation: UInt64 = 0
}

// Tokens are immutable; Foundation permits observer removal on any thread.
// Owning the tokens separately also removes them if the SDK actor is released.
private final class RouteNotificationTokens: @unchecked Sendable {
  private let tokens: [any NSObjectProtocol]
  init(_ tokens: [any NSObjectProtocol]) { self.tokens = tokens }
  deinit { for token in tokens { NotificationCenter.default.removeObserver(token) } }
}

@MainActor
final class SystemAudioSession {
  let routes = AudioRoutesStore()
  var onLifecycle: (@MainActor (SystemAudioLifecycleEvent) -> Void)?
  private var routeObservers: RouteNotificationTokens?
  private var probeTask: Task<Void, Never>?
  private var probeGeneration: UInt64 = 0
  private var lifecycleObserver: VSAudioSessionLifecycleObserver?
  private let authorization = OSAllocatedUnfairLock(initialState: AudioRouteAuthorization())

  init() {
    #if os(iOS)
    var names = [AVAudioSession.routeChangeNotification]
    if #available(iOS 26.0, *) { names.append(AVAudioSession.availableInputsChangeNotification) }
    routeObservers = RouteNotificationTokens(names.map { name in
      NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
        MainActor.assumeIsolated { self?.refreshRoutes() }
      }
    })
    #endif
    lifecycleObserver = VSAudioSessionLifecycleObserver { [weak self] value in
      let event: SystemAudioLifecycleEvent
      switch value.kind {
      case .active: event = .active
      case .inactive:
        event = .inactive(source: value.hasSource ? value.source : nil,
          interruptionReason: value.hasInterruptionReason ? value.interruptionReason : nil)
      case .resumptionRecommended: event = .resumptionRecommended(value.shouldResume)
      case .mediaServicesReset: event = .mediaServicesReset
      @unknown default: return
      }
      MainActor.assumeIsolated { self?.deliver(event) }
    }
    refreshRoutes()
  }

  /// Resumption is advisory: it never activates a session or authorizes an engine.
  private func deliver(_ event: SystemAudioLifecycleEvent) {
    #if DEBUG && targetEnvironment(simulator)
    let authorizedBefore = authorization.withLock { $0.active }
    #endif
    switch event {
    case .inactive, .mediaServicesReset: setSystemAudioAuthorization(false)
    case .active, .resumptionRecommended: break
    }
    onLifecycle?(event)
    #if DEBUG && targetEnvironment(simulator)
    AudioLifecycleDiagnostics.didConsume?(event, authorizedBefore, authorization.withLock { $0.active })
    #endif
    refreshRoutes()
  }

  func setSystemAudioAuthorization(_ active: Bool) {
    authorization.withLock { $0.generation &+= 1; $0.active = active }
  }

  func refreshRoutes() {
    probeGeneration &+= 1
    let generation = probeGeneration
    probeTask?.cancel()
    probeTask = Task { @MainActor [weak self] in
      do { try await Task.sleep(for: .milliseconds(200)) } catch { return }
      let observation = await AudioSessionQueue.shared.observeRoutes()
      guard let self, self.probeGeneration == generation, !Task.isCancelled else { return }
      self.probeTask = nil
      self.publish(observation)
    }
  }

  private func publish(_ observation: ObservedAudioRoutes) {
    routes.update(inputs: observation.inputs, outputs: observation.outputs)
  }

  private func refreshAfterCommand() async {
    probeGeneration &+= 1
    let generation = probeGeneration
    probeTask?.cancel(); probeTask = nil
    let observation = await AudioSessionQueue.shared.observeRoutes()
    guard probeGeneration == generation else { return }
    publish(observation)
  }

  func selectRoute(_ id: String, isActive: Bool) async throws {
    guard let selection = try routes.selection(id, isActive: isActive) else { return }
    let generation = try authorization.withLock { state in
      guard state.active else { throw AudioRouteError.audioSessionInactive }
      return state.generation
    }
    do {
      try await AudioSessionQueue.shared.selectRoute(selection, authorization: authorization, generation: generation)
      await refreshAfterCommand()
    } catch {
      await refreshAfterCommand()
      throw error
    }
  }
}

private extension AudioSessionQueue {
  func observeRoutes() -> ObservedAudioRoutes {
    #if os(iOS)
    let session = AVAudioSession.sharedInstance()
    func port(_ value: AVAudioSessionPortDescription) -> AudioRoutePort {
      AudioRoutePort(uid: value.uid, name: value.portName, type: value.portType.rawValue)
    }
    return ObservedAudioRoutes(inputs: (session.availableInputs ?? []).map(port),
      outputs: session.currentRoute.outputs.map(port))
    #else
    return ObservedAudioRoutes(inputs: [], outputs: [])
    #endif
  }

  func selectRoute(_ selection: AudioRouteSelection,
                   authorization: OSAllocatedUnfairLock<AudioRouteAuthorization>, generation: UInt64) throws {
    #if os(iOS)
    func checkAuthorization() throws {
      guard authorization.withLock({ $0.active && $0.generation == generation }) else {
        throw AudioRouteError.audioSessionInactive
      }
      try Task.checkCancellation()
    }
    try checkAuthorization()
    let session = AVAudioSession.sharedInstance()
    switch selection {
    case .speaker:
      try checkAuthorization()
      try session.overrideOutputAudioPort(.speaker)
    case .input(let uid):
      guard let input = session.availableInputs?.first(where: { $0.uid == uid }) else { throw AudioRouteError.unknownRoute }
      try checkAuthorization()
      try session.overrideOutputAudioPort(.none)
      try checkAuthorization()
      try session.setPreferredInput(input)
    }
    #else
    throw AudioRouteError.unsupported
    #endif
  }
}
#endif
