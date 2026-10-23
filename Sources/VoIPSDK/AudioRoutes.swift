import Foundation

public struct AudioRoute: Codable, Sendable, Equatable, Identifiable {
  public let id: String
  public let name: String
  /// Raw OS port type; the explicit speaker override target uses Speaker.
  public let type: String
  public init(id: String, name: String, type: String) { self.id = id; self.name = name; self.type = type }
}

public struct AudioRoutesSnapshot: Codable, Sendable, Equatable {
  public let revision: UInt64
  public let routes: [AudioRoute]
  public let current: AudioRoute?
  public init(revision: UInt64, routes: [AudioRoute], current: AudioRoute?) {
    self.revision = revision; self.routes = routes; self.current = current
  }
  private enum CodingKeys: String, CodingKey { case revision, routes, current }
  public func encode(to encoder: any Encoder) throws {
    var values = encoder.container(keyedBy: CodingKeys.self)
    try values.encode(revision, forKey: .revision); try values.encode(routes, forKey: .routes)
    if let current { try values.encode(current, forKey: .current) }
    else { try values.encodeNil(forKey: .current) }
  }
}

public enum AudioRouteError: Error, Sendable, Equatable {
  case audioSessionInactive, unknownRoute, unsupported
}

struct AudioRoutePort: Sendable, Equatable {
  let uid: String
  let name: String
  let type: String
}
enum AudioRouteSelection: Sendable, Equatable { case speaker, input(String) }

/// Projects only observed inputs/current output. It never invents other output devices.
@MainActor
final class AudioRoutesStore {
  static let speakerID = "speaker"
  private var selections: [String: AudioRouteSelection] = [:]
  private var observers: [UUID: @MainActor (AudioRoutesSnapshot) -> Void] = [:]
  private var publishing = false
  private(set) var snapshot = AudioRoutesSnapshot(revision: 0, routes: [], current: nil)

  @discardableResult
  func observe(_ callback: @escaping @MainActor (AudioRoutesSnapshot) -> Void) -> UUID {
    let id = UUID(); observers[id] = callback; callback(snapshot); return id
  }
  func removeObserver(_ id: UUID) { observers.removeValue(forKey: id) }

  func update(inputs: [AudioRoutePort], outputs: [AudioRoutePort]) {
    let speaker = AudioRoute(id: Self.speakerID, name: "Speaker", type: "Speaker")
    var routes: [AudioRoute] = []
    var available: [String: AudioRouteSelection] = [:]
    for input in inputs {
      let id = "input:" + input.uid
      guard available[id] == nil else { continue }
      routes.append(AudioRoute(id: id, name: input.name, type: input.type))
      available[id] = .input(input.uid)
    }
    routes.append(speaker); available[speaker.id] = .speaker
    let current = outputs.first.map { AudioRoute(id: "output:" + $0.uid, name: $0.name, type: $0.type) }
    // Current is the observed output itself, never a paired input or a command
    // target. An OS observation does not imply arbitrary output selection exists.
    if let current { routes.append(current) }
    selections = available
    guard routes != snapshot.routes || current != snapshot.current else { return }
    snapshot = AudioRoutesSnapshot(revision: snapshot.revision &+ 1, routes: routes, current: current)
    guard !publishing else { return }
    publishing = true
    var value = snapshot
    while true {
      for callback in Array(observers.values) { callback(value) }
      guard value.revision != snapshot.revision else { break }
      value = snapshot
    }
    publishing = false
  }

  func selection(_ id: String, isActive: Bool) throws -> AudioRouteSelection? {
    guard isActive else { throw AudioRouteError.audioSessionInactive }
    guard let selection = selections[id] else {
      // The already-current output needs no unavailable arbitrary-output API.
      if snapshot.current?.id == id { return nil }
      throw AudioRouteError.unknownRoute
    }
    return selection
  }
}
