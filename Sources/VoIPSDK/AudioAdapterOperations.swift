import Foundation

/// Orders backend preparation and real callbacks and genuine system callbacks.
/// Each adapter enqueues its blocking native work on AudioSessionQueue.
@MainActor
final class AudioAdapterOperations {
  private var tail: Task<Void, Never>?
  private var tailID: UUID?
  var onError: (@MainActor (String) -> Void)?
  func perform(_ operation: @escaping @MainActor () async throws -> Void) async throws {
    try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, any Error>) in
      enqueue {
        do { try await operation(); continuation.resume() }
        catch { continuation.resume(throwing: error) }
      }
    }
  }
  func enqueue(_ operation: @escaping @MainActor () async throws -> Void) {
    let previous = tail; let id = UUID(); tailID = id
    tail = Task { @MainActor [weak self] in
      await previous?.value
      do { try await operation() }
      catch { self?.onError?(String(describing: error)) }
      if self?.tailID == id { self?.tail = nil; self?.tailID = nil }
    }
  }
}
