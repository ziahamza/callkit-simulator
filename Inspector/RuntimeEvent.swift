import Foundation

struct RuntimeActionParameters: Codable {
  var held: Bool?
  var muted: Bool?
  var digits: String?
  var groupTargetUUID: String?
}

// Journal errors and reasons can be numeric codes or text.
enum RuntimeEventError: Codable {
  case code(Int)
  case message(String)
  var messageValue: String? {
    if case .message(let value) = self { return value }
    return nil
  }

  init(from decoder: Decoder) throws {
    let value = try decoder.singleValueContainer()
    if let code = try? value.decode(Int.self) { self = .code(code) }
    else { self = .message(try value.decode(String.self)) }
  }

  func encode(to encoder: Encoder) throws {
    var value = encoder.singleValueContainer()
    switch self {
    case .code(let code): try value.encode(code)
    case .message(let message): try value.encode(message)
    }
  }
}

struct RuntimeEvent: Codable, Identifiable {
  let schemaVersion: Int
  let sequence: Int
  let runID: String
  let pid: Int
  let bundleID: String
  let event: String
  let t_ms: Double
  var uuid: String?
  var callUUID: String?
  var actionID: String?
  var transactionID: String?
  var pushID: String?
  var provider: String?
  var capabilities: [String]?
  var availableSystemChoices: [String]?
  var maximumCallGroups: Int?
  var maximumCallsPerCallGroup: Int?
  var groupTargetUUID: String?
  var requestID: String?
  var reason: RuntimeEventError?
  var code: Int?
  var domain: String?
  var filterReason: String?
  var verification: String?
  var incomingErrorCode: Int?
  var actionIDs: [String]?
  var callUUIDs: [String]?
  var kinds: [String]?
  var parameters: [RuntimeActionParameters]?
  var kind: String?
  var origin: String?
  var apiKind: String?
  var deadlineMS: Double?
  var deadlineSeconds: Double?
  var expiryMS: Double?
  var expirySeconds: Double?
  var latencyMS: Double?
  var fulfilled: Bool?
  var mustReport: Bool?
  var error: RuntimeEventError?
  var connected: Bool?
  var ringing: Bool?
  var held: Bool?
  var ended: Bool?
  var muted: Bool?
  var callerName: String?
  var handle: String?
  var reportSucceeded: Bool {
    if case .code(0)? = error { return true }
    return false
  }
  var id: Int { sequence }
  var caller: String { [callerName, handle].compactMap { $0 }.first { !$0.isEmpty } ?? "Unknown caller" }
  var status: String {
    if apiKind == "livecommunicationkit" {
      if ended == true { return "left" }
      if held == true { return "paused" }
      if connected == true { return "joined" }
      return "idle"
    }
    if ended == true { return "Ended" }
    if held == true { return "On hold" }
    if connected == true { return "Connected" }
    return ringing == true ? "Incoming call" : "Connecting"
  }
  var title: String {
    if let state = InspectorIncomingFilterState(event: self) { return "Incoming filter · \(state.reason.title)" }
    if event == "faults.configured", let incomingErrorCode, [0, 4].contains(incomingErrorCode) {
      return incomingErrorCode == 4 ? "Injected incoming error 4" : "Incoming error injection cleared"
    }
    if event == "report.completed", let code, code != 0 { return "Incoming report rejected · code \(code)" }
    if event == "call.changed" { return "\(caller) · \(status)" }
    if event == "action.settled" { return "\(kind?.capitalized ?? "Action") \(fulfilled == true ? "fulfilled" : "failed")" }
    if event == "action.deadline" { return "\(kind?.capitalized ?? "Action") delivered" }
    return event
  }
  var detail: String {
    var parts = [origin, apiKind].compactMap { $0 }
    if let latencyMS, event != "action.deadline" { parts.append(String(format: "%.1f ms", latencyMS)) }
    if let deadlineSeconds { parts.append(String(format: "%.0f s %@", deadlineSeconds, apiKind == "livecommunicationkit" ? "reported timeoutDate" : "profile budget")) }
    if let expirySeconds { parts.append(String(format: "%.1f s effective expiry", expirySeconds)) }
    if let transactionID { parts.append("Simulator transaction \(transactionID) · modeled; unverified on LCK") }
    if let domain, !domain.isEmpty, let code { parts.append("\(domain) \(code)") }
    if verification == "unverified" { parts.append("Unverified on device") }
    if event == "faults.configured", incomingErrorCode == 4 { parts.append("Not produced by iOS 27.0 for LCK") }
    return parts.joined(separator: " · ")
  }
}
