#if os(iOS)
import AVFAudio
import Foundation
import LiveKit
import VoIPSDK
import os

/// Optional source integration for an app that already uses the LiveKit Swift SDK.
/// Add this file to the app target; it is deliberately outside the dependency-free package.
@MainActor
public final class LiveKitMediaAdapter: MediaAdapter {
  private let authorization = OSAllocatedUnfairLock(initialState: false)
  private let worker: Worker
  public init() { worker = Worker(authorization: authorization) }
  public func setSystemAudioAuthorization(_ active: Bool) {
    authorization.withLock { $0 = active }
    if !active {
      Task { [worker] in
        do { try await worker.revokeIfUnauthorized() }
        catch { NSLog("VoIPSDK LiveKit could not revoke media availability: %@", String(describing: error)) }
      }
    }
  }
  public func prepare() async throws { try await worker.prepare() }
  public func didActivate(_ audioSession: AVAudioSession) async throws { try await worker.setAvailable(true) }
  public func didDeactivate(_ audioSession: AVAudioSession) async throws { try await worker.setAvailable(false) }

  /// Only value arguments cross actors. The backend singleton is resolved and used here;
  /// its synchronous operations cannot block MainActor or require a Sendable assertion.
  private actor Worker {
    nonisolated var unownedExecutor: UnownedSerialExecutor {
      AudioSessionQueue.shared.unownedExecutor
    }
    let authorization: OSAllocatedUnfairLock<Bool>
    private var prepared = false
    init(authorization: OSAllocatedUnfairLock<Bool>) { self.authorization = authorization }
    func prepare() async throws {
      try AudioSessionQueue.checkCancellation()
      AudioManager.shared.audioSession.isAutomaticConfigurationEnabled = false
      try AudioSessionQueue.checkCancellation()
      if !authorization.withLock({ $0 }) { try AudioManager.shared.setEngineAvailability(.none) }
      try AudioSessionQueue.checkCancellation()
      prepared = true
    }
    func setAvailable(_ active: Bool) throws {
      guard prepared else { return }
      let allowed = active && authorization.withLock { $0 }
      try AudioManager.shared.setEngineAvailability(allowed ? .default : .none)
    }
    func revokeIfUnauthorized() throws {
      guard prepared, !authorization.withLock({ $0 }) else { return }
      try AudioManager.shared.setEngineAvailability(.none)
    }
  }
}
#endif
