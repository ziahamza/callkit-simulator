import Foundation

struct InspectorConversationGroup: Codable, Identifiable {
  let id: String
  let simulatorGroupID: String?
  let calls: [RuntimeEvent]
}
struct InspectorProviderConfiguration: Codable, Identifiable {
  let provider: String
  let maximumCallGroups: Int?
  let maximumCallsPerCallGroup: Int?
  let apiKind: String?
  var id: String { provider }
}
struct InspectorTransactionConstituent: Codable, Identifiable {
  let index: Int
  let actionID: String
  let callUUID: String
  let kind: String
  let parameters: RuntimeActionParameters
  var id: String { actionID }
}
struct InspectorSystemTransaction: Codable, Identifiable {
  let id: String
  let sequence: Int
  let constituents: [InspectorTransactionConstituent]
  let callUUIDs: [String]
  let verification: String
}

enum InspectorMultiCallProjection {
  static func groups(_ calls: [RuntimeEvent]) -> [InspectorConversationGroup] {
    var latest: [String: RuntimeEvent] = [:]
    for call in calls where call.event == "call.changed" {
      guard let id = call.uuid, !id.isEmpty else { continue }
      if latest[id] == nil || latest[id]!.sequence < call.sequence { latest[id] = call }
    }
    let live = latest.values.filter { $0.ended != true }
    let grouped = Dictionary(grouping: live) { call in
      call.group.flatMap(UUID.init(uuidString:)).map { "group:" + $0.uuidString } ?? "call:" + call.uuid!
    }
    return grouped.map { key, calls in
      .init(id: key, simulatorGroupID: key.hasPrefix("group:") ? String(key.dropFirst(6)) : nil,
            calls: calls.sorted { $0.uuid! < $1.uuid! })
    }.sorted { $0.id < $1.id }
  }

  static func configurations(_ providerRows: [RuntimeEvent]) -> [InspectorProviderConfiguration] {
    var latest: [String: RuntimeEvent] = [:]
    for row in providerRows where row.event == "provider.created" {
      guard let id = row.provider, !id.isEmpty else { continue }
      if latest[id] == nil || latest[id]!.sequence < row.sequence { latest[id] = row }
    }
    return latest.map { provider, row in
      .init(provider: provider, maximumCallGroups: row.maximumCallGroups.flatMap { $0 > 0 ? $0 : nil },
        maximumCallsPerCallGroup: row.maximumCallsPerCallGroup.flatMap { $0 > 0 ? $0 : nil }, apiKind: row.apiKind)
    }.sorted { $0.provider < $1.provider }
  }

  static func systemTransactions(_ rows: [RuntimeEvent]) -> [InspectorSystemTransaction] {
    rows.filter { $0.origin == "system" }.compactMap(transaction).sorted { $0.sequence > $1.sequence }
  }

  static func pendingCallIDs(transactions: [RuntimeEvent], pending: [RuntimeEvent]) -> Set<String> {
    var calls = Set(pending.flatMap { [$0.callUUID, $0.groupTargetUUID].compactMap { $0 }.filter { !$0.isEmpty } })
    let pendingIDs = Set(pending.compactMap(\.actionID))
    let pendingTransactions = Set(pending.compactMap(\.transactionID))
    for row in transactions {
      guard let value = transaction(row), pendingTransactions.contains(value.id),
            value.constituents.contains(where: { pendingIDs.contains($0.actionID) }) else { continue }
      calls.formUnion(value.callUUIDs)
    }
    return calls
  }

  private static func transaction(_ row: RuntimeEvent) -> InspectorSystemTransaction? {
    guard row.event == "transaction.delivered", let id = row.transactionID, !id.isEmpty,
          let actions = row.actionIDs, !actions.isEmpty, actions.allSatisfy({ !$0.isEmpty }),
          Set(actions).count == actions.count, let calls = row.callUUIDs, let kinds = row.kinds,
          let parameters = row.parameters, calls.count == actions.count, kinds.count == actions.count,
          parameters.count == actions.count, calls.allSatisfy({ !$0.isEmpty }), kinds.allSatisfy({ !$0.isEmpty }) else { return nil }
    let constituents = actions.indices.map { index in
      InspectorTransactionConstituent(index: index, actionID: actions[index], callUUID: calls[index],
                                      kind: kinds[index], parameters: parameters[index])
    }
    let affected = Set(calls + parameters.compactMap(\.groupTargetUUID).filter { !$0.isEmpty })
    return .init(id: id, sequence: row.sequence, constituents: constituents, callUUIDs: affected.sorted(),
                 verification: "Unverified on device · simulator timing and ordering")
  }
}
