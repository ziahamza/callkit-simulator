#if targetEnvironment(simulator)
import SwiftUI
@preconcurrency import CallKit
import AVFAudio
@preconcurrency import PushKit

@MainActor
final class Fixture: NSObject, UIApplicationDelegate, @preconcurrency CXProviderDelegate, @preconcurrency PKPushRegistryDelegate {
  var provider: CXProvider!
  var background: UIBackgroundTaskIdentifier = .invalid
  let controller = CXCallController(queue: .main)
  var registry: PKPushRegistry!
  func application(_ app: UIApplication, didFinishLaunchingWithOptions options: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
    guard RuntimeBootstrap.activate() else { fatalError("Fixture runtime refused") }
    let config = CXProviderConfiguration()
    config.maximumCallGroups = 3
    config.maximumCallsPerCallGroup = 3
    provider = CXProvider(configuration: config)
    provider.setDelegate(self, queue: .main)
    registry = PKPushRegistry(queue: .main)
    registry.delegate = self
    registry.desiredPushTypes = [.voIP]
    background = app.beginBackgroundTask(withName: "Inspector acceptance") {}
    return true
  }
  func ring() {
    let update = CXCallUpdate()
    update.localizedCallerName = "Alex Morgan"
    update.remoteHandle = CXHandle(type: .generic, value: "alex@example.test")
    update.supportsHolding = true
    provider.reportNewIncomingCall(with: UUID(), update: update) { error in
      if let error { NSLog("Fixture report failed: %@", String(describing: error)) }
    }
  }
  func outgoing() {
    controller.request(CXTransaction(action: CXStartCallAction(call: UUID(), handle: CXHandle(type: .generic, value: "Taylor")))) { _ in }
  }
  func providerDidReset(_ provider: CXProvider) {}
  func pushRegistry(_ registry: PKPushRegistry, didUpdate pushCredentials: PKPushCredentials, for type: PKPushType) {}
  func pushRegistry(_ registry: PKPushRegistry, didReceiveIncomingPushWith payload: PKPushPayload, for type: PKPushType, completion: @escaping () -> Void) {
    ring()
    if payload.dictionaryPayload["operation"] as? String == "outgoing" { outgoing() }
    completion()
  }
  func provider(_ provider: CXProvider, execute transaction: CXTransaction) -> Bool {
    for action in transaction.actions {
      DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
        if action is CXPlayDTMFCallAction { action.fail() } else { action.fulfill() }
        if let start = action as? CXStartCallAction {
          let update = CXCallUpdate()
          update.localizedCallerName = "Taylor Reed"
          provider.reportCall(with: start.callUUID, updated: update)
          provider.reportOutgoingCall(with: start.callUUID, startedConnectingAt: Date())
          provider.reportOutgoingCall(with: start.callUUID, connectedAt: Date())
        }
      }
    }
    return true
  }
}
@main
struct FixtureApp: App {
  @UIApplicationDelegateAdaptor(Fixture.self) var fixture
  var body: some Scene {
    WindowGroup {
      VStack(spacing: 20) {
        Image(systemName: "phone.connection.fill").font(.largeTitle)
        Text("Runtime event fixture").font(.title)
        Button("Incoming call") { fixture.ring() }
      }
    }
  }
}
#endif
