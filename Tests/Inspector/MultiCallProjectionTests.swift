import Foundation

@main struct MultiCallProjectionTests {
  static let a = "00000000-0000-0000-0000-000000000001"
  static let b = "00000000-0000-0000-0000-000000000002"
  static let c = "00000000-0000-0000-0000-000000000003"
  static let first = "00000000-0000-0000-0000-000000000101"
  static let second = "00000000-0000-0000-0000-000000000102"
  static func row(_ sequence: Int, _ event: String, _ fields: [String: Any]) throws -> RuntimeEvent {
    let base: [String: Any] = ["schemaVersion": 1, "sequence": sequence, "runID": "test", "pid": 123,
      "bundleID": "test.fixture", "event": event, "t_ms": sequence * 10]
    return try JSONDecoder().decode(RuntimeEvent.self, from: JSONSerialization.data(withJSONObject: base.merging(fields) { _, new in new }))
  }
  static func call(_ sequence: Int, _ id: String, _ group: String?, ended: Bool = false) throws -> RuntimeEvent {
    var fields: [String: Any] = ["uuid": id, "connected": true, "ended": ended, "capabilities": ["pausing", "merging", "unmerging"]]
    fields["group"] = group
    return try row(sequence, "call.changed", fields)
  }
  static func main() throws {
    let initial = try [call(1, a, first), call(2, b, first), call(3, c, second)]
    let groups = InspectorMultiCallProjection.groups(initial)
    precondition(groups.count == 2 && groups.first { $0.simulatorGroupID == first }?.calls.count == 2)
    let movedAnchor = try call(4, a, second)
    let anchorGroups = InspectorMultiCallProjection.groups(initial + [movedAnchor])
    precondition(anchorGroups.first { $0.simulatorGroupID == first }?.calls.map(\.uuid) == [b])
    precondition(Set(anchorGroups.first { $0.simulatorGroupID == second }!.calls.compactMap(\.uuid)) == [a, c])
    let movedNonAnchor = try call(4, b, second)
    let nonAnchorGroups = InspectorMultiCallProjection.groups(initial + [movedNonAnchor])
    precondition(nonAnchorGroups.first { $0.simulatorGroupID == first }?.calls.map(\.uuid) == [a])
    let split = try call(5, b, b)
    precondition(InspectorMultiCallProjection.groups(initial + [movedNonAnchor, split]).count == 3)
    let ended = try call(6, a, first, ended: true)
    precondition(!InspectorMultiCallProjection.groups(initial + [ended]).flatMap(\.calls).contains { $0.uuid == a })
    let legacy = try [call(1, a, nil), call(2, b, "invalid")]
    precondition(InspectorMultiCallProjection.groups(legacy).count == 2)
    precondition(InspectorMultiCallProjection.groups(legacy).allSatisfy { $0.simulatorGroupID == nil })

    let created = try row(1, "provider.created", ["provider": "p", "maximumCallGroups": 3, "maximumCallsPerCallGroup": 2, "apiKind": "livecommunicationkit"])
    let changed = try row(2, "provider.created", ["provider": "p", "maximumCallGroups": 4, "maximumCallsPerCallGroup": 3])
    let configuration = InspectorMultiCallProjection.configurations([created, changed])
    precondition(configuration.count == 1 && configuration[0].maximumCallGroups == 4 && configuration[0].maximumCallsPerCallGroup == 3)
    let malformed = try row(3, "provider.created", ["provider": "", "maximumCallGroups": -1])
    precondition(InspectorMultiCallProjection.configurations([malformed]).isEmpty)
    let unknownLimits = try row(3, "provider.created", ["provider": "p", "maximumCallGroups": -1])
    let unknownConfiguration = InspectorMultiCallProjection.configurations([created, unknownLimits])[0]
    precondition(unknownConfiguration.maximumCallGroups == nil && unknownConfiguration.maximumCallsPerCallGroup == nil)

    let metadata: [String: Any] = ["transactionID": first, "origin": "system", "actionIDs": ["hold", "answer"],
      "callUUIDs": [a, b], "kinds": ["CXSetHeldCallAction", "CXAnswerCallAction"], "parameters": [["held": true], [:]]]
    let delivered = try row(10, "transaction.delivered", metadata)
    let transaction = InspectorMultiCallProjection.systemTransactions([delivered])[0]
    precondition(transaction.id == first && transaction.constituents.map(\.actionID) == ["hold", "answer"])
    precondition(transaction.constituents.map(\.index) == [0, 1] && transaction.constituents[0].parameters.held == true)
    precondition(transaction.verification.contains("Unverified on device"))
    let pending = try row(12, "action.deadline", ["actionID": "answer", "transactionID": first, "callUUID": b])
    precondition(InspectorMultiCallProjection.pendingCallIDs(transactions: [delivered], pending: [pending]) == [a, b])
    precondition(InspectorMultiCallProjection.pendingCallIDs(transactions: [delivered], pending: []).isEmpty)
    let unrelated = try row(13, "action.deadline", ["actionID": "answer", "transactionID": "another-transaction", "callUUID": b])
    precondition(InspectorMultiCallProjection.pendingCallIDs(transactions: [delivered], pending: [unrelated]) == [b])
    let application = try row(10, "transaction.delivered", metadata.merging(["origin": "application"]) { _, new in new })
    precondition(InspectorMultiCallProjection.systemTransactions([application]).isEmpty)
    precondition(InspectorMultiCallProjection.pendingCallIDs(transactions: [application], pending: [pending]) == [a, b])
    let mergeMetadata = metadata.merging(["actionIDs": ["merge"], "callUUIDs": [a], "kinds": ["CXSetGroupCallAction"], "parameters": [["groupTargetUUID": c]]]) { _, new in new }
    let merge = try row(20, "transaction.delivered", mergeMetadata)
    let merging = try row(21, "action.deadline", ["actionID": "merge", "transactionID": first, "callUUID": a, "groupTargetUUID": c])
    precondition(InspectorMultiCallProjection.pendingCallIDs(transactions: [merge], pending: [merging]) == [a, c])
    for change: [String: Any] in [["callUUIDs": [a]], ["actionIDs": ["hold", "hold"]], ["kinds": ["", "CXAnswerCallAction"]], ["parameters": []]] {
      let bad = try row(10, "transaction.delivered", metadata.merging(change) { _, new in new })
      precondition(InspectorMultiCallProjection.systemTransactions([bad]).isEmpty)
      precondition(InspectorMultiCallProjection.pendingCallIDs(transactions: [bad], pending: [merging]).isSuperset(of: [a, c]))
    }
    let encoded = try JSONEncoder().encode(transaction)
    let decoded = try JSONDecoder().decode(InspectorSystemTransaction.self, from: encoded)
    precondition(decoded.constituents.count == 2)
    print("Multi-call projection tests passed")
  }
}
