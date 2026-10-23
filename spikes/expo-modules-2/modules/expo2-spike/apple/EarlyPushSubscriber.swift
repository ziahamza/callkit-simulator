import ExpoModulesCore
import Foundation
import PushKit
import QuartzCore
import os

@Record struct StartupReceipt: Sendable {
  let installedMs: Double
  let firstBundleStartMs: Double
  let firstReceiptLocked: Bool
  let delegateInstalled: Bool
  let observedLoads: Int
}
final class StartupLedger: Sendable {
  static let shared = StartupLedger()
  private struct State: Sendable {
    var installedMs = 0.0
    var installed = false
    var first: (start: Double, valid: Bool)?
    var loads = 0
  }
  private let state = OSAllocatedUnfairLock(initialState: State())
  func install(valid: Bool) {
    state.withLock { guard $0.installedMs == 0 else { return }; $0.installed = valid; $0.installedMs = handlerNow() }
  }
  func loaded(start: Double, valid: Bool) {
    state.withLock { $0.loads += 1; if $0.first == nil { $0.first = (start, $0.installed && valid) } }
  }
  func snapshot() -> StartupReceipt {
    state.withLock { StartupReceipt(installedMs: $0.installedMs, firstBundleStartMs: $0.first?.start ?? 0,
      firstReceiptLocked: $0.first != nil, delegateInstalled: $0.first?.valid ?? false, observedLoads: $0.loads) }
  }
}
@MainActor public final class EarlyPushSubscriber: ExpoAppDelegateSubscriber, PKPushRegistryDelegate {
  private var registry: PKPushRegistry?
  public func subscriberDidRegister() {
    NotificationCenter.default.addObserver(self, selector: #selector(bundleLoaded(_:)),
      name: Notification.Name("RCTJavaScriptDidLoadNotification"), object: nil)
    let registry = PKPushRegistry(queue: DispatchQueue(label: "expo2.spike.push"))
    self.registry = registry
    registry.delegate = self
    registry.desiredPushTypes = [.voIP]
    StartupLedger.shared.install(valid: registry.delegate === self)
    HandlerLedger.shared.start("native-cold", runtime: "native-launch", timeout: 1000) { _ in
      preconditionFailure("The native cold default must not dispatch to JS")
    }
  }
  @objc private func bundleLoaded(_ notification: Notification) {
    StartupLedger.shared.loaded(start: Expo2ScriptDownloadStart(notification.userInfo?["bridge"]), valid: registry?.delegate === self)
  }
  public nonisolated func pushRegistry(_ registry: PKPushRegistry, didUpdate pushCredentials: PKPushCredentials, for type: PKPushType) {}
}
