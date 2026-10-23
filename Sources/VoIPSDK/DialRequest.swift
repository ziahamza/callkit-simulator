import Foundation

/// A request to present an outgoing destination. The app decides whether to dial.
public struct DialRequest: Codable, Sendable, Equatable {
  public let handle: String
  public let video: Bool

  public init(handle: String, video: Bool) {
    self.handle = handle; self.video = video
  }

  public init?(telURL url: URL) {
    guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
          components.scheme?.lowercased() == "tel", components.host == nil,
          components.query == nil, components.fragment == nil,
          let handle = components.percentEncodedPath.removingPercentEncoding,
          Self.isUsableHandle(handle) else { return nil }
    self.init(handle: handle, video: false)
  }

  static func isUsableHandle(_ value: String) -> Bool {
    !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty &&
      !value.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) })
  }
}

/// Process-owned ingress, independent of a conversation manager or JS lifetime.
/// Only the current consumer receives requests; replacing it preserves queued work.
@MainActor
public final class DialRequestRouter {
  public static let shared = DialRequestRouter()
  private var pending: [DialRequest] = []
  private var consumer: (id: UUID, callback: @MainActor (DialRequest) -> Void)?
  private var draining = false

  public init() {}

  @discardableResult
  public func observe(id: UUID = UUID(), _ callback: @escaping @MainActor (DialRequest) -> Void) -> UUID {
    consumer = (id, callback)
    drain()
    return id
  }

  public func removeObserver(_ id: UUID) {
    if consumer?.id == id { consumer = nil }
  }

  func receive(_ request: DialRequest) {
    pending.append(request)
    drain()
  }

  private func drain() {
    guard !draining else { return }
    draining = true
    defer { draining = false }
    while let consumer, !pending.isEmpty {
      let request = pending.removeFirst()
      consumer.callback(request)
    }
  }
}
