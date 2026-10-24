#if targetEnvironment(simulator)
import Foundation
import Combine

struct RuntimeSession: Codable, Equatable {
  let schemaVersion: Int
  let bundleID: String
  let runID: String
  let pid: Int
  let profile: String
}
private struct EventBatch: Decodable {
  let session: RuntimeSession
  let events: [RuntimeEvent]
  let cursor: Int
}
private struct ControlResult: Decodable {
  struct Result: Decodable {
    var actionIDs: [String]?
    var incomingFilter: InspectorIncomingFilterState?
    var incomingErrorCode: Int?
  }
  var id: String?
  let success: Bool
  var error: String?
  var result: Result?
}
private struct BridgeError: Decodable { let error: String }
private struct InspectorProbeSnapshot: Encodable {
  struct TimelinePresentation: Encodable {
    let sequence: Int
    let title: String
    let detail: String
  }
  let nonce: String
  let inspectorPID: Int32
  let wallTimeMS: Int64
  let startedWallTimeMS: Int64
  let session: RuntimeSession?
  let cursor: Int
  let incoming: RuntimeEvent?
  let conversations: [RuntimeEvent]
  let providerConfigurations: [RuntimeEvent]
  let pending: [RuntimeEvent]
  let timeline: [RuntimeEvent]
  let timelinePresentation: [TimelinePresentation]
  let timingReferences: [RuntimeTimingReadout]
  let audioActive: Bool
  let incomingFilter: InspectorIncomingFilter
  let incomingErrorCode: Int
  let online: Bool
  let error: String?
}

@MainActor
final class InspectorModel: ObservableObject {
  @Published var session: RuntimeSession?
  @Published var conversations: [String: RuntimeEvent] = [:]
  @Published var pending: [String: RuntimeEvent] = [:]
  @Published var timeline: [RuntimeEvent] = []
  @Published var audioActive = false
  @Published var online = false
  @Published var busy = false
  @Published var error: String?
  @Published var commandError: String?
  @Published var incomingFilter: InspectorIncomingFilter = .none
  @Published var incomingErrorCode = 0
  @Published private var providerRows: [String: RuntimeEvent] = [:]
  private var transactionCalls: [String: Set<String>] = [:]
  private var transactionOrder: [String] = []
  private var cursor = 0
  private var acknowledgedActions = Set<String>()
  private var actionOrder: [String] = []
  private let endpoint: URL?
  private let token: String
  private let client = URLSession(configuration: .ephemeral)
  private let probe: (nonce: String, startedWallTimeMS: Int64)?

  init(environment: [String: String] = ProcessInfo.processInfo.environment) {
    endpoint = environment["CKSIM_INSPECTOR_URL"].flatMap(URL.init(string:))
    token = environment["CKSIM_INSPECTOR_TOKEN"] ?? ""
    if let nonce = environment["CKSIM_INSPECTOR_PROBE_NONCE"], UUID(uuidString: nonce) != nil {
      probe = (nonce, Int64(Date().timeIntervalSince1970 * 1000))
    } else { probe = nil }
  }
  var incoming: RuntimeEvent? {
    conversations.values.filter { $0.ringing == true && $0.ended != true }.sorted { $0.sequence < $1.sequence }.first
  }
  var timingReferences: [RuntimeTimingReadout] { RuntimeTimingReferences.project(timeline) }
  var providerConfigurations: [RuntimeEvent] { providerRows.values.sorted { $0.sequence < $1.sequence } }
  func targets(excluding call: RuntimeEvent, held: Bool? = nil) -> [RuntimeEvent] {
    conversations.values.filter { $0.uuid != call.uuid && $0.ended != true &&
      (held == nil || ($0.connected == true && $0.held == held)) }.sorted { $0.sequence < $1.sequence }
  }
  func active(excluding id: String?) -> RuntimeEvent? {
    conversations.values.filter { $0.uuid != id && $0.connected == true && $0.held != true && $0.ended != true }
      .sorted { $0.sequence < $1.sequence }.first
  }
  func isPending(_ call: RuntimeEvent) -> Bool {
    guard let id = call.uuid else { return false }
    return pending.values.contains { $0.callUUID == id || $0.groupTargetUUID == id ||
      $0.transactionID.flatMap { transactionCalls[$0]?.contains(id) } == true }
  }

  private func request(_ path: String, body: [String: String]? = nil) async throws -> Data {
    guard let endpoint, endpoint.host == "127.0.0.1", endpoint.scheme == "http", !token.isEmpty else {
      throw NSError(domain: "Inspector", code: 1, userInfo: [NSLocalizedDescriptionKey: "Start this app with cksim inspect to connect to a runtime."])
    }
    let target = URL(string: path, relativeTo: endpoint)!
    var request = URLRequest(url: target)
    request.timeoutInterval = body == nil ? 3 : 20
    request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
    if let body {
      request.httpMethod = "POST"
      request.setValue("application/json", forHTTPHeaderField: "Content-Type")
      request.httpBody = try JSONEncoder().encode(body)
    }
    let (data, response) = try await client.data(for: request)
    guard (response as? HTTPURLResponse)?.statusCode == 200 else {
      let message = (try? JSONDecoder().decode(BridgeError.self, from: data).error) ?? "Inspector connection rejected. Restart cksim inspect."
      throw NSError(domain: "Inspector", code: 2, userInfo: [NSLocalizedDescriptionKey: message])
    }
    return data
  }
  func follow() async {
    while !Task.isCancelled {
      do {
        let batch = try JSONDecoder().decode(EventBatch.self, from: await request("/events?after=\(cursor)"))
        guard batch.session.schemaVersion == 1, session == nil || session == batch.session,
              batch.cursor == cursor + batch.events.count else { throw URLError(.cannotParseResponse) }
        for (offset, row) in batch.events.enumerated() {
          guard row.schemaVersion == 1, row.sequence == cursor + offset + 1,
                row.runID == batch.session.runID, row.pid == batch.session.pid,
                row.bundleID == batch.session.bundleID else { throw URLError(.cannotParseResponse) }
        }
        session = batch.session
        for row in batch.events { consume(row) }
        cursor = batch.cursor
        online = true
        error = nil
      } catch {
        if Task.isCancelled { return }
        online = false
        self.error = error.localizedDescription
      }
      writeProbe()
      try? await Task.sleep(for: .milliseconds(250))
    }
  }
  private func writeProbe() {
    guard let probe else { return }
    guard let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first else { return }
    let snapshot = InspectorProbeSnapshot(nonce: probe.nonce, inspectorPID: ProcessInfo.processInfo.processIdentifier,
      wallTimeMS: Int64(Date().timeIntervalSince1970 * 1000), startedWallTimeMS: probe.startedWallTimeMS,
      session: session, cursor: cursor, incoming: incoming,
      conversations: conversations.values.sorted { $0.sequence < $1.sequence },
      providerConfigurations: providerConfigurations,
      pending: pending.values.sorted { $0.sequence < $1.sequence }, timeline: timeline,
      timelinePresentation: timeline.map { .init(sequence: $0.sequence, title: $0.title, detail: $0.detail) },
      timingReferences: timingReferences,
      audioActive: audioActive, incomingFilter: incomingFilter, incomingErrorCode: incomingErrorCode, online: online, error: error)
    do {
      try FileManager.default.createDirectory(at: documents, withIntermediateDirectories: true)
      try JSONEncoder().encode(snapshot).write(to: documents.appendingPathComponent("cksim-inspector-results.json"), options: .atomic)
    } catch { NSLog("Inspector probe receipt failed: %@", String(describing: error)) }
  }
  private func consume(_ row: RuntimeEvent) {
    timeline.append(row)
    if timeline.count > 300 { timeline.removeFirst(timeline.count - 300) }
    if row.event == "provider.created", let id = row.provider { providerRows[id] = row }
    if row.event == "provider.apiKind", let id = row.provider, var value = providerRows[id] {
      value.apiKind = row.apiKind; providerRows[id] = value
    }
    if row.event == "transaction.delivered", let id = row.transactionID,
       let calls = row.callUUIDs, calls.count == row.actionIDs?.count, !calls.isEmpty {
      if transactionCalls[id] == nil { transactionOrder.append(id) }
      transactionCalls[id] = Set(calls)
      trimTransactions(protecting: id)
    }
    if row.event == "call.changed", let id = row.uuid {
      conversations[id] = row
      let ended = conversations.values.filter { $0.ended == true }.sorted { $0.sequence > $1.sequence }
      for old in ended.dropFirst(5) { if let id = old.uuid { conversations.removeValue(forKey: id) } }
    }
    if let state = InspectorIncomingFilterState(event: row), let id = row.requestID {
      incomingFilter = state.reason
      acknowledge(id)
    }
    if row.event == "faults.configured", let code = row.incomingErrorCode, [0, 4].contains(code),
       let id = row.requestID, !id.isEmpty {
      incomingErrorCode = code
      acknowledge(id)
    }
    if let id = row.actionID {
      acknowledge(id)
      if row.event == "action.deadline" { pending[id] = row }
      if ["action.settled", "action.expired", "action.timeout", "action.cancelled"].contains(row.event) {
        pending.removeValue(forKey: id); trimTransactions()
      }
    }
    if row.event == "audio.activated" { audioActive = true }
    if row.event == "audio.deactivated" { audioActive = false }
  }
  private func trimTransactions(protecting id: String? = nil) {
    let active = Set(pending.values.compactMap(\.transactionID))
    while transactionOrder.count > 100 {
      guard let index = transactionOrder.firstIndex(where: { $0 != id && !active.contains($0) }) else { return }
      transactionCalls.removeValue(forKey: transactionOrder.remove(at: index))
    }
  }
  private func acknowledge(_ id: String) {
    if acknowledgedActions.insert(id).inserted {
      actionOrder.append(id)
      if actionOrder.count > 1000 { acknowledgedActions.remove(actionOrder.removeFirst()) }
    }
  }
  func configureIncomingFilter(_ reason: InspectorIncomingFilter) {
    submitConfiguration(["operation": "incoming-filter", "reason": reason.rawValue]) {
      $0.incomingFilter?.isValid == true && $0.incomingFilter?.reason == reason
    }
  }
  func configureIncomingError(_ code: Int) {
    guard [0, 4].contains(code) else { return }
    submitConfiguration(["operation": "faults", "incomingErrorCode": String(code)]) { $0.incomingErrorCode == code }
  }
  private func submitConfiguration(_ body: [String: String], accepting: @escaping (ControlResult.Result) -> Bool) {
    guard !busy, online else { return }
    busy = true
    Task {
      defer { busy = false }
      do {
        let response = try JSONDecoder().decode(ControlResult.self, from: await request("/command",
          body: body))
        guard response.success, let id = response.id, !id.isEmpty, let result = response.result, accepting(result) else {
          throw NSError(domain: "Inspector", code: 5, userInfo: [NSLocalizedDescriptionKey:
            response.error ?? "Runtime rejected the configuration."])
        }
        let deadline = Date().addingTimeInterval(3)
        while !acknowledgedActions.contains(id) {
          guard Date() < deadline else {
            throw NSError(domain: "Inspector", code: 6, userInfo: [NSLocalizedDescriptionKey:
              "Configuration accepted, but its journal confirmation has not arrived. Check the timeline before retrying."])
          }
          try await Task.sleep(for: .milliseconds(25))
        }
      } catch { commandError = error.localizedDescription }
    }
  }
  func send(_ operation: String, call: RuntimeEvent, active: RuntimeEvent? = nil,
            heldCall: RuntimeEvent? = nil, held: Bool? = nil, groupTarget: RuntimeEvent? = nil,
            applicationRequest: Bool = false) {
    guard !busy, online, let id = call.uuid, !isPending(call),
          [active, heldCall, groupTarget].compactMap({ $0 }).allSatisfy({ !isPending($0) }) else { return }
    busy = true
    Task {
      defer { busy = false }
      do {
        var command = ["operation": operation, "callUUID": id]
        if applicationRequest { command["applicationRequest"] = "true" }
        if let activeID = active?.uuid { command["activeCallUUID"] = activeID }
        if let heldID = heldCall?.uuid { command["heldCallUUID"] = heldID }
        if let held { command["held"] = held ? "true" : "false" }
        if let groupID = groupTarget?.uuid { command["groupWithCallUUID"] = groupID }
        let response = try JSONDecoder().decode(ControlResult.self, from: await request("/command", body: command))
        if !response.success {
          throw NSError(domain: "Inspector", code: 3, userInfo: [NSLocalizedDescriptionKey: response.error ?? "Runtime rejected this call action."])
        }
        guard let ids = response.result?.actionIDs, !ids.isEmpty else { throw URLError(.cannotParseResponse) }
        let deadline = Date().addingTimeInterval(3)
        while !Set(ids).isSubset(of: acknowledgedActions) {
          guard Date() < deadline else {
            throw NSError(domain: "Inspector", code: 4, userInfo: [NSLocalizedDescriptionKey:
              "The control was accepted but its events have not arrived. Its outcome is unknown; check the timeline before retrying."])
          }
          try await Task.sleep(for: .milliseconds(25))
        }
      } catch { self.commandError = error.localizedDescription }
    }
  }
}
#endif
