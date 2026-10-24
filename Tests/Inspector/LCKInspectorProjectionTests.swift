import Foundation

@main
struct LCKInspectorProjectionTests {
  static func event(_ sequence: Int = 1, _ fields: [String: Any] = [:]) throws -> RuntimeEvent {
    let base: [String: Any] = ["schemaVersion": 1, "sequence": sequence, "runID": "run", "pid": 123,
      "bundleID": "fixture", "event": "call.changed", "t_ms": 0]
    return try JSONDecoder().decode(RuntimeEvent.self, from: JSONSerialization.data(withJSONObject:
      base.merging(fields) { _, new in new }))
  }

  static func main() throws {
    let waiting = try event(1, ["uuid": "waiting", "apiKind": "livecommunicationkit", "ringing": true,
      "capabilities": [], "availableSystemChoices": ["end-and-accept", "decline", "hold-and-accept"]])
    precondition(InspectorLCKProjection.systemChoices(for: waiting) == ["end-and-accept", "decline", "hold-and-accept"],
      "Capabilities must not remove observed Hold & Accept")
    precondition(waiting.status == "idle", "A reported incoming LCK conversation is idle, not an invented ringing state")
    for (fields, expected) in [(["connected": true], "joined"), (["held": true], "paused"), (["ended": true], "left")] {
      let call = try event(1, (["apiKind": "livecommunicationkit"] as [String: Any]).merging(fields) { _, new in new })
      precondition(call.status == expected)
    }
    let full = try event(2, ["apiKind": "livecommunicationkit", "availableSystemChoices": ["end-and-accept", "decline", "end-held-and-accept"]])
    precondition(InspectorLCKProjection.systemChoices(for: full) == ["end-and-accept", "decline", "end-held-and-accept"])
    let missing = try event(3, ["apiKind": "livecommunicationkit", "capabilities": ["pausing", "merging"]])
    precondition(InspectorLCKProjection.systemChoices(for: missing) == nil,
      "Missing choice metadata must not invent choices from capabilities or counts")
    let bad = try event(4, ["apiKind": "livecommunicationkit", "availableSystemChoices": ["merge", "answer", "answer", "swap"]])
    precondition(InspectorLCKProjection.systemChoices(for: bad) == ["answer"], "Never offer the inert system Merge control")
    let legacy = try event(5, ["apiKind": "callkit", "availableSystemChoices": ["answer"]])
    precondition(InspectorLCKProjection.systemChoices(for: legacy) == nil)

    let ringingTarget = try event(2, ["uuid": "ringing-target", "ringing": true, "capabilities": []])
    let endedTarget = try event(3, ["uuid": "ended-target", "ended": true])
    let staleTarget = try event(1, ["uuid": "ended-target", "connected": true])
    let targets = InspectorLCKProjection.appMergeTargets(for: waiting, calls: [waiting, ringingTarget, staleTarget, endedTarget])
    precondition(targets.map(\.uuid) == ["ringing-target"], "App Merge must permit a ringing target with no capabilities")
    precondition(InspectorLCKProjection.appMergeTargets(for: waiting, calls: [waiting]).isEmpty)

    let pause = try event(1, ["event": "action.deadline", "apiKind": "livecommunicationkit", "kind": "hold",
      "deadlineMS": 31_000, "deadlineSeconds": 30, "expiryMS": 6_300, "expirySeconds": 5.3])
    let timing = InspectorLCKProjection.actionTiming(for: pause, nowMS: 2_000)
    precondition(timing.reported?.remainingSeconds == 29 && timing.effective?.remainingSeconds == 4.3)
    precondition(abs(timing.reported!.progress - 1 / 30.0) < 0.000001)
    precondition(abs(timing.effective!.progress - 1 / 5.3) < 0.000001)
    let join = try event(1, ["deadlineMS": 31_000, "deadlineSeconds": 30, "expiryMS": 65_000, "expirySeconds": 64])
    let joinTiming = InspectorLCKProjection.actionTiming(for: join, nowMS: 32_000)
    precondition(joinTiming.reported?.remainingSeconds == 0 && joinTiming.reported?.progress == 1)
    precondition(joinTiming.effective?.remainingSeconds == 33)
    precondition(InspectorLCKProjection.actionTiming(for: missing, nowMS: 2_000).effective == nil)
    let invalid = try event(1, ["deadlineMS": 31_000, "deadlineSeconds": 0, "expiryMS": 6_300, "expirySeconds": -1])
    precondition(InspectorLCKProjection.actionTiming(for: invalid, nowMS: 2_000).reported == nil)
    precondition(InspectorLCKProjection.actionTiming(for: invalid, nowMS: 2_000).effective == nil)
    precondition(InspectorLCKProjection.actionTiming(for: pause, nowMS: .infinity).effective == nil)
    precondition(InspectorLCKProjection.provenance.contains("Measured #85") && InspectorLCKProjection.provenance.contains("iPhone 13"))
    precondition(InspectorLCKProjection.sourcePath == "docs/research/callkit-simulator-probe/livecommunicationkit/multicall/README.md")
    print("Measured LCK Inspector projection tests passed")
  }
}
