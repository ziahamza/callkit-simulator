import Foundation

enum InspectorIncomingFilter: String, Codable, CaseIterable, Identifiable {
  case none
  case doNotDisturb = "do-not-disturb"
  case blockList = "block-list"
  case restrictedSharingMode = "restricted-sharing-mode"
  case callIsProtected = "call-is-protected"
  case sensitiveParticipants = "sensitive-participants"
  var id: String { rawValue }
  var code: Int {
    switch self {
    case .none: 0
    case .doNotDisturb: 3
    case .blockList: 4
    case .restrictedSharingMode: 5
    case .callIsProtected: 6
    case .sensitiveParticipants: 7
    }
  }
  var title: String {
    switch self {
    case .none: "No filtering"
    case .doNotDisturb: "Do Not Disturb"
    case .blockList: "Blocked caller"
    case .restrictedSharingMode: "Restricted sharing mode"
    case .callIsProtected: "Protected call"
    case .sensitiveParticipants: "Sensitive participants"
    }
  }
  var verification: String { self == .none ? "not-applicable" : self == .doNotDisturb ? "verified" : "unverified" }
}

struct InspectorIncomingFilterState: Codable, Equatable {
  let reason: InspectorIncomingFilter
  let code: Int
  let verification: String
  var isValid: Bool { code == reason.code && verification == reason.verification }
  init?(event: RuntimeEvent) {
    guard event.event == "incomingFilter.configured", let raw = event.reason?.messageValue,
          let reason = InspectorIncomingFilter(rawValue: raw), let code = event.code,
          let verification = event.verification, event.requestID?.isEmpty == false else { return nil }
    self.reason = reason; self.code = code; self.verification = verification
    guard isValid else { return nil }
  }
}
