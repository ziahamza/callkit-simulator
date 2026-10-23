import Dispatch
import Foundation
import VoIPPlatform

@main
struct QueueTests {
  @MainActor static func main() async throws {
    let actorLocation = await AudioSessionQueue.shared.perform {
      VSAudioSessionIsOnQueue() && !Thread.isMainThread
    }
    precondition(actorLocation, "Swift audio operations must use the native audio queue off main")

    let objcLocation = await withCheckedContinuation { continuation in
      VSAudioSessionGetQueue().async {
        continuation.resume(returning: VSAudioSessionIsOnQueue() && !Thread.isMainThread)
      }
    }
    precondition(objcLocation, "ObjC integrations must use the same native queue")

    let entered = AsyncStream<Void>.makeStream()
    let release = DispatchSemaphore(value: 0)
    let blocking = Task {
      await AudioSessionQueue.shared.perform {
        entered.continuation.yield(())
        let result = release.wait(timeout: .now() + 5)
        return result == .success && VSAudioSessionIsOnQueue()
      }
    }
    for await _ in entered.stream { break }
    // This MainActor continuation must run while the audio worker is blocked.
    // An implementation that blocks MainActor cannot satisfy this ordering.
    release.signal()
    let workerCompleted = await blocking.value
    precondition(workerCompleted, "A blocked audio operation must leave MainActor runnable")
    entered.continuation.finish()

    // The native block does not run in the cancelled Swift Task. The production
    // permit must carry cancellation across that boundary before any setter runs.
    let blocked = AsyncStream<Void>.makeStream()
    let queued = AsyncStream<Void>.makeStream()
    let unblock = DispatchSemaphore(value: 0)
    VSAudioSessionGetQueue().async {
      blocked.continuation.yield(())
      precondition(unblock.wait(timeout: .now() + 5) == .success)
    }
    for await _ in blocked.stream { break }
    let preparation = Task { @MainActor in
      do {
        try await AudioPreparationScope.perform {
          let permit = AudioPreparationScope.permit
          try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, any Error>) in
            VSAudioSessionGetQueue().async {
              do { try permit?.check(); continuation.resume() }
              catch { continuation.resume(throwing: error) }
            }
            queued.continuation.yield(())
          }
        }
        return false
      } catch is CancellationError { return true }
      catch { return false }
    }
    for await _ in queued.stream { break }
    preparation.cancel(); unblock.signal()
    let cancelledBeforeNativeWork = await preparation.value
    precondition(cancelledBeforeNativeWork, "Cancelled queued preparation must reject on the native executor")
    blocked.continuation.finish(); queued.continuation.finish()

    print("passed: shared-native-and-Swift-executor; off-main-audio; blocked-worker-leaves-main-runnable; cancelled-native-queue-permit")
  }
}
