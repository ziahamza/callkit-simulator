import Foundation

// Deliberately withheld facade callbacks/Observation model a platform timing
// boundary. These are whole-client cache contracts, not measured LCK behavior.
@main struct IncomingCacheTests {
  @MainActor static func main() async throws {
    let mode = CommandLine.arguments[1]
    let client = try ConversationClient.forApplication(configuration: .init())
    client.finishLaunch()
    let manager = ConversationManager.latest!
    let payload = IncomingCallPayload(eventId: "cache-event-" + mode,
      serverCallId: "cache-server-" + mode,
      caller: Participant(id: "cache-caller", displayName: "Cache fixture"),
      metadata: ["fixture": .string(mode)])
    var result: [String: Any] = ["mode": mode,
      "classification": "whole production client with platform callback timing deliberately modeled; no LCK execution"]

    func snapshotMatches(_ id: UUID) -> Bool {
      client.snapshots.count == 1 && client.snapshots.first?.id == id &&
        client.snapshots.first?.payload == payload && client.snapshots.first?.members == [payload.caller]
    }
    func attempt(_ reservation: IncomingIntake.Reservation) -> IncomingIntake.Attempt {
      guard case .report(let value) = reservation.disposition else {
        preconditionFailure("Fixture requires one genuine intake report attempt.")
      }
      return value
    }

    let passed: Bool
    switch mode {
    case "reported":
      let reservation = try client.incomingIntake.reserve(payload, source: .pushKit)
      manager.conversations = [Conversation(reservation.id, state: .idle)]
      client.incomingIntake.complete(attempt(reservation), outcome: .reported)
      let before = client.snapshots.count
      let returned = try await client.reportIncomingConversation(payload)
      let after = client.snapshots.count
      passed = before == 0 && returned == reservation.id && snapshotMatches(returned)
      result.merge(["beforeCount": before, "callerCacheCount": after,
        "returnedCanonicalID": returned == reservation.id,
        "callerPayloadMatches": snapshotMatches(returned)]) { _, value in value }

    case "joined-success", "joined-sibling-success":
      let reservation = try client.incomingIntake.reserve(payload, source: .pushKit)
      var entered = false
      let caller = Task { @MainActor () throws -> (UUID, Int, Bool) in
        entered = true
        let returned = try await client.reportIncomingConversation(payload)
        // Capture directly in the caller's continuation, before any further await.
        return (returned, client.snapshots.count, snapshotMatches(returned))
      }
      while !entered { await Task.yield() }
      manager.conversations = [Conversation(reservation.id, state: .idle)]
      if mode == "joined-sibling-success" {
        let sibling = try client.incomingIntake.reserve(payload, source: .pushKit)
        client.incomingIntake.complete(attempt(reservation), outcome: .failed(PlatformFailure.rejected))
        client.incomingIntake.complete(attempt(sibling), outcome: .reported)
      } else {
        client.incomingIntake.complete(attempt(reservation), outcome: .reported)
      }
      let observed = try await caller.value
      passed = observed.0 == reservation.id && observed.1 == 1 && observed.2
      result.merge(["callerCacheCount": observed.1, "callerPayloadMatches": observed.2,
        "returnedCanonicalID": observed.0 == reservation.id]) { _, value in value }

    case "joined-failure":
      let id = try await client.reportIncomingConversation(payload)
      let seeded = snapshotMatches(id)
      // A known owned call now returns its canonical UUID. Exercise a genuinely
      // unreported invitation's failure while the separate cached call disappears.
      var unreported = payload
      unreported.eventId += "-unreported"; unreported.serverCallId += "-unreported"
      let reservation = try client.incomingIntake.reserve(unreported, source: .pushKit)
      precondition(reservation.id != id)
      var entered = false
      let caller = Task { @MainActor () -> (Bool, Int) in
        entered = true
        do {
          _ = try await client.reportIncomingConversation(unreported)
          return (false, client.snapshots.count)
        } catch PlatformFailure.rejected {
          // Capture in the caller's catch, with no yield or extra cache refresh.
          return (true, client.snapshots.count)
        } catch { return (false, client.snapshots.count) }
      }
      while !entered { await Task.yield() }
      manager.conversations = []
      client.incomingIntake.complete(attempt(reservation), outcome: .failed(PlatformFailure.rejected))
      let observed = await caller.value
      passed = seeded && observed.0 && observed.1 == 0
      result.merge(["seededLiveCache": seeded, "callerCaughtExpectedFailure": observed.0,
        "callerCacheCount": observed.1]) { _, value in value }

    default: fatalError("Unknown incoming cache fixture mode.")
    }
    result["success"] = passed
    let bytes = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
    FileHandle.standardOutput.write(bytes + Data([10]))
    if !passed { exit(1) }
  }
}
