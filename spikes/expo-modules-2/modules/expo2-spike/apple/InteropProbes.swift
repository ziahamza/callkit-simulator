import ExpoModulesCore

@Record struct PingEvent: Sendable { let label: String }
@SharedObject final class PingProbe: SharedObject {
  private let label: String
  @JS init(_ label: String) { self.label = label; super.init() }
  @Event var onPing: (PingEvent) -> Void
  @JS func ping() { onPing(PingEvent(label: label)) }
}
@Record struct ClosureProbe: Sendable {
  let times: [Double]
  let threads: [ThreadSample]
  var counts: [Int]
}
