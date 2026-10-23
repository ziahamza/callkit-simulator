import Foundation

/// JSON without untyped, non-Sendable dictionaries at the native/JavaScript boundary.
public enum JSONValue: Codable, Sendable, Equatable {
  case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null

  public init(from decoder: any Decoder) throws {
    let value = try decoder.singleValueContainer()
    if value.decodeNil() { self = .null }
    else if let x = try? value.decode(Bool.self) { self = .bool(x) }
    else if let x = try? value.decode(String.self) { self = .string(x) }
    else if let x = try? value.decode(Double.self) { self = .number(x) }
    else if let x = try? value.decode([String: JSONValue].self) { self = .object(x) }
    else { self = .array(try value.decode([JSONValue].self)) }
  }

  public func encode(to encoder: any Encoder) throws {
    var value = encoder.singleValueContainer()
    switch self {
    case .string(let x): try value.encode(x)
    case .number(let x): try value.encode(x)
    case .bool(let x): try value.encode(x)
    case .object(let x): try value.encode(x)
    case .array(let x): try value.encode(x)
    case .null: try value.encodeNil()
    }
  }
}

public struct Participant: Codable, Sendable, Equatable {
  public var id: String
  public var displayName: String?
  public var avatarUrl: String?
  public var phoneNumber: String?
  public var email: String?

  public init(id: String, displayName: String? = nil, avatarUrl: String? = nil,
              phoneNumber: String? = nil, email: String? = nil) {
    self.id = id; self.displayName = displayName; self.avatarUrl = avatarUrl
    self.phoneNumber = phoneNumber; self.email = email
  }
}

public struct IncomingCallPayload: Codable, Sendable, Equatable {
  public var eventId: String
  public var serverCallId: String
  public var caller: Participant
  public var hasVideo: Bool
  /// The original RFC3339 wire value, when supplied by the transport.
  public var startedAt: String?
  public var metadata: [String: JSONValue]?

  public init(eventId: String, serverCallId: String, caller: Participant, hasVideo: Bool = false,
              startedAt: String? = nil, metadata: [String: JSONValue]? = nil) {
    self.eventId = eventId; self.serverCallId = serverCallId; self.caller = caller
    self.hasVideo = hasVideo; self.startedAt = startedAt; self.metadata = metadata
  }
}

public struct CallEndedPayload: Codable, Sendable, Equatable {
  public var eventId: String
  public var serverCallId: String
  public var reason: ConversationEndedReason
  public init(eventId: String, serverCallId: String, reason: ConversationEndedReason = .remoteEnded) {
    self.eventId = eventId; self.serverCallId = serverCallId; self.reason = reason
  }
}

public enum ConversationDirection: String, Codable, Sendable { case incoming, outgoing }
public enum ConversationStatus: String, Codable, Sendable {
  case ringing, dialing, connecting, connected, held, ending, ended
}
public enum ConversationEndedReason: String, Codable, Sendable {
  case local, remoteEnded, failed, unanswered, answeredElsewhere, declinedElsewhere
}
public struct ConversationCapabilities: Codable, Sendable, Equatable {
  public var hold: Bool
  public var merge: Bool
  public var tones: Bool
  public var video: Bool
  public init(hold: Bool = true, merge: Bool = true, tones: Bool = true, video: Bool = true) {
    self.hold = hold; self.merge = merge; self.tones = tones; self.video = video
  }
}
public struct ConversationEnded: Codable, Sendable, Equatable {
  public var at: Date
  public var reason: ConversationEndedReason
  public init(at: Date, reason: ConversationEndedReason) { self.at = at; self.reason = reason }
}
public enum ConversationActionKind: String, Codable, Sendable {
  case join, start, end, hold, mute, tones, merge, unmerge
}
public enum ConversationActionOrigin: String, Codable, Sendable { case system, app }
public struct PendingAction: Codable, Sendable, Equatable {
  public var id: UUID
  public var kind: ConversationActionKind
  /// Observed pending actions expose the application's deadline, before platform timeout.
  public var deadline: Date
  public var origin: ConversationActionOrigin
  public var held: Bool?
  public var muted: Bool?
  public var digits: String?
  public init(id: UUID, kind: ConversationActionKind, deadline: Date,
              origin: ConversationActionOrigin, held: Bool? = nil, muted: Bool? = nil,
              digits: String? = nil) {
    self.id = id; self.kind = kind; self.deadline = deadline; self.origin = origin
    self.held = held; self.muted = muted; self.digits = digits
  }
}
public struct ConversationSnapshot: Codable, Sendable, Equatable, Identifiable {
  public var id: UUID
  public var direction: ConversationDirection
  public var status: ConversationStatus
  public var members: [Participant]
  public var capabilities: ConversationCapabilities
  public var isMuted: Bool
  public var isVideo: Bool
  public var connectedAt: Date?
  public var ended: ConversationEnded?
  public var payload: IncomingCallPayload?
  public var pending: PendingAction?
}
public enum ConversationReport: Sendable {
  case connecting
  case connected
  case updated(members: [Participant]?, video: Bool?)
  case ended(ConversationEndedReason)
}
public enum IncomingReportSource: Sendable { case foreground, pushKit }
public enum UnhandledActionPolicy: String, Codable, Sendable { case fail, accept }
public enum ConversationClientError: Error, Sendable, Equatable {
  case alreadyConfigured, invalidConfiguration, invalidPayload, unknownConversation, unsupported, invalidDigits, duplicateSDKLinkage
}

public struct ConversationStateUpdate: Codable, Sendable, Equatable {
  public let revision: UInt64
  public let conversations: [ConversationSnapshot]
}

public enum ConversationEvent: String, Codable, Sendable {
  case audioActivated, audioDeactivated, audioActivationMissing, audioActivationFailed, providerReset, providerOwnershipLost
}

public struct ConversationEventContext: Codable, Sendable, Equatable {
  public let type: ConversationEvent
  /// Absent for process-wide system callbacks and reset/ownership events.
  public let conversationID: UUID?
  public init(type: ConversationEvent, conversationID: UUID? = nil) {
    self.type = type; self.conversationID = conversationID
  }
}
