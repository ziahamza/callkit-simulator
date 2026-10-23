import AVFAudio
import Foundation

/// Framework objects remain on the common audio queue; only results cross back.
@MainActor
final class QueuedMediaAdapter: MediaAdapter {
  private let binding = VSSelectedMediaBinding.shared()

  func setSystemAudioAuthorization(_ active: Bool) {
    binding.setSystemAudioAuthorization(active)
  }

  func prepare() async throws { try await prepare(configureSession: false) }
  func prepareForFulfillment() async throws { try await prepare(configureSession: true) }

  private func prepare(configureSession: Bool) async throws {
    let permit = AudioPreparationScope.permit
    while true {
      try AudioSessionQueue.checkCancellation()
      do {
        try await complete { binding.prepare(check: { @Sendable in
          do { try permit?.check(); return nil }
          catch { return error as NSError }
        }, configuration: { @Sendable in
          do {
            try permit?.check()
            if configureSession { try AudioSessionConfiguration.applyOnAudioQueue(permit: permit) }
            return nil
          }
          catch { return error as NSError }
        }, completion: $0) }
        try AudioSessionQueue.checkCancellation()
        return
      } catch {
        let failure = error as NSError
        guard ExpoMediaSelection.name == "livekit", failure.domain == "VoIPSDK.MediaAdapter", failure.code == 1 else { throw error }
        // Only registry readiness retries. The real action's permit/deadline
        // and task cancellation bound this wait without blocking the audio queue.
        try await Task.sleep(for: .milliseconds(50))
      }
    }
  }

  func didActivate(_ audioSession: AVAudioSession) async throws {
    try await complete { binding.activate(audioSession, completion: $0) }
  }
  func didDeactivate(_ audioSession: AVAudioSession) async throws {
    try await complete { binding.deactivate(audioSession, completion: $0) }
  }
  private func complete(_ operation: (@escaping @Sendable (Error?) -> Void) -> Void) async throws {
    try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, any Error>) in
      operation { error in
        if let error { continuation.resume(throwing: error) }
        else { continuation.resume() }
      }
    }
  }
}
