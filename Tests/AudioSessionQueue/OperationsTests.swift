import Foundation

private enum TestFailure: Error, CustomStringConvertible {
  case failed(String)
  var description: String { switch self { case .failed(let message): message } }
}

@MainActor
private func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
  if !condition() { throw TestFailure.failed(message) }
}

@MainActor
private func eventually(_ message: String, _ predicate: () -> Bool) async throws {
  let deadline = ContinuousClock.now.advanced(by: .seconds(2))
  while !predicate() {
    guard ContinuousClock.now < deadline else { throw TestFailure.failed(message) }
    await Task.yield()
  }
}

@MainActor
private final class SuspendedRecovery {
  private var continuation: CheckedContinuation<Void, Never>?
  private(set) var started = false
  private(set) var finished = false
  func run() async {
    started = true
    await withCheckedContinuation { continuation = $0 }
    finished = true
  }
  func release() {
    let pending = continuation
    continuation = nil
    pending?.resume()
  }
}

@main
struct OperationsTests {
  @MainActor static func main() async throws {
    try await adapterOrdering()
    print("passed: suspended adapter preparation orders activation, error and deactivation")
  }
  @MainActor private static func adapterOrdering() async throws {
    let operations = AudioAdapterOperations(), preparation = SuspendedRecovery()
    var order: [String] = []
    var errors: [String] = []
    defer { preparation.release() }
    operations.onError = { error in errors.append(error); order.append("error") }
    operations.enqueue {
      order.append("prepare-start")
      await preparation.run()
      order.append("prepare-finish")
    }
    operations.enqueue {
      order.append("activate")
      throw TestFailure.failed("adapter activation fixture")
    }
    operations.enqueue { order.append("deactivate") }
    try await eventually("Adapter preparation must begin and remain suspended.") { preparation.started }
    try expect(order == ["prepare-start"], "Real audio callbacks must not overtake suspended adapter preparation.")
    preparation.release()
    try await eventually("An adapter error must not prevent later deactivation work.") { order.last == "deactivate" }
    try expect(order == ["prepare-start", "prepare-finish", "activate", "error", "deactivate"] &&
               errors == ["adapter activation fixture"],
               "Backend operations must run in order and report a failure exactly once before continuing.")
  }

}
