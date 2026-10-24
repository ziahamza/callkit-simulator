import Foundation

struct RuntimeActionParameters: Codable {
  var held: Bool?
  var muted: Bool?
  var digits: String?
  var groupTargetUUID: String?
}

// Report completions use numeric error codes; control receipts use error text.
enum RuntimeEventError: Codable {
  case code(Int)
  case message(String)

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
  var actionIDs: [String]?
  var callUUIDs: [String]?
  var kinds: [String]?
  var parameters: [RuntimeActionParameters]?
  var kind: String?
  var origin: String?
  var apiKind: String?
  var deadlineMS: Double?
  var deadlineSeconds: Double?
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
    if ended == true { return "Ended" }
    if held == true { return "On hold" }
    if connected == true { return "Connected" }
    return ringing == true ? "Incoming call" : "Connecting"
  }
  var title: String {
    if event == "call.changed" { return "\(caller) · \(status)" }
    if event == "action.settled" { return "\(kind?.capitalized ?? "Action") \(fulfilled == true ? "fulfilled" : "failed")" }
    if event == "action.deadline" { return "\(kind?.capitalized ?? "Action") delivered" }
    return event
  }
  var detail: String {
    var parts = [origin, apiKind].compactMap { $0 }
    if let latencyMS, event != "action.deadline" { parts.append(String(format: "%.1f ms", latencyMS)) }
    if let deadlineSeconds { parts.append(String(format: "%.0f s profile budget", deadlineSeconds)) }
    return parts.joined(separator: " · ")
  }
}
