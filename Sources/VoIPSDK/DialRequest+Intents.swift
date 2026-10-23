#if canImport(Intents)
import Intents

extension DialRequest {
  /// The RFC carries one destination; ambiguous or unresolved recipients stay
  /// with the host app rather than silently choosing someone to call.
  @available(iOS 14.5, macOS 12.0, watchOS 7.3, *)
  public init?(intent: INStartCallIntent) {
    let contacts = intent.contacts ?? []
    let recipients = contacts.isEmpty ? (intent.callRecordToCallBack?.participants ?? []) : contacts
    guard recipients.count == 1, let handle = recipients[0].personHandle?.value,
          Self.isUsableHandle(handle) else { return nil }
    self.init(handle: handle, video: intent.callCapability == .videoCall)
  }
}
#endif
