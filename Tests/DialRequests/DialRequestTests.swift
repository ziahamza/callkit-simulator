import Foundation
import Intents

private enum Failure: Error { case check(String) }
@MainActor private func expect(_ value: @autoclosure () -> Bool, _ message: String) throws {
  if !value() { throw Failure.check(message) }
}

@main
struct DialRequestTests {
  @MainActor static func main() throws {
    try expect(DialRequest(telURL: URL(string: "tel:%2B15555550142")!) == .init(handle: "+15555550142", video: false),
               "tel requests decode the destination without starting a call")
    for value in ["https://example.com", "tel:", "tel://example.com/123", "tel:123?video=1", "tel:123#fragment", "tel:%00", "tel:%0A123"] {
      try expect(DialRequest(telURL: URL(string: value)!) == nil, "unrelated or malformed URLs remain unhandled: " + value)
    }
    let person = INPerson(personHandle: INPersonHandle(value: "+15555550142", type: .phoneNumber),
                          nameComponents: nil, displayName: nil, image: nil, contactIdentifier: nil, customIdentifier: nil)
    let intent = INStartCallIntent(callRecordFilter: nil, callRecordToCallBack: nil,
      audioRoute: .unknown, destinationType: .normal, contacts: [person], callCapability: .videoCall)
    try expect(DialRequest(intent: intent) == .init(handle: "+15555550142", video: true), "public intent maps its handle and video capability")
    for contacts in [[], [person, person]] {
      let ambiguous = INStartCallIntent(callRecordFilter: nil, callRecordToCallBack: nil,
        audioRoute: .unknown, destinationType: .normal, contacts: contacts, callCapability: .audioCall)
      try expect(DialRequest(intent: ambiguous) == nil, "unresolved or multi-party intents cannot silently select a recipient")
    }
    // Apple's public NS_REFINED_FOR_SWIFT Objective-C initializer imports with
    // this label; no private selector or mutable framework state is involved.
    let record = INCallRecord(__identifier: "redial", dateCreated: nil, callRecordType: .missed,
      callCapability: .audioCall, callDuration: nil, unseen: nil, participants: [person],
      numberOfCalls: nil, isCallerIdBlocked: nil)
    let redial = INStartCallIntent(callRecordFilter: nil, callRecordToCallBack: record,
      audioRoute: .unknown, destinationType: .normal, contacts: nil, callCapability: .audioCall)
    try expect(DialRequest(intent: redial) == .init(handle: "+15555550142", video: false), "redial uses the public call record participant")
    let router = DialRequestRouter()
    let first = DialRequest(handle: "+15555550142", video: false)
    let second = DialRequest(handle: "alice@example.com", video: true)
    router.receive(first)
    router.receive(second)
    var received: [DialRequest] = []
    let observer = router.observe { received.append($0) }
    try expect(received == [first, second], "cold requests wait for a consumer, then drain in arrival order")
    router.removeObserver(observer)
    router.receive(first)
    try expect(received == [first, second], "requests received during reload stay native")
    let replacement = router.observe { received.append($0) }
    try expect(received == [first, second, first], "the next JS owner receives the buffered request")
    router.removeObserver(observer)
    router.receive(second)
    try expect(received == [first, second, first, second], "old teardown cannot detach the newer owner")
    router.removeObserver(replacement)
    router.receive(first); router.receive(second)
    let detaching = UUID()
    _ = router.observe(id: detaching) { request in
      received.append(request)
      router.removeObserver(detaching)
    }
    try expect(received.last == first, "reentrant teardown stops the drain")
    var remainder: [DialRequest] = []
    _ = router.observe { remainder.append($0) }
    try expect(remainder == [second], "reentrant teardown leaves undelivered work for the next owner")
    print("{\"success\":true,\"checks\":[\"tel-payload-and-rejection\",\"public-intent-video\",\"ambiguous-intent-rejection\",\"public-record-redial\",\"cold-buffer-fifo\",\"reload-buffer\",\"stale-owner-detach\",\"reentrant-detach-retains-pending\"]}")
  }
}
