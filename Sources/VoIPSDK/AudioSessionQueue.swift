import Dispatch
import Foundation
import os
#if canImport(VoIPPlatform)
import VoIPPlatform
#endif

/// The SDK's audio executor uses a serial dispatch queue rather than the Swift
/// cooperative pool. Optional native integrations use this same executor; only
/// Sendable values and errors should leave an operation.
@available(macOS 14.0, iOS 17.4, watchOS 10.0, tvOS 17.0, *)
public actor AudioSessionQueue {
  public static let shared = AudioSessionQueue()
  private nonisolated let executor = Executor()

  public nonisolated var unownedExecutor: UnownedSerialExecutor {
    executor.asUnownedSerialExecutor()
  }

  public func perform<Value: Sendable>(_ operation: @Sendable () throws -> Value) rethrows -> Value {
    try operation()
  }

  /// Native adapters may call this between synchronous backend operations. It
  /// also checks the originating action when work is queued in a separate Task.
  public nonisolated static func checkCancellation() throws {
    try Task.checkCancellation()
    try AudioPreparationScope.permit?.check()
  }

  private final class Executor: SerialExecutor {
    func enqueue(_ job: consuming ExecutorJob) {
      let job = UnownedJob(job)
      let executor = asUnownedSerialExecutor()
      VSAudioSessionGetQueue().async { job.runSynchronously(on: executor) }
    }
  }
}

/// A queued native block is not itself a Swift Task. This value carries the
/// originating action/recovery cancellation into that block without extending
/// the action's lifetime or sending a framework object across actors.
final class AudioPreparationPermit: Sendable {
  private let current = OSAllocatedUnfairLock(initialState: true)
  func cancel() { current.withLock { $0 = false } }
  func check() throws { if !current.withLock({ $0 }) { throw CancellationError() } }
}

enum AudioPreparationScope {
  @TaskLocal static var permit: AudioPreparationPermit?

  @MainActor static func perform(_ operation: @escaping @MainActor () async throws -> Void) async throws {
    let permit = AudioPreparationPermit()
    try await withTaskCancellationHandler {
      try await $permit.withValue(permit) { try await operation() }
    } onCancel: { permit.cancel() }
  }
}
