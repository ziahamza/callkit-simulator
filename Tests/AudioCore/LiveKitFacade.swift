import Foundation
import VoIPPlatform

public enum AudioEngineAvailability: Sendable, Equatable { case none, `default` }
public enum AudioFixtureEvent: Sendable, Equatable {
  case automatic(Bool)
  case attempt(AudioEngineAvailability, automatic: Bool)
  case available(AudioEngineAvailability)
}
public enum AudioFixtureError: Error, Equatable {
  case availabilityRejected(AudioEngineAvailability), timedOut
}

// Every field belongs to this inert fixture, never to a framework object.
private final class BackendState: @unchecked Sendable {
  let condition = NSCondition()
  var automatic = true
  var events: [AudioFixtureEvent] = []
  var rejected: AudioEngineAvailability?
  var blocked: AudioEngineAvailability?
  var started = false
}

public final class AudioSession: @unchecked Sendable {
  private let state: BackendState
  fileprivate init(state: BackendState) { self.state = state }
  public var isAutomaticConfigurationEnabled: Bool {
    get { state.condition.lock(); defer { state.condition.unlock() }; return state.automatic }
    set {
      precondition(VSAudioSessionIsOnQueue() && !Thread.isMainThread)
      state.condition.lock(); defer { state.condition.unlock() }
      state.automatic = newValue; state.events.append(.automatic(newValue))
    }
  }
}

public final class AudioManager: @unchecked Sendable {
  public static let shared = AudioManager()
  public let audioSession: AudioSession
  private let state: BackendState
  private init() { let state = BackendState(); self.state = state; audioSession = AudioSession(state: state) }
  public var events: [AudioFixtureEvent] {
    state.condition.lock(); defer { state.condition.unlock() }; return state.events
  }
  public var calls: [AudioEngineAvailability] {
    events.compactMap { if case .available(let value) = $0 { value } else { nil } }
  }
  public func rejectNextAvailability(_ value: AudioEngineAvailability) {
    state.condition.lock(); defer { state.condition.unlock() }; state.rejected = value
  }
  public func armSuspendedAvailability(_ value: AudioEngineAvailability) {
    state.condition.lock(); defer { state.condition.unlock() }
    state.blocked = value; state.started = false
  }
  public var availabilityStarted: Bool {
    state.condition.lock(); defer { state.condition.unlock() }; return state.started
  }
  public func releaseAvailability() {
    state.condition.lock(); state.blocked = nil; state.condition.broadcast(); state.condition.unlock()
  }
  public func setEngineAvailability(_ value: AudioEngineAvailability) throws {
    precondition(VSAudioSessionIsOnQueue() && !Thread.isMainThread)
    state.condition.lock(); defer { state.condition.unlock() }
    state.events.append(.attempt(value, automatic: state.automatic))
    if state.blocked == value {
      state.started = true
      let deadline = Date().addingTimeInterval(3)
      while state.blocked == value {
        guard state.condition.wait(until: deadline) else { throw AudioFixtureError.timedOut }
      }
    }
    if state.rejected == value {
      state.rejected = nil; throw AudioFixtureError.availabilityRejected(value)
    }
    state.events.append(.available(value))
  }
}
